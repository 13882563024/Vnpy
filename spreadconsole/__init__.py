"""本项目自有价差交易应用，沿用 vn.py 的 SpreadEngine 而非修改安装包。"""
from __future__ import annotations

from vnpy_spreadtrading import SpreadTradingApp


class SpreadConsoleApp(SpreadTradingApp):
    """复用官方引擎及图标，仅替换最终用户看到的 Qt 管理窗口。"""

    app_module = __name__
    display_name = "价差交易"
    widget_name = "SpreadConsoleWidget"


__all__ = ["SpreadConsoleApp"]
