"""T4：目标价差与保守取整（与参考实现给出的两个实算基准对拍）。

基准合约对 ``a2611-a2701``：乘数 10、最小变动价位 1、手续费 2 元/边、
``min_exit_pnl = 10`` 元 → 一层「开+平」四条腿费用 = 8 元（1 手）/ 24 元（3 手）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.base import (  # noqa: E402
    DIRECTION_LONG,
    DIRECTION_SHORT,
    ROUND_CEIL,
    ROUND_FLOOR,
    ROUND_NEAREST,
    TARGET_ROUND_NEAREST,
    PositionLayer,
    calc_target_price,
    calc_target_spread,
    entry_spread_price,
    exit_spread_price,
    round_to_tick,
)

SIZE = 10.0
TICK = 1.0
MIN_EXIT_PNL = 10.0
LAYER_AVG = -41.0


def net_pnl(direction: int, avg: float, exit_price: float, size: float, volume: float,
            fee: float) -> float:
    """成交后的净利（元）= 毛利 − 该层「开+平」费用。"""
    gross = direction * (exit_price - avg) * size * volume
    return gross - fee


class TestReferenceBaselines(unittest.TestCase):
    """参考实现的两个实算基准（方案 9 节 T4 ①②）。"""

    def test_one_lot_long(self) -> None:
        target = calc_target_spread(LAYER_AVG, DIRECTION_LONG, MIN_EXIT_PNL, 8.0, SIZE, 1)
        self.assertAlmostEqual(target, -39.2, places=9)

        price = calc_target_price(LAYER_AVG, DIRECTION_LONG, MIN_EXIT_PNL, 8.0, SIZE, 1, TICK)
        self.assertAlmostEqual(price, -39.0, places=9)

        pnl = net_pnl(DIRECTION_LONG, LAYER_AVG, price, SIZE, 1, 8.0)
        self.assertAlmostEqual(pnl, 12.0, places=9)
        self.assertGreaterEqual(pnl, MIN_EXIT_PNL)

    def test_three_lot_long(self) -> None:
        target = calc_target_spread(LAYER_AVG, DIRECTION_LONG, MIN_EXIT_PNL, 24.0, SIZE, 3)
        self.assertAlmostEqual(target, -41 + 34 / 30, places=9)
        self.assertAlmostEqual(target, -39.8666666667, places=9)

        price = calc_target_price(LAYER_AVG, DIRECTION_LONG, MIN_EXIT_PNL, 24.0, SIZE, 3, TICK)
        self.assertAlmostEqual(price, -39.0, places=9)

        pnl = net_pnl(DIRECTION_LONG, LAYER_AVG, price, SIZE, 3, 24.0)
        self.assertAlmostEqual(pnl, 36.0, places=9)
        self.assertGreaterEqual(pnl, MIN_EXIT_PNL)


class TestShortDirection(unittest.TestCase):
    """空头方向：买入止盈，向下取整，净利同样不低于门槛。"""

    def test_short_one_lot(self) -> None:
        target = calc_target_spread(LAYER_AVG, DIRECTION_SHORT, MIN_EXIT_PNL, 8.0, SIZE, 1)
        self.assertAlmostEqual(target, -42.8, places=9)

        price = calc_target_price(LAYER_AVG, DIRECTION_SHORT, MIN_EXIT_PNL, 8.0, SIZE, 1, TICK)
        self.assertAlmostEqual(price, -43.0, places=9)

        pnl = net_pnl(DIRECTION_SHORT, LAYER_AVG, price, SIZE, 1, 8.0)
        self.assertAlmostEqual(pnl, 12.0, places=9)
        self.assertGreaterEqual(pnl, MIN_EXIT_PNL)

    def test_short_three_lot_positive_spread(self) -> None:
        """正价差 + 空头方向：目标价 < 均价，取整向下。"""
        avg = 60.0
        target = calc_target_spread(avg, DIRECTION_SHORT, MIN_EXIT_PNL, 24.0, SIZE, 3)
        self.assertAlmostEqual(target, 60 - 34 / 30, places=9)

        price = calc_target_price(avg, DIRECTION_SHORT, MIN_EXIT_PNL, 24.0, SIZE, 3, TICK)
        self.assertAlmostEqual(price, 58.0, places=9)

        pnl = net_pnl(DIRECTION_SHORT, avg, price, SIZE, 3, 24.0)
        self.assertGreaterEqual(pnl, MIN_EXIT_PNL)

    def test_rounding_never_reduces_profit(self) -> None:
        """遍历价位：两种方向的保守取整都必须保证净利 ≥ min_exit_pnl。"""
        for avg in (-100.5, -41.0, -0.5, 0.0, 3.3, 60.0):
            for direction in (DIRECTION_LONG, DIRECTION_SHORT):
                for volume in (1, 2, 5):
                    fee = 8.0 * volume
                    price = calc_target_price(
                        avg, direction, MIN_EXIT_PNL, fee, SIZE, volume, TICK
                    )
                    pnl = net_pnl(direction, avg, price, SIZE, volume, fee)
                    with self.subTest(avg=avg, direction=direction, volume=volume):
                        self.assertGreaterEqual(pnl, MIN_EXIT_PNL - 1e-9)


class TestRoundToTick(unittest.TestCase):
    def test_ceil_and_floor(self) -> None:
        self.assertAlmostEqual(round_to_tick(-39.2, 1, ROUND_CEIL), -39.0)
        self.assertAlmostEqual(round_to_tick(-39.2, 1, ROUND_FLOOR), -40.0)
        self.assertAlmostEqual(round_to_tick(0.30000000000000004, 0.1, ROUND_CEIL), 0.3)
        self.assertAlmostEqual(round_to_tick(0.31, 0.1, ROUND_CEIL), 0.4)
        self.assertAlmostEqual(round_to_tick(0.39, 0.1, ROUND_FLOOR), 0.3)

    def test_nearest(self) -> None:
        self.assertAlmostEqual(round_to_tick(-39.2, 1, ROUND_NEAREST), -39.0)
        self.assertAlmostEqual(round_to_tick(-39.6, 1, ROUND_NEAREST), -40.0)

    def test_zero_pricetick_returns_raw(self) -> None:
        self.assertAlmostEqual(round_to_tick(-39.2, 0, ROUND_CEIL), -39.2)

    def test_nearest_mode_option(self) -> None:
        price = calc_target_price(
            LAYER_AVG, DIRECTION_LONG, MIN_EXIT_PNL, 8.0, SIZE, 1, TICK,
            round_mode=TARGET_ROUND_NEAREST,
        )
        self.assertAlmostEqual(price, -39.0)


class TestGuards(unittest.TestCase):
    def test_invalid_size_or_volume_returns_avg(self) -> None:
        self.assertEqual(calc_target_spread(LAYER_AVG, DIRECTION_LONG, 10, 8, 0, 1), LAYER_AVG)
        self.assertEqual(calc_target_spread(LAYER_AVG, DIRECTION_LONG, 10, 8, 10, 0), LAYER_AVG)

    def test_zero_fee_and_threshold_keeps_avg(self) -> None:
        price = calc_target_price(LAYER_AVG, DIRECTION_LONG, 0, 0, SIZE, 1, TICK)
        self.assertAlmostEqual(price, LAYER_AVG)


class TestLayerPnl(unittest.TestCase):
    def test_unrealized_pnl_sign(self) -> None:
        layer = PositionLayer(direction=DIRECTION_LONG, volume=2, avg_spread=-41.0)
        self.assertAlmostEqual(layer.unrealized_pnl(-40.0, 10), 20.0)
        self.assertAlmostEqual(layer.unrealized_pnl(-42.0, 10), -20.0)

        short_layer = PositionLayer(direction=DIRECTION_SHORT, volume=2, avg_spread=-41.0)
        self.assertAlmostEqual(short_layer.unrealized_pnl(-42.0, 10), 20.0)
        self.assertAlmostEqual(short_layer.unrealized_pnl(-40.0, 10), -20.0)

    def test_no_pnl_after_close(self) -> None:
        layer = PositionLayer(direction=DIRECTION_LONG, volume=1, avg_spread=-41.0)
        layer.mark_closed(-39.0, None, "止盈")
        self.assertFalse(layer.is_open)
        self.assertAlmostEqual(layer.unrealized_pnl(-39.0, 10), 0.0)


class TestPriceHelpers(unittest.TestCase):
    def test_entry_and_exit_price_by_direction(self) -> None:
        class Fake:
            ask_price = -39.5
            bid_price = -40.5

        snapshot = Fake()
        self.assertEqual(entry_spread_price(snapshot, DIRECTION_LONG), -39.5)
        self.assertEqual(entry_spread_price(snapshot, DIRECTION_SHORT), -40.5)
        self.assertEqual(exit_spread_price(snapshot, DIRECTION_LONG), -40.5)
        self.assertEqual(exit_spread_price(snapshot, DIRECTION_SHORT), -39.5)


if __name__ == "__main__":
    unittest.main()
