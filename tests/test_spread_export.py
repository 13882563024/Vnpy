"""价差 CSV 导出：交易日窗口、优先读库、回退合成、CSV 结构。"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.trader.constant import Exchange  # noqa: E402
from vnpy.trader.object import TickData  # noqa: E402

from utils.spread_config import SpreadConfig  # noqa: E402
from utils.spread_export import (  # noqa: E402
    CSV_HEADER,
    export_spread_day,
    trading_day_window,
)

DAY = date(2026, 11, 12)
NEAR, FAR = "a2701.DCE", "a2703.DCE"
SPREAD = "A_TEST"


def make_tick(symbol: str, exchange: Exchange, moment: datetime,
              bid: float, ask: float) -> TickData:
    return TickData(
        symbol=symbol, exchange=exchange, datetime=moment,
        last_price=(bid + ask) / 2, bid_price_1=bid, ask_price_1=ask,
        bid_volume_1=10, ask_volume_1=10, volume=100, gateway_name="TEST",
    )


class FakeDatabase:
    """``{(symbol, exchange): [tick]}`` 的内存库，接口与 vnpy 数据库一致。"""

    def __init__(self, rows: dict) -> None:
        self.rows = rows
        self.queries: list[tuple] = []

    def load_tick_data(self, symbol, exchange, start, end):
        self.queries.append((symbol, exchange, start, end))
        items = self.rows.get((symbol, exchange.value), [])
        return [tick for tick in items if start <= tick.datetime <= end]


def make_config() -> SpreadConfig:
    return SpreadConfig(name=SPREAD, legs=[NEAR, FAR], size=10, pricetick=1,
                        price_formula="near - far", min_volume=1)


class TestTradingDayWindow(unittest.TestCase):
    def test_thursday_includes_wednesday_night(self) -> None:
        start, end = trading_day_window(DAY)
        self.assertEqual(start, datetime(2026, 11, 11, 20, 0))     # 周三夜盘
        self.assertEqual(end.date(), DAY)
        self.assertEqual(end.hour, 23)

    def test_monday_includes_friday_night(self) -> None:
        start, _end = trading_day_window(date(2026, 11, 16))       # 周一
        self.assertEqual(start, datetime(2026, 11, 13, 20, 0))     # 周五夜盘


class TestExportSpreadDay(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self.temp.name) / "csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def read_rows(self, name: str) -> list[list[str]]:
        with (self.out / DAY.isoformat() / f"{name}.csv").open(encoding="utf-8") as handle:
            return list(csv.reader(handle))

    def test_uses_stored_spread_ticks_and_filters_by_trading_day(self) -> None:
        stored = [
            # 11-11 夜盘 → 属于交易日 11-12
            make_tick(SPREAD, Exchange.LOCAL, datetime(2026, 11, 11, 21, 0, 0, 500000), 99, 101),
            # 11-12 日盘 → 属于 11-12
            make_tick(SPREAD, Exchange.LOCAL, datetime(2026, 11, 12, 9, 0, 1, 0), 98, 100),
            # 11-12 收盘后（夜盘归 11-13）→ 不应导出
            make_tick(SPREAD, Exchange.LOCAL, datetime(2026, 11, 12, 21, 5, 0), 97, 99),
            # 11-11 日盘 → 属于 11-11，不应导出
            make_tick(SPREAD, Exchange.LOCAL, datetime(2026, 11, 11, 10, 0, 0), 95, 97),
        ]
        database = FakeDatabase({(SPREAD, "LOCAL"): stored})

        report = export_spread_day(DAY, self.out, database=database,
                                   configs={SPREAD: make_config()},
                                   output=lambda *_a, **_k: None)

        self.assertEqual(report.total_rows, 2)
        self.assertEqual(report.items[0].source, "stored")
        rows = self.read_rows(SPREAD)
        self.assertEqual(tuple(rows[0]), CSV_HEADER)
        self.assertEqual([row[0] for row in rows[1:]],
                         ["2026-11-11 21:00:00.500", "2026-11-12 09:00:01.000"])
        self.assertEqual([row[1] for row in rows[1:]], ["99", "98"])
        self.assertEqual([row[3] for row in rows[1:]], ["100", "99"])   # mid

    def test_falls_back_to_synthesis_when_no_stored_spread_ticks(self) -> None:
        legs = {
            (NEAR.split(".")[0], "DCE"): [
                make_tick("a2701", Exchange.DCE, datetime(2026, 11, 12, 9, 0, 0, 438000), 4000, 4001),
            ],
            (FAR.split(".")[0], "DCE"): [
                make_tick("a2703", Exchange.DCE, datetime(2026, 11, 12, 9, 0, 0, 470000), 3900, 3901),
            ],
        }
        report = export_spread_day(DAY, self.out, database=FakeDatabase(legs),
                                   configs={SPREAD: make_config()},
                                   output=lambda *_a, **_k: None)

        self.assertEqual(report.total_rows, 1, "两条腿同一时刻合成一条价差")
        self.assertEqual(report.items[0].source, "synth")
        rows = self.read_rows(SPREAD)
        # 合成出的两条腿时刻（.438/.470）落到同一个 0.5 秒格，只留最后一条
        self.assertEqual(rows[1][0], "2026-11-12 09:00:00.000")
        self.assertEqual(rows[1][1], "99")     # 4000 - 3901
        self.assertEqual(rows[1][2], "101")    # 4001 - 3900

    def test_stored_only_mode_does_not_synthesize(self) -> None:
        legs = {(NEAR.split(".")[0], "DCE"): [
            make_tick("a2701", Exchange.DCE, datetime(2026, 11, 12, 9, 0, 0), 4000, 4001),
        ]}
        report = export_spread_day(DAY, self.out, database=FakeDatabase(legs),
                                   configs={SPREAD: make_config()},
                                   source="stored", output=lambda *_a, **_k: None)
        self.assertEqual(report.total_rows, 0)
        self.assertIn("无数据", report.describe())

    def test_summary_is_written_for_every_spread(self) -> None:
        stored = [make_tick(SPREAD, Exchange.LOCAL, datetime(2026, 11, 12, 9, 0, 1), 99, 101)]
        report = export_spread_day(DAY, self.out,
                                   database=FakeDatabase({(SPREAD, "LOCAL"): stored}),
                                   configs={SPREAD: make_config()},
                                   output=lambda *_a, **_k: None)
        with (report.out_dir / "_summary.csv").open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["spread", "trading_day", "rows", "source", "file", "note"])
        self.assertEqual(rows[1][0], SPREAD)
        self.assertEqual(rows[1][1], "2026-11-12")
        self.assertEqual(rows[1][2], "1")
        self.assertEqual(rows[1][3], "stored")

    def test_names_filter_only_exports_requested(self) -> None:
        other = SpreadConfig(name="B_TEST", legs=[NEAR, FAR], size=10, pricetick=1,
                             price_formula="near - far", min_volume=1)
        stored = [make_tick(SPREAD, Exchange.LOCAL, datetime(2026, 11, 12, 9, 0, 1), 99, 101)]
        report = export_spread_day(DAY, self.out,
                                   names=[SPREAD],
                                   database=FakeDatabase({(SPREAD, "LOCAL"): stored}),
                                   configs={SPREAD: make_config(), "B_TEST": other},
                                   output=lambda *_a, **_k: None)
        self.assertEqual([item.name for item in report.items], [SPREAD])
        self.assertFalse((self.out / DAY.isoformat() / "B_TEST.csv").exists())


if __name__ == "__main__":
    unittest.main()
