"""归属已确认的单个 1:1 组合委托，按同一成交口径计算实成量与成本。"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .journal import Outcome, Record


@dataclass(frozen=True)
class Matched:
    lots: int
    average_spread: Decimal | None
    near_average: Decimal | None
    far_average: Decimal | None
    residual_near: int
    residual_far: int


def matched_fills(rows: Iterable[Record], *, exchange: str, sys_id: str, near: str,
                  far: str, combo: str, buy: bool, max_lots: int) -> Matched:
    """纯计算。混合口径不猜先后，直接隔离；缺一腿不补行情价。

    输入仅限一个账户、一个交易日、同一个交易所委托号。派生腿按流水顺序 FIFO
    配对，不把尚未配对的价格计入已成交组合的成本。CTP Direction 0=买，1=卖。
    """
    parts = {s: [] for s in (near, far, combo)}
    if len(parts) != 3 or not all(parts) or not sys_id or type(max_lots) is not int or max_lots <= 0:
        raise ValueError('无效组合定义/委托上限')
    seen = {}; scope = None
    for row in sorted(rows, key=lambda r:r.sequence):
        if row.outcome == Outcome.CONFLICT:
            raise ValueError('成交证据冲突')
        if row.outcome != Outcome.NEW:
            continue
        p = row.payload
        if row.kind != 'fill' or row.identity[0] != exchange or p.get('order_sys_id') != sys_id:
            raise ValueError('成交不属于此委托')
        if scope is not None and scope != (row.account,row.trading_day):
            raise ValueError('跨账户或交易日的成交')
        scope = row.account,row.trading_day
        if row.identity in seen:
            if seen[row.identity] != p: raise ValueError('同一成交号明细冲突')
            continue
        seen[row.identity] = p
        s = p['symbol']
        if s not in parts or p['direction'] != ('0' if buy != (s == far) else '1'):
            raise ValueError('成交腿或方向不符')
        q, price = Decimal(str(p['volume'])), Decimal(str(p['price']))
        if not price.is_finite() or not q.is_finite() or q <= 0 or q != q.to_integral_value():
            raise ValueError('成交数量或价格无效')
        parts[s].append((int(q),price))
    sizes = {s:sum(q for q,_ in fills) for s,fills in parts.items()}
    if max(sizes.values()) > max_lots: raise ValueError('实际成交超过委托数量')
    def avg(s, count):
        amount = Decimal(0); left = count
        for q, price in parts[s]:
            used = min(left, q); amount += used * price; left -= used
            if not left: break
        return amount / count if count else None
    if sizes[combo]:
        if sizes[near] or sizes[far]: raise ValueError('组合和派生腿混合回报须核实')
        return Matched(sizes[combo],avg(combo,sizes[combo]),None,None,0,0)
    paired = min(sizes[near],sizes[far]); n,f = avg(near,paired),avg(far,paired)
    return Matched(paired,n-f if paired else None,n,f,sizes[near]-paired,sizes[far]-paired)
