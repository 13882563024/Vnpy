"""交易所组合单执行器：委托被拒后必须结束算法，让策略拿到终态（2026-11-09 回归）。

事故背景：止盈单在集合竞价被 DCE 拒掉（40052）后，算法一直停在 RUNNING，
策略侧的 ``exit_algoid`` / ``layer.algoid`` 永不释放，该层整晚没有止盈委托。
"""

from __future__ import annotations

import sys
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.object import OrderData, TradeData  # noqa: E402
from vnpy.event import Event  # noqa: E402
from vnpy_spreadtrading.engine import SpreadAlgoEngine  # noqa: E402
from execution.cross_period_algo import CrossPeriodAlgo  # noqa: E402
from vnpy_spreadtrading.base import LegData, SpreadData  # noqa: E402

from execution.exchange_spread import ExchangeSpreadImpl  # noqa: E402
from execution.two_leg import TwoLegMakerImpl  # noqa: E402


class FakeAlgo:
    def __init__(self) -> None:
        self.stopped = False
        self.paused = False
        self.status = Status.NOTTRADED
        self.logs: list[str] = []
        self.exchange_spread_vt_symbol = "SP A2612&A2701.DCE"
        self.exchange_spread = "SP A2612&A2701"

    def write_log(self, msg: str) -> None:
        self.logs.append(msg)

    def stop(self) -> None:
        self.stopped = True
        self.status = Status.CANCELLED


def make_order(orderid: str, status: Status) -> OrderData:
    return OrderData(
        symbol="SP A2612&A2701",
        exchange=Exchange.DCE,
        orderid=orderid,
        direction=Direction.LONG,
        offset=Offset.CLOSE,
        type=OrderType.LIMIT,
        price=-14.0,
        volume=1.0,
        status=status,
        reference="",
        gateway_name="CTP",
    )


class TestExchangeSpreadReject(unittest.TestCase):
    def _impl(self) -> tuple[FakeAlgo, ExchangeSpreadImpl]:
        algo = FakeAlgo()
        impl = ExchangeSpreadImpl(algo)
        impl.order_ids.append("CTP.1")
        impl.active_order_ids.add("CTP.1")
        return algo, impl

    def test_rejected_order_finishes_algo(self) -> None:
        algo, impl = self._impl()
        impl.on_order(make_order("1", Status.REJECTED))

        self.assertTrue(algo.stopped)
        self.assertEqual(algo.status, Status.CANCELLED)
        self.assertTrue(any("组合单被拒" in log for log in algo.logs), algo.logs)
        self.assertTrue(any("CTP 网关日志" in log for log in algo.logs), algo.logs)

    def test_reject_with_pending_order_keeps_algo_alive(self) -> None:
        algo, impl = self._impl()
        impl.active_order_ids.add("CTP.2")          # 还有在途委托
        impl.on_order(make_order("1", Status.REJECTED))
        self.assertFalse(algo.stopped)

    def test_filled_order_never_force_stops(self) -> None:
        """成交回报先于持仓刷新到达时不能抢先收尾，否则会误判“未成交”。"""
        algo, impl = self._impl()
        impl.on_order(make_order("1", Status.ALLTRADED))
        self.assertFalse(algo.stopped)

    def test_paused_algo_is_left_alone(self) -> None:
        algo, impl = self._impl()
        algo.paused = True
        impl.on_order(make_order("1", Status.REJECTED))
        self.assertFalse(algo.stopped)


def make_spread() -> SpreadData:
    near = LegData("A2612.DCE")
    far = LegData("A2701.DCE")
    for leg in (near, far):
        leg.pricetick = 1.0
        leg.size = 10
        leg.min_volume = 1
    return SpreadData(
        name="A_2612_2701",
        legs=[near, far],
        variable_symbols={"near": near.vt_symbol, "far": far.vt_symbol},
        variable_directions={"near": 1, "far": -1},
        price_formula="near - far",
        trading_multipliers={near.vt_symbol: 1, far.vt_symbol: -1},
        active_symbol=near.vt_symbol,
        min_volume=1,
    )


class TestExchangeEventOrdering(unittest.TestCase):
    """真实算法引擎分派（会过滤非 active 和重复成交），仅替代柜台与事件出口。"""

    def setUp(self) -> None:
        engine = self.engine = SpreadAlgoEngine.__new__(SpreadAlgoEngine)
        engine.order_algo_map = {}
        engine.vt_tradeids = set()
        contract = SimpleNamespace(symbol='SP A2612&A2701', exchange=Exchange.DCE,
                                   vt_symbol='SP A2612&A2701.DCE', gateway_name='CTP',
                                   pricetick=1, min_volume=1)
        engine.get_contract = Mock(return_value=contract)
        engine.write_algo_log = Mock()
        engine.cancel_order = Mock()
        engine.data_engine = Mock()
        engine.main_engine = SimpleNamespace(send_order=Mock(return_value='CTP.1'))
        self.events = []
        engine.put_algo_event = lambda algo: self.events.append(algo.get_item())
        self.algo = CrossPeriodAlgo(engine, 'algo', make_spread(), Direction.LONG, -14,
                                    2, 0, 0, False, {
                                        'purpose': 'close', 'mode': 'exchange_spread',
                                        'exchange_spread': contract.symbol,
                                        'exchange_spread_vt_symbol': contract.vt_symbol,
                                    })
        engine.algos = {'algo': self.algo}
        self.assertEqual(engine.order_algo_map, {'CTP.1': self.algo})

    def order(self, status, traded):
        order = make_order('1', status)
        order.volume, order.traded = 2, traded
        self.engine.process_order_event(Event('order', order))

    def trade(self, tid, symbol='SP A2612&A2701', volume=1, price=-14, direction=Direction.LONG):
        trade = TradeData(symbol=symbol, exchange=Exchange.DCE, orderid='1', tradeid=tid,
                          direction=direction, offset=Offset.CLOSE, volume=volume,
                          price=price, gateway_name='CTP')
        self.engine.process_trade_event(Event('trade', trade))

    def timer(self, rounds=1):
        for _ in range(rounds):
            self.engine.process_timer_event(Event('timer'))
        self.assertEqual(self.engine.main_engine.send_order.call_count, 1)

    def test_alltraded_before_trade_never_resends(self):
        self.order(Status.ALLTRADED, 2)
        self.timer(20)
        self.assertTrue(self.algo.is_active())
        self.trade('1')
        self.trade('1')  # 引擎去重
        self.timer(20)
        self.assertEqual(self.algo.traded_volume, 1)
        self.trade('2', price=-12)
        self.assertEqual((self.algo.status, self.algo.traded_volume, self.algo.traded_price),
                         (Status.ALLTRADED, 2, -13))

    def test_cancel_remainder_then_late_leg_trades(self):
        self.algo.stop()
        self.order(Status.CANCELLED, 1)
        self.timer(20)
        self.trade('1', 'A2612', price=2010)
        self.timer(20)
        self.assertTrue(self.algo.is_active())
        self.trade('2', 'A2701', price=2024, direction=Direction.SHORT)
        self.assertEqual((self.algo.status, self.algo.traded_volume, self.algo.traded_price),
                         (Status.CANCELLED, 1, -14))
        self.assertEqual([e.traded_volume for e in self.events if e.status == Status.CANCELLED], [1])

    def test_unsolicited_cancel_then_late_combo_fill(self):
        self.order(Status.CANCELLED, 1)
        self.timer(20)
        self.trade('1')
        self.assertEqual((self.algo.status, self.algo.traded_volume), (Status.CANCELLED, 1))

    def test_trade_before_cancel_remainder(self):
        self.trade('1')
        self.order(Status.CANCELLED, 1)
        self.assertEqual((self.algo.status, self.algo.traded_volume), (Status.CANCELLED, 1))

    def test_unacknowledged_cancel_does_not_release_order(self):
        self.algo.stop()
        self.timer(50)
        self.assertFalse(self.algo.is_order_finished())
        self.assertTrue(self.algo.is_active())
        self.engine.cancel_order.assert_called_once_with(self.algo, 'CTP.1')
        self.order(Status.ALLTRADED, 2)
        self.timer(2)
        self.trade('1', volume=2)
        self.assertEqual(self.algo.status, Status.ALLTRADED)


class FakeLegAlgo(FakeAlgo):
    """两腿执行器需要的最小 algo：腿单表 + 对冲平衡判断。"""

    def __init__(self) -> None:
        super().__init__()
        self.spread = make_spread()
        self.leg_orders = {leg.vt_symbol: [] for leg in self.spread.legs.values()}
        self.leg_traded = defaultdict(float)
        self.leg_cost = defaultdict(float)
        self.hedged = True

    def is_active(self) -> bool:
        return self.status not in (Status.CANCELLED, Status.ALLTRADED)

    def is_hedge_finished(self) -> bool:
        return self.hedged


class TestTwoLegReject(unittest.TestCase):
    def test_rejected_leg_order_finishes_algo(self) -> None:
        algo = FakeLegAlgo()
        impl = TwoLegMakerImpl(algo)
        impl.on_order(make_order("1", Status.REJECTED))

        self.assertTrue(algo.stopped)
        self.assertEqual(algo.status, Status.CANCELLED)

    def test_reject_with_pending_leg_order_keeps_algo_alive(self) -> None:
        algo = FakeLegAlgo()
        impl = TwoLegMakerImpl(algo)
        algo.leg_orders["A2612.DCE"].append("CTP.9")     # 还有在途腿单
        impl.on_order(make_order("1", Status.REJECTED))
        self.assertFalse(algo.stopped)

    def test_unhedged_leg_keeps_algo_alive(self) -> None:
        algo = FakeLegAlgo()
        impl = TwoLegMakerImpl(algo)
        algo.hedged = False
        impl.on_order(make_order("1", Status.REJECTED))
        self.assertFalse(algo.stopped)


if __name__ == "__main__":
    unittest.main()
