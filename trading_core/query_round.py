"""单账户串行查询；无网络、工作线程、sleep 或交易副作用。

由网关在同一把账户锁内驱动（包括发送、版本变更和断线）。调用者负责与其它
柜台查询共享限频及递增 reqid，维护会话版本，并验证归属、冻结量和资金用途。
四个 last 证明请求完整，不代表柜台原子快照，更不构成交易许可。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from sys import float_info
from time import monotonic
from types import MappingProxyType

from .ctp_facts import account_key
from .preflight import GateDenied, QueryRound

_KINDS = ('order', 'trade', 'account', 'position')
Row = Mapping[str, object]


@dataclass(frozen=True)
class QueryRequest:
    kind: str
    reqid: int


@dataclass(frozen=True)
class QuerySnapshot:
    round: QueryRound
    orders: tuple[Row, ...]
    trades: tuple[Row, ...]
    accounts: tuple[Row, ...]
    positions: tuple[Row, ...]


def _finite(x):
    return type(x) in (int, float) and isfinite(x)


def _positive(x):
    if not _finite(x) or x <= 0:
        raise ValueError('时间设置必须是有限正数')
    return x


def _text(row, key):
    value = row.get(key)
    if type(value) is not str or not value.strip():
        raise ValueError(f'{key} 缺失或不合法')
    return value.strip()


def _lots(row, key, minimum=0):
    value = row.get(key)
    if type(value) is not int or value < minimum:
        raise ValueError(f'{key} 手数未知')
    return value


def _row(kind, data, scope):
    # CTP 回报是标量字段的扁平结构。先复制再验证，不持有绑定层的可变对象。
    row = dict(data)
    if any(type(k) is not str or type(v) not in (type(None), str, bytes, bool, float, int)
           for k, v in row.items()):
        raise ValueError('柜台行不是扁平标量记录')
    broker = _text(row, 'BrokerID')
    investor = _text(row, 'AccountID' if kind == 'account' else 'InvestorID')
    if account_key(broker, investor) != scope.account or _text(row, 'TradingDay') != scope.day:
        raise ValueError('回报账户或交易日不匹配')
    if kind == 'account':
        cash = row.get('Available')
        if not _finite(cash) or abs(cash) >= float_info.max:
            raise ValueError('柜台可用资金未知')
    else:
        _text(row, 'InstrumentID')
        if kind == 'order':
            for key in ('FrontID', 'SessionID'):
                if type(row.get(key)) is not int:
                    raise ValueError(f'{key} 未知')
            _text(row, 'OrderRef')
            if row.get('OrderStatus') not in ('0', '1', '2', '3', '4', '5'):
                raise ValueError('委托状态未知')
            original = _lots(row, 'VolumeTotalOriginal', 1)
            if _lots(row, 'VolumeTraded') > original:
                raise ValueError('委托累计成交超过原手数')
        elif kind == 'trade':
            _text(row, 'ExchangeID')
            _text(row, 'TradeID')
            _lots(row, 'Volume', 1)
        else:
            if row.get('PosiDirection') not in ('2', '3'):
                raise ValueError('持仓方向未知')
            _lots(row, 'Position')
    return MappingProxyType(row)


class QueryCollector:
    """一次只处理一笔；在没有 next_request/feed/take_snapshot 时不自行推进。"""
    def __init__(self, *, interval=1.0, timeout=12.0, clock=monotonic):
        self._interval = _positive(interval)
        self._timeout = _positive(timeout)
        self._clock = clock
        self._scope: QueryRound | None = None
        self._last_sent = float('-inf')
        self._last_reqid = 0
        self._req: QueryRequest | None = None
        self._rows: dict[str, list[Row]] = {}
        self._failed = ''

    @property
    def active(self):
        return self._scope is not None

    @property
    def current_round(self) -> QueryRound | None:
        """账户锁内检查归属；中止或领取后不再保留旧轮次。"""
        return self._scope

    @property
    def ready(self) -> bool:
        """仅表示四个末包已收齐；领取时仍要检查连接、版本和时限。"""
        return self.active and self._step == len(_KINDS)

    @property
    def failed_reason(self):
        return self._failed

    def begin(self, round: QueryRound, *, timeout=None):
        if self.active:
            raise GateDenied('已有在途或未领取的查询轮次')
        duration = self._timeout if timeout is None else _positive(timeout)
        now = self._clock()
        if not (_finite(now) and _finite(round.started) and 0 <= now-round.started < duration
                and type(round.version) is int and round.version >= 0
                and all(isinstance(v, str) and v.strip() for v in (round.token, round.account,
                        round.day, round.session, round.decision, round.strategy))):
            raise GateDenied('查询轮次身份未知或已过期')
        self._deadline = round.started + duration
        self._observed = now
        self._scope = round
        self._step = 0
        self._rows = {kind: [] for kind in _KINDS}
        self._failed = ''

    def _clear(self):
        self._scope = self._req = None
        self._rows.clear()

    def abort(self, reason: str):
        if self.active:
            self._failed = str(reason) or '查询已中止'
            self._clear()

    def _current(self, version, connected):
        if not self.active:
            return False
        now = self._clock()
        if connected is not True or type(version) is not int or version != self._scope.version:
            self.abort('连接或状态版本已变化')
        elif not _finite(now) or not self._observed <= now < self._deadline:
            self.abort('查询超时或时钟异常')
        else:
            self._observed = now
        return self.active

    def next_request(self, *, version, connected, allocate_reqid: Callable[[], int]):
        """锁内预注册后返回供立即发送的请求；发送失败必须 abort，不重发。"""
        if (not self._current(version, connected) or self._req or self._step == len(_KINDS)
                or self._observed - self._last_sent < self._interval):
            return None
        try:
            reqid = allocate_reqid()
        except Exception:
            self.abort('无法分配查询请求号')
            raise
        if type(reqid) is not int or reqid <= self._last_reqid:
            self.abort('请求号未知、重复或回退')
            return None
        self._last_reqid = reqid
        self._last_sent = self._observed
        self._req = QueryRequest(_KINDS[self._step], reqid)
        return self._req

    def feed(self, kind, reqid, data, last, *, version, connected, error=None) -> bool:
        """仅返回该帧是否被接受。错误、迟到和局部成功均不能领取快照。"""
        if (not self._current(version, connected) or not self._req or type(reqid) is not int
                or reqid != self._req.reqid):
            return False
        try:
            if kind != self._req.kind or type(last) is not bool:
                raise ValueError('查询类型或 last 不匹配')
            if error is not None and (not isinstance(error, Mapping) or error and
                    (type(error.get('ErrorID')) is not int or error['ErrorID'] != 0)):
                raise ValueError(f'柜台查询错误: {error}')
            if data is not None and not isinstance(data, Mapping):
                raise ValueError('柜台回报类型未知')
            if data:
                self._rows[kind].append(_row(kind, data, self._scope))
            elif not last:
                raise ValueError('非末包没有数据')
            if last:
                if kind == 'account' and not self._rows[kind]:
                    raise ValueError('柜台未返回资金记录')
                self._req = None
                self._step += 1
        except (ValueError, TypeError, OverflowError) as exc:
            self.abort(str(exc))
            return False
        return True

    def take_snapshot(self, *, version, connected) -> QuerySnapshot | None:
        """一次性领取四个同轮请求的完整只读行；领取之后由外层门禁防止滥用。"""
        if not self._current(version, connected) or self._step != len(_KINDS):
            return None
        snapshot = QuerySnapshot(self._scope, *(tuple(self._rows[k]) for k in _KINDS))
        self._clear()
        return snapshot
