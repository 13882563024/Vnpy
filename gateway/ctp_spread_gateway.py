"""CTP 网关子类：为**交易所组合单**加回报容错（不改 site-packages）。

``vnpy_ctp`` 的回报解析有三处无容错的硬下标，组合单回报形态一旦不同就直接
``KeyError`` 抛异常（方案 5.3.3）：

============================  ====================================
``onRtnOrder``  ``symbol_contract_map[symbol]``    组合合约未收录 → KeyError
``onRtnOrder``  ``OFFSET_CTP2VT[CombOffsetFlag]``  回报是两位（"00"）→ KeyError
``onRtnTrade``  ``OFFSET_CTP2VT[OffsetFlag]``      同上
============================  ====================================

策略报单须有逐笔核验证据与持久意图/会话绑定；缺少收集端时默认拒发。
手动报单不标记策略归属，撤单不依赖新增报单门禁。

使用方式（``run_strategy.py``）::

    main_engine.add_gateway(SpreadCtpGateway)

连接配置与 ``CtpGateway`` 完全一致（8 项）。如需两位 ``CombOffsetFlag``，在
``~/.vntrader/ctp_connect.json`` 里手工加一项 ``"组合单偏移位数": 2``（不放进 GUI
表单，避免改动既有的 8 项连接配置）。
"""

from __future__ import annotations

from datetime import datetime
from functools import wraps
from threading import RLock
import json
from pathlib import Path
from time import monotonic

from trading_core.ctp_facts import account_key, append_report, trading_day
from trading_core.journal import Journal, Outcome
from trading_core.preflight import Evidence, Preflight
from trading_core.query_round import QueryCollector
from trading_core.writer_guard import AccountWriter
from trading_core.send_binding import BINDING_KIND, reserve_and_bind, validate_intent
from vnpy.trader.utility import get_file_path

from vnpy.event import EventEngine
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Product, Status
from vnpy.trader.object import AccountData, OrderData, OrderRequest, TradeData, ContractData

from vnpy_ctp import CtpGateway
from vnpy_ctp.gateway.ctp_gateway import (
    CHINA_TZ,
    DIRECTION_CTP2VT,
    DIRECTION_VT2CTP,
    EXCHANGE_CTP2VT,
    OFFSET_CTP2VT,
    OFFSET_VT2CTP,
    ORDERTYPE_CTP2VT,
    ORDERTYPE_VT2CTP,
    STATUS_CTP2VT,
    THOST_FTDC_CC_Immediately,
    THOST_FTDC_FCC_NotForceClose,
    THOST_FTDC_HF_Speculation,
    THOST_FTDC_OST_Canceled,
    THOST_FTDC_PC_Combination,
    THOST_FTDC_OSS_InsertRejected,
    CtpTdApi,
    symbol_contract_map,
)


def with_protocol(address: str) -> str:
    """缺协议前缀时补 ``tcp://``（与父类 ``connect`` 同口径，且不误伤 ssl/socks）。"""
    for prefix in ("tcp://", "ssl://", "socks"):
        if address.startswith(prefix):
            return address
    return "tcp://" + address


def normalize_offset_flag(value, width: int = 1) -> str:
    """把回报里的 ``CombOffsetFlag`` / ``OffsetFlag`` 归一成一位 offset 码。

    - 组合单回报可能是两位（如 ``"00"``），取首字符；
    - 空值回退到开仓（``"0"``），避免 KeyError 打断整个行情/回报线程；
    - ``width=2`` 只在发单时使用，回报侧永远按一位解析。
    """
    text = str(value or "").strip()
    if not text:
        return "0"
    return text[0]


# CTP 查询接口返回码：-1/-2/-3 是链路或流控问题，隔 1 秒以上重发即可。
CTP_RESULT_HINTS = {
    0: "成功",
    -1: "网络连接失败",
    -2: "未处理请求队列已满（柜台限流）",
    -3: "每秒请求数超限（柜台限流）",
}
RETRYABLE_QUERY_CODES = frozenset({-1, -2, -3})


class PositionQueryError(RuntimeError):
    """柜台拒绝持仓查询；``retryable`` 表示稍后重发即可，不代表持仓为空。"""

    def __init__(self, code: int, detail: str = "") -> None:
        self.code = int(code)
        self.retryable = self.code in RETRYABLE_QUERY_CODES
        hint = CTP_RESULT_HINTS.get(self.code, detail or "柜台返回未知错误")
        super().__init__(f"柜台拒绝持仓查询（错误码 {self.code}：{hint}），请稍后重试")


class _RecordedReport(dict):
    """已入账、仅等待父类合约就绪后再投影的回报；并非第二次柜台接收。"""


def _serialized_send_state(fn):
    """Keep login transitions, live activity and the final send decision in one order."""
    @wraps(fn)
    def locked(self, *args, **kwargs):
        with self._send_lock:
            return fn(self, *args, **kwargs)
    return locked


class SpreadCtpTdApi(CtpTdApi):
    """交易 API 子类：组合单回报容错 + 可选两位 ``CombOffsetFlag``。"""

    POSITION_QUERY_TIMEOUT = 10.0  # 超过界面等待窗口后允许重新查询，旧请求仍按 reqid 隔离。

    def __init__(self, gateway, *, journal_path: Path | None = None) -> None:
        super().__init__(gateway)
        self._send_lock = RLock()
        self._writer_guard = None
        self._closed_for_strategy = False
        self._cancel_wait: set[str] = set()
        self.preflight = Preflight(ttl_seconds=15)
        self.query_collector = QueryCollector()
        # 只有活动逐笔轮次独享这些 reqid，绝不能把背景轮询当成本轮答复。
        self._flight_request: tuple[str, int] | None = None
        self._flight_reqids: set[int] = set()  # 迟到的逐笔请求回包不能进入普通 OMS 轮询
        self.journal_path = journal_path
        self.journal: Journal | None = None
        self.journal_error = ''  # 独立锁存，不被正常登录、断线或新的成功回报解除
        self._trades_query_reqid: int | None = None
        self._trades_query_started_at = 0.0
        self.offset_flag_width: int = 1
        self.derived_contracts: set[str] = set()
        self._native_combos: set[tuple[str, str]] = set()
        # 只把已完整返回的本次柜台查询视为可纠正本地持仓的依据。
        self.position_query_pending: bool = False
        self.position_query_generation: int = 0
        self.position_snapshot: dict[str, float] = {}
        self.position_snapshot_sides: dict[str, dict[str, float]] = {}
        self.position_snapshot_account: tuple[str, str] | None = None
        self.position_snapshot_at: float = 0.0
        self._position_query_reqid: int | None = None
        self._position_query_started_at: float = 0.0
        self._position_query_rows: dict[str, float] = {}
        self._position_query_sides: dict[str, dict[str, float]] = {}
        self._position_query_raw_rows: list[dict] = []
        self._position_activity_generation: int = 0
        self._query_activity_generation: int = 0
        self._position_query_failed: bool = False
        # 解析不了的回报绝不能按无单/无成交处理；重连后再核柜台记录。
        self.reconciliation_event_error: str = ""
        # 柜台登录回报里的权威交易日（YYYY-MM-DD）；空串表示柜台上尚未拿到
        self.cabinet_trading_day: str = ""
        # 组合单回报可能缺方向/开平标志：用已知委托信息回填，避免终态回报被丢弃。
        # 内部一律用 CTP 本地 orderid；仅对外事件/快照使用 vt_orderid。
        self._order_cache: dict[str, OrderData] = {}
        self._seen_trades: dict[tuple[str, ...], tuple] = {}
        self._journal_sysids: dict[tuple[str, ...], str] = {}
        self._login_scope: tuple[str, ...] | None = None
        # 本次登录后真正收到过回报的委托（用于区分“实时委托”和“只是本地记忆”）。
        self.live_seen_orderids: set[str] = set()
        # 当日委托查询（vn.py 未封装 reqQryOrder，这里自己发）。
        self.orders_query_pending: bool = False
        self._orders_query_reqid: int | None = None
        self._orders_query_started_at: float = 0.0
        # 柜台当日委托的完整快照。交易所的当日委托列表天然只含**当前交易日**，
        # 所以它是判断“本地记录是否仍然有效”的唯一权威依据（本地时钟可能与柜台差一天）。
        self.orders_snapshot: set[str] = set()
        # OrderSysID → 本地委托号（vt_orderid）：交易所受理后分配，跨重启/重连不变，
        # 是核对"这笔委托柜台还在不在"的稳定键；会话号（FrontID_SessionID_OrderRef）
        # 只在同一进程内有效。
        self.orders_snapshot_sysids: dict[str, str] = {}
        self.orders_snapshot_at: float = 0.0
        self.orders_snapshot_ok: bool = False
        self._orders_query_rows: set[str] = set()
        self._orders_query_sysids: dict[str, str] = {}
        # 视图用的持仓快照：每次完整查询（含后台 auto）都刷新，只供展示；
        # 与核对用的 position_snapshot_* 分开，避免"后台刷新冒充人工核对"。
        self.position_view_sides: dict[str, dict[str, float]] = {}
        self.position_view_account: tuple[str, str] | None = None
        self.position_view_at: float = 0.0
        # "manual"=核对查询（发布快照）；"auto"=后台刷新 OMS 持仓（不动快照）。
        self._position_query_mode: str = "manual"

    def _freeze_journal(self, reason: str) -> None:
        self.journal_error = self.journal_error or reason
        self.reconciliation_event_error = self.journal_error
        self.gateway.write_log(reason + '；禁止新增报单，保留撤单，须人工核对')

    def _record(self, kind: str, data: dict) -> bool:
        if self.journal is None:
            # 未 connect 的纯内存解析器兼容已有离线消费者；不能通过 send_order 发单。
            return not self.journal_error
        try:
            outcome = append_report(self.journal, kind, data, self.brokerid, self.userid,
                                    self.cabinet_trading_day)
            if outcome == Outcome.CONFLICT:
                self._freeze_journal(f'柜台 {kind} 事实明细冲突')
                return False
        except Exception as exc:
            self._freeze_journal(f'柜台 {kind} 流水记录失败：{exc}')
            return False
        # 已提交不等于已派发；本进程的 OMS 投影自行去重。
        return True

    @property
    def position_query_pending(self) -> bool:
        # 界面轮询此属性；超时后自动释放门禁，不依赖 CTP 最后一条回报。
        if (self._position_query_pending
                and monotonic() - self._position_query_started_at >= self.POSITION_QUERY_TIMEOUT):
            self._abort_position_query(keep_snapshot=self._position_query_mode == "auto")
            self.gateway.write_log("柜台持仓查询超时，已丢弃未完整回报；可重新查询")
        return self._position_query_pending

    @position_query_pending.setter
    def position_query_pending(self, pending: bool) -> None:
        self._position_query_pending = pending

    def _abort_position_query(self, *, keep_snapshot: bool = False) -> None:
        """丢弃未完成查询及父类累加器，禁止迟到回报进入 OMS。"""
        self.position_query_pending = False
        self._position_query_failed = True
        if not keep_snapshot:
            self.position_snapshot_at = 0.0
        self._position_query_reqid = None
        self._position_query_started_at = 0.0
        self._position_query_rows = {}
        self._position_query_sides = {}
        self._position_query_raw_rows = []
        self.positions.clear()

    @_serialized_send_state
    def onFrontDisconnected(self, reason: int) -> None:
        self.preflight.invalidate()
        self._abort_position_query()
        self._abort_orders_snapshot()
        self._position_activity_generation += 1
        self.reconciliation_event_error = self.journal_error
        self.cabinet_trading_day = ''
        self._trades_query_reqid = None
        super().onFrontDisconnected(reason)

    def _abort_orders_snapshot(self) -> None:
        """断线/重登后柜台委托快照不再可信：作废，避免据此清理本地记录。"""
        self.orders_query_pending = False
        self._orders_query_reqid = None
        self._orders_query_rows = set()
        self.orders_snapshot = set()
        self.orders_snapshot_sysids = {}
        self.orders_snapshot_at = 0.0
        self.orders_snapshot_ok = False

    @_serialized_send_state
    def onRspUserLogin(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        self.preflight.invalidate()
        self._native_combos.clear()
        # 即便账号名称相同，重连前的持仓/委托快照也不能复用。
        self._abort_position_query()
        self._abort_orders_snapshot()
        self._position_activity_generation += 1
        self.reconciliation_event_error = self.journal_error
        self.cabinet_trading_day = ''
        self._trades_query_reqid = None
        super().onRspUserLogin(data, error, reqid, last)
        # 柜台回报里的 TradingDay 是权威交易日：夜盘登录时它已经是次一交易日，
        # 而本地按自然时间推算不认法定节假日，两者可能不一致。存下来供上层优先使用。
        error_id = 0
        if isinstance(error, dict):
            try:
                error_id = int(error.get("ErrorID") or 0)
            except (TypeError, ValueError):
                error_id = 0
        if not error_id:
            trading_day = str((data or {}).get("TradingDay", "") or "").strip()
            if len(trading_day) == 8 and trading_day.isdigit():
                self.cabinet_trading_day = (f"{trading_day[:4]}-{trading_day[4:6]}"
                                            f"-{trading_day[6:]}")
                scope = (self.brokerid, self.userid, self.cabinet_trading_day)
                if self._login_scope != scope:
                    # 已留存的旧账户/旧交易日缓冲不可在新上下文解析。
                    if self._login_scope is not None:
                        self.order_data.clear()
                        self.trade_data.clear()
                    self._order_cache.clear()
                    self.sysid_orderid_map.clear()
                    self._journal_sysids.clear()
                    self._seen_trades.clear()
                    self.live_seen_orderids.clear()
                self._login_scope = scope
                # 已绑定但尚无柜台回报的本地号也不可被手动委托复用。
                try:
                    high = int(str(data.get('MaxOrderRef') or '').strip() or '0')
                    if self.journal is not None:
                        for row in self.journal.replay():
                            if (row.account == account_key(self.brokerid, self.userid)
                                    and row.trading_day == trading_day
                                    and row.kind in (BINDING_KIND, 'order', 'order_reject')
                                    and row.identity[:2] == (str(self.frontid), str(self.sessionid))):
                                high = max(high, int(row.identity[2]))
                    self.order_ref = max(self.order_ref, high)
                except Exception as exc:
                    self._freeze_journal(f'无法恢复登录会话报单号：{exc}')
                self.gateway.write_log(f"柜台交易日：{self.cabinet_trading_day}")
            else:
                self.cabinet_trading_day = ""

    @_serialized_send_state
    def begin_order_check(self, intent):
        """启动只读核对；发送与签发许可是独立操作。"""
        if (not self.login_status or not self.contract_inited or self.journal is None or
                self.journal_error or self.reconciliation_event_error or
                self._login_scope != (self.brokerid, self.userid, self.cabinet_trading_day) or
                intent.account != account_key(self.brokerid, self.userid) or
                intent.day != trading_day({}, self.cabinet_trading_day)):
            raise RuntimeError('不能为未确认的柜台会话发起核对')
        if self.query_collector.active:
            raise RuntimeError('已有逐笔查询进行中')
        r = self.preflight.begin(intent, f'{self.frontid}/{self.sessionid}')
        self.query_collector.begin(r)
        self._flight_request = None
        return r

    @_serialized_send_state
    def advance_order_check(self):
        def allocate():
            self.reqid += 1
            return self.reqid
        request = self.query_collector.next_request(version=self.preflight.version,
                   connected=bool(self.login_status), allocate_reqid=allocate)
        if request is None:
            return
        self._flight_request = request.kind, request.reqid
        self._flight_reqids.add(request.reqid)
        methods = {'order':self.reqQryOrder,'trade':self.reqQryTrade,
                   'account':self.reqQryTradingAccount,'position':self.reqQryInvestorPosition}
        try:
            n = methods[request.kind]({'BrokerID':self.brokerid,'InvestorID':self.userid}, request.reqid)
            if n:
                raise RuntimeError(f'柜台返回 {n}')
        except Exception as exc:
            self.query_collector.abort(f'逐笔查询 {request.kind} 失败：{exc}')
            self.gateway.write_log(self.query_collector.failed_reason)

    def _receive_order_check(self, kind, data, error, reqid, last):
        if reqid not in self._flight_reqids:
            return False
        if self._flight_request is None or reqid != self._flight_request[1]:
            return True  # 此前轮次/步骤的迟到响应
        accepted = self.query_collector.feed(kind, reqid, data, last, version=self.preflight.version,
                                             connected=bool(self.login_status), error=error)
        if accepted and data and kind in ('order', 'trade'):
            if not self._record('order' if kind == 'order' else 'fill', data):
                self.query_collector.abort(self.journal_error or '查询流水写入失败')
        return True

    @_serialized_send_state
    def take_order_snapshot(self):
        snapshot = self.query_collector.take_snapshot(version=self.preflight.version,
                                                       connected=bool(self.login_status))
        if snapshot is None:
            return None
        from trading_core.account_view import record_snapshot
        try:
            record_snapshot(self.journal, snapshot)
        except Exception as exc:
            self._freeze_journal(f'核对快照未能写入流水：{exc}')
            raise RuntimeError(self.journal_error) from exc
        return snapshot

    def finish_first_entry_check(self, intent, limits):
        if intent.purpose != 'open':
            raise ValueError('此入口仅用于空仓首单')
        return self.finish_order_check(intent, limits)

    @_serialized_send_state
    def finish_order_check(self, intent, limits):
        """只消费网关本轮结果；持仓路径以事实归属校验替代调用者的层数。

        ``limits`` 决定用哪套门禁口径：
        - ``ScopedLimits``：按价差隔离、允许昨仓（单账户多策略的实盘口径）；
        - ``FirstEntryLimits``：专户空仓首单；
        - ``HeldLimits``：专户、单组合、今仓。
        """
        from trading_core.flat_check import FirstEntryLimits, first_entry_evidence
        from trading_core.held_check import HeldLimits, held_evidence
        from trading_core.scoped_check import ScopedLimits, scoped_evidence
        snapshot = self.take_order_snapshot()
        if snapshot is None or self.journal is None or self.journal_error or self.reconciliation_event_error:
            raise RuntimeError(self.query_collector.failed_reason or '本次柜台查询未完成或有冲突')
        if isinstance(limits, ScopedLimits):
            evidence = scoped_evidence(intent, snapshot, self.journal, limits)
        elif intent.purpose == 'open' and isinstance(limits, FirstEntryLimits):
            evidence = first_entry_evidence(intent, snapshot, self.journal, limits)
        elif isinstance(limits, HeldLimits):
            evidence = held_evidence(intent, snapshot, self.journal, limits)
        else:
            raise ValueError('意图和门禁配置不匹配')
        return self.preflight.seal(evidence)

    @_serialized_send_state
    def onRspQryTradingAccount(self, data, error, reqid, last):
        if self._receive_order_check('account', data, error, reqid, last):
            return
        # 父类只映射 balance/frozen/available；总表要看的保证金与盈亏在这里补上。
        if not data or "AccountID" not in data:
            return
        account = AccountData(
            accountid=data["AccountID"],
            balance=data.get("Balance", 0.0),
            frozen=(data.get("FrozenMargin", 0.0) + data.get("FrozenCash", 0.0)
                    + data.get("FrozenCommission", 0.0)),
            gateway_name=self.gateway_name,
        )
        account.available = data.get("Available", 0.0)
        account.curr_margin = data.get("CurrMargin")
        account.close_profit = data.get("CloseProfit")
        account.position_profit = data.get("PositionProfit")
        self.gateway.on_account(account)

    @_serialized_send_state
    def onRspError(self, error: dict, reqid: int, last: bool) -> None:
        if self._flight_request and self._flight_request[1] == reqid:
            self.query_collector.abort(f'柜台逐笔查询错误：{error}')
        if self.position_query_pending and reqid == self._position_query_reqid:
            self._abort_position_query()
        self.gateway.write_error("交易接口报错", error)

    def query_position(self, mode: str = "manual") -> None:
        """发起持仓查询。

        ``mode="manual"``：核对用，结束后发布快照；
        ``mode="auto"``：后台刷新 OMS 持仓用，不发布/不作废核对快照。
        """
        if self.position_query_pending:
            if mode == "auto":
                return  # 已有查询在跑，本轮后台刷新直接跳过
            if self._position_query_mode == "manual":
                raise RuntimeError("柜台上次持仓查询尚未结束，请稍后再核对")
            self._abort_position_query(keep_snapshot=True)  # 让手动查询抢占
        # 基类在目录为空时静默 return，不能把未发请求当作空仓快照。
        if not self.contract_inited or not symbol_contract_map:
            self._abort_position_query()
            raise RuntimeError("柜台合约目录尚未就绪，无法发起持仓查询")
        # 父类按 symbol+direction 累加，失败/超时请求遗留的值不能进入新查询。
        self.positions.clear()
        self._position_query_mode = mode
        self.position_query_pending = True
        if mode == "manual":
            self.position_snapshot_at = 0.0
            self.position_snapshot = {}
            self.position_snapshot_sides = {}
        self._position_query_failed = False
        self._position_query_rows = {}
        self._position_query_sides = {}
        self._position_query_raw_rows = []
        self._query_activity_generation = self._position_activity_generation
        self._position_query_reqid = self.reqid + 1
        self._position_query_started_at = monotonic()
        # 父类不检查 CTP 的发送返回码；请求被拒时不能无限等末条回报。
        try:
            self.reqid += 1
            result = self.reqQryInvestorPosition(
                {"BrokerID": self.brokerid, "InvestorID": self.userid}, self.reqid
            )
        except Exception:
            self._abort_position_query(keep_snapshot=(mode == "auto"))
            raise
        if result:
            self._abort_position_query(keep_snapshot=(mode == "auto"))
            raise PositionQueryError(result)

    def query_orders(self) -> None:
        """查询当日委托（CTP ``ReqQryOrder``）；返回的终态才是权威状态。"""
        if self.orders_query_pending:
            if monotonic() - self._orders_query_started_at < self.POSITION_QUERY_TIMEOUT:
                raise RuntimeError("柜台上次委托查询尚未结束，请稍后再试")
            self._abort_orders_snapshot()
            self.gateway.write_log('柜台上次委托查询超时，已作废；迟到的旧回报将忽略')
        if not self.contract_inited or not symbol_contract_map:
            raise RuntimeError("柜台合约目录尚未就绪，无法发起委托查询")
        self.orders_query_pending = True
        self._orders_query_rows = set()
        self._orders_query_sysids = {}
        self.orders_snapshot_ok = False       # 新查询开始：旧快照立刻作废
        self.orders_snapshot_at = 0.0
        self.reqid += 1
        self._orders_query_reqid = self.reqid
        self._orders_query_started_at = monotonic()
        try:
            result = self.reqQryOrder(
                {"BrokerID": self.brokerid, "InvestorID": self.userid}, self.reqid
            )
            if result:
                raise PositionQueryError(result)
        except Exception:
            self._abort_orders_snapshot()
            raise

    @_serialized_send_state
    def onRspQryOrder(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        if self._receive_order_check('order', data, error, reqid, last):
            return
        if not self.orders_query_pending or reqid != self._orders_query_reqid:
            return  # 迟到/无关的查询回报
        if error and error.get("ErrorID"):
            self.orders_query_pending = False
            self._orders_query_reqid = None
            self._orders_query_rows = set()
            self.orders_snapshot_ok = False
            self.gateway.write_log(
                f"柜台当日委托查询失败（ErrorID={error.get('ErrorID')}）："
                f"{error.get('ErrorMsg', '')}；本地委托状态未知，不做清理")
            return
        if data and not self._record('order', data):
            self._abort_orders_snapshot()
            return
        if data and data.get("InstrumentID"):
            # 先按原始字段记进快照：即使解析不出方向/开平（回报不完整），
            # 也要算作“柜台上确实存在这笔委托”，避免被误判为已失效。
            orderid = (f"{data.get('FrontID')}_{data.get('SessionID')}"
                       f"_{data.get('OrderRef')}")
            self._orders_query_rows.add(f"{self.gateway_name}.{orderid}")
            try:
                order = self._order_from_query(data)
            except (TypeError, ValueError, KeyError):
                order = None
            if order is None:
                self.reconciliation_event_error = f'委托 {orderid} 查询回报无法解析，须人工核对'
                self.gateway.write_log(self.reconciliation_event_error)
                self._abort_orders_snapshot()
                return
            self._publish_order(order, data)
            sysid = str(data.get("OrderSysID") or "").strip()
            if sysid:
                self._orders_query_sysids[sysid] = f"{self.gateway_name}.{orderid}"
        if last:
            self.orders_snapshot = set(self._orders_query_rows)
            self.orders_snapshot_sysids = dict(self._orders_query_sysids)
            self.orders_snapshot_at = monotonic()
            self.orders_snapshot_ok = True
            self.orders_query_pending = False
            self._orders_query_reqid = None
            self.gateway.write_log(
                f"柜台当日委托查询完成：{len(self.orders_snapshot)} 笔"
                f"（其中 {len(self.orders_snapshot_sysids)} 笔已有交易所委托号）")

    def _order_from_query(self, data: dict):
        symbol = str(data.get("InstrumentID", ""))
        # 查询也可能是组合委托首次出现；不能因目录尚无组合代码而漏掉柜台终态。
        contract = self.ensure_contract(data, symbol)
        status = STATUS_CTP2VT.get(data.get("OrderStatus"))
        if status is None:
            return None
        orderid = f"{data.get('FrontID')}_{data.get('SessionID')}_{data.get('OrderRef')}"
        direction = DIRECTION_CTP2VT.get(str(data.get("Direction", "")))
        offset = (OFFSET_CTP2VT.get(normalize_offset_flag(data.get("CombOffsetFlag")))
                  if data.get("CombOffsetFlag") else None)
        cached = self._order_cache.get(orderid)
        if cached is not None and cached.symbol == symbol:
            direction = direction or cached.direction
            offset = offset or cached.offset
        if direction is None or offset is None:
            return None
        try:
            dt = datetime.strptime(
                f"{data.get('InsertDate', '')} {data.get('InsertTime', '')}",
                "%Y%m%d %H:%M:%S",
            ).replace(tzinfo=CHINA_TZ)
        except ValueError:
            dt = datetime.now(CHINA_TZ)
        if (data.get("OrderStatus") == THOST_FTDC_OST_Canceled
                and data.get("OrderSubmitStatus") == THOST_FTDC_OSS_InsertRejected):
            status = Status.REJECTED
        order_type = ORDERTYPE_CTP2VT.get(
            (data.get("OrderPriceType"), data.get("TimeCondition"),
             data.get("VolumeCondition"))
        ) or (cached.type if cached is not None else OrderType.LIMIT)
        order = OrderData(
            symbol=symbol,
            exchange=contract.exchange,
            orderid=orderid,
            type=order_type,
            direction=direction,
            offset=offset,
            price=float(data.get("LimitPrice", 0) or 0),
            volume=float(data.get("VolumeTotalOriginal", 0) or 0),
            traded=float(data.get("VolumeTraded", 0) or 0),
            status=status,
            datetime=dt,
            gateway_name=self.gateway_name,
        )
        return order

    def _sysid_map_and_key(self, data: dict, sysid: str):
        if self.journal is None:
            return self.sysid_orderid_map, sysid
        key = (self.brokerid, self.userid, trading_day(data, self.cabinet_trading_day),
               str(data.get('ExchangeID') or '').strip(), sysid)
        return self._journal_sysids, key

    def _publish_order(self, order: OrderData, data: dict) -> None:
        """实时与查询共用一个单调投影；两者都不能用旧回报复活委托。"""
        orderid = order.orderid
        self.live_seen_orderids.add(order.vt_orderid)
        sysid = str(data.get("OrderSysID") or "").strip()
        if sysid:
            mapping, key = self._sysid_map_and_key(data, sysid)
            if key in mapping and mapping[key] != orderid:
                self._freeze_journal('交易所委托号对应多个会话委托，归属不明')
                return
            mapping[key] = orderid
        previous = self._order_cache.get(orderid)
        if previous is not None:
            if not previous.is_active() and order.is_active():
                return
            if (not previous.is_active() and order.status != previous.status):
                self.reconciliation_event_error = f"委托 {orderid} 柜台终态冲突，须人工核对"
                self.gateway.write_log(self.reconciliation_event_error)
                return
            if order.traded < previous.traded:
                if not order.is_active():
                    self.reconciliation_event_error = f"委托 {orderid} 柜台终态累计成交量回退，须人工核对"
                    self.gateway.write_log(self.reconciliation_event_error)
                return
            if (previous.status == Status.PARTTRADED and order.status == Status.NOTTRADED
                    or previous.status != Status.SUBMITTING and order.status == Status.SUBMITTING
                    or previous.status == order.status and previous.traded == order.traded):
                return
        self._order_cache[orderid] = order
        if not order.is_active():
            self._cancel_wait.discard(orderid)
        self.gateway.on_order(order)

    @_serialized_send_state
    def onRspQryInvestorPosition(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        if self._receive_order_check('position', data, error, reqid, last):
            return
        # 父类不校验 reqid，迟到回报若传下去会污染累加器或直接推送 OMS。
        if not self.position_query_pending or reqid != self._position_query_reqid:
            return
        if error and error.get("ErrorID"):
            self._abort_position_query()
            return
        if data:
            symbol = str(data.get("InstrumentID", ""))
            direction = DIRECTION_CTP2VT.get(data.get("PosiDirection"))
            try:
                volume = float(data.get("Position"))
            except (TypeError, ValueError):
                volume = float("nan")
            sides = self._position_query_sides.setdefault(
                symbol, {"long": 0.0, "short": 0.0, "unknown": 0.0})
            if not symbol or not 0 <= volume < float("inf"):
                # 无法解析的仓位：只影响该合约，不让整份快照作废。
                sides["unknown"] += float("inf")
            elif direction == Direction.LONG:
                sides["long"] += volume
            elif direction == Direction.SHORT:
                sides["short"] += volume
            else:
                # 多空不分的回报（如净仓方向）：该合约无法判定，标记为不可比。
                sides["unknown"] += volume
            if direction in (Direction.LONG, Direction.SHORT) and symbol in symbol_contract_map:
                # 只有方向明确、合约已收录的行才重放给父类，避免 KeyError。
                self._position_query_rows[symbol] = self._position_query_rows.get(symbol, 0.0) + volume
                self._position_query_raw_rows.append(dict(data))
        if not last:
            return
        complete = (not self._position_query_failed and self.contract_inited
                    and self._query_activity_generation == self._position_activity_generation)
        if not complete:
            self._abort_position_query()
            return
        # 只在整次快照通过门禁后重放基类逻辑；空回报绝不推送遗留累加器。
        self.positions.clear()
        try:
            for index, row in enumerate(self._position_query_raw_rows):
                super().onRspQryInvestorPosition(
                    row, {}, reqid, index == len(self._position_query_raw_rows) - 1
                )
        except Exception:
            self._abort_position_query()
            raise
        if self._query_activity_generation != self._position_activity_generation:
            self._abort_position_query()
            return
        if not self._position_query_raw_rows:
            # 完整空仓快照：父类不会发空仓事件；清空仍留在网关的累计值。
            self.positions.clear()
        if self._position_query_mode == "manual":
            # 只有核对查询才发布“可纠依据”快照；后台刷新只更新 OMS 持仓。
            self.position_snapshot = dict(self._position_query_rows)
            self.position_snapshot_sides = {symbol: dict(sides) for symbol, sides in self._position_query_sides.items()}
            self.position_snapshot_account = (self.brokerid, self.userid)
            self.position_snapshot_at = monotonic()
            self.position_query_generation += 1
        # 视图快照：任何一次**完整**查询都发布（含后台 auto），只给总表/状态行看。
        # 复核用的 position_snapshot_* 语义不变，仍然只由 manual 查询发布。
        self.position_view_sides = {symbol: dict(sides) for symbol, sides in self._position_query_sides.items()}
        self.position_view_account = (self.brokerid, self.userid)
        self.position_view_at = monotonic()
        self.position_query_pending = False
        self._position_query_reqid = None
        self._position_query_started_at = 0.0
        self._position_query_raw_rows = []

    # ------------------------------------------------------------ 合约兜底
    @_serialized_send_state
    def onRspQryInstrument(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        super().onRspQryInstrument(data, error, reqid, last)
        # 全局 symbol_contract_map 也可能由行情网关/兜底代码填充，不是本次柜台证明。
        if not (error or {}).get('ErrorID') and data.get('ProductClass') == THOST_FTDC_PC_Combination:
            self._native_combos.add((data['InstrumentID'], data['ExchangeID']))
        if last and not (error or {}).get('ErrorID'):
            self._replay_deferred_reports()

    def _replay_deferred_reports(self) -> None:
        """合约目录就绪后重放登录瞬间收到的委托/成交回报，并解除"目录未完成"的冻结。

        登录时 CTP 往往**先推当日委托/成交回报，再回合约目录**（实测相差十几秒）。
        这些回报当时只能先缓存，但旧实现把 ``reconciliation_event_error`` 置成
        "合约目录尚未完成时收到委托回报" 后就再也没有清过——只要今天有单，之后
        每一次核对都会以"柜台回报待核实"失败。这里在目录就绪时重放并清掉这个
        特定原因；真正解析不了的回报仍会在重放时重新置位。
        """
        if not self.order_data and not self.trade_data:
            if self.reconciliation_event_error.startswith('合约目录尚未完成时收到'):
                self.reconciliation_event_error = ""
            return
        orders, self.order_data = list(self.order_data), []
        trades, self.trade_data = list(self.trade_data), []
        if self.reconciliation_event_error.startswith('合约目录尚未完成时收到'):
            self.reconciliation_event_error = ""
        for report in orders:
            self.onRtnOrder(report)
        for report in trades:
            self.onRtnTrade(report)

    def ensure_contract(self, data: dict, symbol: str) -> ContractData:
        """确保 ``symbol_contract_map`` 里有该合约，缺失时按回报自身信息补建。

        组合合约（如 ``SP a2611&a2701``）可能不在合约查询结果里，而父类实现直接
        ``symbol_contract_map[symbol]`` 取值 → KeyError。补建的合约 ``size=0``，
        与 CTP 对组合合约 ``VolumeMultiple=0`` 的行为一致；所有金额/费用计算都必须
        使用单腿合约的 ``size``（见方案 R7）。
        """
        contract = symbol_contract_map.get(symbol)
        if contract is not None:
            return contract

        exchange_cn = str(data.get("ExchangeID", "")).strip()
        exchange = EXCHANGE_CTP2VT.get(exchange_cn, Exchange.LOCAL)
        contract = ContractData(
            symbol=symbol,
            exchange=exchange,
            name=symbol,
            product=Product.FUTURES,
            size=0,
            pricetick=0,
            min_volume=1,
            gateway_name=self.gateway_name,
        )
        symbol_contract_map[symbol] = contract
        self.derived_contracts.add(symbol)
        self.gateway.write_log(
            f"合约缓存补建：{symbol}（交易所 {exchange.value}），"
            f"组合合约 size=0 属正常，费用/金额请用单腿合约"
        )
        return contract

    # ------------------------------------------------------------ 回报
    @_serialized_send_state
    def onRtnOrder(self, data: dict) -> None:
        self.preflight.invalidate()
        # 查询期间或结束后的异步回报都使快照失效。
        self.position_snapshot_at = 0.0
        self._position_activity_generation += 1
        if not isinstance(data, _RecordedReport) and not self._record('order', data):
            return
        if not self.contract_inited:
            self.reconciliation_event_error = "合约目录尚未完成时收到委托回报"
            self.order_data.append(_RecordedReport(data))
            return

        symbol: str = data["InstrumentID"]
        contract: ContractData = self.ensure_contract(data, symbol)

        frontid: int = data["FrontID"]
        sessionid: int = data["SessionID"]
        order_ref: str = data["OrderRef"]
        orderid: str = f"{frontid}_{sessionid}_{order_ref}"

        status = STATUS_CTP2VT.get(data["OrderStatus"], None)
        if not status:
            self.reconciliation_event_error = f"委托 {orderid} 状态无法识别"
            self.gateway.write_log(self.reconciliation_event_error + "，须人工核对")
            return

        if (
            data["OrderStatus"] == THOST_FTDC_OST_Canceled
            and data["OrderSubmitStatus"] == THOST_FTDC_OSS_InsertRejected
        ):
            status = Status.REJECTED

        timestamp: str = f"{data['InsertDate']} {data['InsertTime']}"
        dt: datetime = datetime.strptime(timestamp, "%Y%m%d %H:%M:%S").replace(tzinfo=CHINA_TZ)

        tp: tuple = (data["OrderPriceType"], data["TimeCondition"], data["VolumeCondition"])
        order_type = ORDERTYPE_CTP2VT.get(tp, None)
        if not order_type:
            self.reconciliation_event_error = f"委托 {orderid} 类型无法识别"
            self.gateway.write_log(self.reconciliation_event_error + "，须人工核对")
            return

        direction = DIRECTION_CTP2VT.get(str(data.get("Direction", "")))
        offset = OFFSET_CTP2VT.get(normalize_offset_flag(data.get("CombOffsetFlag"))) if data.get("CombOffsetFlag") else None
        cached = self._order_cache.get(orderid)
        if cached is not None and cached.symbol == symbol:
            direction = direction or cached.direction
            offset = offset or cached.offset
        if direction is None or offset is None:
            self.reconciliation_event_error = f"委托 {orderid} 方向或开平标志未知"
            self.gateway.write_log(self.reconciliation_event_error + "；禁止继续交易，须与柜台核对")
            return
        order: OrderData = OrderData(
            symbol=symbol,
            exchange=contract.exchange,
            orderid=orderid,
            type=order_type,
            direction=direction,
            offset=offset,
            price=data["LimitPrice"],
            volume=data["VolumeTotalOriginal"],
            traded=data["VolumeTraded"],
            status=status,
            datetime=dt,
            gateway_name=self.gateway_name,
        )
        self._publish_order(order, data)

        status_msg: str = data["StatusMsg"]
        if data["OrderStatus"] == THOST_FTDC_OST_Canceled and status_msg != "已撤单":
            self.gateway.write_log(f"委托 {orderid} 状态更新，{status_msg}")

    @_serialized_send_state
    def onRspOrderInsert(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        """柜台拒单也是权威终态，必须走同一投影，不能被迟到回报覆盖。"""
        self.preflight.invalidate()
        if not self._record('order_reject', dict(data, FrontID=self.frontid,
                            SessionID=self.sessionid, ErrorID=(error or {}).get('ErrorID'))):
            return
        symbol = data["InstrumentID"]
        orderid = f"{self.frontid}_{self.sessionid}_{data['OrderRef']}"
        contract = self.ensure_contract(data, symbol)
        direction = DIRECTION_CTP2VT.get(data.get("Direction"))
        cached = self._order_cache.get(orderid)
        if cached is not None and cached.symbol == symbol:
            direction = direction or cached.direction
        if direction is None:
            self.reconciliation_event_error = f"拒单 {orderid} 方向未知，须人工核对"
            self.gateway.write_log(self.reconciliation_event_error)
        else:
            offset = (OFFSET_CTP2VT.get(normalize_offset_flag(data["CombOffsetFlag"]))
                      if data.get("CombOffsetFlag") else None)
            self._publish_order(OrderData(
                symbol=symbol, exchange=contract.exchange, orderid=orderid,
                type=cached.type if cached else OrderType.LIMIT, direction=direction,
                offset=offset or (cached.offset if cached else Offset.NONE),
                price=data["LimitPrice"], volume=data["VolumeTotalOriginal"],
                status=Status.REJECTED, gateway_name=self.gateway_name,
            ), data)
        self.gateway.write_error("交易委托失败", error)

    def query_trades(self) -> None:
        """显式拉取当日成交明细；不把成功发送查询视作已完整对账。"""
        if (self._trades_query_reqid is not None and
                monotonic() - self._trades_query_started_at < self.POSITION_QUERY_TIMEOUT):
            raise RuntimeError('柜台上次成交查询尚未结束')
        self.reqid += 1
        self._trades_query_reqid = self.reqid
        self._trades_query_started_at = monotonic()
        try:
            result = self.reqQryTrade({'BrokerID': self.brokerid, 'InvestorID': self.userid}, self.reqid)
            if result:
                raise RuntimeError(f'柜台拒绝成交查询：{result}')
        except Exception:
            self._trades_query_reqid = None
            raise

    @_serialized_send_state
    def onRspQryTrade(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        if self._receive_order_check('trade', data, error, reqid, last):
            return
        if reqid != self._trades_query_reqid:
            return
        if error and error.get('ErrorID'):
            self._trades_query_reqid = None
            self.gateway.write_error('交易成交查询失败', error)
            return
        if last:
            self._trades_query_reqid = None
        if data:
            self._project_trade(data)

    @_serialized_send_state
    def onRtnTrade(self, data: dict) -> None:
        self.preflight.invalidate()
        self._project_trade(data)

    def _project_trade(self, data: dict) -> None:
        """成交回报：组合单成交可能是单腿（``CombinationDerived``）或组合代码。"""
        self.position_snapshot_at = 0.0
        self._position_activity_generation += 1
        if not isinstance(data, _RecordedReport) and not self._record('fill', data):
            return
        if not self.contract_inited:
            self.reconciliation_event_error = "合约目录尚未完成时收到成交回报"
            self.trade_data.append(_RecordedReport(data))
            return

        symbol: str = data["InstrumentID"]
        contract: ContractData = self.ensure_contract(data, symbol)

        sysid = str(data.get("OrderSysID") or "").strip()
        mapping, syskey = self._sysid_map_and_key(data, sysid)
        orderid = mapping.get(syskey) if sysid else None
        if not orderid:
            frontid, sessionid, order_ref = data.get("FrontID"), data.get("SessionID"), data.get("OrderRef")
            if frontid is None or sessionid is None or not order_ref:
                self.reconciliation_event_error = "成交早于委托回报且缺订单标识"
                self.gateway.write_log(self.reconciliation_event_error + "；拒绝自动归属，须人工核对")
                return
            orderid = f"{frontid}_{sessionid}_{order_ref}"
            if sysid:
                mapping[syskey] = orderid

        direction = DIRECTION_CTP2VT.get(str(data.get("Direction", "")))
        offset = OFFSET_CTP2VT.get(normalize_offset_flag(data.get("OffsetFlag"))) if data.get("OffsetFlag") else None
        cached = self._order_cache.get(orderid)
        if cached is not None and cached.symbol == symbol:
            direction = direction or cached.direction
            offset = offset or cached.offset
        if direction is None or offset is None:
            self.reconciliation_event_error = f"成交 {data.get('TradeID', '')} 方向或开平标志未知"
            self.gateway.write_log(self.reconciliation_event_error + "；须人工核对")
            return
        timestamp: str = f"{data['TradeDate']} {data['TradeTime']}"
        dt: datetime = datetime.strptime(timestamp, "%Y%m%d %H:%M:%S").replace(tzinfo=CHINA_TZ)

        raw_tradeid = str(data.get('TradeID') or '').strip()
        day = (trading_day(data, self.cabinet_trading_day) if self.journal is not None
               else str(data.get('TradingDay') or data['TradeDate']))
        exchange = str(data.get('ExchangeID') or contract.exchange.value).strip()
        key = (self.brokerid, self.userid, day, exchange, raw_tradeid, symbol)
        # OMS 以 vt_tradeid 唯一索引：派生腿共用成交号时，仍须保留两条。
        trade: TradeData = TradeData(
            symbol=symbol,
            exchange=contract.exchange,
            orderid=orderid,
            tradeid=(json.dumps(key, ensure_ascii=False, separators=(',', ':'))
                     if self.journal is not None else raw_tradeid),
            direction=direction,
            offset=offset,
            price=data["Price"],
            volume=data["Volume"],
            datetime=dt,
            gateway_name=self.gateway_name,
        )
        # 两条派生腿可能共享成交号；按腿识别重放，但绝不把腿量累加到组合委托。
        trade.extra = {'ctp_trade_id': raw_tradeid, 'ctp_order_sys_id': sysid, 'trading_day': day}
        if not raw_tradeid:
            self.reconciliation_event_error = f"委托 {orderid} 成交缺少成交号，须人工核对"
            self.gateway.write_log(self.reconciliation_event_error)
            return
        details = (direction, offset, trade.price, trade.volume, dt)
        previous = self._seen_trades.get(key)
        if previous is not None:
            if previous != details:
                self.reconciliation_event_error = f"委托 {orderid} 同号成交 {trade.tradeid} 明细冲突，须人工核对"
                self.gateway.write_log(self.reconciliation_event_error)
            return
        self._seen_trades[key] = details
        self.live_seen_orderids.add(trade.vt_orderid)
        self.gateway.on_trade(trade)

    # ------------------------------------------------------------ 连接与发单
    @_serialized_send_state
    def connect(self, address: str, userid: str, password: str, brokerid: str,
                auth_code: str, appid: str, production_mode: bool,
                offset_flag_width: int = 1) -> None:
        self.preflight.invalidate()
        self.cabinet_trading_day = ''  # 须由这次登录重新建立会话上下文
        # 不在导入/构造时触碰用户库。真正启动交易通道前先打开流水并恢复冲突门禁；
        # 存储坏了仍允许连接查单/撤单，绝不降级到可发新单的内存模式。
        try:
            self.journal = Journal(self.journal_path if self.journal_path is not None
                                   else get_file_path('ctp_facts.sqlite3'))
            account = account_key(brokerid, userid)
            if any(row.account == account and row.outcome == Outcome.CONFLICT
                   for row in self.journal.replay()):
                self._freeze_journal('账户存在历史流水冲突')
        except Exception as exc:
            self._freeze_journal(f'交易流水初始化失败：{exc}')
        self.offset_flag_width = max(1, int(offset_flag_width or 1))
        if self.offset_flag_width != 1:
            self.gateway.write_log(
                f"组合单 CombOffsetFlag 位数 = {self.offset_flag_width}"
                f"（默认 1 位，按 CTP 官方指南一位即可）"
            )
        super().connect(address, userid, password, brokerid, auth_code, appid, production_mode)

    @_serialized_send_state
    def cancel_order(self, req):
        # 撤单不要求新增报单许可，但一经请求，旧的可用量与并存条件不再可信。
        self.preflight.invalidate()
        self._cancel_wait.add(req.orderid)
        return super().cancel_order(req)

    def close(self):
        # CTP exit 可能等待回报线程；不可持有发送锁等待该线程。
        with self._send_lock:
            self._closed_for_strategy = True
            self.preflight.invalidate()
        try:
            super().close()
        finally:
            with self._send_lock:
                if self._writer_guard is not None:
                    self._writer_guard.close()
                    self._writer_guard = None

    @_serialized_send_state
    def send_order(self, req: OrderRequest) -> str:
        """CrossPeriod 须逐笔核验并持久绑定；手动委托不生成策略意图。"""
        if self.journal is None or self.journal_error:
            self.gateway.write_log(self.journal_error or '交易流水未初始化，禁止报单')
            return ''
        if req.offset not in OFFSET_VT2CTP:
            self.gateway.write_log("请选择开平方向")
            return ""

        if req.type not in ORDERTYPE_VT2CTP:
            self.gateway.write_log(f"当前接口不支持该类型的委托{req.type.value}")
            return ""

        is_strategy = (req.reference or '').startswith('CrossPeriod')
        extra = getattr(req, 'extra', None) or {}
        if not isinstance(extra, dict):
            self.gateway.write_log('报单附加信息无效')
            return ''
        intent = extra.get('order_intent')
        if not is_strategy and (intent is not None or 'preflight_evidence' in extra):
            self.gateway.write_log('非策略报单不可携带策略归属或核验凭证')
            return ''
        contract = symbol_contract_map.get(req.symbol)
        native_combo = (contract is not None and contract.exchange == req.exchange
                        and getattr(contract, 'product', None) == Product.SPREAD
                        and (req.symbol, req.exchange.value) in self._native_combos)
        if is_strategy:
            try:
                if (not self.login_status or self.reconciliation_event_error
                        or self._login_scope != (self.brokerid, self.userid, self.cabinet_trading_day)
                        or type(self.frontid) is not int or type(self.sessionid) is not int):
                    raise ValueError('登录会话缺失或柜台数据需要核对')
                if (not self.contract_inited or not native_combo
                        or self.offset_flag_width not in (1, 2)):
                    raise ValueError('缺少匹配的柜台合约、交易所或合法开平位数')
                if getattr(intent, 'reason', {}).get('shadow_only') is True and not getattr(self, '_paper_only', False):
                    raise ValueError('生产接口禁止发送影子意图')
                validate_intent(req, intent, account_key(self.brokerid, self.userid),
                                trading_day({}, self.cabinet_trading_day), contract.pricetick)
                evidence = extra.get('preflight_evidence')
                if not isinstance(evidence, Evidence):
                    raise ValueError('缺少本次逐笔核验证据')
                if self._closed_for_strategy or self._cancel_wait:
                    raise ValueError('交易通道已关闭或有撤单待确认')
                if req.offset == Offset.CLOSEYESTERDAY:
                    raise ValueError('尚未支持昨仓归属校验，禁止平昨')
                self.preflight.consume(intent, f'{self.frontid}/{self.sessionid}', evidence)
                if self._writer_guard is None:
                    self._writer_guard = AccountWriter(self.journal.path.parent, intent.account)
                if self._writer_guard.account != intent.account:
                    raise ValueError('写入者账户发生变化，请关闭并重新打开交易通道')
                self._writer_guard.acquire()
            except Exception as exc:
                self.gateway.write_log(f'策略报单未通过逐笔核验：{exc}')
                return ''

        self.order_ref += 1
        identity = (str(self.frontid), str(self.sessionid), str(self.order_ref))
        price_type, time_condition, volume_condition = ORDERTYPE_VT2CTP[req.type]
        offset_flag: str = OFFSET_VT2CTP.get(req.offset, "")
        if native_combo and self.offset_flag_width == 2:
            # 两腿须同开同平；左补 '0' 会把平仓编码成一开一平。
            offset_flag *= self.offset_flag_width

        ctp_req: dict = {
            "InstrumentID": req.symbol,
            "ExchangeID": req.exchange.value,
            "LimitPrice": req.price,
            "VolumeTotalOriginal": int(req.volume),
            "OrderPriceType": price_type,
            "Direction": DIRECTION_VT2CTP.get(req.direction, ""),
            "CombOffsetFlag": offset_flag,
            "OrderRef": str(self.order_ref),
            "InvestorID": self.userid,
            "UserID": self.userid,
            "BrokerID": self.brokerid,
            "CombHedgeFlag": THOST_FTDC_HF_Speculation,
            "ContingentCondition": THOST_FTDC_CC_Immediately,
            "ForceCloseReason": THOST_FTDC_FCC_NotForceClose,
            "IsAutoSuspend": 0,
            "TimeCondition": time_condition,
            "VolumeCondition": volume_condition,
            "MinVolume": 1,
        }

        if is_strategy:
            try:
                outcome = reserve_and_bind(self.journal, intent, identity, ctp_req['ExchangeID'])
                if outcome == Outcome.CONFLICT:
                    self._freeze_journal('策略意图或会话报单身份冲突')
                    return ''
                if outcome != Outcome.NEW:
                    self.gateway.write_log('意图或会话号已预留，不重发；请核对柜台')
                    return ''
            except Exception as exc:
                self._freeze_journal(f'策略意图或会话身份未能持久化：{exc}')
                return ''
        self.preflight.invalidate()  # 包括手动报单及发送结果不明的情形
        self.reqid += 1
        try:
            n: int = self.reqOrderInsert(ctp_req, self.reqid)
        except Exception as exc:
            self._freeze_journal(f'委托发送结果不明：{exc}')
            return ''
        if n:
            self.gateway.write_log(f"委托请求发送失败，错误代码：{n}")
            return ""

        orderid = '_'.join(identity)
        order: OrderData = req.create_order_data(orderid, self.gateway_name)
        if orderid not in self._order_cache:  # 不覆盖已经到达的柜台回报
            self._order_cache[orderid] = order
            self.gateway.on_order(order)
        return order.vt_orderid


class SpreadCtpGateway(CtpGateway):
    """把 ``CtpGateway`` 的交易 API 换成带组合单容错的子类。"""

    def __init__(self, event_engine: EventEngine, gateway_name: str, *,
                 journal_path: Path | None = None) -> None:
        super().__init__(event_engine, gateway_name)
        # 父类已创建原始 CtpTdApi（未连接，无副作用），这里替换为容错版本
        self.td_api: SpreadCtpTdApi = SpreadCtpTdApi(self, journal_path=journal_path)

    def init_query(self) -> None:
        """定时刷新资金与持仓；持仓查询走 auto 模式，不干扰人工核对快照。"""
        self.query_functions = [self.query_account, self._auto_query_position]
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def process_timer_event(self, event) -> None:
        if self.td_api.query_collector.active:
            self.td_api.advance_order_check()
            return
        super().process_timer_event(event)

    def _auto_query_position(self) -> None:
        """后台刷新 OMS 持仓，让持仓面板能跟随成交变化。"""
        try:
            self.td_api.query_position(mode="auto")
        except Exception:
            pass  # 后台刷新失败不影响交易；人工核对会给出明确错误

    def connect(self, setting: dict) -> None:
        userid: str = setting["用户名"]
        password: str = setting["密码"]
        brokerid: str = setting["经纪商代码"]
        td_address: str = setting["交易服务器"]
        md_address: str = setting["行情服务器"]
        appid: str = setting["产品名称"]
        auth_code: str = setting["授权编码"]

        environment: str = setting.get("柜台环境", "实盘")
        production_mode: bool = environment == "实盘"
        offset_flag_width: int = int(setting.get("组合单偏移位数", 1) or 1)

        self.td_api.connect(
            with_protocol(td_address), userid, password, brokerid, auth_code, appid,
            production_mode, offset_flag_width,
        )
        self.md_api.connect(with_protocol(md_address), userid, password, brokerid, production_mode)

        self.init_query()
