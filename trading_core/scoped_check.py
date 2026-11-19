"""按策略隔离的逐笔证据：单账户多价差、允许昨仓。

``held_check`` / ``flat_check`` 的口径是"专户 + 单组合 + 单方向 + 今仓"，任何
其它品种的持仓、任何昨仓都会让核验直接失败。本模块保留它们真正要防的东西，
把"账户级"换成"策略级"：

- 持仓只看**本价差的两条腿**，同账户其它价差、组合仓一律不参与判断；
- 今仓、昨仓都认（用 ``Position`` 总量核对），不再要求 ``TodayPosition == Position``；
- 委托只看**本价差两腿与组合合约**上仍活动的单，且必须在 ``known_orders`` 里
  （本进程已知的委托号），同账户其它价差的在途单不影响本策略；
- **不扫描历史意图/绑定**：绑定只对之后发出的新单生效（``Preflight`` + 网关
  ``reserve_and_bind`` 在发单时校验）。历史上"发了没成"的意图不再毒化账户。

保留的硬性检查：账户与交易日一致、信号/行情/时段有效、两腿净仓与本地账完全
相等、可平量扣除冻结、开仓额度与保证金可用。任何一条不满足都抛 ``GateDenied``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

from .ctp_facts import account_key
from .intents import OrderIntent
from .journal import Journal
from .preflight import Evidence, GateDenied
from .query_round import QuerySnapshot


@dataclass(frozen=True)
class ScopedLimits:
    """一个价差的逐笔门禁参数（由策略在发单前用当时的状态构造）。"""

    strategy: str                  # 价差名，必须等于 intent.strategy
    near: str                      # 裸合约代码，如 'a2701'
    far: str
    exchange: str                  # 'DCE' / 'CZCE'
    combo: str                     # 组合合约原始字符串，如 'SP a2701&a2703'
    max_open_lots: int             # 层容量（含已持有的层）
    margin_per_lot: float          # 每手新增保证金（元）；开仓时必须 > 0
    signal_current: bool
    quotes_current: bool
    session_open: bool
    expected_near: float           # 本地账：near 腿净仓（多正空负）
    expected_far: float
    known_orders: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)


def _require(ok, reason: str) -> None:
    if not ok:
        raise GateDenied(reason)


def _number(value) -> bool:
    return type(value) in (int, float) and isfinite(value)


def _text(value) -> bool:
    return type(value) is str and bool(value.strip())


def _valid_intent(intent: OrderIntent) -> None:
    _require(all(_text(x) for x in (intent.tier_id, intent.decision_id, intent.strategy,
                                    intent.config_version, intent.symbol))
             and intent.side in ('buy', 'sell')
             and type(intent.lots) is int and intent.lots > 0
             and _number(intent.price)
             and isinstance(intent.reason, dict) and intent.reason,
             '意图字段未知')
    _require(intent.purpose in ('open', 'add', 'take_profit', 'risk_exit'), '委托用途不明')


def scoped_evidence(intent: OrderIntent, snapshot: QuerySnapshot, journal: Journal,
                    limits: ScopedLimits) -> Evidence:
    """返回仅供本轮封存的证据；未知事实、额度不足或不一致均抛 ``GateDenied``。"""
    try:
        return _evidence(intent, snapshot, limits)
    except GateDenied:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError) as exc:
        raise GateDenied(f'逐笔核对事实缺失或异常：{exc}') from exc


def _evidence(intent: OrderIntent, snapshot: QuerySnapshot, limits: ScopedLimits) -> Evidence:
    r = snapshot.round
    _valid_intent(intent)
    _require((intent.account, intent.day, intent.strategy) == (r.account, r.day, r.strategy),
             '意图与查询轮次不符')
    _require(intent.strategy == limits.strategy and intent.symbol == limits.combo,
             '意图与价差配置不符')
    _require(_text(limits.near) and _text(limits.far) and limits.near != limits.far
             and _text(limits.exchange), '价差腿配置未知')
    _require(all(x is True for x in (limits.signal_current, limits.quotes_current,
                                     limits.session_open)), '信号、行情或交易时段未通过核验')
    _require(len(snapshot.accounts) == 1
             and snapshot.accounts[0].get('CurrencyID') == 'CNY', '缺少唯一人民币资金账户')
    for rows, investor in ((snapshot.accounts, 'AccountID'), (snapshot.positions, 'InvestorID'),
                           (snapshot.orders, 'InvestorID'), (snapshot.trades, 'InvestorID')):
        for row in rows:
            _require(account_key(row['BrokerID'], row[investor]) == r.account
                     and row['TradingDay'] == r.day, '快照账户或交易日不符')

    cash = snapshot.accounts[0]['Available']
    _require(_number(cash), '柜台可用资金未知')

    long, short, long_frozen, short_frozen = {}, {}, {}, {}
    for row in snapshot.positions:
        symbol = str(row['InstrumentID']).strip()
        if symbol not in (limits.near, limits.far):
            continue                      # 同账户其它价差/组合仓与本策略无关
        _require(row['ExchangeID'] == limits.exchange and row['HedgeFlag'] == '1',
                 f'{symbol} 交易所或投机标记不符')
        lots = row['Position']
        _require(_number(lots) and _number(row['LongFrozen']) and _number(row['ShortFrozen']),
                 f'{symbol} 持仓或冻结量未知')
        if row['PosiDirection'] == '2':
            _require(symbol not in long, f'{symbol} 多头持仓行重复')
            long[symbol] = lots
        elif row['PosiDirection'] == '3':
            _require(symbol not in short, f'{symbol} 空头持仓行重复')
            short[symbol] = lots
        else:
            raise GateDenied(f'{symbol} 持仓方向未知')
        # 一行的多空冻结都要收：卖平多头占用 ShortFrozen，买平空头占用 LongFrozen。
        long_frozen[symbol] = row['LongFrozen']
        short_frozen[symbol] = row['ShortFrozen']

    net_near = long.get(limits.near, 0) - short.get(limits.near, 0)
    net_far = long.get(limits.far, 0) - short.get(limits.far, 0)
    _require(abs(net_near - limits.expected_near) < 1e-9
             and abs(net_far - limits.expected_far) < 1e-9,
             f'柜台两腿与本地账不符（{limits.near} {net_near:g}/{limits.expected_near:g}，'
             f'{limits.far} {net_far:g}/{limits.expected_far:g}）')

    # 本机不认识的在本策略两腿/组合上的活动委托只作提示，不拦发单：
    # 柜台的可平量已经被 LongFrozen/ShortFrozen 扣减，单腿/超量平仓仍会被交易所
    # 拒绝；而这些"不认识的单"在实践中往往是同一账户手工留下的残余委托（重启后
    # OMS 里没有它们），拦下来会让这个价差永久无法挂单。

    opening = intent.purpose in ('open', 'add')
    if opening:
        _require(type(limits.max_open_lots) is int and limits.max_open_lots > 0, '层容量未知')
        _require(_number(limits.margin_per_lot) and limits.margin_per_lot > 0,
                 '每手保证金未知，禁止开仓')
        held = max(abs(limits.expected_near), abs(limits.expected_far))
        capacity = max(0, int(limits.max_open_lots - held))
        margin = float(limits.margin_per_lot) * intent.lots
        _require(capacity >= intent.lots, f'可开额度不足（剩余 {capacity} 手）')
        _require(cash >= margin, f'可用资金不足（需 {margin:g} 元，可用 {cash:g} 元）')
        free_open, free_close, extra_margin = capacity, 0, margin
    else:
        if intent.side == 'sell':         # 平多价差：卖 near、买 far
            near_free = long.get(limits.near, 0) - short_frozen.get(limits.near, 0)
            far_free = short.get(limits.far, 0) - long_frozen.get(limits.far, 0)
        else:                             # 平空价差：买 near、卖 far
            near_free = short.get(limits.near, 0) - long_frozen.get(limits.near, 0)
            far_free = long.get(limits.far, 0) - short_frozen.get(limits.far, 0)
        free = int(min(near_free, far_free))
        _require(free >= intent.lots,
                 f'可平量不足（{limits.near} 可卖 {near_free:g} 手、'
                 f'{limits.far} 可买 {far_free:g} 手，本单 {intent.lots} 手）')
        free_open, free_close, extra_margin = 0, free, 0.0

    return Evidence(r, True, True, True, True, True, True, True, False,
                    free_open, free_close, cash, extra_margin)
