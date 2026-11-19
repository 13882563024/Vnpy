"""Terminal menu adapter. The offline window receives a snapshot, never an engine."""
from PySide6.QtCore import Qt


def install_workbench_menu(host):
    menu = host.menuBar().addMenu('离线核对')
    action = menu.addAction('价差核对工作台')
    window = None

    def show_workbench():
        nonlocal window
        if window is None:
            from .model import load_snapshot
            from .window import Window
            window = Window(load_snapshot())
            window.setParent(host, Qt.WindowType.Window)
        window.showNormal()
        window.raise_()
        window.activateWindow()

    action.triggered.connect(show_workbench)
    return action
