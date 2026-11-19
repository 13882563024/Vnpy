"""单写入者的离线纸盘桥。查询由网关定时器/fixture 推进；本模块不连接、重发或撤单。

source_journal 是已生成意图的独立策略库，不是柜台事实库。每个账户始终使用同一份
source_journal。unknown 冻结账户，必须人工核对；submitted 仅表示本地返回单号。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from math import isfinite
from time import monotonic

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderRequest

from .flat_check import FirstEntryLimits
from .held_check import HeldLimits
from .intents import OrderIntent
from .journal import Outcome
from .writer_guard import AccountWriter


@dataclass(frozen=True)
class RuntimeValidation:
    """回调在完整查询后读取当时的配置和报价，不得返回决策生成时缓存的门禁。"""
    config_version: str
    limits: FirstEntryLimits | HeldLimits


@dataclass(frozen=True)
class BridgeResult:
    state: str
    reason: str
    order_id: str = ''


class RuntimeBridge:
    def __init__(self, main_engine, *, gateway_name, source_journal, account, strategy,
                 mode, revalidate, timeout=12, clock=monotonic):
        # 不把字符串或 shadow_only=False 作为使用真实 MainEngine 的许可证。
        from gateway.paper_spread import PaperMainEngine
        from gateway.ctp_spread_gateway import SpreadCtpTdApi
        if type(main_engine) is not PaperMainEngine or mode not in ('simulation', 'paper'):
            raise ValueError('仅允许无网络纸盘 fixture 引擎')
        if not callable(revalidate) or type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0:
            raise ValueError('缺少时序重验回调或核验时限无效')
        self.engine, self.name, self.mode = main_engine, gateway_name, mode
        self.gateway = main_engine.get_gateway(gateway_name)
        self.td = self.gateway.td_api
        if not isinstance(self.td, SpreadCtpTdApi):
            raise ValueError('需要完整的 SpreadCtpTdApi 核验路径')
        self.source, self.account, self.strategy = source_journal, account, strategy
        self._check_fixture()
        self.clock, self.timeout, self.revalidate = clock, timeout, revalidate
        self._owner = AccountWriter(source_journal.path.parent, 'runtime-bridge:' + account)
        self._owner.acquire()
        self._closed = self._processing = False
        self._intent = self._round = None
        self.status = BridgeResult('idle', '尚无意图')
        self._blocked = ''
        try:
            self._recover()
        except BaseException:
            self._owner.close()
            raise

    def _check_fixture(self):
        if (self.engine.get_gateway(self.name) is not self.gateway or self.gateway.td_api is not self.td
                or getattr(self.gateway, 'runtime_fixture_mode', None) != self.mode
                or self.td.connect_status is not False):
            raise ValueError('纸盘路由改变或柜台已连接')
        if self.source.path.samefile(self.td.journal.path):
            raise ValueError('策略来源库与柜台事实库不能相同')

    def _recover(self):
        states = {}
        for row in self.source.replay():
            if row.account != self.account:
                continue
            if row.outcome == Outcome.CONFLICT:
                self._blocked = '来源库存在冲突，需人工核对'
            if row.kind == 'runtime_bridge' and row.outcome == Outcome.NEW:
                strategy, decision, state = row.identity
                states[row.trading_day, strategy, decision] = state
        for (day, strategy, decision), state in states.items():
            if state not in ('submitted', 'denied'):
                self._blocked = '上次运行结果未知，需人工核对，禁止重发'
                if state != 'unknown':
                    self.source.append(self.account, day, 'runtime_bridge', (strategy, decision, 'unknown'),
                                       {'reason': self._blocked, 'order_id': ''})
        if self._blocked:
            self.status = BridgeResult('unknown', self._blocked)

    def _record(self, state, reason, order_id=''):
        i = self._intent
        # 若提交记录失败，当前实例也必须停止，而不是在下一个 poll 重走发送。
        try:
            _, outcome = self.source.append(i.account, i.day, 'runtime_bridge',
                (i.strategy, i.decision_id, state), {'reason': reason, 'order_id': order_id})
            if outcome != Outcome.NEW:
                raise RuntimeError('生命周期阶段已记录或冲突')
        except Exception:
            self._blocked = '生命周期流水写入失败，结果未知，需人工核对'
            self.status = BridgeResult('unknown', self._blocked)
            raise
        self.status = BridgeResult(state, reason, order_id)
        return self.status

    def _end(self, state, reason, order_id=''):
        if self._round is not None and self.td.query_collector.current_round == self._round:
            self.td.query_collector.abort(reason)
        self._round = None
        if state == 'unknown':
            self._blocked = reason
        result = self._record(state, reason, order_id)
        self._intent = None
        return result

    def begin(self, intent: OrderIntent, *, exchange: Exchange, offset: Offset | None = None) -> BridgeResult:
        """接受已持久化且可归属的意图；busy 不会领走他人的 collector。"""
        with self.td._send_lock:
            if self._closed or self._blocked:
                return BridgeResult('unknown', self._blocked or '桥已关闭')
            if self._intent is not None:
                return BridgeResult('busy', '已有在途意图')
            self._check_fixture()
            if not isinstance(intent, OrderIntent) or (intent.account, intent.strategy) != (self.account, self.strategy):
                return BridgeResult('denied', '意图账户或策略归属不符')
            i = deepcopy(intent)
            key = (i.strategy, i.decision_id)
            found = handled = False
            for row in self.source.replay():
                if row.account != i.account:
                    continue
                if row.outcome == Outcome.CONFLICT:
                    self._blocked = '来源库存在冲突，需人工核对'
                    self.status = BridgeResult('unknown', self._blocked)
                    return self.status
                if row.trading_day != i.day or row.identity[:2] != key:
                    continue
                if row.kind == 'order_intent' and row.outcome == Outcome.NEW:
                    found = row.payload == asdict(i)
                if row.kind == 'runtime_bridge':
                    handled = True
            if handled:
                return BridgeResult('duplicate', '该决策已处理，不重发')
            if not found:
                return BridgeResult('denied', '找不到完全匹配的已生成策略意图')
            self._intent = i
            self._record('claimed', '已领取持久意图，仅供纸盘')
            try:
                opening = i.purpose in ('open', 'add')
                if offset is None:
                    offset = Offset.OPEN if opening else Offset.CLOSE
                if (not isinstance(exchange, Exchange) or exchange == Exchange.LOCAL
                        or i.side not in ('buy', 'sell') or i.purpose not in ('open','add','risk_exit','take_profit')
                        or (offset not in ((Offset.OPEN,) if opening else (Offset.CLOSE, Offset.CLOSETODAY)))):
                    raise ValueError('交易所、方向或开平参数无效')
                self._req = OrderRequest(symbol=i.symbol, exchange=exchange,
                    direction=Direction.LONG if i.side == 'buy' else Direction.SHORT,
                    type=OrderType.LIMIT, volume=i.lots, price=i.price, offset=offset,
                    reference=f'CrossPeriod_{i.strategy}_{i.purpose}')
                self._observed = self._started = self.clock()
                if not isfinite(self._started):
                    raise ValueError('时钟无效')
                self._round = self.td.begin_order_check(i)
                return self._record('checking', '等待本轮四项柜台查询完整返回')
            except Exception as exc:
                if self._blocked:
                    raise
                return self._end('denied', str(exc))

    def _current(self):
        self._check_fixture()
        now = self.clock()
        if not self._observed <= now < self._started + self.timeout:
            raise ValueError('运行态核验超时或时钟异常')
        self._observed = now

    def poll(self) -> BridgeResult:
        """只观察本轮；不发查询、无 sleep、四项齐全后仅走既有风险管理发送路径。"""
        with self.td._send_lock:
            if self._closed or self._blocked or self._intent is None or self._processing:
                return self.status
            self._processing = True
            sending = False
            try:
                self._current()
                collector = self.td.query_collector
                if collector.current_round != self._round:
                    raise ValueError(collector.failed_reason or '核验轮次已被替换或失效')
                if not collector.ready:
                    return self.status
                check = self.revalidate(deepcopy(self._intent))
                expected = (FirstEntryLimits, HeldLimits) if self._intent.purpose == 'open' else HeldLimits
                if (not isinstance(check, RuntimeValidation) or check.config_version != self._intent.config_version
                        or not isinstance(check.limits, expected)):
                    raise ValueError('配置版本变化或缺少对应门禁参数')
                if not all(value is True for value in (check.limits.signal_current, check.limits.quotes_current,
                                                       check.limits.session_open)):
                    raise ValueError('当前信号、行情或交易时段未通过重验')
                if collector.current_round != self._round:
                    raise ValueError('时序重验期间查询轮次改变')
                self._current()
                if any(row.account == self.account and row.outcome == Outcome.CONFLICT
                       for row in self.source.replay()):
                    return self._end('unknown', '时序重验时来源库出现冲突，需人工核对')
                evidence = self.td.finish_order_check(self._intent, check.limits)
                if evidence.round != self._round:
                    raise ValueError('封存证据不是本轮结果')
                self._req.extra = {'order_intent': self._intent, 'preflight_evidence': evidence}
                self._record('dispatching', '已完成核验，准备交给 main_engine.send_order')
                self._current()
                sending = True
                order_id = self.engine.send_order(self._req, self.name)
                if not isinstance(order_id, str) or not order_id.strip():
                    return self._end('unknown', '发送路径未返回本地单号，不重发，需核对')
                return self._end('submitted', '发送路径返回本地单号，不代表柜台受理或成交', order_id)
            except Exception as exc:
                if self._blocked:
                    raise
                return self._end('unknown' if sending else 'denied', str(exc) or type(exc).__name__)
            finally:
                self._processing = False

    def close(self):
        """仅释放本地资源，不关闭外部引擎。未完成的核验不再发送。"""
        with self.td._send_lock:
            if self._processing:
                raise RuntimeError('时序校验或提交期间不能重入关闭桥')
            try:
                if self._intent is not None and not self._blocked:
                    self._end('denied', '关闭运行桥，未完成核验不重发')
            finally:
                self._closed = True
                self._owner.close()
