"""逐笔核验派发器（OrderGate）用例：发起、等待、放行、超时、轮次失效。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderRequest

from execution.order_gate import OrderGate
from trading_core.intents import OrderIntent
from trading_core.journal import Outcome
from trading_core.preflight import GateDenied


class FakeJournal:
    def __init__(self, outcome=Outcome.NEW):
        self.outcome = outcome
        self.rows = []

    def append(self, account, day, kind, identity, payload):
        self.rows.append((account, day, kind, identity, payload))
        return len(self.rows), self.outcome


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
    def __init__(self, error=None):
        self.journal = FakeJournal()
        self.query_collector = FakeCollector()
        self.error = error
        self.calls = []

    def begin_order_check(self, intent):
        self.calls.append(('begin', intent.decision_id))
        round_ = SimpleNamespace(token='r1')
        self.query_collector.round = round_
        self.query_collector.ready = False
        return round_

    def finish_order_check(self, intent, limits):
        self.calls.append(('finish', intent.decision_id))
        if self.error is not None:
            raise self.error
        return 'EVIDENCE'


class FakeEngine:
    def __init__(self, td):
        self.td = td
        self.sent = []

    def get_gateway(self, name):
        return SimpleNamespace(td_api=self.td)

    def send_order(self, req, name):
        self.sent.append(req)
        return 'CTP.1_2_3'


def _intent(decision='d1'):
    return OrderIntent('["9999","000001"]', '20261120', 'CS_2701_2703', decision, 'tier-1',
                       'take_profit', 'SP cs2701&cs2703', 'sell', 1, -6.0, 'cfg-v1',
                       {'text': '预挂止盈'})


def _req():
    return OrderRequest(symbol='SP cs2701&cs2703', exchange=Exchange.DCE,
                        direction=Direction.SHORT, offset=Offset.CLOSE,
                        type=OrderType.LIMIT, price=-6.0, volume=1,
                        reference='CrossPeriod_CS_2701_2703_take_profit')


class OrderGateTests(unittest.TestCase):
    def setUp(self):
        self.td = FakeTd()
        self.engine = FakeEngine(self.td)
        self.clock = [100.0]
        self.gate = OrderGate(self.engine, clock=lambda: self.clock[0], timeout=10.0)

    def test_submit_opens_check_and_does_not_send(self):
        error = self.gate.submit(_req(), _intent(), limits='L')

        self.assertEqual(error, '')
        self.assertTrue(self.gate.busy)
        self.assertEqual(self.engine.sent, [])
        self.assertEqual(self.td.calls, [('begin', 'd1')])
        # 意图不在 submit 落库：由网关 reserve_and_bind 在发单前一次性写入，
        # 否则那里的 reserve 会拿到 DUPLICATE，报文变成“已预留，不重发”。
        self.assertEqual(self.td.journal.rows, [])

    def test_pump_waits_then_dispatches_with_evidence(self):
        self.gate.submit(_req(), _intent(), limits='L')

        req, orderid, error = self.gate.pump()
        self.assertEqual((orderid, error), ('', ''))  # 查询未齐

        self.td.query_collector.ready = True
        _, orderid, error = self.gate.pump()

        self.assertEqual((orderid, error), ('CTP.1_2_3', ''))
        self.assertFalse(self.gate.busy)
        req = self.engine.sent[0]
        self.assertEqual(req.extra, {'order_intent': req.extra['order_intent'],
                                     'preflight_evidence': 'EVIDENCE'})
        self.assertEqual(self.td.calls[-1], ('finish', 'd1'))

    def test_timeout_aborts_without_sending(self):
        self.gate.submit(_req(), _intent(), limits='L')
        self.clock[0] += 11

        _, orderid, error = self.gate.pump()

        self.assertEqual(orderid, '')
        self.assertIn('超时', error)
        self.assertFalse(self.gate.busy)
        self.assertEqual(self.engine.sent, [])
        self.assertTrue(self.td.query_collector.failed_reason)

    def test_round_replaced_is_reported(self):
        self.gate.submit(_req(), _intent(), limits='L')
        self.td.query_collector.round = SimpleNamespace(token='other')

        _, orderid, error = self.gate.pump()

        self.assertEqual(orderid, '')
        self.assertIn('轮次', error)
        self.assertFalse(self.gate.busy)

    def test_gate_denied_evidence_blocks_send(self):
        self.td.error = GateDenied('柜台两腿与本地账不符')
        self.gate.submit(_req(), _intent(), limits='L')
        self.td.query_collector.ready = True

        _, orderid, error = self.gate.pump()

        self.assertEqual(orderid, '')
        self.assertIn('柜台两腿与本地账不符', error)
        self.assertEqual(self.engine.sent, [])

    def test_missing_journal_disables_gate(self):
        self.td.journal = None

        self.assertFalse(self.gate.enabled)
        self.assertIn('通道', self.gate.submit(_req(), _intent(), limits='L'))


if __name__ == '__main__':
    unittest.main()
