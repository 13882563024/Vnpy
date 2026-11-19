"""按已归属的实际组合成交核算单层收益；禁止用目标价或信号价冒充成交。"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LayerPnL:
    remaining: int
    realized_gross: Decimal
    realized_net: Decimal | None
    unrealized_gross: Decimal | None
    unallocated_open_fee: Decimal | None


def layer_pnl(*, direction: int, opened: int, entry: Decimal, multiplier: Decimal,
              exits: list[tuple[int, Decimal]], open_fee: Decimal | None,
              paid_exit_fee: Decimal | None, spread_bid: Decimal | None,
              spread_ask: Decimal | None) -> LayerPnL:
    """open_fee 为整层已付开仓费；paid_exit_fee 为已平部分累计实付费用。

    此函数计算完整层的投影视图，不做增量入账。未实现采用可退出价，不扣估算费用。
    """
    if type(direction) is not int or direction not in (-1, 1) or type(opened) is not int or opened <= 0:
        raise ValueError('无效层方向/手数')
    if not entry.is_finite() or not multiplier.is_finite() or multiplier <= 0:
        raise ValueError('无效实际成交价格或合约乘数')
    for fee in (open_fee, paid_exit_fee):
        if fee is not None and (not fee.is_finite() or fee < 0):
            raise ValueError('无效手续费')
    gross = Decimal(0); closed = 0
    for q, px in exits:
        if type(q) is not int or q <= 0 or closed + q > opened or not px.is_finite():
            raise ValueError('无效平仓明细')
        gross += direction * (px-entry) * multiplier * q
        closed += q
    remaining = opened-closed
    allocated = open_fee*closed/opened if open_fee is not None else None
    net = (Decimal(0) if not closed else gross-allocated-paid_exit_fee
           if allocated is not None and paid_exit_fee is not None else None)
    mark = spread_bid if direction == 1 else spread_ask
    unrealized = Decimal(0) if not remaining else (
        direction*(mark-entry)*multiplier*remaining if mark is not None and mark.is_finite() else None)
    return LayerPnL(remaining,gross,net,unrealized,
                    None if open_fee is None else open_fee-allocated)
