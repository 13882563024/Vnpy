"""账户查询协议，纯内存虚拟时钟；不使用柜台、OMS 或真实睡眠。"""
from dataclasses import FrozenInstanceError, replace
from itertools import count
import unittest

from trading_core.ctp_facts import account_key
from trading_core.preflight import QueryRound, GateDenied
from trading_core.query_round import QueryCollector


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.scope = QueryRound('token', account_key('B', 'I'), '20261119', '2/3', 's', 'd', 5, self.now)
        self.state = dict(connected=True, version=5)
        self.ids = count(1).__next__
        self.c = QueryCollector(clock=lambda: self.now)
        self.c.begin(self.scope)
        self.identity = dict(BrokerID='B', InvestorID='I', TradingDay='20261119')
        self.cash = dict(BrokerID='B', AccountID='I', TradingDay='20261119', Available=100)
        self.order = dict(self.identity, InstrumentID='SP A&B', FrontID=1, SessionID=2,
                          OrderRef='3', OrderStatus='1', VolumeTotalOriginal=2, VolumeTraded=1)
        self.fill = dict(self.identity, InstrumentID='A', ExchangeID='DCE', TradeID='7', Volume=1)
        self.position = dict(self.identity, InstrumentID='B', PosiDirection='3', Position=1)

    def next(self):
        return self.c.next_request(**self.state, allocate_reqid=self.ids)

    def feed(self, req, data=None, last=True, error=None):
        return self.c.feed(req.kind, req.reqid, data, last, error=error, **self.state)

    def snapshot(self):
        return self.c.take_snapshot(**self.state)

    def finish(self):
        for row in ({}, {}, self.cash, {}):
            self.assertTrue(self.feed(self.next(), row))
            self.now += 1

    def failed(self):
        self.assertFalse(self.c.active)
        self.assertTrue(self.c.failed_reason)
        self.assertIsNone(self.snapshot())

    def test_paced_multiframe_single_round_read_only(self):
        for index, (kind, row) in enumerate(zip(('order','trade','account','position'),
                (self.order, self.fill, self.cash, self.position)), 1):
            req = self.next()
            self.assertEqual((req.kind, req.reqid), (kind, index))
            with self.assertRaises(GateDenied):
                self.c.begin(replace(self.scope, token='another'))
            self.assertTrue(self.feed(req, row, False))
            self.assertIsNone(self.next())
            self.assertIsNone(self.snapshot())
            self.assertTrue(self.feed(req, {}))
            self.assertFalse(self.feed(req, row))
            self.assertIsNone(self.next())
            self.now += 1
        snap = self.snapshot()
        self.assertEqual(snap.round, self.scope)
        self.assertEqual((snap.orders[0], snap.trades[0], snap.accounts[0], snap.positions[0]),
                         (self.order, self.fill, self.cash, self.position))
        self.order.clear()
        self.assertEqual(snap.orders[0]['OrderRef'], '3')
        with self.assertRaises(TypeError):
            snap.orders[0]['OrderRef'] = '5'
        with self.assertRaises(FrozenInstanceError):
            snap.orders = ()
        self.assertIsNone(self.snapshot())

    def test_peek_ownership_and_ready_never_consumes_snapshot(self):
        self.assertIs(self.c.current_round, self.scope)
        self.assertFalse(self.c.ready)
        self.finish()
        self.assertTrue(self.c.ready)
        self.assertIs(self.snapshot().round, self.scope)
        self.assertIsNone(self.c.current_round)
        self.assertFalse(self.c.ready)
        another = replace(self.scope, token='another', started=self.now)
        self.c.begin(another)
        self.assertIs(self.c.current_round, another)
        self.assertFalse(self.c.ready)
        self.c.abort('断线')
        self.assertIsNone(self.c.current_round)
        self.assertFalse(self.c.ready)

    def test_empty_last_means_complete_except_account(self):
        self.finish()
        snap = self.snapshot()
        self.assertEqual((snap.orders, snap.trades, snap.positions), ((), (), ()))
        self.c.begin(replace(self.scope, token='two', started=self.now))
        for _ in range(2):
            self.assertTrue(self.feed(self.next()))
            self.now += 1
        self.assertFalse(self.feed(self.next()))
        self.failed()

    def test_old_reqid_cannot_finish_new_round_and_rate_survives_abort(self):
        old = self.next()
        self.c.abort('发送返回限流 -3')
        self.failed()
        self.c.begin(replace(self.scope, token='new'))
        self.assertIsNone(self.next())
        self.now += 1
        req = self.next()
        self.assertFalse(self.feed(old, self.order))
        self.assertIsNone(self.next())
        self.assertTrue(self.feed(req))
        self.now += 1
        self.assertIsNone(self.c.next_request(**self.state, allocate_reqid=lambda:old.reqid))
        self.failed()

    def test_own_reqid_wrong_kind_or_nonbool_last_aborts(self):
        for kind,last in [('trade', True),('order', 1)]:
            self.setUp()
            self.assertFalse(self.c.feed(kind,self.next().reqid, self.order, last, **self.state))
            self.failed()

    def test_scope_malformed_rows_and_unknown_errors_never_publish(self):
        for row,error,last in [(dict(self.order, BrokerID='wrong'),{},True),
                               (dict(self.order, TradingDay='20261118'),None,True),
                               (dict(self.order, OrderStatus='a'),{},True),
                               (dict(self.order, VolumeTraded=3),{},True),
                               (dict(self.order, Extra=[]),{},True),
                               ({},None,False), (None, {'ErrorID':-3},True),
                               (self.order, {'ErrorMsg':'unknown'}, True)]:
            with self.subTest(row=row,error=error):
                self.setUp()
                self.assertFalse(self.feed(self.next(),row,last,error))
                self.failed()

    def test_unknown_cash_fill_or_position_is_not_zero(self):
        for stage,bad in [(1,dict(self.fill, TradeID='')), (2,dict(self.cash, Available=None)),
                          (2,dict(self.cash, Available=float('nan'))),
                          (3,dict(self.position, PosiDirection='1'))]:
            self.setUp()
            for row in [{}, {}, self.cash][:stage]:
                self.assertTrue(self.feed(self.next(),row))
                self.now += 1
            self.assertFalse(self.feed(self.next(),bad))
            self.failed()

    def test_timeout_on_receive_and_per_round_override(self):
        self.c.abort('reset')
        self.c.begin(self.scope,timeout=.25)
        req = self.next()
        self.now += .25
        self.assertFalse(self.feed(req,self.order))
        self.failed()
        self.assertIn('超时', self.c.failed_reason)

    def test_timer_and_final_handoff_recheck_version_connection_and_clock(self):
        for completed in (False,True):
            for changes,elapsed in [({'connected':False},0),({'version':6},0),({},12),({},-2)]:
                with self.subTest(completed=completed,changes=changes,elapsed=elapsed):
                    self.setUp()
                    if completed:
                        self.finish()
                    self.state.update(changes)
                    self.now += elapsed
                    self.assertIsNone(self.snapshot() if completed else self.next())
                    self.failed()

    def test_invalid_timing_configuration_and_expired_input(self):
        for value in (None, 0, True, float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError):
                QueryCollector(timeout=value)
            with self.assertRaises(ValueError):
                QueryCollector(interval=value)
        self.c.abort('reset')
        self.now += 12
        with self.assertRaises(GateDenied):
            self.c.begin(self.scope)
