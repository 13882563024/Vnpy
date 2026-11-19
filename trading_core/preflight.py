"""逐笔交易许可：纯领域校验，不执行查询或下单。调用者必须提供本轮查询证据。"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from threading import Lock
from time import monotonic
from uuid import uuid4

from .intents import OrderIntent


class GateDenied(ValueError):
    pass


@dataclass(frozen=True)
class QueryRound:
    token: str
    account: str
    day: str
    session: str
    strategy: str
    decision: str
    version: int
    started: float


@dataclass(frozen=True)
class Evidence:
    """同一轮的完整结果；未知数量用 None，不能以零代替。资金单位为元。"""
    round: QueryRound
    positions_complete: bool
    orders_complete: bool
    fills_complete: bool
    account_complete: bool
    signal_current: bool
    quotes_current: bool
    session_open: bool
    conflict: bool
    # 已扣除柜台活单占用和本地未确认额度，重叠部分不重复扣。
    free_open_lots: int | None
    free_close_lots: int | None
    available_cash: float | None
    extra_margin: float | None


class Preflight:
    """单写入者的进程内一次性凭证；持久幂等、进程独占与持仓投影在外层负责。

    invalidate 在任何委托/成交/重连/配置变化时调用。接受一次请求后所有同版本
    并行核对都要重做，防止两个合约抢同一账户的可用资金。绝不以时间戳充当版本。
    """
    def __init__(self, *, ttl_seconds: float, clock=monotonic):
        if not isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError('核对时限必须为正数')
        self._ttl = ttl_seconds
        self._clock = clock
        self._version = 0
        self._lock = Lock()
        self._pending: dict[str, tuple[QueryRound, str]] = {}
        self._sealed: dict[str, Evidence] = {}

    @staticmethod
    def _fingerprint(intent):
        from dataclasses import asdict
        import json
        return json.dumps(asdict(intent), ensure_ascii=False, sort_keys=True, allow_nan=False)

    def begin(self, intent: OrderIntent, session: str) -> QueryRound:
        if not session.strip():
            raise GateDenied('交易会话未知')
        with self._lock:
            now = self._clock()
            self._pending = {k:v for k,v in self._pending.items() if now-v[0].started <= self._ttl}
            self._sealed = {k:v for k,v in self._sealed.items() if k in self._pending}
            r = QueryRound(uuid4().hex, intent.account, intent.day, session, intent.strategy,
                           intent.decision_id, self._version, now)
            self._pending[r.token] = (r, self._fingerprint(intent))
            return r

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def invalidate(self):
        with self._lock:
            self._version += 1
            self._pending.clear()
            self._sealed.clear()

    def seal(self, evidence: Evidence) -> Evidence:
        """柜台查询收集端在核对完成后封存；策略不能创建或认证自己的证据。"""
        with self._lock:
            r = evidence.round
            pending = self._pending.get(r.token)
            if (not pending or pending[0] != r or r.token in self._sealed
                    or not 0 <= self._clock()-r.started <= self._ttl):
                raise GateDenied('不能封存无效或重复的核对轮次')
            self._sealed[r.token] = evidence
            return evidence

    def consume(self, intent: OrderIntent, session: str, evidence: Evidence) -> None:
        with self._lock:
            r = evidence.round
            pending = self._pending.pop(r.token, None)
            sealed = self._sealed.pop(r.token, None)
            if (sealed is not evidence or pending != (r, self._fingerprint(intent)) or r.session != session
                    or self._version != r.version or not 0 <= self._clock()-r.started <= self._ttl):
                raise GateDenied('下单核对已失效；必须重新查询')
            if not all(v is True for v in (evidence.positions_complete, evidence.orders_complete,
                    evidence.fills_complete, evidence.account_complete, evidence.signal_current,
                    evidence.quotes_current, evidence.session_open)) or evidence.conflict is not False:
                raise GateDenied('柜台查询未完成、行情失效或存在账差')
            opening = intent.purpose in ('open','add')
            if not opening and intent.purpose not in ('take_profit','risk_exit'):
                raise GateDenied('委托用途不明')
            free = evidence.free_open_lots if opening else evidence.free_close_lots
            if type(intent.lots) is not int or intent.lots <= 0 or type(free) is not int or free < intent.lots:
                raise GateDenied('可开/可平额度不足或未知')
            if opening:
                cash, margin = evidence.available_cash, evidence.extra_margin
                if not all(type(x) in (int,float) and isfinite(x) and x >= 0 for x in (cash,margin)) or cash < margin:
                    raise GateDenied('资金或新增保证金未知/不足')
            self._version += 1
            self._pending.clear()
            self._sealed.clear()
