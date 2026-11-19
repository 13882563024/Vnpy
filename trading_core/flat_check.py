"""首仓专用的保守证据投影。只处理专用账户、全账户零持仓且无未决委托。
有仓恢复、活跃止盈共存的加仓及平仓需要逐层归属投影，禁止走此捷径。
"""
from dataclasses import dataclass
from math import isfinite

from .intents import OrderIntent
from .journal import Journal, Outcome
from .preflight import Evidence, GateDenied
from .query_round import QuerySnapshot


@dataclass(frozen=True)
class FirstEntryLimits:
    max_open_lots: int
    margin_per_lot: float
    signal_current: bool
    quotes_current: bool
    session_open: bool


def first_entry_evidence(intent: OrderIntent, snapshot: QuerySnapshot, journal: Journal,
                         limits: FirstEntryLimits) -> Evidence:
    r = snapshot.round
    if intent.purpose != 'open' or (intent.account,intent.day,intent.strategy,intent.decision_id) != (
            r.account,r.day,r.strategy,r.decision):
        raise GateDenied('当前核验仅支持已确认空仓的首仓意图')
    if any(p['Position'] or p.get('LongFrozen',0) or p.get('ShortFrozen',0) for p in snapshot.positions):
        raise GateDenied('账户存在持仓或冻结量，必须核对逐层归属')
    # 已成交的历史单也需要验证归属，初版不凭当前净仓推断历史已结清。
    if snapshot.trades or any(o['OrderStatus'] not in ('4','5') or o['VolumeTraded'] for o in snapshot.orders):
        raise GateDenied('存在成交或未确定为零成交终态的委托')
    if len(snapshot.accounts) != 1 or snapshot.accounts[0].get('CurrencyID') != 'CNY':
        raise GateDenied('缺少唯一人民币资金账户')
    if type(limits.max_open_lots) is not int or limits.max_open_lots <= 0 or not (
            type(limits.margin_per_lot) in (int,float) and isfinite(limits.margin_per_lot) and limits.margin_per_lot>0):
        raise GateDenied('首仓额度或保证金未确认')
    for row in journal.replay():
        if row.account != intent.account: continue
        if row.outcome == Outcome.CONFLICT or row.kind in ('order_intent','order_binding'):
            raise GateDenied('账本存在历史意图或冲突，不能用空户首仓核验代替恢复核对')
    return Evidence(r, True,True,True,True,limits.signal_current,limits.quotes_current,
                    limits.session_open,False,limits.max_open_lots,0,
                    snapshot.accounts[0]['Available'],limits.margin_per_lot*intent.lots)
