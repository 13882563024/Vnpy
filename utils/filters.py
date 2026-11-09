"""开仓过滤器链（方案 2.4）：脏数据 / 盘口宽度 / 报价同步 / 一档深度 / 成交量比例 / 临近到期。

全部是 **tick 级前置条件**，任一不满足则本次不开仓。设计要点：

- 与数据源解耦：过滤对象是 ``SpreadSnapshot``（两腿 ``LegSnapshot`` + 合成价差），
  实盘从 ``SpreadData`` 组装，回测从合成 tick 组装，测试直接构造。
- 脏数据里的"单腿 mid 瞬时跳变"需要上一 tick 的基准，链内部维护；连续
  ``confirm_count`` 次同向超限视为真实跳空并接受（避免一次错价把后续全判脏）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tzlocal import get_localzone_name

from vnpy.trader.utility import ZoneInfo

from utils.calendar import trading_days_to_contract

LOCAL_TZ = ZoneInfo(get_localzone_name())


def as_local(moment: datetime | None) -> datetime | None:
    """把 datetime 统一到本地时区。

    vnpy 里同一个 ``TickData.datetime`` 可能有两种形态：CTP 推送带 ``Asia/Shanghai``，
    而本地构造/回放（``backtest/engine.py``、数据库读回）常常是 naive。两者直接比较会抛
    ``TypeError: can't compare offset-naive and offset-aware datetimes`` —— 而这个异常会被
    ``SpreadStrategyEngine.call_strategy_func`` 吞掉并把策略**静默置为停止**，表面上只看得到
    "策略在跑却毫无反应"。所以这里统一：naive 视为本地时区。
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=LOCAL_TZ)
    return moment



@dataclass
class LegSnapshot:
    """单腿盘口快照。"""

    vt_symbol: str = ""
    bid_price: float = 0.0
    ask_price: float = 0.0
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    last_volume: float = 0.0
    pricetick: float = 0.0
    datetime: datetime | None = None
    limit_up: float = 0.0
    limit_down: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid_price <= 0 or self.ask_price <= 0:
            return 0.0
        return (self.bid_price + self.ask_price) / 2

    @property
    def width(self) -> float:
        if self.bid_price <= 0 or self.ask_price <= 0:
            return 0.0
        return self.ask_price - self.bid_price

    @property
    def quoted(self) -> bool:
        """一档买卖都有效。"""
        return self.bid_price > 0 and self.ask_price > 0


@dataclass
class SpreadSnapshot:
    """一次决策看到的两腿 + 合成价差快照。"""

    near: LegSnapshot
    far: LegSnapshot
    mid: float = 0.0
    bid_price: float = 0.0        # 平多 / 做空的成交价（near_bid - far_ask）
    ask_price: float = 0.0        # 做多 / 平空的成交价（near_ask - far_bid）
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    pricetick: float = 0.0
    datetime: datetime | None = None

    @classmethod
    def build(
        cls,
        near: LegSnapshot,
        far: LegSnapshot,
        *,
        pricetick: float = 0.0,
        bid_volume: float = 0.0,
        ask_volume: float = 0.0,
    ) -> "SpreadSnapshot":
        # 时区统一：naive 与 aware 混用会让 max()/减法直接抛 TypeError（见 as_local 说明）
        near.datetime = as_local(near.datetime)
        far.datetime = as_local(far.datetime)

        mid = (near.mid - far.mid) if (near.quoted and far.quoted) else 0.0
        bid_price = (near.bid_price - far.ask_price) if (near.quoted and far.quoted) else 0.0
        ask_price = (near.ask_price - far.bid_price) if (near.quoted and far.quoted) else 0.0
        moment = None
        if near.datetime and far.datetime:
            moment = max(near.datetime, far.datetime)
        elif near.datetime or far.datetime:
            moment = near.datetime or far.datetime
        return cls(
            near=near,
            far=far,
            mid=mid,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_volume=bid_volume,
            ask_volume=ask_volume,
            pricetick=pricetick or min(
                [tick for tick in (near.pricetick, far.pricetick) if tick > 0] or [0.0]
            ),
            datetime=moment,
        )


@dataclass
class FilterParams:
    """过滤器参数（默认值取推荐文档）。"""

    width_ticks: float = 1.0
    sync_seconds: float = 0.5
    min_depth: float = 1.0
    volume_ratio_max: float = 0.0          # 0 = 不启用
    max_clean_width_ticks: float = 5.0
    max_clean_jump_ticks: float = 20.0
    roll_entry_cutoff_days: int = 3
    roll_exit_days: int = 1
    jump_confirm_count: int = 3


@dataclass
class FilterResult:
    passed: bool = True
    blocked_by: str | None = None

    def block(self, reason: str) -> "FilterResult":
        self.passed = False
        self.blocked_by = reason
        return self


class FilterChain:
    """按方案 2.4 的顺序执行过滤器。"""

    def __init__(self, params: FilterParams | None = None, pricetick: float = 0.0) -> None:
        self.params: FilterParams = params or FilterParams()
        self.pricetick: float = float(pricetick)
        self._last_mid: dict[str, float] = {}
        self._jump_streak: dict[str, int] = {}
        self.last_blocked: str | None = None

    def reset(self) -> None:
        self._last_mid.clear()
        self._jump_streak.clear()
        self.last_blocked = None

    # ------------------------------------------------------------ 单项检查
    def clean_leg(self, leg: LegSnapshot) -> tuple[bool, str | None]:
        """单腿脏数据：无报价 / 买卖倒挂 / 宽度超限 / mid 瞬时跳变。"""
        params = self.params
        if not leg.quoted:
            return False, f"脏数据:{leg.vt_symbol} 无有效报价 bid={leg.bid_price} ask={leg.ask_price}"
        if leg.ask_price < leg.bid_price:
            return False, f"脏数据:{leg.vt_symbol} 买卖倒挂 bid={leg.bid_price} ask={leg.ask_price}"
        if leg.limit_up and leg.ask_price > leg.limit_up:
            return False, f"脏数据:{leg.vt_symbol} 报价超涨停 {leg.ask_price}>{leg.limit_up}"
        if leg.limit_down and leg.bid_price < leg.limit_down:
            return False, f"脏数据:{leg.vt_symbol} 报价超跌停 {leg.bid_price}<{leg.limit_down}"

        tick = leg.pricetick or self.pricetick
        width = leg.width
        if tick > 0 and params.max_clean_width_ticks > 0:
            if width > params.max_clean_width_ticks * tick + tick * 0.5:
                return False, (
                    f"脏数据:{leg.vt_symbol} 盘口宽度 {width:g} > "
                    f"{params.max_clean_width_ticks:g} tick"
                )

        mid = leg.mid
        previous = self._last_mid.get(leg.vt_symbol)
        if previous is not None and tick > 0 and params.max_clean_jump_ticks > 0:
            jump_ticks = abs(mid - previous) / tick
            if jump_ticks > params.max_clean_jump_ticks:
                streak = self._jump_streak.get(leg.vt_symbol, 0) + 1
                self._jump_streak[leg.vt_symbol] = streak
                if streak < max(1, params.jump_confirm_count):
                    return False, (
                        f"脏数据:{leg.vt_symbol} mid 跳变 {jump_ticks:.0f} tick > "
                        f"{params.max_clean_jump_ticks:g}"
                    )
                # 连续多次同向超限 → 认为是真实跳空，接受新基准
                self._last_mid[leg.vt_symbol] = mid
                self._jump_streak[leg.vt_symbol] = 0
                return True, None

        self._last_mid[leg.vt_symbol] = mid
        self._jump_streak[leg.vt_symbol] = 0
        return True, None

    def width_ok(self, snapshot: SpreadSnapshot) -> tuple[bool, str | None]:
        params = self.params
        if params.width_ticks <= 0:
            return True, None
        for leg in (snapshot.near, snapshot.far):
            tick = leg.pricetick or self.pricetick
            if tick <= 0:
                continue
            limit = params.width_ticks * tick
            if leg.quoted and leg.width > limit + tick * 0.5:
                return False, (
                    f"W{params.width_ticks:g}:{leg.vt_symbol} 盘口宽度 {leg.width:g} > {limit:g}"
                )
        return True, None

    def sync_ok(self, snapshot: SpreadSnapshot) -> tuple[bool, str | None]:
        """两腿报价时间差检查。

        **精度自适应**：CTP 对郑商所（CZCE）推送的 ``UpdateMillisec`` 恒为 0，时间戳只有秒级
        精度，两腿真实相差 500ms 的两笔 tick 会被记成相差 1 秒 —— 若直接按 ``sync_seconds``
        卡，CZCE 品种会被系统性误杀（实测 OI/RM 全部被拦）。因此当两腿时间戳都是整秒时，
        阈值放宽容纳 1 秒，这属于数据精度不足的补偿，方向上是"允许原本就该配对的报价"。
        """
        params = self.params
        if params.sync_seconds <= 0:
            return True, None
        near_time, far_time = snapshot.near.datetime, snapshot.far.datetime
        if near_time is None or far_time is None:
            return True, None       # 缺时间戳时退化为视为同步（与参考实现一致）

        limit = params.sync_seconds
        if near_time.microsecond == 0 and far_time.microsecond == 0:
            limit += 1.0

        delta = abs((near_time - far_time).total_seconds())
        if delta > limit:
            return False, f"S{params.sync_seconds:g}:两腿报价差 {delta:.3f}s"
        return True, None

    def depth_ok(self, snapshot: SpreadSnapshot) -> tuple[bool, str | None]:
        params = self.params
        if params.min_depth <= 0:
            return True, None
        volumes = [
            snapshot.near.bid_volume,
            snapshot.near.ask_volume,
            snapshot.far.bid_volume,
            snapshot.far.ask_volume,
        ]
        depth = min(volumes)
        if depth < params.min_depth:
            return False, f"D{params.min_depth:g}:一档最小挂量 {depth:g} < {params.min_depth:g}"
        return True, None

    def volume_ratio_ok(self, snapshot: SpreadSnapshot) -> tuple[bool, str | None]:
        limit = self.params.volume_ratio_max
        if limit <= 0:
            return True, None
        near_volume, far_volume = snapshot.near.last_volume, snapshot.far.last_volume
        if min(near_volume, far_volume) <= 0:
            return False, "VR:某腿成交量为 0"
        ratio = max(near_volume, far_volume) / min(near_volume, far_volume)
        if ratio > limit:
            return False, f"VR{limit:g}:两腿成交量比 {ratio:.1f}"
        return True, None

    def roll_entry_ok(self, symbols: tuple[str, str], trading_day: str) -> tuple[bool, str | None]:
        cutoff = self.params.roll_entry_cutoff_days
        if cutoff <= 0:
            return True, None
        for symbol in symbols:
            days = trading_days_to_contract(symbol, trading_day)
            if days is not None and days <= cutoff:
                return False, f"REC{cutoff}:{symbol} 距当月约 {days} 交易日"
        return True, None

    def roll_exit_needed(self, symbols: tuple[str, str], trading_day: str) -> tuple[bool, str | None]:
        days_limit = self.params.roll_exit_days
        if days_limit <= 0:
            return False, None
        for symbol in symbols:
            days = trading_days_to_contract(symbol, trading_day)
            if days is not None and days <= days_limit:
                return True, f"REX{days_limit}:{symbol} 下一交易日进当月（约 {days} 交易日）"
        return False, None

    # ------------------------------------------------------------ 组合
    def entry_allowed(self, snapshot: SpreadSnapshot, trading_day: str) -> FilterResult:
        """开仓前的完整过滤链，返回第一个不通过的原因。"""
        result = FilterResult()

        for leg in (snapshot.near, snapshot.far):
            ok, reason = self.clean_leg(leg)
            if not ok:
                self.last_blocked = reason
                return result.block(reason or "脏数据")

        for name, check in (
            ("width", lambda: self.width_ok(snapshot)),
            ("sync", lambda: self.sync_ok(snapshot)),
            ("depth", lambda: self.depth_ok(snapshot)),
            ("volume_ratio", lambda: self.volume_ratio_ok(snapshot)),
            ("roll_entry", lambda: self.roll_entry_ok(
                (snapshot.near.vt_symbol, snapshot.far.vt_symbol), trading_day)),
        ):
            ok, reason = check()
            if not ok:
                self.last_blocked = reason
                return result.block(reason or name)

        self.last_blocked = None
        return result
