"""接线验证：策略类能被价差引擎发现、algo_class 能替换、价差能量化注册。

这些是 P5 的关键前提，而它们都容易**静默失败**：

- ``SpreadStrategyEngine`` 只在构造时按 ``Path.cwd()/strategies`` 发现策略类，而 vnpy 的
  ``MainEngine`` 会把 cwd 切到 ``~/.vntrader`` → 必须显式再加载一次；
- ``SpreadAlgoEngine.algo_class`` 是类属性，替换不成功时不会报错，只会一直走
  ``SpreadTakerAlgo`` → 必须断言。

本测试不需要柜台：合约缺失时 ``add_spread`` 只是不订阅行情，其余流程照常。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.engine import BaseEngine, MainEngine  # noqa: E402
from vnpy_spreadtrading import SpreadTradingApp  # noqa: E402
from vnpy_spreadtrading.algo import SpreadTakerAlgo  # noqa: E402
from vnpy_spreadtrading.engine import SpreadDataEngine, SpreadStrategyEngine  # noqa: E402

from execution.cross_period_algo import CrossPeriodAlgo  # noqa: E402
from utils.spread_config import load_spread_configs  # noqa: E402

STRATEGY_CLASS = "CrossPeriodMRStrategy"


class TestSpreadEngineWiring(unittest.TestCase):
    def setUp(self) -> None:
        # 不写 ~/.vntrader/spread_trading_*.json：测试不应污染用户实盘配置
        self._patchers = [
            patch.object(SpreadDataEngine, "save_setting", lambda self: None),
            patch.object(SpreadStrategyEngine, "update_strategy_setting",
                         lambda self, name, setting: None),
            patch.object(SpreadStrategyEngine, "remove_strategy_setting",
                         lambda self, name: None),
        ]
        for patcher in self._patchers:
            patcher.start()

        self.event_engine = EventEngine()
        self.main_engine = MainEngine(self.event_engine)
        self.spread_engine = self.main_engine.add_app(SpreadTradingApp)

    def tearDown(self) -> None:
        self.main_engine.close()
        for patcher in reversed(self._patchers):
            patcher.stop()

    def test_strategy_class_discovered_after_explicit_reload(self) -> None:
        """MainEngine 构造后 cwd 已变，必须显式再加载一次策略目录。"""
        self.assertNotIn(STRATEGY_CLASS, self.spread_engine.get_all_strategy_class_names())

        self.spread_engine.strategy_engine.load_strategy_class_from_folder(
            PROJECT_ROOT / "strategies", "strategies"
        )
        self.assertIn(STRATEGY_CLASS, self.spread_engine.get_all_strategy_class_names())

    def test_algo_class_replacement(self) -> None:
        self.assertIs(self.spread_engine.algo_engine.algo_class, SpreadTakerAlgo)

        self.spread_engine.algo_engine.algo_class = CrossPeriodAlgo
        self.assertIs(self.spread_engine.algo_engine.algo_class, CrossPeriodAlgo)

    def test_register_spreads_from_config(self) -> None:
        configs = load_spread_configs()
        self.assertGreater(len(configs), 0)

        for name, config in configs.items():
            self.spread_engine.add_spread(
                name=name,
                leg_settings=config.leg_settings(),
                price_formula=config.price_formula,
                active_symbol=config.active_symbol,
                min_volume=config.min_volume,
            )

        names = self.spread_engine.get_all_spread_names()
        for name in configs:
            self.assertIn(name, names)
            spread = self.spread_engine.get_spread(name)
            self.assertEqual(list(spread.variable_symbols.values()), configs[name].legs)
            # 无柜台时合约未加载，pricetick/size 由策略在运行期从单腿合约补齐
            self.assertGreaterEqual(spread.pricetick, 0)

    def test_strategy_can_be_added_and_inited(self) -> None:
        configs = load_spread_configs()
        name, config = next(iter(configs.items()))

        self.spread_engine.strategy_engine.load_strategy_class_from_folder(
            PROJECT_ROOT / "strategies", "strategies"
        )
        self.spread_engine.add_spread(
            name=name,
            leg_settings=config.leg_settings(),
            price_formula=config.price_formula,
            active_symbol=config.active_symbol,
            min_volume=config.min_volume,
        )

        self.spread_engine.add_strategy(STRATEGY_CLASS, "wiring_test", name, {"live_trading": False})
        strategy = self.spread_engine.strategy_engine.strategies["wiring_test"]
        self.assertFalse(strategy.live_trading)

        self.spread_engine.strategy_engine.init_strategy("wiring_test").result(timeout=10)
        self.assertTrue(strategy.inited)
        # 只计算模式：初始化后仍不应有任何在途算法
        self.assertEqual(strategy.algoids, set())

    # ---------------- GUI 启动路径（main.py 调用的同一个函数） ----------------
    def test_setup_spread_trading_three_steps(self) -> None:
        """GUI 启动与命令行共用的接线：策略类 + 算法类 + 引擎启动 + 价差注册。"""
        from utils.engine_setup import setup_spread_trading

        result = setup_spread_trading(self.spread_engine, log=lambda *_: None)

        self.assertIn(STRATEGY_CLASS, result["strategy_classes"])
        self.assertEqual(result["algo_class"], "CrossPeriodAlgo")
        self.assertIs(self.spread_engine.algo_engine.algo_class, CrossPeriodAlgo)

        expected = len(load_spread_configs())
        # 已存在 + 新建 必须覆盖 spreads.json 里的全部价差（允许上一次运行已保存过）
        self.assertEqual(len(result["created"]) + len(result["existing"]), expected)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(len(self.spread_engine.get_all_spread_names()), expected)
        # 引擎必须真的 start 过，否则价差永远不会合成
        self.assertTrue(self.spread_engine.active)

    def test_setup_never_leaves_taker_algo(self) -> None:
        """接线后绝不能残留默认的吃单算法（真实账号上会立即成交）。"""
        from utils.engine_setup import setup_spread_trading

        setup_spread_trading(self.spread_engine, log=lambda *_: None)
        self.assertIsNot(self.spread_engine.algo_engine.algo_class, SpreadTakerAlgo)

    def test_install_algo_class_is_idempotent(self) -> None:
        from utils.engine_setup import install_algo_class

        install_algo_class(self.spread_engine)
        install_algo_class(self.spread_engine)
        self.assertIs(self.spread_engine.algo_engine.algo_class, CrossPeriodAlgo)

    # ---------------- GUI「功能 → 行情数据」面板 ----------------
    def test_tick_browser_widget_loads_data(self) -> None:
        """面板必须能列出库里已采集的合约并显示明细（Qt 不可用时跳过）。"""
        try:
            from vnpy.trader.ui import create_qapp
        except Exception as exc:  # noqa: BLE001 - 无 Qt 运行时的环境
            self.skipTest(f"Qt 不可用：{exc}")

        try:
            app = create_qapp()
        except Exception as exc:  # noqa: BLE001 - 无显示环境（headless）
            self.skipTest(f"无法创建 QApplication：{exc}")
        self.assertIsNotNone(app)

        widget_class = self.resolve_app_widget("tickbrowser", "TickBrowserWidget")

        from tickbrowser import TickBrowserApp

        self.main_engine.add_app(TickBrowserApp)

        widget = widget_class(self.main_engine, self.event_engine)
        self.addCleanup(widget.deleteLater)

        from recorder.tick_store import TickStore

        expected = len(TickStore().list_symbols())
        if expected == 0:
            self.assertEqual(widget.symbol_list.count(), 1)   # 只有占位项
            return

        self.assertEqual(widget.symbol_list.count(), expected)
        self.assertGreater(widget.table.rowCount(), 0)
        self.assertEqual(widget.table.columnCount(), 7)

    @staticmethod
    def resolve_app_widget(app_module: str, widget_name: str):
        """按 ``MainWindow.init_menu`` 的方式取 widget 类。

        它取的是 ``<app_module>.ui`` **包本身**的属性，而不是 ``<app_module>.ui.widget``
        子模块 —— 少写一行 re-export 就会在启动 GUI 时抛 AttributeError。
        """
        from importlib import import_module

        ui_module = import_module(app_module + ".ui")
        return getattr(ui_module, widget_name)

    def test_tick_browser_widget_resolves_like_mainwindow(self) -> None:
        """不用 Qt 也能验证的注册契约：包名 → ui 包 → widget_name 属性。"""
        widget_class = self.resolve_app_widget("tickbrowser", "TickBrowserWidget")
        self.assertEqual(widget_class.__name__, "TickBrowserWidget")

    def test_tick_browser_app_declaration(self) -> None:
        from pathlib import Path

        from tickbrowser import TickBrowserApp

        self.assertEqual(TickBrowserApp.app_module, "tickbrowser")
        self.assertEqual(TickBrowserApp.widget_name, "TickBrowserWidget")
        self.assertTrue(Path(TickBrowserApp.app_path).is_dir())
        self.assertTrue(issubclass(TickBrowserApp.engine_class, BaseEngine))


if __name__ == "__main__":
    unittest.main()
