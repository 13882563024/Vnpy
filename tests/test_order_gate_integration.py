"""算法 ↔ 逐笔核验的端到端串联：先申请核验，四项查询齐了才真正发单。"""
from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vnpy.trader.constant import Direction, Exchange
from vnpy_spreadtrading.engine import SpreadAlgoEngine

from execution.cross_period_algo import CrossPeriodAlgo
from tests.test_executor_reject import make_spread
from trading_core.intents import OrderIntent
from trading_core.journal import Outcome


class FakeJournal:
    def __init__(self):
        self.rows = []

    def append(self, account, day, kind, identity, payload):
        self.rows.append((account, day, kind, identity, payload))
        return len(self.rows), Outcome.NEW


class FakeCollector:
    def __init__(self):
        self.round = None
        self.ready = False
        self.failed_reason = ''

    @property
    def current_round(self):
        return self.round

    def abort(self, reason):
        self.failed_reason = str(reason)
        self.round = None


class FakeTd:
    def __init__(self):
        self.brokerid = '9999'
        self.userid = '000001'
        self.cabinet_trading_day = '2026-11-20'
        self.journal = FakeJournal()
        self.query_collector = FakeCollector()
        self.finish_calls = []

    def begin_order_check(self, intent):
        round_ = SimpleNamespace(token='r1')
        self.query_collector.round = round_
        return round_

    def finish_order_check(self, intent, limits):
        self.finish_calls.append((intent, limits))
        return 'EVIDENCE'


class IntegrationTests(unittest.TestCase):
    def _algo(self, extra_overrides=None, *, send_result='CTP.1', gate=True):
        engine = SpreadAlgoEngine.__new__(SpreadAlgoEngine)
        engine.order_algo_map = {}
        engine.data_engine = Mock()
        contract = SimpleNamespace(symbol='SP A2612&A2701', exchange=Exchange.DCE,
                                   vt_symbol='SP A2612&A2701.DCE', gateway_name='CTP',
                                   pricetick=1, min_volume=1)
        engine.get_contract = Mock(return_value=contract)
        engine.get_tick = Mock(return_value=SimpleNamespace(
            bid_price_1=1.0, ask_price_1=1.1, datetime=datetime.now()))
        engine.write_algo_log = Mock()
        engine.cancel_order = Mock()
        engine.put_algo_event = Mock()
        td = FakeTd()
        main_engine = SimpleNamespace(
            send_order=Mock(return_value=send_result),
            get_all_active_orders=lambda: [],
        )
        if gate:
            main_engine.get_gateway = lambda _: SimpleNamespace(td_api=td)
        engine.main_engine = main_engine
        extra = {
            'purpose': 'take_profit', 'mode': 'exchange_spread',
            'exchange_spread': contract.symbol,
            'exchange_spread_vt_symbol': contract.vt_symbol,
            'tier_id': 'tier-1', 'near': 'A2612', 'far': 'A2701',
            'near_vt': 'A2612.DCE', 'far_vt': 'A2701.DCE', 'exchange': 'DCE',
            'max_open_lots': 3, 'margin_per_lot': 5000.0,
            'config_version': 'cfg-v1', 'trading_sessions': 'always',
        }
        extra.update(extra_overrides or {})
        with patch.object(CrossPeriodAlgo, '_quotes_current', return_value=True), \
                patch.object(CrossPeriodAlgo, '_session_open', return_value=True):
            algo = CrossPeriodAlgo(engine, 'algo', make_spread(), Direction.LONG, -14,
                                   1, 0, 0, False, extra)
        engine.algos = {'algo': algo}
        return algo, engine, td

    def test_rejected_send_finishes_algo_so_strategy_replaces(self):
        """发送路径返回空单号时不会再有回报，算法必须收尾，策略才会重挂。"""
        algo, engine, td = self._algo(send_result='', gate=False)

        engine.main_engine.send_order.assert_called_once()
        self.assertTrue(algo.stopped)
        self.assertTrue(any('组合单发送失败' in log for log in self._logs(engine)),
                        self._logs(engine))

    def test_first_attempt_only_starts_the_check(self):
        algo, engine, td = self._algo()

        engine.main_engine.send_order.assert_not_called()
        self.assertTrue(any('已申请逐笔核验' in log for log in self._logs(engine)), self._logs(engine))

    def test_dispatch_happens_after_queries_complete(self):
        algo, engine, td = self._algo()
        td.query_collector.ready = True

        algo.on_interval()

        engine.main_engine.send_order.assert_called_once()
        req = engine.main_engine.send_order.call_args[0][0]
        self.assertIsInstance(req.extra['order_intent'], OrderIntent)
        self.assertEqual(req.extra['preflight_evidence'], 'EVIDENCE')
        self.assertEqual(algo.impl.order_ids, ['CTP.1'])
        self.assertIn('CTP.1', algo.impl.active_order_ids)
        self.assertEqual(algo.impl.fill_baseline['CTP.1'], 0.0)

    def test_busy_checker_is_queued_and_retried(self):
        """柜台一次只允许一轮逐笔查询；排不上要每秒重试，不能一次失败就放弃。"""
        calls = {'n': 0}
        original = FakeTd.begin_order_check

        def flaky(td_self, intent):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('已有逐笔查询进行中')
            return original(td_self, intent)

        with patch.object(FakeTd, 'begin_order_check', flaky):
            algo, engine, td = self._algo()

        algo._quotes_current = lambda extra: True
        algo._session_open = lambda spec: True
        self.assertIsNotNone(algo._gate_wait_req)
        engine.main_engine.send_order.assert_not_called()

        td.query_collector.ready = True
        algo.on_interval()      # 重试申请，排上
        algo.on_interval()      # 推进核验并派发

        engine.main_engine.send_order.assert_called_once()
        self.assertIsNone(algo._gate_wait_req)
        self.assertEqual(algo.impl.order_ids, ['CTP.1'])

    def test_take_profit_check_uses_settled_layer_book(self):
        # Even when a combination fill has not updated SpreadData.leg_pos,
        # verification compares the fresh cabinet snapshot with the settled layers.
        algo, engine, td = self._algo({'expected_close_legs': (2, -2)})
        self.assertFalse(algo.spread.leg_pos)
        td.query_collector.ready = True
        algo.on_interval()

        engine.main_engine.send_order.assert_called_once()
        limits = td.finish_calls[0][1]
        self.assertEqual((limits.expected_near, limits.expected_far), (2, -2))

    def test_queued_check_failure_releases_algo_slot(self):
        with patch.object(FakeTd, 'begin_order_check', side_effect=RuntimeError('已有逐笔查询进行中')):
            algo, engine, td = self._algo()
        with patch.object(td, 'begin_order_check', side_effect=RuntimeError('会话未确认')):
            algo.on_interval()

        engine.main_engine.send_order.assert_not_called()
        self.assertTrue(algo.stopped)
        self.assertFalse(algo.is_gate_pending())

    def test_timeout_is_reported_and_nothing_is_sent(self):
        algo, engine, td = self._algo()
        algo._gate.timeout = 0.0
        algo._gate._pending.started -= 1

        algo.on_interval()

        engine.main_engine.send_order.assert_not_called()
        self.assertTrue(any('核验未通过' in log or '超时' in log for log in self._logs(engine)),
                        self._logs(engine))
        # 算法必须结束，策略才能拿到终态并重挂；否则 layer.algoid 永远占着止盈位。
        self.assertTrue(algo.stopped)

    def test_paused_algo_drops_pending_check(self):
        algo, engine, td = self._algo()

        algo.pause()

        self.assertFalse(algo._gate.busy)
        self.assertTrue(any('放弃' in log for log in self._logs(engine)), self._logs(engine))

    @staticmethod
    def _logs(engine):
        return [str(call.args[-1]) for call in engine.write_algo_log.call_args_list]


if __name__ == '__main__':
    unittest.main()
