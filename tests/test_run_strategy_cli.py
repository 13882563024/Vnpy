"""CLI 安全边界：全部离线，不创建真实引擎、不读连接配置、不连接柜台。"""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import run_strategy as cli


class StrategyCliTests(TestCase):
    def setUp(self):
        self.event = self.enterContext(patch.object(cli, "EventEngine"))
        self.engine = self.enterContext(patch.object(cli, "MainEngine"))
        self.credentials = self.enterContext(patch.object(cli, "load_json"))

    def assert_not_started(self):
        self.event.assert_not_called()
        self.engine.assert_not_called()
        self.credentials.assert_not_called()

    def test_live_refused_before_engine_or_config(self):
        for flags in ([], ["--md-only"], ["--close-only"], ["--no-connect"], ["--list"],
                      ["--setting", "live_trading=false"]):
            with self.subTest(flags=flags), redirect_stderr(StringIO()) as err, patch.object(cli, "load_spread_table") as load:
                self.assertNotEqual(cli.main(["--live", *flags]), 0)
                self.assertIn("逐笔核验", err.getvalue())
                load.assert_not_called()
                self.assert_not_started()

    def test_no_live_alias_or_parameter_override(self):
        for items in (["trading=1"], [" live_trading = on "], ["live_trading=on", "live_trading=off"],
                      ["managed_by_console=true"]):
            flags = [flag for item in items for flag in ("--setting", item)]
            with self.subTest(items=items), self.assertRaises(SystemExit) as err:
                cli.main(flags)
            self.assertIn(items[0].split("=")[0].strip(), str(err.exception))
            self.assert_not_started()

    def test_self_check_needs_no_engine_or_credentials(self):
        for flags in (["--list"], ["--no-connect"]):
            with self.subTest(flags=flags), patch.object(cli, "load_spread_table", return_value={}), patch.object(cli, "self_check", return_value=0) as check:
                self.assertEqual(cli.main(flags), 0)
                check.assert_called_once_with({}, [])
                self.assert_not_started()

    def test_observation_starts_only_market_data_gateway(self):
        for flags in ([], ["--md-only"]):
            with (
                self.subTest(flags=flags),
                redirect_stdout(StringIO()),
                patch.object(cli, "load_spread_table", return_value={}),
                patch.object(cli, "self_check", return_value=0),
                patch.object(cli, "warn_off_session"),
                patch.object(cli, "setup_spread_trading", return_value={"strategy_classes": [cli.STRATEGY_CLASS]}),
                patch.object(cli, "wait_md_login", return_value=True),
                patch.object(cli, "add_strategies", return_value=["observer"]) as add,
                patch.object(cli.time, "time", side_effect=[0, 0, 1, 1]),
                patch.object(cli.time, "sleep"),
            ):
                engine = self.engine.return_value
                engine.reset_mock()
                self.credentials.return_value = {"行情服务器": "unused"}
                gateway = engine.add_gateway.return_value
                gateway.gateway_name = "mock_md"
                self.assertEqual(cli.main([*flags, "--no-record", "--minutes", "0.001", "--setting", "max_positions=5"]), 0)
                engine.add_gateway.assert_called_once_with(cli.CtpMdOnlyGateway)
                engine.connect.assert_called_once_with(self.credentials.return_value, "mock_md")
                self.assertEqual(add.call_args.args[-1]["max_positions"], 5)
                engine.add_app.return_value.start_strategy.assert_called_once_with("observer")
                engine.close.assert_called_once()

    def test_merged_settings_force_read_only_for_new_and_saved_instances(self):
        configs = {"s": SimpleNamespace(strategy={"live_trading": True, "max_positions": 9})}
        overrides = {"trading": "true", "live_trading": True, "max_positions": 4}
        name = f"{cli.STRATEGY_CLASS}_s"
        for saved in (True, False):
            with self.subTest(saved=saved):
                se = Mock()
                se.strategies = {name: object()} if saved else {}
                self.assertEqual(cli.add_strategies(SimpleNamespace(strategy_engine=se), configs, [], overrides), [name])
                call = se.edit_strategy if saved else se.add_strategy
                self.assertEqual(call.call_args.args[-1], {"live_trading": False, "max_positions": 4})
