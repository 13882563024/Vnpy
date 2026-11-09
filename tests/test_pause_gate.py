"""保留旧委托的策略暂停门禁：fake gateway 不连接真实柜台。"""
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.object import OrderData, TradeData

from execution.base import AlgoJob, AlgoKind
from execution.cross_period_algo import CrossPeriodAlgo
from tests.test_strategy import FAR, NEAR, feed_quote, make_strategy


class FakeGateway:
    def __init__(self, spread):
        self.main_engine = self
        self.spread = spread
        self.algos = {}
        self.order_algo_map = {}
        self.data_engine = SimpleNamespace(update_order_spread_map=lambda *args: None)
        self.orders = {}
        self.sent = []
        self.cancelled = []
        self.logs = []
        self.contracts = {
            leg.vt_symbol: SimpleNamespace(
                symbol=leg.vt_symbol.split('.')[0], exchange=Exchange.DCE,
                pricetick=leg.pricetick, min_volume=leg.min_volume,
                gateway_name='FAKE', vt_symbol=leg.vt_symbol,
            ) for leg in spread.legs.values()
        }
        self.contracts['SP a2701&a2703.DCE'] = SimpleNamespace(
            symbol='SP a2701&a2703', exchange=Exchange.DCE, pricetick=1,
            min_volume=1, gateway_name='FAKE', vt_symbol='SP a2701&a2703.DCE',
        )

    def get_contract(self, symbol):
        return self.contracts.get(symbol)

    def get_tick(self, symbol):
        leg = self.spread.legs.get(symbol)
        return leg.tick if leg else None

    def convert_order_request(self, req, *args):
        return [req]

    def send_order(self, req, gateway_name):
        orderid = f'{gateway_name}.{len(self.sent) + 1}'
        self.sent.append((orderid, req))
        self.orders[orderid] = req
        return orderid

    def cancel_order(self, algo, orderid):
        self.cancelled.append(orderid)

    def write_algo_log(self, algo, message):
        self.logs.append(message)

    def put_algo_event(self, algo):
        pass


def make_algo(mode='two_leg', purpose='close'):
    strategy, engine, spread = make_strategy()
    feed_quote(spread, 4000, 4001, 4102, 4103)
    gateway = FakeGateway(spread)
    engine.spread_engine = SimpleNamespace(algo_engine=gateway)
    algo = CrossPeriodAlgo(
        gateway, 'ALGO_1', spread, Direction.LONG, -100, 2, 0, 0, False,
        {'mode': mode, 'purpose': purpose, 'exchange_spread': 'SP a2701&a2703',
         'exchange_spread_vt_symbol': 'SP a2701&a2703.DCE',
         'market_close_timeout': 1, 'entry_partial_timeout': 1},
    )
    gateway.algos[algo.algoid] = algo
    strategy.algoids.add(algo.algoid)
    return strategy, gateway, algo, spread


def order_report(orderid, symbol, *, volume=2, traded=1, status=Status.PARTTRADED):
    return OrderData(
        symbol=symbol.split('.')[0], exchange=Exchange.DCE,
        orderid=orderid.split('.')[-1], type=OrderType.LIMIT,
        direction=Direction.LONG, offset=Offset.CLOSE,
        price=4001, volume=volume, traded=traded, status=status,
        datetime=datetime(2026, 11, 2, 10), gateway_name='FAKE',
    )


class TestPauseGate(unittest.TestCase):
    def test_two_leg_pause_keeps_order_and_blocks_hedge_timer_and_restart(self):
        strategy, gateway, algo, spread = make_algo()
        self.assertEqual(len(gateway.sent), 1)
        self.assertTrue(any("报单已提交" in log and "用途=close" in log
                            and "模式=two_leg" in log for log in gateway.logs),
                        gateway.logs)
        old_orderid = gateway.sent[0][0]
        strategy.pause_strategy()
        self.assertTrue(algo.paused)
        algo.update_order(order_report(old_orderid, NEAR))
        self.assertTrue(any("委托状态" in log and "仅记账" in log for log in gateway.logs),
                        gateway.logs)
        algo.update_trade(TradeData(
            symbol=NEAR.split('.')[0], exchange=Exchange.DCE,
            orderid=old_orderid.split('.')[-1], tradeid='T1',
            direction=Direction.LONG, offset=Offset.CLOSE,
            price=4001, volume=1, datetime=datetime(2026, 11, 2, 10),
            gateway_name='FAKE',
        ))
        self.assertTrue(any("暂停期间成交" in log and "仅被动记账" in log
                            for log in gateway.logs), gateway.logs)
        algo.on_interval()
        algo.impl.hedge_passive_legs()
        algo.impl.send_active_order()
        algo.stop()
        strategy.on_spread_data()
        strategy.on_stop()  # 即使调用方误用停止回调，也不能撤单
        self.assertEqual(len(gateway.sent), 1)
        self.assertEqual(gateway.cancelled, [])
        self.assertIn(old_orderid, gateway.orders)
        self.assertEqual(algo.leg_traded[NEAR], 1)
        state = strategy.get_pause_state()
        self.assertEqual(state['trades_by_order'][old_orderid], 1)
        self.assertEqual(state['orders'][old_orderid]['traded'], 1)
        self.assertFalse(strategy.resume_strategy())
        self.assertFalse(strategy.resume_strategy(reconciled=True))
        self.assertTrue(strategy.paused)

    def test_pre_pause_trade_volume_is_baseline_not_new_trade(self):
        strategy, gateway, algo, _spread = make_algo(mode='exchange_spread')
        orderid = gateway.sent[0][0]
        algo.order_trade_volume[orderid] = 1
        strategy.pause_strategy()
        self.assertEqual(strategy.get_pause_state()['trades_by_order'], {})
        algo.order_trade_volume[orderid] = 2
        self.assertEqual(strategy.get_pause_state()['trades_by_order'][orderid], 1)

    def test_exchange_order_kept_and_no_requote_after_report(self):
        strategy, gateway, algo, spread = make_algo(mode='exchange_spread')
        self.assertEqual(len(gateway.sent), 1)
        old_orderid = gateway.sent[0][0]
        strategy.pause_strategy()
        algo.update_order(order_report(old_orderid, 'SP a2701&a2703.DCE'))
        algo.on_interval()
        algo.impl.send_order()
        algo.impl.cancel_all()
        self.assertEqual(len(gateway.sent), 1)
        self.assertEqual(gateway.cancelled, [])
        self.assertIn(old_orderid, gateway.orders)
        self.assertTrue(strategy.get_pause_state()['reconciliation_required'])

    def test_strategy_callback_does_not_dispatch_old_pending_action(self):
        strategy, engine, spread = make_strategy()
        from strategies.cross_period_mean_reversion import PendingAction
        strategy.pending_open = PendingAction(kind='open', direction=1, volume=1)
        strategy.pause_strategy()
        self.assertIsNone(strategy.pending_open)
        strategy._advance_state()
        self.assertEqual(engine.algos, {})
        self.assertTrue(strategy.resume_strategy(reconciled=True))
        self.assertFalse(strategy.paused)

    def test_resume_requires_finished_orders_and_explicit_confirmation(self):
        strategy, gateway, algo, _ = make_algo(mode='exchange_spread')
        old_orderid = gateway.sent[0][0]
        strategy.pause_strategy()
        algo.update_order(order_report(old_orderid, 'SP a2701&a2703.DCE',
                                       traded=2, status=Status.ALLTRADED))
        algo.update_trade(TradeData(
            symbol='SP a2701&a2703', exchange=Exchange.DCE,
            orderid=old_orderid.split('.')[-1], tradeid='T_DONE',
            direction=Direction.LONG, offset=Offset.CLOSE,
            price=-100, volume=2, datetime=datetime(2026, 11, 2, 10),
            gateway_name='FAKE',
        ))
        self.assertFalse(strategy.resume_strategy())
        self.assertFalse(strategy.resume_strategy(reconciled=True))
        self.assertTrue(strategy.paused)  # 暂停后成交不能仅靠终结委托放行。

    def test_pause_before_init_blocks_start_and_strategy_dispatch(self):
        strategy, engine, spread = make_strategy()
        strategy.inited = False
        strategy.pause_strategy()
        self.assertEqual(strategy.start_long_algo(1, 1, 0, 0), '')
        self.assertEqual(engine.algos, {})
        strategy._load_static_config()
        self.assertTrue(strategy.paused)

    def test_exchange_leg_trade_reports_create_layer_and_preplace_exit(self):
        strategy, gateway, algo, _spread = make_algo(
            mode="exchange_spread", purpose="open")
        orderid = gateway.sent[0][0]
        for symbol, direction, price, tradeid in (
            (NEAR, Direction.LONG, 5265.0, "T_NEAR"),
            (FAR, Direction.SHORT, 5279.0, "T_FAR"),
        ):
            algo.update_trade(TradeData(
                symbol=symbol.split(".")[0], exchange=Exchange.DCE,
                orderid=orderid.split(".")[-1], tradeid=tradeid,
                direction=direction, offset=Offset.OPEN,
                price=price, volume=2, datetime=datetime(2026, 11, 2, 10),
                gateway_name="FAKE",
            ))

        self.assertEqual(algo.traded_volume, 2)
        self.assertEqual(algo.status, Status.ALLTRADED)
        self.assertAlmostEqual(algo.traded_price, -14.0)

        strategy.jobs[algo.algoid] = AlgoJob(
            algoid=algo.algoid, kind=AlgoKind.OPEN, direction=1,
            volume=2, layer_index=1, target_spread=-14.0, reason="测试开仓",
        )
        strategy.on_spread_algo(algo)
        self.assertEqual(len(strategy.layers), 1)
        self.assertTrue(any("挂出止盈单" in message for message in strategy.strategy_engine.logs),
                        strategy.strategy_engine.logs)

    def test_unknown_or_missing_exchange_mode_never_degrades_to_two_leg(self):
        strategy, gateway, algo, _ = make_algo(mode='exchange_spread')
        gateway.sent.clear()
        second = CrossPeriodAlgo(
            gateway, 'ALGO_2', gateway.spread, Direction.LONG, -100, 1, 0, 0, False,
            {'mode': 'exchange_spread', 'exchange_spread_vt_symbol': ''},
        )
        self.assertIsNone(second.impl)
        self.assertEqual(gateway.sent, [])


if __name__ == '__main__':
    unittest.main()
