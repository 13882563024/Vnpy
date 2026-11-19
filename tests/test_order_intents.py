import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from trading_core.journal import Journal, Outcome
from trading_core.intents import OrderIntent


class IntentTests(unittest.TestCase):
    def test_crash_retry_and_changed_order(self):
        intent = OrderIntent('account', '20261119', 'spread', 'decision-2', 'tier-2',
                             'add', 'SP A&B', 'sell', 1, 0.0, 'cfg-v1',
                             {'z':1.42,'k':1.3,'tick':'tick-100'})
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'journal.db'
            self.assertEqual(intent.reserve(Journal(path)), Outcome.NEW)
            self.assertEqual(intent.reserve(Journal(path)), Outcome.DUPLICATE)
            self.assertEqual(replace(intent,lots=3).reserve(Journal(path)), Outcome.CONFLICT)
            rows = list(Journal.open_readonly(path).replay())
            self.assertEqual(rows[0].payload['reason']['z'], 1.42)
            self.assertEqual(rows[0].payload['price'], 0)
            with self.assertRaises(ValueError):
                replace(intent,reason={}).reserve(Journal(path))
