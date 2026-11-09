"""按交易日把价差 tick 导出成 CSV。

取数口径：

1. **交易日窗口**：交易日 ``D`` = 前一工作日晚 20:00 起 ~ ``D`` 当天 23:59:59.999999。
   夜盘（20:55 之后）按 :func:`utils.calendar.trading_day_of` 归属 ``D``，周末自动
   向前跳到周五（11-12 周四的夜盘在 11-11 周三晚上）。
2. **优先用落库的价差 tick**（``symbol=价差名``、``exchange=LOCAL``，由
   :class:`recorder.tick_recorder.TickRecorder` 写入）；这批数据从 2026-11 起才存在。
3. **没有落库数据时回退到两腿合成**（``backtest.engine.synthesize_spread_ticks``，
   与策略/回测同一套口径），所以历史日期也能导。

输出：``<out>/<交易日>/<价差名>.csv``，列固定为
``datetime,bid,ask,mid,bid_volume,ask_volume``（本地时间、毫秒精度），另有一个
``_summary.csv`` 汇总每个价差导出了多少行、来自哪个来源。

导出行同样按**存储口径**归整：时间戳向下取整到 500ms 网格（``01.438 → 01.000``、
``01.938 → 01.500``），同一格只留最后一条盘口 —— 所以"库里存的"和"现合成的"导出来
是同一粒度，回测不会因为来源不同而失真。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from vnpy.trader.constant import Exchange
from vnpy.trader.database import BaseDatabase, get_database

from utils.calendar import trading_day_of
from utils.spread_config import load_spread_configs
from utils.spread_synth import build_spreads

DEFAULT_OUTPUT_ROOT = Path("exports") / "spread_csv"
CSV_HEADER = ("datetime", "bid", "ask", "mid", "bid_volume", "ask_volume")
#: 交易日窗口的起点：夜盘 21:00 开盘，往前留一小时给集合竞价与时钟误差。
NIGHT_START = time(20, 0)


@dataclass
class SpreadExport:
    """单个价差的导出结果。"""

    name: str
    rows: int = 0
    source: str = ""            # stored / synth
    path: Path | None = None
    note: str = ""


@dataclass
class ExportReport:
    """一次导出的汇总。"""

    day: date
    out_dir: Path
    start: datetime
    end: datetime
    snap_ms: int = 500
    items: list[SpreadExport] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(item.rows for item in self.items)

    @property
    def exported(self) -> list[SpreadExport]:
        return [item for item in self.items if item.rows]

    def describe(self) -> str:
        lines = [
            f"交易日 {self.day.isoformat()} 的价差 CSV 已导出到 {self.out_dir}",
            f"取数窗口 {self.start:%Y-%m-%d %H:%M} ~ {self.end:%Y-%m-%d %H:%M}，"
            f"价差 {len(self.exported)}/{len(self.items)} 个有数据，合计 {self.total_rows} 行"
            f"（时间网格 {self.snap_ms}ms，同格保留最后一条盘口）",
        ]
        for item in self.items:
            if item.rows:
                lines.append(f"  {item.name:<16} {item.rows:>7} 行  {item.source}"
                             + (f"  {item.note}" if item.note else ""))
            else:
                lines.append(f"  {item.name:<16} {'—':>7}    无数据"
                             + (f"  {item.note}" if item.note else ""))
        return "\n".join(lines)


def previous_weekday(day: date) -> date:
    """上一个工作日（不处理节假日）。"""
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def trading_day_window(day: date) -> tuple[datetime, datetime]:
    """交易日 ``day`` 的取数窗口（本地时间，左闭右闭）。"""
    start = datetime.combine(previous_weekday(day), NIGHT_START)
    end = datetime.combine(day, time(23, 59, 59, 999999))
    return start, end


def _format_moment(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def dedupe_to_grid(ticks, snap_ms: int = 500) -> list:
    """按存储口径归整：时间戳向下取整到 ``snap_ms`` 网格，同一格保留最后一条。

    与 :class:`recorder.tick_recorder.TickRecorder` 落库时完全一致 —— 这样"库里
    存的价差"和"现合成的价差"导出后是同一粒度，回测不会因为来源不同而失真。
    """
    from recorder.tick_recorder import snap_timestamp

    latest: dict[datetime, object] = {}
    for tick in ticks:
        moment = snap_timestamp(tick.datetime, snap_ms)
        tick.datetime = moment
        latest[moment] = tick
    return [latest[moment] for moment in sorted(latest)]


def _write_csv(path: Path, ticks) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for tick in ticks:
            bid = float(tick.bid_price_1 or 0)
            ask = float(tick.ask_price_1 or 0)
            writer.writerow((
                _format_moment(tick.datetime),
                f"{bid:g}",
                f"{ask:g}",
                f"{(bid + ask) / 2:g}",
                f"{float(tick.bid_volume_1 or 0):g}",
                f"{float(tick.ask_volume_1 or 0):g}",
            ))
    return len(ticks)


def _stored_ticks(name: str, start: datetime, end: datetime,
                  database: BaseDatabase) -> list:
    try:
        return list(database.load_tick_data(name, Exchange.LOCAL, start, end))
    except Exception:      # noqa: BLE001 - 查库失败就退回到合成
        return []


def _synth_ticks(spread, start: datetime, end: datetime, database: BaseDatabase,
                 output) -> list:
    from backtest.engine import synthesize_spread_ticks

    ticks, _frames = synthesize_spread_ticks(
        spread, start, end, output=output, database=database)
    return ticks


def export_spread_day(
    day: date,
    out_dir: Path | str | None = None,
    *,
    names: list[str] | None = None,
    source: str = "auto",
    database: BaseDatabase | None = None,
    configs: dict | None = None,
    snap_ms: int = 500,
    progress=None,
    output=print,
) -> ExportReport:
    """导出交易日 ``day`` 的全部（或指定）价差 tick。

    ``source``：``auto`` = 先读库里的价差 tick、没有就两腿合成；
    ``stored`` = 只读库；``synth`` = 一律现合成。
    ``configs`` 缺省读 ``config/spreads.json``（测试可注入）。
    ``progress(done, total, name)`` 每开始一个价差回调一次（GUI 后台线程用它刷进度）。
    """
    if source not in ("auto", "stored", "synth"):
        raise ValueError(f"source 只能是 auto/stored/synth，实际 {source!r}")

    database = database or get_database()
    configs = configs if configs is not None else load_spread_configs()
    wanted = [name for name in configs if not names or name in set(names)]
    spreads = build_spreads(configs[name] for name in wanted)
    start, end = trading_day_window(day)
    target = (Path(out_dir) if out_dir else DEFAULT_OUTPUT_ROOT) / day.isoformat()

    report = ExportReport(day=day, out_dir=target, start=start, end=end, snap_ms=snap_ms)
    total = len(wanted)
    for done, name in enumerate(sorted(wanted), start=1):
        if callback := progress:
            callback(done, total, name)
        item = SpreadExport(name=name)
        report.items.append(item)

        ticks = []
        if source in ("auto", "stored"):
            ticks = [tick for tick in _stored_ticks(name, start, end, database)
                     if trading_day_of(tick.datetime) == day]
            if ticks:
                item.source = "stored"
        if not ticks and source in ("auto", "synth"):
            spread = spreads.get(name)
            if spread is None:
                item.note = "配置不完整，无法合成"
                continue
            ticks = [tick for tick in _synth_ticks(spread, start, end, database, output)
                     if trading_day_of(tick.datetime) == day]
            if ticks:
                item.source = "synth"

        if not ticks:
            item.note = item.note or "窗口内没有 tick（未订阅或未采集）"
            continue
        ticks = dedupe_to_grid(ticks, snap_ms)
        item.path = target / f"{name}.csv"
        item.rows = _write_csv(item.path, ticks)

    _write_summary(report)
    return report


def _write_summary(report: ExportReport) -> None:
    report.out_dir.mkdir(parents=True, exist_ok=True)
    path = report.out_dir / "_summary.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("spread", "trading_day", "rows", "source", "file", "note"))
        for item in report.items:
            writer.writerow((
                item.name,
                report.day.isoformat(),
                item.rows,
                item.source,
                item.path.name if item.path else "",
                item.note,
            ))
