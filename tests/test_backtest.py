"""T7（可执行版）：两腿 tick 合成 → 回放 → 撮合 → 精确计费。

不依赖真实数据库：用假的 ``get_database`` 注入两腿 tick，端到端跑一遍回测引擎。
真实数据到位后，``run_backtest.py`` 走的是同一条代码路径。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.object import TickData  # noqa: E402

import backtest.engine as backtest_engine  # noqa: E402
from backtest.engine import (  # noqa: E402
    CrossPeriodBacktestingEngine,
    build_spread_data,
    synthesize_spread_ticks,
)
from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy  # noqa: E402
from utils.spread_config import SpreadConfig  # noqa: E402

NEAR = "a2612.DCE"
FAR = "a2701.DCE"
START = datetime(2026, 9, 1, 10, 0, 0)


def make_config() -> SpreadConfig:
    return SpreadConfig(
        name="A_TEST",
        legs=[NEAR, FAR],
        active_symbol=NEAR,
        exchange="DCE",
        product="A",
        size=10,
        pricetick=1,
        executor="two_leg",
        stop_loss=10000,
        trading_sessions="",
    )


def make_tick(symbol: str, exchange: Exchange, moment: datetime,
              bid: float, ask: float, volume: int = 1000) -> TickData:
    return TickData(
        symbol=symbol.split(".")[0],
        exchange=exchange,
        datetime=moment,
        volume=volume,
        bid_price_1=bid,
        ask_price_1=ask,
        bid_volume_1=10,
        ask_volume_1=10,
        last_price=(bid + ask) / 2,
        gateway_name="TEST",
    )


class FakeDatabase:
    def __init__(self, data: dict[str, list[TickData]]) -> None:
        self.data = data

    def load_tick_data(self, symbol, exchange, start, end):
        key = f"{symbol}.{exchange.value}"
        return [tick for tick in self.data.get(key, []) if start <= tick.datetime <= end]


def build_ticks(price_path: list[tuple[float, float]]) -> dict[str, list[TickData]]:
    """``price_path`` 是 far 腿的 (bid, ask) 序列；near 腿固定 4000/4001。"""
    near_ticks: list[TickData] = []
    far_ticks: list[TickData] = []
    for index, (far_bid, far_ask) in enumerate(price_path):
        moment = START + timedelta(milliseconds=200 * index)
        near_ticks.append(make_tick(NEAR, Exchange.DCE, moment, 4000, 4001))
        far_ticks.append(make_tick(FAR, Exchange.DCE, moment, far_bid, far_ask))
    return {NEAR: near_ticks, FAR: far_ticks}


class TestSynthesis(unittest.TestCase):
    def setUp(self) -> None:
        self.config = make_config()
        self.spread = build_spread_data(self.config)

    def test_build_spread_data(self) -> None:
        self.assertEqual(self.spread.name, "A_TEST")
        self.assertEqual(self.spread.pricetick, 1)
        self.assertEqual(self.spread.legs[NEAR].size, 10)
        self.assertEqual(self.spread.variable_symbols["near"], NEAR)

    def test_merge_and_price(self) -> None:
        ticks = build_ticks([(4100, 4101), (4102, 4103)])
        original = backtest_engine.get_database
        backtest_engine.get_database = lambda: FakeDatabase(ticks)
        try:
            spread_ticks, frames = synthesize_spread_ticks(
                self.spread, START - timedelta(minutes=1), START + timedelta(minutes=1),
                output=lambda *_: None,
            )
        finally:
            backtest_engine.get_database = original

        self.assertGreaterEqual(len(spread_ticks), 2)
        # 第一条只看得到 near，另一腿还没报价 → 不生成合成 tick；
        # 首条合成 tick 是 far 到达后的状态：bid = near_bid − far_ask，ask = near_ask − far_bid
        first = spread_ticks[0]
        self.assertAlmostEqual(first.bid_price_1, 4000 - 4101)
        self.assertAlmostEqual(first.ask_price_1, 4001 - 4100)
        self.assertEqual(len(frames), len(spread_ticks))
        self.assertIn(NEAR, frames[0])
        self.assertIn(FAR, frames[0])

    def test_missing_leg_data(self) -> None:
        original = backtest_engine.get_database
        backtest_engine.get_database = lambda: FakeDatabase({NEAR: [], FAR: []})
        try:
            spread_ticks, _ = synthesize_spread_ticks(
                self.spread, START, START + timedelta(minutes=1), output=lambda *_: None,
            )
        finally:
            backtest_engine.get_database = original
        self.assertEqual(spread_ticks, [])


class TestEndToEndBacktest(unittest.TestCase):
    def test_round_trip_produces_profit_and_fee(self) -> None:
        # 预热：价差在 -100 / -99 交替（sd ≈ 0.5）
        path: list[tuple[float, float]] = []
        for index in range(120):
            path.append((4100, 4101) if index % 2 == 0 else (4099, 4100))
        # 砸到 -105（远超 5 倍标准差）触发做多价差
        path.extend([(4105, 4106)] * 6)
        # 回升到 bid = -102 触发止盈
        path.extend([(4101, 4102)] * 10)

        ticks = build_ticks(path)
        original = backtest_engine.get_database
        backtest_engine.get_database = lambda: FakeDatabase(ticks)
        try:
            config = make_config()
            spread = build_spread_data(config)

            engine = CrossPeriodBacktestingEngine()
            engine.set_parameters(
                spread=spread, interval=Interval.TICK, start=START,
                rate=0, slippage=0, size=config.size, pricetick=config.pricetick,
                capital=1_000_000, end=START + timedelta(hours=1),
            )
            engine.add_strategy(CrossPeriodMRStrategy, {
                "live_trading": True,
                "min_exit_pnl_override": 10,
                "entry_k": 5.0,          # 只在极端偏离时开仓，避免震荡里来回
                "max_positions": 1,      # 只跑一层，保证一开一平
                "trading_sessions": "",
                "stop_loss": 10000,
                "max_hold_days": 999,
                # 该用例只验证开平逻辑，数据只有两分钟，关掉最小预热门槛
                "warmup_min_minutes": 0,
                "warmup_min_samples": 0,
            })
            engine.output = lambda *_: None

            engine.load_data()
            self.assertGreater(len(engine.history_data), 100)

            engine.run_backtesting()
            self.assertEqual(engine.trades and len(engine.trades), 2, "应恰好一开一平")
            self.assertEqual(engine.strategy.layers, [])

            df = engine.calculate_result()
            self.assertIsNotNone(df)

            statistics = engine.calculate_statistics(output=False)
            self.assertEqual(statistics["total_trade_count"], 2)
            # 每条价差成交 2 腿 × 2 元/手 = 4 元，两条共 8 元
            self.assertAlmostEqual(statistics["total_commission"], 8.0)
            self.assertAlmostEqual(statistics["total_net_pnl"], 12.0)
        finally:
            backtest_engine.get_database = original

    def test_no_data_returns_error_path(self) -> None:
        original = backtest_engine.get_database
        backtest_engine.get_database = lambda: FakeDatabase({NEAR: [], FAR: []})
        try:
            config = make_config()
            spread = build_spread_data(config)
            engine = CrossPeriodBacktestingEngine()
            engine.set_parameters(
                spread=spread, interval=Interval.TICK, start=START,
                rate=0, slippage=0, size=config.size, pricetick=config.pricetick,
                end=START + timedelta(hours=1),
            )
            engine.output = lambda *_: None
            engine.load_data()
            self.assertEqual(engine.history_data, [])
        finally:
            backtest_engine.get_database = original


if __name__ == "__main__":
    unittest.main()
