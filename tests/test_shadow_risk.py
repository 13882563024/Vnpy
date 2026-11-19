"""Offline, journal-backed risk tests. Every position starts with a paired fill."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_core.journal import Journal
from trading_core.layers import Working, Unknown, Terminal
from trading_core.paired_fills import Matched
from trading_core.shadow_strategy import ShadowConfig, ShadowStrategy, EntryChecks, LegQuote, FeeEstimate


class ShadowRiskTests(unittest.TestCase):
    day = '20261119'

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.j = Journal(Path(tmp.name) / 'shadow.db')
        self.c = ShadowConfig('v', 'N', 'F', 'N&F', 1, 10, min_samples=3,
                              min_span=2, layer_step=0, max_jump_ticks=0)
        self.seq = 0

    def restart(self):
        self.s = ShadowStrategy(self.j, self.c, account='a', strategy='s')

    def quote(self, mid, *, delivery=20, ordinal=100, can_open=True):
        self.seq += 1
        t = self.seq
        n = LegQuote('N', 'n'+str(t), t, 100+mid, 101+mid, 2, 2, 2)
        f = LegQuote('F', 'f'+str(t), t, 100, 101, 2, 2, 2)
        return self.s.on_quote(self.day, n, f, now=t,
            checks=EntryChecks(True, can_open, True, True, True, delivery, ordinal))

    def first(self, mid, fill):
        self.restart()
        self.quote(-1, can_open=False)
        self.quote(0, can_open=False)
        order = self.quote(mid).intent
        self.fill(order, order.lots, fill)
        self.status(order, Terminal, order.lots)
        return order

    def fill(self, order, qty, price):
        m = Matched(qty, Decimal(str(price)), None, None, 0, 0)
        return self.s.on_matched(order.day, f'{order.decision_id}:{qty}', order.decision_id, m)

    def status(self, order, cls, *args):
        return self.s.on_status(self.day, f'{cls.__name__}:{order.decision_id}', cls(order.decision_id, *args))

    def tp(self, tier):
        fees = FeeEstimate(tier, self.day, self.day, 1, 1, 1, 1, 'schedule')
        return self.s.protect(self.day, 'tp'+tier, fees).intent

    def test_short_stop_zero_ask_exact_threshold_partial_and_replay(self):
        self.c = replace(self.c, allow_stop_loss=True, stop_loss=60, layer_lots=2)
        self.first(8, -1)
        self.day = '20261120'  # fresh empty statistical window, no entry permit
        stop = self.quote(-1, ordinal=101, can_open=False).intent
        self.assertEqual((stop.purpose, stop.reason['risk_rule'], stop.reason['gross_pnl'],
                          stop.price, stop.side, stop.lots), ('risk_exit', 'layer_stop', '-20', 0, 'buy', 2))
        self.assertEqual(self.s.book.layers[0].remaining, 2)
        self.fill(stop, 1, 0)
        self.assertIsNone(self.quote(-10, ordinal=101).intent)
        self.status(stop, Terminal, 1)
        self.restart()
        retry = self.quote(-10, ordinal=101).intent
        self.assertEqual((retry.tier_id, retry.lots, retry.price), ('L1', 1, -9))
        self.fill(retry, 1, -9)
        self.status(retry, Terminal, 1)
        before = list(self.j.replay())
        self.restart()
        self.assertEqual(self.s.book.layers, ())
        self.assertEqual(before, list(self.j.replay()))

    def test_roll_beats_layer_and_time_cancel_unknown_and_late_fill(self):
        self.c = replace(self.c, allow_stop_loss=True, stop_loss=3000, max_hold_days=1)
        self.first(-8, 85)
        tp = self.tp('L1')
        self.status(tp, Working)
        add = self.quote(-12).intent
        self.day = '20261123'
        result = self.quote(-20, delivery=1, ordinal=102)
        self.assertEqual({a.order_id for a in result.cancels}, {tp.decision_id, add.decision_id})
        self.assertIsNone(result.intent)
        self.status(add, Unknown)
        self.assertIsNone(self.tp('L1'))
        self.status(tp, Terminal, 0)
        self.status(add, Terminal, 1)  # detail not yet received: no replacement
        self.restart()
        waiting = self.quote(-25, delivery=20, ordinal=102)
        self.assertEqual((waiting.intent, waiting.cancels), (None, ()))
        self.fill(add, 1, -13)
        for tier in ('L2', 'L1'):
            close = self.quote(-20, ordinal=102).intent
            self.assertEqual((close.reason['risk_rule'], close.tier_id, close.price), ('roll_exit', tier, -21))
            self.fill(close, 1, -21)
            self.status(close, Terminal, 1)
        self.restart()
        self.assertEqual(self.s.book.layers, ())

    def test_price_stop_selects_eligible_lower_layer_before_time_stop(self):
        self.c = replace(self.c, allow_stop_loss=True, stop_loss=600, max_hold_days=1)
        self.first(-8, 0)
        tp = self.tp('L1')
        self.status(tp, Working)
        add = self.quote(-10).intent
        self.fill(add, 1, -30)
        self.status(add, Terminal, 1)
        self.status(tp, Terminal, 0)
        self.day = '20261120'
        lower = self.quote(-19, ordinal=101).intent
        self.assertEqual((lower.reason['risk_rule'], lower.tier_id), ('layer_stop', 'L1'))
        self.fill(lower, 1, -20)
        self.status(lower, Terminal, 1)
        top = self.quote(-19, ordinal=101).intent
        self.assertEqual((top.reason['risk_rule'], top.reason['day_count_source'], top.tier_id),
                         ('time_stop', 'exchange_day_index', 'L2'))

    def test_whole_time_exit_drains_top_down_with_estimated_days(self):
        self.c = replace(self.c, max_hold_days=5)
        self.first(-8, 0)
        tp = self.tp('L1')
        self.status(tp, Working)
        add = self.quote(-10).intent
        self.fill(add, 1, -10)
        self.status(add, Terminal, 1)
        self.status(tp, Terminal, 0)
        self.day = '20261126'
        for tier in ('L2', 'L1'):
            exit_order = self.quote(7, ordinal=None).intent
            self.assertEqual((exit_order.tier_id, exit_order.reason['held_days'],
                              exit_order.reason['day_count_source']),
                             (tier, 5, 'estimated_calendar_days_times_5_over_7'))
            self.fill(exit_order, 1, 6)
            self.status(exit_order, Terminal, 1)
            self.restart()
        self.assertEqual(self.s.book.layers, ())

    def test_legacy_journal_replays_original_intents_and_recognizes_retry(self):
        order = self.first(-8, 0)
        old_j = Journal(self.j.path.with_name('old.db'))
        old_config = {k:v for k,v in asdict(self.c).items()
                      if k not in ('roll_exit_days', 'stop_loss', 'max_hold_days', 'allow_stop_loss')}
        fingerprint = sha256(json.dumps([old_config], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        for r in self.j.replay():
            p = r.payload
            if r.kind == 'shadow_input':
                p['config'] = fingerprint
                if p['kind'] == 'quote':
                    del p['data']['checks']['trading_day_index']
            else:
                p['reason']['config'] = old_config
                del p['reason']['checks']['trading_day_index']
            old_j.append(r.account, r.trading_day, r.kind, r.identity, p)
        self.j = old_j
        self.restart()
        self.assertEqual(self.s.intents[order.decision_id].reason['config'], old_config)
        self.seq = 2
        self.assertEqual(self.quote(-8, ordinal=None).reason, 'duplicate')
        self.assertEqual(self.quote(-6, delivery=1).intent.purpose, 'risk_exit')
        self.restart()
        self.assertEqual(len(self.s.intents), 2)

    def test_risk_rules_can_be_disabled(self):
        self.c = replace(self.c, stop_loss=1, allow_stop_loss=False, roll_exit_days=0, max_hold_days=0)
        self.first(-8, 0)
        self.day = '20261221'
        self.assertIsNone(self.quote(-50, delivery=0, ordinal=120).intent)
        self.assertEqual(len(self.s.book.layers), 1)
        self.assertFalse(any(i.purpose == 'risk_exit' for i in self.s.intents.values()))


if __name__ == '__main__':
    unittest.main()
