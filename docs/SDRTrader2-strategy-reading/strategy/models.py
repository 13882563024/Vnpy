"""策略层数据模型（规划方案 §6：持仓层级 + 信号 + 动作）。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class Side(Enum):
    """价差方向：LONG = 做多价差（买 near 卖 far）；SHORT = 做空价差（卖 near 买 far）。"""
    LONG = auto()
    SHORT = auto()

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @classmethod
    def from_spread(cls, spread: float, mean: float) -> "Side":
        return cls.LONG if spread < mean else cls.SHORT


@dataclass
class Tier:
    """单层持仓。tier_id = 稳定层身份（uuid）：平仓挂单/回报按层身份定位，
    替代脆弱的 list 索引（索引会因其他层平仓而错位）。"""
    volume: int = 0               # 手数
    avg_price: float = 0.0        # 加权平均建仓价（价差点数）
    open_ts: float = 0.0          # 建层时间戳
    open_trading_day: str = ""    # 建层交易日
    entry_mean: float = 0.0       # 建层时的 mean（逐层回归平仓参照）
    tier_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])  # 稳定层身份

    @property
    def cost(self) -> float:
        return self.avg_price * self.volume


@dataclass
class Position:
    """价差持仓：同一时间只允许单方向（推荐文档 §4）。"""
    side: Optional[Side] = None
    tiers: list[Tier] = field(default_factory=list)
    entry_mean: float = 0.0       # 开仓时的 mean（回归平仓参照）
    open_trading_day: str = ""

    @property
    def volume(self) -> int:
        return sum(t.volume for t in self.tiers)

    @property
    def tier_count(self) -> int:
        return len(self.tiers)

    @property
    def avg_price(self) -> float:
        v = self.volume
        if v == 0:
            return 0.0
        return sum(t.cost for t in self.tiers) / v

    @property
    def is_open(self) -> bool:
        return self.side is not None and self.volume > 0

    def unrealized_pnl(self, spread: float, multiplier: float) -> float:
        """当前浮动盈亏（元）= 方向 × (当前价差 − 均价) × 乘数 × 手数。"""
        if not self.is_open:
            return 0.0
        return self.side.sign * (spread - self.avg_price) * multiplier * self.volume


@dataclass
class Signal:
    """一次决策的输入信号。"""
    ts: float = 0.0
    trading_day: str = ""
    spread: float = 0.0
    mean: float = 0.0             # 最近 N 分钟均值
    sd: float = 0.0               # 最近 M 分钟标准差
    near_volume: int = 0          # 近月累计成交量
    far_volume: int = 0           # 远月累计成交量
    near_inst: str = ""
    far_inst: str = ""
    exit_price: float = float("nan")  # 当前平仓可成交价差（盘口对手价，与执行口径一致）
    # 当前开仓对手价差（盘口买卖一价，挂单用，与执行口径一致）：
    long_cross_price: float = float("nan")   # 做多（买near卖far）：near.ask - far.bid
    short_cross_price: float = float("nan")  # 做空（卖near买far）：near.bid - far.ask

    @property
    def ready(self) -> bool:
        """统计窗口是否就绪（mean/sd 有效）。"""
        return self.mean == self.mean and self.sd == self.sd and self.sd > 0


class ActionType(Enum):
    OPEN = auto()        # 开底仓（第 1 手）
    ADD = auto()         # 加一层
    CLOSE = auto()       # 平仓（整仓或指定层）


class OrderStatus(str, Enum):
    """报单状态（对齐 BMR2 模拟/实盘接口）。"""
    WAITING_ACCEPT = "WAITING_ACCEPT"  # 提交后未确认
    ACTIVE = "ACTIVE"                  # 已确认活跃（挂单中，含 PARTTRADED）
    TRADED = "TRADED"                  # 全部成交（终态）
    REJECTED = "REJECTED"              # 拒绝（终态，熔断 +1）
    CANCELLED = "CANCELLED"            # 已撤（终态）
    TIMEOUT = "TIMEOUT"                # 超时（终态，熔断 +1）


TERMINAL_STATUSES: frozenset[str] = frozenset(
    {OrderStatus.TRADED.value, OrderStatus.REJECTED.value,
     OrderStatus.CANCELLED.value, OrderStatus.TIMEOUT.value})
ACTIVE_STATUSES: frozenset[str] = frozenset(
    {OrderStatus.WAITING_ACCEPT.value, OrderStatus.ACTIVE.value})


# 预挂止盈平仓单在 reason 中的标识：
# - 不参与挂单超时撤单（OrderBook.expire_stale 跳过），等目标价成交；
# - 加仓 / 市价平仓前按此标识识别并先撤销（避免裸单 / 占住唯一挂单槽）。
PREPLACE_EXIT_TAG = "预挂止盈"

# 市价平仓单（止损/强平/时间止损/全平）在实盘通道按对手价发出限价单：
# 未成交则在此秒数后撤单重发（追价），不依赖 exit.order_timeout_s —— 后者可为 0
# 或极大值（表示不撤单），若沿用它，止损伤单会一直挂着不重发。
MARKET_CLOSE_TIMEOUT_S = 5.0


@dataclass
class Order:
    """报单（实时区）：限价挂单生命周期（对齐 BMR2 设计文档 §3.2）。

    target_tier 语义（SDR2 层为 list 索引）：
    - 开仓/加仓单：目标新层索引（如底仓=0、第 2 层=1 …）；
    - 平仓单：要平的层索引（-1 = 整仓）。
    """
    local_id: str
    slug: str
    direction: str               # buy / sell（买卖方向）
    side: str                    # open / close（开平标志）
    volume: int                  # 报单手数
    price: float                 # 挂单价（限价单；市价单为 0）
    status: str                  # OrderStatus
    target_tier: int = -1        # 归属层索引（见上）
    open_price: float = 0.0      # 平仓单：对应目标层的建仓价（整仓=加权均价；开/加仓单为 0）
    order_time: float = field(default_factory=time.time)
    trading_day: str = ""
    reason: str = ""             # 策略原因（网页报单记录显示）
    ctp_ref: str = ""
    order_sys_id: str = ""       # 交易所报单系统编号（跨会话唯一；回报回填，匹配/撤单用）
    cum_volume: int = 0          # 累计成交手数
    cum_amount: float = 0.0      # 累计成交金额
    last_trade_price: float = 0.0
    error_msg: str = ""
    tier_id: str = ""            # 目标层稳定身份（平仓单=要平的层；开仓单=目标新层）
    cancelling: bool = False     # 撤单中标记（本地独有，撤单确认/超时兜底用）
    cancel_started_at: float = 0.0   # 撤单发起时间（心跳超时重试依据）
    cancel_retries: int = 0          # 撤单重发次数（撤单未确认兜底，见 OrderBook.retry_stale_cancels）
    timeout_at: float = 0.0          # 挂单超时判定截止（timeout = order_time + order_timeout_s）
    market: bool = False             # 市价平仓单（止损/强平/全平）：实盘按对手价限价发出，
                                     # 短超时撤单重发（见 OrderBook.expire_stale）

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def avg_trade_price(self) -> float:
        """成交加权均价（建层依据）。"""
        if self.cum_volume <= 0:
            return self.price
        return self.cum_amount / self.cum_volume

    def to_dict(self) -> dict:
        return {
            "local_id": self.local_id, "slug": self.slug,
            "direction": self.direction, "side": self.side,
            "volume": self.volume, "price": self.price, "status": self.status,
            "target_tier": self.target_tier, "open_price": self.open_price,
            "order_time": self.order_time,
            "trading_day": self.trading_day, "reason": self.reason,
            "ctp_ref": self.ctp_ref, "cum_volume": self.cum_volume,
            "cum_amount": self.cum_amount, "last_trade_price": self.last_trade_price,
            "order_sys_id": self.order_sys_id, "error_msg": self.error_msg,
            "tier_id": self.tier_id, "cancelling": self.cancelling,
            "cancel_started_at": self.cancel_started_at, "timeout_at": self.timeout_at,
            "cancel_retries": self.cancel_retries, "market": self.market,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        return cls(
            local_id=str(d["local_id"]), slug=str(d.get("slug", "")),
            direction=str(d.get("direction", "buy")), side=str(d.get("side", "open")),
            volume=int(d.get("volume", 0)), price=float(d.get("price", 0.0)),
            status=str(d.get("status", OrderStatus.WAITING_ACCEPT.value)),
            target_tier=int(d.get("target_tier", -1)),
            open_price=float(d.get("open_price", 0.0) or 0.0),
            order_time=float(d.get("order_time", 0.0)),
            trading_day=str(d.get("trading_day", "") or ""),
            reason=str(d.get("reason", "") or ""),
            ctp_ref=str(d.get("ctp_ref", "")),
            order_sys_id=str(d.get("order_sys_id", "") or ""),
            cum_volume=int(d.get("cum_volume", 0)),
            cum_amount=float(d.get("cum_amount", 0.0)),
            last_trade_price=float(d.get("last_trade_price", 0.0)),
            error_msg=str(d.get("error_msg", "") or ""),
            tier_id=str(d.get("tier_id", "") or ""),
            cancelling=bool(d.get("cancelling", False)),
            cancel_started_at=float(d.get("cancel_started_at", 0.0)),
            cancel_retries=int(d.get("cancel_retries", 0) or 0),
            timeout_at=float(d.get("timeout_at", 0.0)),
            market=bool(d.get("market", False)),
        )


@dataclass
class Action:
    type: ActionType
    volume: int = 1
    side: Optional[Side] = None
    reason: str = ""
    tier_index: int = -1          # CLOSE 时指定平仓的层索引；-1 = 整仓全平
    tier_id: str = ""             # CLOSE 时指定平仓的层稳定身份（替代脆弱索引）
    price: float = 0.0            # 限价挂单价；0 = 市价（止损/强平/全平直接成交）
    market: bool = False          # True = 市价单（不挂单，直接成交）
