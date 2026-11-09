"""``CrossPeriodAlgo``：策略的唯一算法入口，内部按 ``extra["mode"]`` 分派。

为什么要自带一个 algo：``vnpy_spreadtrading`` 只提供 ``SpreadTakerAlgo``（主动腿吃单 +
被动腿对冲），语义是"立刻成交"；而本策略的核心是**预挂止盈单等价格回来**，两者相反。
另外 ``SpreadTakerAlgo`` 走 ``SpreadAlgoEngine.send_order``，那里 offset 被硬编码为
``Offset.OPEN``，无法用于平仓（会把止盈变成反向开仓），所以这里自己发单。

接入方式（``SpreadAlgoEngine.algo_class`` 是类属性、全局唯一）::

    spread_engine.algo_engine.algo_class = CrossPeriodAlgo

``extra`` 约定：

============================  ====================================================
``mode``                      ``two_leg`` / ``exchange_spread``
``purpose``                   ``open`` / ``take_profit`` / ``close``
``exchange_spread``           组合合约原始字符串（含空格与 ``&``，不 strip）
``exchange_spread_vt_symbol`` 组合合约 vt_symbol（已带交易所后缀）
``offset_flag_width``         CTP 组合单 ``CombOffsetFlag`` 位数（1 或 2）
``market_close_timeout``      平仓单未成交多少秒后撤单按新对手价重发
``entry_partial_timeout``     开仓单部分成交后多少秒收尾（撤剩余 + 按已成交量建层）
``offset``                    ``open`` / ``close``（缺省由 purpose 推断）
============================  ====================================================
"""

from __future__ import annotations

from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.object import OrderData, TickData, TradeData
from vnpy.trader.utility import round_to

from vnpy_spreadtrading.template import SpreadAlgoTemplate
from vnpy_spreadtrading.base import SpreadData

from execution.base import AlgoKind, AlgoImpl
from utils.spread_config import EXECUTOR_EXCHANGE_SPREAD, EXECUTOR_TWO_LEG


class CrossPeriodAlgo(SpreadAlgoTemplate):
    """跨期均值回归策略的执行算法（两腿 maker / 交易所组合单）。"""

    algo_name: str = "CrossPeriod"

    def __init__(
        self,
        algo_engine,
        algoid: str,
        spread: SpreadData,
        direction: Direction,
        price: float,
        volume: float,
        payup: int,
        interval: int,
        lock: bool,
        extra: dict,
    ) -> None:
        super().__init__(
            algo_engine, algoid, spread, direction,
            price, volume, payup, interval, lock, extra or {},
        )

        extra = extra or {}
        self.purpose: str = str(extra.get("purpose", AlgoKind.OPEN))
        self.mode: str = str(extra.get("mode", EXECUTOR_TWO_LEG)).strip().lower()
        self.exchange_spread: str = str(extra.get("exchange_spread", "") or "")
        self.exchange_spread_vt_symbol: str = str(extra.get("exchange_spread_vt_symbol", "") or "")
        self.offset_flag_width: int = int(extra.get("offset_flag_width", 1) or 1)
        self.market_close_timeout: float = float(extra.get("market_close_timeout", 5.0) or 0.0)
        self.entry_partial_timeout: float = float(extra.get("entry_partial_timeout", 10.0) or 0.0)
        self.reason: str = str(extra.get("reason", "") or "")
        self._order_status_log: dict[str, str] = {}

        offset_text = str(extra.get("offset", "") or "").lower()
        if offset_text == "close":
            self.offset = Offset.CLOSE
        elif offset_text == "open":
            self.offset = Offset.OPEN
        else:
            self.offset = (
                Offset.CLOSE
                if self.purpose in (AlgoKind.TAKE_PROFIT, AlgoKind.CLOSE)
                else Offset.OPEN
            )

        self.elapsed: int = 0
        self.stop_elapsed: int = 0
        self.impl: AlgoImpl | None = None
        self.paused: bool = False
        self._build_impl()

    def pause(self) -> None:
        """不停止算法、也不撤已有委托；回报仍可被动记账。"""
        self.paused = True
        self.write_log("算法暂停：保留在途委托，成交/订单回报继续核对，不自动对冲")

    def resume(self, *, reconciled: bool = False) -> bool:
        """禁止对尚在途的旧算法解冻，防止使用暂停前目标价。"""
        if not reconciled or self.is_active():
            return False
        self.paused = False
        return True

    # ------------------------------------------------------------ 构造
    def _build_impl(self) -> None:
        if self.mode == EXECUTOR_EXCHANGE_SPREAD and self.exchange_spread_vt_symbol:
            from execution.exchange_spread import ExchangeSpreadImpl

            self.impl = ExchangeSpreadImpl(self)
        elif self.mode == EXECUTOR_TWO_LEG:
            from execution.two_leg import TwoLegMakerImpl

            self.impl = TwoLegMakerImpl(self)
        else:
            self.write_log("组合合约不可用或未知执行器：拒绝隐式回退两腿模式")
            return

        try:
            self.impl.start()
        except Exception as exc:  # noqa: BLE001 - 启动失败要让策略看见而不是静默
            self.write_log(f"算法启动失败：{exc!r}")

    # ------------------------------------------------------------ 属性
    @property
    def main_engine(self):
        return getattr(self.algo_engine, "main_engine", None)

    # ------------------------------------------------------------ 发单
    def send_leg_order(
        self,
        vt_symbol: str,
        price: float,
        volume: float,
        direction: Direction,
        *,
        offset: Offset | None = None,
        fak: bool = False,
    ) -> list[str]:
        """发送一条腿单（自定义 offset），并登记回算法引擎的映射。

        ``SpreadAlgoEngine.send_order`` 的 offset 硬编码为 OPEN，无法平仓；这里复刻它的
        其余逻辑（涨跌停夹紧、订单映射、价差映射），只把 offset 交给调用方。
        """
        if self.paused:
            return []
        contract = self.get_contract(vt_symbol)
        if not contract:
            self.write_log(f"发单失败：找不到合约 {vt_symbol}")
            return []

        main_engine = self.main_engine
        if main_engine is None:                     # 回测引擎不发真实委托
            return []

        leg = self.spread.legs.get(vt_symbol)
        if leg is not None:
            if leg.min_volume:
                volume = round_to(volume, leg.min_volume)
            if leg.pricetick:
                price = round_to(price, leg.pricetick)
        if volume <= 0:
            return []

        tick: TickData | None = self.get_tick(vt_symbol)
        if tick:
            if direction == Direction.LONG and tick.limit_up:
                price = min(price, tick.limit_up)
            elif direction == Direction.SHORT and tick.limit_down:
                price = max(price, tick.limit_down)

        req = self._make_request(
            contract=contract,
            price=price,
            volume=volume,
            direction=direction,
            offset=offset if offset is not None else self.offset,
            fak=fak,
        )

        net: bool = not self.lock
        vt_orderids: list[str] = []
        for converted in main_engine.convert_order_request(req, contract.gateway_name, self.lock, net):
            self._send_raw_request(converted, contract.gateway_name, vt_symbol, vt_orderids)

        if not vt_orderids:
            self.write_log(
                f"发单未成功：{vt_symbol} {direction.value} {volume:g}@{price:g} "
                f"offset={req.offset.value}"
            )
        return vt_orderids

    def send_spread_order(self, contract, price: float, volume: float, direction: Direction) -> list[str]:
        """直接对交易所组合合约发一条价差单（不经 offset 转换）。

        组合合约的持仓按单腿登记，组合合约本身没有持仓数据；若走
        ``convert_order_request``，平仓单会被判成"无可平持仓"并**转成开仓**，
        造成反向开仓。这里直接下发原始委托。
        """
        main_engine = self.main_engine
        if main_engine is None or self.paused:
            return []

        req = self._make_request(
            contract=contract,
            price=round_to(price, contract.pricetick or self.spread.pricetick or 0.01),
            volume=volume,
            direction=direction,
            offset=self.offset,
            fak=False,
        )
        vt_orderids: list[str] = []
        self._send_raw_request(req, contract.gateway_name, contract.vt_symbol, vt_orderids)
        return vt_orderids

    def _make_request(self, contract, price: float, volume: float,
                      direction: Direction, offset: Offset, fak: bool):
        from vnpy.trader.object import OrderRequest

        return OrderRequest(
            symbol=contract.symbol,
            exchange=contract.exchange,
            direction=direction,
            offset=offset,
            type=OrderType.FAK if fak else OrderType.LIMIT,
            price=float(price),
            volume=float(volume),
            reference=f"CrossPeriod_{self.spread_name}_{self.purpose}",
        )

    def _send_raw_request(self, req, gateway_name: str, vt_symbol: str,
                          vt_orderids: list[str]) -> None:
        main_engine = self.main_engine
        if self.paused:
            return
        vt_orderid = main_engine.send_order(req, gateway_name)
        if not vt_orderid:
            self.write_log(f"委托请求被拒绝：{req.symbol} {req.direction.value} "
                           f"{req.volume:g}@{req.price:g}")
            return

        vt_orderids.append(vt_orderid)
        # 保持"委托号 → 算法"的路由，回报才能回到 update_order / update_trade
        order_algo_map = getattr(self.algo_engine, "order_algo_map", None)
        if order_algo_map is not None:
            order_algo_map[vt_orderid] = self

        data_engine = getattr(self.algo_engine, "data_engine", None)
        if data_engine is not None:
            data_engine.update_order_spread_map(vt_orderid, self.spread)

        self.leg_orders[vt_symbol].append(vt_orderid)
        self.write_log(
            f"报单已提交 {vt_orderid} {req.vt_symbol} "
            f"{req.direction.value}/{req.offset.value} {req.volume:g}@{req.price:g} "
            f"{req.type.value} 用途={self.purpose} 模式={self.mode} 策略={self.spread_name} "
            f"算法={self.algoid}" + (f" 依据={self.reason}" if self.reason else "")
        )

    # ------------------------------------------------------------ 模板钩子
    def stop(self) -> None:
        if self.paused or not self.is_active():
            return
        self.write_log("算法停止中")
        self.stopped = True
        self.stop_elapsed = self.elapsed
        self.cancel_all_order()
        self.check_algo_cancelled()

    def on_tick(self, tick: TickData) -> None:
        if self.paused:
            return
        if self.impl:
            self.impl.on_tick(tick)

    def on_order(self, order: OrderData) -> None:
        status = order.status.value
        if status != self._order_status_log.get(order.vt_orderid):
            self._order_status_log[order.vt_orderid] = status
            # 拒单/撤单由模板统一打印，这里补状态迁移与成交量，避免重复。
            if order.status not in (Status.REJECTED, Status.CANCELLED):
                self.write_log(
                    f"委托状态 {order.vt_orderid} {status} "
                    f"成交{order.traded:g}/{order.volume:g} 价{order.price:g} "
                    f"{order.direction.value}/{order.offset.value} 算法={self.algoid}"
                    + ("（策略暂停中，仅记账）" if self.paused else "")
                )
        if self.impl:
            self.impl.on_order(order)

    def on_trade(self, trade: TradeData) -> None:
        if self.paused:
            self.write_log(
                f"暂停期间成交 {trade.vt_orderid} {trade.direction.value}/{trade.offset.value} "
                f"{trade.volume:g}@{trade.price:g} 算法={self.algoid}"
                f"（仅被动记账，不触发对冲/平仓）"
            )
        if self.impl:
            self.impl.on_trade(trade)

    def on_interval(self) -> None:
        if self.paused:
            return
        self.elapsed += 1
        # 撤单回报丢失的兜底：停止 15s 后强制收尾，避免算法长期卡在"停止中"
        if self.stopped and self.elapsed - self.stop_elapsed >= 15:
            self.write_log("撤单确认超时 15s，强制结束算法（请人工核对挂单）")
            if self.impl is not None:
                self.impl.force_finish()
            self.leg_orders.clear()
            self.status = Status.CANCELLED
            self.put_event()
            return
        if self.impl:
            self.impl.on_interval()

    # ------------------------------------------------------------ 覆写
    def update_trade(self, trade: TradeData) -> None:
        """成交推送可能先于委托推送到达（组合单尤其如此），先补一个占位委托。"""
        if trade.vt_orderid not in self.orders:
            self.orders[trade.vt_orderid] = OrderData(
                symbol=trade.symbol,
                exchange=trade.exchange,
                orderid=trade.orderid,
                type=OrderType.LIMIT,
                direction=trade.direction,
                offset=trade.offset,
                price=trade.price,
                volume=trade.volume,
                traded=trade.volume,
                status=Status.ALLTRADED,
                datetime=trade.datetime,
                gateway_name=trade.gateway_name,
            )
        super().update_trade(trade)

    def calculate_traded_volume(self) -> None:
        if self.mode == EXECUTOR_EXCHANGE_SPREAD and self.impl is not None:
            self.impl.calculate_traded_volume()
            return
        super().calculate_traded_volume()

    def calculate_traded_price(self) -> None:
        if self.mode == EXECUTOR_EXCHANGE_SPREAD and self.impl is not None:
            self.impl.calculate_traded_price()
            return
        super().calculate_traded_price()

    def is_order_finished(self) -> bool:
        if self.mode == EXECUTOR_EXCHANGE_SPREAD and self.impl is not None:
            return self.impl.is_order_finished()
        return super().is_order_finished()

    def cancel_all_order(self) -> None:
        if self.paused:
            return
        if self.impl is not None:
            self.impl.cancel_all()
            return
        super().cancel_all_order()
