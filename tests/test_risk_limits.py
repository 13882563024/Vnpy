"""全局已用资金上限：配置读写与账户占比计算。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.risk_limits import (
    AccountUsage,
    RiskLimits,
    account_usage,
    load_risk_limits,
    save_risk_limits,
)


class RiskLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "risk.json"

    def test_defaults_when_missing_or_broken(self):
        self.assertEqual(load_risk_limits(self.path), RiskLimits())
        self.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(load_risk_limits(self.path), RiskLimits())

    def test_save_and_load_roundtrip(self):
        save_risk_limits(RiskLimits(enabled=False, max_margin_ratio=0.65), self.path)
        loaded = load_risk_limits(self.path)
        self.assertFalse(loaded.enabled)
        self.assertAlmostEqual(loaded.max_margin_ratio, 0.65)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["max_margin_ratio"], 0.65)

    def test_rejects_invalid_ratio(self):
        for value in (0, -0.1, 1.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                save_risk_limits(RiskLimits(max_margin_ratio=value), self.path)

    def test_account_usage_is_conservative(self):
        engine = SimpleNamespace(get_all_accounts=lambda: [
            SimpleNamespace(gateway_name="CTP", balance=100_000.0, available=20_000.0),
            SimpleNamespace(gateway_name="OTHER", balance=999_999.0, available=0.0),
        ])
        usage = account_usage(engine)
        self.assertIsInstance(usage, AccountUsage)
        self.assertAlmostEqual(usage.ratio, 0.8)
        self.assertEqual(usage.balance, 100_000.0)

    def test_no_account_data_returns_none(self):
        self.assertIsNone(account_usage(SimpleNamespace(get_all_accounts=lambda: [])))
        self.assertIsNone(account_usage(SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
