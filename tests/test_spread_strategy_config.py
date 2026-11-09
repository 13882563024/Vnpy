"""每个价差独立策略参数的单元测试。

覆盖三件事：
1. 优先级：策略类默认值 → spreads.json 的 ``strategy`` → 命令行 ``--setting``
2. 拼错的参数名会被识别出来（而不是静默失效）
3. 按 spreads.json 能建出参数各不相同的策略实例
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.engine_setup import (  # noqa: E402
    strategy_setting_for,
    unknown_strategy_keys,
)
from utils.spread_config import SpreadConfig, parse_spread_config  # noqa: E402


def make_config(name: str = "X", strategy: dict | None = None) -> SpreadConfig:
    return SpreadConfig(
        name=name,
        legs=["a2611.DCE", "a2701.DCE"],
        active_symbol="a2611.DCE",
        size=10,
        pricetick=1,
        strategy=dict(strategy or {}),
    )


class TestParseStrategySection(unittest.TestCase):
    def test_strategy_section_parsed(self) -> None:
        config = parse_spread_config("X", {
            "legs": ["a2611.DCE", "a2701.DCE"],
            "size": 10,
            "pricetick": 1,
            "strategy": {"max_positions": 3, "max_hold_days": 999},
        })
        self.assertEqual(config.strategy, {"max_positions": 3, "max_hold_days": 999})

    def test_missing_strategy_section_is_empty(self) -> None:
        config = parse_spread_config("X", {"legs": ["a2611.DCE", "a2701.DCE"], "size": 10})
        self.assertEqual(config.strategy, {})

    def test_non_dict_strategy_ignored(self) -> None:
        config = parse_spread_config("X", {"legs": ["a2611.DCE"], "strategy": "oops"})
        self.assertEqual(config.strategy, {})


class TestStrategySettingMerge(unittest.TestCase):
    def test_defaults_when_nothing_configured(self) -> None:
        setting = strategy_setting_for(make_config(), None)
        self.assertEqual(setting, {})

    def test_config_only(self) -> None:
        setting = strategy_setting_for(make_config(strategy={"max_positions": 3}), None)
        self.assertEqual(setting, {"max_positions": 3})

    def test_cli_overrides_config(self) -> None:
        config = make_config(strategy={"max_positions": 3, "fee_buffer": 1.1})
        setting = strategy_setting_for(config, {"max_positions": 5, "live_trading": True})
        self.assertEqual(setting["max_positions"], 5, "命令行优先级更高")
        self.assertEqual(setting["fee_buffer"], 1.1, "命令行没给的沿用配置")
        self.assertTrue(setting["live_trading"])

    def test_unknown_config_key_ignored(self) -> None:
        config = make_config(strategy={"max_position": 3, "layer_step": 0.5})   # 拼错一个
        setting = strategy_setting_for(config, None)
        self.assertNotIn("max_position", setting)
        self.assertEqual(setting["layer_step"], 0.5)


class TestUnknownStrategyKeys(unittest.TestCase):
    def test_detects_typo(self) -> None:
        configs = {
            "OK": make_config("OK", {"max_positions": 3}),
            "BAD": make_config("BAD", {"max_position": 3}),
        }
        problems = unknown_strategy_keys(configs)
        self.assertEqual(len(problems), 1)
        self.assertIn("BAD", problems[0])
        self.assertIn("max_position", problems[0])

    def test_clean_config_reports_nothing(self) -> None:
        configs = {"OK": make_config("OK", {"max_positions": 3, "max_hold_days": 999})}
        self.assertEqual(unknown_strategy_keys(configs), [])


class TestRealConfigFile(unittest.TestCase):
    """真实 config/spreads.json 必须自身干净（没有拼错的参数名）。"""

    def test_project_config_has_no_unknown_keys(self) -> None:
        from utils.spread_config import load_spread_configs

        configs = load_spread_configs()
        self.assertGreater(len(configs), 0)
        self.assertEqual(unknown_strategy_keys(configs), [])

    def test_per_spread_settings_differ(self) -> None:
        from utils.spread_config import load_spread_configs

        configs = load_spread_configs()
        settings = {name: strategy_setting_for(config) for name, config in configs.items()}
        with_override = {name: value for name, value in settings.items() if value}
        self.assertGreater(len(with_override), 0, "示例配置里应至少有一个价差带 strategy 段")
        # 至少存在两个价差的参数不完全相同（体现"独立配置"）；
        # 注册表只剩一个合约时，这项无从比较，跳过即可。
        distinct = {tuple(sorted(value.items())) for value in settings.values()}
        if len(settings) > 1:
            self.assertGreater(len(distinct), 1)


if __name__ == "__main__":
    unittest.main()
