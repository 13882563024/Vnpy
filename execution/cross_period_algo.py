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

from time import monotonic

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
        self.extra: dict = dict(extra)
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
        self.stop_timeout_warned: bool = False
        self.impl: AlgoImpl | None = None
        self.paused: bool = False
        # 逐笔核验派发器：None = 网关不支持（纸面/回测）或尚未创建
        self._gate = None
        self._gate_name: str = ""
        # 排队中的报单：柜台一次只允许一轮逐笔查询，别的合约在核验时先记住请求，
        # 每秒重试直到排上；否则止盈单会"一次没排上就永远不再尝试"。
        self._gate_wait_req = None
        self._gate_wait_at: float = 0.0
        self._gate_wait_logged: bool = False
        # 「核验上下文缺失」的具体原因 -> 上次打印时间（同一原因 60s 一次，避免刷屏）
        self._gate_ctx_log_at: dict[str, float] = {}
        self._build_impl()

    def pause(self) -> None:
        """不停止算法、也不撤已有委托；回报仍可被动记账。"""
        self.paused = True
        self._cancel_gate('算法暂停，放弃未完成的逐笔核验')
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
        if self.paused or main_engine is None:
            return
        gate = self._order_gate(gateway_name)
        if gate is not None:
            if self.mode != EXECUTOR_EXCHANGE_SPREAD:
                self.write_log(
                    "拒绝发单：逐笔核验只支持交易所组合单（mode=exchange_spread），"
                    "两腿模式尚未接入核验凭证"
                )
                return
            self._send_with_gate(gate, req, vt_symbol, vt_orderids)
            return
        vt_orderid = main_engine.send_order(req, gateway_name)
        if not vt_orderid:
            self.write_log(f"委托请求被拒绝：{req.symbol} {req.direction.value} "
                           f"{req.volume:g}@{req.price:g}")
            return
        self._register_sent(req, vt_symbol, vt_orderid, vt_orderids)

    # ------------------------------------------------------------ 逐笔核验
    def _order_gate(self, gateway_name: str):
        """可用的逐笔核验派发器；纸面/回测引擎返回 None（走原路径）。"""
        engine = self.main_engine
        if engine is None or not callable(getattr(engine, "get_gateway", None)):
            return None
        from execution.order_gate import OrderGate

        if self._gate is None or self._gate_name != gateway_name:
            self._gate = OrderGate(engine, gateway_name)
            self._gate_name = gateway_name
        return self._gate if self._gate.enabled else None

    def _send_with_gate(self, gate, req, vt_symbol: str, vt_orderids: list[str]) -> None:
        """先申请核验、后续再派发；同步路径只负责"这一秒能不能发"。"""
        if gate.busy:
            pending, vt_orderid, error = gate.pump()
            if error:
                self._gate_failed(error)
                return
            if not vt_orderid or pending is None:
                return
            self._register_sent(pending, pending.vt_symbol, vt_orderid, vt_orderids)
            return
        error = self._submit_gate(gate, req)
        if not error:
            self._gate_wait_req = None
            self._gate_wait_logged = False
            self.write_log(
                f"已申请逐笔核验：{req.symbol} {req.direction.value} {req.volume:g}@{req.price:g} "
                f"用途={self.purpose}；四项柜台查询返回后自动发单"
            )
            return
        if '进行中' in error:
            # 别的合约正在核验：排队，由 on_interval 每秒重试。
            self._gate_wait_req = req
            self._gate_wait_at = monotonic()
            if not self._gate_wait_logged:
                self._gate_wait_logged = True
                self.write_log(f"逐笔核验排队：{error}；本单每秒重试直到排上")
            return
        self.write_log(f"无法开始逐笔核验：{error}")

    def _submit_gate(self, gate, req) -> str:
        intent, limits = self._gate_payload(req)
        if intent is None or limits is None:
            return ("拒绝发单：缺少逐笔核验上下文（账户/交易日/配置版本/层/保证金），"
                    "请先在面板保存完整配置")
        return gate.submit(req, intent, limits)

    def _pump_gate(self) -> None:
        """每秒推进一次：在途核验优先，其次重试排队中的报单。"""
        gate = self._gate
        if gate is None:
            return
        if gate.busy:
            pending, vt_orderid, error = gate.pump()
            if error:
                self._gate_failed(error)
                return
            if vt_orderid and pending is not None:
                self._register_sent(pending, pending.vt_symbol, vt_orderid, [])
            return
        req = self._gate_wait_req
        if req is None:
            return
        if monotonic() - self._gate_wait_at > 60.0:
            self._gate_failed("排队超过 60 秒仍未排上柜台核验")
            return
        error = self._submit_gate(gate, req)
        if not error:
            self._gate_wait_req = None
            self._gate_wait_logged = False
            self.write_log(
                f"排队成功，已申请逐笔核验：{req.symbol} {req.direction.value} "
                f"{req.volume:g}@{req.price:g} 用途={self.purpose}")
            return
        if '进行中' not in error:
            self._gate_failed(error)

    def _gate_failed(self, error: str) -> None:
        """核验没通过：记录原因并结束本算法，让策略拿到终态后自己重挂。

        不结束算法的话，``layer.algoid`` 会一直占着止盈槽位，策略以为止盈已挂，
        这一层就再也没有止盈单了（它不会因为"算法没发出去单"而重试）。
        """
        self.write_log(f"逐笔核验未通过：{error}")
        self._gate_wait_req = None
        self._gate_wait_logged = False
        if self.is_active() and not self.stopped and not self.paused:
            self.stop()

    def is_gate_pending(self) -> bool:
        """是否还在逐笔核验里排队/等待派发（此时"没发出单"是正常的，不是失败）。"""
        return self._gate_wait_req is not None or (self._gate is not None and self._gate.busy)

    def _cancel_gate(self, reason: str) -> None:
        gate = self._gate
        if gate is not None and gate.busy:
            gate.cancel(reason)
            self.write_log(f"逐笔核验已放弃：{reason}")
        if self._gate_wait_req is not None:
            self._gate_wait_req = None
            self._gate_wait_logged = False
            self.write_log(f"逐笔核验排队已取消：{reason}")

    def _log_gate_context_missing(self, reason: str) -> None:
        """说清楚核验上下文到底缺哪一项；同一原因 60 秒一次，避免排队重试时刷屏。"""
        now = monotonic()
        if now - self._gate_ctx_log_at.get(reason, 0.0) < 60:
            return
        self._gate_ctx_log_at[reason] = now
        self.write_log(f"逐笔核验上下文缺失：{reason}")

    def _gate_payload(self, req):
        """把当前算法上下文翻译成 (OrderIntent, ScopedLimits)；缺任何一项就拒发。"""
        from trading_core.ctp_facts import account_key, trading_day
        from trading_core.intents import OrderIntent
        from trading_core.scoped_check import ScopedLimits

        extra = self.extra
        required = ("near", "far", "near_vt", "far_vt", "exchange", "max_open_lots",
                    "margin_per_lot", "config_version", "trading_sessions")
        missing = [key for key in required if key not in extra]
        if missing:
            self._log_gate_context_missing(f"算法 extra 缺少 {missing}（策略侧没把核验上下文传下来）")
            return None, None
        engine = self.main_engine
        gateway = engine.get_gateway(self._gate_name) if engine is not None else None
        td = getattr(gateway, "td_api", None) if gateway is not None else None
        if td is None:
            self._log_gate_context_missing(f"网关 {self._gate_name!r} 没有交易接口（td_api）")
            return None, None
        try:
            account = account_key(td.brokerid, td.userid)
            day = trading_day({}, getattr(td, "cabinet_trading_day", ""))
        except Exception as exc:  # noqa: BLE001 - 缺任何一项都必须说清楚是哪一项
            self._log_gate_context_missing(
                f"账户/交易日取不到（{exc}）；经纪商={getattr(td, 'brokerid', '')!r} "
                f"投资者={getattr(td, 'userid', '')!r} "
                f"柜台交易日={getattr(td, 'cabinet_trading_day', '')!r}"
                f" —— 请重新登录交易接口，确认日志里有「柜台交易日」"
            )
            return None, None
        purpose = {"open": "open", "take_profit": "take_profit",
                   "close": "risk_exit"}.get(self.purpose)
        if purpose is None:
            self._log_gate_context_missing(f"未知的算法用途 {self.purpose!r}")
            return None, None
        tier = str(extra.get("tier_id") or self.algoid)
        lots = int(round(float(req.volume)))
        if lots <= 0:
            self._log_gate_context_missing(f"报单手数无效（volume={req.volume!r}）")
            return None, None
        # 决策号必须"同一笔重试复用、改价改量视为新决策"：否则改价重挂会撞上
        # journal 的 CONFLICT，被永久拒绝。
        decision = f"{self.algoid}:{tier}:{purpose}:{lots}:{float(req.price):.4f}"
        intent = OrderIntent(
            account, day, self.spread_name, decision, tier, purpose,
            str(req.symbol), "buy" if req.direction == Direction.LONG else "sell",
            lots, float(req.price), str(extra["config_version"]),
            {"purpose": purpose, "tier": tier, "reason": self.reason, "price": float(req.price)},
        )
        legs = getattr(self.spread, "leg_pos", {}) or {}
        expected_near = legs.get(str(extra["near_vt"]), 0)
        expected_far = legs.get(str(extra["far_vt"]), 0)
        if self.purpose == AlgoKind.TAKE_PROFIT and "expected_close_legs" in extra:
            expected_near, expected_far = extra["expected_close_legs"]
        limits = ScopedLimits(
            strategy=self.spread_name,
            near=str(extra["near"]), far=str(extra["far"]),
            exchange=str(extra["exchange"]), combo=str(req.symbol),
            max_open_lots=int(extra["max_open_lots"]),
            margin_per_lot=float(extra["margin_per_lot"]),
            signal_current=True,
            quotes_current=self._quotes_current(extra),
            session_open=self._session_open(str(extra["trading_sessions"])),
            expected_near=float(expected_near or 0),
            expected_far=float(expected_far or 0),
            known_orders=self._known_orders(extra),
        )
        return intent, limits

    def _quotes_current(self, extra) -> bool:
        from datetime import datetime as moment_type

        from utils.filters import as_local

        for key in ("near_vt", "far_vt"):
            tick = self.get_tick(str(extra[key]))
            if tick is None or not tick.bid_price_1 or not tick.ask_price_1:
                return False
            timestamp = as_local(tick.datetime)
            if timestamp is None or abs((as_local(moment_type.now()) - timestamp).total_seconds()) > 5:
                return False
        return True

    def _session_open(self, spec: str) -> bool:
        from datetime import datetime as moment_type

        from utils.calendar import TradingSessions

        try:
            return TradingSessions(spec).is_open(moment_type.now())
        except Exception:
            return False

    def _known_orders(self, extra) -> frozenset[tuple[str, str, str]]:
        """本机已知的活动委托号（CTP orderid = FrontID_SessionID_OrderRef）。"""
        engine = self.main_engine
        getter = getattr(engine, "get_all_active_orders", None)
        if not callable(getter):
            return frozenset()
        try:
            orders = list(getter())
        except Exception:
            return frozenset()
        symbols = {str(extra["near"]), str(extra["far"])}
        known = set()
        for order in orders:
            if getattr(order, "symbol", "") not in symbols:
                continue
            parts = str(getattr(order, "orderid", "")).split("_")
            if len(parts) == 3 and all(parts):
                known.add(tuple(parts))
        return frozenset(known)

    def _register_sent(self, req, vt_symbol: str, vt_orderid: str,
                       vt_orderids: list[str]) -> None:
        vt_orderids.append(vt_orderid)
        # 保持"委托号 → 算法"的路由，回报才能回到 update_order / update_trade
        order_algo_map = getattr(self.algo_engine, "order_algo_map", None)
        if order_algo_map is not None:
            order_algo_map[vt_orderid] = self

        data_engine = getattr(self.algo_engine, "data_engine", None)
        if data_engine is not None:
            data_engine.update_order_spread_map(vt_orderid, self.spread)

        self.leg_orders[vt_symbol].append(vt_orderid)
        hook = getattr(self.impl, "accept_sent_order", None)
        if callable(hook):
            hook(vt_orderid)
        self.write_log(
            f"报单已提交 {vt_orderid} {req.vt_symbol} "
            f"{req.direction.value}/{req.offset.value} {req.volume:g}@{req.price:g} "
            f"{req.type.value} 用途={self.purpose} 模式={self.mode} 策略={self.spread_name} "
            f"算法={self.algoid}" + (f" 依据={self.reason}" if self.reason else "")
        )

    # ------------------------------------------------------------ 模板钩子
    def stop(self) -> None:
        if self.paused or self.stopped or not self.is_active():
            return
        self.write_log("算法停止中")
        self.stopped = True
        self.stop_elapsed = self.elapsed
        self._cancel_gate('算法停止，放弃未完成的逐笔核验')
        self.cancel_all_order()
        self.check_algo_cancelled()

    def check_algo_cancelled(self) -> None:
        # update_order 在 on_order 之后还会检查一次，不能重复发布/覆盖终态。
        if self.is_active():
            super().check_algo_cancelled()

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
            self._cancel_gate('算法暂停，放弃未完成的逐笔核验')
            return
        self.elapsed += 1
        self._pump_gate()
        # 引擎只对 active 算法分派订单/成交。超时不是柜台撤单确认。
        if self.stopped and not self.stop_timeout_warned and self.elapsed - self.stop_elapsed >= 15:
            self.stop_timeout_warned = True
            self.write_log("停止确认超时：保留订单与成交路由，不释放算法；请人工核对挂单及成交")
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
