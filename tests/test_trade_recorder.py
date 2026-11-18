"""委托/成交 CSV 记录：去重、按交易日分目录、flush 与导出工具。"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.object import OrderData, TradeData  # noqa: E402

from recorder.trade_recorder import (  # noqa: E402
    ORDER_HEADER,
    TRADE_HEADER,
    TradeRecorder,
)
from utils.record_export import export_range, list_days  # noqa: E402


def make_order(orderid: str = "1", *, moment: datetime | None = None,
               status: Status = Status.ALLTRADED, traded: float = 1.0,
               symbol: str = "SP a2701&a2703") -> OrderData:
    return OrderData(
        symbol=symbol, exchange=Exchange.DCE, orderid=orderid,
        direction=Direction.LONG, offset=Offset.CLOSE, type=OrderType.LIMIT,
        price=-14.0, volume=1.0, traded=traded, status=status,
        datetime=moment or datetime(2026, 11, 12, 10, 0, 0),
        reference="CrossPeriod_A_take_profit", gateway_name="CTP",
    )


def make_trade(tradeid: str = "T1", *, moment: datetime | None = None,
               orderid: str = "1", symbol: str = "a2703",
               direction: Direction = Direction.LONG,
               offset: Offset = Offset.CLOSE,
               price: float = 5210.0, volume: float = 1.0) -> TradeData:
    return TradeData(
        symbol=symbol.split(".")[0], exchange=Exchange.DCE, orderid=orderid,
        tradeid=tradeid, direction=direction, offset=offset,
        price=price, volume=volume,
        datetime=moment or datetime(2026, 11, 12, 10, 0, 0), gateway_name="CTP",
    )


class TestTradeRecorder(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recorder = TradeRecorder(root=self.root, batch_size=1000)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _read(self, day: str, filename: str) -> list[dict]:
        path = self.root / day / filename
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_writes_order_and_trade_with_header(self) -> None:
        self.recorder.on_order(make_order())
        self.recorder.on_trade(make_trade())
        self.recorder.flush()

        orders = self._read("2026-11-12", "orders.csv")
        trades = self._read("2026-11-12", "trades.csv")
        self.assertEqual(list(orders[0]), list(ORDER_HEADER))
        self.assertEqual(list(trades[0]), list(TRADE_HEADER))
        self.assertEqual(orders[0]["vt_orderid"], "CTP.1")
        self.assertEqual(orders[0]["vt_symbol"], "SP a2701&a2703.DCE")
        self.assertEqual(orders[0]["status"], "全部成交")
        self.assertEqual(trades[0]["tradeid"], "T1")
        self.assertEqual(trades[0]["price"], "5210")

    def test_night_session_belongs_to_next_trading_day(self) -> None:
        night = datetime(2026, 11, 12, 21, 30, 0)     # 周四夜盘 → 周五交易日
        self.recorder.on_order(make_order(moment=night))
        self.recorder.flush()
        self.assertTrue((self.root / "2026-11-13" / "orders.csv").exists())
        self.assertFalse((self.root / "2026-11-12" / "orders.csv").exists())

    def test_duplicate_order_status_is_skipped(self) -> None:
        order = make_order()
        self.recorder.on_order(order)
        self.recorder.on_order(make_order())      # CTP 重推同一状态
        self.recorder.flush()

        rows = self._read("2026-11-12", "orders.csv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.recorder.stats.orders_skipped, 1)

    def test_status_transition_keeps_each_row(self) -> None:
        self.recorder.on_order(make_order(status=Status.SUBMITTING, traded=0))
        self.recorder.on_order(make_order(status=Status.ALLTRADED, traded=1))
        self.recorder.flush()
        rows = self._read("2026-11-12", "orders.csv")
        self.assertEqual([row["status"] for row in rows], ["提交中", "全部成交"])

    def test_duplicate_tradeid_is_skipped(self) -> None:
        self.recorder.on_trade(make_trade("T1"))
        self.recorder.on_trade(make_trade("T1"))
        self.recorder.flush()
        self.assertEqual(len(self._read("2026-11-12", "trades.csv")), 1)
        self.assertEqual(self.recorder.stats.trades_skipped, 1)

    def test_switching_trading_day_flushes_previous(self) -> None:
        self.recorder.on_order(make_order("1"))
        self.recorder.on_order(make_order(
            "2", moment=datetime(2026, 11, 13, 10, 0, 0)))
        self.recorder.flush()
        self.assertEqual(len(self._read("2026-11-12", "orders.csv")), 1)
        self.assertEqual(len(self._read("2026-11-13", "orders.csv")), 1)

    def test_disabled_kinds_write_nothing(self) -> None:
        recorder = TradeRecorder(root=self.root, record_orders=False,
                                 record_trades=False, batch_size=1000)
        recorder.on_order(make_order())
        recorder.on_trade(make_trade())
        recorder.flush()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_recorded_days_lists_sorted_desc(self) -> None:
        self.recorder.on_order(make_order("1"))
        self.recorder.on_order(make_order(
            "2", moment=datetime(2026, 11, 13, 10, 0, 0)))
        self.recorder.flush()
        self.assertEqual(self.recorder.recorded_days(),
                         ["2026-11-13", "2026-11-12"])


class TestExportTool(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "records"
        recorder = TradeRecorder(root=self.root, batch_size=1000)
        recorder.on_order(make_order("1", symbol="SP a2701&a2703"))
        recorder.on_order(make_order("2", symbol="SPD PK701&PK703"))
        recorder.on_trade(make_trade("T1"))
        recorder.on_order(make_order(
            "3", moment=datetime(2026, 11, 13, 10, 0, 0)))
        recorder.flush()
        self.out = Path(self.temp.name) / "exports"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_list_days_counts(self) -> None:
        rows = list_days(self.root)
        self.assertEqual([row[0] for row in rows], ["2026-11-13", "2026-11-12"])
        self.assertEqual(rows[1][1], 2)      # 12 号两笔委托
        self.assertEqual(rows[1][2], 1)      # 一笔成交

    def test_export_single_day(self) -> None:
        result = export_range(date(2026, 11, 12), date(2026, 11, 12),
                              root=self.root, out_dir=self.out, output=lambda *_: None)
        self.assertEqual(result, {"orders": 2, "trades": 1, "spread_trades": 0})
        self.assertTrue((self.out / "orders.csv").exists())

    def test_export_range_merges_days(self) -> None:
        result = export_range(date(2026, 11, 12), date(2026, 11, 13),
                              root=self.root, out_dir=self.out, output=lambda *_: None)
        self.assertEqual(result["orders"], 3)

    def test_export_with_match_filter(self) -> None:
        result = export_range(date(2026, 11, 12), date(2026, 11, 12),
                              root=self.root, out_dir=self.out,
                              match="PK", output=lambda *_: None)
        self.assertEqual(result["orders"], 1)
        with (self.out / "orders.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertIn("PK", rows[0]["vt_symbol"])

    def test_export_rejects_reversed_range(self) -> None:
        with self.assertRaises(ValueError):
            export_range(date(2026, 11, 13), date(2026, 11, 12),
                         root=self.root, out_dir=self.out, output=lambda *_: None)


class TestEnsureDay(unittest.TestCase):
    """启动时就把目录和表头建好，否则没有交易的日子看起来像"没输出"。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recorder = TradeRecorder(root=self.root, batch_size=1000)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_creates_three_files_with_header(self) -> None:
        day_dir = self.recorder.ensure_day(datetime(2026, 11, 12, 10, 0, 0))

        self.assertEqual(day_dir, self.root / "2026-11-12")
        for filename in ("orders.csv", "trades.csv", "spread_trades.csv"):
            path = day_dir / filename
            self.assertTrue(path.exists(), filename)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1, f"{filename} 应只有表头")

    def test_night_session_uses_next_trading_day(self) -> None:
        day_dir = self.recorder.ensure_day(datetime(2026, 11, 12, 21, 30, 0))
        self.assertEqual(day_dir.name, "2026-11-13")

    def test_existing_data_is_not_overwritten(self) -> None:
        day_dir = self.recorder.ensure_day(datetime(2026, 11, 12, 10, 0, 0))
        self.recorder.on_order(make_order())
        self.recorder.flush()

        self.recorder.ensure_day(datetime(2026, 11, 12, 15, 0, 0))   # 再次预创建

        rows = self._read(day_dir / "orders.csv")
        self.assertEqual(len(rows), 1)

    @staticmethod
    def _read(path: Path) -> list[dict]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))


class TestSpreadTradeSynthesis(unittest.TestCase):
    """两条腿合成一行价差成交 —— 用户要的"开仓价"就在这里。"""

    SPREADS = {"A_2701_2703": ("a2701.DCE", "a2703.DCE")}

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recorder = TradeRecorder(root=self.root, batch_size=1000,
                                      spreads=self.SPREADS)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _read(self, day: str = "2026-11-12") -> list[dict]:
        path = self.root / day / "spread_trades.csv"
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_two_legs_produce_one_spread_row(self) -> None:
        self.recorder.on_trade(make_trade("T1", symbol="a2701", orderid="9",
                                          direction=Direction.LONG,
                                          offset=Offset.OPEN, price=5210.0))
        self.recorder.on_trade(make_trade("T2", symbol="a2703", orderid="9",
                                          direction=Direction.SHORT,
                                          offset=Offset.OPEN, price=5224.0))
        self.recorder.flush()

        rows = self._read()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["spread"], "A_2701_2703")
        self.assertEqual(row["vt_orderid"], "CTP.9")
        self.assertEqual(row["direction"], "开多价差")
        self.assertEqual(row["offset"], "开")
        self.assertEqual(row["near_symbol"], "a2701.DCE")
        self.assertEqual(row["far_symbol"], "a2703.DCE")
        self.assertEqual(row["near_price"], "5210")
        self.assertEqual(row["far_price"], "5224")
        self.assertEqual(row["spread_price"], "-14")      # near - far
        self.assertEqual(row["volume"], "1")

    def test_single_leg_does_not_emit(self) -> None:
        self.recorder.on_trade(make_trade("T1", symbol="a2701", orderid="9"))
        self.recorder.flush()
        self.assertEqual(self._read(), [])

    def test_close_direction_is_labelled(self) -> None:
        # 平多价差：卖出 near、买入 far
        self.recorder.on_trade(make_trade("T1", symbol="a2701", orderid="9",
                                          direction=Direction.SHORT,
                                          offset=Offset.CLOSE, price=5224.0))
        self.recorder.on_trade(make_trade("T2", symbol="a2703", orderid="9",
                                          direction=Direction.LONG,
                                          offset=Offset.CLOSE, price=5238.0))
        self.recorder.flush()
        rows = self._read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "平多价差")
        self.assertEqual(rows[0]["spread_price"], "-14")

    def test_short_open_is_labelled(self) -> None:
        self.recorder.on_trade(make_trade("T1", symbol="a2701", orderid="9",
                                          direction=Direction.SHORT,
                                          offset=Offset.OPEN, price=5224.0))
        self.recorder.on_trade(make_trade("T2", symbol="a2703", orderid="9",
                                          direction=Direction.LONG,
                                          offset=Offset.OPEN, price=5238.0))
        self.recorder.flush()
        rows = self._read()
        self.assertEqual(rows[0]["direction"], "开空价差")

    def test_without_spread_config_no_synthesis(self) -> None:
        recorder = TradeRecorder(root=self.root, batch_size=1000)
        recorder.on_trade(make_trade("T1", symbol="a2701", orderid="9"))
        recorder.on_trade(make_trade("T2", symbol="a2703", orderid="9"))
        recorder.flush()
        self.assertEqual(self._read(), [])


if __name__ == "__main__":
    unittest.main()
