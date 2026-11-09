"""T5：过滤器边界（恰好等于 / 刚好超过各一例）。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.calendar import (  # noqa: E402
    TradingSessions,
    in_call_auction,
    parse_contract_month,
    trading_day_of,
    trading_days_to_contract,
)
from utils.filters import FilterChain, FilterParams, LegSnapshot, SpreadSnapshot  # noqa: E402

MOMENT = datetime(2026, 11, 2, 10, 0, 0)


def leg(symbol: str = "A2611.DCE", bid: float = 4000.0, ask: float = 4001.0,
        bid_volume: float = 10, ask_volume: float = 10, volume: float = 1000,
        pricetick: float = 1.0, moment: datetime | None = None) -> LegSnapshot:
    return LegSnapshot(
        vt_symbol=symbol,
        bid_price=bid,
        ask_price=ask,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        last_volume=volume,
        pricetick=pricetick,
        datetime=moment or MOMENT,
    )


def snapshot(near: LegSnapshot | None = None, far: LegSnapshot | None = None) -> SpreadSnapshot:
    near = near or leg()
    far = far or leg("A2701.DCE", 4001.0, 4002.0)
    return SpreadSnapshot.build(near, far)


class TestCleanLeg(unittest.TestCase):
    def setup_chain(self, **kwargs) -> FilterChain:
        return FilterChain(FilterParams(**kwargs), pricetick=1.0)

    def test_missing_quote(self) -> None:
        chain = self.setup_chain()
        ok, reason = chain.clean_leg(leg(bid=0, ask=0))
        self.assertFalse(ok)
        self.assertIn("无有效报价", reason or "")

    def test_crossed_quote(self) -> None:
        chain = self.setup_chain()
        ok, reason = chain.clean_leg(leg(bid=4002, ask=4001))
        self.assertFalse(ok)
        self.assertIn("买卖倒挂", reason or "")

    def test_width_limit_boundary(self) -> None:
        chain = self.setup_chain(max_clean_width_ticks=5)
        ok, _ = chain.clean_leg(leg(bid=4000, ask=4005))       # 恰好 5 tick
        self.assertTrue(ok)

        chain = self.setup_chain(max_clean_width_ticks=5)
        ok, reason = chain.clean_leg(leg(bid=4000, ask=4007))  # 7 tick
        self.assertFalse(ok)
        self.assertIn("盘口宽度", reason or "")

    def test_jump_filter_and_confirm(self) -> None:
        chain = self.setup_chain(max_clean_jump_ticks=20, jump_confirm_count=3)
        ok, _ = chain.clean_leg(leg(bid=4000, ask=4001))
        self.assertTrue(ok)

        # 单笔跳变 100 tick → 过滤
        ok, reason = chain.clean_leg(leg(bid=4100, ask=4101))
        self.assertFalse(ok)
        self.assertIn("跳变", reason or "")

        # 连续第 2 次仍过滤
        ok, _ = chain.clean_leg(leg(bid=4100, ask=4101))
        self.assertFalse(ok)

        # 第 3 次视为真实跳空，接受并更新基准
        ok, _ = chain.clean_leg(leg(bid=4100, ask=4101))
        self.assertTrue(ok)

        # 基准已更新，回到 4100 附近不再判脏
        ok, _ = chain.clean_leg(leg(bid=4101, ask=4102))
        self.assertTrue(ok)


class TestWidthAndSync(unittest.TestCase):
    def test_width_boundary(self) -> None:
        chain = FilterChain(FilterParams(width_ticks=1), pricetick=1.0)
        ok, _ = chain.width_ok(snapshot(leg(bid=4000, ask=4001), leg("A2701.DCE", 4001, 4002)))
        self.assertTrue(ok)

        ok, reason = chain.width_ok(
            snapshot(leg(bid=4000, ask=4002), leg("A2701.DCE", 4001, 4002))
        )
        self.assertFalse(ok)
        self.assertIn("盘口宽度", reason or "")

    def test_sync_boundary(self) -> None:
        chain = FilterChain(FilterParams(sync_seconds=0.5), pricetick=1.0)
        late = MOMENT + timedelta(seconds=0.5)
        ok, _ = chain.sync_ok(snapshot(leg(moment=MOMENT), leg("A2701.DCE", 4001, 4002, moment=late)))
        self.assertTrue(ok)

        later = MOMENT + timedelta(seconds=0.8)
        ok, reason = chain.sync_ok(
            snapshot(leg(moment=MOMENT), leg("A2701.DCE", 4001, 4002, moment=later))
        )
        self.assertFalse(ok)
        self.assertIn("两腿报价差", reason or "")

    def test_sync_skipped_without_timestamps(self) -> None:
        chain = FilterChain(FilterParams(sync_seconds=0.5), pricetick=1.0)
        near = leg()
        far = leg("A2701.DCE", 4001, 4002)
        near.datetime = None
        far.datetime = None
        snap = SpreadSnapshot.build(near, far)
        ok, _ = chain.sync_ok(snap)
        self.assertTrue(ok)

    def test_sync_tolerates_second_resolution(self) -> None:
        """CZCE 的 UpdateMillisec 恒为 0：两腿都整秒时容许 1 秒差，不误杀。"""
        chain = FilterChain(FilterParams(sync_seconds=0.5), pricetick=1.0)
        one_second = MOMENT + timedelta(seconds=1)
        ok, _ = chain.sync_ok(
            snapshot(leg(moment=MOMENT), leg("A2701.DCE", 4001, 4002, moment=one_second))
        )
        self.assertTrue(ok, "整秒精度的两腿差 1s 属数据精度问题，应放行")

        # 超过 1 秒仍要拦
        two_seconds = MOMENT + timedelta(seconds=2)
        ok, reason = chain.sync_ok(
            snapshot(leg(moment=MOMENT), leg("A2701.DCE", 4001, 4002, moment=two_seconds))
        )
        self.assertFalse(ok)
        self.assertIn("两腿报价差", reason or "")

    def test_sync_strict_when_millis_present(self) -> None:
        """有毫秒精度时仍按原阈值卡（0.8s > 0.5s 要拦）。"""
        chain = FilterChain(FilterParams(sync_seconds=0.5), pricetick=1.0)
        with_millis = MOMENT + timedelta(milliseconds=800)
        ok, _ = chain.sync_ok(
            snapshot(leg(moment=MOMENT), leg("A2701.DCE", 4001, 4002, moment=with_millis))
        )
        self.assertFalse(ok)


class TestDepthAndVolumeRatio(unittest.TestCase):
    def test_depth_boundary(self) -> None:
        chain = FilterChain(FilterParams(min_depth=1), pricetick=1.0)
        ok, _ = chain.depth_ok(snapshot(leg(bid_volume=1, ask_volume=1)))
        self.assertTrue(ok)

        ok, reason = chain.depth_ok(snapshot(leg(bid_volume=0)))
        self.assertFalse(ok)
        self.assertIn("一档最小挂量", reason or "")

    def test_volume_ratio_disabled(self) -> None:
        chain = FilterChain(FilterParams(volume_ratio_max=0), pricetick=1.0)
        ok, _ = chain.volume_ratio_ok(snapshot(leg(volume=0), leg("A2701.DCE", volume=0)))
        self.assertTrue(ok)

    def test_volume_ratio_boundary(self) -> None:
        chain = FilterChain(FilterParams(volume_ratio_max=10), pricetick=1.0)
        ok, _ = chain.volume_ratio_ok(snapshot(leg(volume=10_000), leg("A2701.DCE", volume=1_000)))
        self.assertTrue(ok)

        chain = FilterChain(FilterParams(volume_ratio_max=10), pricetick=1.0)
        ok, reason = chain.volume_ratio_ok(
            snapshot(leg(volume=10_001), leg("A2701.DCE", volume=1_000))
        )
        self.assertFalse(ok)
        self.assertIn("成交量比", reason or "")

    def test_volume_ratio_zero_volume(self) -> None:
        chain = FilterChain(FilterParams(volume_ratio_max=10), pricetick=1.0)
        ok, reason = chain.volume_ratio_ok(snapshot(leg(volume=0), leg("A2701.DCE", volume=100)))
        self.assertFalse(ok)
        self.assertIn("成交量为 0", reason or "")


class TestRollFilters(unittest.TestCase):
    """REC3 / REX1：用合约月份近似交易日数（2026-11-02 观察 A2611 已进当月）。"""

    def test_trading_days_to_contract(self) -> None:
        # 2026-11-02 距 2027-01-01 = 60 自然日 → 约 42 交易日
        self.assertEqual(trading_days_to_contract("A2701", "2026-11-02"), 42)
        # 2026-11-02 距 2026-12-01 = 29 自然日 → 约 20 交易日
        self.assertEqual(trading_days_to_contract("A2612", "2026-11-02"), 20)

    def test_roll_entry_boundary(self) -> None:
        params = FilterParams(roll_entry_cutoff_days=3)
        chain = FilterChain(params, pricetick=1.0)

        # A2612 距当月约 20 交易日 → 允许开仓
        ok, _ = chain.roll_entry_ok(("A2612", "A2701"), "2026-11-02")
        self.assertTrue(ok)

        # 2026-11-30 距 2026-12-01 = 1 自然日 → 0 交易日 ≤ 3 → 禁止开仓
        ok, reason = chain.roll_entry_ok(("A2612", "A2701"), "2026-11-30")
        self.assertFalse(ok)
        self.assertIn("REC3", reason or "")

    def test_roll_exit_needed(self) -> None:
        chain = FilterChain(FilterParams(roll_exit_days=1), pricetick=1.0)
        need, _ = chain.roll_exit_needed(("A2612", "A2701"), "2026-10-20")
        self.assertFalse(need)

        need, reason = chain.roll_exit_needed(("A2612", "A2701"), "2026-11-30")
        self.assertTrue(need)
        self.assertIn("REX1", reason or "")

    def test_zero_days_disables(self) -> None:
        chain = FilterChain(FilterParams(roll_entry_cutoff_days=0, roll_exit_days=0), pricetick=1.0)
        self.assertTrue(chain.roll_entry_ok(("A2611", "A2701"), "2026-10-30")[0])
        self.assertFalse(chain.roll_exit_needed(("A2611", "A2701"), "2026-10-30")[0])


class TestEntryAllowedChain(unittest.TestCase):
    def test_pass(self) -> None:
        chain = FilterChain(FilterParams(volume_ratio_max=10), pricetick=1.0)
        result = chain.entry_allowed(snapshot(), "2026-09-01")
        self.assertTrue(result.passed)
        self.assertIsNone(result.blocked_by)

    def test_first_failure_short_circuits(self) -> None:
        chain = FilterChain(FilterParams(volume_ratio_max=10), pricetick=1.0)
        snap = snapshot(
            leg(bid=4000, ask=4003),                     # 宽度超标
            leg("A2701.DCE", 4001, 4002, volume=1),      # 成交量比也超标
        )
        result = chain.entry_allowed(snap, "2026-09-01")
        self.assertFalse(result.passed)
        self.assertIn("W1", result.blocked_by or "")

    def test_dirty_leg_blocks_first(self) -> None:
        chain = FilterChain(FilterParams(), pricetick=1.0)
        result = chain.entry_allowed(snapshot(leg(bid=0, ask=0)), "2026-09-01")
        self.assertFalse(result.passed)
        self.assertIn("脏数据", result.blocked_by or "")


class TestCalendar(unittest.TestCase):
    def test_contract_month(self) -> None:
        self.assertEqual(str(parse_contract_month("OI611")), "2026-11-01")
        self.assertEqual(str(parse_contract_month("ao2609")), "2026-09-01")
        self.assertIsNone(parse_contract_month("OI"))

    def test_night_session_maps_to_next_trading_day(self) -> None:
        # 周四夜盘 → 周五
        self.assertEqual(trading_day_of(datetime(2026, 11, 5, 22, 0)).isoformat(), "2026-11-06")
        # 周五夜盘 → 下周一（跳过周末）
        self.assertEqual(trading_day_of(datetime(2026, 11, 6, 22, 0)).isoformat(), "2026-11-09")
        # 凌晨两点仍属上一夜盘
        self.assertEqual(trading_day_of(datetime(2026, 11, 7, 2, 0)).isoformat(), "2026-11-09")
        # 日盘
        self.assertEqual(trading_day_of(datetime(2026, 11, 6, 10, 0)).isoformat(), "2026-11-06")

    def test_call_auction(self) -> None:
        self.assertTrue(in_call_auction(datetime(2026, 11, 6, 8, 59)))
        self.assertTrue(in_call_auction(datetime(2026, 11, 6, 20, 55)))
        self.assertFalse(in_call_auction(datetime(2026, 11, 6, 9, 0)))
        self.assertFalse(in_call_auction(datetime(2026, 11, 6, 10, 0)))

    def test_sessions(self) -> None:
        sessions = TradingSessions("night_2300")
        self.assertTrue(sessions.is_open(datetime(2026, 11, 6, 10, 0)))
        self.assertTrue(sessions.is_open(datetime(2026, 11, 6, 22, 0)))
        self.assertFalse(sessions.is_open(datetime(2026, 11, 6, 23, 30)))
        self.assertFalse(sessions.is_open(datetime(2026, 11, 6, 12, 0)))

        overnight = TradingSessions("night_0100")
        self.assertTrue(overnight.is_open(datetime(2026, 11, 7, 0, 30)))

        unrestricted = TradingSessions("")
        self.assertTrue(unrestricted.unrestricted)
        self.assertTrue(unrestricted.is_open(datetime(2026, 11, 7, 3, 0)))
        # 集合竞价即使不限时段也要拦截
        self.assertFalse(unrestricted.is_open(datetime(2026, 11, 6, 8, 57)))

    def test_explicit_segments(self) -> None:
        sessions = TradingSessions("day:09:00-10:15;night:21:00-23:00")
        self.assertTrue(sessions.is_open(datetime(2026, 11, 6, 9, 30)))
        self.assertFalse(sessions.is_open(datetime(2026, 11, 6, 11, 0)))
        self.assertTrue(sessions.is_open(datetime(2026, 11, 6, 22, 0)))


if __name__ == "__main__":
    unittest.main()
