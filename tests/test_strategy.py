"""T6：状态机（开仓 / 部分成交 / 止盈 / 先撤后平 / 时间止损）。

用 mock 的策略引擎驱动，不连接柜台、不依赖回测数据；撮合语义与
``vnpy_spreadtrading`` 回测引擎一致：委托按"触价成交"或"显式成交"两种方式推进。
"""

from __future__ import annotations

import sys
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.trader.constant import Direction, Exchange, Status  # noqa: E402
from vnpy.trader.object import TickData  # noqa: E402
from vnpy_spreadtrading.base import EngineType, LegData, SpreadData  # noqa: E402

from execution.base import DIRECTION_LONG  # noqa: E402
from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy  # noqa: E402
from utils.risk_limits import RiskLimits  # noqa: E402

NEAR = "A2612.DCE"
FAR = "A2701.DCE"
SPREAD_NAME = "A_2612_2701"
MOMENT = datetime(2026, 11, 2, 10, 0, 0)


class FakeAlgo:
    """记录 extra 的假算法；``fill`` 模拟全量/部分成交。"""

    def __init__(self, algoid: str, spread: SpreadData, direction: Direction,
                 price: float, volume: float, extra: dict) -> None:
        self.algoid = algoid
        self.spread = spread
        self.direction = direction
        self.price = price
        self.volume = volume
        self.extra = extra or {}
        self.status = Status.NOTTRADED
        self.traded_price = 0.0
        self.traded_volume = 0.0
        self.leg_traded = defaultdict(float)
        self.leg_cost = defaultdict(float)
        self.stopped = False

    @property
    def purpose(self) -> str:
        return str(self.extra.get("purpose", ""))

    def is_active(self) -> bool:
        return self.status not in (Status.CANCELLED, Status.ALLTRADED)

    def stop(self) -> None:
        if self.is_active():
            self.stopped = True
            self.status = Status.CANCELLED


class FakeEngine:
    """最小策略引擎：等价于回测引擎的算法管理部分。"""

    def __init__(self) -> None:
        self.algos: dict[str, FakeAlgo] = {}
        self.strategies: dict[str, object] = {}
        self.logs: list[str] = []
        self.counter = 0

    def reset_logs(self) -> None:
        self.logs.clear()

    # --- vnpy 策略引擎接口 ---
    def start_algo(self, strategy, spread_name, direction, price, volume,
                   payup, interval, lock, extra) -> str:
        self.counter += 1
        algoid = f"ALGO_{self.counter}"
        self.algos[algoid] = FakeAlgo(algoid, strategy.spread, direction, price, volume, extra)
        self.strategies[algoid] = strategy
        return algoid

    def stop_algo(self, strategy, algoid: str) -> None:
        algo = self.algos.get(algoid)
        if algo is None or not algo.is_active():
            return
        algo.stop()
        strategy.update_spread_algo(algo)

    def write_strategy_log(self, strategy, msg: str) -> None:
        self.logs.append(msg)

    def put_strategy_event(self, strategy) -> None:
        pass

    def send_notification(self, msg, strategy=None) -> None:
        pass

    def get_engine_type(self):
        return EngineType.BACKTESTING

    def load_bar(self, spread, days, interval, callback) -> None:
        pass

    def load_tick(self, spread, days, callback) -> None:
        pass

    # --- 测试辅助 ---
    def fill(self, algoid: str, price: float | None = None,
             volume: float | None = None) -> FakeAlgo:
        algo = self.algos[algoid]
        algo.traded_volume = algo.volume if volume is None else volume
        algo.traded_price = algo.price if price is None else price
        algo.status = Status.ALLTRADED
        self.strategies[algoid].update_spread_algo(algo)
        return algo

    def active_algos(self, purpose: str | None = None) -> list[FakeAlgo]:
        return [
            algo for algo in self.algos.values()
            if algo.is_active() and (purpose is None or algo.purpose == purpose)
        ]

    def last_log(self) -> str:
        return self.logs[-1] if self.logs else ""


def make_spread() -> SpreadData:
    near = LegData(NEAR)
    near.pricetick = 1.0
    near.size = 10
    near.min_volume = 1

    far = LegData(FAR)
    far.pricetick = 1.0
    far.size = 10
    far.min_volume = 1

    spread = SpreadData(
        name=SPREAD_NAME,
        legs=[near, far],
        variable_symbols={"near": NEAR, "far": FAR},
        variable_directions={"near": 1, "far": -1},
        price_formula="near - far",
        trading_multipliers={NEAR: 1, FAR: -1},
        active_symbol=NEAR,
        min_volume=1,
    )
    return spread


def feed_quote(spread: SpreadData, near_bid: float, near_ask: float,
               far_bid: float, far_ask: float,
               moment: datetime = MOMENT) -> None:
    for symbol, bid, ask in ((NEAR, near_bid, near_ask), (FAR, far_bid, far_ask)):
        tick = TickData(
            symbol=symbol.split(".")[0],
            exchange=Exchange.DCE,
            datetime=moment,
            volume=1000,
            bid_price_1=bid,
            ask_price_1=ask,
            bid_volume_1=10,
            ask_volume_1=10,
            gateway_name="TEST",
        )
        spread.legs[symbol].update_tick(tick)
    spread.bid_volume = 10
    spread.ask_volume = 10
    spread.datetime = moment


def make_strategy(setting: dict | None = None) -> tuple[CrossPeriodMRStrategy, FakeEngine, SpreadData]:
    spread = make_spread()
    engine = FakeEngine()
    options = {
        "live_trading": True,
        "stop_loss": 200,
        "max_hold_days": 10,
        "layer_volume": 1,
        "trading_sessions": "",
        # 单元测试只关心交易逻辑，默认关掉最小预热门槛
        "warmup_min_samples": 0,
        "warmup_min_minutes": 0,
    }
    options.update(setting or {})
    strategy = CrossPeriodMRStrategy(engine, "test_strategy", spread, options)
    strategy.inited = True
    strategy.trading = True
    strategy.on_init()
    strategy.on_start()
    return strategy, engine, spread


def warm_up(strategy: CrossPeriodMRStrategy, values: list[float],
            start: datetime | None = None) -> None:
    """按 1 秒步长喂历史价差；时间戳必须落在 mean 窗口（3 分钟）内。"""
    origin = start or (MOMENT - timedelta(minutes=2))
    for index, value in enumerate(values):
        strategy.stat.push((origin + timedelta(seconds=index)).timestamp(), value)


class TestMarginGate(unittest.TestCase):
    def test_blocks_open_above_configured_ratio(self) -> None:
        strategy, engine, _spread = make_strategy()
        engine.main_engine = SimpleNamespace(get_all_accounts=lambda: [
            SimpleNamespace(gateway_name="CTP", balance=100_000.0, available=20_000.0)
        ])
        with patch("strategies.cross_period_mean_reversion.load_risk_limits",
                   return_value=RiskLimits(enabled=True, max_margin_ratio=0.70)):
            reason = strategy._margin_gate_reason()
        self.assertIn("已用资金", reason)
        self.assertAlmostEqual(strategy.last_margin_ratio or 0, 0.8)

    def test_allows_below_limit_and_when_disabled(self) -> None:
        strategy, engine, _spread = make_strategy()
        engine.main_engine = SimpleNamespace(get_all_accounts=lambda: [
            SimpleNamespace(gateway_name="CTP", balance=100_000.0, available=50_000.0)
        ])
        with patch("strategies.cross_period_mean_reversion.load_risk_limits",
                   return_value=RiskLimits(enabled=True, max_margin_ratio=0.70)):
            self.assertEqual(strategy._margin_gate_reason(), "")
        with patch("strategies.cross_period_mean_reversion.load_risk_limits",
                   return_value=RiskLimits(enabled=False, max_margin_ratio=0.70)):
            self.assertEqual(strategy._margin_gate_reason(), "")

    def test_blocks_when_account_data_missing(self) -> None:
        strategy, engine, _spread = make_strategy()
        engine.main_engine = SimpleNamespace(get_all_accounts=lambda: [])
        with patch("strategies.cross_period_mean_reversion.load_risk_limits",
                   return_value=RiskLimits(enabled=True, max_margin_ratio=0.70)):
            self.assertIn("无账户资金数据", strategy._margin_gate_reason())


class TestLegacyPositionAdoption(unittest.TestCase):
    def test_adopts_matching_leg_positions_and_marks_layer(self) -> None:
        strategy, engine, spread = make_strategy()
        near = spread.legs[NEAR]
        far = spread.legs[FAR]
        near.net_pos = 1
        near.net_pos_price = 5265.0
        far.net_pos = -1
        far.net_pos_price = 5279.0
        spread.net_pos = 1
        engine.main_engine = SimpleNamespace(get_all_positions=lambda: [
            SimpleNamespace(vt_symbol=NEAR, direction=Direction.LONG, volume=1, price=5265.0),
            SimpleNamespace(vt_symbol=FAR, direction=Direction.SHORT, volume=1, price=5279.0),
        ])

        self.assertTrue(strategy.can_adopt_legacy_position())
        self.assertTrue(strategy.adopt_legacy_position())
        self.assertEqual(len(strategy.layers), 1)
        layer = strategy.layers[0]
        self.assertTrue(layer.adopted)
        self.assertEqual(layer.direction, DIRECTION_LONG)
        self.assertAlmostEqual(layer.volume, 1)
        self.assertAlmostEqual(layer.avg_spread, -14.0)

    def test_adoption_never_uses_market_price_as_cabinet_cost(self) -> None:
        strategy, engine, spread = make_strategy()
        spread.legs[NEAR].net_pos = 1
        spread.legs[FAR].net_pos = -1
        spread.legs[NEAR].last_price = 5265.0
        spread.legs[FAR].last_price = 5279.0
        spread.net_pos = 1
        engine.main_engine = SimpleNamespace(get_all_positions=lambda: [])
        self.assertFalse(strategy.can_adopt_legacy_position())
        self.assertEqual(strategy.layers, [])

    def test_resume_does_not_clear_cabinet_slot_reservations(self) -> None:
        from execution.base import PositionLayer

        strategy, _engine, _spread = make_strategy()
        layer = PositionLayer(direction=DIRECTION_LONG, volume=1, avg_spread=-20.0,
                              near_price=5265.0, far_price=5285.0, layer_index=1)
        strategy.layers.append(layer)
        strategy.pause_strategy()
        strategy.reserved_exit_orders["CTP.9_1_2"] = layer.tier_id
        strategy.reserved_open_orders["CTP.8_1_2"] = ""
        self.assertTrue(strategy.resume_strategy(reconciled=True))
        # 恢复交易不能释放柜台委托占位，否则会重复挂止盈/加仓
        self.assertIn("CTP.9_1_2", strategy.reserved_exit_orders)
        self.assertIn("CTP.8_1_2", strategy.reserved_open_orders)

    def test_reserved_open_order_blocks_duplicate_entry(self) -> None:
        strategy, engine, _spread = make_strategy()
        strategy.reserved_open_orders["CTP.8_1_2"] = ""
        self.assertTrue(strategy._reserved_open_active())
        strategy._check_entry(None, MOMENT)   # 槽位被占，直接返回
        self.assertEqual(engine.algos, {})

    def test_reserved_exit_order_blocks_duplicate_take_profit(self) -> None:
        from execution.base import PositionLayer

        strategy, engine, spread = make_strategy()
        layer = PositionLayer(direction=DIRECTION_LONG, volume=1, avg_spread=-20.0,
                              near_price=5265.0, far_price=5285.0, layer_index=1)
        strategy.layers.append(layer)
        strategy.direction = DIRECTION_LONG
        strategy.reserved_exit_orders["CTP.9_1_2"] = layer.tier_id
        strategy._maybe_preplace_exit(MOMENT)
        self.assertEqual(engine.algos, {})          # 不再挂第二张止盈
        self.assertEqual(layer.algoid, "CTP.9_1_2")  # 该层显示已有平仓单

    def test_preplace_exit_waits_for_cabinet_confirmation(self) -> None:
        """柜台没确认这笔仓位前不挂平仓单，避免“平仓量超过持仓量”。"""
        from execution.base import PositionLayer

        strategy, engine, _spread = make_strategy()
        layer = PositionLayer(direction=DIRECTION_LONG, volume=1, avg_spread=-20.0,
                              near_price=5265.0, far_price=5285.0, layer_index=1)
        strategy.layers.append(layer)
        strategy.direction = DIRECTION_LONG
        strategy.cabinet_position_check = lambda direction, volume: False
        strategy._maybe_preplace_exit(MOMENT)
        self.assertEqual(engine.algos, {})          # 柜台无持仓 → 暂缓
        strategy.cabinet_position_check = lambda direction, volume: True
        strategy._maybe_preplace_exit(MOMENT)
        self.assertTrue(engine.algos)               # 柜台确认后才挂

    def test_rejects_mismatched_leg_positions(self) -> None:
        strategy, _engine, spread = make_strategy()
        spread.legs[NEAR].net_pos = 2
        spread.legs[NEAR].net_pos_price = 5265.0
        spread.legs[FAR].net_pos = -1
        spread.legs[FAR].net_pos_price = 5279.0
        spread.net_pos = 1
        self.assertFalse(strategy.can_adopt_legacy_position())
        self.assertFalse(strategy.adopt_legacy_position())
        self.assertEqual(strategy.layers, [])


class TestWarmupGate(unittest.TestCase):
    def test_window_needs_samples_and_coverage(self) -> None:
        from utils.stat_window import StatWindow

        window = StatWindow(mean_seconds=180, sd_seconds=1800)
        for index in range(5):
            window.push(1000.0 + index, 1.0 + (index % 2))
        self.assertFalse(window.ready(min_samples=30, min_seconds=300))
        for index in range(5, 400):
            window.push(1000.0 + index, 1.0 + (index % 2))
        self.assertTrue(window.ready(min_samples=30, min_seconds=300))
        self.assertGreaterEqual(window.span(), 300)

    def test_strategy_does_not_open_before_warmup_ready(self) -> None:
        strategy, _engine, spread = make_strategy(
            {"warmup_min_samples": 30, "warmup_min_minutes": 5})
        strategy._check_entry = lambda *args: (_ for _ in ()).throw(
            AssertionError("预热不足时不应进入开仓判定"))
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()   # 应只记日志，不进入 _check_entry
        self.assertFalse(strategy._warmup_ready())

    def test_strategy_opens_only_after_warmup_satisfied(self) -> None:
        strategy, _engine, spread = make_strategy(
            {"warmup_min_samples": 5, "warmup_min_minutes": 1})
        called = []
        strategy._check_entry = lambda *args: called.append(True)
        for index in range(80):
            strategy.stat.push(MOMENT.timestamp() - 100 + index, 1.0 + (index % 2))
        self.assertTrue(strategy._warmup_ready())
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()
        self.assertEqual(called, [True])


class TestCustomFee(unittest.TestCase):
    def test_custom_fee_overrides_table_spec(self) -> None:
        from unittest.mock import patch
        from utils.spread_config import SpreadConfig

        config = SpreadConfig(
            name="A_2612_2701", legs=[NEAR, FAR], active_symbol=NEAR,
            fee={"unit": "元", "open": 3.0, "close_yesterday": 3.0, "close_today": 0.0},
        )
        with patch("strategies.cross_period_mean_reversion.get_spread_config",
                   return_value=config):
            strategy, _engine, _spread = make_strategy()
        self.assertEqual(strategy.fee_spec.unit, "元")
        self.assertEqual(strategy.fee_spec.open, 3.0)
        self.assertEqual(strategy.fee_spec.close_yesterday, 3.0)
        self.assertEqual(strategy.fee_spec.close_today, 0.0)


class TestOpenAndTakeProfit(unittest.TestCase):
    def test_signal_opens_then_take_profit_closes(self) -> None:
        strategy, engine, spread = make_strategy({"min_exit_pnl_override": 10})
        strategy.cfg.margin_per_lot = 5000
        warm_up(strategy, [-100.0, -100.4] * 20)

        # mid = 4000.5 - 4100.5 = -100；把价差压到 -102 → 跌破 1 倍标准差，做多价差
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()

        self.assertEqual(strategy.direction, 0)
        self.assertTrue(
            any("报单依据[开仓]" in log and "z=" in log and "报单=1手" in log
                and "执行=two_leg" in log and "保证金/手=5000元" in log
                for log in engine.logs),
            engine.logs,
        )
        open_algos = engine.active_algos("open")
        self.assertEqual(len(open_algos), 1)
        self.assertEqual(open_algos[0].direction, Direction.LONG)
        self.assertAlmostEqual(open_algos[0].price, -101.0)   # 买 near(ask 4001) - 卖 far(bid 4102)

        # 开仓成交 → 建层 + 立刻挂止盈
        engine.fill(open_algos[0].algoid, price=-101.0)
        self.assertEqual(len(strategy.layers), 1)
        layer = strategy.layers[0]
        self.assertEqual(layer.direction, DIRECTION_LONG)
        self.assertAlmostEqual(layer.volume, 1)

        exit_algos = engine.active_algos("take_profit")
        self.assertEqual(len(exit_algos), 1)
        self.assertTrue(any("报单依据[止盈]" in log and "层均价=" in log
                            for log in engine.logs), engine.logs)
        self.assertEqual(exit_algos[0].direction, Direction.SHORT)
        # 目标价 = -101 + (10 + 费用)/(10 × 1)，向上取整到 1 tick
        self.assertGreater(exit_algos[0].price, layer.avg_spread)
        self.assertAlmostEqual(exit_algos[0].price, layer.target_spread)

        # 止盈成交 → 层归档、净利 ≥ 门槛
        exit_price = exit_algos[0].price
        engine.fill(exit_algos[0].algoid, price=exit_price)

        self.assertEqual(strategy.layers, [])
        self.assertEqual(strategy.direction, 0)
        self.assertEqual(len(strategy.closed_layers), 1)
        self.assertGreaterEqual(strategy.daily_realized, 10.0 - 1e-6)

    def test_partial_fill_creates_smaller_layer(self) -> None:
        strategy, engine, spread = make_strategy()
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()

        open_algo = engine.active_algos("open")[0]
        engine.fill(open_algo.algoid, price=-101.0, volume=0.4)
        self.assertEqual(len(strategy.layers), 1)
        self.assertAlmostEqual(strategy.layers[0].volume, 0.4)

    def test_compute_only_mode_does_not_trade(self) -> None:
        strategy, engine, spread = make_strategy({"live_trading": False})
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()

        self.assertEqual(engine.algos, {})
        self.assertTrue(any("只计算不发单" in log for log in engine.logs))


class TestStopLoss(unittest.TestCase):
    def _open_layer(self, strategy, engine, spread, price: float = -101.0):
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()
        open_algo = engine.active_algos("open")[0]
        engine.fill(open_algo.algoid, price=price)
        return strategy.layers[0]

    def test_stop_loss_cancels_take_profit_before_closing(self) -> None:
        strategy, engine, spread = make_strategy({"stop_loss": 100})   # 每层 100 元
        layer = self._open_layer(strategy, engine, spread)
        engine.reset_logs()

        # size=10，浮亏 ≤ -100 元需要价差再跌 1 点以上：把价差打到 -105
        feed_quote(spread, 4000, 4001, 4105, 4106, moment=MOMENT + timedelta(seconds=1))
        strategy.on_spread_data()

        close_algos = engine.active_algos("close")
        self.assertEqual(len(close_algos), 1, "止损应发出平仓 algo")
        self.assertEqual(close_algos[0].direction, Direction.SHORT)
        # 平仓前止盈单必须已被撤销
        self.assertEqual(engine.active_algos("take_profit"), [])
        self.assertTrue(any("平仓前先撤在挂单" in log for log in engine.logs))

        engine.fill(close_algos[0].algoid, price=-105.0)
        self.assertEqual(strategy.layers, [])
        self.assertEqual(strategy.direction, 0)
        self.assertLess(strategy.daily_realized, 0)
        self.assertTrue(layer.close_reason.startswith("止损"))

    def test_stop_loss_respects_threshold_boundary(self) -> None:
        """浮亏刚好没到 -stop_loss/max_positions 时不该止损。"""
        strategy, engine, spread = make_strategy({"stop_loss": 1000})
        self._open_layer(strategy, engine, spread)
        # 每层阈值 1000/5 = 200 元 → 10 点价差即 100 元，20 点才 200 元
        feed_quote(spread, 4000, 4001, 4105, 4106, moment=MOMENT + timedelta(seconds=1))
        strategy.on_spread_data()
        self.assertEqual(engine.active_algos("close"), [])
        self.assertEqual(len(strategy.layers), 1)


class TestTimeStopAndRoll(unittest.TestCase):
    def test_time_stop_closes_whole_position(self) -> None:
        strategy, engine, spread = make_strategy({"max_hold_days": 1})
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()
        engine.fill(engine.active_algos("open")[0].algoid, price=-101.0)

        # 把开仓交易日提前到 1 周前
        strategy.layers[0].open_trading_day = "2026-10-27"
        feed_quote(spread, 4000, 4001, 4102, 4103, moment=MOMENT + timedelta(seconds=2))
        strategy.on_spread_data()

        close_algos = engine.active_algos("close")
        self.assertEqual(len(close_algos), 1)
        self.assertTrue(any("时间止损" in log for log in engine.logs))

    def test_roll_exit_forces_close(self) -> None:
        """A2612 在 2026-11-30 距当月不足 1 交易日 → 强平。"""
        late = datetime(2026, 11, 30, 10, 0, 0)
        strategy, engine, spread = make_strategy({"max_hold_days": 999})
        warm_up(strategy, [-100.0, -100.4] * 20, start=late - timedelta(minutes=2))
        feed_quote(spread, 4000, 4001, 4102, 4103, moment=late)
        strategy.on_spread_data()

        # 换月临近 → 既不开仓，也不该有持仓
        self.assertEqual(engine.active_algos("open"), [])
        self.assertTrue(
            any("REC3" in log or "REX1" in log for log in engine.logs),
            engine.logs,
        )


class TestExecutorRouting(unittest.TestCase):
    def test_two_leg_extra(self) -> None:
        strategy, engine, spread = make_strategy({"executor": "two_leg"})
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()

        algo = engine.active_algos("open")[0]
        self.assertEqual(algo.extra.get("mode"), "two_leg")
        self.assertEqual(algo.extra.get("purpose"), "open")
        self.assertEqual(algo.extra.get("offset_flag_width"), 1)

    def test_exchange_spread_without_code_blocks_trading(self) -> None:
        """组合代码缺失时拒绝发单，不能隐式降级为有裸腿风险的 two_leg。"""
        strategy, engine, spread = make_strategy({"executor": "exchange_spread"})
        self.assertEqual(strategy.executor_mode, "exchange_spread")
        self.assertTrue(any("禁止发单" in log for log in engine.logs))

    def test_close_only_never_opens(self) -> None:
        strategy, engine, spread = make_strategy({"close_only": True})
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()
        self.assertEqual(engine.active_algos("open"), [])


class TestTierManagement(unittest.TestCase):
    def test_adds_second_layer_on_deeper_move(self) -> None:
        strategy, engine, spread = make_strategy({"max_positions": 3, "layer_step": 0.3,
                                                 "min_exit_pnl_override": 10})
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()
        engine.fill(engine.active_algos("open")[0].algoid, price=-101.0)
        self.assertEqual(len(strategy.layers), 1)

        # 第 2 层阈值更严：把价差打到 -103 再开一层
        feed_quote(spread, 4000, 4001, 4104, 4105, moment=MOMENT + timedelta(seconds=3))
        strategy.on_spread_data()

        # 加仓前必须先撤掉在挂止盈单
        self.assertTrue(any("加仓前撤掉在挂止盈单" in log for log in engine.logs))

        open_algos = engine.active_algos("open")
        self.assertEqual(len(open_algos), 1)
        engine.fill(open_algos[0].algoid, price=-103.0)
        self.assertEqual(len(strategy.layers), 2)
        self.assertEqual(strategy.layers[1].layer_index, 2)

        # 止盈只挂最新层一张
        exit_algos = engine.active_algos("take_profit")
        self.assertEqual(len(exit_algos), 1)
        self.assertAlmostEqual(exit_algos[0].volume, 1)

    def test_max_positions_caps_layers(self) -> None:
        strategy, engine, spread = make_strategy({"max_positions": 1})
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()
        engine.fill(engine.active_algos("open")[0].algoid, price=-101.0)

        # 层数已到上限：再深跌也不开新层
        feed_quote(spread, 4000, 4001, 4110, 4111, moment=MOMENT + timedelta(seconds=3))
        strategy.on_spread_data()
        self.assertEqual(len(strategy.layers), 1)
        self.assertEqual(engine.active_algos("open"), [])


class TestNewTradingDay(unittest.TestCase):
    def test_archives_algos_and_keeps_position(self) -> None:
        strategy, engine, spread = make_strategy()
        warm_up(strategy, [-100.0, -100.4] * 20)
        feed_quote(spread, 4000, 4001, 4102, 4103)
        strategy.on_spread_data()
        engine.fill(engine.active_algos("open")[0].algoid, price=-101.0)
        self.assertEqual(strategy.trading_day, "2026-11-02")

        # 次日：旧止盈单归档，按持仓重挂
        next_day = datetime(2026, 11, 3, 10, 0, 0)
        feed_quote(spread, 4000, 4001, 4102, 4103, moment=next_day)
        strategy.on_spread_data()

        self.assertEqual(strategy.trading_day, "2026-11-03")
        self.assertTrue(any("进入新交易日" in log for log in engine.logs))
        self.assertEqual(len(strategy.layers), 1)
        # 新交易日的止盈单由策略重新挂出（旧单仅本地归档，不再撤单）
        self.assertEqual(len(strategy.algoids), 1)


if __name__ == "__main__":
    unittest.main()
