"""CTP 网关子类：为**交易所组合单**加回报容错（不改 site-packages）。

``vnpy_ctp`` 的回报解析有三处无容错的硬下标，组合单回报形态一旦不同就直接
``KeyError`` 抛异常（方案 5.3.3）：

============================  ====================================
``onRtnOrder``  ``symbol_contract_map[symbol]``    组合合约未收录 → KeyError
``onRtnOrder``  ``OFFSET_CTP2VT[CombOffsetFlag]``  回报是两位（"00"）→ KeyError
``onRtnTrade``  ``OFFSET_CTP2VT[OffsetFlag]``      同上
============================  ====================================

本类只做覆盖与兜底，不发放新能力：发单仍走父类逻辑，仅在**需要两位
``CombOffsetFlag`` 时**（``组合单偏移位数=2``）改写一位实现。

使用方式（``run_strategy.py``）::

    main_engine.add_gateway(SpreadCtpGateway)

连接配置与 ``CtpGateway`` 完全一致（8 项）。如需两位 ``CombOffsetFlag``，在
``~/.vntrader/ctp_connect.json`` 里手工加一项 ``"组合单偏移位数": 2``（不放进 GUI
表单，避免改动既有的 8 项连接配置）。
"""

from __future__ import annotations

from datetime import datetime
from time import monotonic

from vnpy.event import EventEngine
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Product, Status
from vnpy.trader.object import OrderData, OrderRequest, TradeData, ContractData

from vnpy_ctp import CtpGateway
from vnpy_ctp.gateway.ctp_gateway import (
    CHINA_TZ,
    DIRECTION_CTP2VT,
    DIRECTION_VT2CTP,
    EXCHANGE_CTP2VT,
    OFFSET_CTP2VT,
    OFFSET_VT2CTP,
    ORDERTYPE_CTP2VT,
    ORDERTYPE_VT2CTP,
    STATUS_CTP2VT,
    THOST_FTDC_CC_Immediately,
    THOST_FTDC_FCC_NotForceClose,
    THOST_FTDC_HF_Speculation,
    THOST_FTDC_OST_Canceled,
    THOST_FTDC_OSS_InsertRejected,
    CtpTdApi,
    symbol_contract_map,
)


def with_protocol(address: str) -> str:
    """缺协议前缀时补 ``tcp://``（与父类 ``connect`` 同口径，且不误伤 ssl/socks）。"""
    for prefix in ("tcp://", "ssl://", "socks"):
        if address.startswith(prefix):
            return address
    return "tcp://" + address


def normalize_offset_flag(value, width: int = 1) -> str:
    """把回报里的 ``CombOffsetFlag`` / ``OffsetFlag`` 归一成一位 offset 码。

    - 组合单回报可能是两位（如 ``"00"``），取首字符；
    - 空值回退到开仓（``"0"``），避免 KeyError 打断整个行情/回报线程；
    - ``width=2`` 只在发单时使用，回报侧永远按一位解析。
    """
    text = str(value or "").strip()
    if not text:
        return "0"
    return text[0]


# CTP 查询接口返回码：-1/-2/-3 是链路或流控问题，隔 1 秒以上重发即可。
CTP_RESULT_HINTS = {
    0: "成功",
    -1: "网络连接失败",
    -2: "未处理请求队列已满（柜台限流）",
    -3: "每秒请求数超限（柜台限流）",
}
RETRYABLE_QUERY_CODES = frozenset({-1, -2, -3})


class PositionQueryError(RuntimeError):
    """柜台拒绝持仓查询；``retryable`` 表示稍后重发即可，不代表持仓为空。"""

    def __init__(self, code: int, detail: str = "") -> None:
        self.code = int(code)
        self.retryable = self.code in RETRYABLE_QUERY_CODES
        hint = CTP_RESULT_HINTS.get(self.code, detail or "柜台返回未知错误")
        super().__init__(f"柜台拒绝持仓查询（错误码 {self.code}：{hint}），请稍后重试")


class SpreadCtpTdApi(CtpTdApi):
    """交易 API 子类：组合单回报容错 + 可选两位 ``CombOffsetFlag``。"""

    POSITION_QUERY_TIMEOUT = 10.0  # 超过界面等待窗口后允许重新查询，旧请求仍按 reqid 隔离。

    def __init__(self, gateway) -> None:
        super().__init__(gateway)
        self.offset_flag_width: int = 1
        self.derived_contracts: set[str] = set()
        # 只把已完整返回的本次柜台查询视为可纠正本地持仓的依据。
        self.position_query_pending: bool = False
        self.position_query_generation: int = 0
        self.position_snapshot: dict[str, float] = {}
        self.position_snapshot_sides: dict[str, dict[str, float]] = {}
        self.position_snapshot_account: tuple[str, str] | None = None
        self.position_snapshot_at: float = 0.0
        self._position_query_reqid: int | None = None
        self._position_query_started_at: float = 0.0
        self._position_query_rows: dict[str, float] = {}
        self._position_query_sides: dict[str, dict[str, float]] = {}
        self._position_query_raw_rows: list[dict] = []
        self._position_activity_generation: int = 0
        self._query_activity_generation: int = 0
        self._position_query_failed: bool = False
        # 解析不了的回报绝不能按无单/无成交处理；重连后再核柜台记录。
        self.reconciliation_event_error: str = ""
        # 组合单回报可能缺方向/开平标志：用已知委托信息回填，避免终态回报被丢弃。
        self._known_fields: dict[str, tuple[Direction, Offset]] = {}
        self._order_cache: dict[str, OrderData] = {}
        self._trade_filled: dict[str, float] = {}
        # 本次登录后真正收到过回报的委托（用于区分“实时委托”和“只是本地记忆”）。
        self.live_seen_orderids: set[str] = set()
        # 当日委托查询（vn.py 未封装 reqQryOrder，这里自己发）。
        self.orders_query_pending: bool = False
        self._orders_query_reqid: int | None = None
        self._orders_query_started_at: float = 0.0
        # "manual"=核对查询（发布快照）；"auto"=后台刷新 OMS 持仓（不动快照）。
        self._position_query_mode: str = "manual"

    @property
    def position_query_pending(self) -> bool:
        # 界面轮询此属性；超时后自动释放门禁，不依赖 CTP 最后一条回报。
        if (self._position_query_pending
                and monotonic() - self._position_query_started_at >= self.POSITION_QUERY_TIMEOUT):
            self._abort_position_query(keep_snapshot=self._position_query_mode == "auto")
            self.gateway.write_log("柜台持仓查询超时，已丢弃未完整回报；可重新查询")
        return self._position_query_pending

    @position_query_pending.setter
    def position_query_pending(self, pending: bool) -> None:
        self._position_query_pending = pending

    def _abort_position_query(self, *, keep_snapshot: bool = False) -> None:
        """丢弃未完成查询及父类累加器，禁止迟到回报进入 OMS。"""
        self.position_query_pending = False
        self._position_query_failed = True
        if not keep_snapshot:
            self.position_snapshot_at = 0.0
        self._position_query_reqid = None
        self._position_query_started_at = 0.0
        self._position_query_rows = {}
        self._position_query_sides = {}
        self._position_query_raw_rows = []
        self.positions.clear()

    def onFrontDisconnected(self, reason: int) -> None:
        self._abort_position_query()
        self._position_activity_generation += 1
        self.reconciliation_event_error = ""
        super().onFrontDisconnected(reason)

    def onRspUserLogin(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        # 即便账号名称相同，重连前的持仓快照也不能复用。
        self._abort_position_query()
        self._position_activity_generation += 1
        self.reconciliation_event_error = ""
        super().onRspUserLogin(data, error, reqid, last)

    def onRspError(self, error: dict, reqid: int, last: bool) -> None:
        if self.position_query_pending and reqid == self._position_query_reqid:
            self._abort_position_query()
        self.gateway.write_error("交易接口报错", error)

    def query_position(self, mode: str = "manual") -> None:
        """发起持仓查询。

        ``mode="manual"``：核对用，结束后发布快照；
        ``mode="auto"``：后台刷新 OMS 持仓用，不发布/不作废核对快照。
        """
        if self.position_query_pending:
            if mode == "auto":
                return  # 已有查询在跑，本轮后台刷新直接跳过
            if self._position_query_mode == "manual":
                raise RuntimeError("柜台上次持仓查询尚未结束，请稍后再核对")
            self._abort_position_query(keep_snapshot=True)  # 让手动查询抢占
        # 基类在目录为空时静默 return，不能把未发请求当作空仓快照。
        if not self.contract_inited or not symbol_contract_map:
            self._abort_position_query()
            raise RuntimeError("柜台合约目录尚未就绪，无法发起持仓查询")
        # 父类按 symbol+direction 累加，失败/超时请求遗留的值不能进入新查询。
        self.positions.clear()
        self._position_query_mode = mode
        self.position_query_pending = True
        if mode == "manual":
            self.position_snapshot_at = 0.0
            self.position_snapshot = {}
            self.position_snapshot_sides = {}
        self._position_query_failed = False
        self._position_query_rows = {}
        self._position_query_sides = {}
        self._position_query_raw_rows = []
        self._query_activity_generation = self._position_activity_generation
        self._position_query_reqid = self.reqid + 1
        self._position_query_started_at = monotonic()
        # 父类不检查 CTP 的发送返回码；请求被拒时不能无限等末条回报。
        try:
            self.reqid += 1
            result = self.reqQryInvestorPosition(
                {"BrokerID": self.brokerid, "InvestorID": self.userid}, self.reqid
            )
        except Exception:
            self._abort_position_query(keep_snapshot=(mode == "auto"))
            raise
        if result:
            self._abort_position_query(keep_snapshot=(mode == "auto"))
            raise PositionQueryError(result)

    def query_orders(self) -> None:
        """查询当日委托（CTP ``ReqQryOrder``）；返回的终态才是权威状态。"""
        if self.orders_query_pending:
            raise RuntimeError("柜台上次委托查询尚未结束，请稍后再试")
        if not self.contract_inited or not symbol_contract_map:
            raise RuntimeError("柜台合约目录尚未就绪，无法发起委托查询")
        self.reqid += 1
        self._orders_query_reqid = self.reqid
        self.orders_query_pending = True
        self._orders_query_started_at = monotonic()
        result = self.reqQryOrder(
            {"BrokerID": self.brokerid, "InvestorID": self.userid}, self.reqid
        )
        if result:
            self.orders_query_pending = False
            self._orders_query_reqid = None
            raise PositionQueryError(result)

    def onRspQryOrder(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        if not self.orders_query_pending or reqid != self._orders_query_reqid:
            return  # 迟到/无关的查询回报
        if error and error.get("ErrorID"):
            self.orders_query_pending = False
            self._orders_query_reqid = None
            return
        if data and data.get("InstrumentID"):
            order = self._order_from_query(data)
            if order is not None:
                self.gateway.on_order(order)
        if last:
            self.orders_query_pending = False
            self._orders_query_reqid = None

    def _order_from_query(self, data: dict):
        symbol = str(data.get("InstrumentID", ""))
        contract = symbol_contract_map.get(symbol)
        if contract is None:
            return None
        status = STATUS_CTP2VT.get(data.get("OrderStatus"))
        if status is None:
            return None
        orderid = f"{data.get('FrontID')}_{data.get('SessionID')}_{data.get('OrderRef')}"
        direction = DIRECTION_CTP2VT.get(str(data.get("Direction", "")))
        offset = (OFFSET_CTP2VT.get(normalize_offset_flag(data.get("CombOffsetFlag")))
                  if data.get("CombOffsetFlag") else None)
        known = self._known_fields.get(orderid)
        if known is not None:
            direction = direction or known[0]
            offset = offset or known[1]
        if direction is None or offset is None:
            return None
        try:
            dt = datetime.strptime(
                f"{data.get('InsertDate', '')} {data.get('InsertTime', '')}",
                "%Y%m%d %H:%M:%S",
            ).replace(tzinfo=CHINA_TZ)
        except ValueError:
            dt = datetime.now(CHINA_TZ)
        cached = self._order_cache.get(f"CTP.{orderid}")
        order_type = ORDERTYPE_CTP2VT.get(
            (data.get("OrderPriceType"), data.get("TimeCondition"),
             data.get("VolumeCondition"))
        ) or (cached.type if cached is not None else OrderType.LIMIT)
        order = OrderData(
            symbol=symbol,
            exchange=contract.exchange,
            orderid=orderid,
            type=order_type,
            direction=direction,
            offset=offset,
            price=float(data.get("LimitPrice", 0) or 0),
            volume=float(data.get("VolumeTotalOriginal", 0) or 0),
            traded=float(data.get("VolumeTraded", 0) or 0),
            status=status,
            datetime=dt,
            gateway_name=self.gateway_name,
        )
        vt_orderid = order.vt_orderid
        self._known_fields[orderid] = (direction, offset)
        self._order_cache[vt_orderid] = order
        self.live_seen_orderids.add(vt_orderid)
        return order

    def onRspQryInvestorPosition(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        # 父类不校验 reqid，迟到回报若传下去会污染累加器或直接推送 OMS。
        if not self.position_query_pending or reqid != self._position_query_reqid:
            return
        if error and error.get("ErrorID"):
            self._abort_position_query()
            return
        if data:
            symbol = str(data.get("InstrumentID", ""))
            direction = DIRECTION_CTP2VT.get(data.get("PosiDirection"))
            try:
                volume = float(data.get("Position"))
            except (TypeError, ValueError):
                volume = float("nan")
            sides = self._position_query_sides.setdefault(
                symbol, {"long": 0.0, "short": 0.0, "unknown": 0.0})
            if not symbol or not 0 <= volume < float("inf"):
                # 无法解析的仓位：只影响该合约，不让整份快照作废。
                sides["unknown"] += float("inf")
            elif direction == Direction.LONG:
                sides["long"] += volume
            elif direction == Direction.SHORT:
                sides["short"] += volume
            else:
                # 多空不分的回报（如净仓方向）：该合约无法判定，标记为不可比。
                sides["unknown"] += volume
            if direction in (Direction.LONG, Direction.SHORT) and symbol in symbol_contract_map:
                # 只有方向明确、合约已收录的行才重放给父类，避免 KeyError。
                self._position_query_rows[symbol] = self._position_query_rows.get(symbol, 0.0) + volume
                self._position_query_raw_rows.append(dict(data))
        if not last:
            return
        complete = (not self._position_query_failed and self.contract_inited
                    and self._query_activity_generation == self._position_activity_generation)
        if not complete:
            self._abort_position_query()
            return
        # 只在整次快照通过门禁后重放基类逻辑；空回报绝不推送遗留累加器。
        self.positions.clear()
        try:
            for index, row in enumerate(self._position_query_raw_rows):
                super().onRspQryInvestorPosition(
                    row, {}, reqid, index == len(self._position_query_raw_rows) - 1
                )
        except Exception:
            self._abort_position_query()
            raise
        if self._query_activity_generation != self._position_activity_generation:
            self._abort_position_query()
            return
        if not self._position_query_raw_rows:
            # 完整空仓快照：父类不会发空仓事件；清空仍留在网关的累计值。
            self.positions.clear()
        if self._position_query_mode == "manual":
            # 只有核对查询才发布“可纠依据”快照；后台刷新只更新 OMS 持仓。
            self.position_snapshot = dict(self._position_query_rows)
            self.position_snapshot_sides = {symbol: dict(sides) for symbol, sides in self._position_query_sides.items()}
            self.position_snapshot_account = (self.brokerid, self.userid)
            self.position_snapshot_at = monotonic()
            self.position_query_generation += 1
        self.position_query_pending = False
        self._position_query_reqid = None
        self._position_query_started_at = 0.0
        self._position_query_raw_rows = []

    # ------------------------------------------------------------ 合约兜底
    def ensure_contract(self, data: dict, symbol: str) -> ContractData:
        """确保 ``symbol_contract_map`` 里有该合约，缺失时按回报自身信息补建。

        组合合约（如 ``SP a2611&a2701``）可能不在合约查询结果里，而父类实现直接
        ``symbol_contract_map[symbol]`` 取值 → KeyError。补建的合约 ``size=0``，
        与 CTP 对组合合约 ``VolumeMultiple=0`` 的行为一致；所有金额/费用计算都必须
        使用单腿合约的 ``size``（见方案 R7）。
        """
        contract = symbol_contract_map.get(symbol)
        if contract is not None:
            return contract

        exchange_cn = str(data.get("ExchangeID", "")).strip()
        exchange = EXCHANGE_CTP2VT.get(exchange_cn, Exchange.LOCAL)
        contract = ContractData(
            symbol=symbol,
            exchange=exchange,
            name=symbol,
            product=Product.FUTURES,
            size=0,
            pricetick=0,
            min_volume=1,
            gateway_name=self.gateway_name,
        )
        symbol_contract_map[symbol] = contract
        self.derived_contracts.add(symbol)
        self.gateway.write_log(
            f"合约缓存补建：{symbol}（交易所 {exchange.value}），"
            f"组合合约 size=0 属正常，费用/金额请用单腿合约"
        )
        return contract

    # ------------------------------------------------------------ 回报
    def onRtnOrder(self, data: dict) -> None:
        # 查询期间或结束后的异步回报都使快照失效。
        self.position_snapshot_at = 0.0
        self._position_activity_generation += 1
        """委托回报：组合单可能带来未收录的合约代码与两位 offset。"""
        if not self.contract_inited:
            self.reconciliation_event_error = "合约目录尚未完成时收到委托回报"
            self.order_data.append(data)
            return

        symbol: str = data["InstrumentID"]
        contract: ContractData = self.ensure_contract(data, symbol)

        frontid: int = data["FrontID"]
        sessionid: int = data["SessionID"]
        order_ref: str = data["OrderRef"]
        orderid: str = f"{frontid}_{sessionid}_{order_ref}"

        status = STATUS_CTP2VT.get(data["OrderStatus"], None)
        if not status:
            self.reconciliation_event_error = f"委托 {orderid} 状态无法识别"
            self.gateway.write_log(self.reconciliation_event_error + "，须人工核对")
            return

        if (
            data["OrderStatus"] == THOST_FTDC_OST_Canceled
            and data["OrderSubmitStatus"] == THOST_FTDC_OSS_InsertRejected
        ):
            status = Status.REJECTED

        timestamp: str = f"{data['InsertDate']} {data['InsertTime']}"
        dt: datetime = datetime.strptime(timestamp, "%Y%m%d %H:%M:%S").replace(tzinfo=CHINA_TZ)

        tp: tuple = (data["OrderPriceType"], data["TimeCondition"], data["VolumeCondition"])
        order_type = ORDERTYPE_CTP2VT.get(tp, None)
        if not order_type:
            self.reconciliation_event_error = f"委托 {orderid} 类型无法识别"
            self.gateway.write_log(self.reconciliation_event_error + "，须人工核对")
            return

        direction = DIRECTION_CTP2VT.get(str(data.get("Direction", "")))
        offset = OFFSET_CTP2VT.get(normalize_offset_flag(data.get("CombOffsetFlag"))) if data.get("CombOffsetFlag") else None
        known = self._known_fields.get(orderid)
        if known is not None:
            # 组合单的终态回报有时不带方向/开平：沿用该委托已知信息，不能丢弃。
            direction = direction or known[0]
            offset = offset or known[1]
        if direction is None or offset is None:
            self.reconciliation_event_error = f"委托 {orderid} 方向或开平标志未知"
            self.gateway.write_log(self.reconciliation_event_error + "；禁止继续交易，须与柜台核对")
            return
        order: OrderData = OrderData(
            symbol=symbol,
            exchange=contract.exchange,
            orderid=orderid,
            type=order_type,
            direction=direction,
            offset=offset,
            price=data["LimitPrice"],
            volume=data["VolumeTotalOriginal"],
            traded=data["VolumeTraded"],
            status=status,
            datetime=dt,
            gateway_name=self.gateway_name,
        )
        self.gateway.on_order(order)
        self.live_seen_orderids.add(order.vt_orderid)
        self._known_fields[orderid] = (direction, offset)
        self._order_cache[orderid] = order
        if order.is_active():
            self._trade_filled.setdefault(orderid, float(order.traded or 0))

        self.sysid_orderid_map[data["OrderSysID"]] = orderid

        status_msg: str = data["StatusMsg"]
        if data["OrderStatus"] == THOST_FTDC_OST_Canceled and status_msg != "已撤单":
            self.gateway.write_log(f"委托 {orderid} 状态更新，{status_msg}")

    def onRtnTrade(self, data: dict) -> None:
        """成交回报：组合单成交可能是单腿（``CombinationDerived``）或组合代码。"""
        self.position_snapshot_at = 0.0
        self._position_activity_generation += 1
        if not self.contract_inited:
            self.reconciliation_event_error = "合约目录尚未完成时收到成交回报"
            self.trade_data.append(data)
            return

        symbol: str = data["InstrumentID"]
        contract: ContractData = self.ensure_contract(data, symbol)

        orderid: str | None = self.sysid_orderid_map.get(data["OrderSysID"], None)
        if not orderid:
            frontid, sessionid, order_ref = data.get("FrontID"), data.get("SessionID"), data.get("OrderRef")
            if frontid is None or sessionid is None or not order_ref:
                self.reconciliation_event_error = "成交早于委托回报且缺订单标识"
                self.gateway.write_log(self.reconciliation_event_error + "；拒绝自动归属，须人工核对")
                return
            orderid = f"{frontid}_{sessionid}_{order_ref}"
            self.sysid_orderid_map[data["OrderSysID"]] = orderid

        direction = DIRECTION_CTP2VT.get(str(data.get("Direction", "")))
        offset = OFFSET_CTP2VT.get(normalize_offset_flag(data.get("OffsetFlag"))) if data.get("OffsetFlag") else None
        known = self._known_fields.get(orderid)
        if known is not None:
            direction = direction or known[0]
            offset = offset or known[1]
        if direction is None or offset is None:
            self.reconciliation_event_error = f"成交 {data.get('TradeID', '')} 方向或开平标志未知"
            self.gateway.write_log(self.reconciliation_event_error + "；须人工核对")
            return
        timestamp: str = f"{data['TradeDate']} {data['TradeTime']}"
        dt: datetime = datetime.strptime(timestamp, "%Y%m%d %H:%M:%S").replace(tzinfo=CHINA_TZ)

        trade: TradeData = TradeData(
            symbol=symbol,
            exchange=contract.exchange,
            orderid=orderid,
            tradeid=data["TradeID"],
            direction=direction,
            offset=offset,
            price=data["Price"],
            volume=data["Volume"],
            datetime=dt,
            gateway_name=self.gateway_name,
        )
        self.gateway.on_trade(trade)
        self.live_seen_orderids.add(trade.vt_orderid)
        self._mark_order_filled(orderid, float(data.get("Volume", 0) or 0))

    def _mark_order_filled(self, orderid: str, volume: float) -> None:
        """成交累计达到委托量时补发终态委托，避免界面一直显示“未成交”。"""
        order = self._order_cache.get(orderid)
        if order is None or not order.is_active() or volume <= 0:
            return
        filled = self._trade_filled.get(orderid, float(order.traded or 0)) + volume
        self._trade_filled[orderid] = filled
        if order.volume and filled + 1e-9 >= float(order.volume):
            order.traded = float(order.volume)
            order.status = Status.ALLTRADED
            self.gateway.on_order(order)

    # ------------------------------------------------------------ 连接与发单
    def connect(self, address: str, userid: str, password: str, brokerid: str,
                auth_code: str, appid: str, production_mode: bool,
                offset_flag_width: int = 1) -> None:
        self.offset_flag_width = max(1, int(offset_flag_width or 1))
        if self.offset_flag_width != 1:
            self.gateway.write_log(
                f"组合单 CombOffsetFlag 位数 = {self.offset_flag_width}"
                f"（默认 1 位，按 CTP 官方指南一位即可）"
            )
        super().connect(address, userid, password, brokerid, auth_code, appid, production_mode)

    def send_order(self, req: OrderRequest) -> str:
        """与父类一致，唯一区别是 ``CombOffsetFlag`` 可按配置补到两位。"""
        if req.offset not in OFFSET_VT2CTP:
            self.gateway.write_log("请选择开平方向")
            return ""

        if req.type not in ORDERTYPE_VT2CTP:
            self.gateway.write_log(f"当前接口不支持该类型的委托{req.type.value}")
            return ""

        self.order_ref += 1

        price_type, time_condition, volume_condition = ORDERTYPE_VT2CTP[req.type]
        offset_flag: str = OFFSET_VT2CTP.get(req.offset, "")
        if self.offset_flag_width > 1:
            offset_flag = offset_flag.rjust(self.offset_flag_width, "0")

        ctp_req: dict = {
            "InstrumentID": req.symbol,
            "ExchangeID": req.exchange.value,
            "LimitPrice": req.price,
            "VolumeTotalOriginal": int(req.volume),
            "OrderPriceType": price_type,
            "Direction": DIRECTION_VT2CTP.get(req.direction, ""),
            "CombOffsetFlag": offset_flag,
            "OrderRef": str(self.order_ref),
            "InvestorID": self.userid,
            "UserID": self.userid,
            "BrokerID": self.brokerid,
            "CombHedgeFlag": THOST_FTDC_HF_Speculation,
            "ContingentCondition": THOST_FTDC_CC_Immediately,
            "ForceCloseReason": THOST_FTDC_FCC_NotForceClose,
            "IsAutoSuspend": 0,
            "TimeCondition": time_condition,
            "VolumeCondition": volume_condition,
            "MinVolume": 1,
        }

        self.reqid += 1
        n: int = self.reqOrderInsert(ctp_req, self.reqid)
        if n:
            self.gateway.write_log(f"委托请求发送失败，错误代码：{n}")
            return ""

        orderid: str = f"{self.frontid}_{self.sessionid}_{self.order_ref}"
        order: OrderData = req.create_order_data(orderid, self.gateway_name)
        self.gateway.on_order(order)
        self._known_fields[order.vt_orderid] = (req.direction, req.offset)
        self._order_cache[order.vt_orderid] = order
        self._trade_filled[order.vt_orderid] = float(order.traded or 0)
        return order.vt_orderid


class SpreadCtpGateway(CtpGateway):
    """把 ``CtpGateway`` 的交易 API 换成带组合单容错的子类。"""

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        # 父类已创建原始 CtpTdApi（未连接，无副作用），这里替换为容错版本
        self.td_api: SpreadCtpTdApi = SpreadCtpTdApi(self)

    def init_query(self) -> None:
        """定时刷新资金与持仓；持仓查询走 auto 模式，不干扰人工核对快照。"""
        self.query_functions = [self.query_account, self._auto_query_position]
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def _auto_query_position(self) -> None:
        """后台刷新 OMS 持仓，让持仓面板能跟随成交变化。"""
        try:
            self.td_api.query_position(mode="auto")
        except Exception:
            pass  # 后台刷新失败不影响交易；人工核对会给出明确错误

    def connect(self, setting: dict) -> None:
        userid: str = setting["用户名"]
        password: str = setting["密码"]
        brokerid: str = setting["经纪商代码"]
        td_address: str = setting["交易服务器"]
        md_address: str = setting["行情服务器"]
        appid: str = setting["产品名称"]
        auth_code: str = setting["授权编码"]

        environment: str = setting.get("柜台环境", "实盘")
        production_mode: bool = environment == "实盘"
        offset_flag_width: int = int(setting.get("组合单偏移位数", 1) or 1)

        self.td_api.connect(
            with_protocol(td_address), userid, password, brokerid, auth_code, appid,
            production_mode, offset_flag_width,
        )
        self.md_api.connect(with_protocol(md_address), userid, password, brokerid, production_mode)

        self.init_query()
