import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDockWidget, QLabel, QToolBar
from vnpy.trader.ui import MainWindow

from workbench.home_terminal import HomeTerminal


def seeded_ui(host):
    dock = QDockWidget('测试委托', host)
    dock.setObjectName('orders')
    dock.setWidget(QLabel('订单'))
    host.addDockWidget(Qt.LeftDockWidgetArea, dock)
    hidden = QDockWidget('隐藏面板', host)
    hidden.setObjectName('hidden-panel')
    host.addDockWidget(Qt.RightDockWidgetArea, hidden)
    hidden.hide()
    bar = host.addToolBar('专业操作')
    bar.setObjectName('actions')
    host.menuBar().addMenu('系统')


class HomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(MainWindow, 'init_ui', seeded_ui):
            self.host = HomeTerminal(None, None, None)
        self.host.show()
        self.app.processEvents()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.hide)
        self.addCleanup(self.host._timer.stop)

    def test_startup_home_and_roundtrip_restore(self):
        w = self.host
        self.assertTrue(w.home.isVisible())
        self.assertTrue(w.findChild(QDockWidget,'orders').isHidden())
        self.assertTrue(w.findChild(QToolBar,'actions').isHidden())
        self.assertIn('A_2701_2703',w._rows)
        w.home.professional.emit()
        self.assertFalse(w.home.isVisible())
        self.assertTrue(w.findChild(QDockWidget,'orders').isVisible())
        self.assertTrue(w.findChild(QDockWidget,'hidden-panel').isHidden())
        w.menuBar().actions()[0].trigger()
        self.assertTrue(w.home.isVisible())
        with patch.object(MainWindow,'save_window_setting') as save:
            w.save_window_setting('custom')
            save.assert_called_once_with('custom')
            self.assertTrue(w.findChild(QDockWidget,'orders').isVisible())

    def test_drilldown_and_configuration_keep_selection(self):
        w = self.host
        name = 'A_2701_2703'
        w.home.orders_spread.emit(name)
        self.assertIn(name,w._detail.windowTitle())
        self.assertTrue(w._detail.isVisible())
        w.home.inspect_spread.emit(name)
        self.assertIn('合约详情',w._detail.windowTitle())
        with patch('workbench.draft_dialog.DraftDialog',return_value=Mock()) as factory:
            w.home.configure_spread.emit(name)
            self.assertEqual(factory.call_args.args[1], name)
