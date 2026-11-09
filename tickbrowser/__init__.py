"""行情数据浏览应用：把已采集的 tick / 合成价差放进 GUI 菜单。

挂载方式与 vnpy 官方应用一致：``MainWindow.init_menu`` 会遍历 ``main_engine.apps``，
按 ``import_module(app_module + ".ui")`` 拿到 ``widget_name`` 对应的类，因此本包必须有
``ui`` 子模块且 widget 类名与 ``widget_name`` 一致。
"""

from __future__ import annotations

from pathlib import Path

from vnpy.trader.app import BaseApp

from tickbrowser.engine import APP_NAME, TickBrowserEngine


class TickBrowserApp(BaseApp):
    """「行情数据」应用（只读查询，不订阅行情、不发单）。"""

    app_name: str = APP_NAME
    app_module: str = __module__
    app_path: Path = Path(__file__).parent
    display_name: str = "行情数据"
    engine_class: type[TickBrowserEngine] = TickBrowserEngine
    widget_name: str = "TickBrowserWidget"
    icon_name: str = ""


__all__ = ["TickBrowserApp", "TickBrowserEngine", "APP_NAME"]
