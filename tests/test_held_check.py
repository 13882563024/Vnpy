"""专用账户有仓核验：合成 CTP 原始字段 + 临时 SQLite，绝不连接柜台。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from trading_core.ctp_facts import account_key, append_report
from trading_core.held_check import HeldLimits, held_evidence
from trading_core.intents import OrderIntent
from trading_core.journal import Journal
from trading_core.preflight import GateDenied, Preflight
from trading_core.query_round import QuerySnapshot


class HeldChecks(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.j = Journal(Path(tmp.name) / 'journal.sqlite')
        self.account = account_key('b', 'u')
        self.day = '20261119'
        self.sym = 'SP n&f'
        self.common = dict(BrokerID='b', InvestorID='u', TradingDay=self.day, ExchangeID='DCE')
        self.cash = dict(BrokerID='b', AccountID='u', TradingDay=self.day, CurrencyID='CNY', Available=10000)
        self.limits = HeldLimits('n', 'f', 'DCE', 4, 1000, True, True, True)
        self.gate = Preflight(ttl_seconds=30)
        self.orders, self.trades = [], []
        self.intent = OrderIntent(self.account, self.day, 's', 'candidate', 'L1', 'take_profit',
                                  self.sym, 'sell', 2, -1, 'v1', {'claimed_layer_lots': 9999})
        self.add_order('open', 'L1', 'buy', 2, 2, '0')
        self.positions = [dict(self.common, InstrumentID=s, PosiDirection=d, Position=2, TodayPosition=2,
                               YdPosition=0, HedgeFlag='1', LongFrozen=0, ShortFrozen=0)
                          for s, d in [('n', '2'), ('f', '3')]]

    def record(self, kind, data):
        return append_report(self.j, kind, data, 'b', 'u', self.day)

    def add_order(self, purpose, tier, side, qty, filled, status, combo=False):
        ref = str(len(self.orders) + 1)
        old = replace(self.intent, decision_id='history-' + ref, tier_id=tier,
                      purpose=purpose, side=side, lots=qty)
        old.reserve(self.j)
        self.j.append(self.account, self.day, 'order_binding', ('7', '8', ref),
                      {'strategy': 's', 'decision_id': old.decision_id, 'exchange': 'DCE'})
        d = '0' if side == 'buy' else '1'
        offset = '0' if purpose in ('open', 'add') else '1'
        o = dict(self.common, FrontID=7, SessionID=8, InstrumentID=self.sym, OrderRef=ref,
                 OrderSysID='sys' + ref, Direction=d, CombOffsetFlag=2*offset, LimitPrice=old.price,
                 VolumeTotalOriginal=qty, VolumeTraded=filled, OrderStatus=status, OrderSubmitStatus='3',
                 OrderPriceType='2', TimeCondition='3', VolumeCondition='1')
        self.orders.append(o)
        self.record('order', o)
        if filled:
            for s, direction in ([(self.sym, d)] if combo else [('n', d), ('f', '1' if d == '0' else '0')]):
                t = dict(self.common, InstrumentID=s, Direction=direction, Price=100 if s == 'n' else 101,
                         Volume=filled, OrderRef=ref, OrderSysID=o['OrderSysID'], OffsetFlag=offset, TradeID=ref+s)
                self.trades.append(t)
                self.record('fill', t)

    def check(self):
        r = self.gate.begin(self.intent, 'session')
        snapshot = QuerySnapshot(r, tuple(self.orders), tuple(self.trades), (self.cash,), tuple(self.positions))
        ev = held_evidence(self.intent, snapshot, self.j, self.limits)
        self.gate.consume(self.intent, 'session', self.gate.seal(ev))
        return ev

    def protect(self):
        self.add_order('take_profit', 'L1', 'sell', 2, 0, '3')
        self.positions[0]['ShortFrozen'] = self.positions[1]['LongFrozen'] = 2

    def candidate_add(self):
        self.intent = replace(self.intent, purpose='add', tier_id='L2', side='buy')

    def test_paired_legs_take_profit_and_exit(self):
        for purpose in ('take_profit', 'risk_exit'):
            self.intent = replace(self.intent, purpose=purpose)
            self.assertEqual(self.check().free_close_lots, 2)

    def test_add_with_confirmed_fully_frozen_top_tp(self):
        self.protect()
        self.candidate_add()
        ev = self.check()
        self.assertEqual((ev.free_open_lots, ev.free_close_lots, ev.extra_margin), (2, 0, 2000))
        for changes in ({'max_open_lots': 3}, {'margin_per_lot': 6000}, {'quotes_current': False}):
            with self.subTest(changes=changes), self.assertRaises(GateDenied):
                self.limits = replace(HeldLimits('n', 'f', 'DCE', 4, 1000, True, True, True), **changes)
                self.check()

    def test_filled_terminal_partial_tp_releases_only_residual(self):
        self.add_order('take_profit', 'L1', 'sell', 2, 1, '5', combo=True)
        for p in self.positions:
            p.update(Position=1, TodayPosition=1)
        with self.assertRaises(GateDenied):
            self.check()
        self.intent = replace(self.intent, lots=1)
        self.assertEqual(self.check().free_close_lots, 1)

    def test_missing_wrong_and_used_layer_no_add_without_tp(self):
        for changes in ({'lots': 3}, {'tier_id': 'L0'}, {'side': 'buy'}, {'decision_id': 'history-1'},
                        {'purpose': 'add', 'tier_id': 'L2', 'side': 'buy'}):
            original = self.intent
            with self.subTest(changes=changes), self.assertRaises(GateDenied):
                self.intent = replace(original, **changes)
                self.check()
            self.intent = original

    def test_active_tp_blocks_double_close_same_tier_add_and_wrong_freezes(self):
        self.protect()
        with self.assertRaises(GateDenied):
            self.check()
        self.intent = replace(self.intent, purpose='add', side='buy')
        with self.assertRaises(GateDenied):
            self.check()
        self.candidate_add()
        self.positions[0]['ShortFrozen'] -= 1
        with self.assertRaises(GateDenied):
            self.check()

    def test_positions_must_match_each_leg_direction_history_and_exchange(self):
        for changes in ({'InstrumentID': 'other'}, {'PosiDirection': '3'}, {'TodayPosition': 1},
                        {'YdPosition': 1}, {'LongFrozen': 1}, {'Position': 1}, {'ExchangeID': 'OTHER'},
                        {'InvestorID': 'unknown'}, {'HedgeFlag': '2'}, {'ShortFrozen': None}):
            original = self.positions[0]
            with self.subTest(changes=changes), self.assertRaises(GateDenied):
                self.positions[0] = dict(original, **changes)
                self.check()
            self.positions[0] = original

    def test_active_cancel_request_not_resolved_by_old_working_snapshot(self):
        self.protect()
        self.candidate_add()
        self.record('order', dict(self.orders[-1], OrderSubmitStatus='1'))
        with self.assertRaises(GateDenied):
            self.check()

    def test_wrong_tier_even_when_gross_position_can_cover_close(self):
        self.add_order('add', 'L2', 'buy', 1, 1, '0')
        for p in self.positions:
            p.update(Position=3, TodayPosition=3)
        with self.assertRaises(GateDenied):
            self.check()
        self.intent = replace(self.intent, tier_id='L2', lots=2)
        with self.assertRaises(GateDenied):
            self.check()
        self.intent = replace(self.intent, lots=1)
        self.assertEqual(self.check().free_close_lots, 1)

    def test_partial_query_and_unbound_work(self):
        for seq in (self.orders, self.trades):
            row = seq.pop()
            with self.assertRaises(GateDenied):
                self.check()
            seq.append(row)
        self.orders.append(dict(self.orders[0], OrderRef='foreign'))
        with self.assertRaises(GateDenied):
            self.check()
        self.orders.pop()
        replace(self.intent, decision_id='pending').reserve(self.j)
        with self.assertRaises(GateDenied):
            self.check()

    def test_active_open_blocks_even_if_zero_filled(self):
        self.add_order('add', 'L2', 'buy', 1, 0, '3')
        with self.assertRaises(GateDenied):
            self.check()

    def test_orphan_leg_fill_even_when_snapshot_contains_it(self):
        orphan = dict(self.trades[0], TradeID='other', OrderSysID='unbound')
        self.record('fill', orphan)
        self.trades.append(orphan)
        with self.assertRaises(GateDenied):
            self.check()

    def test_mixed_combo_and_derived_fills(self):
        mixed = dict(self.trades[0], InstrumentID=self.sym, TradeID='other')
        self.record('fill', mixed)
        self.trades.append(mixed)
        with self.assertRaises(GateDenied):
            self.check()

    def test_replay_across_all_days_rejects_prior_pending(self):
        replace(self.intent, day='20261118', decision_id='orphan').reserve(self.j)
        with self.assertRaisesRegex(GateDenied, '跨交易日'):
            self.check()

    def test_other_account_is_isolated_but_own_fact_conflict_is_fatal(self):
        replace(self.intent, account='other', day='20261118').reserve(self.j)
        self.check()
        self.record('fill', dict(self.trades[0], Price=999))
        with self.assertRaisesRegex(GateDenied, '冲突'):
            self.check()
