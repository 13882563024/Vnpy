import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace as N
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDockWidget, QLabel, QToolBar
from vnpy.trader.ui import MainWindow

from workbench.home_terminal import BUSY_VIEW_MAX_AGE, HomeTerminal
from workbench.overview_page import OverviewPage
from utils import layer_store, pnl_ledger


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


    def test_busy_gateway_preserves_previous_frame_until_new_data_arrives(self):
        w = self.host
        lock = Lock()
        td = N(brokerid='b', userid='u', cabinet_trading_day='2026-11-19',
               login_status=True, _send_lock=lock)
        account = N(accountid='u', gateway_name='CTP', balance=123)
        w.main_engine = N(gateways={'CTP': N(td_api=td)}, get_all_accounts=lambda: [account])
        w.refresh_home()
        balance = w.home._metrics['balance']
        self.assertEqual(balance.text(), '123.00')
        previous = w.home.table.item(0, 0)
        with lock:
            account.balance = 456
            w.refresh_home()
            self.assertEqual(balance.text(), '123.00')
            self.assertIn('显示上次数据', w.home._state.text())
            self.assertIs(w.home.table.item(0, 0), previous)
        w.refresh_home()
        self.assertEqual(balance.text(), '456.00')
        self.assertNotIn('显示上次数据', w.home._state.text())
        td.login_status = False
        w.refresh_home()
        self.assertEqual(balance.text(), '未连接柜台')

    def test_busy_gateway_does_not_keep_stale_values_indefinitely(self):
        w = self.host
        lock = Lock()
        td = N(brokerid='b', userid='u', cabinet_trading_day='2026-11-19',
               login_status=True, _send_lock=lock)
        account = N(accountid='u', gateway_name='CTP', balance=123)
        w.main_engine = N(gateways={'CTP': N(td_api=td)}, get_all_accounts=lambda: [account])
        w.refresh_home()
        w._last_view_at -= BUSY_VIEW_MAX_AGE + 1
        with lock:
            w.refresh_home()
        self.assertEqual(w.home._metrics['balance'].text(), '未知')
        self.assertIn('状态待确认', w.home._state.text())

    def test_table_reuses_items_and_preserves_selection_on_refresh(self):
        page = OverviewPage(self.host)
        a = dict(name='A', priority=1, status='未决')
        b = dict(name='B', priority=2, status='正常')
        page.set_view({'rows': [a, b]})
        page.table.setCurrentCell(1, 4)
        status_item = page.table.item(0, 1)
        self.assertTrue(status_item.font().bold())

        page.set_view({'rows': [a, dict(b, priority=0, status='异常')]})
        self.assertIs(status_item, page.table.item(0, 1))
        self.assertEqual(status_item.text(), '异常')
        self.assertEqual(page._current()['name'], 'B')
        self.assertEqual(page.table.currentColumn(), 4)

        page.set_view({'rows': [dict(b, priority=2), dict(a, priority=3)]})
        self.assertFalse(status_item.font().bold())

    def test_disconnected_home_reads_persisted_layers_and_realized_pnl(self):
        w = self.host
        name = 'A_2701_2703'
        layer = dict(layer_index=2, direction=1, volume=1, avg_spread=-14,
                     near_price=4200, far_price=4214, target_spread=-9, algoid='old-order')
        with TemporaryDirectory() as temp, \
                patch.object(layer_store, '_path', return_value=Path(temp) / 'layers.json'), \
                patch.object(pnl_ledger, '_path', return_value=Path(temp) / 'pnl.json'), \
                patch('workbench.overview_data.trading_day_str', return_value='2026-11-19'):
            layer_store.save(name, [dict(layer, state='CLOSED', layer_index=3), layer,
                                    dict(layer, layer_index=4, volume='bad')])
            pnl_ledger.record(name, 123.45, '2026-11-19')
            w.refresh_home()

            row = w._rows[name]
            self.assertEqual(row['layers'], '本地 1 层')
            self.assertIn('第2层', row['legs'])
            self.assertEqual(row['take_profit'], '-9')
            self.assertEqual((row['total'], row['today']), ('+123.45', '+123.45'))
            self.assertIsNone(row['held'])
            self.assertEqual(row['protection'], '柜台未核实')
            self.assertIn('本地缓存', w.home._scope.text())
            self.assertEqual(w.home._metrics['balance'].text(), '未连接柜台')

            layer_store.save(name, [])
            with patch('workbench.overview_data.trading_day_str', return_value='2026-11-20'):
                w.refresh_home()
            self.assertEqual(w._rows[name]['layers'], '本地 0 层')
            self.assertEqual(w._rows[name]['today'], '未知')
            self.assertIsNone(w._rows[name]['held'])
