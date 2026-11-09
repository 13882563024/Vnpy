"""**只连行情前置**的 CTP 网关：进程内不创建任何交易 API。

为什么单独做一个：

- 采集只需要行情，而 ``CtpGateway`` 会同时连交易前置、走认证/登录/结算确认并定时查
  资金与持仓。用真实带钱账号时这些都不是必要的，应该从进程层面断掉。
- ``vnpy_ctp`` 的 ``onRtnDepthMarketData`` 里有一句 ``symbol_contract_map.get(symbol)``，
  取不到合约就**直接丢弃 tick**；而这个字典是由 ``CtpTdApi.onRspQryInstrument`` 填充的。
  只连行情时它永远是空的 → 一条 tick 都录不到。所以这里用 ``register_contract()``
  自行注册合约占位（交易所来自配置，``size/pricetick`` 不影响 tick 落库）。

交易相关方法一律空实现或返回空串，**下不了单**。
"""

from __future__ import annotations

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Exchange, Product
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import CancelRequest, ContractData, OrderRequest, SubscribeRequest

from vnpy_ctp.gateway.ctp_gateway import EXCHANGE_CTP2VT, CtpMdApi, symbol_contract_map


class CtpMdOnlyGateway(BaseGateway):
    """行情专用网关：只有 ``CtpMdApi``，没有 ``CtpTdApi``。"""

    default_name: str = "CTP_MD"

    default_setting: dict = {
        "用户名": "",
        "密码": "",
        "经纪商代码": "",
        "行情服务器": "",
    }

    exchanges: list[Exchange] = list(EXCHANGE_CTP2VT.values())

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)

        self.md_api: CtpMdApi = CtpMdApi(self)
        self.contracts: dict[str, ContractData] = {}
        self.count: int = 0

    # ------------------------------------------------------------ 合约占位
    def register_contract(
        self,
        symbol: str,
        exchange: Exchange,
        size: float = 1.0,
        pricetick: float = 0.0,
        name: str = "",
    ) -> ContractData:
        """注册合约，两个地方都要写：

        1. ``symbol_contract_map``（vnpy_ctp 模块级）：**行情回调**靠它拿 exchange/name，
           不写就会静默丢弃 tick；
        2. ``on_contract`` 事件 → OmsEngine 缓存：``main_engine.get_contract`` 靠它，
           不写的话 ``SpreadDataEngine.add_spread`` 找不到合约就**不会订阅行情**。
        """
        contract = ContractData(
            symbol=symbol,
            exchange=exchange,
            name=name or symbol,
            product=Product.FUTURES,
            size=size,
            pricetick=pricetick,
            min_volume=1,
            gateway_name=self.gateway_name,
        )
        self.contracts[contract.vt_symbol] = contract
        symbol_contract_map[symbol] = contract
        self.on_contract(contract)
        return contract

    # ------------------------------------------------------------ 连接
    def connect(self, setting: dict) -> None:
        userid: str = setting["用户名"]
        password: str = setting["密码"]
        brokerid: str = setting["经纪商代码"]
        md_address: str = setting["行情服务器"]

        environment: str = setting.get("柜台环境", "实盘")
        production_mode: bool = environment == "实盘"

        for prefix in ("tcp://", "ssl://", "socks"):
            if md_address.startswith(prefix):
                break
        else:
            md_address = "tcp://" + md_address

        self.write_log("仅连接行情前置（不创建交易 API，不会发出任何交易请求）")
        self.md_api.connect(md_address, userid, password, brokerid, production_mode)

        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def process_timer_event(self, event: Event) -> None:
        """每秒一次：更新行情侧的自然日期（DCE 的 ActionDay 为空时要用它拼时间戳）。"""
        self.count += 1
        if self.count < 2:
            return
        self.count = 0
        self.md_api.update_date()

    def subscribe(self, req: SubscribeRequest) -> None:
        self.md_api.subscribe(req)

    def close(self) -> None:
        self.md_api.close()

    # ------------------------------------------------------------ 交易能力：明确禁用
    def send_order(self, req: OrderRequest) -> str:
        self.write_log("本网关为行情专用，禁止报单")
        return ""

    def cancel_order(self, req: CancelRequest) -> None:
        self.write_log("本网关为行情专用，禁止撤单")

    def query_account(self) -> None:
        return

    def query_position(self) -> None:
        return

    # ------------------------------------------------------------ 辅助
    def get_all_contract_vt_symbols(self) -> list[str]:
        return list(self.contracts.keys())
