"""The normal vn.py window, with a read-only first screen and its original dock layout."""
import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialog, QDockWidget, QLabel, QPlainTextEdit, QToolBar, QVBoxLayout
from vnpy.trader.ui import MainWindow

from .model import DEFAULT_CONFIG, load_snapshot
from .overview_data import overview_view
from .overview_page import OverviewPage


class HomeTerminal(MainWindow):
    def __init__(self, main_engine, event_engine, spread_engine, *, config_path=DEFAULT_CONFIG):
        self._at_home = False
        super().__init__(main_engine, event_engine)
        self._spread_menu_promoted = self._promote_spread_menu()
        self._spread_engine = spread_engine
        self._path = Path(config_path)
        self._revision = None
        self._configs = {}
        self._rows = {}
        self._detail = None
        self._detail_key = ('', '')
        self.home = OverviewPage(self)
        self.setCentralWidget(self.home)
        self.home.professional.connect(self.show_professional)
        self.home.inspect_spread.connect(lambda name: self._show_detail(name, '合约详情'))
        self.home.orders_spread.connect(lambda name: self._show_detail(name, '委托成交'))
        self.home.configure_spread.connect(self._configure)
        home_action = QAction('交易总览', self)
        home_action.triggered.connect(self.show_home)
        home_action.setShortcut('Alt+1')
        bar = self.menuBar()
        bar.insertAction(bar.actions()[0], home_action)
        self._timer = QTimer(self, interval=1500)
        self._timer.timeout.connect(self.refresh_home)
        self.show_home()

    def _promote_spread_menu(self) -> bool:
        """把「价差交易」从「功能」菜单提到菜单栏一级（与功能同级）。"""
        try:
            from spreadconsole import SpreadConsoleApp
        except Exception:
            return False
        title = SpreadConsoleApp.display_name
        bar = self.menuBar()
        for action in bar.actions():
            menu = action.menu()
            if menu is None or menu.title().replace('&', '') == title:
                continue
            for item in menu.actions():
                if item.text().replace('&', '') != title:
                    continue
                menu.removeAction(item)
                top = bar.addMenu(title)
                top.addAction(item)
                return True
        return False

    def show_home(self):
        if not self._at_home:
            self._professional_state = self.saveState()
            self._at_home = True
            for pane in [*self.findChildren(QDockWidget), *self.findChildren(QToolBar)]:
                pane.hide()
            self.home.show()
            self.setWindowTitle('交易总览 · VeighNa')
        self.refresh_home()
        self._timer.start()

    def show_professional(self):
        if self._at_home:
            self._timer.stop()
            if self._detail:
                self._detail.hide()
            self.home.hide()
            self.restoreState(self._professional_state)
            self._at_home = False
            self.setWindowTitle(self.window_title)

    def save_window_setting(self, name):
        # Base closeEvent persists custom layout only after exit is confirmed.
        if name == 'custom' and self._at_home:
            self.show_professional()
        super().save_window_setting(name)

    def restore_window_setting(self):
        self.show_professional()
        super().restore_window_setting()

    def refresh_home(self):
        problem = ''
        try:
            stat = self._path.stat()
            stamp = stat.st_mtime_ns, stat.st_size
            if stamp != self._revision:
                config = json.loads(self._path.read_text(encoding='utf-8'))
                if not isinstance(config, dict) or not all(isinstance(x, dict) for x in config.values()):
                    raise ValueError('价差注册表格式错误')
                self._configs, self._revision = config, stamp
        except (ValueError, OSError) as exc:
            problem = f'配置读取失败：{exc}'
        try:
            view = overview_view(self._configs, self.main_engine, self._spread_engine)
        except Exception:
            # Concurrent cache updates are possible; do not leave old values on screen as current.
            view = overview_view(self._configs)
            problem = '本轮终端数据不可用，等待刷新'
        if problem:
            view['source'] = problem
        self._rows = {r['name']: r for r in view['rows']}
        self.home.set_view(view)
        if self._detail and self._detail.isVisible():
            self._refresh_detail()

    def _refresh_detail(self):
        name, section = self._detail_key
        r = self._rows.get(name, {})
        self._detail.setWindowTitle(f'{name} · {section}')
        self._detail_title.setText(f"{r.get('display',name)}　{section}")
        value = r.get('orders' if section == '委托成交' else 'detail')
        if isinstance(value, (tuple, list)):
            value = '\n'.join(map(str,value))
        text = str(value or '暂无可核实记录')
        if self._detail_text.toPlainText() != text:
            self._detail_text.setPlainText(text)

    def _show_detail(self, name, section):
        if name not in self._rows:
            return
        if self._detail is None:
            self._detail = QDialog(self)
            self._detail.resize(730, 450)
            box = QVBoxLayout(self._detail)
            self._detail_title = QLabel(self._detail)
            self._detail_title.setTextFormat(Qt.TextFormat.PlainText)
            self._detail_text = QPlainTextEdit(self._detail, readOnly=True)
            box.addWidget(self._detail_title)
            box.addWidget(self._detail_text)
        self._detail_key = name, section
        self._refresh_detail()
        self._detail.show()
        self._detail.raise_()

    def _configure(self, name):
        from .draft_dialog import DraftDialog
        from .model import Sources
        snap = load_snapshot(Sources(config=self._path))
        if name in snap.spreads:
            dlg = DraftDialog(snap, name, self)
            try:
                dlg.exec()
            finally:
                dlg.deleteLater()
