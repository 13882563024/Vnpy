"""重启后恢复的“在途委托”占位算法。

真正的 ``CrossPeriodAlgo`` 依赖内存里的报价/对冲状态，无法从磁盘重建；
这里只提供一个只读跟踪对象：让价差算法引擎和在途算法列表仍然认领这笔委托，
收到成交/撤单回报时更新状态，但绝不发单、撤单或对冲。
"""
from __future__ import annotations

from vnpy.trader.object import OrderData, TradeData


class RecoveredAlgo:
    """A minimal SpreadAlgoTemplate-compatible placeholder for a live order."""

    def __init__(self, algoid: str, spread, order: OrderData) -> None:
        self.algoid = algoid
        self.spread = spread
        self.orders: dict[str, OrderData] = {order.vt_orderid: order}
        self.order_trade_volume: dict[str, float] = {
            order.vt_orderid: float(order.traded or 0)
        }
        self.paused = True
        self.status = order.status
        self.done = not order.is_active()

    # --- 生命周期：永远保持“只跟踪” ---------------------------------
    def is_active(self) -> bool:
        return any(order.is_active() for order in self.orders.values())

    def is_order_finished(self) -> bool:
        return not self.is_active()

    def pause(self) -> None:
        self.paused = True

    def stop(self) -> None:
        self.paused = True

    def update_timer(self) -> None:
        return

    def update_tick(self, tick) -> None:
        return

    def put_event(self) -> None:
        return

    def write_log(self, msg: str) -> None:
        return

    # --- 回报：只更新记录，不发新单 ---------------------------------
    def update_order(self, order: OrderData) -> None:
        if order.vt_orderid in self.orders:
            self.orders[order.vt_orderid] = order
            self.status = order.status

    def update_trade(self, trade: TradeData) -> None:
        if trade.vt_orderid in self.orders:
            self.order_trade_volume[trade.vt_orderid] = (
                self.order_trade_volume.get(trade.vt_orderid, 0.0) + float(trade.volume or 0)
            )
