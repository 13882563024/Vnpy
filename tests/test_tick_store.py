"""``TickStore``（tick 只读查询层）的单元测试。

用临时 SQLite 造数据，不碰 ``~/.vntrader/database.db``。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from recorder.tick_store import TickStore  # noqa: E402

SCHEMA = """
create table dbtickdata (
    symbol text not null,
    exchange text not null,
    datetime text not null,
    last_price float,
    bid_price_1 float,
    ask_price_1 float,
    bid_volume_1 float,
    ask_volume_1 float,
    volume float,
    primary key (symbol, exchange, datetime)
);
create table dbtickoverview (
    symbol text not null,
    exchange text not null,
    start text,
    end text,
    count integer,
    primary key (symbol, exchange)
);
"""


class TestTickStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        connection = sqlite3.connect(str(self.db_path))
        connection.executescript(SCHEMA)
        rows = [
            ("OI701", "CZCE", "2026-10-28 21:03:26", 10270.0, 10270.0, 10271.0, 4, 25, 41761),
            ("OI701", "CZCE", "2026-10-28 21:03:27", 10271.0, 10271.0, 10272.0, 6, 12, 41770),
            ("OI703", "CZCE", "2026-10-28 21:03:26", 10000.0, 10000.0, 10001.0, 3, 8, 1234),
            ("a2701", "DCE", "2026-10-29 09:01:00.330000", 5390.0, 5390.0, 5391.0, 10, 10, 999),
        ]
        connection.executemany("insert into dbtickdata values (?,?,?,?,?,?,?,?,?)", rows)
        # overview 故意写虚高的 count，验证查询层不采信它
        connection.executemany(
            "insert into dbtickoverview values (?,?,?,?,?)",
            [("OI701", "CZCE", "2026-10-28 21:03:26", "2026-10-28 21:03:27", 999)],
        )
        connection.commit()
        connection.close()
        self.store = TickStore(self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_exists_and_describe(self) -> None:
        self.assertTrue(self.store.exists)
        self.assertIn("test.db", self.store.describe())

    def test_list_symbols_uses_real_rows(self) -> None:
        symbols = self.store.list_symbols()
        self.assertEqual([s.vt_symbol for s in symbols], ["OI701.CZCE", "OI703.CZCE", "a2701.DCE"])
        by_symbol = {s.symbol: s for s in symbols}
        # overview 写的是 999，实际只有 2 行 —— 必须以实际行数为准
        self.assertEqual(by_symbol["OI701"].count, 2)
        self.assertEqual(by_symbol["OI701"].start, "2026-10-28 21:03:26")
        self.assertEqual(by_symbol["OI701"].end, "2026-10-28 21:03:27")

    def test_total_ticks(self) -> None:
        self.assertEqual(self.store.total_ticks(), 4)

    def test_load_recent_ordering_and_limit(self) -> None:
        rows = self.store.load_recent("OI701", "CZCE", limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "2026-10-28 21:03:27", "应返回最新一条")

        rows = self.store.load_recent("OI701", "CZCE", limit=10)
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[0][0], rows[1][0], "应为时间倒序")

    def test_load_recent_without_exchange(self) -> None:
        rows = self.store.load_recent("OI701", "", limit=10)
        self.assertEqual(len(rows), 2)

    def test_load_recent_unknown_symbol(self) -> None:
        self.assertEqual(self.store.load_recent("NOPE", "CZCE"), [])

    def test_list_days(self) -> None:
        days = self.store.list_days()
        keys = [(d.day, d.symbol) for d in days]
        self.assertEqual(
            keys,
            [("2026-10-28", "OI701"), ("2026-10-28", "OI703"), ("2026-10-29", "a2701")],
        )
        self.assertEqual(days[0].count, 2)


class TestTickStoreMissingDatabase(unittest.TestCase):
    def test_missing_file_returns_empty(self) -> None:
        store = TickStore(Path(tempfile.gettempdir()) / "definitely_not_here_12345.db")
        self.assertFalse(store.exists)
        self.assertEqual(store.list_symbols(), [])
        self.assertEqual(store.total_ticks(), 0)
        self.assertEqual(store.load_recent("OI701"), [])
        self.assertEqual(store.list_days(), [])

    def test_empty_database_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "empty.db"
            connection = sqlite3.connect(str(db_path))
            try:
                connection.executescript(SCHEMA)
            finally:
                connection.close()
            store = TickStore(db_path)
            self.assertTrue(store.exists)
            self.assertEqual(store.list_symbols(), [])
            self.assertEqual(store.total_ticks(), 0)


if __name__ == "__main__":
    unittest.main()
