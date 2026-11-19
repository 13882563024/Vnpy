"""交易所原生套利执行器：直接对挂牌组合合约报一条价差限价单。

可行性已由使用者实盘经验确认（CTP 可以直接对 ``SP a2611&a2701`` 这类组合合约下单，
成交后**持仓按单腿登记**），但 ``vnpy_ctp`` 的回报解析仍需账号实测，因此这里做了
两种回报形态的兼容：

- **组合口径**：成交回报的 ``InstrumentID`` 就是组合代码 → 自己累加成交量/成交额；
- **腿口径**：成交回报是两条单腿成交（``THOST_FTDC_TRDT_CombinationDerived``）
  → 回退到 ``SpreadAlgoTemplate`` 的按腿统计（``calculate_traded_volume``）。

哪一种是真实形态由**第一次成交回报**决定，并打日志说明，便于实盘核对（方案 5.3.4）。
"""

from __future__ import annotations

from vnpy.trader.constant import Status
from vnpy.trader.object import OrderData, TickData, TradeData
from vnpy.trader.utility import round_to

from execution.base import AlgoKind


class ExchangeSpreadImpl:
    """组合单执行实现（一条委托，无腿裸露窗口）。"""

    def __init__(self, algo) -> None:
        self.algo = algo
        self.vt_symbol: str = algo.exchange_spread_vt_symbol
        self.spread_symbol: str = algo.exchange_spread
        self.order_ids: list[str] = []
        self.active_order_ids: set[str] = set()

        self.traded_volume: float = 0.0
        self.traded_amount: float = 0.0
        self.traded_price: float = 0.0

        # None = 尚未收到成交回报；True = 组合口径；False = 腿口径（回退基类统计）
        self.spread_mode_reports: bool | None = None
        self.rejected_count: int = 0
        self.contract_missing: bool = False
        # 组合单串行发送。记录发单时已匹配的手数，将委托累计成交换算为
        # 算法累计成交门槛（单腿回报不能把两腿数量直接相加）。
        self.fill_baseline: dict[str, float] = {}
        self.awaiting_fills: dict[str, float] = {}
        self.cancelled_order = False
        self.cancel_started: int | None = None
        self.cancel_warned = False

    # ------------------------------------------------------------ 发单
    def start(self) -> None:
        self.send_order()

    def current_price(self) -> float:
        algo = self.algo
        if algo.purpose == AlgoKind.CLOSE:
            from vnpy.trader.constant import Direction

            spread = algo.spread
            if spread.bid_volume and spread.ask_volume:
                price = spread.bid_price if algo.direction == Direction.SHORT else spread.ask_price
                if price:
                    algo.price = price
        return algo.price

    def send_order(self) -> None:
        algo = self.algo
        if algo.paused or algo.stopped or not self.is_order_finished():
            return
        contract = algo.get_contract(self.vt_symbol)
        if contract is None:
            if not self.contract_missing:
                self.contract_missing = True
                algo.write_log(
                    f"组合合约 {self.vt_symbol} 不在合约列表，组合单不可用 —— "
                    f"请检查 spreads.json 的 exchange_spread/exchange，或切换 executor=two_leg"
                )
            return

        remaining = abs(algo.target) - abs(self.traded_volume)
        if remaining <= 1e-9:
            return

        price = round_to(self.current_price(), contract.pricetick or algo.spread.pricetick or 0.01)
        vt_orderids = algo.send_spread_order(contract, price, remaining, algo.direction)
        if not vt_orderids:
            if getattr(algo, "is_gate_pending", lambda: False)():
                return          # 还在逐笔核验里排队，没发出单是正常的
            algo.write_log(
                f"组合单发送失败：{contract.symbol} {algo.direction.value} "
                f"{remaining:g}@{price:g}偏移={algo.offset.value}"
            )
            # 发送路径直接返回空（闸门拒发/没拿到本地单号）时不会再有委托回报，
            # 算法会一直挂着、策略以为止盈已挂。这里主动收尾，让策略拿到终态重挂。
            self._finish_if_dead()
            return

        for vt_orderid in vt_orderids:
            self.fill_baseline[vt_orderid] = self.traded_volume
        self.order_ids.extend(vt_orderids)
        self.active_order_ids.update(vt_orderids)
        algo.write_log(
            f"发出组合单[{ '|'.join(vt_orderids) }] {contract.symbol} "
            f"{algo.direction.value} {remaining:g}@{price:g} offset={algo.offset.value} "
            f"（偏移标志 {algo.offset_flag_width} 位）"
        )

    # ------------------------------------------------------------ 回报
    def accept_sent_order(self, vt_orderid: str) -> None:
        """逐笔核验通过后才真正发出的单：补登记，纳入成交匹配基线。

        算法在 ``submit`` 阶段并没有拿到委托号（那时还没发单），所以这里由
        派发回调补上；``fill_baseline`` 必须早于任何成交回报写入。
        """
        if vt_orderid in self.order_ids:
            return
        self.fill_baseline[vt_orderid] = self.traded_volume
        self.order_ids.append(vt_orderid)
        self.active_order_ids.add(vt_orderid)

    def on_trade(self, trade: TradeData) -> None:
        algo = self.algo
        if trade.vt_orderid not in self.active_order_ids and trade.vt_orderid not in self.order_ids:
            return

        combo_report = trade.vt_symbol in (self.vt_symbol, self.spread_symbol)
        if self.spread_mode_reports is not None and self.spread_mode_reports != combo_report:
            algo.write_log(f"忽略与首笔成交口径不一致的回报 {trade.vt_tradeid}")
            return
        if combo_report:
            if self.spread_mode_reports is None:
                self.spread_mode_reports = True
                algo.write_log("成交回报按【组合代码】推送（组合口径统计生效）")
            self.traded_volume += trade.volume
            self.traded_amount += trade.volume * trade.price
            self.traded_price = self.traded_amount / self.traded_volume
        else:
            if self.spread_mode_reports is None:
                self.spread_mode_reports = False
                algo.write_log(f"成交回报按【单腿代码】推送（{trade.vt_symbol}），改用按腿统计")
            self._sync_from_legs()

        algo.calculate_traded_volume()
        algo.calculate_traded_price()
        goal = self.awaiting_fills.get(trade.vt_orderid)
        if goal is not None and self.traded_volume + 1e-9 >= goal:
            del self.awaiting_fills[trade.vt_orderid]
        if self.cancelled_order and algo.is_active():
            self._finish_if_dead()
        # update_trade 不像 update_order 那样在钩子后复查 stopped。
        if algo.stopped and algo.is_active():
            algo.check_algo_cancelled()
        if algo.status != Status.CANCELLED:  # check_algo_cancelled 已发布终态
            algo.put_event()

    def _sync_from_legs(self) -> None:
        """把两腿成交量折算成已匹配的价差手数。

        组合单的成交回报可能按单腿推送（组合合约本身没有持仓）。若只依赖
        ``traded_volume``，两腿都成交后算法仍会停在 0 手，策略不会建层，也就不会
        预挂止盈。这里取两腿共同的、已匹配的最小价差手数。
        """
        algo = self.algo
        spread = algo.spread
        volumes: list[float] = []
        for leg in spread.legs.values():
            traded = float(algo.leg_traded.get(leg.vt_symbol, 0) or 0)
            volumes.append(
                spread.calculate_spread_volume(leg.vt_symbol, traded))

        matched = 0.0
        if volumes and all(abs(value) > 1e-9 for value in volumes):
            signs = {1 if value > 0 else -1 for value in volumes}
            if len(signs) == 1:
                matched = min(volumes, key=abs)

        self.traded_volume = abs(matched)
        if self.traded_volume:
            prices: list[float] = []
            for leg in spread.legs.values():
                traded = float(algo.leg_traded.get(leg.vt_symbol, 0) or 0)
                cost = float(algo.leg_cost.get(leg.vt_symbol, 0) or 0)
                prices.append(abs(cost / traded) if abs(traded) > 1e-9 else 0.0)
            if len(prices) == 2 and all(prices):
                self.traded_price = prices[0] - prices[1]

    def on_order(self, order: OrderData) -> None:
        if order.vt_orderid not in self.order_ids:
            return
        if order.status in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED):
            self.active_order_ids.discard(order.vt_orderid)
            # ALLTRADED 即使 traded 字段滞后也必须等足整单成交。
            expected = max(order.volume, order.traded) if order.status == Status.ALLTRADED else order.traded
            goal = self.fill_baseline.get(order.vt_orderid, 0.0) + expected
            if self.traded_volume + 1e-9 < goal and goal > self.awaiting_fills.get(order.vt_orderid, 0):
                self.awaiting_fills[order.vt_orderid] = goal
                self.algo.write_log(
                    f"等待成交明细 {order.vt_orderid}：委托{order.status.value}，"
                    f"算法已匹配 {self.traded_volume:g}/{goal:g} 手；核对前不重发、不收尾"
                )
            if not self.active_order_ids:
                self.cancel_started = None
        if order.status == Status.REJECTED:
            self.rejected_count += 1
            self.algo.write_log(
                f"组合单被拒（第 {self.rejected_count} 次）：{order.symbol} "
                f"offset={order.offset.value} 报单引用={order.reference or '—'}"
                f"（拒单原因见同一时刻 CTP 网关日志）"
            )
        if order.status in (Status.REJECTED, Status.CANCELLED):
            self.cancelled_order = True
            self._finish_if_dead()

    def _finish_if_dead(self) -> None:
        """委托全部终止后结束算法，把终态交回策略。

        模板只在 ``stopped`` 时才把状态推到 CANCELLED，因此**被拒 / 被动撤单**的算法
        会一直停在 RUNNING：策略侧等不到终态，``exit_algoid`` 与 ``layer.algoid``
        也就永远不清空，止盈单一旦被拒就再也不会重挂（实盘表现为仓位裸奔）。
        这里在确认没有在途委托后主动走 ``stop()``，让策略收到 CANCELLED 并重挂。
        """
        algo = self.algo
        if not self.is_order_finished() or algo.paused or algo.stopped:
            return
        algo.stop()

    def on_tick(self, tick: TickData) -> None:
        return

    # ------------------------------------------------------------ 超时
    def on_interval(self) -> None:
        algo = self.algo
        if algo.paused or not algo.is_active():
            return

        if self.awaiting_fills:
            return
        if not self.active_order_ids:
            if algo.stopped:
                return
            if algo.purpose == AlgoKind.CLOSE:
                if abs(self.traded_volume) < abs(algo.target) - 1e-9:
                    self.send_order()
            return

        if self.cancel_started is not None:
            if not self.cancel_warned and algo.elapsed - self.cancel_started >= 10:
                self.cancel_warned = True
                algo.write_log("撤单回报超时：保留在途组合单，不重发；请人工核对柜台委托及成交")
            return
        if algo.stopped:
            return

        if algo.purpose == AlgoKind.CLOSE and algo.market_close_timeout > 0:
            if algo.elapsed >= algo.market_close_timeout:
                algo.write_log(
                    f"组合平仓单 {algo.market_close_timeout:g}s 未成交，撤单按最新对手价重发"
                )
                self.cancel_all()
        elif algo.purpose == AlgoKind.OPEN and algo.entry_partial_timeout > 0:
            if algo.elapsed >= algo.entry_partial_timeout and self.traded_volume > 0:
                algo.write_log("组合开仓单部分成交超时，撤销剩余部分")
                self.cancel_all()

    # ------------------------------------------------------------ 统计
    def calculate_traded_volume(self) -> None:
        from vnpy.trader.constant import Direction

        algo = self.algo
        signed = self.traded_volume if algo.direction == Direction.LONG else -self.traded_volume
        algo.traded = signed
        algo.traded_volume = abs(signed)

        target = algo.target
        if target > 0 and signed >= target:
            algo.status = Status.ALLTRADED
        elif target < 0 and signed <= target:
            algo.status = Status.ALLTRADED
        elif not signed:
            algo.status = Status.NOTTRADED
        else:
            algo.status = Status.PARTTRADED

    def calculate_traded_price(self) -> None:
        algo = self.algo
        if not self.traded_volume:
            algo.traded_price = 0
            return
        pricetick = algo.spread.pricetick
        price = self.traded_price
        algo.traded_price = round_to(price, pricetick) if pricetick else price

    # ------------------------------------------------------------ 收尾
    def cancel_all(self) -> None:
        algo = self.algo
        if algo.paused or self.cancel_started is not None:
            return
        if self.active_order_ids:
            self.cancel_started = algo.elapsed
            self.cancel_warned = False
        for vt_orderid in list(self.active_order_ids):
            algo.algo_engine.cancel_order(algo, vt_orderid)

    def is_order_finished(self) -> bool:
        return not self.active_order_ids and not self.awaiting_fills
