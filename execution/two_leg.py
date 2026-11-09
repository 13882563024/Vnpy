"""两腿执行器（默认）：主动腿挂限价等成交 + 被动腿对手价 IOC 对冲。

为什么不直接用 ``SpreadTakerAlgo``：它一启动就吃主动腿，语义是"立刻成交"；而止盈的
本质是**挂着等价格回来**。这里把主动腿做成 maker，被动腿成交后立刻对冲，并保留
"一腿成交另一腿未成交"的兜底（重发对冲 + 告警）。

超时口径（方案 3.4 / 5.2）：

- ``take_profit``：**不撤单追价**，一直挂着等目标价成交；
- ``close``（止损/强平/时间止损）：``market_close_timeout`` 秒未成交 → 撤单按最新对手价重发；
- ``open``：``entry_partial_timeout`` 秒后若已部分成交 → 撤剩余，按已成交量建层。
"""

from __future__ import annotations

from vnpy.trader.constant import Direction
from vnpy.trader.object import ContractData, OrderData, TickData
from vnpy.trader.utility import round_to

from vnpy_spreadtrading.base import LegData

from execution.base import AlgoKind


def round_leg_volume(leg: LegData, volume: float) -> float:
    if leg.min_volume:
        return round_to(volume, leg.min_volume)
    return volume


class TwoLegMakerImpl:
    """主动腿 maker + 被动腿 taker 的两腿执行实现。"""

    def __init__(self, algo) -> None:
        self.algo = algo
        self.spread = algo.spread
        self.active_leg: LegData = algo.spread.active_leg
        self.active_symbol: str = self.active_leg.vt_symbol
        self.near_leg: LegData | None = algo.spread.variable_legs.get("near")
        self.far_leg: LegData | None = algo.spread.variable_legs.get("far")
        self.requote_count: int = 0
        self.hedge_warned: bool = False

    # ------------------------------------------------------------ 状态
    def is_hedge_finished(self) -> bool:
        return self.algo.is_hedge_finished()

    def has_active_orders(self) -> bool:
        for vt_orderids in self.algo.leg_orders.values():
            if vt_orderids:
                return True
        return False

    def spread_volume_left(self) -> float:
        return abs(self.algo.target) - abs(self.algo.traded)

    # ------------------------------------------------------------ 价格
    def current_price(self) -> float:
        """本次要用的价差价格。

        - ``close``：每次重发都用**最新对手价**，否则撤单重发没有意义；
        - ``open`` / ``take_profit``：沿用策略给定的价格（对手价 / 止盈目标价）。
        """
        algo = self.algo
        if algo.purpose == AlgoKind.CLOSE:
            spread = self.spread
            if spread.bid_volume and spread.ask_volume:
                price = spread.bid_price if algo.direction == Direction.SHORT else spread.ask_price
                if price:
                    algo.price = price
        return algo.price

    def active_leg_price(self) -> float:
        """把价差价格换算成主动腿的限价。"""
        algo = self.algo
        target = self.current_price()
        near, far = self.near_leg, self.far_leg
        if near is None or far is None:
            return target

        is_near_active = self.active_symbol == near.vt_symbol
        if algo.direction == Direction.LONG:        # 买 near、卖 far
            if is_near_active:
                # near 成交价 ≤ target + far_bid → 价差 ≤ target
                return target + far.bid_price
            return near.ask_price - target
        # 卖 near、买 far
        if is_near_active:
            # near 成交价 ≥ target + far_ask → 价差 ≥ target
            return target + far.ask_price
        return near.bid_price - target

    # ------------------------------------------------------------ 发单
    def start(self) -> None:
        self.send_active_order()

    def send_active_order(self) -> None:
        algo = self.algo
        if algo.paused:
            return
        left = self.spread_volume_left()
        if left <= 1e-9:
            return

        spread_volume = left if algo.direction == Direction.LONG else -left
        available = (
            self.spread.ask_volume if algo.direction == Direction.LONG else self.spread.bid_volume
        )
        if available > 0:
            capped = min(left, available)
            spread_volume = capped if algo.direction == Direction.LONG else -capped

        leg_volume = self.spread.calculate_leg_volume(self.active_symbol, spread_volume)
        if not leg_volume:
            return

        price = self.active_leg_price()
        direction = Direction.LONG if leg_volume > 0 else Direction.SHORT
        self.algo.send_leg_order(
            self.active_symbol,
            price,
            abs(leg_volume),
            direction,
            offset=self.algo.offset,
            fak=False,
        )

    def hedge_passive_legs(self) -> None:
        """对未对冲的被动腿发对手价 IOC 单。"""
        algo = self.algo
        if algo.paused:
            return
        active_traded = round_leg_volume(self.active_leg, algo.leg_traded[self.active_symbol])
        hedge_volume = self.spread.calculate_spread_volume(self.active_symbol, active_traded)

        for leg in self.spread.passive_legs:
            if algo.leg_orders[leg.vt_symbol]:
                continue        # 已有在途对冲单

            passive_traded = round_leg_volume(leg, algo.leg_traded[leg.vt_symbol])
            target = self.spread.calculate_leg_volume(leg.vt_symbol, hedge_volume)
            volume = target - passive_traded
            if not volume:
                continue

            tick: TickData | None = algo.get_tick(leg.vt_symbol)
            contract: ContractData | None = algo.get_contract(leg.vt_symbol)
            if not tick or not contract:
                algo.write_log(f"对冲失败：{leg.vt_symbol} 缺行情或合约信息")
                continue

            if volume > 0:
                price = tick.ask_price_1 + contract.pricetick * algo.payup
                direction = Direction.LONG
            else:
                price = tick.bid_price_1 - contract.pricetick * algo.payup
                direction = Direction.SHORT

            algo.write_log(f"对冲被动腿 {leg.vt_symbol} {direction.value} {abs(volume):g}")
            algo.send_leg_order(
                leg.vt_symbol,
                price,
                abs(volume),
                direction,
                offset=algo.offset,
                fak=True,
            )

    # ------------------------------------------------------------ 钩子
    def on_tick(self, tick: TickData) -> None:
        algo = self.algo
        if algo.paused or not algo.is_active():
            return
        if self.has_active_orders():
            return
        if not self.is_hedge_finished():
            self.hedge_passive_legs()
            return
        if self.spread_volume_left() > 1e-9 and not algo.stopped:
            self.send_active_order()

    def on_order(self, order: OrderData) -> None:
        algo = self.algo
        if algo.paused or not algo.is_active():
            return
        if self.has_active_orders():
            return
        if not self.is_hedge_finished():
            self.hedge_passive_legs()
            return
        if algo.purpose == AlgoKind.CLOSE and not algo.stopped:
            if abs(algo.traded) < abs(algo.target) - 1e-9 and self.spread_volume_left() > 1e-9:
                self.send_active_order()

    def on_trade(self, trade) -> None:
        return

    def on_interval(self) -> None:
        algo = self.algo
        if algo.paused or not algo.is_active():
            return

        if self.has_active_orders():
            if algo.purpose == AlgoKind.CLOSE and algo.market_close_timeout > 0:
                if algo.elapsed >= algo.market_close_timeout:
                    self.requote_count += 1
                    algo.write_log(
                        f"平仓单 {algo.market_close_timeout:g}s 未成交，撤单按最新对手价重发"
                        f"（第 {self.requote_count} 次）"
                    )
                    algo.cancel_all_order()
            elif algo.purpose == AlgoKind.OPEN and algo.entry_partial_timeout > 0:
                if algo.elapsed >= algo.entry_partial_timeout and abs(algo.traded) > 0:
                    algo.write_log("开仓单部分成交超时，撤销剩余部分")
                    algo.cancel_all_order()
            return

        if not self.is_hedge_finished():
            self.hedge_passive_legs()
            return

        if algo.purpose == AlgoKind.CLOSE and self.spread_volume_left() > 1e-9 and not algo.stopped:
            self.send_active_order()

    def cancel_all(self) -> None:
        """撤掉本算法发出的全部腿单（绕开模板的 cancel_leg_order 以避免递归）。"""
        algo = self.algo
        if algo.paused:
            return
        for vt_orderids in list(algo.leg_orders.values()):
            for vt_orderid in list(vt_orderids):
                algo.algo_engine.cancel_order(algo, vt_orderid)

    def force_finish(self) -> None:
        for vt_orderids in self.algo.leg_orders.values():
            vt_orderids.clear()

    def is_order_finished(self) -> bool:
        return not self.has_active_orders()
