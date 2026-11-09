"""跨期均值回归策略（vnpy_spreadtrading 载体）。

策略规则见 ``docs/plan/PLAN-spreadtrading-跨期均值回归策略实现.md``：

- 信号：``spread = near_mid - far_mid``（mid）；``mean`` 取最近 N 分钟、``sd`` 取最近 M 分钟；
- 分层加仓：第 i 层阈值 ``X_i = entry_k + layer_step × (i − 1)``；
- 平仓优先级：换月强平 → 逐层止损 → 时间止损 → 逐层止盈（预挂单成交）；
- **回归平仓被逐层止盈取代**（用户决策）：每层开仓成交后立即为**最新层**挂一张止盈单，
  目标价 = 层均价 + 方向 × (min_exit_pnl + 该层开平费用) ÷ (乘数 × 手数)。

执行层通过 ``extra["mode"]`` 在两腿 / 交易所组合单之间切换；策略侧不关心细节，
只实现 ``LayerExecutor`` 的四个方法（``open_layer`` / ``exit_layer`` / ``close_layer`` /
``cancel``）。

参数命名说明：方案 7 节里的 ``trading``（默认 False = 只算不发单）在本类中叫
``live_trading``，因为 ``SpreadStrategyTemplate.trading`` 已经是"策略已启动"的开关；
``setting`` 里若仍写 ``trading`` 会被当作 ``live_trading`` 的别名接受。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from vnpy.trader.constant import Direction, Status
from vnpy.trader.object import BarData, TickData

from vnpy_spreadtrading.template import SpreadStrategyTemplate

from execution.base import (
    AlgoJob,
    AlgoKind,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    PositionLayer,
    TierState,
    calc_target_price,
    entry_spread_price,
    exit_spread_price,
)
from fees.calculator import FeeCalculator, KIND_CLOSE, spec_from_record
from utils.calendar import (
    TradingSessions,
    trading_day_str,
    trading_days_between,
)
from utils.filters import FilterChain, FilterParams, LegSnapshot, SpreadSnapshot
from utils.risk_limits import account_usage, load_risk_limits
from utils.spread_config import (
    EXECUTOR_EXCHANGE_SPREAD,
    EXECUTOR_TWO_LEG,
    SpreadConfig,
    get_spread_config,
)
from utils.stat_window import StatWindow


@dataclass
class PendingAction:
    """待执行的动仓请求（等撤单/等在途算法结束后再发）。"""

    kind: str                              # "open" | "close"
    direction: int = 0
    volume: float = 0.0
    layer_index: int = 0
    tier_ids: list[str] = field(default_factory=list)
    reason: str = ""
    price: float = 0.0
    created_at: float = 0.0
    cancel_requested_at: float = 0.0
    last_warn_at: float = 0.0


class CrossPeriodMRStrategy(SpreadStrategyTemplate):
    """跨期均值回归 · 分层加仓 · 逐层止损 + 预挂止盈。"""

    author: str = "CrossPeriodMR"

    # ---------------- 信号 ----------------
    mean_window_minutes: int = 3
    sd_window_minutes: int = 30
    entry_k: float = 1.0
    layer_step: float = 0.3

    # ---------------- 分层 ----------------
    max_positions: int = 5
    layer_volume: float = 1.0

    # ---------------- 过滤器 ----------------
    width_ticks: float = 1.0
    sync_seconds: float = 0.5
    min_depth: float = 1.0
    vr_max: float = 0.0
    max_clean_width_ticks: float = 5.0
    max_clean_jump_ticks: float = 20.0

    # ---------------- 平仓 ----------------
    stop_loss: float = 10000.0
    max_hold_days: int = 10
    roll_entry_cutoff_days: int = 3
    roll_exit_days: int = 1

    # ---------------- 止盈 ----------------
    exit_preplace: bool = True
    min_exit_pnl_override: float = 0.0
    fee_buffer: float = 1.0
    target_round_mode: str = "conservative"

    # ---------------- 执行 ----------------
    executor: str = EXECUTOR_TWO_LEG
    spread_offset_flag_width: int = 1
    market_close_timeout: float = 5.0
    entry_partial_timeout: float = 10.0
    payup: int = 0

    # ---------------- 风控 ----------------
    max_exit_per_tick: int = 1
    close_only: bool = False

    # ---------------- 实盘 ----------------
    live_trading: bool = False
    trading_sessions: str = ""
    warmup_days: int = 0
    # 最小预热门槛：样本数与实际覆盖时长都达标才允许开仓/加仓
    warmup_min_samples: int = 30
    warmup_min_minutes: float = 5.0
    status_log_seconds: float = 60.0
    signal_log_seconds: float = 30.0

    parameters: list[str] = [
        "mean_window_minutes", "sd_window_minutes", "entry_k", "layer_step",
        "max_positions", "layer_volume",
        "width_ticks", "sync_seconds", "min_depth", "vr_max",
        "max_clean_width_ticks", "max_clean_jump_ticks",
        "stop_loss", "max_hold_days", "roll_entry_cutoff_days", "roll_exit_days",
        "exit_preplace", "min_exit_pnl_override", "fee_buffer", "target_round_mode",
        "executor", "spread_offset_flag_width", "market_close_timeout",
        "entry_partial_timeout", "payup",
        "max_exit_per_tick", "close_only",
        "live_trading", "trading_sessions", "warmup_days",
        "warmup_min_samples", "warmup_min_minutes",
        "status_log_seconds", "signal_log_seconds",
    ]

    variables: list[str] = [
        "direction_text", "position_volume", "layer_count", "spread_value",
        "mean", "sd", "trading_day", "executor_mode", "last_blocked",
        "daily_realized", "session_open", "paused", "pause_reconciliation_required",
    ]

    def __init__(self, strategy_engine, strategy_name, spread, setting: dict) -> None:
        super().__init__(strategy_engine, strategy_name, spread, setting)
        self._executor_explicit = isinstance(setting, dict) and "executor" in setting

        # 参数别名：方案 7 节里叫 trading
        if isinstance(setting, dict) and "trading" in setting and "live_trading" not in setting:
            self.live_trading = bool(setting["trading"])

        self.cfg: SpreadConfig = SpreadConfig(name=spread.name)
        self.fee_calc: FeeCalculator = FeeCalculator()
        self.filters: FilterChain = FilterChain(FilterParams())
        self.stat: StatWindow = StatWindow(
            mean_seconds=float(self.mean_window_minutes) * 60,
            sd_seconds=float(self.sd_window_minutes) * 60,
        )
        self.sessions: TradingSessions = TradingSessions(self.trading_sessions)

        self.size: float = 0.0
        self.pricetick: float = 0.0
        self.fee_spec = self.fee_calc.default_spec()
        self.stop_loss_per_tier: float = float(self.stop_loss) / max(1, int(self.max_positions))

        self.direction: int = 0
        self.layers: list[PositionLayer] = []
        self.closed_layers: deque[PositionLayer] = deque(maxlen=200)
        self.jobs: dict[str, AlgoJob] = {}
        self.exit_algoid: str = ""
        self.pending_open: PendingAction | None = None
        self.pending_close: PendingAction | None = None
        self._snapshot: SpreadSnapshot | None = None
        self._last_push_key: str = ""
        self._last_pos_check: float = 0.0

        self.trading_day: str = ""
        self.mean: float = 0.0
        self.sd: float = 0.0
        self.spread_value: float = 0.0
        self.daily_realized: float = 0.0
        self.last_blocked: str = ""

        self.direction_text: str = "空仓"
        self.position_volume: float = 0.0
        self.layer_count: int = 0
        self.executor_mode: str = EXECUTOR_TWO_LEG
        self.session_open: bool = True
        self._signal_log_at: dict[str, float] = {}
        self._blocked_log_at: dict[str, float] = {}
        self._in_advance: bool = False
        self._last_status_log: float = 0.0
        self._last_warmup_log: float = 0.0
        self._last_close_guard_log: float = 0.0
        self.last_margin_ratio: float | None = None
        # 只暂停策略动作，不触碰柜台在途委托；重启后仍须重新核对。
        self.paused: bool = False
        self.pause_reconciliation_required: bool = False
        self.pause_order_reports: dict[str, dict] = {}
        self.pause_trade_reports: dict[str, float] = {}
        self._pause_trade_baseline: dict[str, float] = {}
        self.pause_position_at: float = 0.0
        # 柜台已有、但本进程没有算法对象的平仓委托：orderid -> 占用它的层 tier_id
        # （"" 表示层归属不明确）。只要它们还活跃，就不再补挂止盈，避免重复下单。
        self.reserved_exit_orders: dict[str, str] = {}
        # 同理，已存在但本进程不认识的建仓/加仓委托：占住开仓槽位，避免重复加仓。
        self.reserved_open_orders: dict[str, str] = {}

    def _owned_algos(self) -> list:
        """查找本策略已有的真实算法（回测假引擎也可提供 algos）。"""
        spread_engine = getattr(self.strategy_engine, "spread_engine", None)
        algo_engine = getattr(spread_engine, "algo_engine", None)
        algos = getattr(algo_engine, "algos", None)
        if algos is None:
            algos = getattr(self.strategy_engine, "algos", {})
        return [algos[algoid] for algoid in list(self.algoids) if algoid in algos]

    def _order_by_id(self, orderid: str):
        """查 OMS 里的委托；查不到返回 None（状态不明）。"""
        main_engine = getattr(self.strategy_engine, "main_engine", None)
        getter = getattr(main_engine, "get_order", None)
        if not callable(getter):
            return None
        try:
            return getter(orderid)
        except Exception:
            return None

    def _reserved_open_active(self) -> bool:
        """柜台是否已有本进程不认识的建仓/加仓委托仍占着开仓槽位。

        与平仓槽位同样的口径：只由核对/同步或跨日归档释放，不凭回报终态自行解除。
        """
        return bool(self.reserved_open_orders)

    def _reserved_exit_active(self, layer) -> bool:
        """该层（或层归属不明的）柜台平仓委托是否仍占着止盈位。

        不做“凭回报终态自动释放”：委托成交/撤单后的剩余量必须靠核对确认。
        只有层本身已经消失（平仓/同步）时才释放，或由人工清除遗留委托。
        """
        tiers = {item.tier_id for item in self.layers}
        active = False
        for orderid, tier_id in list(self.reserved_exit_orders.items()):
            if tier_id and tier_id not in tiers:
                self.reserved_exit_orders.pop(orderid, None)
                continue
            if tier_id and layer is not None and tier_id != layer.tier_id:
                continue
            active = True
            if layer is not None and not layer.algoid:
                layer.algoid = orderid  # 仅用于展示“该层已有平仓单”
        return active

    def pause_strategy(self) -> None:
        """供 GUI 调用：立即阻断决策及算法发单/撤单，原有委托留在柜台。"""
        if self.paused:
            return
        self.paused = True
        self.pause_reconciliation_required = True
        self.pause_position_at = float(getattr(self.spread, "net_pos", 0) or 0)
        self.pause_order_reports.clear()
        self.pause_trade_reports.clear()
        self._pause_trade_baseline = {
            orderid: float(volume)
            for algo in self._owned_algos()
            for orderid, volume in getattr(algo, "order_trade_volume", {}).items()
        }
        self.pending_open = None
        self.pending_close = None
        for algo in self._owned_algos():
            pause = getattr(algo, "pause", None)
            if callable(pause):
                pause()
            self._record_pause_reports(algo)
        self.write_log("策略暂停：委托保留且可能成交；不再自动对冲/撤单/止损，恢复前须核对")
        self.put_event()

    def _record_pause_reports(self, algo) -> None:
        """按委托 ID 收集被动回报，含暂停期间的部分成交。"""
        for orderid, order in getattr(algo, "orders", {}).items():
            self.pause_order_reports[orderid] = {
                "algoid": algo.algoid,
                "status": getattr(getattr(order, "status", None), "value", ""),
                "volume": float(getattr(order, "volume", 0) or 0),
                "traded": float(getattr(order, "traded", 0) or 0),
            }
        for orderid, volume in getattr(algo, "order_trade_volume", {}).items():
            delta = float(volume) - self._pause_trade_baseline.get(orderid, 0.0)
            if delta > 1e-9:
                self.pause_trade_reports[orderid] = delta

    def get_pause_state(self) -> dict:
        """GUI/人工核对入口：订单、累计成交与本地层账，仅供展示，不代替柜台核对。"""
        if self.paused:
            for algo in self._owned_algos():
                self._record_pause_reports(algo)
        return {
            "paused": self.paused,
            "reconciliation_required": self.pause_reconciliation_required,
            "orders": dict(self.pause_order_reports),
            "trades_by_order": dict(self.pause_trade_reports),
            "spread_net_pos_at_pause": self.pause_position_at,
            "spread_net_pos_now": float(getattr(self.spread, "net_pos", 0) or 0),
            "layers": [(layer.tier_id, layer.direction, layer.volume) for layer in self.layers],
            "active_algoids": [algo.algoid for algo in self._owned_algos()
                               if algo.is_active()],
        }

    def resume_strategy(self, *, reconciled: bool = False) -> bool:
        """仅外部核对并显式确认后恢复；旧算法未结束则保持暂停。"""
        if not self.paused:
            return True
        if not reconciled:
            self.write_log("恢复被拒：须先核对柜台委托、两腿持仓、暂停期间成交和策略层账")
            return False
        state = self.get_pause_state()
        # 已被柜台委托占住槽位的委托/算法不再阻止恢复：它们由 reserved_* 防重，
        # 不会被再下一笔；其余在途委托仍须人工核对。
        reserved = set(self.reserved_exit_orders) | set(self.reserved_open_orders)
        trades = {key: value for key, value in state.get("trades_by_order", {}).items()
                  if key not in reserved}
        if trades:
            self.write_log("恢复被拒：暂停后有成交，须重新核对成交归属与逐层持仓")
            return False
        active_algos = [
            algoid for algoid in state["active_algoids"]
            if algoid not in reserved
            and algoid.replace("recovered_", "", 1) not in reserved
        ]
        pending = {key: value for key, value in state["orders"].items() if key not in reserved}
        if active_algos or any(
            report.get("status") not in (Status.ALLTRADED.value, Status.CANCELLED.value,
                                         Status.REJECTED.value)
            for report in pending.values()
        ):
            self.write_log("恢复被拒：旧算法或委托仍在途，请先人工处理柜台委托并等待回报")
            return False
        self.pending_open = None
        self.pending_close = None
        self._archive_stale_algos()
        self.pause_order_reports.clear()
        self.pause_trade_reports.clear()
        self._pause_trade_baseline.clear()
        self._last_push_key = ""
        self._rebuild_stat()
        self.pause_reconciliation_required = False
        self.paused = False
        self.write_log("核对确认后恢复策略；重新按最新行情决策")
        self.put_event()
        return True

    # ================================================================ 生命周期
    def on_init(self) -> None:
        self.write_log("策略初始化开始")
        self._load_static_config()
        self._refresh_contract_metrics()
        self._rebuild_filters()
        self._rebuild_stat()

        if self.warmup_days > 0:
            self.write_log(f"加载最近 {self.warmup_days} 天 tick 预热统计窗口")
            self.load_tick(int(self.warmup_days))

        self.write_log(
            f"初始化完成：价差={self.spread_name} 乘数={self.size:g} 最小变动={self.pricetick:g} "
            f"费率={self.fee_spec.unit}"
            f"({self.fee_spec.open:g}/{self.fee_spec.close_yesterday:g}/{self.fee_spec.close_today:g})"
        )
        self.write_log(
            f"执行器={self.executor} 主动腿={self.cfg.active_symbol or '未配置'} "
            f"组合合约={self.cfg.exchange_spread or '未配置'}"
        )
        if self.min_exit_pnl_override > 0:
            self.write_log(f"min_exit_pnl 使用覆盖值 {self.min_exit_pnl_override:g} 元（自适应关闭）")

    def on_start(self) -> None:
        self._rebuild_filters()
        if not self.live_trading:
            self.write_log(
                "当前为【只计算不发单】模式（live_trading=False）：日志会打印每层止盈目标价，"
                "不会真的报单"
            )
        if self.close_only:
            self.write_log("close_only=True：只平不开，忽略所有开仓信号")
        self.write_log(
            f"退出配置：只平仓={self.close_only} 预挂止盈={self.exit_preplace} "
            f"平仓超时={self.market_close_timeout:g}s 开仓部分成交超时={self.entry_partial_timeout:g}s"
        )
        self.write_log(
            f"策略启动 分层上限={self.max_positions} 层步长={self.layer_step:g} "
            f"止损/层={self.stop_loss_per_tier:g}元 时段={self.sessions.describe()}"
        )

    def start_algo(self, direction, price, volume, payup, interval, lock, extra) -> str:
        if self.paused:
            return ""
        return super().start_algo(direction, price, volume, payup, interval, lock, extra)

    def stop_algo(self, algoid: str) -> None:
        if not self.paused:
            super().stop_algo(algoid)

    def on_stop(self) -> None:
        if self.paused:
            self.write_log("策略暂停期间收到停止回调：不撤现有委托，请人工核对后再恢复")
            return
        self._cancel_all_algos("策略停止")
        self.write_log("策略已停止")

    def on_spread_data(self) -> None:
        if self.paused:
            return
        snapshot = self._build_snapshot()
        if snapshot is None:
            return

        moment = snapshot.datetime or datetime.now()
        self._snapshot = snapshot
        self.spread_value = snapshot.mid
        self._refresh_contract_metrics()
        self._handle_new_trading_day(moment)

        self.session_open = self.sessions.is_open(moment)
        self._advance_state(moment)

        if not self.session_open:
            # 非交易时段：不开不平，但仍要维护止盈挂单（例如收盘前补挂）
            self._maybe_preplace_exit(moment)
            self._refresh_variables()
            return

        # 只在可交易时段喂窗口：集合竞价与节假日的挂单快照会污染 mean/sd
        self._push_window(snapshot, moment)
        self.mean = self.stat.mean() or 0.0
        self.sd = self.stat.sd() or 0.0

        if self._warmup_ready():
            # 1) 平仓决策（每 tick 至多一条）
            if self._check_exit(snapshot, moment):
                self._refresh_variables()
                return

            # 2) 开仓 / 加仓（close_only 时只平不开）
            if not self.close_only:
                self._check_entry(snapshot, moment)
        else:
            # 预热不足只禁止开仓；止损/换月/时间止损仍需生效，避免冷启动裸奔。
            if self._check_exit(snapshot, moment):
                self._refresh_variables()
                return
            self._log_warmup(moment)

        # 3) 预挂止盈（含只平不开 / 重启后对新最新层的补挂）
        self._maybe_preplace_exit(moment)
        self._refresh_variables()
        self._maybe_log_status(snapshot, moment)

    def _maybe_log_status(self, snapshot: SpreadSnapshot, moment: datetime) -> None:
        """限频打印价差实时状态：验证「两腿 tick → 合成价差 → 策略输入」这条链是否在跑。

        纸面观察（``live_trading=False``）时这是唯一的活性指标 —— 价差不动就说明订阅或
        合成没work，比等信号来得直接。
        """
        interval = float(self.status_log_seconds or 0)
        if interval <= 0:
            return
        now = (moment or datetime.now()).timestamp()
        if self._last_status_log and now - self._last_status_log < interval:
            return
        self._last_status_log = now

        target = self.layers[-1].target_spread if self.layers else 0.0
        self.write_log(
            f"状态 价差={snapshot.mid:.3f} bid={snapshot.bid_price:.3f} ask={snapshot.ask_price:.3f} "
            f"mean={self.mean:.3f} sd={self.sd:.3f} 方向={self.direction_text} "
            f"层数={self.layer_count} 累计净利={self.daily_realized:.0f}元"
            + (f" 最新层止盈目标={target:.3f}" if target else "")
            + (f" 已用资金={self.last_margin_ratio:.1%}"
               if self.last_margin_ratio is not None else "")
        )

    def on_spread_tick(self, tick: TickData) -> None:
        """``load_tick`` 预热回调：只喂统计窗口，不做任何决策。"""
        if tick is None or not tick.datetime:
            return
        if tick.bid_price_1 <= 0 or tick.ask_price_1 <= 0:
            return
        mid = (tick.bid_price_1 + tick.ask_price_1) / 2
        self.stat.push(tick.datetime.timestamp(), mid)

    def on_spread_bar(self, bar: BarData) -> None:
        return

    def on_spread_pos(self) -> None:
        """价差持仓核对：策略记录的层合计必须与价差净仓一致。"""
        if not self.layers:
            return
        expected = self.direction * sum(layer.volume for layer in self.layers)
        net = self.spread.net_pos
        if abs(net - expected) < 1e-9:
            return
        now = time.time()
        if now - self._last_pos_check < 5:
            return
        self._last_pos_check = now
        self.write_log(f"持仓核对：价差净仓 {net:g} ≠ 策略记录 {expected:g}（请人工核对）")

    def on_spread_algo(self, algo) -> None:
        if self.paused:
            self._record_pause_reports(algo)
            self.pause_reconciliation_required = True
            return
        self._handle_algo(algo)
        self._refresh_variables()

    # ================================================================ 配置与组件
    def _load_static_config(self) -> None:
        config = get_spread_config(self.spread_name)
        near_symbol = self.spread.variable_symbols.get("near", "")
        far_symbol = self.spread.variable_symbols.get("far", "")

        if config is None:
            config = SpreadConfig(
                name=self.spread_name,
                legs=[near_symbol, far_symbol],
                active_symbol=self.spread.active_leg.vt_symbol,
            )
            self.write_log(
                f"config/spreads.json 未找到价差 {self.spread_name}，按价差自身信息运行"
                f"（executor 回落到 two_leg，stop_loss 用策略参数）"
            )
        else:
            if config.legs and config.legs != [near_symbol, far_symbol]:
                self.write_log(
                    f"警告：spreads.json 的 legs {config.legs} 与已注册价差 "
                    f"{[near_symbol, far_symbol]} 不一致，以注册价差为准"
                )
            for problem in config.validate():
                self.write_log(f"配置检查：{problem}")

        self.cfg = config
        if not self.trading_sessions:
            self.sessions = TradingSessions(config.trading_sessions)
        if not self.cfg.active_symbol:
            self.cfg.active_symbol = self.spread.active_leg.vt_symbol

        self.executor_mode = self._resolve_executor()
        table_spec = self.fee_calc.spec(near_symbol, far_symbol, slug=config.slug)
        if config.fee:
            self.fee_spec = spec_from_record(config.fee, table_spec)
            self.write_log(
                f"手续费使用自定义配置：{self.fee_spec.unit} "
                f"{self.fee_spec.open:g}/{self.fee_spec.close_yesterday:g}/"
                f"{self.fee_spec.close_today:g}"
            )
        else:
            self.fee_spec = table_spec

        # 止损：品种级（spreads.json）优先于策略参数，因为各品种推荐值不同（J=20000，其余 10000）
        stop_loss = float(self.cfg.stop_loss) if self.cfg.stop_loss > 0 else float(self.stop_loss)
        self.stop_loss_per_tier = stop_loss / max(1, int(self.max_positions))
        self.write_log(
            f"止损口径：整仓 {stop_loss:g} 元 ÷ {max(1, int(self.max_positions))} 层 "
            f"= 每层 {self.stop_loss_per_tier:g} 元"
        )

    # ------------------------------------------------------------ 旧持仓接管
    def _legacy_positions(self) -> list:
        main_engine = getattr(self.strategy_engine, "main_engine", None)
        getter = getattr(main_engine, "get_all_positions", None)
        if not callable(getter):
            return []
        try:
            return list(getter())
        except Exception:
            return []

    def _legacy_net_pos(self, leg) -> float:
        net = float(getattr(leg, "net_pos", 0) or 0)
        if abs(net) < 1e-9:
            net = float(getattr(self.spread, "leg_pos", {}).get(leg.vt_symbol, 0) or 0)
        return net

    def _legacy_leg_price(self, leg) -> float | None:
        """只接受柜台持仓缓存中的对应方向均价；不以行情价代替持仓成本。"""
        net = self._legacy_net_pos(leg)
        side = Direction.LONG if net > 0 else Direction.SHORT
        candidates = []
        for position in self._legacy_positions():
            if getattr(position, "vt_symbol", "") != leg.vt_symbol:
                continue
            direction = getattr(position, "direction", None)
            volume = abs(float(getattr(position, "volume", 0) or 0))
            price = float(getattr(position, "price", 0) or 0)
            if direction == side and volume > 0 and price > 0:
                candidates.append((price, volume))
        if candidates:
            total = sum(volume for _price, volume in candidates)
            if abs(total - abs(net)) < 1e-6:
                return sum(price * volume for price, volume in candidates) / total
        return None

    def can_adopt_legacy_position(self) -> bool:
        """判断当前价差净仓能否按两腿均价接管；不发起任何委托。"""
        if self.layers:
            return False
        net_pos = float(getattr(self.spread, "net_pos", 0) or 0)
        if abs(net_pos) < 1e-9:
            return False
        near = self.spread.legs.get(self.spread.variable_symbols.get("near", ""))
        far = self.spread.legs.get(self.spread.variable_symbols.get("far", ""))
        if near is None or far is None:
            return False
        near_net = self._legacy_net_pos(near)
        far_net = self._legacy_net_pos(far)
        if (abs(near_net - net_pos) > 1e-6
                or abs(far_net + net_pos) > 1e-6):
            return False
        if abs(near_net) < 1e-9:
            return False
        if abs(far_net) < 1e-9:
            return False
        if abs(abs(near_net) - abs(far_net)) > 1e-6:
            return False
        return self._legacy_leg_price(near) is not None and self._legacy_leg_price(far) is not None

    def adopt_legacy_position(self) -> bool:
        """把启动前已存在的价差净仓接管成第 1 层（标记 adopted），用于补挂止盈。

        不是无风险自动开仓：只在两腿仓位数量一致、能取得两腿均价时成立；
        接管后仍需人工核对，且只影响止损/止盈，不会反向开仓。
        """
        if not self.can_adopt_legacy_position():
            self.write_log("旧持仓接管失败：两腿仓位不一致或缺少持仓均价")
            return False
        net_pos = float(self.spread.net_pos)
        near = self.spread.legs[self.spread.variable_symbols["near"]]
        far = self.spread.legs[self.spread.variable_symbols["far"]]
        near_price = self._legacy_leg_price(near)
        far_price = self._legacy_leg_price(far)
        if near_price is None or far_price is None:
            return False
        entry = float(near_price) - float(far_price)
        if not entry:
            self.write_log("旧持仓接管失败：两腿均价无法折算价差")
            return False

        layer = PositionLayer(
            direction=DIRECTION_LONG if net_pos > 0 else DIRECTION_SHORT,
            volume=abs(net_pos),
            avg_spread=entry,
            near_price=float(near_price),
            far_price=float(far_price),
            open_trading_day=self.trading_day or "",
            open_datetime=datetime.now(),
            layer_index=1,
            entry_mean=self.mean,
            entry_sd=self.sd,
            adopted=True,
        )
        self.layers.append(layer)
        self.direction = layer.direction
        self._refresh_variables()
        self.write_log(
            f"接管旧持仓：{layer.describe()} 价差={entry:g} "
            f"（按柜台腿均价 {near.vt_symbol}={near_price:g} / {far.vt_symbol}={far_price:g}）"
        )
        self._maybe_preplace_exit(layer.open_datetime)
        return True

    def _resolve_executor(self) -> str:
        """显式策略参数优先，其余取注册表；组合不可用时拒绝隐式降级。"""
        requested = str(
            self.executor if self._executor_explicit else self.cfg.executor
        ).strip().lower()
        if requested == EXECUTOR_EXCHANGE_SPREAD:
            if not self.cfg.exchange_spread_vt_symbol:
                self.write_log("executor=exchange_spread 但缺组合合约代码：禁止发单，不降级两腿")
            return EXECUTOR_EXCHANGE_SPREAD
        if requested == EXECUTOR_TWO_LEG:
            return EXECUTOR_TWO_LEG
        self.write_log(f"不支持的 executor={requested!r}：禁止发单")
        return requested

    def _refresh_contract_metrics(self) -> None:
        """乘数 / 最小变动一律取**单腿合约**（组合合约 VolumeMultiple=0，见方案 R7）。"""
        near = self.spread.legs.get(self.spread.variable_symbols.get("near", ""))
        size = (near.size if near and near.size else 0) or self.cfg.size
        self.size = float(size or 0)
        pricetick = self.spread.pricetick or self.cfg.pricetick or (
            near.pricetick if near else 0
        )
        self.pricetick = float(pricetick or 0)

    def _rebuild_filters(self) -> None:
        params = FilterParams(
            width_ticks=float(self.width_ticks),
            sync_seconds=float(self.sync_seconds),
            min_depth=float(self.min_depth),
            volume_ratio_max=float(self.vr_max or self.cfg.vr_max or 0),
            max_clean_width_ticks=float(self.max_clean_width_ticks),
            max_clean_jump_ticks=float(self.max_clean_jump_ticks),
            roll_entry_cutoff_days=int(self.roll_entry_cutoff_days),
            roll_exit_days=int(self.roll_exit_days),
        )
        self.filters = FilterChain(params, self.pricetick)

    def _rebuild_stat(self) -> None:
        self.stat = StatWindow(
            mean_seconds=float(self.mean_window_minutes) * 60,
            sd_seconds=float(self.sd_window_minutes) * 60,
        )

    # ================================================================ 快照
    def _build_snapshot(self) -> SpreadSnapshot | None:
        near = self.spread.legs.get(self.spread.variable_symbols.get("near", ""))
        far = self.spread.legs.get(self.spread.variable_symbols.get("far", ""))
        if near is None or far is None:
            return None
        if not near.bid_volume or not far.bid_volume:
            return None

        return SpreadSnapshot.build(
            self._leg_snapshot(near),
            self._leg_snapshot(far),
            pricetick=self.pricetick,
            bid_volume=self.spread.bid_volume,
            ask_volume=self.spread.ask_volume,
        )

    @staticmethod
    def _leg_snapshot(leg) -> LegSnapshot:
        tick = leg.tick
        return LegSnapshot(
            vt_symbol=leg.vt_symbol,
            bid_price=leg.bid_price,
            ask_price=leg.ask_price,
            bid_volume=leg.bid_volume,
            ask_volume=leg.ask_volume,
            last_volume=float(getattr(tick, "volume", 0) or 0),
            pricetick=leg.pricetick,
            datetime=getattr(tick, "datetime", None),
            limit_up=float(getattr(tick, "limit_up", 0) or 0),
            limit_down=float(getattr(tick, "limit_down", 0) or 0),
        )

    def _push_window(self, snapshot: SpreadSnapshot, moment: datetime) -> None:
        """同一 tick 只入窗口一次（两腿各推一次 tick 会重复计算同一价差）。"""
        key = moment.isoformat() if moment else str(snapshot.mid)
        if key == self._last_push_key:
            return
        self._last_push_key = key
        self.stat.push((moment or datetime.now()).timestamp(), snapshot.mid)

    # ================================================================ 交易日
    def _handle_new_trading_day(self, moment: datetime) -> None:
        day = trading_day_str(moment)
        if day == self.trading_day:
            return

        previous = self.trading_day
        self.trading_day = day
        self.daily_realized = 0.0

        if previous:
            # CTP 委托当日有效：新交易日直接归档旧单（不查柜台、不发撤单）。
            # 也只有跨日才释放“柜台委托占位”；恢复策略时必须保留，否则会重复挂单。
            self.reserved_exit_orders.clear()
            self.reserved_open_orders.clear()
            self._archive_stale_algos()
            self.filters.reset()
            self.write_log(f"进入新交易日 {day}：归档旧委托，按当前持仓重挂止盈单")
            if self.layers:
                self._maybe_preplace_exit(moment)

    def _archive_stale_algos(self) -> None:
        self.algoids.clear()
        self.jobs.clear()
        self.exit_algoid = ""
        self.pending_open = None
        self.pending_close = None
        for layer in self.layers:
            layer.algoid = ""
            if layer.state == TierState.PENDING_RETRY:
                layer.state = TierState.OPEN

    # ================================================================ 平仓判定
    def _check_exit(self, snapshot: SpreadSnapshot, moment: datetime) -> bool:
        """按优先级检查平仓条件；每 tick 至多产生一条决策。"""
        if not self.layers:
            return False
        if self.pending_close is not None or self.pending_open is not None:
            return False
        if self._inflight_jobs((AlgoKind.OPEN,)):
            return False

        # 1) 换月强平（整仓）
        need, reason = self.filters.roll_exit_needed(self.cfg.symbol_pair, self.trading_day)
        if need:
            self._request_close([], reason or "换月强平", moment)
            return True

        exit_price = exit_spread_price(snapshot, self.direction)
        if exit_price and self.size > 0:
            # 2) 逐层止损
            worst: PositionLayer | None = None
            worst_pnl = 0.0
            for layer in self.layers:
                pnl = layer.unrealized_pnl(exit_price, self.size)
                if pnl <= -self.stop_loss_per_tier and (worst is None or pnl < worst_pnl):
                    worst = layer
                    worst_pnl = pnl
            if worst is not None:
                self._request_close(
                    [worst.tier_id],
                    f"止损 {worst.describe()} 浮亏 {worst_pnl:.0f}≤{-self.stop_loss_per_tier:.0f}",
                    moment,
                )
                return True

        # 3) 时间止损（整仓，按最早一层的开仓交易日算）
        first_day = min((layer.open_trading_day for layer in self.layers if layer.open_trading_day),
                        default="")
        held = trading_days_between(first_day, self.trading_day) if first_day else None
        if held is not None and int(self.max_hold_days) > 0 and held >= int(self.max_hold_days):
            self._request_close([], f"时间止损 持仓{held}日≥{self.max_hold_days}", moment)
            return True

        return False

    def _request_close(self, tier_ids: list[str], reason: str, moment: datetime) -> None:
        volume = sum(layer.volume for layer in self._select_layers(tier_ids))
        if volume <= 0:
            return
        if not self._close_covered(self.direction, volume):
            self._log_close_guard(None, reason)
            return
        self.pending_close = PendingAction(
            kind="close",
            direction=self.direction,
            volume=volume,
            tier_ids=list(tier_ids),
            reason=reason,
            created_at=(moment or datetime.now()).timestamp(),
        )
        self.write_log(f"平仓请求：{reason} 共{volume:g}手（先撤在挂单再发平仓单）")
        self._advance_state(moment)

    def _select_layers(self, tier_ids: list[str]) -> list[PositionLayer]:
        if not tier_ids:
            return [layer for layer in self.layers if layer.is_open]
        wanted = set(tier_ids)
        return [layer for layer in self.layers if layer.is_open and layer.tier_id in wanted]

    # ================================================================ 开仓判定
    def _warmup_ready(self) -> bool:
        """样本数和覆盖时长都达标才认为统计量可信。"""
        return self.stat.ready(
            min_samples=int(self.warmup_min_samples),
            min_seconds=float(self.warmup_min_minutes) * 60,
        )

    def _log_warmup(self, moment: datetime | None) -> None:
        now = (moment or datetime.now()).timestamp()
        if now - self._last_warmup_log < 30:
            return
        self._last_warmup_log = now
        self.write_log(
            f"预热中：样本 {self.stat.count}/{int(self.warmup_min_samples)}，"
            f"覆盖 {self.stat.span() / 60:.1f}/{float(self.warmup_min_minutes):g} 分钟；"
            f"暂不开仓（止损/时间止损仍有效）"
        )

    def _close_covered(self, direction: int, volume: float) -> bool:
        """发平仓单前确认“这笔仓位确实存在”。

        优先问柜台（``cabinet_position_check`` 由控制层注入，基于 OMS 持仓）；
        冷却期内没有柜台数据时只做本地净仓校验。宁可晚几秒挂单，也不发
        会被柜台以“平仓量超过持仓量”拒绝的委托。
        """
        volume = float(volume)
        if volume <= 0 or int(direction) == 0:
            return False
        net = float(getattr(self.spread, "net_pos", 0) or 0)
        if net and (net * int(direction) <= 0 or abs(net) + 1e-9 < volume):
            return False
        checker = getattr(self, "cabinet_position_check", None)
        if callable(checker):
            try:
                return bool(checker(int(direction), volume))
            except Exception as exc:      # 校验本身出错必须留痕，否则只会无声地永远不挂单
                now = time.time()
                if now - getattr(self, "_last_cover_error_log", 0.0) >= 60:
                    self._last_cover_error_log = now
                    self.write_log(f"柜台持仓校验异常，暂缓挂单：{exc}")
                return False
        return True

    def _log_close_guard(self, layer, reason: str = "") -> None:
        now = time.time()
        if now - self._last_close_guard_log < 30:
            return
        self._last_close_guard_log = now
        target = layer.describe() if layer is not None else reason
        self.write_log(
            f"暂缓挂平仓单：柜台未确认对应层级仓位（{target}）；"
            f"等待持仓刷新后自动重试"
        )

    def _check_entry(self, snapshot: SpreadSnapshot, moment: datetime) -> None:
        if self.pending_open is not None or self.pending_close is not None:
            return
        if self._inflight_jobs((AlgoKind.OPEN, AlgoKind.CLOSE)):
            return
        if self._reserved_open_active():
            return  # 柜台已有建仓/加仓委托，不能再下一笔
        if len(self.layers) >= int(self.max_positions):
            return

        mean, sd = self.stat.mean(), self.stat.sd()
        if mean is None or sd is None or sd <= 0:
            return

        layer_index = len(self.layers) + 1
        threshold_units = float(self.entry_k) + float(self.layer_step) * (layer_index - 1)
        band = threshold_units * sd

        if self.direction == 0:
            if snapshot.mid < mean - band:
                direction = DIRECTION_LONG
            elif snapshot.mid > mean + band:
                direction = DIRECTION_SHORT
            else:
                return
        else:
            direction = self.direction
            if direction > 0 and not snapshot.mid < mean - band:
                return
            if direction < 0 and not snapshot.mid > mean + band:
                return

        result = self.filters.entry_allowed(snapshot, self.trading_day)
        if not result.passed:
            self.last_blocked = result.blocked_by or ""
            self._log_blocked(self.last_blocked, (moment or datetime.now()).timestamp())
            return
        self.last_blocked = ""

        price = entry_spread_price(snapshot, direction)
        if not price:
            return

        self.pending_open = PendingAction(
            kind="open",
            direction=direction,
            volume=float(self.layer_volume),
            layer_index=layer_index,
            price=price,
            reason=(
                f"第{layer_index}层 {'做多' if direction > 0 else '做空'}价差 "
                f"spread={snapshot.mid:.3f} mean={mean:.3f} sd={sd:.3f} "
                f"阈值={threshold_units:.2f}sd"
            ),
            created_at=(moment or datetime.now()).timestamp(),
        )
        self._log_signal(self.pending_open, "开仓信号")
        self._advance_state(moment)

    def _log_signal(self, request: PendingAction, prefix: str) -> None:
        """信号日志限频：同一方向同一层 ``signal_log_seconds`` 内只打印一次。

        价差在阈值附近抖动时每 tick 都会满足条件，不限频会瞬间刷满日志。
        """
        interval = float(self.signal_log_seconds or 0)
        key = f"{prefix}:{request.kind}:{request.direction}:{request.layer_index}"
        now = request.created_at or time.time()
        if interval > 0:
            if now - self._signal_log_at.get(key, 0.0) < interval:
                return
            self._signal_log_at[key] = now
        self.write_log(f"{prefix}：{request.reason}")

    def _log_order_basis(self, kind: str, request: PendingAction, price: float,
                         algoid: str = "") -> None:
        """每笔报单打印可复核的决策依据：价格、统计量、阈值、盘口、风控与执行方式。"""
        snap = self._snapshot
        mean = float(self.stat.mean() or 0.0)
        sd = float(self.stat.sd() or 0.0)
        mid = float(snap.mid) if snap else 0.0
        z = (mid - mean) / sd if sd else 0.0
        width_ticks = (
            (snap.ask_price - snap.bid_price) / self.pricetick
            if snap and self.pricetick else 0.0
        )
        depth = (
            min(snap.near.bid_volume, snap.near.ask_volume,
                snap.far.bid_volume, snap.far.ask_volume)
            if snap else 0.0
        )
        threshold = (
            float(self.entry_k)
            + float(self.layer_step) * max(0, int(request.layer_index) - 1)
        )
        margin_text = ""
        if self.cfg.margin_per_lot > 0:
            margin_text = (
                f" 保证金/手={self.cfg.margin_per_lot:g}元"
                f" 本单保证金={self.cfg.margin_per_lot * request.volume:g}元"
            )
        fee_text = (
            f" 手续费={self.fee_spec.unit} "
            f"{self.fee_spec.open:g}/{self.fee_spec.close_yesterday:g}/"
            f"{self.fee_spec.close_today:g}"
        )
        self.write_log(
            f"报单依据[{kind}] {request.reason} | "
            f"价差 mid={mid:.3f} bid={snap.bid_price if snap else 0:.3f} "
            f"ask={snap.ask_price if snap else 0:.3f} mean={mean:.3f} sd={sd:.3f} "
            f"z={z:+.2f} 阈值={threshold:.2f}sd 盘口宽={width_ticks:.1f}tick "
            f"最小深度={depth:g} 报单={request.volume:g}手@{price:g} "
            f"每层止损={self.stop_loss_per_tier:.0f}元{margin_text}{fee_text} "
            f"执行={self.executor_mode} "
            f"组合={self.cfg.exchange_spread or '—'} 策略={self.strategy_name}"
            + (f" 算法={algoid}" if algoid else "")
        )

    def _can_send(self) -> bool:
        """是否允许真的报单（``trading`` 是模板的启动开关，``live_trading`` 是发单开关）。"""
        return bool(self.trading and self.live_trading and not self.paused and
                    self.executor_mode in (EXECUTOR_TWO_LEG, EXECUTOR_EXCHANGE_SPREAD) and
                    (self.executor_mode != EXECUTOR_EXCHANGE_SPREAD or
                     self.cfg.exchange_spread_vt_symbol))

    def _margin_gate_reason(self) -> str:
        """全局已用资金上限；达到阈值后只拦截开仓/加仓，不影响平仓。"""
        limits = load_risk_limits()
        if not limits.enabled:
            return ""
        main_engine = getattr(self.strategy_engine, "main_engine", None)
        if main_engine is None:      # 回测/假引擎没有账户资金，不参与实盘风控
            return ""
        usage = account_usage(main_engine)
        if usage is None:
            return "无账户资金数据，无法确认已用资金比例"
        self.last_margin_ratio = usage.ratio
        if usage.ratio >= limits.max_margin_ratio:
            return (
                f"已用资金 {usage.ratio:.1%} ≥ 上限 {limits.max_margin_ratio:.0%}"
                f"（权益 {usage.balance:.0f} / 可用 {usage.available:.0f}）"
            )
        return ""

    def _log_risk_blocked(self, reason: str, now: float) -> None:
        key = "margin"
        if now - self._blocked_log_at.get(key, 0.0) < 60:
            return
        self._blocked_log_at[key] = now
        self.write_log(f"开仓被风控拦截：{reason}")

    def _log_blocked(self, reason: str, now: float) -> None:
        """过滤器拦截日志限频（同一原因 60 秒一次），避免每 tick 刷屏。"""
        if not reason:
            return
        key = reason.split(":", 1)[0]
        last = self._blocked_log_at.get(key, 0.0)
        if now - last < 60:
            return
        self._blocked_log_at[key] = now
        self.write_log(f"开仓被过滤：{reason}")

    def _active_algoids(self) -> list[str]:
        return [algoid for algoid in self.algoids if algoid]

    def _inflight_jobs(self, kinds: tuple[str, ...] | None = None) -> list[AlgoJob]:
        return [
            job for job in self.jobs.values()
            if not job.done and (kinds is None or job.kind in kinds)
        ]

    # ================================================================ 状态机
    def _advance_state(self, moment: datetime | None = None) -> None:
        """推进"先撤后发"的状态机（每个 tick、每次算法回报都调用）。

        撤单会同步触发 ``on_spread_algo`` → 可能重入本方法，因此用重入标记保护，
        由外层循环继续推进（最多 3 轮，避免同一 tick 内无限循环）。
        """
        if self.paused or self._in_advance:
            return
        self._in_advance = True
        try:
            now = (moment or datetime.now()).timestamp()
            for _ in range(3):
                if not self._advance_once(now, moment):
                    break
        finally:
            self._in_advance = False

    def _advance_once(self, now: float, moment: datetime | None) -> bool:
        """推进一步；返回 True 表示状态已推进（可再来一轮）。"""
        if self.pending_close is not None:
            request = self.pending_close
            algoids = self._active_algoids()
            if algoids:
                if not request.cancel_requested_at:
                    request.cancel_requested_at = now
                    self.write_log("平仓前先撤在挂单，等撤单确认后再发平仓单")
                for algoid in algoids:
                    self.stop_algo(algoid)
                if now - request.cancel_requested_at >= 3 and now - request.last_warn_at >= 3:
                    request.last_warn_at = now
                    self.write_log("警告：撤单确认超过 3s 未完成，暂缓平仓（避免仓已平、单还在）")
                # 返回 True：让外层再推进一轮（撤单可能已同步完成，随即可以发平仓单）
                return True
            self._dispatch_close(moment)
            return True

        if self.pending_open is not None:
            request = self.pending_open
            algoids = self._active_algoids()
            if algoids:
                # 加仓前先撤止盈单，让出挂单槽位
                if not request.cancel_requested_at:
                    request.cancel_requested_at = now
                    self.write_log("加仓前撤掉在挂止盈单")
                for algoid in algoids:
                    self.stop_algo(algoid)
                return True
            self._dispatch_open(moment)
            return True

        return False

    def _cancel_all_algos(self, reason: str) -> None:
        algoids = self._active_algoids()
        if not algoids:
            return
        self.write_log(f"{reason}：撤销 {len(algoids)} 个在途算法")
        for algoid in algoids:
            self.stop_algo(algoid)

    def _dispatch_open(self, moment: datetime | None = None) -> None:
        request = self.pending_open
        if request is None:
            return
        self.pending_open = None

        if not self._can_send():
            self._log_compute_only(request)
            return

        risk_reason = self._margin_gate_reason()
        if risk_reason:
            self._log_risk_blocked(
                risk_reason, (moment or datetime.now()).timestamp())
            return

        algoid = self.open_layer(self.spread, request.direction, request.volume,
                                 request.layer_index, request.reason)
        if not algoid:
            self.write_log("开仓算法启动失败（未启动交易或 live_trading=False）")
            return

        self.jobs[algoid] = AlgoJob(
            algoid=algoid,
            kind=AlgoKind.OPEN,
            direction=request.direction,
            volume=request.volume,
            layer_index=request.layer_index,
            target_spread=request.price,
            reason=request.reason,
        )
        self.write_log(f"发出开仓算法 {algoid}：{request.reason} @ {request.price:g}")
        self._log_order_basis("开仓", request, request.price, algoid)

    def _log_compute_only(self, request: PendingAction) -> None:
        """只计算不发单：与信号日志共用同一限频 key，避免同一条信号打印两遍。"""
        prefix = "开仓信号" if request.kind == "open" else "平仓请求"
        self._log_signal(request, prefix)

    def _dispatch_close(self, moment: datetime | None = None) -> None:
        request = self.pending_close
        if request is None:
            return
        self.pending_close = None

        if not self._can_send():
            self._log_compute_only(request)
            return

        algoid = self.close_layer(self.spread, request.tier_ids, request.reason)
        if not algoid:
            self.write_log("平仓算法启动失败（未启动交易或 live_trading=False）")
            return

        self.jobs[algoid] = AlgoJob(
            algoid=algoid,
            kind=AlgoKind.CLOSE,
            direction=request.direction,
            volume=request.volume,
            tier_ids=list(request.tier_ids),
            target_spread=request.price,
            reason=request.reason,
        )
        price = request.price or (
            exit_spread_price(self._snapshot, self.direction) if self._snapshot else 0.0
        )
        self.write_log(f"发出平仓算法 {algoid}：{request.reason} 共{request.volume:g}手")
        self._log_order_basis("平仓", request, price, algoid)

    # ================================================================ 预挂止盈
    def _maybe_preplace_exit(self, moment: datetime | None = None) -> None:
        layer = self.layers[-1] if self.layers else None
        if layer is None:
            return

        target = self._calc_layer_target(layer)
        if target is None:
            return
        if abs(target - layer.target_spread) > 1e-9:
            layer.target_spread = target
            self.write_log(
                f"{layer.describe()} 止盈目标 {target:g}"
                f"（净利≥{self._layer_min_exit_pnl(layer):.0f}元 费用{self._layer_fee(layer):.2f}元）"
            )

        if not self.exit_preplace or not self._can_send():
            return
        if self._reserved_exit_active(layer):
            return  # 柜台已有该层平仓委托，绝不再挂第二张
        if not self._close_covered(layer.direction, layer.volume):
            self._log_close_guard(layer)
            return
        if self.exit_algoid or self.pending_close is not None or self.pending_open is not None:
            return
        if self._inflight_jobs((AlgoKind.OPEN, AlgoKind.CLOSE)):
            return
        if layer.state == TierState.PENDING_RETRY and layer.retry_count >= 3:
            self.write_log(f"止盈单连续挂出失败 3 次，停止重挂：{layer.describe()}（请人工处理）")
            return

        algoid = self.exit_layer(self.spread, layer)
        if not algoid:
            layer.state = TierState.PENDING_RETRY
            layer.retry_count += 1
            self.write_log(f"止盈单挂出失败（第 {layer.retry_count} 次），下一 tick 重试：{layer.describe()}")
            return

        layer.state = TierState.OPEN
        layer.algoid = algoid
        self.exit_algoid = algoid
        self.jobs[algoid] = AlgoJob(
            algoid=algoid,
            kind=AlgoKind.TAKE_PROFIT,
            direction=layer.direction,
            volume=layer.volume,
            layer_index=layer.layer_index,
            tier_id=layer.tier_id,
            target_spread=layer.target_spread,
            reason=f"预挂止盈 第{layer.layer_index}层",
        )
        self.write_log(f"挂出止盈单 {algoid}：{layer.describe()}")
        self._log_order_basis(
            "止盈",
            PendingAction(
                kind="take_profit",
                direction=layer.direction,
                volume=layer.volume,
                layer_index=layer.layer_index,
                reason=f"预挂止盈 第{layer.layer_index}层 层均价={layer.avg_spread:g} "
                       f"目标={layer.target_spread:g}",
                price=layer.target_spread,
            ),
            layer.target_spread,
            algoid,
        )

    def _calc_layer_target(self, layer: PositionLayer) -> float | None:
        if layer.volume <= 0 or self.size <= 0:
            return None
        return calc_target_price(
            layer.avg_spread,
            layer.direction,
            self._layer_min_exit_pnl(layer),
            self._layer_fee(layer),
            self.size,
            layer.volume,
            self.pricetick,
            self.target_round_mode,
        )

    def _layer_min_exit_pnl(self, layer: PositionLayer) -> float:
        if self.min_exit_pnl_override > 0:
            return float(self.min_exit_pnl_override)
        return self.fee_calc.min_exit_pnl(
            self.fee_spec,
            self.size,
            self.pricetick,
            near_price=layer.near_price,
            far_price=layer.far_price,
        )

    def _layer_fee(self, layer: PositionLayer) -> float:
        """该层「开 + 平」四条腿费用（元），按当日交易日判定平今/平昨。"""
        fee = self._layer_fee_for_volume(layer, layer.volume)
        layer.fee_oc = fee
        return fee

    # ================================================================ 算法回报
    def _handle_algo(self, algo) -> None:
        job = self.jobs.get(algo.algoid)
        if job is None or job.done:
            return

        status = getattr(algo, "status", None)
        if status == Status.ALLTRADED:
            job.done = True
            self._finish_job(algo, job, filled=True)
        elif status in (Status.CANCELLED, Status.REJECTED):
            job.done = True
            filled = abs(getattr(algo, "traded_volume", 0) or 0) > 0
            self._finish_job(algo, job, filled=filled)

        self._prune_jobs()
        self._advance_state(self._snapshot.datetime if self._snapshot else None)

    def _finish_job(self, algo, job: AlgoJob, filled: bool) -> None:
        volume = abs(getattr(algo, "traded_volume", 0) or 0)
        price = float(getattr(algo, "traded_price", 0) or job.target_spread or 0.0)

        if job.kind == AlgoKind.OPEN:
            self._on_open_filled(algo, job, volume, price, filled)
        elif job.kind == AlgoKind.TAKE_PROFIT:
            self._on_exit_filled(algo, job, volume, price, filled)
        else:
            self._on_close_filled(algo, job, volume, price, filled)

    def _on_open_filled(self, algo, job: AlgoJob, volume: float, price: float, filled: bool) -> None:
        if volume <= 0 or not filled:
            self.write_log(
                f"开仓未成交（{job.reason}）：algo {job.algoid} 成交 0 手，忽略本次信号"
            )
            return

        near_price, far_price = self._leg_prices_from_algo(algo, job.direction, price)
        layer = PositionLayer(
            direction=job.direction,
            volume=volume,
            avg_spread=price,
            near_price=near_price,
            far_price=far_price,
            open_trading_day=self.trading_day,
            open_datetime=self._snapshot.datetime if self._snapshot else datetime.now(),
            layer_index=len(self.layers) + 1,
            entry_mean=self.mean,
            entry_sd=self.sd,
        )
        self.layers.append(layer)
        self.direction = job.direction
        self._refresh_variables()

        self.write_log(
            f"开仓成交 {layer.describe()} 价差{price:g} 成交{volume:g}手"
            f"（腿价 {near_price:g}/{far_price:g}）"
        )
        # 成交后立即为最新层挂止盈
        self._maybe_preplace_exit(layer.open_datetime)

    def _on_exit_filled(self, algo, job: AlgoJob, volume: float, price: float, filled: bool) -> None:
        layer = self._find_layer(job.tier_id)
        if self.exit_algoid == algo.algoid:
            self.exit_algoid = ""
        if layer is None:
            self.write_log("止盈成交但找不到对应层（请人工核对）")
            return
        if volume <= 0 or not filled:
            self.write_log(f"止盈单未成交（{job.reason}），保持挂单等待目标价")
            layer.algoid = ""
            return

        closed = min(volume, layer.volume)
        realized = self._settle_layer(layer, closed, price, "逐层止盈")
        self.write_log(
            f"止盈成交 第{layer.layer_index}层 价差{price:g} 平{closed:g}手 净利{realized:.0f}元"
        )
        self._maybe_preplace_exit(self._snapshot.datetime if self._snapshot else None)

    def _on_close_filled(self, algo, job: AlgoJob, volume: float, price: float, filled: bool) -> None:
        layers = self._select_layers(job.tier_ids) or [
            layer for layer in self.layers if layer.is_open
        ]
        if volume <= 0 or not filled:
            self.write_log(f"平仓未成交（{job.reason}）：algo {job.algoid} 成交 0 手，保持持仓")
            return

        remaining = volume
        total = 0.0
        for layer in list(layers):
            if remaining <= 0:
                break
            closed = min(remaining, layer.volume)
            remaining -= closed
            total += self._settle_layer(layer, closed, price, job.reason)

        self.write_log(f"平仓成交 价差{price:g} 共{volume:g}手 净利{total:.0f}元（{job.reason}）")
        if not self.layers:
            self.direction = 0
            self.write_log("当前已空仓")

    def _settle_layer(self, layer: PositionLayer, volume: float, price: float,
                      reason: str) -> float:
        """结算（可能部分）平掉一层，返回该部分净利（元）。"""
        fee = self._layer_fee_for_volume(layer, volume)
        gross = layer.direction * (price - layer.avg_spread) * self.size * volume
        net = gross - fee

        original_volume = layer.volume
        layer.volume -= volume
        self.daily_realized += net

        if layer.volume <= 1e-9:
            layer.volume = original_volume      # 还原成交手数，便于归档展示
            layer.mark_closed(price, self._snapshot.datetime if self._snapshot else None, reason)
            self.layers.remove(layer)
            self.closed_layers.append(layer)
            if not self.layers:
                self.direction = 0
        return net

    def _layer_fee_for_volume(self, layer: PositionLayer, volume: float) -> float:
        close_kind = self.fee_calc.close_kind(layer.open_trading_day, self.trading_day) \
            if layer.open_trading_day else KIND_CLOSE
        breakdown = self.fee_calc.round_trip(
            self.fee_spec,
            self.size,
            volume,
            near_open_price=layer.near_price,
            far_open_price=layer.far_price,
            close_kind=close_kind,
            buffer=float(self.fee_buffer),
        )
        return breakdown.total

    def _leg_prices_from_algo(self, algo, direction: int, spread_price: float):
        """取该笔成交的两腿价格（手续费按腿价计算）。

        优先用算法记录的腿成交均价；回测/无腿成交时退化为当前快照腿价，
        再不行用传入的价差价格兜底。
        """
        near_symbol = self.spread.variable_symbols.get("near", "")
        far_symbol = self.spread.variable_symbols.get("far", "")
        prices: dict[str, float] = {}

        for symbol in (near_symbol, far_symbol):
            leg_traded = getattr(algo, "leg_traded", {}).get(symbol, 0) if hasattr(algo, "leg_traded") else 0
            leg_cost = getattr(algo, "leg_cost", {}).get(symbol, 0) if hasattr(algo, "leg_cost") else 0
            if leg_traded:
                prices[symbol] = abs(leg_cost / leg_traded)

        snapshot = self._snapshot
        if snapshot is not None:
            if near_symbol not in prices:
                prices[near_symbol] = (
                    snapshot.near.ask_price if direction > 0 else snapshot.near.bid_price
                )
            if far_symbol not in prices:
                prices[far_symbol] = (
                    snapshot.far.bid_price if direction > 0 else snapshot.far.ask_price
                )
        else:
            prices.setdefault(near_symbol, abs(spread_price))
            prices.setdefault(far_symbol, abs(spread_price))

        return float(prices.get(near_symbol, 0.0)), float(prices.get(far_symbol, 0.0))

    def _find_layer(self, tier_id: str) -> PositionLayer | None:
        for layer in self.layers:
            if layer.tier_id == tier_id:
                return layer
        return None

    def _prune_jobs(self) -> None:
        for algoid in [key for key, job in self.jobs.items() if job.done]:
            self.jobs.pop(algoid, None)

    # ================================================================ LayerExecutor
    def open_layer(self, spread, direction: int, volume: float,
                   layer_index: int, reason: str = "") -> str:
        if not self._can_send():
            return ""
        price = entry_spread_price(self._snapshot, direction) if self._snapshot else 0.0
        extra = self._build_extra(AlgoKind.OPEN, reason)
        if direction > 0:
            return self.start_long_algo(price, volume, int(self.payup), 0, False, extra)
        return self.start_short_algo(price, volume, int(self.payup), 0, False, extra)

    def exit_layer(self, spread, layer: PositionLayer) -> str:
        if not self._can_send():
            return ""
        extra = self._build_extra(AlgoKind.TAKE_PROFIT, f"预挂止盈 第{layer.layer_index}层")
        if layer.direction > 0:
            return self.start_short_algo(layer.target_spread, layer.volume,
                                         int(self.payup), 0, False, extra)
        return self.start_long_algo(layer.target_spread, layer.volume,
                                    int(self.payup), 0, False, extra)

    def close_layer(self, spread, tier_ids: list[str], reason: str = "") -> str:
        if not self._can_send():
            return ""
        layers = self._select_layers(tier_ids)
        volume = sum(layer.volume for layer in layers)
        if volume <= 0 or self._snapshot is None:
            return ""
        price = exit_spread_price(self._snapshot, self.direction)
        extra = self._build_extra(AlgoKind.CLOSE, reason)
        if self.direction > 0:
            return self.start_short_algo(price, volume, int(self.payup), 0, False, extra)
        return self.start_long_algo(price, volume, int(self.payup), 0, False, extra)

    def cancel(self, algoid: str) -> None:
        if not self.paused:
            self.stop_algo(algoid)

    def _build_extra(self, purpose: str, reason: str = "") -> dict:
        exchange_symbol = ""
        if self.executor_mode == EXECUTOR_EXCHANGE_SPREAD:
            exchange_symbol = self.cfg.exchange_spread_vt_symbol
        return {
            "mode": self.executor_mode,
            "purpose": purpose,
            "reason": reason,
            "exchange_spread": self.cfg.exchange_spread,
            "exchange_spread_vt_symbol": exchange_symbol,
            "offset_flag_width": int(self.spread_offset_flag_width),
            "market_close_timeout": float(self.market_close_timeout),
            "entry_partial_timeout": float(self.entry_partial_timeout),
        }

    # ================================================================ 展示
    def _refresh_variables(self) -> None:
        self.position_volume = sum(layer.volume for layer in self.layers)
        self.layer_count = len(self.layers)
        if self.direction > 0:
            self.direction_text = "多价差"
        elif self.direction < 0:
            self.direction_text = "空价差"
        else:
            self.direction_text = "空仓"

    def get_data(self) -> dict:
        self._refresh_variables()
        return super().get_data()
