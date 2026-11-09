"""过滤器链（推荐文档 §5：W1 / S0.5 / D1 / VR / REC / REX / MOM）。

每个过滤器返回 (通过与否, 阻止原因)；链上任何一个不过即阻止开仓。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from data_loader.calendar import parse_contract_month
from data_loader.reader import SpreadTick
from strategy.config import FilterParams
from strategy.models import Side, Signal


@dataclass
class FilterResult:
    passed: bool = True
    blocked_by: Optional[str] = None
    details: dict = field(default_factory=dict)


def _days_to_contract_month(inst: str, trading_day: str) -> Optional[int]:
    """距合约进入当月的自然日数；trading_day 格式 YYYY-MM-DD。"""
    cm = parse_contract_month(inst)
    if cm is None:
        return None
    try:
        cur = date.fromisoformat(trading_day)
    except ValueError:
        return None
    return (cm - cur).days


def _trading_days_to_contract(inst: str, trading_day: str) -> Optional[int]:
    """距合约进入当月的近似交易日数（自然日 × 5/7）。"""
    days = _days_to_contract_month(inst, trading_day)
    if days is None:
        return None
    return int(days * 5 / 7)


class FilterChain:
    """按推荐文档 §5 顺序执行过滤器；MOM 需要最近 momentum_min 分钟的 spread 历史。"""

    def __init__(self, params: FilterParams, tick_size: float) -> None:
        self.p = params
        self.tick_size = tick_size

    # ---------- 5.1 盘口宽度 ----------
    def width_ok(self, tick: SpreadTick) -> tuple[bool, Optional[str]]:
        max_w = self.p.width_ticks * self.tick_size
        if self.tick_size > 0 and (tick.near.width > max_w or tick.far.width > max_w):
            return False, f"W1:盘口宽度超标 near={tick.near.width:.1f} far={tick.far.width:.1f}"
        return True, None

    # ---------- 5.2 报价同步 ----------
    def sync_ok(self, tick: SpreadTick) -> tuple[bool, Optional[str]]:
        if tick.near.recv_ns and tick.far.recv_ns:
            dt = abs(tick.near.recv_ns - tick.far.recv_ns) / 1e9
            if dt > self.p.sync_s:
                return False, f"S0.5:报价不同步 {dt:.3f}s"
        # 数据缺 recv_ns 时退化为用事件时间（同一条 tick 内两腿已由采集端合成，视为同步）
        return True, None

    # ---------- 5.3 一档深度 ----------
    def depth_ok(self, tick: SpreadTick) -> tuple[bool, Optional[str]]:
        # replay 数据无一档挂量字段：数据层已保证有报价，视为通过（实盘 L1 深度压力另行评估）
        return True, None

    # ---------- 5.4 成交量比例 ----------
    def volume_ratio_ok(self, tick: SpreadTick) -> tuple[bool, Optional[str]]:
        vr = self.p.volume_ratio_max
        if vr <= 0:
            return True, None
        nv, fv = tick.near.volume, tick.far.volume
        if min(nv, fv) == 0:
            return False, "VR:某腿成交量为0"
        ratio = max(nv, fv) / min(nv, fv)
        if ratio > vr:
            return False, f"VR:两腿成交量比例 {ratio:.1f}>{vr}"
        return True, None

    # ---------- 5.5 临近到期（开仓过滤 + 强平） ----------
    def roll_entry_ok(self, inst_pair: tuple[str, str], trading_day: str) -> tuple[bool, Optional[str]]:
        cutoff = self.p.roll_entry_cutoff_days
        if cutoff <= 0:
            return True, None
        for inst in inst_pair:
            td = _trading_days_to_contract(inst, trading_day)
            if td is not None and td <= cutoff:
                return False, f"REC{cutoff}:{inst}距当月 {td} 交易日"
        return True, None

    def roll_exit_needed(self, inst_pair: tuple[str, str], trading_day: str) -> tuple[bool, Optional[str]]:
        rex = self.p.roll_exit_days
        if rex <= 0:
            return False, None
        for inst in inst_pair:
            td = _trading_days_to_contract(inst, trading_day)
            if td is not None and td <= rex:
                return True, f"REX{rex}:{inst}下一交易日进当月"
        return False, None

    # ---------- 5.6 动量 ----------
    def momentum_ok(self, side: Side, momentum: float) -> tuple[bool, Optional[str]]:
        mom_min = self.p.momentum_min
        if mom_min <= 0:
            return True, None
        if side is Side.LONG and momentum < 0:
            return False, f"MOM:做多但价差仍在下跌 {momentum:.2f}"
        if side is Side.SHORT and momentum > 0:
            return False, f"MOM:做空但价差仍在上涨 {momentum:.2f}"
        return True, None

    # ---------- 组合 ----------
    def entry_allowed(self, tick: SpreadTick, trading_day: str,
                      momentum: Optional[float]) -> FilterResult:
        """开仓前的完整过滤链。"""
        checks = [
            ("width", lambda: self.width_ok(tick)),
            ("sync", lambda: self.sync_ok(tick)),
            ("depth", lambda: self.depth_ok(tick)),
            ("volume_ratio", lambda: self.volume_ratio_ok(tick)),
            ("roll_entry", lambda: self.roll_entry_ok(
                (tick.near.inst, tick.far.inst), trading_day)),
        ]
        res = FilterResult()
        for name, fn in checks:
            ok, reason = fn()
            if not ok:
                res.passed = False
                res.blocked_by = reason
                return res
        # 动量需在方向确定后判定（由状态机调用 momentum_ok）
        return res
