"""真实 begin/finish、查询回包和风险路由；仅使用无网络的 PaperGateway。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from vnpy.trader.constant import Exchange
from gateway.paper_spread import PaperGateway, PaperMainEngine
from trading_core.ctp_facts import account_key
from trading_core.flat_check import FirstEntryLimits
from trading_core.intents import OrderIntent
from trading_core.journal import Journal
from trading_core.runtime_bridge import RuntimeBridge, RuntimeValidation


class BridgeTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.now = 0.
        self.source = Journal(root / 'shadow.sqlite')
        config = SimpleNamespace(near='a2701', far='a2703', symbol='SP a2701&a2703', tick=1, max_lots=3)
        self.gw = PaperGateway(SimpleNamespace(config=config, exchange='DCE'), root / 'facts.sqlite',
                               'a', '20261119', 5000., lambda: self.now)
        self.addCleanup(self.gw.close)
        self.engine = PaperMainEngine(self.gw)
        self.intent = OrderIntent(account_key('paper', 'a'), '20261119', 'strat', 'd1', 'L1', 'open',
                                  config.symbol, 'buy', 1, -5., 'v1', {'shadow_only': True})
        self.intent.reserve(self.source)
        self.limits = FirstEntryLimits(3, 300., True, True, True)
        self.check = Mock(return_value=RuntimeValidation('v1', self.limits))
        self.kw = dict(gateway_name='PAPER', source_journal=self.source, account=self.intent.account,
                       strategy='strat', mode='paper', revalidate=self.check, clock=lambda: self.now)
        self.bridge = RuntimeBridge(self.engine, **self.kw)
        self.addCleanup(self.bridge.close)

    def start(self):
        return self.bridge.begin(self.intent, exchange=Exchange.DCE)

    def advance(self, n=4):
        for _ in range(n):
            self.now += 1.
            self.gw.td_api.advance_order_check()

    def test_complete_round_once_via_engine(self):
        self.assertEqual(self.start().state, 'checking')
        self.advance(3)
        self.assertEqual(self.bridge.poll().state, 'checking')
        self.check.assert_not_called()
        self.advance(1)
        with patch.object(self.engine, 'send_order', wraps=self.engine.send_order) as route:
            result = self.bridge.poll()
            self.assertEqual(result.state, 'submitted', self.gw.messages)
            self.assertEqual(self.bridge.poll(), result)
            self.assertEqual(self.start().state, 'duplicate')
            route.assert_called_once()
            req, name = route.call_args.args
            self.assertEqual((name, req.extra['order_intent']), ('PAPER', self.intent))
            self.assertEqual(req.extra['preflight_evidence'].round.decision, 'd1')
        self.check.assert_called_once_with(self.intent)
        self.assertEqual([r.identity[-1] for r in self.source.replay() if r.kind == 'runtime_bridge'],
                         ['claimed', 'checking', 'dispatching', 'submitted'])
        self.assertEqual(len(self.gw.orders), 1)
        self.assertTrue(any(r.kind == 'order_binding' for r in self.gw.td_api.journal.replay()))

    def test_no_live_route_or_forged_attribution(self):
        with self.assertRaises(ValueError): RuntimeBridge(Mock(), **self.kw)
        with self.assertRaises(ValueError): RuntimeBridge(self.engine, **dict(self.kw, mode='live'))
        self.assertEqual(self.bridge.begin(replace(self.intent, price=0), exchange=Exchange.DCE).state, 'denied')
        self.assertEqual(self.bridge.begin(replace(self.intent, strategy='other'), exchange=Exchange.DCE).state, 'denied')
        self.assertFalse(self.gw.td_api.query_collector.active)

    def test_query_failure_does_not_revalidate_or_retry(self):
        self.start()
        self.gw.td_api.query_collector.abort('query error')
        self.assertIn('query error', self.bridge.poll().reason)
        self.check.assert_not_called()
        self.assertEqual(self.start().state, 'duplicate')

    def test_never_aborts_another_round(self):
        self.start()
        a = self.gw.td_api
        a.query_collector.abort('failed')
        other = a.begin_order_check(replace(self.intent, decision_id='someone-else'))
        self.assertEqual(self.bridge.poll().state, 'denied')
        self.assertEqual(a.query_collector.current_round, other)
        self.assertFalse(self.gw.orders)

    def test_source_conflict_during_check(self):
        self.start(); self.advance()
        replace(self.intent, price=-6).reserve(self.source)
        self.assertEqual(self.bridge.poll().state, 'unknown')
        self.assertFalse(self.gw.orders)

    def test_state_change_after_last_prevents_send(self):
        self.start(); self.advance()
        self.gw.td_api.preflight.invalidate()
        self.assertEqual(self.bridge.poll().state, 'denied')
        self.assertFalse(self.gw.orders)

    def test_timeout_while_waiting(self):
        self.start(); self.advance(1)
        self.now = 20.
        self.assertIn('超时', self.bridge.poll().reason)
        self.assertFalse(self.gw.td_api.query_collector.active)
        self.assertEqual(self.start().state, 'duplicate')

    def test_configuration_changed_after_query(self):
        self.start(); self.advance()
        self.check.return_value = RuntimeValidation('v2', self.limits)
        self.assertEqual(self.bridge.poll().state, 'denied')
        self.check.assert_called_once()
        self.assertFalse(self.gw.orders)

    def test_stale_quotes_after_query(self):
        self.start(); self.advance()
        self.check.return_value = RuntimeValidation('v1', replace(self.limits, quotes_current=False))
        self.assertEqual(self.bridge.poll().state, 'denied')
        self.assertFalse(self.gw.orders)

    def test_risk_path_no_ack_freezes_without_replay(self):
        self.start(); self.advance()
        # Real vnpy RiskEngine rejects before reaching the paper counter.
        self.gw.config.max_lots = 0
        with patch.object(self.engine, 'send_order', wraps=self.engine.send_order) as route:
            self.assertEqual(self.bridge.poll().state, 'unknown')
            self.assertEqual(self.start().state, 'unknown')
            self.bridge.poll(); route.assert_called_once()
        self.assertFalse(self.gw.orders)
        self.bridge.close()
        restarted = RuntimeBridge(self.engine, **self.kw)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.status.state, 'unknown')

    def test_restart_after_intent_claimed_before_persisted_reply(self):
        self.bridge.close()
        self.source.append(self.intent.account, self.intent.day, 'runtime_bridge',
                           (self.intent.strategy, self.intent.decision_id, 'dispatching'),
                           {'reason': 'interrupt', 'order_id': ''})
        restarted = RuntimeBridge(self.engine, **self.kw)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.status.state, 'unknown')
        self.assertEqual(restarted.begin(self.intent, exchange=Exchange.DCE).state, 'unknown')
        self.assertFalse(self.gw.orders)
