import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from trading_core.journal import Journal, Outcome, Record
from trading_core.layers import Terminal, Working
from trading_core.paired_fills import Matched, matched_fills
from trading_core.shadow_strategy import EntryChecks, FeeEstimate, LegQuote, ShadowConfig, ShadowStrategy


class ShadowTests(unittest.TestCase):
    day = '20261119'
    checks = EntryChecks(True, True, True, True, True, 20)

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.journal = Journal(Path(tmp.name)/'shadow.sqlite')
        self.config = ShadowConfig('v1', 'N', 'F', 'SP N&F', 1, 10, max_jump_ticks=80)
        self.restart()

    def restart(self):
        self.s = ShadowStrategy(self.journal, self.config, account='A', strategy='S')

    def quote(self, t, mid, **kw):
        # Fresh identities even for multiple distinct events in one millisecond.
        suffix = kw.pop('suffix', str(t))
        now = kw.pop('now', t)
        n = LegQuote('N', 'N'+suffix, t, 100+mid, 101+mid, 5, 5, 5)
        f = LegQuote('F', 'F'+suffix, t, 100, 101, 5, 5, 5)
        return self.s.on_quote(self.day, replace(n, **kw.pop('leg', {})), f, now=now,
                               checks=kw.pop('checks', self.checks))

    def warm(self):
        for i in range(30):
            self.assertIsNone(self.quote(i*11, (-1)**i, checks=replace(self.checks, can_open=False)).intent)

    def actual(self, order, spread):
        # Trusted fixture mimics two exchange leg facts attributed to this order.
        def leg(seq, name, price):
            buy = (order.side == 'buy') != (name == 'F')
            return Record(seq, 'A', self.day, 'fill', ('DCE', name+order.decision_id),
                          dict(order_sys_id=order.decision_id, symbol=name, direction='0' if buy else '1',
                               volume=1, price=price), Outcome.NEW)
        rows = [leg(1, 'N', 100+spread), leg(2, 'F', 100)]
        actual = matched_fills(rows, exchange='DCE', sys_id=order.decision_id, near='N', far='F',
                               combo=self.config.symbol, buy=order.side=='buy', max_lots=order.lots)
        return self.s.on_matched(self.day, 'pair'+order.decision_id, order.decision_id, actual)

    def terminal(self, order, filled):
        self.s.on_status(self.day, 'term'+order.decision_id, Terminal(order.decision_id, filled))

    def protect(self, cycle, tier):
        fees = FeeEstimate(tier, self.day, self.day, 2, 2, 2, 2, 'verified fixed schedule')
        return self.s.protect(self.day, cycle, fees)

    def test_three_layers_and_restart(self):
        self.warm()
        tp = None
        costs = [-12, -19, -38]
        for i, (mid, actual) in enumerate(zip((-8, -20, -40), costs)):
            tier = 'L'+str(i+1)
            o = self.quote(340+i, mid).intent
            self.assertEqual((o.purpose, o.tier_id, o.price, o.config_version),
                             ('add' if i else 'open', tier, mid+1, 'v1'))
            self.assertEqual(o.reason['k'], .3+i)
            self.assertAlmostEqual(o.reason['z'], (mid-o.reason['mean'])/o.reason['sd'])
            self.assertEqual(len(self.s.book.layers), i)  # intent is not a fill
            cancels = self.actual(o, actual).cancels
            self.terminal(o, 1)
            if tp:
                self.assertEqual([a.order_id for a in cancels], [tp.decision_id])
                self.assertIsNone(self.protect('pending cancel'+tier, tier).intent)
                self.terminal(tp, 0)
            tp = self.protect('cover'+tier, tier).intent
            self.assertEqual((tp.tier_id, tp.price, tp.side), (tier, actual+2, 'sell'))
            self.s.on_status(self.day, 'working'+tier, Working(tp.decision_id))
        self.assertIsNone(self.quote(344, -70).intent)
        before = dict(self.s.intents)
        rows = list(self.journal.replay())
        for _ in range(2):
            self.restart()
            self.assertEqual(before, self.s.intents)
            self.assertEqual([self.s.costs[l.tier_id] for l in self.s.book.layers], costs)
            self.assertEqual(rows, list(self.journal.replay()))
        self.assertEqual(self.quote(342, -40).reason, 'duplicate')
        for i in (2, 1, 0):
            self.assertEqual((tp.tier_id,tp.price), ('L'+str(i+1), costs[i]+2))
            self.actual(tp, tp.price)
            self.terminal(tp, 1)
            self.assertEqual(len(self.s.book.layers), i)
            if i:
                tp = self.protect('restore'+str(i), 'L'+str(i)).intent
        self.restart()
        self.assertEqual(self.s.book.layers, ())
        self.assertIsNone(self.s.book.direction)
        self.assertTrue({r.kind for r in self.journal.replay()} <= {'order_intent', 'shadow_input'})

    def test_short_zero_cost_unpaired_blocks_and_crash_repair(self):
        self.warm()
        with patch.object(self.s, '_reserve', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.quote(340, 8)
        self.restart()  # repairs missing materialization; returns no intent
        o, = self.s.intents.values()
        self.assertEqual((o.side, o.price), ('sell', 7))
        self.assertEqual(self.quote(340, 8).reason, 'duplicate')
        self.s.on_matched(self.day, 'near only', o.decision_id, Matched(0,None,None,None,1,0))
        self.assertEqual(self.quote(341, 20).reason, 'unpaired leg exposure')
        self.assertEqual(self.s.book.layers, ())
        self.actual(o, 0)
        self.terminal(o, 1)
        self.restart()
        self.assertEqual(self.s.costs['L1'], Decimal(0))
        self.assertIsNone(self.protect('wrong layer', 'L2').intent)
        tp = self.protect('cover', 'L1').intent
        self.assertEqual((tp.side, tp.price, tp.reason['estimated_fee']), ('buy', -2, '8'))

    def test_distinct_windows_and_strict_threshold(self):
        # Sample sd([-1, 0, 1]) = 1, while the short window contains [0, 1].
        self.config = replace(self.config, mean_seconds=1, sd_seconds=10, min_span=2,
                              min_samples=3, entry_k=0.5)
        self.restart()
        self.quote(0, -1)
        self.quote(1, 0)
        self.assertEqual(self.quote(2, 1).reason, 'below threshold')  # z == k
        self.assertEqual((self.s.window.mean(), self.s.window.sd()), (0.5, 1))
        self.assertEqual(self.quote(3, 10).intent.side, 'sell')

    def test_partial_cumulative_cost_and_unconfirmed_jump(self):
        self.config = replace(self.config, layer_lots=2, max_jump_ticks=5, min_exit_pnl_override=10)
        self.restart()
        self.warm()
        self.assertEqual(self.quote(340, -9).reason, 'unconfirmed leg jump')
        self.restart()
        self.assertEqual(self.quote(341, -10).reason, 'unconfirmed leg jump')
        o = self.quote(342, -10).intent
        first = Matched(1,Decimal('-10'),None,None,0,0)
        cancel = self.s.on_matched(self.day, 'part', o.decision_id, first).cancels
        self.assertEqual([(a.order_id,a.lots) for a in cancel], [(o.decision_id,1)])
        self.assertIsNone(self.protect('not final', 'L1').intent)
        self.restart()
        second = Matched(2,Decimal('-12.5'),None,None,0,0)
        self.s.on_matched(self.day, 'late second', o.decision_id, second)
        self.terminal(o, 2)
        tp = self.protect('final', 'L1').intent
        self.assertEqual((tp.price, tp.lots, tp.reason['actual_open'], tp.reason['min_net']),
                         (-11, 2, '-12.5', '10'))
        self.s.on_matched(self.day, 'partial close', tp.decision_id, first)
        self.terminal(tp, 1)
        self.restart()
        retry = self.protect('remaining', 'L1').intent
        self.assertEqual((retry.price, retry.lots, retry.reason['actual_open']), (-10, 1, '-12.5'))

    def test_quality_filters_and_snapshot_identity(self):
        self.assertEqual(self.quote(1, 0, now=6).reason, 'invalid or stale leg')
        self.assertEqual(self.quote(2, 0, leg={'timestamp':None}).reason, 'invalid or stale leg')
        self.assertEqual(self.quote(3, 0, leg={'timestamp':2.4}).reason, 'unsynchronized')
        self.assertEqual(self.quote(4, 0, leg={'ask':103}).reason, 'width')
        self.assertEqual(self.quote(5, 0, leg={'limit_down':102}).reason, 'price limits')
        self.assertEqual(self.s.window.count, 0)
        self.assertEqual(self.quote(6, 0).reason, 'warming up')
        self.assertEqual(self.quote(6, 1, suffix='6b').reason, 'warming up')
        self.assertEqual(self.quote(6, 0).reason, 'duplicate')
        self.assertEqual(self.s.window.count, 2)
        self.assertEqual(self.quote(7, 0, leg={'ask_size':0}).reason, 'depth')
        self.assertEqual(self.quote(8, 0, checks=replace(self.checks, days_to_delivery=None)).reason, 'entry checks')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.quote(6, 3)
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.restart()


if __name__ == '__main__':
    unittest.main()
