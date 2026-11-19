from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_core.journal import Journal, Outcome


class JournalTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'account.sqlite'
        self.j = Journal(self.path)

    def fill(self, *, leg='near', price=-10, account='broker/account', day='20261119'):
        return self.j.append(account, day, 'fill', ('DCE', 'trade123', leg), {'price':price})[1]

    def test_restart_preserves_conflict_and_canonical_fill(self):
        self.assertEqual(self.fill(), Outcome.NEW)
        self.j = Journal(self.path)
        self.assertEqual(self.fill(), Outcome.DUPLICATE)
        self.assertEqual(self.fill(price=1), Outcome.CONFLICT)
        rows = list(self.j.replay())
        self.assertEqual([r.outcome for r in rows], [Outcome.NEW, Outcome.DUPLICATE, Outcome.CONFLICT])
        self.assertEqual(rows[0].payload, {'price':-10})
        self.assertEqual(list(self.j.replay(after=2)), rows[2:])

    def test_same_trade_number_distinct_leg_account_or_day(self):
        self.assertEqual([self.fill(), self.fill(leg='far'), self.fill(account='other'),
                          self.fill(day='20261120')], [Outcome.NEW]*4)

    def test_concurrent_duplicate_produces_one_canonical_event(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.fill(), range(12)))
        self.assertEqual(results.count(Outcome.NEW), 1)
        self.assertEqual(results.count(Outcome.DUPLICATE), 11)

    def test_readonly_report_filters_without_creating_or_appending(self):
        import sqlite3
        from tools.replay_ctp_facts import summarize
        for price in (2, 2, 3):
            self.fill(price=price)
        self.fill(day='20261120')
        report = summarize(self.path, day='20261119')
        self.assertEqual(report['canonical_events'], {'fill': 1})
        self.assertEqual(report['conflict_sequences'], [3])
        self.assertEqual(report['outcomes'], {'new': 1, 'duplicate': 1, 'conflict': 1})
        with self.assertRaises(sqlite3.OperationalError):
            Journal.open_readonly(self.path).append('a', 'd', 'fill', ('id',), {})
        missing = self.path.with_name('missing.db')
        with self.assertRaises(FileNotFoundError):
            summarize(missing)
        self.assertFalse(missing.exists())

    def test_missing_identity_and_nonfinite_price_not_committed(self):
        for leg, price in [('', 12), ('near', float('nan'))]:
            with self.assertRaises(ValueError):
                self.fill(leg=leg, price=price)
        self.assertEqual(list(self.j.replay()), [])
