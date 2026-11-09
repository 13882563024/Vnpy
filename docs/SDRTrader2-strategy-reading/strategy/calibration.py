"""持仓校准：从 CTP 单腿持仓反推跨期组合持仓，与本地持仓比对并生成校准计划。

反推依据（字段口径 / 公式 / 边界情况）见 `docs/position-calibration.md`。

本模块是**纯计算层**（不访问 broker、不碰磁盘），便于单测：
  aggregate_legs    通道返回的腿持仓行 → 按 (合约, 方向) 汇总（今昨仓合并）
  infer_combo       两腿持仓 → 组合维度（方向 / 手数 / 价差成本）+ 告警
  plan_calibration  组合反推结果 vs 本地分层持仓 → 校准计划（保留层结构规则）

查询 CTP、落盘与日志由 `strategy.engine.calibrate_position_from_ctp` 负责。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from strategy.models import Position
from trading.broker import PositionInfo

# 腿均价合理性上限（元/吨）：超出说明 cost/price 口径不符（例如把金额当价格），
# 此时按「均价不可用」处理，只校准方向与手数（见文档 §5.4）。
MAX_LEG_PRICE = 1_000_000.0

SIDE_CN = {"LONG": "多", "SHORT": "空", "": "空仓"}

_MISSING_EPS = 1e-9


def _missing_price(px: float) -> bool:
    """均价是否缺失（0 占位/未填）。

    注意：组合均价是**价差**（近月 − 远月），负数完全合法（近月贴水），
    不能拿 `<= 0` 当缺失判据，否则合法的负均价会被 CTP 盯市成本覆盖。
    """
    return abs(float(px or 0.0)) < _MISSING_EPS



@dataclass
class LegPosition:
    """单腿持仓（按合约 + 方向汇总后的口径）。"""
    instrument: str
    direction: str            # long / short
    volume: int
    avg_price: float = 0.0    # 腿均价（元/吨；通道直接给价格时可用）
    cost: float = 0.0         # 持仓成本金额（CTP PositionCost；含乘数）


def aggregate_legs(rows: Iterable[PositionInfo]) -> dict[str, list[LegPosition]]:
    """腿持仓行 → `{合约: [多, 空]}`（同合约同方向合并手数与成本）。

    CTP 持仓按「方向 + 今昨仓」拆成多行，反推前必须合并，否则同一腿会出现
    多条记录、成本均价按行而不是按总量计算。
    """
    buckets: dict[tuple[str, str], LegPosition] = {}
    for r in rows:
        instrument = str(getattr(r, "slug", "") or "")
        direction = str(getattr(r, "direction", "") or "").lower()
        if direction not in ("long", "short"):
            continue
        vol = int(getattr(r, "volume", 0) or 0)
        if vol <= 0:
            continue
        price = float(getattr(r, "avg_price", 0.0) or 0.0)
        cost = float(getattr(r, "cost", 0.0) or 0.0)
        cur = buckets.get((instrument, direction))
        if cur is None:
            buckets[(instrument, direction)] = LegPosition(
                instrument, direction, vol, price, cost)
            continue
        # 有价格的行按手数加权；无价格的行累加成本金额。同一通道口径一致，
        # 不会混用（混用时优先价格均价，成本仅作备用）。
        if price > 0 and cur.avg_price > 0:
            total = cur.volume + vol
            cur.avg_price = (cur.avg_price * cur.volume + price * vol) / total
            cur.cost += cost
        elif price > 0 and cur.avg_price <= 0:
            cur.avg_price = price
            cur.cost += cost
        else:
            cur.cost += cost
        cur.volume += vol
    out: dict[str, list[LegPosition]] = {}
    for leg in buckets.values():
        out.setdefault(leg.instrument, []).append(leg)
    for legs in out.values():
        legs.sort(key=lambda x: (x.direction != "long", x.instrument))
    return out


def leg_price(legs: list[LegPosition], multiplier: float) -> tuple[float, bool]:
    """腿均价（元/吨）+ 是否完整可用。

    口径：优先用通道给的 `avg_price`（已是价格）；否则用持仓成本金额反推
    `Σcost ÷ (Σ手数 × 乘数)`（CTP `PositionCost` 口径）。
    部分行缺价格 / 结果越界 → 视为不可用（不猜）。
    """
    volume = sum(l.volume for l in legs)
    if volume <= 0:
        return 0.0, False
    priced = [l for l in legs if l.avg_price > 0]
    if priced:
        priced_vol = sum(l.volume for l in priced)
        px = sum(l.avg_price * l.volume for l in priced) / priced_vol
        ok = priced_vol == volume and 0 < px <= MAX_LEG_PRICE
        return (px if ok else 0.0), ok
    total_cost = sum(l.cost for l in legs if l.cost > 0)
    if total_cost > 0 and multiplier > 0:
        px = total_cost / (volume * multiplier)
        ok = 0 < px <= MAX_LEG_PRICE
        return (px if ok else 0.0), ok
    return 0.0, False


def _vol(legs: list[LegPosition], direction: str) -> int:
    return sum(l.volume for l in legs if l.direction == direction)


@dataclass
class ComboInference:
    """单腿持仓 → 跨期组合持仓的反推结果。"""
    slug: str = ""
    side: str = ""                 # LONG / SHORT / ""（无净持仓）
    volume: int = 0                # 净手数
    avg_price: float = 0.0         # 组合成本价差（近 − 远）
    avg_price_valid: bool = False  # 成本是否反推成功
    long_volume: int = 0           # 配对得到的组合多头手数
    short_volume: int = 0          # 配对得到的组合空头手数
    long_avg_price: float = 0.0
    short_avg_price: float = 0.0
    near_long: int = 0
    near_short: int = 0
    far_long: int = 0
    far_short: int = 0
    legs: list[dict] = field(default_factory=list)      # 腿明细（展示用）
    warnings: list[str] = field(default_factory=list)

    @property
    def has_position(self) -> bool:
        return self.side in ("LONG", "SHORT") and self.volume > 0

    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "side": self.side, "volume": self.volume,
            "avg_price": round(self.avg_price, 4),
            "avg_price_valid": self.avg_price_valid,
            "long_volume": self.long_volume, "short_volume": self.short_volume,
            "long_avg_price": round(self.long_avg_price, 4),
            "short_avg_price": round(self.short_avg_price, 4),
            "near_long": self.near_long, "near_short": self.near_short,
            "far_long": self.far_long, "far_short": self.far_short,
            "has_position": self.has_position,
            "legs": self.legs, "warnings": self.warnings,
        }


def infer_combo(near_legs: list[LegPosition], far_legs: list[LegPosition], *,
                slug: str = "", multiplier: float = 1.0,
                suppress_imbalance_warning: bool = False) -> ComboInference:
    """两腿持仓 → 组合持仓（方向 / 手数 / 价差成本），并按 §5 生成告警。

    suppress_imbalance_warning=True：账户级联合反推时用（腿手数已被其他组合占用，
    剩余不匹配不再等于"裸腿"，由 `allocate_legs` 统一给出未使用腿说明）。
    """
    inf = ComboInference(slug=slug)
    inf.near_long, inf.near_short = _vol(near_legs, "long"), _vol(near_legs, "short")
    inf.far_long, inf.far_short = _vol(far_legs, "long"), _vol(far_legs, "short")
    inf.long_volume = min(inf.near_long, inf.far_short)
    inf.short_volume = min(inf.near_short, inf.far_long)

    mult = float(multiplier or 0.0)
    near_long_px, near_long_ok = leg_price(
        [l for l in near_legs if l.direction == "long"], mult)
    far_short_px, far_short_ok = leg_price(
        [l for l in far_legs if l.direction == "short"], mult)
    near_short_px, near_short_ok = leg_price(
        [l for l in near_legs if l.direction == "short"], mult)
    far_long_px, far_long_ok = leg_price(
        [l for l in far_legs if l.direction == "long"], mult)

    if inf.long_volume > 0:
        inf.long_avg_price = near_long_px - far_short_px
    if inf.short_volume > 0:
        inf.short_avg_price = near_short_px - far_long_px

    if inf.long_volume > inf.short_volume:
        inf.side = "LONG"
        inf.volume = inf.long_volume - inf.short_volume
        inf.avg_price = inf.long_avg_price
        inf.avg_price_valid = near_long_ok and far_short_ok
    elif inf.short_volume > inf.long_volume:
        inf.side = "SHORT"
        inf.volume = inf.short_volume - inf.long_volume
        inf.avg_price = inf.short_avg_price
        inf.avg_price_valid = near_short_ok and far_long_ok

    if mult <= 0:
        inf.warnings.append(
            f"合约乘数未配置（multiplier={mult}）：腿持仓成本无法换算为价格，"
            f"组合均价不可用")
    for label, near_v, far_v in (("多头", inf.near_long, inf.far_short),
                                 ("空头", inf.near_short, inf.far_long)):
        if near_v != far_v and not suppress_imbalance_warning:
            inf.warnings.append(
                f"两腿{label}手数不匹配：近月 {near_v} 手 vs 远月 {far_v} 手，"
                f"未配对 {abs(near_v - far_v)} 手（可能是其他跨期组合/手工单占用，需核对）")
    if inf.long_volume > 0 and inf.short_volume > 0:
        inf.warnings.append(
            f"柜台双向持仓（组合多 {inf.long_volume} 手 / 空 {inf.short_volume} 手）："
            f"本地只支持单方向 → 按净手数 {inf.volume} 手校准")
    if inf.has_position and not inf.avg_price_valid:
        inf.warnings.append(
            "腿持仓成本不可用（缺持仓成本/均价或乘数不符）：组合均价无法反推，"
            "均价沿用本地或留 0")
    inf.legs = [
        {"instrument": l.instrument, "direction": l.direction,
         "volume": l.volume, "avg_price": round(l.avg_price, 4),
         "cost": round(l.cost, 2)}
        for l in list(near_legs) + list(far_legs)
    ]
    return inf


@dataclass
class CalibrationPlan:
    """校准计划（dry-run 预览与应用共用）。"""
    slug: str = ""
    local: dict = field(default_factory=dict)
    target: dict = field(default_factory=dict)
    tiers: list[dict] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rebuild: bool = False           # 是否重建为单层（方向变化/本地空仓）
    avg_price_source: str = ""      # local / ctp / mixed / ""（无持仓）

    @property
    def need_change(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "need_change": self.need_change,
            "rebuild": self.rebuild, "local": self.local,
            "target": self.target, "tiers": self.tiers,
            "changes": self.changes, "warnings": self.warnings,
            "avg_price_source": self.avg_price_source,
        }


def _tier_snapshot(t) -> dict:
    """本地 Tier → 计划层快照（保留 tier_id 等稳定身份）。"""
    return {
        "volume": int(t.volume),
        "avg_price": float(t.avg_price or 0.0),
        "tier_id": str(getattr(t, "tier_id", "") or ""),
        "open_ts": float(getattr(t, "open_ts", 0.0) or 0.0),
        "open_trading_day": str(getattr(t, "open_trading_day", "") or ""),
        "entry_mean": float(getattr(t, "entry_mean", 0.0) or 0.0),
        "source": "keep",
    }


def plan_calibration(local_pos: Position, inference: ComboInference, *,
                     max_positions: int = 3, tick_size: float = 1.0,
                     trading_day: str = "", entry_mean: float = 0.0,
                     now: Optional[float] = None) -> CalibrationPlan:
    """组合反推结果 vs 本地分层持仓 → 校准计划。

    规则（用户确认口径，详见文档 §6）：
    - 均价：本地已有 → 保留并告警差异；本地缺失 → 用 CTP 反推成本补齐；
    - 层结构：方向一致时尽量保留（差额从最新层削减 / 按层补齐、必要时新增层）；
      方向不同或本地空仓 → 重建为单层；
    - 手数 / 方向始终以柜台反推结果为准。
    """
    now = time.time() if now is None else float(now)
    local_open = bool(local_pos.is_open)
    local_side = local_pos.side.name if local_open and local_pos.side else ""
    local_vol = local_pos.volume if local_open else 0
    local_avg = local_pos.avg_price if local_vol > 0 else 0.0
    target_side = inference.side if inference.has_position else ""
    target_vol = int(inference.volume) if inference.has_position else 0
    ctp_avg = inference.avg_price if inference.avg_price_valid else 0.0

    plan = CalibrationPlan(
        slug=inference.slug,
        local={"side": local_side, "volume": local_vol,
               "avg_price": round(local_avg, 4),
               "tier_count": len(local_pos.tiers) if local_open else 0},
        target={"side": target_side, "volume": target_vol,
                "ctp_avg_price": round(ctp_avg, 4),
                "ctp_avg_price_valid": bool(inference.avg_price_valid)},
        warnings=list(inference.warnings),
    )

    # ---- 均价选择：本地已有则保留，缺失才用 CTP 反推（盯市成本）补齐 ----
    if target_vol <= 0:
        avg_price = 0.0
        plan.avg_price_source = ""
    elif local_vol <= 0 or _missing_price(local_avg):
        avg_price = ctp_avg
        plan.avg_price_source = "ctp" if inference.avg_price_valid else ""
        if not inference.avg_price_valid:
            plan.warnings.append("CTP 组合成本不可用：新持仓均价留 0，请人工补填")
    elif inference.avg_price_valid:
        avg_price = local_avg
        plan.avg_price_source = "local"
        diff = ctp_avg - local_avg
        if abs(diff) > max(float(tick_size or 0.0), 1e-9):
            plan.warnings.append(
                f"均价差异：本地 {local_avg:.4f} vs CTP 反推 {ctp_avg:.4f}"
                f"（差 {diff:+.4f}）。CTP 为盯市成本（昨仓按昨结算价重算），"
                f"不代表原始建仓价 → 保留本地均价")
    else:
        avg_price = local_avg
        plan.avg_price_source = "local"
        plan.warnings.append("CTP 组合成本不可用：均价沿用本地")
    plan.target["avg_price"] = round(avg_price, 4)

    # ---- 层结构 ----
    if target_vol <= 0:
        plan.tiers = []
        plan.rebuild = bool(local_vol)
        if local_vol:
            plan.changes.append(
                f"清空持仓（本地 {SIDE_CN.get(local_side, local_side)} {local_vol} 手 → CTP 无持仓）")
        return plan

    if local_vol <= 0 or local_side != target_side:
        plan.rebuild = True
        plan.tiers = [{
            "volume": target_vol, "avg_price": round(avg_price, 4), "tier_id": "",
            "open_ts": now, "open_trading_day": trading_day,
            "entry_mean": entry_mean, "source": "rebuild"}]
        if local_vol > 0:
            plan.changes.append(
                f"方向 {SIDE_CN.get(local_side, local_side)} → "
                f"{SIDE_CN.get(target_side, target_side)}（重建为单层）")
            plan.changes.append(f"手数 {local_vol} → {target_vol}")
        else:
            plan.changes.append(f"本地空仓 → 按 CTP 建仓 {target_vol} 手（单层）")
        return plan

    # 方向一致：保留层结构，按差额削减（最新层优先）/ 补齐（必要时新增层）
    tiers = [_tier_snapshot(t) for t in local_pos.tiers if int(t.volume) > 0]
    cur = sum(t["volume"] for t in tiers)
    if not tiers:      # 防御：本地 is_open 但层为空 → 按重建处理
        plan.rebuild = True
        tiers = [{"volume": target_vol, "avg_price": round(avg_price, 4),
                  "tier_id": "", "open_ts": now,
                  "open_trading_day": trading_day, "entry_mean": entry_mean,
                  "source": "rebuild"}]
        plan.changes.append(f"本地层明细缺失 → 重建 {target_vol} 手")
    elif cur > target_vol:
        need = cur - target_vol
        for t in reversed(tiers):
            if need <= 0:
                break
            cut = min(need, t["volume"])
            t["volume"] -= cut
            need -= cut
            t["source"] = "shrink" if t["volume"] > 0 else "removed"
        tiers = [t for t in tiers if t["volume"] > 0]
    elif cur < target_vol:
        need = target_vol - cur
        cap = max(1, max(t["volume"] for t in tiers))
        for t in reversed(tiers):
            if need <= 0:
                break
            add = min(need, cap - t["volume"])
            if add > 0:
                t["volume"] += add
                need -= add
                t["source"] = "grow"
        limit = max(1, int(max_positions or 1))
        while need > 0:
            if len(tiers) < limit:
                v = min(cap, need)
                tiers.append({
                    "volume": v, "avg_price": round(avg_price, 4), "tier_id": "",
                    "open_ts": now, "open_trading_day": trading_day,
                    "entry_mean": entry_mean, "source": "new"})
                need -= v
            else:
                tiers[-1]["volume"] += need
                tiers[-1]["source"] = "grow"
                plan.warnings.append(
                    f"手数超出分层上限（max_positions={limit}）："
                    f"额外 {need} 手并入最新层")
                need = 0

    # 层均价缺失（人工新增层只填手数）→ 用 CTP 反推成本补齐（不覆盖已有均价）
    filled = 0
    if inference.avg_price_valid:
        for t in tiers:
            if _missing_price(t["avg_price"]):
                t["avg_price"] = round(ctp_avg, 4)
                filled += 1
        if filled:
            plan.avg_price_source = ("mixed" if plan.avg_price_source == "local"
                                     else plan.avg_price_source or "ctp")

    if local_vol != target_vol:
        plan.changes.append(f"手数 {local_vol} → {target_vol}（以柜台为准）")
    if len(tiers) != len(local_pos.tiers):
        plan.changes.append(
            f"层数 {len(local_pos.tiers)} → {len(tiers)}（保留层结构：差额从最新层削减/按层补齐）")
    if filled:
        plan.changes.append(f"{filled} 层均价缺失 → 用 CTP 反推成本补齐")

    plan.tiers = [{
        "volume": int(t["volume"]), "avg_price": round(float(t["avg_price"]), 4),
        "tier_id": t["tier_id"], "open_ts": t["open_ts"],
        "open_trading_day": t["open_trading_day"],
        "entry_mean": t["entry_mean"], "source": t["source"],
    } for t in tiers]
    return plan


# ====================== 账户级联合反推（多合约共享腿） ======================

def _add_hands(bucket: dict, instrument: str, direction: str, hands: int) -> None:
    slot = bucket.setdefault(instrument, {"long": 0, "short": 0})
    slot[direction] = int(slot.get(direction, 0)) + int(hands)


def _legs_with_remaining(legs: list[LegPosition],
                         remaining: dict[tuple[str, str], int]) -> list[LegPosition]:
    """按"剩余可用手数"裁剪腿（均价沿用原腿加权均价）。

    扣减后严格均价会随分批成本变化，但 CTP 只给汇总成本、无法精确还原分批，
    故均价保持不变（反推成本仍是同一批腿的加权均价，误差可接受）。
    """
    out: list[LegPosition] = []
    for leg in legs:
        vol = int(remaining.get((leg.instrument, leg.direction), 0))
        if vol <= 0:
            continue
        out.append(LegPosition(leg.instrument, leg.direction, vol,
                               leg.avg_price, leg.cost))
    return out


@dataclass
class AccountAllocation:
    """账户级联合反推结果：每个合约的组合反推 + 每条腿的占用/剩余。"""
    inferences: dict[str, ComboInference] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)          # 分配顺序（本地有持仓优先）
    used: dict[str, dict] = field(default_factory=dict)     # instrument → {long, short}（本系统组合占用）
    unused: dict[str, dict] = field(default_factory=dict)   # instrument → {long, short}（无人使用）
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "inferences": {s: i.to_dict() for s, i in self.inferences.items()},
            "order": list(self.order),
            "used": {k: dict(v) for k, v in self.used.items()},
            "unused": {k: dict(v) for k, v in self.unused.items()},
            "warnings": list(self.warnings),
        }


def allocate_legs(contracts: list[dict],
                  leg_holdings: dict[str, list[LegPosition]],
                  *, open_slugs: Iterable[str] = ()) -> AccountAllocation:
    """多合约共享腿的联合反推：按顺序全局配对，各合约只从"剩余腿手数"里取配对 min。

    为什么需要：同一腿可能同时是多个跨期组合的一边（如 a2701 是 a2611-a2701 的远月、
    也是 a2701-a2703 的近月）。逐合约独立反推时，另一组合占用的那部分会被误判成
    "单边裸腿"；全局配对把已占用手数扣掉，剩余不匹配才算真异常。

    分配顺序：**本地有持仓的合约优先**（避免新开仓合约抢走已有持仓的腿），
    其余按 slug 排序（结果确定，便于复现）。

    contracts 每项：{"slug", "near_leg", "far_leg", "multiplier"}。
    """
    remaining: dict[tuple[str, str], int] = {}
    for instrument, legs in leg_holdings.items():
        for leg in legs:
            key = (instrument, leg.direction)
            remaining[key] = remaining.get(key, 0) + int(leg.volume)

    open_set = set(open_slugs or ())
    order = sorted(contracts, key=lambda c: (c["slug"] not in open_set, c["slug"]))
    allocation = AccountAllocation(order=[c["slug"] for c in order])
    for c in order:
        slug = str(c.get("slug") or "")
        near = str(c.get("near_leg") or "")
        far = str(c.get("far_leg") or "")
        inf = infer_combo(
            _legs_with_remaining(leg_holdings.get(near, []), remaining),
            _legs_with_remaining(leg_holdings.get(far, []), remaining),
            slug=slug, multiplier=float(c.get("multiplier") or 0.0),
            suppress_imbalance_warning=True)
        allocation.inferences[slug] = inf
        for volume, near_dir, far_dir in ((inf.long_volume, "long", "short"),
                                          (inf.short_volume, "short", "long")):
            if volume <= 0:
                continue
            remaining[(near, near_dir)] = remaining.get((near, near_dir), 0) - volume
            remaining[(far, far_dir)] = remaining.get((far, far_dir), 0) - volume
            _add_hands(allocation.used, near, near_dir, volume)
            _add_hands(allocation.used, far, far_dir, volume)

    for (instrument, direction), volume in sorted(remaining.items()):
        if volume > 0:
            _add_hands(allocation.unused, instrument, direction, volume)
    for instrument, slot in sorted(allocation.unused.items()):
        parts = [f"{n} 手{'多' if d == 'long' else '空'}"
                 for d, n in (("long", slot.get("long", 0)),
                              ("short", slot.get("short", 0))) if n > 0]
        allocation.warnings.append(
            f"{instrument} 另有 {'、'.join(parts)} 未被任何已注册跨期组合使用"
            f"（可能属于手工单/其他策略/未注册合约，请核对）")
    return allocation


def leg_conservation(contracts: list[dict], positions: dict,
                     leg_holdings: dict[str, list[LegPosition]]) -> dict:
    """腿总量守恒校验：本地各合约持仓所需的腿手数，是否都能在柜台腿持仓里找到。

    为什么：共享腿若被柜台**净持仓口径抵消**（同合约多空相抵）或被其他策略/手工单
    占用，按剩余腿反推会得到"少持仓/无持仓"，直接应用校准会把本地真实持仓误清掉。
    需求 > 可用 → `ok=False`，调用方必须**拒绝应用**（只允许预览/告警）。

    positions: slug → Position（本地持仓）。
    返回 {ok, required, available, shortfalls}（均可 JSON 序列化）。
    """
    required: dict[str, dict] = {}
    for c in contracts:
        pos = positions.get(str(c.get("slug") or ""))
        if pos is None or not getattr(pos, "is_open", False) or pos.side is None:
            continue
        volume = int(pos.volume)
        side = pos.side.name
        if side == "LONG":      # 做多价差 = 买近月 + 卖远月
            _add_hands(required, str(c["near_leg"]), "long", volume)
            _add_hands(required, str(c["far_leg"]), "short", volume)
        elif side == "SHORT":   # 做空价差 = 卖近月 + 买远月
            _add_hands(required, str(c["near_leg"]), "short", volume)
            _add_hands(required, str(c["far_leg"]), "long", volume)

    available: dict[str, dict] = {}
    for instrument, legs in leg_holdings.items():
        for leg in legs:
            _add_hands(available, instrument, leg.direction, leg.volume)

    shortfalls: list[dict] = []
    for instrument, slot in sorted(required.items()):
        for direction, need in (("long", slot.get("long", 0)),
                                ("short", slot.get("short", 0))):
            if need <= 0:
                continue
            have = int(available.get(instrument, {}).get(direction, 0))
            if need > have:
                shortfalls.append({
                    "instrument": instrument, "direction": direction,
                    "required": need, "available": have,
                    "reason": (f"{instrument} {'多' if direction == 'long' else '空'}头"
                               f"本地需求 {need} 手 > 柜台可用 {have} 手：可能为净持仓口径"
                               f"抵消，或其他策略/手工单占用，禁止自动校准"),
                })
    return {"ok": not shortfalls, "required": required,
            "available": available, "shortfalls": shortfalls}
