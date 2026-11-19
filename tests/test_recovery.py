"""复用同一套合成 CTP 柜台/SQLite 夹具，不调用交易接口。"""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from unittest import TestCase

from tests import test_held_check as fixtures
from trading_core.journal import Journal
from trading_core.preflight import GateDenied
from trading_core.query_round import QuerySnapshot
from trading_core.recovery import RecoveryScope, audit_rollover, build_baseline


class Rollover(TestCase):
    def setUp(self):
        f = self.f = fixtures.HeldChecks()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.scope = RecoveryScope(f.account, 's', 'v1', 'DCE', 'n', 'f')
        self.round = f.gate.begin(f.intent, 'previous')
        self.j = Journal.open_readonly(f.j.path)

    def prior(self):
        f = self.f
        return QuerySnapshot(self.round, tuple(f.orders), tuple(f.trades), (f.cash,), tuple(f.positions))

    def current(self):
        r = replace(self.round, day='20261120', session='new', token='fresh')
        return QuerySnapshot(r, (), (), (dict(self.f.cash, TradingDay=r.day),), tuple(
            dict(p, TradingDay=r.day, TodayPosition=0, YdPosition=p['Position']) for p in self.f.positions))

    def build(self):
        return build_baseline(self.scope, self.prior(), self.j)

    def audit(self, baseline, current=None, day=None):
        return audit_rollover(baseline, self.prior(), current or self.current(), self.j,
                              previous_trading_day=day or self.round.day)

    def test_partial_close_keeps_entry_cost_and_both_tiers_in_intent_order(self):
        f = self.f
        f.add_order('take_profit', 'L1', 'sell', 2, 1, '5', combo=True)
        f.add_order('add', 'L2', 'buy', 1, 1, '0', combo=True)
        before = list(self.j.replay())
        b = self.build()
        self.assertEqual([(l.tier_id, l.side, l.lots, l.open_day, l.spread_cost, l.near_cost, l.far_cost)
                          for l in b.layers], [('L1', 'buy', 1, f.day, Decimal(-1), Decimal(100), Decimal(101)),
                                              ('L2', 'buy', 1, f.day, Decimal(101), None, None)])
        with self.assertRaises(FrozenInstanceError):
            b.layers[0].lots = 10
        self.assertTrue(self.audit(b).matched)
        self.assertEqual(list(self.j.replay()), before)

    def test_saved_full_batches_support_offline_audit(self):
        from trading_core.account_view import record_snapshot
        from trading_core.snapshot_archive import load_batch
        prior,current=self.prior(),self.current()
        for snap in (prior,current): record_snapshot(self.f.j,snap)
        old,new=(load_batch(self.j,account=self.f.account,day=s.round.day,token=s.round.token) for s in (prior,current))
        baseline=build_baseline(self.scope,old,self.j)
        self.assertTrue(audit_rollover(baseline,old,new,self.j,previous_trading_day=prior.round.day).matched)

    def test_short_legs_verified_individually(self):
        self.f.add_order('risk_exit', 'L1', 'sell', 2, 2, '0')
        self.f.add_order('open', 'short1', 'sell', 2, 2, '0')
        for p in self.f.positions:
            p['PosiDirection'] = '2' if p['PosiDirection'] == '3' else '3'
        b = self.build()
        self.assertEqual(b.layers[0].side, 'sell')
        self.assertTrue(self.audit(b).matched)

    def test_partial_today_yesterday_freeze_wrong_side_and_missing_legs_audited(self):
        b, c = self.build(), self.current()
        for patch in ({'TodayPosition': 1, 'YdPosition': 1}, {'Position': 3}, {'ShortFrozen': 1},
                      {'PosiDirection': '3'}, {'InvestorID': 'other'}, {'ExchangeID': 'other'},
                      {'HedgeFlag': '2'}, {'YdPosition': None}):
            with self.subTest(patch=patch):
                out = self.audit(b, replace(c, positions=(dict(c.positions[0], **patch), c.positions[1])))
                self.assertFalse(out.matched)
                self.assertTrue(out.review_steps)
        for rows in (c.positions[:1], c.positions + c.positions[:1],
                     c.positions + (dict(c.positions[0], InstrumentID='foreign'),)):
            self.assertFalse(self.audit(b, replace(c, positions=rows)).matched)

    def test_queried_new_activities_wrong_calendar_and_tampered_baseline(self):
        b, c = self.build(), self.current()
        for kind in ('orders', 'trades'):
            row = dict(getattr(self.f, 'orders' if kind == 'orders' else 'trades')[0], TradingDay=c.round.day)
            self.assertFalse(self.audit(b, replace(c, **{kind: (row,)})).matched)
        self.assertFalse(self.audit(b, day='20261118').matched)
        self.assertFalse(self.audit(b, replace(c, round=self.round)).matched)
        self.assertFalse(self.audit(replace(b, layers=(replace(b.layers[0], spread_cost=Decimal(0)),))).matched)
        self.assertFalse(self.audit(replace(b, evidence_digest='forged')).matched)

    def test_prior_account_and_order_evidence_required(self):
        p = self.prior()
        for changes in ({'accounts': ()}, {'orders': ()}, {'trades': p.trades[:1]}, {'positions': ()},
                        {'positions': (dict(p.positions[0], YdPosition=2), p.positions[1])}):
            with self.subTest(changes=changes), self.assertRaises(GateDenied):
                build_baseline(self.scope, replace(p, **changes), self.j)

    def test_prior_active_order_and_unbound_pending_rejected(self):
        self.f.protect()
        with self.assertRaisesRegex(GateDenied, '未终结'):
            self.build()
        replace(self.f.intent, decision_id='orphan').reserve(self.f.j)
        with self.assertRaisesRegex(GateDenied, '未决'):
            self.build()

    def test_terminal_followed_by_new_active_history_is_not_closed(self):
        self.f.record('order', dict(self.f.orders[0], OrderStatus='3', VolumeTraded=0))
        with self.assertRaisesRegex(GateDenied, '未闭合'):
            self.build()

    def test_mixed_and_conflicting_reports_rejected(self):
        f = self.f
        mixed = dict(f.trades[0], InstrumentID=f.sym, TradeID='mix')
        f.record('fill', mixed)
        f.trades.append(mixed)
        with self.assertRaisesRegex(GateDenied, '混合'):
            self.build()
        f.record('fill', dict(mixed, Price=40))
        with self.assertRaisesRegex(GateDenied, '冲突'):
            self.build()

    def test_late_fact_cannot_hide_behind_previously_valid_baseline(self):
        b = self.build()
        replace(self.f.intent, day='20261120', decision_id='unsent').reserve(self.f.j)
        self.assertFalse(self.audit(b).matched)

    def test_unrelated_old_pending_not_silently_filtered(self):
        replace(self.f.intent, day='20261118').reserve(self.f.j)
        with self.assertRaisesRegex(GateDenied, '其它交易日'):
            self.build()
