"""把策略单接入逐笔核验：先持久化意图、再走四项柜台查询，最后才发单。

为什么不能在 ``send_order`` 里同步等：推进柜台查询的是网关 ``EVENT_TIMER`` 里的
``advance_order_check()``，而策略的 tick / 算法回调也跑在事件线程上——同步等待会
把推进查询的定时器一起堵死。所以拆成两步：

1. :meth:`OrderGate.submit`：把意图写进 journal，``begin_order_check`` 开始一轮查询，
   立刻返回（此时**没有**发单）；
2. :meth:`OrderGate.pump`：由算法每秒调用一次；四项查询齐全后 ``finish_order_check``
   封存证据，挂到 ``req.extra``，再交给 ``main_engine.send_order``。

一个 :class:`OrderGate` 同时只处理一笔（``Preflight`` 本身就是单写入者一次性凭证）。
超时、轮次被替换、核验不通过都会清空在途状态并返回可读原因，由调用方决定重试。
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from vnpy.trader.object import OrderRequest

from trading_core.intents import OrderIntent
from trading_core.journal import Outcome


@dataclass
class _Pending:
    req: OrderRequest
    intent: OrderIntent
    limits: object
    round: object
    started: float


class OrderGate:
    """单账户单写入者的逐笔核验派发器。"""

    def __init__(self, main_engine, gateway_name: str = 'CTP', *,
                 clock=monotonic, timeout: float = 10.0) -> None:
        if timeout <= 0:
            raise ValueError('核验时限必须为正数')
        self.main_engine = main_engine
        self.gateway_name = gateway_name
        self.clock = clock
        self.timeout = float(timeout)
        self._pending: _Pending | None = None

    # ------------------------------------------------------------ 状态
    @property
    def busy(self) -> bool:
        return self._pending is not None

    def _td(self):
        engine = self.main_engine
        getter = getattr(engine, 'get_gateway', None)
        if not callable(getter):
            return None
        gateway = getter(self.gateway_name)
        return getattr(gateway, 'td_api', None) if gateway is not None else None

    @property
    def enabled(self) -> bool:
        """网关是否支持逐笔核验；回测/纸面引擎没有这些方法。"""
        td = self._td()
        return (td is not None
                and callable(getattr(td, 'begin_order_check', None))
                and callable(getattr(td, 'finish_order_check', None))
                and getattr(td, 'journal', None) is not None)

    # ------------------------------------------------------------ 第一步
    def submit(self, req: OrderRequest, intent: OrderIntent, limits) -> str:
        """开始一轮核验并持久化意图；返回空串表示已受理，否则是拒绝原因。"""
        if self._pending is not None:
            return '上一笔仍在核验中'
        td = self._td()
        if td is None or getattr(td, 'journal', None) is None:
            return '交易通道未就绪，无法核验'
        # 意图不在这里落库：网关 ``reserve_and_bind`` 会在真正发单前一次性写入
        # 意图+会话绑定。这里先写会让那次写入变成 DUPLICATE，报文变成
        # “意图或会话号已预留，不重发”，任何单都发不出去。
        try:
            round_ = td.begin_order_check(intent)
        except Exception as exc:                       # noqa: BLE001 - 会话未确认等
            return f'无法开始逐笔核验：{exc}'
        self._pending = _Pending(req, intent, limits, round_, self.clock())
        return ''

    # ------------------------------------------------------------ 第二步
    def pump(self) -> tuple[OrderRequest | None, str, str]:
        """推进在途核验；返回 (req, vt_orderid, 错误)。

        三种结果：``(req, '', '')`` 继续等（本轮四项查询还没齐）；
        ``(req, orderid, '')`` 已发单；``(req, '', 原因)`` 核验失败或超时。
        返回 ``req`` 是为了让调用方补登记委托号（成交回报要靠它路由）。
        """
        pending = self._pending
        if pending is None:
            return None, '', ''
        td = self._td()
        if td is None or getattr(td, 'journal', None) is None:
            return self._fail('交易通道已关闭')
        collector = getattr(td, 'query_collector', None)
        if collector is None or getattr(collector, 'current_round', None) != pending.round:
            reason = getattr(collector, 'failed_reason', '') or '核验轮次已被替换或失效'
            return self._fail(reason)
        if not getattr(collector, 'ready', False):
            if self.clock() - pending.started > self.timeout:
                try:
                    collector.abort('逐笔核验超时')
                except Exception:                      # noqa: BLE001
                    pass
                return self._fail('逐笔核验超时（四项柜台查询未在时限内返回）')
            return pending.req, '', ''
        try:
            evidence = td.finish_order_check(pending.intent, pending.limits)
        except Exception as exc:                       # noqa: BLE001 - GateDenied 等
            return self._fail(f'逐笔核验未通过：{exc}')
        req = pending.req
        req.extra = {'order_intent': pending.intent, 'preflight_evidence': evidence}
        self._pending = None
        try:
            vt_orderid = self.main_engine.send_order(req, self.gateway_name)
        except Exception as exc:                       # noqa: BLE001 - 发送异常不能静默
            return req, '', f'发送异常：{exc}'
        if not vt_orderid:
            return req, '', '发送路径拒绝（详见网关日志）'
        return req, vt_orderid, ''

    def _fail(self, reason: str) -> tuple[OrderRequest | None, str, str]:
        pending, self._pending = self._pending, None
        if pending is not None:
            td = self._td()
            collector = getattr(td, 'query_collector', None) if td is not None else None
            if collector is not None and getattr(collector, 'current_round', None) == pending.round:
                try:
                    collector.abort(reason)
                except Exception:                      # noqa: BLE001
                    pass
        return (pending.req if pending is not None else None), '', str(reason) or '核验失败'

    def cancel(self, reason: str = '算法停止，放弃未完成的核验') -> None:
        """算法停止/暂停时清理在途核验；不撤单、不重发。"""
        if self._pending is not None:
            self._fail(reason)
