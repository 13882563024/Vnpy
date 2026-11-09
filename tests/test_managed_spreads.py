"""GUI 组合输入、持久化与动态接线的边界单测（无真实 vn.py 网关）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.managed_spreads import (  # noqa: E402
    ManagedSpreadError, ManagedSpreadService, add_managed_spread,
    parse_combination, verify_contracts,
)

TODAY = date(2026, 11, 2)


class ParsingTests(unittest.TestCase):
    def test_parse_preserves_code_and_derives_spread(self):
        spread = parse_combination("  DCE.SP a2701&a2703  ", today=TODAY)
        self.assertEqual(spread.id, "A_2701_2703")
        self.assertEqual(spread.display_name, "DCE.SP a2701&a2703")
        self.assertEqual(spread.exchange_spread, "SP a2701&a2703")
        self.assertEqual(spread.exchange_spread_vt_symbol, "SP a2701&a2703.DCE")
        self.assertEqual(spread.legs, ("a2701.DCE", "a2703.DCE"))

    def test_rejects_invalid_and_expired_input(self):
        for value in ("", "CZCE.SPD OI701&OI703", "DCE.SP a2701&a2703&j2705",
                      "DCE.SP a2701&j2703", "DCE.SP a2713&a2801",
                      "DCE.SP a2703&a2701", "DCE.SP a2701&a2701",
                      "DCE.SP  a2701&a2703", "DCE.SP a2610&a2701", "x" * 100):
            with self.subTest(value=value), self.assertRaises(ManagedSpreadError):
                parse_combination(value, today=TODAY)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config" / "spreads.json"
        self.service = ManagedSpreadService(self.path)

    def test_add_duplicate_and_preserve_legacy_fields(self):
        legacy = {"A_2611_2701": {"legs": ["a2611.DCE", "a2701.DCE"],
                                    "exchange_spread": "SP a2611&a2701", "exchange": "DCE",
                                    "custom": "retained", "size": 10}}
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        config, created = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        self.assertTrue(created)
        self.assertEqual(config.name, "A_2701_2703")
        self.assertEqual(config.legs, ["a2701.DCE", "a2703.DCE"])
        self.assertEqual(config.price_formula, "near - far")
        self.assertEqual([leg["trading_multiplier"] for leg in config.leg_settings()], [1, -1])
        self.assertEqual(config.size, 0)
        self.assertEqual(config.remark, "")
        self.assertEqual(config.margin_per_lot, 0.0)
        self.assertFalse(config.strategy["live_trading"])
        self.assertIn("size", " ".join(config.validate()))
        first = self.path.read_bytes()
        same, created = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        self.assertFalse(created)
        self.assertEqual(config.name, same.name)
        self.assertEqual(self.path.read_bytes(), first)
        self.assertEqual(json.loads(first)["A_2611_2701"]["custom"], "retained")
        self.assertEqual(set(self.service.list()), {"A_2611_2701", "A_2701_2703"})

    def test_duplicate_legacy_id_found_and_conflicting_id_rejected(self):
        existing = {"OLD": {"exchange_spread": "SP a2701&a2703", "exchange": "DCE",
                            "legs": ["a2701.DCE", "a2703.DCE"], "custom": 1}}
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(existing), encoding="utf-8")
        found, created = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        self.assertEqual(found.name, "OLD")
        self.assertFalse(created)
        existing["A_2701_2703"] = {"exchange_spread": "SP j2701&j2703",
                                    "exchange": "DCE", "legs": ["j2701.DCE", "j2703.DCE"]}
        self.path.write_text(json.dumps(existing), encoding="utf-8")
        with self.assertRaisesRegex(ManagedSpreadError, "冲突"):
            self.service.add("DCE.SP a2701&a2703", today=TODAY)

    def test_malformed_file_and_failed_replace_preserve_original(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ManagedSpreadError, "拒绝覆盖"):
            self.service.add("DCE.SP a2701&a2703", today=TODAY)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")
        self.path.write_text("{}", encoding="utf-8")
        with patch("utils.managed_spreads.os.replace", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                self.service.add("DCE.SP a2701&a2703", today=TODAY)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{}")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_remove_is_atomic_and_rejects_missing_or_broken_file(self):
        config, _ = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        removed = self.service.remove(config.name)
        self.assertEqual(removed.name, config.name)
        self.assertIsNone(self.service.get(config.name))
        self.assertEqual(self.service.list(), {})
        with self.assertRaisesRegex(ManagedSpreadError, "不在注册表"):
            self.service.remove(config.name)

        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ManagedSpreadError, "拒绝覆盖"):
            self.service.remove("A_2701_2703")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_edit_rejects_live_switch_and_keeps_original(self):
        self.service.add("DCE.SP a2701&a2703", today=TODAY)
        before = self.path.read_bytes()
        with self.assertRaises(ManagedSpreadError):
            self.service.edit_settings("A_2701_2703", strategy={"live_trading": True})
        self.assertEqual(self.path.read_bytes(), before)
        config = self.service.edit_settings("A_2701_2703", stop_loss=1000,
                                            strategy={"max_positions": 2})
        self.assertEqual(config.stop_loss, 1000)
        self.assertEqual(config.strategy["max_positions"], 2)
        self.assertFalse(config.strategy["live_trading"])

    def test_edit_remark_and_margin_per_lot(self):
        config, _ = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        updated = self.service.edit_settings(
            config.name, remark="  a 1-3 正套，重点观察  ", margin_per_lot=5000)
        self.assertEqual(updated.remark, "a 1-3 正套，重点观察")
        self.assertEqual(updated.margin_per_lot, 5000)
        raw = json.loads(self.path.read_text(encoding="utf-8"))[config.name]
        self.assertEqual(raw["remark"], "a 1-3 正套，重点观察")
        self.assertEqual(raw["margin_per_lot"], 5000)

        with self.assertRaisesRegex(ManagedSpreadError, "每手保证金"):
            self.service.edit_settings(config.name, margin_per_lot=-1)
        with self.assertRaisesRegex(ManagedSpreadError, "不超过 120"):
            self.service.edit_settings(config.name, remark="x" * 121)

    def test_edit_fee_persists_and_validates(self):
        config, _ = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        updated = self.service.edit_settings(
            config.name,
            fee={"unit": "元", "open": 3.0, "close_yesterday": 3.0, "close_today": 0.0},
        )
        self.assertEqual(updated.fee["unit"], "元")
        self.assertEqual(updated.fee["open"], 3.0)
        self.assertEqual(updated.fee["close_today"], 0.0)
        raw = json.loads(self.path.read_text(encoding="utf-8"))[config.name]
        self.assertEqual(raw["fee"]["close_today"], 0.0)

        with self.assertRaisesRegex(ManagedSpreadError, "计费单位|unit"):
            self.service.edit_settings(config.name, fee={"unit": "元/手"})
        with self.assertRaisesRegex(ManagedSpreadError, "fee.open"):
            self.service.edit_settings(config.name, fee={"unit": "元", "open": -1})
        with self.assertRaisesRegex(ManagedSpreadError, "未知手续费字段"):
            self.service.edit_settings(config.name, fee={"unit": "元", "typo": 1})

        cleared = self.service.edit_settings(config.name, fee={})
        self.assertEqual(cleared.fee, {})

    def test_trading_enabled_persists_and_verification_preserves_it(self):
        config, _ = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        self.service.set_trading_enabled(config.name, True)
        self.assertTrue(self.service.get(config.name).trading_enabled)

        contracts = {symbol: SimpleNamespace(size=10, pricetick=1, exchange="DCE")
                     for symbol in (*config.legs, config.exchange_spread_vt_symbol)}
        checked, listed = self.service.verify_and_save(config.name, contracts.get)
        self.assertTrue(listed)
        self.assertTrue(checked.trading_enabled)
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))[config.name]
                        ["trading_enabled"])

        contracts.pop(config.exchange_spread_vt_symbol)
        unavailable, listed = self.service.verify_and_save(config.name, contracts.get)
        self.assertFalse(listed)
        self.assertFalse(unavailable.trading_enabled)

    def test_contract_verification_and_no_implicit_executor_fallback(self):
        config, _ = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        contracts = {symbol: SimpleNamespace(size=10, pricetick=1, exchange="DCE")
                     for symbol in config.legs}
        self.assertEqual(verify_contracts(config, contracts.get), (10., 1., False))
        updated, listed = self.service.verify_and_save(config.name, contracts.get)
        self.assertFalse(listed)
        self.assertEqual(updated.executor, "exchange_spread")
        self.assertEqual(updated.size, 10)
        raw = json.loads(self.path.read_text(encoding="utf-8"))[config.name]
        self.assertEqual(raw["contract_status"], "combination_unavailable")
        self.assertFalse(raw["trading_enabled"])
        contracts[config.exchange_spread_vt_symbol] = SimpleNamespace(exchange="DCE")
        updated, listed = self.service.verify_and_save(config.name, contracts.get)
        self.assertTrue(listed)
        self.assertEqual(updated.pricetick, 1)
        self.assertFalse(json.loads(self.path.read_text(encoding="utf-8"))[config.name]["trading_enabled"])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = ManagedSpreadService(Path(self.tmp.name) / "spreads.json")
        self.config, _ = self.service.add("DCE.SP a2701&a2703", today=TODAY)
        self.spreads = {}
        self.strategies = {}
        self.added_strategies = []
        self.engine = SimpleNamespace(
            get_spread=lambda name: self.spreads.get(name),
            add_spread=self._add_spread,
            strategy_engine=SimpleNamespace(strategies=self.strategies,
                                            add_strategy=self._add_strategy),
        )

    def _add_spread(self, **kwargs):
        self.spreads[kwargs["name"]] = SimpleNamespace(
            variable_symbols={item["variable"]: item["vt_symbol"]
                              for item in kwargs["leg_settings"]})

    def _add_strategy(self, *args):
        self.added_strategies.append(args)
        self.strategies[args[1]] = object()

    def test_register_and_strategy_are_idempotent_and_no_auto_start(self):
        self.assertTrue(self.service.register(self.engine, self.config))
        self.assertFalse(self.service.register(self.engine, self.config))
        name = self.service.ensure_strategy(self.engine, self.config)
        self.assertEqual(name, self.service.ensure_strategy(self.engine, self.config))
        self.assertEqual(len(self.added_strategies), 1)
        self.assertFalse(self.added_strategies[0][3]["live_trading"])
        self.assertEqual(self.added_strategies[0][2], self.config.name)

    def test_shared_subscription_and_reconnect(self):
        subscribed = []
        self.assertEqual(self.service.subscribe_legs(self.config, subscribed.append), self.config.legs)
        self.assertEqual(self.service.subscribe_legs(self.config, subscribed.append), [])
        self.service.reset_subscriptions()
        self.assertEqual(self.service.subscribe_legs(self.config, subscribed.append), self.config.legs)
        self.assertEqual(len(subscribed), 4)


if __name__ == "__main__":
    unittest.main()
