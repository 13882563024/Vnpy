"""``TickRecorder``：把实时 tick 批量写入 vnpy 数据库。

落库口径（2026-11 起）：

1. **时间戳归到 0.5 秒网格**：秒内 <0.5s 归到 ``.000``，>=0.5s 归到 ``.500``
   （``01.438 → 01.000``、``01.938 → 01.500``），与交易所 0.5 秒快照对齐。
2. **同一个 0.5 秒格里只留一条**（后到的覆盖先到的，保留该格最新的盘口），
   所以单腿保持 0.5 秒一条，不会因为快照抖动写出重复时间戳。
3. **合成价差一并落库**：每来一条腿 tick 就用两腿最新盘口重算价差，
   再按同样的 0.5 秒网格落库（symbol=价差名，exchange=LOCAL，正是 vnpy 自带
   价差回测 ``load_tick_data(spread.name, Exchange.LOCAL)`` 认的口径）。

为什么不能"收到就存一条"：``vnpy_sqlite`` 的 ``save_tick_data`` 内部按 10 条一批做
upsert，还会顺带更新 ``dbtickoverview``；逐条调用会让每条 tick 都触发一次
「查询 overview + count 全表」，写入吞吐量会被拖到不可用。

一个**必须遵守的实现细节**（已核对 ``vnpy_sqlite/sqlite_database.py``）：``save_tick_data``
只用 ``ticks[0]`` 的 ``symbol/exchange`` 去更新 overview，所以**不同合约不能混在一批里存**，
否则 overview 的计数会全算到第一个合约头上。因此这里按 ``vt_symbol`` 分组后再分别调用。

另一个同样必须遵守的细节：``save_tick_data`` 会**原地修改**传入 tick 的 ``__dict__``
（``pop('vt_symbol')``、``pop('gateway_name')``，并把 ``datetime`` 转成 UTC）。实时 tick
与 ``EVENT_TICK`` 是同一批对象，直接传进去会让 GUI 稍后读到的 tick 缺字段、报
``'TickData' object has no attribute 'vt_symbol'``。所以落库前一律先复制。

写库失败不丢数据：失败的一批进入 ``_retry``，下一轮优先重试；堆积超过上限才丢弃最旧的
并告警（宁可少录，不可让内存无限增长）。
"""

from __future__ import annotations

import time
from collections import defaultdict
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from vnpy.trader.constant import Exchange
from vnpy.trader.database import BaseDatabase, get_database
from vnpy.trader.object import TickData

DEFAULT_BATCH_SIZE = 300
DEFAULT_FLUSH_INTERVAL = 2.0
MAX_RETRY_TICKS = 2000

#: 落库时间网格（毫秒）。0 = 不归整，按原始时间戳存。
DEFAULT_SNAP_MS = 500

#: 合成价差 tick 的 gateway 名（与 CTP 腿区分）。
SPREAD_GATEWAY = "SPREAD_SYNTH"


def snap_timestamp(moment: datetime, snap_ms: int = DEFAULT_SNAP_MS) -> datetime:
    """把时间戳向下取整到 ``snap_ms`` 网格：``01.438 → 01.000``、``01.938 → 01.500``。"""
    if not snap_ms or snap_ms <= 0:
        return moment
    step = int(snap_ms) * 1000                 # 微秒
    base = moment.replace(microsecond=0)
    return base + timedelta(microseconds=(moment.microsecond // step) * step)


@dataclass
class RecorderStats:
    """录制统计（``summary()`` 展示用）。"""

    received: int = 0
    written: int = 0
    dropped_invalid: int = 0
    dropped_unsubscribed: int = 0
    dropped_overflow: int = 0
    write_errors: int = 0
    pending: int = 0
    retry: int = 0
    collapsed: int = 0          # 同一 0.5 秒格内被合并掉的条数
    spread_ticks: int = 0       # 其中合成价差 tick 的条数
    per_symbol: dict[str, int] = field(default_factory=dict)
    first_tick_at: datetime | None = None
    last_tick_at: datetime | None = None


class TickRecorder:
    """按合约缓冲 + 批量落库，并维护逐合约的接收计数。

    ``symbols`` 为空列表表示**录所有收到的 tick**（GUI/策略进程里最常用）；
    非空时只录白名单内的合约。

    ``spreads`` 传入 ``{价差名: SpreadData}`` 时，会把合成价差也按同样的 0.5 秒
    网格落库（symbol=价差名，exchange=LOCAL）。
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        database: BaseDatabase | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        output=print,
        clock=time.time,
        spreads: dict | None = None,
        record_spreads: bool = True,
        snap_ms: int = DEFAULT_SNAP_MS,
    ) -> None:
        self.symbols: list[str] = list(dict.fromkeys(symbols or []))
        self.symbol_set: set[str] = set(self.symbols)
        self.database: BaseDatabase = database if database is not None else get_database()
        self.batch_size: int = max(1, int(batch_size))
        self.flush_interval: float = max(0.1, float(flush_interval))
        self.output = output
        self.clock = clock
        self.snap_ms: int = max(0, int(snap_ms))
        self.record_spreads: bool = bool(record_spreads)
        self.spreads: dict = dict(spreads or {}) if self.record_spreads else {}

        # 腿 → 用到它的价差（与 SpreadDataEngine 的 symbol_spread_map 同一思路）
        self._spreads_by_leg: dict[str, list] = defaultdict(list)
        for spread in self.spreads.values():
            for vt_symbol in getattr(spread, "legs", {}):
                self._spreads_by_leg[vt_symbol].append(spread)

        self._buffer: list[TickData] = []
        self._retry: list[TickData] = []
        # vt_symbol -> (已缓冲的那条 tick 的时间格, 在 _buffer 里的下标)
        self._buckets: dict[str, tuple[datetime, int]] = {}
        self._last_flush: float = clock()

        self.stats: RecorderStats = RecorderStats()
        self.stats.per_symbol = {symbol: 0 for symbol in self.symbols}

    @property
    def records_all(self) -> bool:
        return not self.symbol_set

    # ------------------------------------------------------------ 接收
    def on_tick(self, tick: TickData) -> None:
        self.stats.received += 1

        if self.symbol_set and tick.vt_symbol not in self.symbol_set:
            self.stats.dropped_unsubscribed += 1
            return

        if tick.datetime is None or not self._is_valid(tick):
            self.stats.dropped_invalid += 1
            return

        self._accept(tick)
        self._update_spreads(tick)

    def _accept(self, tick: TickData) -> None:
        """归整时间戳后入缓冲；同一 0.5 秒格里只保留最后一条。"""
        stored = self._normalize(tick)
        bucket = stored.datetime
        vt_symbol = stored.vt_symbol

        previous = self._buckets.get(vt_symbol)
        if previous is not None and previous[0] == bucket:
            self._buffer[previous[1]] = stored      # 覆盖：保留该格最新盘口
            self.stats.collapsed += 1
            self.stats.pending = len(self._buffer)
            return

        self._buckets[vt_symbol] = (bucket, len(self._buffer))
        self._buffer.append(stored)
        self.stats.per_symbol[vt_symbol] = self.stats.per_symbol.get(vt_symbol, 0) + 1
        if self.stats.first_tick_at is None:
            self.stats.first_tick_at = stored.datetime
        self.stats.last_tick_at = stored.datetime
        self.stats.pending = len(self._buffer)

        self.maybe_flush()

    def _normalize(self, tick: TickData) -> TickData:
        """复制一份并归整时间格（绝不改 EVENT_TICK 上的原对象）。"""
        stored = copy(tick)
        stored.datetime = snap_timestamp(tick.datetime, self.snap_ms)
        return stored

    # ------------------------------------------------------------ 价差合成
    def _update_spreads(self, tick: TickData) -> None:
        """用新到的腿 tick 重算相关价差，合格就按同一网格落库。"""
        for spread in self._spreads_by_leg.get(tick.vt_symbol, ()):
            leg = spread.legs.get(tick.vt_symbol)
            if leg is None:
                continue
            leg.update_tick(tick)
            if not spread.calculate_price():
                continue
            item = self._spread_tick(spread, tick.datetime)
            if item is None:
                continue
            self.stats.spread_ticks += 1
            self._accept(item)

    @staticmethod
    def _spread_tick(spread, moment: datetime) -> TickData | None:
        """把算好的价差盘口包成一条 TickData（symbol=价差名，exchange=LOCAL）。"""
        bid = float(getattr(spread, "bid_price", 0) or 0)
        ask = float(getattr(spread, "ask_price", 0) or 0)
        if not bid or not ask:
            return None
        return TickData(
            symbol=spread.name,
            exchange=Exchange.LOCAL,
            datetime=moment,
            name=spread.name,
            last_price=(bid + ask) / 2,
            bid_price_1=bid,
            ask_price_1=ask,
            bid_volume_1=float(getattr(spread, "bid_volume", 0) or 0),
            ask_volume_1=float(getattr(spread, "ask_volume", 0) or 0),
            gateway_name=SPREAD_GATEWAY,
        )

    @staticmethod
    def _is_valid(tick: TickData) -> bool:
        """丢弃明显空的推送（全 0 快照），保留脏报价交给策略过滤器处理。"""
        prices = (tick.last_price, tick.bid_price_1, tick.ask_price_1)
        return any(price and price > 0 for price in prices)

    # ------------------------------------------------------------ 落库
    def maybe_flush(self) -> int:
        """触发条件：缓冲满 ``batch_size``，或距上次落库超过 ``flush_interval``。"""
        if not self._buffer:
            return 0
        if len(self._buffer) >= self.batch_size or (self.clock() - self._last_flush) >= self.flush_interval:
            return self.flush()
        return 0

    def flush(self) -> int:
        """把缓冲 + 重试队列落库；返回本次成功写入的条数。"""
        pending = self._retry + self._buffer
        self._retry = []
        self._buffer = []
        self._buckets.clear()
        self._last_flush = self.clock()

        if not pending:
            self.stats.pending = 0
            return 0

        groups: dict[str, list[TickData]] = defaultdict(list)
        for tick in pending:
            groups[tick.vt_symbol].append(tick)

        written = 0
        for vt_symbol, ticks in groups.items():
            ticks.sort(key=lambda item: item.datetime)
            # vnpy_sqlite.save_tick_data 直接改传入 tick 的 __dict__：
            # 它 pop 掉 vt_symbol / gateway_name，并把 datetime 换成 UTC。
            # 这些 tick 与 EVENT_TICK 是同一批对象，GUI 队列稍后还要读 vt_symbol，
            # 不复制会导致 TickMonitor/TradingWidget 报 “TickData has no attribute vt_symbol”。
            storage = [copy(tick) for tick in ticks]
            try:
                ok = self.database.save_tick_data(storage, stream=True)
            except Exception as exc:  # noqa: BLE001 - 录制不能因为一次写库失败就退出
                self.stats.write_errors += 1
                self._push_retry(ticks)
                self.output(f"[警告] 写入 {vt_symbol} 失败（{exc!r}），{len(ticks)} 条转入重试")
                continue

            if ok is False:
                self.stats.write_errors += 1
                self._push_retry(ticks)
                self.output(f"[警告] 写入 {vt_symbol} 返回失败，{len(ticks)} 条转入重试")
                continue

            written += len(ticks)

        self.stats.written += written
        self.stats.pending = len(self._buffer)
        self.stats.retry = len(self._retry)
        return written

    def _push_retry(self, ticks: list[TickData]) -> None:
        self._retry.extend(ticks)
        overflow = len(self._retry) - MAX_RETRY_TICKS
        if overflow > 0:
            self._retry = self._retry[overflow:]
            self.stats.dropped_overflow += overflow
            self.output(f"[警告] 重试队列超过 {MAX_RETRY_TICKS} 条，丢弃最旧 {overflow} 条")

    # ------------------------------------------------------------ 汇总
    def summary(self) -> str:
        stats = self.stats
        lines = [
            f"共收到 {stats.received} 条 tick，已落库 {stats.written} 条"
            f"（未订阅 {stats.dropped_unsubscribed} / 无效 {stats.dropped_invalid} / "
            f"同格合并 {stats.collapsed} / 溢出丢弃 {stats.dropped_overflow} / "
            f"写库失败 {stats.write_errors} 次）",
            f"时间网格 {self.snap_ms or '原始'}ms；其中合成价差 {stats.spread_ticks} 条",
        ]
        if stats.first_tick_at and stats.last_tick_at:
            lines.append(f"时间范围：{stats.first_tick_at} ~ {stats.last_tick_at}")
        lines.append("逐合约条数：")
        symbols = [symbol for symbol in self.symbols if symbol in stats.per_symbol]
        extra = sorted(symbol for symbol in stats.per_symbol if symbol not in set(self.symbols))
        for symbol in symbols + extra:
            lines.append(f"  {symbol:<16} {stats.per_symbol.get(symbol, 0)}")
        return "\n".join(lines)

    def progress(self) -> str:
        stats = self.stats
        last = stats.last_tick_at.strftime("%H:%M:%S") if stats.last_tick_at else "—"
        return (
            f"已录 {stats.written} 条（缓冲 {len(self._buffer)}，重试 {len(self._retry)}），"
            f"最新 tick {last}"
        )
