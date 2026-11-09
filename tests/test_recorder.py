"""采集器与前置预检的单元测试。

不需要柜台：``TickRecorder`` 的数据库依赖可注入，前置预检只做本机 TCP 判断。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.trader.constant import Exchange  # noqa: E402
from vnpy.trader.object import TickData  # noqa: E402

from recorder.tick_recorder import MAX_RETRY_TICKS, TickRecorder  # noqa: E402
from run_recorder import collect_symbols, parse_host_port, probe_address  # noqa: E402
from utils.spread_config import SpreadConfig  # noqa: E402

MOMENT = datetime(2026, 11, 2, 10, 0, 0)
NEAR = "a2612.DCE"
FAR = "a2701.DCE"


def make_tick(symbol: str, offset_seconds: float = 0.0, bid: float = 4000.0,
              ask: float = 4001.0) -> TickData:
    exchange = Exchange[symbol.split(".")[1]]
    return TickData(
        symbol=symbol.split(".")[0],
        exchange=exchange,
        datetime=MOMENT + timedelta(seconds=offset_seconds),
        last_price=(bid + ask) / 2,
        bid_price_1=bid,
        ask_price_1=ask,
        bid_volume_1=10,
        ask_volume_1=10,
        volume=1000,
        gateway_name="TEST",
    )


class FakeDatabase:
    """记录每次 ``save_tick_data`` 调用，可注入异常。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[list[str], int, bool]] = []
        self.fail_times = fail_times

    def save_tick_data(self, ticks, stream: bool = False) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("磁盘写入失败")
        symbols = sorted({tick.vt_symbol for tick in ticks})
        self.calls.append((symbols, len(ticks), stream))
        return True

    def total_written(self) -> int:
        return sum(count for _, count, _ in self.calls)


class TestTickRecorder(unittest.TestCase):
    def setUp(self) -> None:
        self.clock_value = 0.0
        self.messages: list[str] = []

    def make(self, database=None, batch_size: int = 3, flush_interval: float = 2.0) -> TickRecorder:
        return TickRecorder(
            symbols=[NEAR, FAR],
            database=database or FakeDatabase(),
            batch_size=batch_size,
            flush_interval=flush_interval,
            output=self.messages.append,
            clock=lambda: self.clock_value,
        )

    def test_batch_size_triggers_flush(self) -> None:
        database = FakeDatabase()
        recorder = self.make(database, batch_size=3)

        recorder.on_tick(make_tick(NEAR, 0))
        recorder.on_tick(make_tick(NEAR, 1))
        self.assertEqual(database.calls, [])

        recorder.on_tick(make_tick(NEAR, 2))
        self.assertEqual(len(database.calls), 1)
        self.assertEqual(database.calls[0][1], 3)
        self.assertTrue(database.calls[0][2], "stream 模式才不会被 overview 全表 count 拖慢")

    def test_flush_interval_triggers_flush(self) -> None:
        database = FakeDatabase()
        recorder = self.make(database, batch_size=100, flush_interval=2.0)

        recorder.on_tick(make_tick(NEAR, 0))
        recorder.maybe_flush()
        self.assertEqual(database.calls, [])

        self.clock_value = 5.0
        recorder.on_tick(make_tick(NEAR, 1))
        self.assertEqual(len(database.calls), 1)

    def test_groups_by_symbol(self) -> None:
        """不同合约必须分开落库：save_tick_data 用首条 tick 更新 overview。"""
        database = FakeDatabase()
        recorder = self.make(database, batch_size=100)

        recorder.on_tick(make_tick(NEAR, 0))
        recorder.on_tick(make_tick(FAR, 0))
        recorder.flush()

        self.assertEqual(len(database.calls), 2)
        for symbols, count, _ in database.calls:
            self.assertEqual(len(symbols), 1)
            self.assertEqual(count, 1)

    def test_ticks_sorted_within_group(self) -> None:
        database = FakeDatabase()
        recorder = self.make(database, batch_size=100)
        recorder.on_tick(make_tick(NEAR, 3))
        recorder.on_tick(make_tick(NEAR, 1))
        recorder.on_tick(make_tick(NEAR, 2))

        captured: list[datetime] = []

        class Ordered(FakeDatabase):
            def save_tick_data(self, ticks, stream=False):  # noqa: D102
                captured.extend(tick.datetime for tick in ticks)
                return True

        recorder.database = Ordered()
        recorder.flush()
        self.assertEqual(captured, sorted(captured))

    def test_storage_copy_keeps_live_tick_intact(self) -> None:
        """真 vnpy_sqlite 会 pop 掉传入 tick 的 vt_symbol；不能污染 EVENT_TICK 的对象。"""
        class MutatingDatabase(FakeDatabase):
            def save_tick_data(self, ticks, stream=False):  # noqa: D102
                symbols = sorted({tick.vt_symbol for tick in ticks})
                for tick in ticks:
                    tick.__dict__.pop("vt_symbol", None)
                    tick.__dict__.pop("gateway_name", None)
                self.calls.append((symbols, len(ticks), stream))
                return True

        recorder = self.make(MutatingDatabase(), batch_size=100)
        tick = make_tick(NEAR, 0)
        recorder.on_tick(tick)
        recorder.flush()

        self.assertEqual(tick.vt_symbol, NEAR)
        self.assertEqual(tick.gateway_name, "TEST")
        self.assertEqual(recorder.stats.written, 1)
        self.assertEqual(recorder.stats.write_errors, 0)

    def test_failed_mutating_write_retries_without_losing_identity(self) -> None:
        class MutatingFailOnce(FakeDatabase):
            def save_tick_data(self, ticks, stream=False):  # noqa: D102
                for tick in ticks:
                    tick.__dict__.pop("vt_symbol", None)
                if self.fail_times > 0:
                    self.fail_times -= 1
                    raise RuntimeError("磁盘写入失败")
                return True

        database = MutatingFailOnce(fail_times=1)
        recorder = self.make(database, batch_size=100)
        tick = make_tick(NEAR, 0)
        recorder.on_tick(tick)
        recorder.flush()
        self.assertEqual(len(recorder._retry), 1)
        self.assertEqual(recorder._retry[0].vt_symbol, NEAR)

        recorder.flush()
        self.assertEqual(recorder.stats.written, 1)
        self.assertEqual(tick.vt_symbol, NEAR)

    def test_unsubscribed_and_invalid_filtered(self) -> None:
        database = FakeDatabase()
        recorder = self.make(database, batch_size=100)

        recorder.on_tick(make_tick("OI701.CZCE"))          # 未订阅
        empty = make_tick(NEAR)
        empty.bid_price_1 = empty.ask_price_1 = empty.last_price = 0
        recorder.on_tick(empty)                            # 无效（全 0）
        recorder.flush()

        self.assertEqual(database.calls, [])
        self.assertEqual(recorder.stats.dropped_unsubscribed, 1)
        self.assertEqual(recorder.stats.dropped_invalid, 1)
        self.assertEqual(recorder.stats.written, 0)

    def test_write_error_goes_to_retry_then_succeeds(self) -> None:
        database = FakeDatabase(fail_times=1)
        recorder = self.make(database, batch_size=100)

        recorder.on_tick(make_tick(NEAR, 0))
        recorder.flush()
        self.assertEqual(recorder.stats.write_errors, 1)
        self.assertEqual(len(recorder._retry), 1)
        self.assertTrue(any("写入" in msg for msg in self.messages))

        recorder.flush()
        self.assertEqual(recorder.stats.written, 1)
        self.assertEqual(len(recorder._retry), 0)

    def test_retry_overflow_drops_oldest(self) -> None:
        recorder = self.make(FakeDatabase(fail_times=1), batch_size=100)
        recorder.on_tick(make_tick(NEAR, 0))
        recorder.flush()

        # 手工塞爆重试队列
        recorder._retry.extend(make_tick(NEAR, index + 1) for index in range(MAX_RETRY_TICKS + 50))
        recorder.database = FakeDatabase(fail_times=1)
        recorder.flush()

        self.assertGreater(recorder.stats.dropped_overflow, 0)
        self.assertLessEqual(len(recorder._retry), MAX_RETRY_TICKS)

    def test_summary_counts_per_symbol(self) -> None:
        database = FakeDatabase()
        recorder = self.make(database, batch_size=100)
        for index in range(4):
            recorder.on_tick(make_tick(NEAR, index))
        for index in range(2):
            recorder.on_tick(make_tick(FAR, index))
        recorder.flush()

        summary = recorder.summary()
        self.assertIn("共收到 6 条 tick，已落库 6 条", summary)
        self.assertIn(NEAR, summary)
        self.assertEqual(recorder.stats.per_symbol[NEAR], 4)
        self.assertEqual(recorder.stats.per_symbol[FAR], 2)
        self.assertEqual(database.total_written(), 6)

    def test_progress_text(self) -> None:
        recorder = self.make(FakeDatabase(), batch_size=100)
        recorder.on_tick(make_tick(NEAR, 0))
        text = recorder.progress()
        self.assertIn("已录 0 条", text)
        self.assertIn("10:00:00", text)


class TestSnapGrid(unittest.TestCase):
    """落库时间网格：01.438 → 01.000、01.938 → 01.500。"""

    def test_snap_timestamp_floors_to_half_second(self) -> None:
        from recorder.tick_recorder import snap_timestamp

        base = datetime(2026, 11, 6, 21, 0, 1)
        self.assertEqual(snap_timestamp(base + timedelta(milliseconds=438)),
                         base)
        self.assertEqual(snap_timestamp(base + timedelta(milliseconds=938)),
                         base + timedelta(milliseconds=500))
        self.assertEqual(snap_timestamp(base + timedelta(milliseconds=999)),
                         base + timedelta(milliseconds=500))
        self.assertEqual(snap_timestamp(base + timedelta(milliseconds=0)),
                         base)
        self.assertEqual(snap_timestamp(base + timedelta(milliseconds=438), 0),
                         base + timedelta(milliseconds=438))  # 0 = 不归整

    def test_stored_timestamp_is_snapped_and_live_tick_untouched(self) -> None:
        captured: list[datetime] = []

        class Capture(FakeDatabase):
            def save_tick_data(self, ticks, stream=False):  # noqa: D102
                captured.extend(tick.datetime for tick in ticks)
                return True

        recorder = TickRecorder(symbols=[NEAR], database=Capture(), batch_size=100)
        tick = make_tick(NEAR)
        tick.datetime = MOMENT + timedelta(milliseconds=438)
        recorder.on_tick(tick)
        recorder.flush()

        self.assertEqual(captured, [MOMENT])
        self.assertEqual(tick.datetime, MOMENT + timedelta(milliseconds=438),
                         "EVENT_TICK 上的原对象不能被改时间")

    def test_same_half_second_bucket_keeps_last_quote(self) -> None:
        captured = []

        class Capture(FakeDatabase):
            def save_tick_data(self, ticks, stream=False):  # noqa: D102
                captured.extend(ticks)
                return True

        recorder = TickRecorder(symbols=[NEAR], database=Capture(), batch_size=100)
        first = make_tick(NEAR, bid=4000, ask=4001)
        first.datetime = MOMENT + timedelta(milliseconds=20)
        second = make_tick(NEAR, bid=4005, ask=4006)
        second.datetime = MOMENT + timedelta(milliseconds=470)
        recorder.on_tick(first)
        recorder.on_tick(second)
        recorder.flush()

        self.assertEqual(len(captured), 1, "同一 0.5 秒格只留一条")
        self.assertEqual(captured[0].datetime, MOMENT)
        self.assertEqual(captured[0].bid_price_1, 4005, "保留该格最新盘口")
        self.assertEqual(recorder.stats.collapsed, 1)


class TestSpreadRecording(unittest.TestCase):
    """合成价差也要落库：symbol=价差名、exchange=LOCAL、0.5 秒网格。"""

    def make_recorder(self, captured: list) -> TickRecorder:
        from utils.spread_synth import build_spreads

        class Capture(FakeDatabase):
            def save_tick_data(self, ticks, stream=False):  # noqa: D102
                captured.extend(ticks)
                return True

        config = SpreadConfig(name="A_TEST", legs=[NEAR, FAR], size=10, pricetick=1,
                              price_formula="near - far", min_volume=1)
        return TickRecorder(symbols=[NEAR, FAR], database=Capture(), batch_size=1000,
                            spreads=build_spreads([config]))

    def test_spread_tick_is_stored_on_half_second_grid(self) -> None:
        captured: list = []
        recorder = self.make_recorder(captured)

        near = make_tick(NEAR, bid=4000, ask=4001)
        near.datetime = MOMENT + timedelta(milliseconds=438)
        far = make_tick(FAR, bid=3900, ask=3901)
        far.datetime = MOMENT + timedelta(milliseconds=470)
        recorder.on_tick(near)
        recorder.on_tick(far)
        recorder.flush()

        spreads = [tick for tick in captured if tick.vt_symbol == "A_TEST.LOCAL"]
        self.assertEqual(len(spreads), 1, "两条腿的同一 0.5 秒格合成一条价差")
        spread = spreads[0]
        self.assertEqual(spread.datetime, MOMENT)
        self.assertEqual(spread.bid_price_1, 99)     # near_bid - far_ask = 4000 - 3901
        self.assertEqual(spread.ask_price_1, 101)    # near_ask - far_bid = 4001 - 3900
        # 第二条腿到齐才算得出盘口：第一次腿 tick 没有价差，第二次才有
        self.assertEqual(recorder.stats.spread_ticks, 1)

    def test_record_spreads_switch_disables_synthesis(self) -> None:
        captured: list = []
        recorder = self.make_recorder(captured)
        recorder.record_spreads = False
        recorder.spreads = {}
        recorder._spreads_by_leg.clear()

        recorder.on_tick(make_tick(NEAR))
        recorder.on_tick(make_tick(FAR))
        recorder.flush()
        self.assertFalse([t for t in captured if t.vt_symbol == "A_TEST.LOCAL"])


class TestAddressProbe(unittest.TestCase):
    def test_parse_host_port(self) -> None:
        self.assertEqual(parse_host_port("tcp://1.2.3.4:41213"), ("1.2.3.4", 41213))
        self.assertEqual(parse_host_port("1.2.3.4:10131"), ("1.2.3.4", 10131))
        self.assertEqual(parse_host_port("ssl://h:1"), ("h", 1))
        self.assertIsNone(parse_host_port("socks5://u:p@h:1080"))
        self.assertIsNone(parse_host_port(""))
        self.assertIsNone(parse_host_port("tcp://1.2.3.4"))

    def test_probe_localhost_closed_port(self) -> None:
        # 本机高位端口基本不会有人监听；只看返回类型，不断言具体值
        result = probe_address("tcp://127.0.0.1:9", timeout=0.5)
        self.assertIn(result, (True, False))

    def test_probe_unparsable_returns_none(self) -> None:
        self.assertIsNone(probe_address("socks5://user:pass@host:1080"))


class TestCollectSymbols(unittest.TestCase):
    def setUp(self) -> None:
        # 不依赖用户当前的 config/spreads.json（合约会被增删）
        configs = {
            "A_2611_2701": SpreadConfig(
                name="A_2611_2701", legs=["a2611.DCE", "a2701.DCE"],
                active_symbol="a2611.DCE"),
            "OI_701_703": SpreadConfig(
                name="OI_701_703", legs=["OI701.CZCE", "OI703.CZCE"],
                active_symbol="OI701.CZCE"),
        }
        patcher = patch("run_recorder.load_spread_configs", return_value=configs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults_to_all_spreads(self) -> None:
        symbols = collect_symbols([], "")
        self.assertIn("a2611.DCE", symbols)
        self.assertIn("OI701.CZCE", symbols)

    def test_filter_by_spread(self) -> None:
        symbols = collect_symbols(["A_2611_2701"], "")
        self.assertEqual(symbols, ["a2611.DCE", "a2701.DCE"])

    def test_extra_symbols_merged_and_deduplicated(self) -> None:
        symbols = collect_symbols(["A_2611_2701"], "a2611.DCE, extra2701.DCE")
        self.assertEqual(symbols, ["a2611.DCE", "a2701.DCE", "extra2701.DCE"])

    def test_unknown_spread_yields_only_extra(self) -> None:
        symbols = collect_symbols(["NOT_EXIST"], "x2701.DCE")
        self.assertEqual(symbols, ["x2701.DCE"])


class StoringDatabase:
    """既接受 ``save_tick_data`` 又能按 ``load_tick_data`` 读回（对齐 vnpy 数据库契约）。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[TickData]] = {}

    def save_tick_data(self, ticks, stream: bool = False) -> bool:
        for tick in ticks:
            key = (tick.symbol, tick.exchange.value)
            self.rows.setdefault(key, []).append(tick)
        return True

    def load_tick_data(self, symbol, exchange, start, end):
        rows = self.rows.get((symbol, exchange.value), [])
        return [tick for tick in rows if start <= tick.datetime <= end]


class TestRecorderToBacktest(unittest.TestCase):
    """端到端：录制 → 落库 → 读回 → 合成价差（这正是采集的目的）。"""

    def test_recorded_ticks_become_spread_ticks(self) -> None:
        import backtest.engine as backtest_engine
        from backtest.engine import build_spread_data, synthesize_spread_ticks
        from utils.spread_config import SpreadConfig

        database = StoringDatabase()
        recorder = TickRecorder(
            symbols=[NEAR, FAR], database=database, batch_size=100, output=lambda *_: None
        )

        for index in range(20):
            recorder.on_tick(make_tick(NEAR, index, bid=4000, ask=4001))
            recorder.on_tick(make_tick(FAR, index, bid=4100 + index, ask=4101 + index))
        recorder.flush()

        self.assertEqual(recorder.stats.written, 40)

        config = SpreadConfig(
            name="A_REC_TEST",
            legs=[NEAR, FAR],
            active_symbol=NEAR,
            size=10,
            pricetick=1,
        )
        spread = build_spread_data(config)

        original = backtest_engine.get_database
        backtest_engine.get_database = lambda: database
        try:
            spread_ticks, frames = synthesize_spread_ticks(
                spread, MOMENT - timedelta(minutes=1), MOMENT + timedelta(minutes=1),
                output=lambda *_: None,
            )
        finally:
            backtest_engine.get_database = original

        self.assertGreater(len(spread_ticks), 0)
        self.assertEqual(len(spread_ticks), len(frames))
        # 首条合成 tick：bid = near_bid − far_ask，ask = near_ask − far_bid
        first = spread_ticks[0]
        self.assertAlmostEqual(first.bid_price_1, 4000 - 4101)
        self.assertAlmostEqual(first.ask_price_1, 4001 - 4100)
        # 最后一帧两腿都在
        self.assertIn(NEAR, frames[-1])
        self.assertIn(FAR, frames[-1])


class TestRealSqliteRoundtrip(unittest.TestCase):
    """用真实 ``vnpy_sqlite`` 验证落库契约（字段兼容、读回、overview 归属）。

    把 peewee 的库文件指到临时目录，**不碰** ``~/.vntrader/database.db``。
    """

    def setUp(self) -> None:
        import tempfile

        import vnpy_sqlite.sqlite_database as sq

        self.sq = sq
        self.original_path = str(sq.db.database)
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        temp_path = Path(self.tmpdir.name) / "recorder_test.db"

        sq.db.close()
        sq.db.init(str(temp_path))
        # SqliteDatabase 构造时会自己 connect + create_tables
        self.database = sq.SqliteDatabase()

    def tearDown(self) -> None:
        self.sq.db.close()
        self.sq.db.init(self.original_path)
        self.tmpdir.cleanup()

    def test_write_and_read_back_both_legs(self) -> None:
        recorder = TickRecorder(
            symbols=[NEAR, FAR], database=self.database, batch_size=100, output=lambda *_: None
        )
        for index in range(5):
            recorder.on_tick(make_tick(NEAR, index))
            recorder.on_tick(make_tick(FAR, index))
        recorder.flush()
        self.assertEqual(recorder.stats.written, 10)

        start = MOMENT - timedelta(minutes=1)
        end = MOMENT + timedelta(minutes=1)
        near_rows = self.database.load_tick_data("a2612", Exchange.DCE, start, end)
        far_rows = self.database.load_tick_data("a2701", Exchange.DCE, start, end)

        self.assertEqual(len(near_rows), 5)
        self.assertEqual(len(far_rows), 5)
        self.assertAlmostEqual(near_rows[0].bid_price_1, 4000)
        self.assertAlmostEqual(near_rows[0].ask_price_1, 4001)

        # 分组落库的收益：两个合约的 overview 各自独立计数
        import vnpy_sqlite.sqlite_database as sq
        overviews = {row.symbol: row.count for row in sq.DbTickOverview.select()}
        self.assertEqual(overviews.get("a2612"), 5)
        self.assertEqual(overviews.get("a2701"), 5)

    def test_same_tick_upsert_does_not_duplicate(self) -> None:
        recorder = TickRecorder(
            symbols=[NEAR], database=self.database, batch_size=100, output=lambda *_: None
        )
        tick = make_tick(NEAR, 0)
        recorder.on_tick(tick)
        recorder.flush()
        recorder.on_tick(make_tick(NEAR, 0))     # 同一时间戳再写一次
        recorder.flush()

        rows = self.database.load_tick_data(
            "a2612", Exchange.DCE, MOMENT - timedelta(minutes=1), MOMENT + timedelta(minutes=1)
        )
        self.assertEqual(len(rows), 1, "同主键应 upsert 而不是插入重复行")


if __name__ == "__main__":
    unittest.main()
