"""报单机制（重构）：实时报单区 + 历史报单区。

- 实时区（OrderBook.live）：程序重点管理——未终态订单（挂单中/待确认/撤单中），
  负责发单、互斥、超时撤单、撤单、回报应用（状态迁移）、归档、持久化与恢复；
- 历史区（OrderHistory）：只读记录——终态订单档案（orders_hist_<交易日>.jsonl）
  + 报单变更流水（orders_events_<交易日>.jsonl，每步状态迁移一条事件）。

设计原则（对齐 BMR2）：状态是虚构的，记录是真实的——报单记录是权威，
状态由回报/指令驱动迁移，每次迁移写一条变更流水（可完整复盘）。
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Callable, Optional

from core.persistence import Store
from strategy.models import (
    ACTIVE_STATUSES,
    MARKET_CLOSE_TIMEOUT_S,
    PREPLACE_EXIT_TAG,
    Order,
    OrderStatus,
    TERMINAL_STATUSES,
)
from trading.broker import OrderRef, OrderSpec, OrderStatusInfo, Report


class OrderEventType(str, Enum):
    """报单变更流水事件类型。"""
    SUBMITTED = "SUBMITTED"        # 发单
    ACCEPTED = "ACCEPTED"          # 受理（回填 ctp_ref / order_sys_id）
    PARTTRADED = "PARTTRADED"      # 部分成交
    FILLED = "FILLED"              # 成交（全部）
    CANCEL_SENT = "CANCEL_SENT"    # 发起撤单
    CANCELLED = "CANCELLED"        # 撤单确认
    REJECTED = "REJECTED"          # 拒绝
    TIMEOUT = "TIMEOUT"            # 超时
    SUSPENDED = "SUSPENDED"        # 挂起（交易未激活/非交易时段，不撮合）
    RESUMED = "RESUMED"            # 恢复活跃
    ARCHIVED = "ARCHIVED"          # 归档（终态 → 历史区）
    RECONCILED = "RECONCILED"      # 对账修正（以 CTP 为准修正本地状态）
    MANUAL_EDIT = "MANUAL_EDIT"    # 人工编辑（实时报单/持仓）
    MANUAL_DELETE = "MANUAL_DELETE"  # 人工删除（实时报单记录/持仓层）

    @property
    def cn(self) -> str:
        return EVENT_CN.get(self.value, self.value)


EVENT_CN = {
    "SUBMITTED": "已提交",
    "ACCEPTED": "已受理",
    "PARTTRADED": "部分成交",
    "FILLED": "成交",
    "CANCEL_SENT": "撤单请求",
    "CANCELLED": "已撤",
    "REJECTED": "已拒绝",
    "TIMEOUT": "已超时",
    "SUSPENDED": "挂起",
    "RESUMED": "恢复",
    "ARCHIVED": "归档",
    "RECONCILED": "对账修正",
    "MANUAL_EDIT": "人工编辑",
    "MANUAL_DELETE": "人工删除",
}

STATUS_CN = {
    "WAITING_ACCEPT": "待确认",
    "ACTIVE": "挂单中",
    "TRADED": "已成交",
    "CANCELLED": "已撤",
    "REJECTED": "已拒绝",
    "TIMEOUT": "已超时",
    "SUSPENDED": "挂起",
}

ACTION_CN = {"open": "开仓", "add": "加仓", "close": "平仓"}
DIRECTION_CN = {"buy": "买", "sell": "卖"}
SIDE_CN = {"LONG": "多", "SHORT": "空"}   # 价差方向（策略持仓方向）

RECENT_LIMIT = 200        # 网页实时报单记录上限
EVENTS_PER_ORDER = 30     # 单笔报单内存保留的变更流水条数

# 撤单未确认兜底（实盘：撤单请求发出后迟迟收不到终态回报，如重启后旧会话单的
# 回报匹配不到）——超过间隔重发撤单，最多重发 CANCEL_RETRY_MAX 次后停手告警。
CANCEL_RETRY_INTERVAL_S = 30.0
CANCEL_RETRY_MAX = 5

# 「柜台查无此报单」核验阈值（见 reconcile_reports）：
# 撤单已重发达此次数仍未确认 + CTP 当日报单里按 order_sys_id 查不到该单
# → 判定柜台确实没有这张报单（撤单被反复拒绝，典型 err=25「找不到对应的报单」），
# 本地归档释放「同 slug 单挂单」互斥槽；未达阈值只告警，避免把仍挂的单误删成裸单。
MISSING_ORDER_VERIFY_RETRIES = 3


def new_local_id(slug: str) -> str:
    """生成稳定唯一的报单 id（uuid 片段，重启不撞号；旧 ORD 序号记录兼容读取）。"""
    return f"{slug}-{uuid.uuid4().hex[:10]}"


def order_action_of(o: dict) -> str:
    """报单意图：open / add / close（兼容两种记录格式）。

    - Order.to_dict() 格式：direction(buy/sell) + side(open/close) + target_tier(int)；
    - 旧市价单记录格式：action + side(LONG/SHORT)。
    """
    if o.get("action") and o.get("side") in ("LONG", "SHORT"):
        return str(o["action"])
    side = str(o.get("side", "open"))
    target = o.get("target_tier", -1)
    if side == "close":
        return "close"
    # 开/加仓单 side 均为 "open"，靠 target_tier 区分：>0 才是加仓，
    # 底仓开仓单 target_tier=0（目标新层索引）应判为 open（此前 >=0 误判成 add）
    if isinstance(target, int) and target > 0:
        return "add"
    return "open"


def order_direction_of(o: dict) -> str:
    """买卖方向（buy / sell）：限价单记录直接有 direction；旧市价单由 side 推断。

    旧市价格式（record_sync）的 side 是持仓方向（LONG/SHORT），买卖方向需按开/平区分：
    - 开仓 LONG = 买、开仓 SHORT = 卖；
    - 平仓 LONG = 卖组合、平仓 SHORT = 买组合（此前一律按开仓推断，平 LONG 被误显示为"买平"）。
    """
    d = o.get("direction")
    if d in ("buy", "sell"):
        return str(d)
    side = o.get("side")
    if order_action_of(o) == "close":
        return "sell" if side in ("LONG", "sell") else "buy"
    return "buy" if side in ("LONG", "buy") else "sell"


def order_side_long(o: dict) -> bool:
    """是否为做多价差（LONG）。"""
    return order_direction_of(o) == "buy"


def order_position_side_of(o: dict) -> str:
    """报单归属的持仓方向 LONG / SHORT（复盘对齐用，区别于 order_direction_of）。

    - 挂单格式（direction + side + target_tier）：
      开/加多 = 买 → LONG；开/加空 = 卖 → SHORT；
      平多 = 卖 → LONG；平空 = 买 → SHORT（平仓单方向与持仓方向相反）。
    - 旧市价格式（action + side）：side 本身即持仓方向。

    结果用于把报单记录与信号 tick（pos_side）、成交记录（side）按同一方向列对齐。
    """
    d = o.get("direction")
    if d in ("buy", "sell"):
        if order_action_of(o) == "close":
            return "SHORT" if d == "buy" else "LONG"
        return "LONG" if d == "buy" else "SHORT"
    return str(o.get("side", "") or "")


def exit_kind_of(reason: str) -> str:
    """平仓类别标记（由 reason 解析）：止盈 / 止损 / 时间止损 / 强平 / 全平 / 其他。

    仅在平仓单上使用（order_to_display 对 close 单调用）；无法识别返回 "其他"。
    顺序注意："时间止损" 含 "止损"，必须先判时间。
    """
    if not reason:
        return "其他"
    if "回归" in reason or "止盈" in reason:
        return "止盈"
    if "时间" in reason and "止损" in reason:
        return "时间止损"
    if "止损" in reason:
        return "止损"
    if "强平" in reason or "REX" in reason:
        return "强平"
    if "全平" in reason:
        return "全平"
    return "其他"


def order_to_display(o: dict, events: Optional[list[dict]] = None) -> dict:
    """报单记录 → 前端展示格式（通俗中文 + 保留技术字段 _raw）。

    兼容两种来源：Order.to_dict()（限价挂单）与旧市价单记录（action+side）。
    输出字段：
      order_id / action(+_cn) / direction_cn / volume / price /
      status(+_cn) / target_tier(+_cn) / order_time / trading_day / reason /
      exit_kind(+_cn，平仓单：止盈/止损/强平/时间止损/全平/其他) /
      events（变更流水展示） / _raw（技术字段：local_id/ctp_ref/order_sys_id/…）
    """
    action = order_action_of(o)
    status = str(o.get("status", OrderStatus.WAITING_ACCEPT.value))
    target = o.get("target_tier", -1)
    long = order_side_long(o)
    target_cn = ""
    if action == "close":
        if isinstance(target, int) and target >= 0:
            target_cn = f"第{target + 1}层"
        else:
            target_cn = "整仓"
    reason = str(o.get("reason", "") or "")
    exit_kind = exit_kind_of(reason) if action == "close" else ""
    display = {
        "order_id": str(o.get("local_id", "")),
        "action": action,
        "action_cn": ACTION_CN.get(action, action),
        "direction": "buy" if long else "sell",
        "direction_cn": "买" if long else "卖",
        "volume": int(o.get("volume", 0)),
        "price": o.get("price", 0.0),
        "status": status,
        "status_cn": STATUS_CN.get(status, status),
        "target_tier": target,
        "target_tier_cn": target_cn,
        "open_price": float(o.get("open_price", 0.0) or 0.0),   # 平仓单：对应开仓价
        "order_time": o.get("order_time", 0.0),
        "trading_day": str(o.get("trading_day", "") or ""),
        "reason": reason,
        "exit_kind": exit_kind,
        "exit_kind_cn": exit_kind,
        "events": [
            {
                "ts": e.get("ts", 0.0),
                "event": e.get("event", ""),
                "event_cn": EVENT_CN.get(str(e.get("event", "")), str(e.get("event", ""))),
                "detail": str(e.get("detail", "") or ""),
            }
            for e in (events or [])
        ],
        "_raw": {
            "local_id": str(o.get("local_id", "")),
            "slug": str(o.get("slug", "") or ""),
            "ctp_ref": str(o.get("ctp_ref", "") or ""),
            "order_sys_id": str(o.get("order_sys_id", "") or ""),
            "cum_volume": int(o.get("cum_volume", 0) or 0),
            "cum_amount": float(o.get("cum_amount", 0.0) or 0.0),
            "last_trade_price": o.get("last_trade_price", 0.0) or 0.0,
            "error_msg": str(o.get("error_msg", "") or ""),
            "cancelling": bool(o.get("cancelling", False)),
            "archived_at": o.get("archived_at"),
            "archive_reason": str(o.get("archive_reason", "") or ""),
        },
    }
    return display


class OrderHistory:
    """历史报单区：终态订单档案 + 报单变更流水（只读记录，按模式+交易日分文件）。"""

    def __init__(self, store: Store, slug: str, mode_prefix: str) -> None:
        self.store = store
        self.slug = slug
        self.prefix = mode_prefix   # "sim_" / "real_"

    def archive(self, order: Order, reason: str) -> None:
        """终态归档：orders_hist_<交易日>.jsonl（保留本地报单全字段 + 归档信息）。"""
        date = order.trading_day or time.strftime("%Y-%m-%d")
        rec = order.to_dict()
        rec["archived_at"] = time.time()
        rec["archive_reason"] = reason
        self.store.append_jsonl(self.slug, f"{self.prefix}orders_hist_{date}.jsonl", rec)

    def archive_dict(self, rec: dict, trading_day: str, reason: str = "") -> None:
        """终态归档（dict 来源：市价同步撮合记录等）。"""
        date = trading_day or time.strftime("%Y-%m-%d")
        out = dict(rec)
        out["archived_at"] = time.time()
        out["archive_reason"] = reason
        self.store.append_jsonl(self.slug, f"{self.prefix}orders_hist_{date}.jsonl", out)

    def record_event(self, local_id: str, trading_day: str, event: OrderEventType,
                     detail: str = "", ts: Optional[float] = None) -> dict:
        """写一条报单变更流水（orders_events_<交易日>.jsonl），返回事件记录。"""
        date = trading_day or time.strftime("%Y-%m-%d")
        rec = {
            "ts": ts if ts is not None else time.time(),
            "trading_day": date,
            "local_id": local_id,
            "slug": self.slug,
            "event": event.value,
            "detail": detail,
        }
        self.store.append_jsonl(self.slug, f"{self.prefix}orders_events_{date}.jsonl", rec)
        return rec

    def read_hist(self, trading_day: str) -> list[dict]:
        """读当日历史报单档案（网页回看）。"""
        return self.store.read_jsonl(self.slug, f"{self.prefix}orders_hist_{trading_day}.jsonl")

    def read_events(self, trading_day: str) -> list[dict]:
        """读当日变更流水（网页回看 / 启动恢复内存缓存）。"""
        return self.store.read_jsonl(self.slug, f"{self.prefix}orders_events_{trading_day}.jsonl")

    def find_archived(self, ctp_ref: str = "", order_sys_id: str = "") -> Optional[dict]:
        """按 CTP 引用/交易所系统号在历史档案中查找（孤儿回报幂等判定用）。

        扫描最近 7 天的 orders_hist 文件（孤儿回报可能跨交易日迟到）。"""
        if not ctp_ref and not order_sys_id:
            return None
        import datetime
        today = datetime.date.today()
        for i in range(7):
            d = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for rec in self.read_hist(d):
                if ctp_ref and rec.get("ctp_ref") == ctp_ref:
                    return rec
                if order_sys_id and rec.get("order_sys_id") == order_sys_id:
                    return rec
        return None


class OrderBook:
    """实时报单区：未终态订单集合 + 报单管理职责。

    同 slug 同时最多一张未终态自动单（互斥，防层级错位）；
    终态订单归档到 OrderHistory 并从实时区移除。
    """

    def __init__(self, *, store: Store, slug: str, mode_prefix: str,
                 broker=None, instrument: str = "",
                 on_traded: Optional[Callable[[Order], None]] = None,
                 on_cancel_confirmed: Optional[Callable[[Order, bool], None]] = None,
                 on_orphan: Optional[Callable[[Report, str], None]] = None,
                 on_rejected: Optional[Callable[[Order], None]] = None) -> None:
        self.store = store
        self.slug = slug
        self.prefix = mode_prefix
        self.broker = broker
        # CTP 合约代码（组合代码，如 "SPD a2611&a2701"）：撤单/回报归属要用；
        # 缺失时撤单指令拼不出来（实盘会静默不撤）
        self._instrument = instrument
        self.live: dict[str, Order] = {}       # local_id → 未终态订单（含 SUSPENDED）
        self.recent: list[dict] = []           # 最近报单记录（≤200 条，网页显示）
        self.history = OrderHistory(store, slug, mode_prefix)
        self.events: dict[str, list[dict]] = {}  # local_id → 变更流水（内存缓存）
        self.on_traded = on_traded             # 成交回调（引擎建层/平层）
        self.on_cancel_confirmed = on_cancel_confirmed  # 撤单确认回调（引擎风控等）
        self.on_orphan = on_orphan             # 孤儿回报回调（reason: archived/unknown）
        self.on_rejected = on_rejected         # 拒单/超时回调（引擎熔断计数）
        self._timeout_cancel_sent: set[str] = set()    # 已发起超时撤单的 local_id（防重复发）
        self._archived_keys: set[tuple[str, str]] = set()  # 本会话归档的 (ctp_ref, order_sys_id)（孤儿幂等快速判定）

    # ---------- 记录 ----------
    def _append_recent(self, rec: dict) -> None:
        self.recent.append(rec)
        if len(self.recent) > RECENT_LIMIT:
            self.recent = self.recent[-RECENT_LIMIT:]

    def _update_recent(self, local_id: str, rec: dict) -> None:
        for i, r in enumerate(self.recent):
            if r.get("local_id") == local_id:
                self.recent[i] = rec
                return
        self._append_recent(rec)

    def _record_event(self, local_id: str, trading_day: str, event: OrderEventType,
                      detail: str = "", ts: Optional[float] = None) -> None:
        rec = self.history.record_event(local_id, trading_day, event, detail, ts)
        evs = self.events.setdefault(local_id, [])
        evs.append(rec)
        if len(evs) > EVENTS_PER_ORDER:
            self.events[local_id] = evs[-EVENTS_PER_ORDER:]

    # ---------- 发单 ----------
    async def place(self, spec: OrderSpec, trading_day: str,
                    order_time: float) -> Optional[Order]:
        """发限价挂单（互斥 + 同义单幂等）。返回挂单；None = 未发（已有挂单/同义单）。"""
        if self.broker is None:
            return None
        if self.live:
            return None                        # 互斥：已有未终态挂单
        if any(o.is_active for o in self.live.values()):
            return None
        order = Order(
            local_id=new_local_id(self.slug), slug=self.slug,
            direction=spec.direction, side=spec.side,
            volume=spec.volume, price=spec.price,
            status=OrderStatus.WAITING_ACCEPT.value,   # 状态机收敛：提交后未确认
            target_tier=spec.target_tier, open_price=spec.open_price,
            order_time=order_time,
            trading_day=trading_day, reason=spec.reason or "",
            tier_id=spec.tier_id if hasattr(spec, "tier_id") else "",
            timeout_at=order_time + (spec.timeout_seconds or 0.0),
            market=bool(getattr(spec, "market", False)),
        )
        spec.local_id = order.local_id   # 回填本地 id：Broker 沿用（绑定必须正确）
        self.live[order.local_id] = order
        self._append_recent(order.to_dict())
        self._record_event(order.local_id, trading_day, OrderEventType.SUBMITTED,
                           f"发{spec.side}单 {order.volume}手 @ {order.price:.4f}")
        try:
            ref = await self.broker.send_order(spec)
        except Exception:
            self.live.pop(order.local_id, None)
            raise
        ref_broker = getattr(ref, "broker_ref", "") or getattr(ref, "ctp_ref", "")
        if ref_broker:
            order.ctp_ref = ref_broker
            self._update_recent(order.local_id, order.to_dict())
        # 状态机收敛：模拟通道即时受理（回报立即可得）→ 立即应用转 ACTIVE；
        # 实盘等 CTP 受理回报（WAITING_ACCEPT → ACTIVE，回填 ctp_ref/order_sys_id）。
        if not getattr(self.broker, "is_real", False):
            try:
                reports = await self.broker.drain_reports(self.slug)
                if reports:
                    self.apply_reports(reports)
            except Exception:
                pass
        self.persist()
        return order

    # ---------- 撤单 ----------
    async def cancel(self, local_id: str) -> bool:
        """撤指定报单（幂等）。返回是否发起了撤单。"""
        o = self.live.get(local_id)
        if o is None or o.is_terminal:
            return False
        if o.cancelling:
            return False                        # 已在撤单中
        if self.broker is None:
            o.status = OrderStatus.CANCELLED.value
            self._record_event(local_id, o.trading_day, OrderEventType.CANCELLED,
                               "本地撤单（无交易通道）")
            self.archive(o, "cancelled")
            self.persist()
            return True
        try:
            await self.broker.cancel_order(OrderRef(
                local_id=local_id, broker_ref=o.ctp_ref, slug=self.slug,
                instrument=self._instrument or self.slug, side=o.side,
                order_sys_id=o.order_sys_id))
        except Exception:
            return False                        # 指令异常 → 不置位，可重试
        o.cancelling = True
        o.cancel_started_at = time.time()
        self._record_event(local_id, o.trading_day, OrderEventType.CANCEL_SENT,
                           f"撤单 {o.volume}手 @ {o.price:.4f}")
        self._update_recent(local_id, o.to_dict())
        self.persist()
        return True

    async def cancel_all(self) -> int:
        """撤全部未终态挂单。返回发起撤单数。"""
        n = 0
        for local_id in list(self.live.keys()):
            if await self.cancel(local_id):
                n += 1
        return n

    def edit(self, local_id: str, *, price=None, volume=None,
             direction=None, side=None, status=None) -> bool:
        """人工编辑实时报单（仅实时区非终态单；对齐 BMR2 update_order）。

        编辑不重新发单：仅修正本地记录（人工修正场景，用户自负其责）。
        """
        o = self.live.get(local_id)
        if o is None or o.is_terminal:
            return False
        detail = []
        if price is not None:
            o.price = float(price)
            detail.append(f"价格→{o.price:.4f}")
        if volume is not None:
            o.volume = int(volume)
            detail.append(f"手数→{o.volume}")
        if direction in ("buy", "sell"):
            o.direction = str(direction)
            detail.append(f"方向→{o.direction}")
        if side in ("open", "close"):
            o.side = str(side)
            detail.append(f"开平→{o.side}")
        if status in (OrderStatus.WAITING_ACCEPT.value, OrderStatus.ACTIVE.value,
                      OrderStatus.TRADED.value, OrderStatus.CANCELLED.value,
                      OrderStatus.REJECTED.value, OrderStatus.TIMEOUT.value):
            o.status = str(status)
            detail.append(f"状态→{o.status}")
        if not detail:
            return False
        self._record_event(local_id, o.trading_day, OrderEventType.MANUAL_EDIT,
                           "人工编辑 " + " ".join(detail))
        self._update_recent(local_id, o.to_dict())
        self.persist()
        return True

    def remove(self, local_id: str) -> bool:
        """人工删除实时报单记录（仅实时区；对齐 BMR2 delete_order）。

        仅删除本地记录，不向柜台发撤单（人工操作，用户自负其责）；
        若订单在柜台侧仍活跃，由对账/后续回报发现。
        """
        o = self.live.get(local_id)
        if o is None:
            return False
        self._record_event(local_id, o.trading_day, OrderEventType.MANUAL_DELETE,
                           f"人工删除报单记录（{o.direction}{o.side} {o.volume}手 @ {o.price:.4f}）")
        self.live.pop(local_id, None)
        self._timeout_cancel_sent.discard(local_id)
        self.recent[:] = [r for r in self.recent if r.get("local_id") != local_id]
        self.persist()
        return True

    async def retry_stale_cancels(self, ts: float, timeout_s: float = CANCEL_RETRY_INTERVAL_S,
                                  max_per_call: int = 1) -> list[str]:
        """撤单未确认兜底：cancelling 超过 timeout_s 仍无终态 → 重发撤单（限次）。

        为什么：撤单请求发出后终态由回报驱动，而重启后旧会话单的回报可能匹配
        不到本地单（本会话映射为空）→ 单永久停在"撤单中"：网页再点撤单因
        cancelling 短路直接失败，且它占住「同 slug 单挂单」互斥槽 → 该合约不再
        挂单。重发前清 cancelling（cancel 幂等依赖它），超过 CANCEL_RETRY_MAX 次
        仍无终态则停手告警（避免无限重发刷爆柜台）。

        max_per_call 限制单次调用重发数量（心跳路径防查询/发单风暴）。
        返回本次重发撤单的 local_id 列表。
        """
        if self.broker is None or not self.live or timeout_s <= 0:
            return []
        retried: list[str] = []
        for o in list(self.live.values()):
            if len(retried) >= max_per_call:
                break
            if not o.cancelling or o.cancel_started_at <= 0:
                continue
            if ts - o.cancel_started_at < timeout_s:
                continue                        # 未超时：继续等回报
            if o.cancel_retries >= CANCEL_RETRY_MAX:
                if o.cancel_retries == CANCEL_RETRY_MAX:
                    o.cancel_retries += 1       # 只告警一次
                    self._record_event(
                        o.local_id, o.trading_day, OrderEventType.TIMEOUT,
                        f"撤单重发 {CANCEL_RETRY_MAX} 次仍未确认，停止重发（请人工核对柜台/持仓）")
                continue
            o.cancel_retries += 1
            o.cancelling = False                # 允许 cancel() 再次发起
            o.cancel_started_at = ts            # 节流：下一轮至少再等 timeout_s
            if await self.cancel(o.local_id):
                # cancel() 内用墙钟刷新发起时间；这里统一到判定用的时间轴（心跳 ts）
                o.cancel_started_at = ts
                retried.append(o.local_id)
            else:
                o.cancelling = True             # 指令未发出：恢复"撤单中"，等下一轮再试
        if retried:
            self.persist()
        return retried

    async def expire_stale(self, ts: float, timeout_s: float) -> list[Order]:
        """挂单超时自动撤单（order_timeout_s；0 = 不启用，不重挂）。

        超时仅发起撤单、不立即移除本地挂单：终态由撤单/成交回报经
        apply_reports 归档（避免撤单窗口内成交回报丢失导致本地与实盘不一致）。
        用行情时间 ts 对比（回测/模拟回放下墙钟会失真）。

        市价平仓单（止损/强平/全平）例外：用固定 MARKET_CLOSE_TIMEOUT_S 判定，
        与 order_timeout_s 无关 —— 否则配置成 0/极大值时止损单挂不上价就不再重发。
        """
        if self.broker is None or not self.live:
            return []
        expired: list[Order] = []
        for o in list(self.live.values()):
            if o.local_id in self._timeout_cancel_sent or o.order_time <= 0:
                continue
            if PREPLACE_EXIT_TAG in (o.reason or ""):
                continue        # 预挂止盈单长期有效，等目标价成交，不参与超时撤单
            limit = MARKET_CLOSE_TIMEOUT_S if getattr(o, "market", False) else timeout_s
            if limit <= 0 or ts - o.order_time < limit:
                continue
            try:
                await self.broker.cancel_order(OrderRef(
                    local_id=o.local_id, broker_ref=o.ctp_ref, slug=self.slug,
                    instrument=self._instrument or self.slug, side=o.side,
                    order_sys_id=o.order_sys_id))
            except Exception:
                continue
            self._timeout_cancel_sent.add(o.local_id)
            o.cancelling = True
            o.cancel_started_at = time.time()
            self._record_event(o.local_id, o.trading_day, OrderEventType.CANCEL_SENT,
                               f"挂单超时（{limit:.0f}s）撤单")
            expired.append(o)
        self.persist()
        return expired

    # ---------- 回报应用（状态迁移 + 归档） ----------
    def reconcile_reports(self, ctp_orders: list[OrderStatusInfo],
                          trading_day: str) -> tuple[list[Report], list[str]]:
        """CTP 当日报单查询结果 → 本地实时区对齐：返回 (需应用的回报, 告警文本)。

        为什么：重启后本会话的「CTP 引用 → 本地单」映射为空，柜台侧旧会话单的
        成交/撤单回报可能匹配不到本地单，本地会永久停在"挂单中"（实测：柜台已撤/
        已成交，本地仍 ACTIVE + cancelling）。回报匹配已按 order_sys_id 加固，
        本方法是最后一层兜底：启动/心跳时用报单查询把本地状态拉回真实。

        匹配：order_sys_id（跨会话唯一）优先，回退 ctp_ref。
        修正：只生成"与本地不同"的回报，交由 apply_reports 走完全相同的状态迁移/
        归档/建层平层路径（不在此另写一套状态机）。
        CTP 查不到本地单 → 默认只告警、不自动归档（报单查询口径可能只含未终结单，
        自动归档会把仍挂的单误删成裸单 → 后续平仓重复）；**唯一例外**是
        `_should_archive_missing` 判定的"撤单已被反复拒绝"场景：撤单重发达
        MISSING_ORDER_VERIFY_RETRIES 次仍未确认，且当日报单里按 order_sys_id/ref
        确实查不到 → 证据充分，生成 CANCELLED 回报本地归档，释放挂单槽
        （否则该单永久停在"挂单中 + 撤单中"，该合约再也不挂新的止盈单）。
        """
        reports: list[Report] = []
        warnings: list[str] = []
        by_sysid: dict[str, OrderStatusInfo] = {}
        by_ref: dict[str, OrderStatusInfo] = {}
        for c in ctp_orders:
            sid = str(c.order_sys_id or "").strip()
            if sid:
                by_sysid[sid] = c
            if c.broker_ref:
                by_ref[str(c.broker_ref)] = c
        matched: set[int] = set()
        for o in list(self.live.values()):
            sid = str(o.order_sys_id or "").strip()
            c = by_sysid.get(sid) if sid else None
            if c is None and not sid and o.ctp_ref:
                # 只有本地单**没有** order_sys_id 时才用会话内 order_ref 回退：
                # ref 每个会话从 2 重新编号，跨会话必然撞号 → 会把别的单认成自己的，
                # 也会让"柜台查无此单"永远判不出来（本地残留清理不掉）。
                c = by_ref.get(str(o.ctp_ref))
            if c is None and not sid and not o.ctp_ref:
                # 本地单完全没有 CTP 标识（发单后未收到任何回报就重启）→ 无法按 ID 匹配。
                # 退一步在未匹配的未终结报单里找「同合约 + 同开平 + 同手数」的唯一候选；
                # 候选不唯一则告警不动（宁可人工核对，也不把别家的单认成自己的）。
                cands = [x for x in ctp_orders
                         if id(x) not in matched
                         and str(x.status or "") == OrderStatus.ACTIVE.value
                         and str(x.side or "") == str(o.side or "")
                         and int(x.volume or 0) == int(o.volume or 0)]
                if len(cands) == 1:
                    c = cands[0]
                    warnings.append(
                        f"本地报单 {o.local_id} 无 CTP 标识，按「同合约/同开平/同手数」"
                        f"唯一候选对齐（ref={c.broker_ref} sysid={c.order_sys_id}）")
            if c is None and self._should_archive_missing(o, sid):
                # 核验通过：柜台确实没有这张报单 → 归档（CANCELLED 终态）释放
                # 「同 slug 单挂单」槽，下一拍引擎可按持仓重挂止盈单。
                why = ("撤单已重发 "
                       f"{o.cancel_retries} 次仍未确认"
                       if (o.cancelling
                           and o.cancel_retries >= MISSING_ORDER_VERIFY_RETRIES)
                       else "预挂止盈单在柜台当日报单中已不存在")
                warnings.append(
                    f"本地报单 {o.local_id}（{o.direction}{o.side} {o.volume}手 @ {o.price:.2f}）"
                    f"按 sysid={sid} 在 CTP 当日报单中查不到，且{why}"
                    f" → 判定柜台已无此报单，本地归档（释放挂单槽）")
                reports.append(Report(
                    local_id=o.local_id, status=OrderStatus.CANCELLED.value,
                    slug=self.slug,
                    error_msg=(f"柜台查无此报单（sysid={sid}，{why}），对账归档"),
                    ctp_ref=str(o.ctp_ref or ""), order_sys_id=sid, ts=time.time()))
                continue
            if c is None:
                warnings.append(
                    f"本地报单 {o.local_id}（{o.direction}{o.side} {o.volume}手 @ {o.price:.2f}）"
                    f"在 CTP 当日报单中查不到，未自动修改，请人工核对")
                continue
            matched.add(id(c))
            status = str(c.status or "")
            ref = str(c.broker_ref or o.ctp_ref or "")
            csid = str(c.order_sys_id or "").strip() or sid
            if status == OrderStatus.TRADED.value:
                cum = int(c.cum_volume or 0) or int(c.volume or 0) or int(o.volume or 0)
                price = float(c.price or 0.0) or float(o.price or 0.0)
                reports.append(Report(
                    local_id=o.local_id, status=status, slug=self.slug,
                    cum_volume=cum, cum_amount=round(price * cum, 4),
                    trade_price=price, trade_volume=cum,
                    error_msg="对账修正（价取报单价，真实成交价以 CTP 成交明细为准）",
                    ctp_ref=ref, order_sys_id=csid, ts=time.time()))
            elif status in (OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value,
                            OrderStatus.TIMEOUT.value):
                reports.append(Report(
                    local_id=o.local_id, status=status, slug=self.slug,
                    error_msg="对账修正：CTP 侧该报单已终结",
                    ctp_ref=ref, order_sys_id=csid, ts=time.time()))
            elif status == OrderStatus.ACTIVE.value:
                if (o.status != OrderStatus.ACTIVE.value or str(o.ctp_ref or "") != ref
                        or sid != csid):
                    reports.append(Report(
                        local_id=o.local_id, status=status, slug=self.slug,
                        ctp_ref=ref, order_sys_id=csid, ts=time.time()))
            # 其它状态（WAITING_ACCEPT 等）：无需修正
        for c in ctp_orders:
            if id(c) in matched:
                continue
            if str(c.status or "") in (OrderStatus.TRADED.value, OrderStatus.CANCELLED.value,
                                       OrderStatus.REJECTED.value, OrderStatus.TIMEOUT.value):
                continue            # 已终结的历史单：本地无实时记录属正常
            warnings.append(
                f"CTP 报单 ref={c.broker_ref} sysid={c.order_sys_id} "
                f"（{c.direction}{c.side} {c.volume}手 @ {c.price:.2f}）本地无对应实时报单"
                f"（可能是其他策略/人工单）")
        return reports, warnings

    def _should_archive_missing(self, o: Order, order_sys_id: str = "") -> bool:
        """CTP 查无此单时，是否可判定"柜台已无该报单"并本地归档。

        **前提：order_sys_id（交易所报单编号）非空**。只有它跨会话唯一；
        会话内 order_ref 每会话从 2 重新编号，按 ref 判"查无此单"会撞号 → 可能把
        别人的单误判成自己的、或把自己的单误判成不存在，因此没有 sysid 时一律
        只告警、不动账本（宁可人工核对，也不丢记录）。

        满足任一即可归档：
        1. 撤单被反复拒绝：本地单处于「撤单中」且撤单已重发 ≥
           MISSING_ORDER_VERIFY_RETRIES 次（实盘典型 err=25「找不到对应的报单」）；
        2. 预挂止盈单：无成交风险、只占「同 slug 单挂单」槽；当日报单查询口径
           包含终态单（ALLTRADED/CANCELLED 均在内），按 sysid 查无即柜台已没有它。
        """
        sid = str(order_sys_id or o.order_sys_id or "").strip()
        if not sid:
            return False
        if o.cancelling and o.cancel_retries >= MISSING_ORDER_VERIFY_RETRIES:
            return True
        return PREPLACE_EXIT_TAG in (o.reason or "")

    def apply_reports(self, reports: list[Report]) -> int:
        """应用一批回报：状态迁移 + 变更流水；终态归档；成交触发回调。

        回报匹配：local_id → ctp_ref（会话内）→ order_sys_id（跨会话）兜底。
        返回处理的回报条数（孤儿回报不计入，由调用方/对账处理）。
        """
        handled = 0
        for r in reports:
            o = self._match(r)
            if o is None:
                # 孤儿回报：先查会话内归档索引，再查历史区（已终态归档 → 幂等忽略），
                # 完全未知 → 告警回调。不再静默丢弃：每条回报都有归宿，可排查"回报丢了/重复报"。
                if self.on_orphan is not None:
                    reason = "unknown"
                    if r.ctp_ref or r.order_sys_id:
                        if ((r.ctp_ref, r.order_sys_id) in self._archived_keys
                                or self.history.find_archived(r.ctp_ref, r.order_sys_id)):
                            reason = "archived"
                    self.on_orphan(r, reason)
                continue
            handled += 1
            if r.status == "ACTIVE":
                changed = False
                if o.status != OrderStatus.ACTIVE.value:
                    o.status = OrderStatus.ACTIVE.value   # WAITING_ACCEPT → ACTIVE
                    changed = True
                if r.ctp_ref and o.ctp_ref != r.ctp_ref:
                    o.ctp_ref = r.ctp_ref
                    changed = True
                if r.order_sys_id and o.order_sys_id != r.order_sys_id:
                    o.order_sys_id = r.order_sys_id
                    changed = True
                if changed:
                    self._record_event(o.local_id, o.trading_day, OrderEventType.ACCEPTED,
                                       f"受理 ref={r.ctp_ref} sysid={r.order_sys_id}")
                    self._update_recent(o.local_id, o.to_dict())
                continue                        # 受理回报：挂单保持 ACTIVE
            if r.status == "TRADED":
                if r.ctp_ref:
                    o.ctp_ref = r.ctp_ref
                if r.order_sys_id:
                    o.order_sys_id = r.order_sys_id
                fill_vol = r.cum_volume if r.cum_volume > 0 else o.volume
                fill_price = (r.trade_price if r.trade_price
                              else (r.cum_amount / r.cum_volume
                                    if r.cum_volume > 0 and r.cum_amount > 0
                                    else o.price))
                o.status = r.status
                o.cum_volume = fill_vol
                o.cum_amount = (r.cum_amount if r.cum_amount
                                else fill_price * fill_vol)
                o.last_trade_price = fill_price
                self._record_event(o.local_id, o.trading_day, OrderEventType.FILLED,
                                   f"成交 {fill_vol}手 @ {fill_price:.4f}")
                if self.on_traded is not None:
                    self.on_traded(o)           # 引擎：建层/平层 + 风控复位
                self.archive(o, "traded")
            elif r.status in ("CANCELLED", "REJECTED", "TIMEOUT"):
                # 真正的终态：撤/拒/超时 → 状态迁移 + 归档
                was_cancelling = o.cancelling
                o.status = r.status
                o.cancelling = False
                o.cancel_started_at = 0.0
                event = {
                    "CANCELLED": OrderEventType.CANCELLED,
                    "REJECTED": OrderEventType.REJECTED,
                    "TIMEOUT": OrderEventType.TIMEOUT,
                }[r.status]
                detail = f"挂单{r.status}"
                if r.error_msg:
                    detail += f" {r.error_msg}"
                self._record_event(o.local_id, o.trading_day, event, detail)
                if event is OrderEventType.REJECTED and self.on_rejected is not None:
                    self.on_rejected(o)     # 拒单/超时 → 熔断计数（防拒单-重发死循环）
                if event is OrderEventType.CANCELLED and self.on_cancel_confirmed is not None:
                    self.on_cancel_confirmed(o, was_cancelling)
                self.archive(o, r.status.lower())
            else:
                # WAITING_ACCEPT / 未知状态回报：非终态，不改变订单状态、不归档。
                # （实盘通道未知状态被默认映射后，若按终态处理会误杀仍挂单中的订单，
                #   导致实时报单区被清空——见 cpp_broker._map_status 未知映射）
                continue
        self.persist()
        return handled

    def _match(self, r: Report) -> Optional[Order]:
        """回报 → 实时区订单（local_id → ctp_ref → order_sys_id 兜底）。"""
        o = self.live.get(r.local_id)
        if o is None and r.ctp_ref:
            o = next((x for x in self.live.values() if x.ctp_ref == r.ctp_ref), None)
        if o is None and r.order_sys_id:
            o = next((x for x in self.live.values() if x.order_sys_id == r.order_sys_id), None)
        return o

    # ---------- 终态归档 ----------
    def archive(self, o: Order, reason: str) -> None:
        """终态挂单：更新报单记录、归档历史、写流水、从实时区移除。"""
        rec = o.to_dict()
        self._update_recent(o.local_id, rec)
        self.history.archive(o, reason)
        self._record_event(o.local_id, o.trading_day, OrderEventType.ARCHIVED,
                           f"归档（{reason}）")
        self.live.pop(o.local_id, None)
        self._timeout_cancel_sent.discard(o.local_id)
        if o.ctp_ref or o.order_sys_id:
            self._archived_keys.add((o.ctp_ref, o.order_sys_id))
        self.persist()

    # ---------- 市价同步撮合记录（无 broker 模式：报单即成交） ----------
    def record_sync(self, action: str, side, volume: int, price: float,
                    ts: float, trading_day: str, reason: str = "",
                    open_price: float = 0.0) -> None:
        """生成一条市价单报单记录（status=TRADED，无挂单生命周期），直接归档。"""
        if volume <= 0:
            return                              # 防御：0 手不产生报单记录
        side_name = side.name if hasattr(side, "name") else str(side)
        order = {
            "local_id": new_local_id(self.slug),
            "slug": self.slug,
            "action": action,                   # open / add / close
            "side": side_name,
            "volume": volume,
            "price": round(price, 4),
            "status": OrderStatus.TRADED.value,
            "open_price": round(open_price, 4),   # 平仓单：对应目标层建仓价
            "order_time": ts,
            "trading_day": trading_day,
            "reason": reason or "",
        }
        self._append_recent(order)
        self.history.archive_dict(order, trading_day, "sync")
        self._record_event(order["local_id"], trading_day, OrderEventType.SUBMITTED,
                           f"市价{action}单 {volume}手 @ {round(price, 4):.4f}")
        self._record_event(order["local_id"], trading_day, OrderEventType.FILLED,
                           f"成交 {volume}手 @ {round(price, 4):.4f}", ts=ts)

    # ---------- 持久化 / 恢复 ----------
    def persist(self) -> None:
        """实时区报单落盘（orders_live.jsonl），重启恢复。"""
        self.store.write_jsonl(self.slug, f"{self.prefix}orders_live.jsonl",
                               [o.to_dict() for o in self.live.values()])

    def set_mode_prefix(self, mode_prefix: str) -> None:
        """模式切换后更新文件前缀（sim_/real_），历史区同步。"""
        self.prefix = mode_prefix
        self.history.prefix = mode_prefix

    def set_instrument(self, instrument: str) -> None:
        """更新 CTP 合约代码（配置热更新后调用；撤单/回报归属依赖它）。"""
        self._instrument = instrument or ""

    def load(self, trading_day: str) -> None:
        """启动/模式切换恢复：实时区（全部未终态单）+ 当日报单记录 + 变更流水。

        无前缀旧文件（历史迁移数据，均为模拟时代产生）仅 simulate 模式回退；
        real 不回退，避免读到模拟数据。"""
        # 恢复未终态单：CTP 报单只在一个交易日内存活，旧交易日的单在柜台侧日终已
        # 作废 → 不恢复进实时区（否则幽灵单常驻并因互斥阻塞新挂单），按跨日失效归档。
        if self.broker is not None:
            for d in self._read_jsonl(f"{self.prefix}orders_live.jsonl"):
                try:
                    o = Order.from_dict(d)
                except (KeyError, TypeError, ValueError):
                    continue
                if not o.is_active:
                    continue
                if trading_day and o.trading_day and o.trading_day < trading_day:
                    o.status = OrderStatus.CANCELLED.value   # 柜台日终作废，口径同「已撤」
                    o.cancelling = False
                    o.cancel_started_at = 0.0
                    self.archive(o, "stale")
                    continue
                self.live[o.local_id] = o
                self._append_recent(o.to_dict())
                self._register_restored(o)
        # 当日报单记录（网页显示恢复）
        orders = self._read_jsonl(f"{self.prefix}orders_hist_{trading_day}.jsonl")
        if orders:
            merged = (self.recent + orders)[-RECENT_LIMIT:]
            self.recent[:] = merged
        # 当日变更流水（内存缓存恢复）
        for e in self._read_jsonl(f"{self.prefix}orders_events_{trading_day}.jsonl"):
            lid = str(e.get("local_id", ""))
            if not lid:
                continue
            evs = self.events.setdefault(lid, [])
            evs.append(e)
        for lid in self.events:
            self.events[lid] = self.events[lid][-EVENTS_PER_ORDER:]

    def _register_restored(self, o: Order) -> None:
        """把重启恢复的实时单登记给 Broker（实盘：回报按 order_sys_id 跨会话匹配）。

        不登记则重启后柜台侧旧会话单的成交/撤单回报匹配不到本地单而被丢弃：
        本地永久停在"挂单中"，既不建层/平层，又占住「同 slug 单挂单」互斥槽。
        登记失败不阻断恢复（模拟通道该方法为默认空实现）。
        """
        reg = getattr(self.broker, "register_restored_order", None)
        if reg is None:
            return
        try:
            reg(OrderRef(local_id=o.local_id, broker_ref=o.ctp_ref, slug=self.slug,
                         instrument=self._instrument, side=o.side,
                         order_sys_id=o.order_sys_id))
        except Exception:  # noqa: BLE001 - 登记失败不影响恢复与后续回报兜底
            pass

    def _read_jsonl(self, filename: str) -> list[dict]:
        """按模式前缀读取；simulate 模式回退无前缀旧文件。"""
        recs = self.store.read_jsonl(self.slug, filename)
        if recs or not filename.startswith(self.prefix):
            return recs
        if self.prefix != "sim_":
            return []
        return self.store.read_jsonl(self.slug, filename[len(self.prefix):])

    def events_of(self, local_id: str) -> list[dict]:
        """单笔报单的变更流水（内存缓存）。"""
        return list(self.events.get(local_id, []))

    def reset(self) -> None:
        """清空内存（模式切换重载前调用；原地清空保持 engine 视图引用有效）。"""
        self.live.clear()
        self.recent.clear()
        self.events.clear()
        self._timeout_cancel_sent.clear()
        self._archived_keys.clear()
