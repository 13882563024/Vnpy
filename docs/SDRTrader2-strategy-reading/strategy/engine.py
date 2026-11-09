"""策略引擎（规划方案 §6）：每 slug 一个，事件入口。

- on_tick：更新统计窗口 → 构造 Signal → 过滤 → 状态机 → 执行动作；
- 成交执行：回测模式为同步撮合（市价立即成交，按两腿对手价）；
  实时模式复用同一决策逻辑，经 Broker 接口异步执行（M3/M5）。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

from data_loader.reader import SpreadTick
from market.kline import KlineAggregator
from market.session_profile import is_in_session_at, session_profile_from_config
from strategy.calibration import aggregate_legs, infer_combo, plan_calibration
from strategy.config import StrategyConfig
from strategy.exit_rules import ExitRules
from strategy.filters import FilterChain
from strategy.models import (MARKET_CLOSE_TIMEOUT_S, PREPLACE_EXIT_TAG, Action,
                             ActionType, Order, OrderStatus, Position, Side,
                             Signal, Tier)
from strategy.fees import UNIT_FIXED, FeeSpec
from strategy.order_book import CANCEL_RETRY_INTERVAL_S, OrderBook
from strategy.stat_window import StatWindow
from strategy.state_machine import DecisionContext, StateMachine
from trading.broker import OrderSpec


def spread_entry_price(tick: SpreadTick, side: Side,
                       slippage_points: float = 0.0) -> float:
    """市价单开仓的价差成交价（两腿对手价，推荐文档 §3）。

    做多价差（买 near / 卖 far）：near.ask - far.bid
    做空价差（卖 near / 买 far）：near.bid - far.ask
    滑点使开仓价劣化（做多更高、做空更低）：
      entry_slipped = entry + side.sign × slippage_points
    """
    base = (tick.near.ask - tick.far.bid) if side is Side.LONG else (tick.near.bid - tick.far.ask)
    return base + side.sign * slippage_points


def _nan_none(v, digits: int = 6):
    """NaN → None（落盘 JSON 用：NaN 不是标准 JSON，读取方/表格易踩坑）。"""
    if v is None:
        return None
    try:
        if v != v:
            return None
    except TypeError:
        return None
    return round(v, digits)


def spread_exit_price(tick: SpreadTick, side: Side,
                      slippage_points: float = 0.0) -> float:
    """市价单平仓的价差成交价（反向腿）。

    平 LONG（卖 near / 买 far）：near.bid - far.ask
    平 SHORT（买 near / 卖 far）：near.ask - far.bid
    滑点使平仓价劣化（做多更低、做空更高）：
      exit_slipped = exit - side.sign × slippage_points
    """
    base = (tick.near.bid - tick.far.ask) if side is Side.LONG else (tick.near.ask - tick.far.bid)
    return base - side.sign * slippage_points


@dataclass
class Trade:
    """一笔完整交易（开→平）。"""
    side: Side
    volume: int = 1
    entry_price: float = 0.0
    exit_price: float = 0.0
    open_ts: float = 0.0
    close_ts: float = 0.0
    open_trading_day: str = ""
    close_trading_day: str = ""
    gross_pnl: float = 0.0
    fee: float = 0.0
    exit_reason: str = ""

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fee


class StrategyEngine:
    # 实盘报单对账节流（秒）：心跳 1s 一次，避免对 CTP 查询形成风暴
    ORDER_RECONCILE_INTERVAL_S = 10.0

    def __init__(self, config: StrategyConfig, fee_per_lot: float = 2.0,
                 slippage_ticks: float = 0.0, store=None,
                 auto_trading: bool = False, trading_day: str = "",
                 broker=None, margin_check=None,
                 fee_spec: Optional[FeeSpec] = None) -> None:
        self.cfg = config
        self.fee_per_lot = fee_per_lot
        # 费率规格：调用方未提供时退化为固定 fee_per_lot（4 边同价，保持旧口径）
        self.fee_spec = fee_spec or FeeSpec(unit=UNIT_FIXED, open=fee_per_lot,
                                            close=fee_per_lot, today=fee_per_lot)
        self.slippage_points = slippage_ticks * config.tick_size   # 价差滑点（点数）
        self.broker = broker                       # 可选：挂单撮合通道（无则同步成交）
        self.margin_check = margin_check           # 可选：账户级保证金上限检查（web 层注入）
        self._build_strategy()
        self.pos: Position = Position()
        self.trades: list[Trade] = []
        self._momentum_ts: list[tuple[float, float]] = []   # 最近 momentum 窗口内 spread 序列
        self.actions_log: list[dict] = []
        self.spread_hist: list[tuple[float, float, float]] = []  # (ts, spread, mean)，图表用
        self.klines: list[dict] = []             # 闭合 K 线历史（≤500 根，落盘 klines_cache.json）
        self.decision_log: list[dict] = []                 # 决策摘要（前端日志/警告）
        # ---- 报单管理（重构：实时报单区 OrderBook + 历史报单区 OrderHistory） ----
        self.ob = OrderBook(
            store=store, slug=config.slug, mode_prefix=self._mode_prefix(),
            broker=broker,
            instrument=getattr(config, "instrument", "") or "",
            on_traded=self._handle_order_traded,       # 成交 → 建层/平层 + 风控复位
            on_cancel_confirmed=self._handle_cancel_confirmed,
            on_orphan=self._handle_orphan_report,      # 孤儿回报 → 幂等/告警
            on_rejected=self._handle_order_rejected,   # 拒单/超时回报 → 熔断计数
        )
        self.orders = self.ob.recent                    # 兼容视图：报单记录（网页显示，含挂单）
        self._live_orders = self.ob.live                # 兼容视图：挂单中订单 local_id → Order
        self._last_tick: Optional[SpreadTick] = None       # 最近一条 tick（心跳兜底用）
        self._last_trading_day = trading_day or ""
        self._cancel_requested = False                     # 撤全部指令标记
        self._preplace_pending = False                     # 开/加仓成交 → 待预挂平仓单
        self._order_reconciled_day = ""                    # 已完成 CTP 报单对账的交易日
        self._last_order_reconcile_ts = 0.0                # 上次报单对账时刻（节流）

        # ---- 实时门禁（M5） ----
        self.trading_active = auto_trading     # 交易开关（默认关；回测/模拟开启）
        self.close_only = False                # 只平不开（全平后保持，手动恢复；BMR2 语义）
        self.reconciling = False               # 对账期间只平不开
        self._close_requested = False          # clear_position 挂起（下个 tick 全平）
        self._store = store                    # 持久化（成交历史/熔断）
        self.risk = None
        if store is not None:
            from strategy.risk import RiskManager
            self.risk = RiskManager(config.slug, store)
        # 启动时恢复历史 K 线缓存（对齐 BMR2 _load_state：补充 K 线/闭合
        # K 线跨重启保留，注入统计窗口聚合器，统计与图表立即生效）
        self._load_klines_cache()
        # 启动时恢复本地状态（对齐需求：模式区分，重启自动加载）：
        # 实时持仓 + 当日成交/报单（网页显示恢复，跨重启不丢失）
        self._load_persisted()

    # ---------- 策略对象构建 / 热更新 ----------
    def _build_strategy(self) -> None:
        """按当前 self.cfg 重建统计窗口/过滤器/平仓规则/状态机。

        统计口径（最新机制）：mean = 最近 N 分钟 spread 均值、sd = 最近 M 分钟
        spread 标准差，直接用最近几分钟内采集到的 tick 数据集计算——不做 m1 K 线
        聚合（StatWindow 按秒滚动窗口，增量维护 sum/sum_sq）。决策每 tick 触发。

        K 线聚合器（kline_agg）与统计解耦：仅用于图表显示、klines_cache 落盘与
        补充 K 线导入（对齐 BMR2 聚合系统），不参与 mean/sd 计算。
        """
        cfg = self.cfg
        self.window = StatWindow(
            n_seconds=max(1, cfg.signal.n_minutes) * 60,
            m_seconds=max(1, cfg.signal.m_minutes) * 60,
        )
        self.kline_agg = KlineAggregator(period_minutes=1)
        # 热更新重建后：把内存中已积累的闭合 K 线重新注入（图表历史不丢）
        if getattr(self, "klines", None):
            agg = self.kline_agg
            merged = {k["ts"]: k for k in agg.closed}
            for b in self.klines:
                merged[b["ts"]] = b
            agg.closed = sorted(merged.values(), key=lambda k: k["ts"])[-agg.max_history:]
        self.filters = FilterChain(cfg.filters, cfg.tick_size)
        # 合约级交易时段模板（session 配置优先，缺省按品种前缀推断；热更新生效）
        import dataclasses
        self.session_profile = session_profile_from_config(
            dataclasses.asdict(cfg.session), cfg.prefix)
        # 回归平仓阈值 min_exit_pnl 由配置提供（网页/注册表可改，不再硬编码覆盖）
        self.exit_rules = ExitRules(
            stop_loss=cfg.exit.stop_loss,
            min_exit_pnl=cfg.exit.min_exit_pnl,
            max_hold_days=cfg.exit.max_hold_days,
            multiplier=cfg.multiplier,
            max_positions=cfg.layering.max_positions,
            filters=self.filters,
            fee_fn=self._tier_fee,      # 回归止盈按「净利」（毛利 − 手续费）判定
        )
        self.sm = StateMachine(
            signal=cfg.signal,
            layering=cfg.layering,
            filters=self.filters,
            exit_rules=self.exit_rules,
        )

    def update_config(self, new_cfg: StrategyConfig) -> None:
        """热更新策略配置（重建统计窗口/过滤器/平仓规则/状态机）。

        保留持仓/成交/图表历史；统计窗口清空重建（新参数从零积累，与
        BMR2 的 kline_period 热更新语义一致）。
        """
        self.cfg = new_cfg
        self.slippage_points = 0.0   # 滑点由外部（回测/Web）管理，此处不覆盖
        self.ob.set_instrument(getattr(new_cfg, "instrument", "") or "")
        self._build_strategy()

    # ---------- 控制指令（ControlEvent） ----------
    async def on_control(self, command: str) -> str:
        """处理控制指令，返回状态描述。"""
        if command == "start_trading":
            self.trading_active = True
            self.close_only = False            # 重新启动交易 = 恢复自动开仓
            self._ensure_trade_channel()       # 实盘：检查/重拉 C++ 报单引擎 exe
            # 通道就绪后按 CTP 当日报单对齐本地实时报单：重启恢复的旧会话单回报
            # 匹配不到本地单（本会话 ref 映射为空），需以柜台状态为准修正
            # （未登录时由心跳兜底重试）
            await self._reconcile_orders_if_due(time.time())
            # 启动交易即按当前交易日清理旧交易日实时单（CTP 报单只在一个交易日内存活）：
            # 未启动采集时心跳/tick 不走，否则重启恢复的旧单会一直留在实时报单区
            # 并因互斥永久阻塞新挂单
            await self._clear_previous_day_orders(self._last_trading_day, time.time())
            return "trading_active"
        if command == "stop_trading":
            self.trading_active = False
            return "trading_stopped"
        if command == "clear_position":
            self._close_requested = True
            self.close_only = True             # 全平后保持只平不开（防自动重新开仓）
            return "close_requested"
        if command == "cancel_orders":
            # 撤全部挂单：实盘/模拟统一走 broker 撤单（不重挂）
            if self.broker is not None:
                await self._cancel_live_orders()
            self._cancel_requested = True
            return "cancel_requested"
        if command == "reset_risk":
            if self.risk:
                self.risk.reset()
            return "risk_reset"
        return f"unknown:{command}"

    def _ensure_trade_channel(self) -> None:
        """启动交易时确保交易通道存活（实盘 = C++ 报单引擎 exe 子进程）。

        exe 未运行/已崩溃时由 Broker 按冷却自动重拉（CppBroker.ensure_running）；
        模拟通道为无操作。通道异常不阻断"启动交易"本身——发单失败仍走既有
        熔断链路计数，崩溃原因由 Broker 落盘 flow/order/cpp_engine.log。
        """
        ensure = getattr(self.broker, "ensure_running", None)
        if ensure is None:
            return
        try:
            ensure("启动交易")
        except Exception:  # noqa: BLE001 - 通道重拉失败不影响控制指令
            pass

    def consume_cancel_request(self) -> bool:
        """读取并清除撤单指令（实时引擎消费用）。"""
        v = self._cancel_requested
        self._cancel_requested = False
        return v

    def set_reconciling(self, flag: bool) -> None:
        """对账期间只平不开（由对账流程调用）。"""
        self.reconciling = flag

    @property
    def config_close_only(self) -> bool:
        """合约配置"只平仓"开关（持久化，重启/热更新后仍生效）。

        开启后引擎不执行任何自动开仓/加仓动作，只允许平仓；人工持仓维护
        （add_tier/edit_tier）不受此开关限制。
        """
        return bool(getattr(self.cfg, "close_only", False))

    def in_session(self, ts: float) -> bool:
        """当前行情时间是否处于该合约交易时段（合约 session 配置优先）。"""
        return is_in_session_at(self.session_profile, ts)

    def risk_state(self) -> dict:
        return self.risk.state_dict() if self.risk else {}

    async def _clear_previous_day_orders(self, trading_day: str,
                                         ts: float = 0.0) -> bool:
        """新交易日清理旧交易日实时报单：直接归档，再按当前持仓重挂预挂止盈。

        CTP 报单只在一个交易日内存活（日终由柜台作废）：跨交易日后本地未终态单在
        CTP 侧必然已终结。查询拿不到（ReqQryOrder 只返回当日委托）、撤单也只会得到
        "报单不存在"错误且不产生回报 → 等确认会让幽灵单常驻实时报单区，并因
        「同 slug 单挂单」互斥永久阻塞新挂单。故旧交易日的单一律直接归档移除，
        不查询、不发撤单。

        返回 False 分支已不再出现（旧实现为"等撤单/查询确认"）；保留返回值语义以
        兼容调用方（True = 本拍可继续撮合/决策）。
        """
        if not trading_day or self.broker is None:
            return True
        if not any(o.trading_day and o.trading_day < trading_day
                   for o in self._live_orders.values()):
            return True
        # 先落账已有回报：清理前刚好到达的终态单按其真实终态归档，不被跨日覆盖
        await self._apply_reports()
        stale = [o for o in list(self._live_orders.values())
                 if o.trading_day and o.trading_day < trading_day]
        for o in stale:
            detail = (f"跨日清理旧交易日报单 {o.trading_day} "
                      f"{o.direction}{o.side} {o.volume}手 @ {o.price:.2f}")
            if o.cum_volume:
                detail += f"（已成交 {o.cum_volume} 手，请核对持仓）"
            o.status = OrderStatus.CANCELLED.value   # 柜台日终作废，口径同「已撤」
            o.cancelling = False
            o.cancel_started_at = 0.0
            self.ob.archive(o, "stale")
            self._log_decision(ts or time.time(), o.trading_day, o.price, "撤", detail)
        # 心跳路径不经过 on_tick，最近交易日仍是旧值：同步后再决定是否重挂，
        # 否则重挂单会归属旧交易日，导致下一拍又被当作旧单撤销（撤-挂循环）。
        if trading_day > (self._last_trading_day or ""):
            self._last_trading_day = trading_day
        # 旧单已清理：有持仓则按当前持仓重新预挂止盈
        # （仅交易时段且交易已激活，避免非交易时段挂单被拒）
        if self.pos.is_open and self.trading_active and (not ts or self.in_session(ts)):
            self._preplace_pending = True
        return True

    # ---------- 心跳兜底（D6） ----------
    async def on_heartbeat(self, ts: float, trading_day: str) -> list[Action]:
        """无行情 tick 时周期兜底：用最近价差检查平仓条件（时间止损/回归/强平）。

        开仓需要新行情，不做；仅当持仓存在且满足平仓条件时动作。
        """
        # K 线断流兜底：bucket 窗口已结束仍无新 tick → 强制闭合落盘（对齐 BMR2）
        self._flush_stale_kline()
        # 挂单回报兜底（成交/撤单状态应用）——不依赖交易时段：
        # 非交易时段/行情断流期间回报照常落账，避免实盘轮询回报积压迟到
        if not await self._clear_previous_day_orders(trading_day, ts):
            return []
        await self._apply_reports()
        # 实盘报单对账（引擎登录后每交易日一次 + 存在撤单中的单时重查）：
        # 重启恢复的旧会话单回报匹配不到本地单，需用 CTP 当日报单修正本地状态
        await self._reconcile_orders_if_due(ts)
        # 撤单未确认兜底：重发撤单（限次），避免本地永久停在"撤单中"占住挂单槽
        await self._retry_stale_cancels(ts)
        # 交易开关门：未激活时只落账回报，不产生任何自动单（对齐 on_tick 的开关门；
        # 此前缺失导致"未开启交易仍按回归/时间/强平判据发平仓单"→ 连续拒单熔断）
        if not self.trading_active:
            return []
        # 非交易时段：不执行平仓兜底（避免收盘后/周末用旧行情误平仓）
        if not self.in_session(ts):
            return []
        if not self.pos.is_open:
            return []
        if not self._last_tick:
            return []
        # 熔断门：全部停手
        if self.risk is not None and self.risk.open:
            return []
        tick = self._last_tick
        sig = Signal(
            ts=ts,
            trading_day=trading_day or self._last_trading_day,
            spread=tick.spread,
            mean=self.window.mean() if self.window.mean() is not None else float("nan"),
            sd=self.window.sd() if self.window.sd() is not None else float("nan"),
            near_volume=tick.near.volume,
            far_volume=tick.far.volume,
            near_inst=tick.near.inst,
            far_inst=tick.far.inst,
            exit_price=spread_exit_price(tick, self.pos.side, self.slippage_points),
        )
        fres = self.filters.entry_allowed(tick, sig.trading_day, None)
        ctx = DecisionContext(self.pos, sig, fres, None)
        actions = [a for a in self.sm.evaluate(ctx) if a.type is ActionType.CLOSE]
        # 计算日志落盘（复盘用，对齐 BMR2）
        self._record_decision(ts, sig.trading_day, tick, sig, fres, actions)
        for a in actions:
            await self._execute(a, tick, sig.trading_day)
        # 预挂止盈每拍兜底（同 on_tick：行情断流时心跳也能补挂/重挂最新层止盈单）
        await self._maybe_preplace_exit(ts)
        return actions

    # ---------- 动量 ----------
    def _momentum(self, ts: float, spread: float) -> Optional[float]:
        """最近 momentum_min 分钟的 spread 变化（MOM0 不启用返回 None）。"""
        mom = self.cfg.filters.momentum_min
        if mom <= 0:
            return None
        cutoff = ts - mom * 60.0
        self._momentum_ts = [(t, s) for (t, s) in self._momentum_ts if t >= cutoff]
        self._momentum_ts.append((ts, spread))
        if len(self._momentum_ts) < 2:
            return 0.0
        return self._momentum_ts[-1][1] - self._momentum_ts[0][1]

    # ---------- 事件入口 ----------
    def _log_decision(self, ts: float, trading_day: str, spread: float,
                      action: str, detail: str) -> None:
        """记录一条决策摘要（前端日志/警告面板）。

        去重：输出内容（action + detail）与上一条一致时不再追加，
        避免高频 tick 下"统计窗口未就绪/未触发阈值/交易未激活"等
        连续重复状态刷屏；spread 为数值不参与判重。"""
        if (self.decision_log
                and self.decision_log[-1]["action"] == action
                and self.decision_log[-1]["detail"] == detail):
            return
        self.decision_log.append({
            "ts": ts, "trading_day": trading_day, "spread": round(spread, 3),
            "action": action, "detail": detail,
        })
        if len(self.decision_log) > 500:
            self.decision_log = self.decision_log[-500:]

    def _record_decision(self, ts: float, trading_day: str, tick: SpreadTick,
                         sig: Signal, fres, actions: list) -> None:
        """计算日志落盘（复盘用）：每次决策的输入 ctx + 输出 actions，
        落盘 data/<slug>/<sim_|real_>signals_<交易日>.jsonl（模拟/实盘独立）。

        字段口径见 strategy/records.py（信号 tick：spread/mean/sd + 开平对手价 + 方向）。"""
        if self._store is None:
            return
        date = trading_day or time.strftime("%Y-%m-%d")
        rec = {
            "ts": ts,
            "trading_day": date,
            "slug": self.cfg.slug,
            "mode": self.cfg.mode,
            "spread": round(tick.spread, 6),
            "mean": _nan_none(sig.mean),
            "sd": _nan_none(sig.sd),
            "signal_ready": bool(sig.ready),
            "pos_side": self.pos.side.name if self.pos.side else None,
            "exit_price": _nan_none(sig.exit_price),
            "long_cross_price": _nan_none(sig.long_cross_price),
            "short_cross_price": _nan_none(sig.short_cross_price),
            "near_volume": tick.near.volume,
            "far_volume": tick.far.volume,
            "filter_passed": bool(fres.passed),
            "filter_blocked_by": fres.blocked_by,
            "actions": [
                {"type": a.type.name, "side": a.side.name if a.side else None,
                 "volume": a.volume, "reason": a.reason or "",
                 "tier_index": a.tier_index, "price": a.price,
                 "market": a.market}
                for a in actions
            ],
        }
        self._store.append_jsonl(self.cfg.slug,
                                 f"{self._mode_prefix()}signals_{date}.jsonl", rec)

    def _record_skip(self, ts: float, trading_day: str, tick: SpreadTick,
                     reason: str) -> None:
        """计算日志落盘（精简）：门禁短路路径（熔断/全平/交易未激活）的
        决策过程留痕，字段与 _record_decision 兼容（skip_reason 标识原因）。"""
        if self._store is None:
            return
        date = trading_day or time.strftime("%Y-%m-%d")
        mean = self.window.mean()
        sd = self.window.sd()
        rec = {
            "ts": ts,
            "trading_day": date,
            "slug": self.cfg.slug,
            "mode": self.cfg.mode,
            "spread": round(tick.spread, 6),
            "mean": _nan_none(mean),
            "sd": _nan_none(sd),
            "signal_ready": bool(mean is not None and sd is not None and sd > 0),
            "pos_side": self.pos.side.name if self.pos.side else None,
            "exit_price": _nan_none(spread_exit_price(tick, self.pos.side,
                                                      self.slippage_points))
            if self.pos.is_open else None,
            "long_cross_price": _nan_none(tick.near.ask - tick.far.bid),
            "short_cross_price": _nan_none(tick.near.bid - tick.far.ask),
            "near_volume": tick.near.volume,
            "far_volume": tick.far.volume,
            "filter_passed": None,
            "filter_blocked_by": None,
            "actions": [],
            "skip_reason": reason,
        }
        self._store.append_jsonl(self.cfg.slug,
                                 f"{self._mode_prefix()}signals_{date}.jsonl", rec)

    async def on_tick(self, tick: SpreadTick, trading_day: str) -> list[Action]:
        """处理一条跨期 tick，返回本拍执行的动作。"""
        self._last_tick = tick
        self._last_trading_day = trading_day
        self.window.push(tick.ts, tick.spread)
        # K 线聚合（仅图表/缓存用，不参与统计）：闭合 → 入历史 + 落盘
        closed = self.kline_agg.update(tick.spread, tick.ts)
        if closed is not None:
            self._record_kline(closed)
        # 记录 spread 历史（最近 6000 条，图表用）——门禁前记录，采集阶段也积累行情
        m = self.window.mean()
        self.spread_hist.append((tick.ts, tick.spread, m if m is not None else float("nan")))
        if len(self.spread_hist) > 6000:
            self.spread_hist = self.spread_hist[-6000:]
        momentum = self._momentum(tick.ts, tick.spread)

        # ---- 新交易日：先清理旧日报单，再按当前持仓决定是否重新挂止盈 ----
        if not await self._clear_previous_day_orders(trading_day, tick.ts):
            return []
        # ---- 挂单撮合 + 回报应用（先撮合已挂单，再基于最新持仓决策） ----
        await self._expire_stale_orders(tick.ts)
        await self._match_and_apply(tick.spread)

        # ---- 熔断门：全部停手（含平仓单；人工操作优先） ----
        if self.risk is not None and self.risk.open:
            self._record_skip(tick.ts, trading_day, tick, "熔断停手")
            self._log_decision(tick.ts, trading_day, tick.spread, "无", "熔断停手")
            return []

        # ---- 全平挂起（clear_position）：撤挂单 + 市价全平 ----
        # 撤单未确认 / 通道未就绪 / 发单失败时保留请求（_close_requested 回置 True），
        # 下一拍重试 —— 否则全平指令会静默丢失（实盘尤其危险）。
        if self._close_requested:
            if self.broker is not None:
                await self._cancel_live_orders()   # 先撤挂单（不重挂）
                if self._live_orders:
                    return []                     # 等撤单终态，期间成交先落账
            self._close_requested = False
            if self.pos.is_open:
                self._record_skip(tick.ts, trading_day, tick, "全平指令")
                self._log_decision(tick.ts, trading_day, tick.spread, "平", "全平指令")
                ok = await self._execute_market_close(
                    Action(ActionType.CLOSE, volume=self.pos.volume,
                           reason="全平指令", market=True),
                    tick, trading_day)
                if not ok:
                    self._close_requested = True   # 未发出 → 下一拍重试
            return []

        # ---- 交易开关门：未激活时拒绝一切报单行为（含平仓单），不发单 ----
        # 回报应用（_match_and_apply）已在上方执行：成交/撤单回报照常更新
        # 本地持仓与记录；全平指令（clear_position）在上方独立处理，不受影响。
        if not self.trading_active:
            self._record_skip(tick.ts, trading_day, tick, "交易未激活")
            self._log_decision(tick.ts, trading_day, tick.spread, "无", "交易未激活")
            return []

        # ---- 交易时段门禁：非交易时段不产生任何自动单（人工全平/撤单不受限） ----
        if not self.in_session(tick.ts):
            self._record_skip(tick.ts, trading_day, tick, "非交易时段")
            self._log_decision(tick.ts, trading_day, tick.spread, "无", "非交易时段")
            return []

        sig = Signal(
            ts=tick.ts,
            trading_day=trading_day,
            spread=tick.spread,
            mean=self.window.mean() if self.window.mean() is not None else float("nan"),
            sd=self.window.sd() if self.window.sd() is not None else float("nan"),
            near_volume=tick.near.volume,
            far_volume=tick.far.volume,
            near_inst=tick.near.inst,
            far_inst=tick.far.inst,
            exit_price=spread_exit_price(tick, self.pos.side, self.slippage_points)
            if self.pos.is_open else float("nan"),
            long_cross_price=tick.near.ask - tick.far.bid,
            short_cross_price=tick.near.bid - tick.far.ask,
        )
        fres = self.filters.entry_allowed(tick, trading_day, momentum)
        ctx = DecisionContext(self.pos, sig, fres, momentum)
        actions = self.sm.evaluate(ctx)

        # ---- 对账门：核验期间只平不开 ----
        if self.reconciling:
            actions = [a for a in actions if a.type is ActionType.CLOSE]
        # ---- 只平不开门：全平后保持（防自动重新开仓；BMR2 语义）
        #      或合约配置"只平仓"（持久化开关，同样只保留平仓动作） ----
        if self.close_only or self.config_close_only:
            actions = [a for a in actions if a.type is ActionType.CLOSE]

        # 计算日志落盘（复盘用）：每次决策的输入 ctx + 输出 actions
        self._record_decision(tick.ts, trading_day, tick, sig, fres, actions)

        # 决策日志
        if actions:
            for a in actions:
                kind = {ActionType.OPEN: "开", ActionType.ADD: "加",
                        ActionType.CLOSE: "平"}[a.type]
                self._log_decision(tick.ts, trading_day, tick.spread,
                                   kind, a.reason or "")
        else:
            if not sig.ready:
                self._log_decision(tick.ts, trading_day, tick.spread, "无", "统计窗口未就绪")
            elif not fres.passed and fres.blocked_by:
                self._log_decision(tick.ts, trading_day, tick.spread, "无",
                                   f"过滤器阻止: {fres.blocked_by}")
            elif self.reconciling:
                self._log_decision(tick.ts, trading_day, tick.spread, "无", "对账中只平不开")
            elif self.config_close_only:
                self._log_decision(tick.ts, trading_day, tick.spread, "无", "只平仓（配置）")
            else:
                self._log_decision(tick.ts, trading_day, tick.spread, "无", "未触发阈值")

        for a in actions:
            await self._execute(a, tick, trading_day)
        # ---- 预挂止盈每拍兜底（仅最新层） ----
        # 预挂此前只在「开/加仓成交、平掉一层、跨日清旧单」事件上触发一次，标记
        # 消费后不再重试 → 开启开关时已有持仓、重启恢复持仓、加仓撤单而加仓单未
        # 成交、手动撤单等场景下持仓会长期没有止盈单。此处每拍补挂一次（门禁与
        # _maybe_preplace_exit 一致：开关/持仓/无在途挂单/已激活/交易时段）。
        # 放在本拍动作之后：开/加/平动作优先占用同 slug 的唯一挂单槽。
        await self._maybe_preplace_exit(tick.ts)
        return actions

    # ---------- 熔断事件转发（实时回报路径） ----------
    def on_order_failure(self) -> bool:
        """发单失败（REJECTED/TIMEOUT/异常）→ 熔断计数 +1；返回是否触发熔断。"""
        if self.risk is None:
            return False
        return self.risk.on_order_failure()

    def on_traded(self) -> None:
        """成交 → 复位失败计数。"""
        if self.risk is not None:
            self.risk.on_traded()

    # ---------- 成交执行（有 broker → 限价挂单；无 broker → 同步撮合） ----------
    def _real_channel_missing(self) -> bool:
        """实盘模式但没有交易通道：此时禁止本地记账（否则产生柜台不存在的假报单/假持仓）。"""
        return self.cfg.mode == "real" and self.broker is None

    def _skip_no_channel(self, tick: SpreadTick, trading_day: str) -> None:
        self._log_decision(tick.ts, trading_day, tick.spread, "风控",
                           "实盘模式但交易通道未就绪，跳过本次动作（不发假单）")

    async def _execute(self, action: Action, tick: SpreadTick,
                       trading_day: str) -> bool:
        """执行动作；返回 False = 未发出（通道未就绪/发单接口拦住），调用方可用它重试。"""
        if self._real_channel_missing():
            # 实盘模式而没有通道：绝不能记账建层/平仓（本地会造出柜台不存在的数据）
            self._skip_no_channel(tick, trading_day)
            return False
        if action.type is ActionType.OPEN:
            if self.broker is not None:
                return await self._place_order(action, tick, trading_day, "open")
            assert action.side is not None
            price = spread_entry_price(tick, action.side, self.slippage_points)
            entry_mean = self.window.mean() or 0.0
            self.pos.side = action.side
            self.pos.tiers = [Tier(action.volume, price, tick.ts, trading_day, entry_mean)]
            self.pos.entry_mean = entry_mean
            self.pos.open_trading_day = trading_day
            self._make_order("open", action.side, action.volume, price,
                             tick.ts, trading_day, action.reason)
            self._persist_position()
            return True
        if action.type is ActionType.ADD:
            if self.broker is not None:
                # 加仓前撤掉预挂的平仓单（它占着唯一挂单槽），加仓成交后按新层重挂
                await self._cancel_preplace_exit()
                return await self._place_order(action, tick, trading_day, "add")
            assert action.side is not None and self.pos.side == action.side
            price = spread_entry_price(tick, self.pos.side, self.slippage_points)
            entry_mean = self.window.mean() or 0.0
            self.pos.tiers.append(Tier(action.volume, price, tick.ts,
                                       trading_day, entry_mean))
            self._make_order("add", action.side, action.volume, price,
                             tick.ts, trading_day, action.reason)
            self._persist_position()
            return True
        if action.type is ActionType.CLOSE:
            if self.broker is not None and not action.market:
                return await self._place_order(action, tick, trading_day, "close")
            return await self._execute_market_close(action, tick, trading_day)
        return False

    async def _execute_market_close(self, action: Action, tick: SpreadTick,
                                    trading_day: str) -> bool:
        """市价平仓（止损/强平/时间止损/全平）执行。返回是否已发出/已完成。

        - 实盘通道：没有本地撮合能力，必须按对手价发真实限价单（穿透成交），
          成交与持仓变动一律以 CTP 回报为准；**本地绝不记账成交** —— 否则柜台
          收不到平仓单、本地却显示已平，持仓与柜台会永久性脱节，且引擎以为空仓
          而继续开新仓（2026-10-19 焦炭 3 手即由此丢失）。
        - 无 broker（回测）/模拟通道：保留本地同步撮合（原行为）。
        - 实盘模式但通道缺失：拒绝记账（否则又是假报单），只记日志等通道恢复。

        实盘发单前先清空在途挂单（预挂止盈等）：同一 slug 只允许一个在途单；
        撤单尚未确认时本 tick 不发单，返回 False 让调用方下个 tick 重试
        （不破坏互斥假设，也避免与在途止盈单同时成交造成超平）。
        """
        if self.broker is None and self.cfg.mode == "real":
            self._skip_no_channel(tick, trading_day)
            return False
        if self.broker is None or not getattr(self.broker, "is_real", False):
            # 回测/模拟通道：先撤预挂单，避免仓已平而挂单残留（裸单）
            if self.broker is not None:
                await self._cancel_preplace_exit()
            self._close(tick, trading_day, action.reason,
                        volume=action.volume, tier_index=action.tier_index,
                        tier_id=action.tier_id)
            return True
        if self._live_orders:
            await self._cancel_live_orders()
            if self._live_orders:
                self._log_decision(tick.ts, trading_day, tick.spread, "平",
                                   f"{action.reason}：等待在途挂单撤销确认后发单")
                return False
        if not self.pos.is_open:
            return True
        price = spread_exit_price(tick, self.pos.side, self.slippage_points)
        return await self._place_order(action, tick, trading_day, "close",
                                       price=price, market=True)

    # ---------- 限价挂单（broker 模式） ----------
    def _tier_open_price(self, tier_id: str, tier_index: int) -> float:
        """平仓目标层的建仓价（显示用）：tier_id 优先 → tier_index 回退 → 整仓加权均价。"""
        if tier_id:
            for t in self.pos.tiers:
                if t.tier_id == tier_id:
                    return t.avg_price
        if 0 <= tier_index < len(self.pos.tiers):
            return self.pos.tiers[tier_index].avg_price
        return self.pos.avg_price if self.pos.is_open else 0.0

    async def _place_order(self, action: Action, tick: SpreadTick, trading_day: str,
                           act_kind: str, *, price: Optional[float] = None,
                           market: bool = False) -> bool:
        """发限价挂单（挂单中）。同 slug 同时最多一个未终态单（互斥，防层级错位）。

        price 覆盖 action.price（市场平仓单的 action.price 为 0，实盘按对手价发出）；
        market=True 标记该单为市价平仓单（短超时撤单重发，见 OrderBook.expire_stale）。
        返回是否真的发出（False = 被互斥/风控拦住或发单失败）。
        """
        if self._live_orders:
            self._log_decision(tick.ts, trading_day, tick.spread, "挂",
                               "已有挂单未成交，跳过")
            return False
        # 账户级保证金上限（开/加仓前检查，跨合约合计；超限阻止发单）
        if act_kind in ("open", "add") and self.margin_check is not None:
            allowed, ratio = await self.margin_check(self.cfg.slug, action.volume)
            if not allowed:
                self._log_decision(tick.ts, trading_day, tick.spread, "风控",
                                   f"保证金上限：预计占用率 {ratio * 100:.1f}%，阻止{act_kind} {action.volume}手")
                return False
        # 方向：开/加仓用动作方向；平仓用持仓方向（CLOSE 动作不带 side）
        if act_kind == "close":
            side = self.pos.side
        else:
            side = action.side
        assert side is not None
        # 买卖方向：开/加多=买；平多（卖组合）=sell；空反之
        if act_kind == "close":
            direction = "sell" if side is Side.LONG else "buy"
        else:
            direction = "buy" if side is Side.LONG else "sell"
        target_tier = action.tier_index if act_kind == "close" else self.pos.tier_count
        px = float(action.price if price is None else price)
        spec = OrderSpec(
            slug=self.cfg.slug, direction=direction, side=("open" if act_kind != "close" else "close"),
            volume=action.volume, price=px,
            instrument=self.cfg.instrument, local_id="",
            reason=action.reason or "", target_tier=target_tier,
            open_price=self._tier_open_price(getattr(action, "tier_id", ""),
                                             action.tier_index) if act_kind == "close" else 0.0,
            tier_id=getattr(action, "tier_id", ""),
            timeout_seconds=float(getattr(self.cfg.exit, "order_timeout_s", 60.0) or 0.0),
            market=market,
        )
        try:
            order = await self.ob.place(spec, trading_day, tick.ts)
        except Exception as e:  # noqa: BLE001 - 发单失败计入熔断
            if self.risk is not None:
                self.risk.on_order_failure()
            self._log_decision(tick.ts, trading_day, tick.spread, "挂",
                               f"发单失败: {e}")
            return False
        if order is None:
            self._log_decision(tick.ts, trading_day, tick.spread, "挂",
                               "已有挂单未成交，跳过")
            return False
        kind_cn = "市价平仓" if market else f"限价{act_kind}"
        self._log_decision(tick.ts, trading_day, tick.spread, "挂",
                           f"{kind_cn} 挂单 {action.volume}手 @ {px:.2f} {action.reason}")
        return True

    async def _expire_stale_orders(self, ts: float) -> None:
        """挂单超时自动撤单（order_timeout_s 可配置秒数；0 = 不启用；不重挂）。

        超时仅发起撤单、不立即移除本地挂单：终态由撤单/成交回报经
        _apply_reports 归档（避免撤单窗口内成交回报丢失导致本地与实盘持仓不一致）。
        用行情时间 ts 对比（回测/模拟回放下墙钟会失真）。

        市价平仓单（止损/强平/全平）由 OrderBook 按固定 MARKET_CLOSE_TIMEOUT_S
        判定，因此 order_timeout_s=0（不启用超时撤单）时仍会做本检查。
        """
        timeout = getattr(self.cfg.exit, "order_timeout_s", 60.0) or 0.0
        if self.broker is None or not self._live_orders:
            return
        for o in await self.ob.expire_stale(ts, timeout):
            limit = MARKET_CLOSE_TIMEOUT_S if getattr(o, "market", False) else timeout
            self._log_decision(ts, o.trading_day, o.price, "撤",
                               f"挂单超时（{limit:.0f}s）撤单 {o.reason}")

    async def _cancel_live_orders(self) -> None:
        """撤掉全部挂单（全平/停止交易时调用；不重挂），并立即应用撤单回报。"""
        await self.ob.cancel_all()
        await self._apply_reports()

    async def _match_and_apply(self, spread: float) -> None:
        """挂单撮合 + 回报应用（tick 到达时，先撮合再处理回报再决策）。

        模拟通道 tick 驱动撮合（on_market_tick）；实盘通道无撮合
        （Broker 默认空实现），等待 CTP 回报。
        """
        if self.broker is None:
            return
        try:
            await self.broker.on_market_tick(self.cfg.slug, spread)
            await self._apply_reports()
        except Exception:  # noqa: BLE001
            pass

    async def _apply_reports(self) -> None:
        """拉取回报并应用：成交 → 建层/平层；撤/拒/超时 → 归档（不重挂）。

        回报应用不依赖行情 tick（心跳/控制指令路径同样调用）；状态迁移与
        变更流水由 OrderBook.apply_reports 统一处理。"""
        if self.broker is None:
            return
        reports = await self.broker.drain_reports(self.cfg.slug)
        if reports:
            self.ob.apply_reports(reports)
        # 开/加仓成交后按配置预挂平仓单：成交回调（_on_open_traded）是同步的，
        # 发单必须回到异步上下文执行，故在此统一处理待办标记。
        if self._preplace_pending:
            await self._maybe_preplace_exit()

    async def reconcile_orders_from_ctp(self, ts: float = 0.0) -> list[str]:
        """用 CTP 当日报单修正本地实时报单状态（实盘；模拟通道直接返回空）。

        为什么：回报匹配依赖「CTP 引用 → 本地单」映射，重启后该映射为空，柜台侧
        旧会话单的成交/撤单回报匹配不到本地单 → 本地永久停在"挂单中"（实测：柜台
        已撤/已成交，本地仍 ACTIVE + cancelling；既不建层/平层，又占住「同 slug
        单挂单」互斥槽使该合约不再挂单）。故在引擎登录后（启动交易/采集 + 心跳）
        按 order_sys_id 对齐，以 CTP 为准生成回报走既有 apply_reports 路径。

        调用频次由调用方控制（engine._reconcile_orders_if_due 负责每日一次 + 节流）。
        返回差异告警列表（同时写入决策日志）；未登录/查询失败返回空（下次心跳重试）。
        """
        if self.broker is None or not getattr(self.broker, "is_real", False):
            return []
        day = self._last_trading_day or time.strftime("%Y-%m-%d")
        if not getattr(self.broker, "ctp_logged_in", False):
            return []                        # 交易前置未登录：等下一拍心跳
        now = ts or time.time()
        try:
            ctp_orders = await self.broker.query_order(self.cfg.slug)
        except Exception as e:  # noqa: BLE001 - 对账查询失败不阻断交易链路
            self._log_decision(now, day, 0.0, "对账", f"CTP 报单查询失败: {e}")
            return []
        self._order_reconciled_day = day
        self._last_order_reconcile_ts = now
        reports, warnings = self.ob.reconcile_reports(ctp_orders, day)
        # 「柜台查无此报单」核验结论单独记账：这类是"撤单被反复拒绝 + 当日报单查无"
        # 判定的本地归档（释放挂单槽），与普通状态修正分开，便于事后审计。
        archived_missing = [r for r in reports
                            if r.status == OrderStatus.CANCELLED.value
                            and "柜台查无此报单" in (r.error_msg or "")]
        if reports:
            self.ob.apply_reports(reports)
        self._log_decision(
            now, day, 0.0, "对账",
            f"CTP 报单对账：当日报单 {len(ctp_orders)} 条，修正 {len(reports)} 条"
            + (f"，{len(warnings)} 条差异待人工核对" if warnings else ""))
        for r in archived_missing:
            self._log_decision(
                now, day, 0.0, "核验",
                f"柜台查无此报单（撤单多次未确认）→ 本地归档 {r.local_id}"
                f"（释放挂单槽，下拍按持仓重挂止盈单）")
        for w in warnings:
            self._log_decision(now, day, 0.0, "对账", w)
        return warnings

    async def _reconcile_orders_if_due(self, ts: float) -> None:
        """心跳兜底：实盘未完成当日对账、或存在撤单中的单时按节流重查 CTP 报单。

        本地实时区为空时直接跳过（无需修正，也避免多合约启动时集中空查柜台）。
        """
        if self.broker is None or not getattr(self.broker, "is_real", False):
            return
        if not self._live_orders:
            return
        day = self._last_trading_day or time.strftime("%Y-%m-%d")
        pending = any(o.cancelling for o in self._live_orders.values())
        if self._order_reconciled_day == day and not pending:
            return
        if ts and (ts - self._last_order_reconcile_ts) < self.ORDER_RECONCILE_INTERVAL_S:
            return
        await self.reconcile_orders_from_ctp(ts)

    async def _retry_stale_cancels(self, ts: float) -> None:
        """撤单未确认兜底（心跳）：重发撤单（限次）并把重发写入决策日志。"""
        if self.broker is None or not self._live_orders:
            return
        for lid in await self.ob.retry_stale_cancels(ts, CANCEL_RETRY_INTERVAL_S):
            o = self._live_orders.get(lid)
            self._log_decision(
                ts, (o.trading_day if o else "") or self._last_trading_day,
                (o.price if o else 0.0), "撤",
                f"撤单未确认（{CANCEL_RETRY_INTERVAL_S:.0f}s），重发撤单"
                + (f"（第 {o.cancel_retries} 次）" if o else ""))

    async def calibrate_position_from_ctp(self, *, dry_run: bool = True) -> dict:
        """持仓校准（人工触发）：CTP 单腿持仓 → 反推跨期组合 → 与本地比对/校准。

        反推依据见 docs/position-calibration.md：
        - 近月多 + 远月空 = 组合多头，近月空 + 远月多 = 组合空头（取配对 min）；
        - 组合成本 = 近月腿均价 − 远月腿均价（价差口径 near − far）；
        - 手数/方向以柜台为准；层结构尽量保留；均价本地缺失才用 CTP 盯市成本补齐。

        dry_run=True 只返回比对与计划（不修改本地持仓）；False 时按计划重建持仓层
        并落盘（只改本地记账，不发单/不撤单）。查询失败或通道不支持 →
        ok=False 且**绝不修改**本地持仓（fail-safe）。
        """
        slug = self.cfg.slug
        day = self._last_trading_day or time.strftime("%Y-%m-%d")
        if self.broker is None:
            return {"ok": False, "slug": slug,
                    "error": "无交易通道（或通道不支持单腿持仓查询），无法校准持仓"}
        if not getattr(self.broker, "is_real", False):
            # 模拟通道没有柜台持仓：空腿持仓会被误读为「柜台无持仓」而清空本地持仓
            return {"ok": False, "slug": slug,
                    "error": "持仓校准只对实盘通道有效（模拟模式无 CTP 柜台持仓）"}
        if not getattr(self.broker, "ctp_logged_in", False):
            # 未登录时查询结果不可信（可能返回空）→ 拒绝，避免误清空持仓
            return {"ok": False, "slug": slug,
                    "error": "交易前置未登录，无法查询柜台持仓（请先启动交易并等待登录）"}
        query = getattr(self.broker, "query_leg_position", None)
        if query is None:
            return {"ok": False, "slug": slug,
                    "error": "当前交易通道不支持单腿持仓查询，无法校准持仓"}
        near, far = (self.cfg.near_leg or ""), (self.cfg.far_leg or "")
        if not near or not far:
            return {"ok": False, "slug": slug,
                    "error": f"合约 {slug} 未配置 near_leg/far_leg，无法反推跨期持仓"}
        try:
            near_rows = await query(near)
            far_rows = await query(far)
        except Exception as e:  # noqa: BLE001 - 查询失败不修改本地（fail-safe）
            self._log_decision(time.time(), day, 0.0, "校准",
                               f"CTP 腿持仓查询失败（未修改本地持仓）: {e}")
            return {"ok": False, "slug": slug,
                    "error": f"查询 CTP 腿持仓失败：{e}"}

        legs = aggregate_legs(list(near_rows or []) + list(far_rows or []))
        inference = infer_combo(legs.get(near, []), legs.get(far, []),
                                slug=slug, multiplier=self.cfg.multiplier)
        plan = plan_calibration(
            self.pos, inference,
            max_positions=self.cfg.layering.max_positions,
            tick_size=self.cfg.tick_size, trading_day=day,
            entry_mean=self.window.mean() or 0.0,
            now=time.time())
        live_orders = list(self._live_orders.values()) if self._live_orders else []
        if live_orders:
            # 校准只改本地记账：柜台挂单不会被撤/改，数量与校准后的持仓可能不匹配
            plan.warnings.append(
                f"本地仍有 {len(live_orders)} 笔未终态挂单：校准只改本地记账，"
                f"不会自动撤单/改单，请确认柜台挂单与校准后的持仓是否匹配")

        result = {
            "ok": True, "slug": slug, "dry_run": bool(dry_run),
            "near_leg": near, "far_leg": far,
            "multiplier": self.cfg.multiplier,
            "live_orders": len(live_orders),
            "legs": {k: [{"instrument": l.instrument, "direction": l.direction,
                          "volume": l.volume, "avg_price": round(l.avg_price, 4),
                          "cost": round(l.cost, 2)} for l in v]
                     for k, v in legs.items()},
            "inference": inference.to_dict(),
            "plan": plan.to_dict(),
            "applied": False,
        }
        if dry_run:
            return result
        if not plan.need_change:
            return result
        self.apply_calibration_plan(plan)
        result["applied"] = True
        result["position"] = {
            "side": self.pos.side.name if self.pos.side else None,
            "volume": self.pos.volume,
            "tier_count": self.pos.tier_count,
            "avg_price": round(self.pos.avg_price, 4),
            "tiers": [{"tier_id": t.tier_id, "volume": t.volume,
                       "avg_price": round(t.avg_price, 4)} for t in self.pos.tiers],
        }
        return result

    def apply_calibration_plan(self, plan) -> None:
        """按校准计划重建本地持仓层（只改本地记账 + 落盘 + 决策日志）。

        供单合约校准（`calibrate_position_from_ctp`）与账户级校准
        （`web.server.App.calibrate_position`：共享腿联合反推 + 守恒校验）共用。
        """
        tiers: list[Tier] = []
        for t in plan.tiers:
            vol = int(t.get("volume") or 0)
            if vol <= 0:
                continue
            tier = Tier(volume=vol,
                        avg_price=float(t.get("avg_price") or 0.0),
                        open_ts=float(t.get("open_ts") or 0.0),
                        open_trading_day=str(t.get("open_trading_day") or ""),
                        entry_mean=float(t.get("entry_mean") or 0.0))
            tid = str(t.get("tier_id") or "")
            if tid:
                tier.tier_id = tid      # 保留层的稳定身份（平仓单/回报按它定位）
            tiers.append(tier)
        side_name = str(plan.target.get("side") or "")
        if tiers and side_name in ("LONG", "SHORT"):
            same_side = (self.pos.is_open and self.pos.side is not None
                         and self.pos.side.name == side_name)
            self.pos = Position(
                side=Side[side_name], tiers=tiers,
                entry_mean=self.pos.entry_mean if same_side else (self.window.mean() or 0.0),
                open_trading_day=(self.pos.open_trading_day if same_side
                                  else (self._last_trading_day or "")))
        else:
            self.pos = Position()
        self._persist_position()
        detail = "持仓校准（以 CTP 单腿持仓反推为准）：" + (
            "；".join(plan.changes) if plan.changes else "无变更")
        if plan.warnings:
            detail += "｜告警：" + "；".join(plan.warnings)
        self._log_decision(time.time(), self._last_trading_day, 0.0, "校准", detail)

    def _handle_order_traded(self, o: Order) -> None:
        """成交回调（OrderBook → 引擎）：风控复位 + 建层/平层。"""
        if self.risk is not None:
            self.risk.on_traded()      # 成交 → 复位发单失败计数（熔断接线）
        if o.side == "open":
            self._on_open_traded(o)
        else:
            self._on_close_traded(o)

    def _handle_cancel_confirmed(self, o: Order, was_cancelling: bool) -> None:
        """撤单确认回调（OrderBook → 引擎）。

        阶段4 扩展：非手动撤单（交易所/风控直接撤）连续发生 → 熔断检测。
        当前仅预留接口。"""
        return

    def _handle_order_rejected(self, o: Order) -> None:
        """拒单/超时回报回调（OrderBook → 引擎）：熔断计数 +1。

        回报路径的 REJECTED/TIMEOUT 此前不经过发单异常分支，连续拒单不会触发
        熔断 → 挂单被拒后实时区清空、下个 tick 又重发同单 → 拒单-重发死循环。
        接入熔断后：连续失败达阈值 → 全部停手（含平仓单），人工复位后恢复。"""
        if self.risk is None:
            return
        triggered = self.risk.on_order_failure()
        self._log_decision(time.time(), self._last_trading_day, o.price, "风控",
                           f"拒单回报 {o.reason or o.status}（连续失败 {self.risk.consecutive_failures} 次）")
        if triggered:
            self._log_decision(time.time(), self._last_trading_day, o.price, "风控",
                               "熔断触发：连续拒单达阈值，全部停手（人工复位后恢复）")

    def _handle_orphan_report(self, r, reason: str) -> None:
        """孤儿回报回调（匹配不到实时区报单）。

        - archived：该单已终态归档（重报/迟到的回报）→ 幂等忽略，不重复处理；
        - unknown：完全未知的回报 → 告警（可能本地记录丢失/跨合约撞号）。
        """
        if reason == "archived":
            return
        self._log_decision(time.time(), self._last_trading_day, 0.0, "回报",
                           f"孤儿回报 {r.status} local_id={r.local_id} ref={r.ctp_ref}（匹配不到报单，需人工核对）")

    def _on_open_traded(self, o) -> None:
        """开/加仓挂单成交 → 建层（追加尾部）。"""
        entry_mean = self.window.mean() or 0.0
        side = Side.LONG if o.direction == "buy" else Side.SHORT
        if not self.pos.is_open:
            self.pos.side = side
            self.pos.entry_mean = entry_mean
            self.pos.open_trading_day = o.trading_day
        self.pos.tiers.append(Tier(o.cum_volume, o.avg_trade_price,
                                   o.order_time, o.trading_day, entry_mean))
        # 报单记录由挂单本身成交归档产生（apply_reports → archive），此处不再
        # _make_order 重复生成一条"市价格式"记录（否则网页同一笔开/加仓显示 2 条）
        self._persist_position()
        self._preplace_pending = True    # 成交后预挂平仓单（_apply_reports 末尾异步执行）
        self._log_decision(o.order_time, o.trading_day, o.avg_trade_price, "开",
                           f"挂单成交 {o.cum_volume}手 @ {o.avg_trade_price:.2f} {o.reason}")

    def _resolve_tier_index(self, tier_id: str, tier_index: int) -> int:
        """按稳定层身份定位层索引；tier_id 为空/找不到回退 tier_index（带范围保护）。

        返回 -1 = 无法定位（整仓或异常，调用方跳过）。"""
        if tier_id:
            for i, t in enumerate(self.pos.tiers):
                if t.tier_id == tier_id:
                    return i
            # 层身份找不到（如重启后旧挂单 tier_id 已失效）→ 告警，不盲用索引平错层
            self._log_decision(time.time(), self._last_trading_day, 0.0, "平",
                               f"平仓目标层 {tier_id} 不存在（重启后层身份可能变化），跳过本次平仓")
            return -1
        if 0 <= tier_index < len(self.pos.tiers):
            return tier_index
        return -1

    def _on_close_traded(self, o) -> None:
        """平仓挂单成交 → 平指定层（成交价结算）。按 tier_id 定位层（回退 target_tier 索引）。

        整仓平仓单（target_tier=-1 且无 tier_id，如全平指令/实盘市价全平）按成交手数
        从最新层逐层结算，与 _close 的整仓分支口径一致（每层一条 Trade）。
        """
        side = self.pos.side
        if side is None:
            return
        target = self._resolve_tier_index(o.tier_id, o.target_tier)
        if target < 0 and not o.tier_id and o.target_tier < 0:
            self._close_tiers_filled(o)
            return
        if target < 0 or target >= len(self.pos.tiers):
            self._log_decision(o.order_time, o.trading_day, o.avg_trade_price, "平",
                               f"平仓成交 {o.local_id} 目标层不可定位（tier_id={o.tier_id}），跳过（需人工核对）")
            return
        tier = self.pos.tiers.pop(target)
        exit_price = o.avg_trade_price
        gross = side.sign * (exit_price - tier.avg_price) * self.cfg.multiplier * tier.volume
        fee = self.trade_fee(side, tier.volume, self._last_tick,
                             tier.open_trading_day, o.trading_day)
        self.trades.append(Trade(
            side=side, volume=tier.volume,
            entry_price=tier.avg_price, exit_price=exit_price,
            open_ts=tier.open_ts, close_ts=o.order_time,
            open_trading_day=tier.open_trading_day,
            close_trading_day=o.trading_day,
            gross_pnl=gross, fee=fee, exit_reason=o.reason,
        ))
        self._persist_trade(self.trades[-1])
        # 报单记录由平仓挂单本身成交归档产生（apply_reports → archive），
        # 此处不再 _make_order 重复生成（否则同一笔平仓显示 2 条记录）
        if not self.pos.tiers:
            self.pos = Position()
        self._persist_position()
        self._log_decision(o.order_time, o.trading_day, exit_price, "平",
                           f"挂单成交平 {tier.volume}手 @ {exit_price:.2f} {o.reason}")
        # 多层滚动：平掉一层后为新的最新层重挂预挂止盈单。此处只置待办标记，
        # 由 _apply_reports 末尾异步执行（_maybe_preplace_exit 会再校验配置开关/
        # 持仓/在途挂单）；否则多层持仓下"最新层平掉后剩余层没有预挂单"。
        if self.pos.is_open and not self._live_orders:
            self._preplace_pending = True

    def _close_tiers_filled(self, o) -> None:
        """整仓平仓成交（target_tier=-1、无 tier_id）：按成交手数从最新层逐层结算。

        实盘市价全平（止损/强平/时间止损/全平指令）走的是真实报单 → 成交回报里
        没有目标层身份，故按柜台成交手数逐层平；成交价取回报均价，与 _close
        整仓分支口径一致（每层一条 Trade）。
        """
        side = self.pos.side
        if side is None:
            return
        exit_price = o.avg_trade_price
        remain = int(o.cum_volume or 0) or self.pos.volume
        while remain > 0 and self.pos.tiers:
            tier = self.pos.tiers[-1]
            vol = min(remain, tier.volume)
            gross = side.sign * (exit_price - tier.avg_price) * self.cfg.multiplier * vol
            fee = self.trade_fee(side, vol, self._last_tick,
                                 tier.open_trading_day, o.trading_day)
            self.trades.append(Trade(
                side=side, volume=vol,
                entry_price=tier.avg_price, exit_price=exit_price,
                open_ts=tier.open_ts, close_ts=o.order_time,
                open_trading_day=tier.open_trading_day,
                close_trading_day=o.trading_day,
                gross_pnl=gross, fee=fee, exit_reason=o.reason,
            ))
            self._persist_trade(self.trades[-1])
            remain -= vol
            if vol >= tier.volume:
                self.pos.tiers.pop()
            else:
                tier.volume -= vol      # 部分成交（罕见）：剩余手数留在本层
        if not self.pos.tiers:
            self.pos = Position()
        self._persist_position()
        self._log_decision(o.order_time, o.trading_day, exit_price, "平",
                           f"整仓平仓成交 @ {exit_price:.2f} {o.reason}")

    def _archive_live_order(self, o) -> None:
        """终态挂单归档（兼容转发：OrderBook.archive 统一处理）。"""
        self.ob.archive(o, "terminal")

    def reload_mode_data(self, trading_day: str = "") -> None:
        """模式切换后重新加载对应模式的数据（实时持仓/成交/报单/K线/挂单）。

        挂单撤单由 save_config 在切换前完成（async）；此处仅清空内存后
        按新模式的 <sim_|real_> 前缀文件重新加载。
        """
        if trading_day:
            self._last_trading_day = trading_day
        self.pos = Position()
        self.trades = []
        self.ob.set_mode_prefix(self._mode_prefix())
        self.ob.reset()
        self.klines = []
        self.kline_agg.closed = []
        self._load_klines_cache()
        self._load_persisted()

    def _make_order(self, action: str, side: Side, volume: int, price: float,
                    ts: float, trading_day: str, reason: str = "",
                    open_price: float = 0.0) -> None:
        """生成一条报单记录（市价同步撮合：报单即成交，status=TRADED）。

        委托 OrderBook.record_sync：报单记录 + 历史归档（orders_hist）+ 变更流水。
        """
        self.ob.record_sync(action, side, volume, price, ts, trading_day, reason,
                            open_price=open_price)

    def trade_fee(self, side: Side, volume: int, tick: Optional[SpreadTick],
                  open_trading_day: str, close_trading_day: str) -> float:
        """一笔成交（开 2 腿 + 平 2 腿）的手续费合计（元）。

        - 元计费：2 腿 × (开 + 平) × 手数 —— 与旧口径 ``4 × fee_per_lot`` 一致；
        - 万分之：Σ(腿价 × 乘数 × 费率 ÷ 10000) × 手数；
        - 平仓用「平今」还是「平昨」：同一交易日开平 → 平今，否则平昨；
        - 开仓边的腿价用平仓时刻的盘口对手价近似（开仓时腿价未持久化）；
          无行情快照（tick 为 None）时退化为元口径（万分之品种记为 0）。
        """
        spec = self.fee_spec
        if tick is None:
            return 4.0 * spec.per_lot * volume
        mult = self.cfg.multiplier
        if side is Side.LONG:
            open_near, open_far = tick.near.ask, tick.far.bid      # 开多：买 near / 卖 far
            close_near, close_far = tick.near.bid, tick.far.ask    # 平多：卖 near / 买 far
        else:
            open_near, open_far = tick.near.bid, tick.far.ask
            close_near, close_far = tick.near.ask, tick.far.bid
        close_kind = ("today" if open_trading_day and open_trading_day == close_trading_day
                      else "close")
        per_lot = (spec.edge_fee(open_near, mult, "open")
                   + spec.edge_fee(open_far, mult, "open")
                   + spec.edge_fee(close_near, mult, close_kind)
                   + spec.edge_fee(close_far, mult, close_kind))
        return per_lot * volume

    def _tier_fee(self, tier, pos_side: Side, sig: Signal) -> float:
        """该层「开 + 平」一次的手续费（元）。

        供 ExitRules 的回归止盈净利判据使用，与结算（trade_fee）同口径；
        无行情快照时按元口径退化。
        """
        return self.trade_fee(pos_side, tier.volume, self._last_tick,
                              getattr(tier, "open_trading_day", ""),
                              getattr(sig, "trading_day", ""))

    def _tier_fee_at(self, tier, trading_day: str) -> float:
        """该层「开 + 平」一次的手续费（按给定平仓交易日判定平今/平昨）。"""
        return self.trade_fee(self.pos.side, tier.volume, self._last_tick,
                              getattr(tier, "open_trading_day", ""), trading_day)

    # ---------- 目标权益全平（账户级止盈保护；口径见 strategy/target_equity.py） ----------
    def estimate_position_exit(self, trading_day: str = "") -> Optional[dict]:
        """本合约持仓按当前对手价平仓的净盈估算（账户级目标权益判定用）。

        - 毛利 = 方向 × (平仓对手价 − 建仓均价) × 乘数 × 手数
          （平多 = near.bid − far.ask；平空 = near.ask − far.bid，与执行口径一致）；
        - 手续费 = 该持仓「开 + 平」合计（trade_fee：开 2 腿 + 平 2 腿，同日开平按平今）。

        返回 None = 无持仓 / 无行情 / 价差异常 → 调用方应视为「无法估算」，**不得**
        用残缺数据做全平决策。
        """
        if not self.pos.is_open or self.pos.side is None:
            return None
        tick = self._last_tick
        if tick is None:
            return None
        exit_px = spread_exit_price(tick, self.pos.side, self.slippage_points)
        if exit_px != exit_px:              # NaN：无有效报价
            return None
        day = trading_day or self._last_trading_day
        sign = self.pos.side.sign
        mult = self.cfg.multiplier
        gross = sum(sign * (exit_px - t.avg_price) * mult * t.volume
                    for t in self.pos.tiers)
        fee = sum(self._tier_fee_at(t, day) for t in self.pos.tiers)
        return {"slug": self.cfg.slug, "side": self.pos.side.name,
                "volume": self.pos.volume, "exit_price": exit_px,
                "gross_pnl": gross, "fee": fee}

    def request_flatten(self, reason: str, *, close_only: bool = True) -> None:
        """挂起全平请求（目标权益达成 / 人工调用）。

        与人工 `clear_position` 共用同一条全平路径：下个 tick 先撤在途挂单，再按
        对手价市价全平；发单失败/撤单未确认时保留请求下拍重试。
        `close_only=True` 同时锁成只平不开（目标达成后不再自动开仓，需手动
        「启动交易」恢复，与 clear_position 语义一致）。
        """
        self.close_only = bool(close_only)
        self._close_requested = True
        self._log_decision(time.time(), self._last_trading_day, 0.0, "平", reason)

    def preplace_exit_price(self, tier) -> float:
        """预挂平仓的目标价差：使该层净利 = min_exit_pnl。

        由止盈判据「毛利 − 手续费 ≥ min_exit_pnl」反解：

            目标价 = 层均价 + 方向 × (min_exit_pnl + 手续费) ÷ (乘数 × 手数)

        按 tick_size 取整，且取整方向保证净利不缩水（LONG 向上、SHORT 向下）；
        无法计算（无持仓/手数或乘数非法）返回 0。
        """
        side = self.pos.side
        volume = int(getattr(tier, "volume", 0) or 0)
        mult = float(self.cfg.multiplier or 0.0)
        if side is None or volume <= 0 or mult <= 0:
            return 0.0
        fee = self._tier_fee_at(tier, self._last_trading_day)
        raw = tier.avg_price + (side.sign * (float(self.cfg.exit.min_exit_pnl) + fee)
                                / (mult * volume))
        ts = float(self.cfg.tick_size or 0.0)
        if ts <= 0:
            return raw
        if side is Side.LONG:                    # 卖出止盈：向上取整，保住净利
            return math.ceil(raw / ts) * ts
        return math.floor(raw / ts) * ts         # 买入止盈：向下取整

    async def _cancel_preplace_exit(self) -> None:
        """撤掉预挂的止盈平仓单（只撤预挂单，不动其他挂单）。

        加仓前、市价平仓前调用：前者让出唯一挂单槽，后者避免"仓已平、单还在"。
        """
        ids = [lid for lid, o in self._live_orders.items()
               if o.side == "close" and PREPLACE_EXIT_TAG in (o.reason or "")]
        if not ids:
            return
        for lid in ids:
            await self.ob.cancel(lid)
        await self._apply_reports()

    async def _maybe_preplace_exit(self, ts: float = 0.0) -> None:
        """按配置预挂平仓单（cfg.exit_preplace 开启时）。

        调用点：
        - 事件驱动：开/加仓成交、平掉一层、跨日清旧单后（经 _preplace_pending）；
        - 每拍兜底：on_tick / on_heartbeat 末尾（补挂上述事件被跳过后缺失的止盈单）。

        目标价 = preplace_exit_price（净利反解）；只为最新层挂一张（与逐层止盈
        「从最新层开始」口径一致）。「同 slug 单挂单」互斥天然防重复挂出。
        """
        self._preplace_pending = False
        if not getattr(self.cfg, "exit_preplace", False):
            return
        if self.broker is None or not self.pos.is_open or self._live_orders:
            return
        # 门禁：未激活交易不主动挂单（与 on_tick 的开关门一致）；
        # 显式传入 ts 时同时校验交易时段（未传时由调用方 on_tick 的门禁保证）。
        if not self.trading_active:
            return
        if ts and not self.in_session(ts):
            return
        tier = self.pos.tiers[-1]
        # 价差可为负（跨期价差常年为负，如 a2611-a2701 ≈ −41），不能用 price <= 0
        # 当"算不出来"的哨兵；有效性按前置条件判定，价格由 preplace_exit_price 计算。
        if self.pos.side is None or tier.volume <= 0 or not self.cfg.multiplier:
            return
        price = self.preplace_exit_price(tier)
        side = self.pos.side
        target = len(self.pos.tiers) - 1
        spec = OrderSpec(
            slug=self.cfg.slug,
            direction=("sell" if side is Side.LONG else "buy"),
            side="close", volume=tier.volume, price=price,
            instrument=self.cfg.instrument, local_id="",
            reason=f"{PREPLACE_EXIT_TAG} 第{target + 1}层",
            target_tier=target, open_price=tier.avg_price,
            tier_id=getattr(tier, "tier_id", ""),
            timeout_seconds=0.0,     # 预挂单不参与超时撤单（等目标价成交）
        )
        try:
            order = await self.ob.place(spec, self._last_trading_day, ts or time.time())
        except Exception as e:  # noqa: BLE001 - 预挂失败计入熔断，不影响持仓
            if self.risk is not None:
                self.risk.on_order_failure()
            self._log_decision(ts or time.time(), self._last_trading_day, price, "挂",
                               f"预挂平仓失败: {e}")
            return
        if order is not None:
            self._log_decision(ts or time.time(), self._last_trading_day, price, "挂",
                               f"预挂平仓 {tier.volume}手 @ {price:.2f}"
                               f"（目标净利 {self.cfg.exit.min_exit_pnl:.0f} 元）")

    def _close(self, tick: SpreadTick, trading_day: str, reason: str,
               volume: int = 0, tier_index: int = -1, tier_id: str = "") -> None:
        """平仓（逐层仓位管理）。

        - tier_index >= 0 或 tier_id 非空：只平指定层（该层手数，独立盈亏结算为一条 Trade）。
          优先按 tier_id（稳定层身份）定位，找不到回退 tier_index（带范围保护）——
          避免其他层平仓导致索引错位而平错层；
        - 否则（tier_index=-1 且 tier_id 为空，含 volume=0 全平指令）：整仓全平，
          按层逐层结算（每层独立 Trade 记录），报单记录一条 close（总手数）。
        平仓后实时持仓落盘（<mode>position.json）。
        """
        if not self.pos.is_open:
            return
        side = self.pos.side

        # ---- 逐层平仓（止盈/止损单层） ----
        target = self._resolve_tier_index(tier_id, tier_index)
        if target >= 0:
            if target >= len(self.pos.tiers):
                return
            tier = self.pos.tiers.pop(target)
            exit_price = spread_exit_price(tick, side, self.slippage_points)
            gross = (side.sign * (exit_price - tier.avg_price)
                     * self.cfg.multiplier * tier.volume)
            fee = self.trade_fee(side, tier.volume, tick,
                                 tier.open_trading_day, trading_day)
            self.trades.append(Trade(
                side=side, volume=tier.volume,
                entry_price=tier.avg_price, exit_price=exit_price,
                open_ts=tier.open_ts, close_ts=tick.ts,
                open_trading_day=tier.open_trading_day,
                close_trading_day=trading_day,
                gross_pnl=gross, fee=fee, exit_reason=reason,
            ))
            self._persist_trade(self.trades[-1])
            self._make_order("close", side, tier.volume, exit_price,
                             tick.ts, trading_day, reason,
                             open_price=tier.avg_price)
            if not self.pos.tiers:
                self.pos = Position()
            self._persist_position()
            return

        # ---- 整仓全平（强平/时间止损/全平指令/手动平仓） ----
        exit_price = spread_exit_price(tick, side, self.slippage_points)
        total_volume = self.pos.volume
        whole_avg = self.pos.avg_price
        for tier in self.pos.tiers:
            gross = (side.sign * (exit_price - tier.avg_price)
                     * self.cfg.multiplier * tier.volume)
            fee = self.trade_fee(side, tier.volume, tick,
                                 tier.open_trading_day, trading_day)
            self.trades.append(Trade(
                side=side, volume=tier.volume,
                entry_price=tier.avg_price, exit_price=exit_price,
                open_ts=tier.open_ts, close_ts=tick.ts,
                open_trading_day=tier.open_trading_day,
                close_trading_day=trading_day,
                gross_pnl=gross, fee=fee, exit_reason=reason,
            ))
            self._persist_trade(self.trades[-1])
        self._make_order("close", side, total_volume, exit_price,
                         tick.ts, trading_day, reason,
                         open_price=whole_avg)
        self.pos = Position()
        self._persist_position()

    # ---------- 人工维护持仓（对齐 BMR2：新增/编辑/删除持仓层） ----------
    def add_tier(self, *, volume=0, avg_price=0.0, open_trading_day: str = "",
                 side=None) -> Optional[Tier]:
        """人工新增持仓层（空仓 = 手动建仓；对齐 BMR2 的人工持仓维护）。

        - 有持仓：方向继承当前持仓，新层追加到末尾（最新层）；
        - 空仓：必须指定 side（"LONG"/"SHORT"）才能建仓，否则返回 None；
        - 手数 <= 0 或无方向 → 返回 None（不产生任何变更）；
        - 均价缺省 0（界面只填手数，之后用编辑补充）；建仓日缺省用最近交易日；
        - entry_mean 取当前窗口均值（无行情 → 0），与真实开仓口径一致；
        - 只改本地持仓与决策日志，不产生报单/成交流水（与人工编辑/删除一致）。
        """
        try:
            v = int(volume)
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        if self.pos.is_open:
            pos_side = self.pos.side
        else:
            name = str(side or "").upper()
            if name not in ("LONG", "SHORT"):
                return None                       # 空仓必须有方向才能建仓
            pos_side = Side[name]
        try:
            px = float(avg_price or 0.0)
        except (TypeError, ValueError):
            px = 0.0
        day = str(open_trading_day or "").strip() or self._last_trading_day
        entry_mean = self.window.mean() or 0.0
        if not self.pos.is_open:
            self.pos = Position(side=pos_side, tiers=[], entry_mean=entry_mean,
                                open_trading_day=day)
        tier = Tier(volume=v, avg_price=px, open_ts=time.time(),
                    open_trading_day=day, entry_mean=entry_mean)
        self.pos.tiers.append(tier)
        self._log_decision(time.time(), self._last_trading_day, 0.0, "持仓",
                           f"人工新增持仓层 {tier.tier_id}: {pos_side.name} {v}手"
                           f" @{px:.4f}")
        self._persist_position()
        return tier

    def edit_tier(self, tier_id: str, *, volume=None, avg_price=None,
                  open_trading_day=None) -> bool:
        """人工编辑持仓层（按 tier_id 定位）。volume<=0 → 移除该层。"""
        if not self.pos.is_open:
            return False
        for t in self.pos.tiers:
            if t.tier_id != tier_id:
                continue
            detail = []
            if volume is not None:
                t.volume = int(volume)
                detail.append(f"手数→{t.volume}")
            if avg_price is not None:
                t.avg_price = float(avg_price)
                detail.append(f"均价→{t.avg_price:.4f}")
            if open_trading_day:
                t.open_trading_day = str(open_trading_day)
                detail.append(f"建仓日→{t.open_trading_day}")
            if t.volume <= 0:
                self.pos.tiers.remove(t)
                if not self.pos.tiers:
                    self.pos = Position()
                self._log_decision(time.time(), self._last_trading_day, 0.0, "持仓",
                                   f"人工删除持仓层 {tier_id}")
            else:
                self._log_decision(time.time(), self._last_trading_day, 0.0, "持仓",
                                   f"人工编辑持仓层 {tier_id}: {' '.join(detail)}")
            self._persist_position()
            return True
        return False

    def remove_tier(self, tier_id: str) -> bool:
        """人工删除持仓层（按 tier_id）。"""
        if not self.pos.is_open:
            return False
        for t in self.pos.tiers:
            if t.tier_id == tier_id:
                self.pos.tiers.remove(t)
                if not self.pos.tiers:
                    self.pos = Position()
                self._log_decision(time.time(), self._last_trading_day, 0.0, "持仓",
                                   f"人工删除持仓层 {tier_id}")
                self._persist_position()
                return True
        return False

    def _persist_trade(self, trade: Trade) -> None:
        """成交按模式+交易日持久化（E1：data/<slug>/<sim_|real_>trades_<交易日>.jsonl）。"""
        if self._store is None or not trade.close_trading_day:
            return
        self._store.append_jsonl(
            self.cfg.slug,
            f"{self._mode_prefix()}trades_{trade.close_trading_day}.jsonl", {
                "side": trade.side.name, "volume": trade.volume,
                "slug": self.cfg.slug, "mode": self.cfg.mode,
                "ts": trade.close_ts,
                "entry_price": trade.entry_price, "exit_price": trade.exit_price,
                "open_ts": trade.open_ts, "close_ts": trade.close_ts,
                "open_trading_day": trade.open_trading_day,
                "close_trading_day": trade.close_trading_day,
                "gross_pnl": trade.gross_pnl, "fee": trade.fee,
                "net_pnl": trade.net_pnl, "reason": trade.exit_reason,
            })

    # ---------- 实时持仓持久化（重启自动加载，模拟/实盘独立文件） ----------
    def _position_filename(self) -> str:
        return f"{self._mode_prefix()}position.json"

    def _persist_position(self) -> None:
        """实时持仓按模式落盘（sim_position.json / real_position.json）。

        空仓时删除文件（不残留空快照），模式切换/重启后读不到即为无持仓。
        """
        if self._store is None:
            return
        pos = self.pos
        if not pos.is_open:
            self._store.remove_file(self.cfg.slug, self._position_filename())
            return
        self._store.write_json(self.cfg.slug, self._position_filename(), {
            "side": pos.side.name if pos.side else None,
            "entry_mean": pos.entry_mean,
            "open_trading_day": pos.open_trading_day,
            "tiers": [
                {"volume": t.volume, "avg_price": t.avg_price,
                 "open_ts": t.open_ts, "open_trading_day": t.open_trading_day,
                 "entry_mean": t.entry_mean, "tier_id": t.tier_id}
                for t in pos.tiers
            ],
        })

    def _read_mode_file(self, filename: str) -> list[dict]:
        """按模式前缀读取当日文件。

        无前缀旧文件（历史迁移数据，均为模拟时代产生）仅归 simulate 模式；
        real 模式不回退，避免读到模拟数据。
        """
        if self._store is None:
            return []
        recs = self._store.read_jsonl(self.cfg.slug, f"{self._mode_prefix()}{filename}")
        if recs:
            return recs
        if self.cfg.mode != "simulate":
            return []
        return self._store.read_jsonl(self.cfg.slug, filename)

    def _load_persisted(self) -> None:
        """启动加载（对齐需求：模式区分、重启自动加载）：
        实时持仓 + 当日成交/报单（网页显示恢复，跨重启不丢失）。"""
        if self._store is None:
            return
        # ---- 实时持仓 ----
        rec = self._store.read_json(self.cfg.slug, self._position_filename(), None)
        if rec is None and self.cfg.mode == "simulate":
            rec = self._store.read_json(self.cfg.slug, "position.json", None)  # 兼容旧文件（仅模拟）
        if rec:
            try:
                tiers = []
                for t in rec.get("tiers", []):
                    if int(t.get("volume", 0) or 0) <= 0:   # 防御：0 手层不入仓
                        continue
                    tier = Tier(t.get("volume", 0), t.get("avg_price", 0.0),
                                t.get("open_ts", 0.0), t.get("open_trading_day", ""),
                                t.get("entry_mean", 0.0))
                    tid = str(t.get("tier_id", "") or "")
                    if tid:
                        tier.tier_id = tid                  # 旧数据缺 tier_id → 保留自动生成值
                    tiers.append(tier)
                side_name = rec.get("side")
                side = Side[side_name] if side_name in ("LONG", "SHORT") else None
                if side and tiers:
                    self.pos = Position(
                        side=side, tiers=tiers,
                        entry_mean=float(rec.get("entry_mean", 0.0) or 0.0),
                        open_trading_day=rec.get("open_trading_day", "") or "")
            except (KeyError, TypeError, ValueError):
                self.pos = Position()
        # ---- 当日成交 / 报单（网页显示恢复） ----
        day = self._last_trading_day or time.strftime("%Y-%m-%d")
        for r in self._read_mode_file(f"trades_{day}.jsonl"):
            try:
                self.trades.append(Trade(
                    side=Side[r.get("side", "")], volume=int(r.get("volume", 0)),
                    entry_price=float(r.get("entry_price", 0.0)),
                    exit_price=float(r.get("exit_price", 0.0)),
                    open_ts=float(r.get("open_ts", 0.0)),
                    close_ts=float(r.get("close_ts", 0.0)),
                    open_trading_day=r.get("open_trading_day", "") or "",
                    close_trading_day=r.get("close_trading_day", "") or "",
                    gross_pnl=float(r.get("gross_pnl", 0.0)),
                    fee=float(r.get("fee", 0.0)),
                    exit_reason=r.get("reason", "") or "",
                ))
            except (KeyError, ValueError):
                continue
        # ---- 报单/挂单/变更流水（OrderBook 统一恢复：实时区 + 历史区 + 流水） ----
        self.ob.load(day)

    # ---------- K 线历史与缓存（对齐 BMR2 聚合系统：落盘 klines_cache.json） ----------
    def _mode_prefix(self) -> str:
        """模式文件前缀：模拟/实盘的 K 线缓存独立存储（对齐 BMR2）。"""
        return "sim_" if self.cfg.mode == "simulate" else "real_"

    def _load_klines_cache(self) -> None:
        """启动时从 klines_cache.json 恢复历史 K 线（对齐 BMR2 _load_state：
        补充 K 线/历史闭合 K 线跨重启保留，注入统计窗口聚合器使其生效）。"""
        if self._store is None:
            return
        cache = self._store.read_json(
            self.cfg.slug, f"{self._mode_prefix()}klines_cache.json", []) or []
        if not cache and self.cfg.mode == "simulate":
            # 兼容无前缀旧文件（仅模拟模式）
            cache = self._store.read_json(
                self.cfg.slug, "klines_cache.json", []) or []
        if not cache:
            return
        self.klines = sorted(cache, key=lambda k: k.get("ts", 0))[-500:]
        agg = self.kline_agg
        merged = {k["ts"]: k for k in agg.closed}
        for b in self.klines:
            merged[b["ts"]] = b
        agg.closed = sorted(merged.values(), key=lambda k: k["ts"])[-agg.max_history:]

    def _record_kline(self, closed: dict) -> None:
        """闭合 K 线入历史 + 落盘缓存（对齐 BMR2，同 ts 幂等 upsert）。

        由引擎 on_tick 喂 kline_agg 时闭合回调（统计窗口不参与）。"""
        if self._store is None:
            return
        ts = closed["ts"]
        merged = {k["ts"]: k for k in self.klines}
        merged[ts] = closed
        self.klines = sorted(merged.values(), key=lambda k: k["ts"])[-500:]
        self._store.write_json(self.cfg.slug,
                               f"{self._mode_prefix()}klines_cache.json", self.klines)

    def flush_kline(self) -> None:
        """立即闭合当前未闭合 K 线（停止采集/程序退出时调用，最后一根落盘）。
        幂等：无未闭合 K 线时为空操作。"""
        closed = self.kline_agg.flush()
        if closed is not None:
            self._record_kline(closed)

    def _flush_stale_kline(self) -> None:
        """心跳兜底：未闭合 K 线 bucket 窗口已结束且超过宽限仍无新 tick
        → 强制闭合落盘（收盘/断流场景，对齐 BMR2 KLINE_CLOSE_GRACE_SECONDS=5）。"""
        closed = self.kline_agg.flush_if_stale(time.time(), 5.0)
        if closed is not None:
            self._record_kline(closed)

    def supplement_klines(self, bars: list[dict]) -> int:
        """补充历史 K 线（SQL 导入，market/sql_kline_loader）。

        按时间点覆盖式合并（upsert）→ 排序限 500 根 → 落盘缓存；
        同步注入 K 线聚合器（kline_agg.closed），使 /klines 图表可见
        导入的历史。返回生效根数（新增 + 覆盖）。
        """
        if not bars:
            return 0
        old_ts = {k["ts"] for k in self.klines}
        merged = {k["ts"]: k for k in self.klines}
        added = covered = 0
        for b in bars:
            ts = int(b.get("ts") or 0)
            if not ts:
                continue
            if ts in old_ts:
                covered += 1
            else:
                added += 1
            merged[ts] = b
        if not merged:
            return 0
        self.klines = sorted(merged.values(), key=lambda k: k["ts"])[-500:]
        if self._store is not None:
            self._store.write_json(self.cfg.slug,
                                   f"{self._mode_prefix()}klines_cache.json", self.klines)
        # 同步 K 线聚合器：导入的闭合 K 线进入 kline_agg.closed（图表可见）
        agg = self.kline_agg
        agg_closed = {k["ts"]: k for k in agg.closed}
        for b in self.klines:
            agg_closed[b["ts"]] = b
        agg.closed = sorted(agg_closed.values(), key=lambda k: k["ts"])[-agg.max_history:]
        self._log_decision(time.time(), self._last_trading_day,
                           self.klines[-1]["close"] if self.klines else 0.0,
                           "无", f"补充K线：新增 {added} 根、覆盖 {covered} 根（累计 {len(self.klines)} 根）")
        return added + covered
