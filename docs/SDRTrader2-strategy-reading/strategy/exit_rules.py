"""平仓规则（推荐文档 §8，优先级从高到低）— 逐层仓位管理：

1. 下一交易日任一腿进入当月 → 整仓强平（REX1）
2. 逐层止损：任一单层浮动亏损 ≤ −stop_loss_per_tier → 平该层（每 tick 最多一层）
3. 持仓超过 max_hold_days → 整仓时间止损
4. 逐层止盈（回归平仓）：从最新层开始，该层回到建仓时 mean 方向
   且该层**净利**（毛利 − 开平手续费）≥ min_exit_pnl → 平该层（每 tick 最多一层）

每 tick 至多产生一条平仓决策（最多平一层），避免整仓一次性全平的
"一下平掉多个仓位"行为；强平/时间止损为整仓级清仓事件。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from strategy.filters import FilterChain
from strategy.models import Position, Side, Signal


@dataclass
class ExitDecision:
    exit: bool = False
    reason: str = ""
    priority: int = 0   # 1=强平 2=止损 3=时间 4=回归
    volume: int = 0     # 平仓手数（>0）；整仓全平时 = pos.volume
    tier_index: int = -1  # 平仓的层索引；-1 = 整仓
    tier_id: str = ""     # 平仓的层稳定身份（止损/回归单层用）


def _trading_days_between(d1: str, d2: str) -> Optional[int]:
    """两个交易日的自然日差 × 5/7 近似交易日数（d2 - d1）。"""
    try:
        a = date.fromisoformat(d1)
        b = date.fromisoformat(d2)
    except (ValueError, TypeError):
        return None
    return int((b - a).days * 5 / 7)


class ExitRules:
    def __init__(self, *, stop_loss: float, min_exit_pnl: float,
                 max_hold_days: int, multiplier: float,
                 max_positions: int = 5,
                 filters: Optional[FilterChain] = None,
                 fee_fn: Optional[Callable] = None) -> None:
        self.stop_loss = stop_loss
        self.min_exit_pnl = min_exit_pnl
        self.max_hold_days = max_hold_days
        self.multiplier = multiplier
        # 逐层止损阈值 = 整仓止损 / 最大层数（每层独立风险预算）
        self.stop_loss_per_tier = stop_loss / max(1, max_positions)
        self.filters = filters
        # 该层「开 → 平」一次的手续费（元）：(tier, pos_side, sig) -> float。
        # 供回归止盈的净利判据使用；未提供时退化为毛利判据（兼容旧调用/测试）。
        self.fee_fn = fee_fn

    def decide(self, pos: Position, sig: Signal,
               near_inst: str, far_inst: str) -> list[ExitDecision]:
        """按优先级检查平仓条件，返回本次应执行的决策（0 或 1 条）。"""
        if not pos.is_open:
            return []

        # 1. 强平（REX1）→ 整仓全平
        if self.filters is not None:
            need, reason = self.filters.roll_exit_needed((near_inst, far_inst), sig.trading_day)
            if need:
                return [ExitDecision(True, reason, 1, pos.volume, -1)]

        # 2. 逐层止损（先于时间止损，保持原优先级顺序）
        worst: Optional[tuple[float, int, object]] = None
        for i, tier in enumerate(pos.tiers):
            pnl = self._tier_pnl(tier, pos.side, sig)
            if pnl <= -self.stop_loss_per_tier:
                if worst is None or pnl < worst[0]:
                    worst = (pnl, i, tier)
        if worst is not None:
            pnl, i, tier = worst
            return [ExitDecision(
                True,
                f"止损 第{i + 1}层 pnl={pnl:.0f}<={-self.stop_loss_per_tier:.0f}",
                2, tier.volume, i, getattr(tier, "tier_id", ""))]

        # 3. 时间止损 → 整仓全平
        if pos.open_trading_day:
            held = _trading_days_between(pos.open_trading_day, sig.trading_day)
            if held is not None and held > self.max_hold_days:
                return [ExitDecision(
                    True, f"时间止损 持仓{held}>{self.max_hold_days}日", 3,
                    pos.volume, -1)]

        # 4. 逐层止盈（回归平仓）：从最新层开始，回到该层建仓 mean 方向 + 净利达标
        for i in range(len(pos.tiers) - 1, -1, -1):
            tier = pos.tiers[i]
            net = self._tier_net_pnl(tier, pos.side, sig)
            back_to_mean = (
                (pos.side is Side.LONG and sig.mean >= tier.entry_mean)
                or (pos.side is Side.SHORT and sig.mean <= tier.entry_mean)
            )
            if back_to_mean and net >= self.min_exit_pnl:
                return [ExitDecision(
                    True,
                    f"回归平仓 第{i + 1}层 mean={sig.mean:.1f} 净利={net:.0f}",
                    4, tier.volume, i, getattr(tier, "tier_id", ""))]

        return []

    def _tier_pnl(self, tier, pos_side: Side, sig: Signal) -> float:
        """单层浮动盈亏（元）。优先用平仓对手价，缺省退化为 mid 价差。"""
        price = sig.exit_price if sig.exit_price == sig.exit_price else sig.spread
        return pos_side.sign * (price - tier.avg_price) * self.multiplier * tier.volume

    def _tier_net_pnl(self, tier, pos_side: Side, sig: Signal) -> float:
        """单层净利（元）= 毛利 − 该层「开+平」手续费。

        fee_fn 未提供时退化为毛利（兼容旧调用）。费用估算失败不阻断平仓判定。
        """
        gross = self._tier_pnl(tier, pos_side, sig)
        if self.fee_fn is None:
            return gross
        try:
            return gross - float(self.fee_fn(tier, pos_side, sig))
        except Exception:  # noqa: BLE001
            return gross
