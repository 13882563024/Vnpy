"""执行层公共定义：层（tier）数据、目标价差与保守取整、执行器协议。

目标价差（方案 3.1）::

    target = layer_avg + direction × (min_exit_pnl + fee_oc) ÷ (size × layer_volume)

量纲：``(元 + 元) ÷ (元/点 × 手) = 点``，与 ``layer_avg`` 同为价差点，可直接相加。

取整（方案 3.3）：多价差卖出止盈 → **向上**取整；空价差买入止盈 → **向下**取整。
反过来取整会让成交后的净利低于 ``min_exit_pnl``，与需求冲突。
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from utils.filters import SpreadSnapshot

# 价差方向：+1 = 做多价差（买 near 卖 far）；-1 = 做空价差（卖 near 买 far）
DIRECTION_LONG = 1
DIRECTION_SHORT = -1

ROUND_CEIL = "ceil"
ROUND_FLOOR = "floor"
ROUND_NEAREST = "nearest"
ROUND_MODES = (ROUND_CEIL, ROUND_FLOOR, ROUND_NEAREST)

TARGET_ROUND_CONSERVATIVE = "conservative"
TARGET_ROUND_NEAREST = "nearest"


class TierState:
    """层的生命周期状态。"""

    OPEN = "OPEN"
    PENDING_RETRY = "PENDING_RETRY"     # 止盈单挂出失败，指数退避重挂
    CLOSED = "CLOSED"


class AlgoKind:
    """算法用途（写入 ``extra["purpose"]``，也用于策略侧的状态机）。"""

    OPEN = "open"                 # 开仓 / 加仓
    TAKE_PROFIT = "take_profit"   # 预挂止盈
    CLOSE = "close"               # 止损 / 时间止损 / 换月强平（对手价，超时撤单重发）


def new_tier_id() -> str:
    return uuid.uuid4().hex[:8]


def round_to_tick(price: float, pricetick: float, mode: str = ROUND_NEAREST) -> float:
    """把价格对齐到最小变动价位；``mode`` 决定取整方向。"""
    tick = float(pricetick or 0.0)
    if tick <= 0:
        return float(price)
    eps = 1e-9
    if mode == ROUND_CEIL:
        value = math.ceil(float(price) / tick - eps) * tick
    elif mode == ROUND_FLOOR:
        value = math.floor(float(price) / tick + eps) * tick
    elif mode == ROUND_NEAREST:
        value = round(float(price) / tick) * tick
    else:
        raise ValueError(f"未知取整模式：{mode}（应为 {ROUND_MODES}）")
    return round(value, 10)


def calc_target_spread(
    layer_avg: float,
    direction: int,
    min_exit_pnl: float,
    fee_oc: float,
    size: float,
    volume: float,
) -> float:
    """未取整的目标价差（价差点）。"""
    if size <= 0 or volume <= 0:
        return float(layer_avg)
    delta = (float(min_exit_pnl) + float(fee_oc)) / (float(size) * float(volume))
    return float(layer_avg) + (1 if direction > 0 else -1) * delta


def calc_target_price(
    layer_avg: float,
    direction: int,
    min_exit_pnl: float,
    fee_oc: float,
    size: float,
    volume: float,
    pricetick: float = 0.0,
    round_mode: str = TARGET_ROUND_CONSERVATIVE,
) -> float:
    """目标止盈价（已按 ``pricetick`` 保守取整）。"""
    raw = calc_target_spread(layer_avg, direction, min_exit_pnl, fee_oc, size, volume)
    if round_mode == TARGET_ROUND_NEAREST:
        return round_to_tick(raw, pricetick, ROUND_NEAREST)
    return round_to_tick(raw, pricetick, ROUND_CEIL if direction > 0 else ROUND_FLOOR)


def entry_spread_price(snapshot: SpreadSnapshot, direction: int) -> float:
    """开仓可成交的价差（对手价）：做多买 near 卖 far → ask；做空 → bid。"""
    return snapshot.ask_price if direction > 0 else snapshot.bid_price


def exit_spread_price(snapshot: SpreadSnapshot, direction: int) -> float:
    """平仓可成交的价差（对手价）：平多卖 near 买 far → bid；平空 → ask。"""
    return snapshot.bid_price if direction > 0 else snapshot.ask_price


@dataclass
class PositionLayer:
    """一层持仓（组合手数口径）。``tier_id`` 是稳定身份，避免用列表索引平错层。"""

    tier_id: str = field(default_factory=new_tier_id)
    direction: int = DIRECTION_LONG
    volume: float = 0.0
    avg_spread: float = 0.0            # 该层价差成交均价
    near_price: float = 0.0            # 开仓时 near 腿成交价（算手续费）
    far_price: float = 0.0             # 开仓时 far 腿成交价
    open_trading_day: str = ""
    open_datetime: datetime | None = None
    layer_index: int = 1
    entry_mean: float = 0.0
    entry_sd: float = 0.0
    fee_oc: float = 0.0                # 该层「开 + 平」手续费估算（元）
    min_exit_pnl: float = 0.0          # 该层要求的最低净盈利（元）
    target_spread: float = 0.0         # 预挂止盈价（价差点）
    state: str = TierState.OPEN
    adopted: bool = False              # 启动时从柜台旧持仓接管，不是本进程开仓
    algoid: str = ""                   # 该层在挂的止盈 algo
    retry_count: int = 0
    realized_pnl: float = 0.0
    close_spread: float = 0.0
    close_datetime: datetime | None = None
    close_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.state != TierState.CLOSED and self.volume > 0

    def unrealized_pnl(self, exit_price: float, size: float) -> float:
        """该层浮盈（元）：方向 × (平仓对手价 − 层均价) × 乘数 × 手数。"""
        if not self.is_open:
            return 0.0
        return self.direction * (float(exit_price) - self.avg_spread) * float(size) * self.volume

    def mark_closed(self, close_spread: float, moment: datetime | None, reason: str) -> float:
        """标记该层已平，返回已实现毛利（未扣手续费）。"""
        self.state = TierState.CLOSED
        self.close_spread = float(close_spread)
        self.close_datetime = moment
        self.close_reason = reason
        self.realized_pnl = (self.close_spread - self.avg_spread) * self.direction
        self.algoid = ""
        return self.realized_pnl

    def describe(self) -> str:
        """一行摘要；``target_spread`` 尚未计算（=0）时不显示，避免日志出现"目标0"。"""
        text = (
            f"第{self.layer_index}层[{self.tier_id}] "
            f"{'多' if self.direction > 0 else '空'}{self.volume:g}手 均价{self.avg_spread:g}"
        )
        if self.target_spread:
            text += f" 目标{self.target_spread:g}"
        return text


@dataclass
class AlgoJob:
    """策略侧记录的"这个 algoid 是为了什么"，用于回报到达时定位层。"""

    algoid: str
    kind: str
    direction: int
    volume: float
    layer_index: int = -1
    tier_id: str = ""
    tier_ids: list[str] = field(default_factory=list)
    target_spread: float = 0.0
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    done: bool = False


class AlgoImpl(Protocol):
    """``CrossPeriodAlgo`` 内部的具体执行实现（两腿 / 交易所组合单）。"""

    def start(self) -> None:
        """算法启动后发出第一笔委托。"""
        ...

    def on_tick(self, tick) -> None:
        ...

    def on_order(self, order) -> None:
        ...

    def on_trade(self, trade) -> None:
        ...

    def on_interval(self) -> None:
        """每秒一次（``interval=0``），负责超时撤单/追价/部分成交收尾。"""
        ...

    def cancel_all(self) -> None:
        ...

    def is_order_finished(self) -> bool:
        ...


class LayerExecutor(Protocol):
    """执行层接口（方案 5.1）：策略只通过这四个方法动仓。

    实现由策略自身承担（它持有 ``strategy_engine`` 才能 ``start_algo``），
    具体走两腿还是交易所组合单由 ``extra["mode"]`` 决定，对策略透明。
    """

    def open_layer(self, spread, direction: int, volume: float,
                   layer_index: int, reason: str = "") -> str:
        """开一层（或加一层），返回 algoid。"""
        ...

    def exit_layer(self, spread, layer: PositionLayer) -> str:
        """按 ``layer.target_spread`` 预挂止盈单，返回 algoid。"""
        ...

    def close_layer(self, spread, tier_ids: list[str], reason: str = "") -> str:
        """对手价平掉指定层（空列表 = 整仓），返回 algoid。"""
        ...

    def cancel(self, algoid: str) -> None:
        """撤销指定 algo（及其挂单）。"""
        ...
