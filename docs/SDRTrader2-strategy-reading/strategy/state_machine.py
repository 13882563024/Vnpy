"""策略状态机（规划方案 §6）：evaluate(ctx) → Action[]，纯函数。

市价单模型（order_type=market）下的决策：
- 持仓中：按平仓优先级检查（强平>止损>时间>回归），未触发则检查加仓；
- 无持仓：过滤器全过 + 统计窗口就绪 + |偏离| >= 分层阈值 → 开仓；
- 分层：第 n+1 层阈值 = X + n × layer_step（n = 当前层数）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from strategy.config import LayeringParams, SignalParams
from strategy.exit_rules import ExitRules
from strategy.filters import FilterChain, FilterResult
from strategy.models import Action, ActionType, Position, Side, Signal


@dataclass
class DecisionContext:
    """一次 evaluate 的输入。"""
    pos: Position
    sig: Signal
    filter_result: FilterResult
    momentum: Optional[float] = None      # 最近 momentum_min 分钟 spread 变化


class StateMachine:
    def __init__(self, *, signal: SignalParams, layering: LayeringParams,
                 filters: FilterChain, exit_rules: ExitRules) -> None:
        self.signal = signal
        self.layering = layering
        self.filters = filters
        self.exit_rules = exit_rules

    # ---------- 限价挂单价 ----------
    def _tick_size(self) -> float:
        return max(0.000001, getattr(self.filters, "tick_size", 1.0) or 1.0)

    def limit_price(self, side: Side, sig: Signal) -> float:
        """开/加仓挂单价 = 当前盘口对手价（买卖一价），保证价格贴近真实可成交价：

        做多（买 near 卖 far）：near.ask - far.bid；
        做空（卖 near 买 far）：near.bid - far.ask。
        对手价本身即盘口价差，无需再取整到 tick。
        """
        return sig.long_cross_price if side is Side.LONG else sig.short_cross_price

    def close_limit_price(self, sig: Signal, pos_side: Side) -> float:
        """平仓限价挂单价：平仓对手价取整（平多=卖向下、平空=买向上）。"""
        ts = self._tick_size()
        px = sig.exit_price if sig.exit_price == sig.exit_price else sig.spread
        if pos_side is Side.LONG:     # 卖组合 → 向下取整
            return math.floor(px / ts) * ts
        return math.ceil(px / ts) * ts  # 买组合 → 向上取整

    # ---------- 开仓/加仓阈值 ----------
    def _entry_threshold(self, tier_count: int) -> float:
        """第 tier_count+1 层的偏离阈值（sd 倍数）。"""
        return self.signal.x_sd + tier_count * self.layering.layer_step

    def _open_allowed(self, ctx: DecisionContext) -> tuple[bool, Optional[Action]]:
        """无仓时判断是否开底仓。"""
        sig = ctx.sig
        if not sig.ready:
            return False, None
        if not ctx.filter_result.passed:
            return False, None

        dev = (sig.spread - sig.mean) / sig.sd
        thr = self._entry_threshold(0)
        side = Side.from_spread(sig.spread, sig.mean)
        triggered = (side is Side.LONG and dev <= -thr) or (side is Side.SHORT and dev >= thr)
        if not triggered:
            return False, None

        # 动量过滤
        ok, _ = self.filters.momentum_ok(side, ctx.momentum if ctx.momentum is not None else 0.0)
        if not ok:
            return False, None

        return True, Action(ActionType.OPEN, 1, side,
                            f"开仓 side={side.name} dev={dev:.2f}",
                            price=self.limit_price(side, sig))

    def _add_allowed(self, ctx: DecisionContext) -> tuple[bool, Optional[Action]]:
        """持仓中判断是否加一层（同方向继续偏离）。"""
        pos = ctx.pos
        sig = ctx.sig
        if not sig.ready or not ctx.filter_result.passed:
            return False, None
        # 总手数上限（底仓 1 手 + 加仓至 max_positions 手为止）
        if pos.volume >= self.layering.max_positions:
            return False, None
        n = pos.tier_count
        dev = (sig.spread - sig.mean) / sig.sd
        thr = self._entry_threshold(n)
        side = pos.side
        triggered = (side is Side.LONG and dev <= -thr) or (side is Side.SHORT and dev >= thr)
        if not triggered:
            return False, None

        ok, _ = self.filters.momentum_ok(side, ctx.momentum if ctx.momentum is not None else 0.0)
        if not ok:
            return False, None

        return True, Action(ActionType.ADD, 1, side, f"加仓 第{n + 1}层 dev={dev:.2f}",
                            price=self.limit_price(side, sig))

    # ---------- 主入口 ----------
    def evaluate(self, ctx: DecisionContext) -> list[Action]:
        pos = ctx.pos
        sig = ctx.sig

        # 持仓中：平仓优先级（逐层退出，每 tick 至多一层）
        if pos.is_open:
            decisions = self.exit_rules.decide(pos, sig, sig.near_inst, sig.far_inst)
            if decisions:
                d = decisions[0]
                # 强平(1)/止损(2)/时间止损(3) → 市价立即成交；回归止盈(4) → 限价挂单
                market = d.priority in (1, 2, 3)
                price = 0.0 if market else self.close_limit_price(sig, pos.side)
                return [Action(ActionType.CLOSE, d.volume, reason=d.reason,
                               tier_index=d.tier_index, tier_id=d.tier_id,
                               price=price, market=market)]
            # 未平仓 → 尝试加仓
            ok, act = self._add_allowed(ctx)
            if ok and act is not None:
                return [act]
            return []

        # 无仓：开底仓
        ok, act = self._open_allowed(ctx)
        if ok and act is not None:
            return [act]
        return []
