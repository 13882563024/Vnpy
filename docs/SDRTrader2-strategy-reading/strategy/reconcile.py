"""对账核验（规划方案 §6.3）：启动采集/断线重连强制对账，只比对不修改。

- 持仓：本地各层同方向手数合计 vs CTP（分腿归并后组合维度），不一致 → 警告；
- 报单：本地有、CTP 无 → 移除本地记录；CTP 有、本地无 → 提示性警告（可能其他策略）；
- 对账期间只平不开（engine 置 reconciling）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from strategy.models import Position
from trading.broker import PositionInfo


@dataclass
class ReconcileResult:
    to_remove_local_ids: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    logs: list = field(default_factory=list)   # 信息性日志（不构成警告）


def reconcile_positions(local_pos: Position, ctp_positions: list[PositionInfo],
                        slug: str) -> list[str]:
    """持仓比对：本地（组合维度）vs CTP（已归并组合维度）。不一致 → 警告列表。"""
    warnings: list[str] = []
    if not local_pos.is_open:
        local_long = local_short = 0
    else:
        local_long = local_pos.volume if local_pos.side.name == "LONG" else 0
        local_short = local_pos.volume if local_pos.side.name == "SHORT" else 0
    ctp_long = sum(p.volume for p in ctp_positions if p.direction == "long")
    ctp_short = sum(p.volume for p in ctp_positions if p.direction == "short")
    if local_long != ctp_long:
        warnings.append(
            f"[{slug}] 持仓不一致：本地多头 {local_long} 手 vs CTP {ctp_long} 手（只警告，不修改）")
    if local_short != ctp_short:
        warnings.append(
            f"[{slug}] 持仓不一致：本地空头 {local_short} 手 vs CTP {ctp_short} 手（只警告，不修改）")
    return warnings


def combo_position_from_legs(near: list[PositionInfo], far: list[PositionInfo],
                             slug: str) -> list[PositionInfo]:
    """把单腿持仓映射为跨期组合持仓（CTP 组合单拆腿后持仓按腿登记，用户口径）。

    跨期价差 = 近月 − 远月：
      近月多 + 远月空 → 组合多头；
      近月空 + 远月多 → 组合空头。
    取配对手数 min；腿数不匹配时未配对部分视为非组合单边仓，不纳入（提示异常）。
    """
    def _vol(positions: list[PositionInfo], direction: str) -> int:
        return sum(p.volume for p in positions if p.direction == direction)

    out: list[PositionInfo] = []
    combo_long = min(_vol(near, "long"), _vol(far, "short"))
    if combo_long > 0:
        out.append(PositionInfo(slug=slug, direction="long", volume=combo_long))
    combo_short = min(_vol(near, "short"), _vol(far, "long"))
    if combo_short > 0:
        out.append(PositionInfo(slug=slug, direction="short", volume=combo_short))
    return out


def leg_imbalance_warnings(near: list[PositionInfo], far: list[PositionInfo],
                           slug: str) -> list[str]:
    """两腿手数不匹配（组合'残破'）→ 警告（正常组合单不会产生，多半是历史遗留/手工单）。"""
    warnings: list[str] = []
    for direction in ("long", "short"):
        nv = sum(p.volume for p in near if p.direction == direction)
        fv = sum(p.volume for p in far if p.direction == direction)
        if nv != fv:
            warnings.append(
                f"[{slug}] 两腿{direction}手数不匹配：近月 {nv} 手 vs 远月 {fv} 手（组合残破，需人工核对）")
    return warnings


def reconcile_orders(local_orders: dict, ctp_orders: list,
                     slug: str) -> ReconcileResult:
    """报单比对：
    - 本地有、CTP 无 → 移除本地记录（to_remove）；
    - CTP 有、本地无 → 提示性警告（可能是其他策略）。
    """
    result = ReconcileResult()
    ctp_refs = {o.broker_ref for o in ctp_orders if o.broker_ref}
    for local_id, o in local_orders.items():
        if o.get("status") not in ("TRADED", "CANCELLED", "REJECTED", "TIMEOUT"):
            # 未终态单必须能在 CTP 找到，否则视为本地残留
            if o.get("broker_ref") and o["broker_ref"] not in ctp_refs:
                result.to_remove_local_ids.append(local_id)
                result.logs.append(
                    f"[{slug}] 本地报单 {local_id} 不在 CTP 中 → 移除本地记录")
    local_refs = {o.get("broker_ref") for o in local_orders.values() if o.get("broker_ref")}
    for o in ctp_orders:
        if o.broker_ref and o.broker_ref not in local_refs:
            result.warnings.append(
                f"[{slug}] CTP 报单 ref={o.broker_ref} 本地无记录（可能是其他策略）")
    return result
