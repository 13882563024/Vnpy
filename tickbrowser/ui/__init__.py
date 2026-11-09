"""tick 浏览面板的界面层。

``MainWindow.init_menu`` 是这么取 widget 类的::

    ui_module = import_module(app.app_module + ".ui")
    widget_class = getattr(ui_module, app.widget_name)

也就是从**包本身**取属性，所以这里必须把类导出到包命名空间 —— 放在 ``widget.py``
里而不导出，启动 GUI 会直接抛 ``AttributeError: module 'tickbrowser.ui' has no
attribute 'TickBrowserWidget'``。
"""

from tickbrowser.ui.widget import TickBrowserWidget

__all__ = ["TickBrowserWidget"]
