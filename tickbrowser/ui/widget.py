"""tick 浏览面板：在 GUI「功能」菜单里查看已采集的行情数据。

为什么不用 ``vnpy_datamanager``：它 1.2.0 的界面只列 K 线（``Interval.MINUTE/HOUR/DAILY``
+ ``load_bar_data``），tick 只能"下载入库"、不能浏览 —— 装了也看不到本项目的采集结果。

面板提供三块视图：

1. **tick 明细**：左侧合约列表，右侧最近 N 条 tick（价格 / 买一卖一 / 挂量 / 成交量）
2. **按天汇总**：每天每个合约采了多少条
3. **价差预览**：把两腿 tick 合成价差（与策略、回测同一套合成口径）
"""

from __future__ import annotations

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import QtCore, QtWidgets

TICK_HEADERS = ("时间", "最新", "买一", "卖一", "买量", "卖量", "成交量")
DAY_HEADERS = ("日期", "合约", "条数")
SPREAD_HEADERS = ("时间", "价差bid", "价差ask", "mid", "可成交手数")

VIEW_TICK = "tick 明细"
VIEW_DAY = "按天汇总"
VIEW_SPREAD = "价差预览"


class TickBrowserWidget(QtWidgets.QWidget):
    """行情数据浏览界面。"""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.engine = main_engine.get_engine("TickBrowser")
        if self.engine is None:
            # 兜底：面板可以独立使用（不依赖 add_app 是否成功）
            from tickbrowser.engine import TickBrowserEngine

            self.engine = TickBrowserEngine(main_engine, event_engine)

        self.init_ui()
        self.refresh_symbols()

    # ------------------------------------------------------------ 界面
    def init_ui(self) -> None:
        self.setWindowTitle("行情数据浏览")
        self.setMinimumSize(1000, 620)

        # 顶部工具条
        self.mode_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.mode_combo.addItems([VIEW_TICK, VIEW_DAY, VIEW_SPREAD])
        self.mode_combo.currentTextChanged.connect(self.on_mode_changed)

        self.spread_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.spread_combo.setMinimumWidth(180)
        self.spread_combo.currentTextChanged.connect(self.on_spread_changed)

        self.limit_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.limit_spin.setRange(10, 100000)
        self.limit_spin.setValue(200)
        self.limit_spin.setSingleStep(100)

        refresh_button = QtWidgets.QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh_symbols)

        show_button = QtWidgets.QPushButton("查看")
        show_button.clicked.connect(self.show_current)

        # 导出：弹对话框勾选价差，后台线程导 CSV（不阻塞主界面）
        export_button = QtWidgets.QPushButton("导出价差 CSV…")
        export_button.setToolTip("按交易日导出：选中价差、后台执行，导出时仍可正常看行情")
        export_button.clicked.connect(self.export_spreads)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("视图"))
        top.addWidget(self.mode_combo)
        top.addWidget(QtWidgets.QLabel("价差"))
        top.addWidget(self.spread_combo)
        top.addWidget(QtWidgets.QLabel("条数"))
        top.addWidget(self.limit_spin)
        top.addWidget(show_button)
        top.addWidget(refresh_button)
        top.addSpacing(12)
        top.addWidget(export_button)
        top.addStretch()

        # 左侧合约列表
        self.symbol_list: QtWidgets.QListWidget = QtWidgets.QListWidget()
        self.symbol_list.setMinimumWidth(320)
        self.symbol_list.itemClicked.connect(lambda _item: self.show_current())

        # 右侧表格
        self.table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self.symbol_list)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(1, 3)

        # 底部状态栏
        self.status_label: QtWidgets.QLabel = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(splitter)
        layout.addWidget(self.status_label)
        self.setLayout(layout)

        self.spread_combo.addItems(self.engine.spread_names())

    # ------------------------------------------------------------ 数据
    def refresh_symbols(self) -> None:
        """重新读库里的合约汇总（条数以 dbtickdata 实际行数为准）。"""
        self.symbol_list.clear()
        current_spread = self.spread_combo.currentText()
        names = self.engine.spread_names()
        if [self.spread_combo.itemText(i) for i in range(self.spread_combo.count())] != names:
            self.spread_combo.blockSignals(True)
            self.spread_combo.clear()
            self.spread_combo.addItems(names)
            if current_spread in names:
                self.spread_combo.setCurrentText(current_spread)
            self.spread_combo.blockSignals(False)
        self.status_label.setText(self.engine.describe_store())

        symbols = self.engine.list_symbols()
        if not symbols:
            item = QtWidgets.QListWidgetItem("（还没有采集数据）")
            item.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.symbol_list.addItem(item)
            return

        for summary in symbols:
            item = QtWidgets.QListWidgetItem(summary.describe())
            item.setData(QtCore.Qt.ItemDataRole.UserRole, (summary.symbol, summary.exchange))
            self.symbol_list.addItem(item)

        self.symbol_list.setCurrentRow(0)
        self.show_current()

    def current_symbol(self) -> tuple[str, str]:
        item = self.symbol_list.currentItem()
        if item is None:
            return "", ""
        data = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return tuple(data) if data else ("", "")

    def on_mode_changed(self, mode: str) -> None:
        is_spread = mode == VIEW_SPREAD
        self.spread_combo.setEnabled(is_spread)
        self.symbol_list.setEnabled(not is_spread and mode == VIEW_TICK)
        self.show_current()

    def on_spread_changed(self, _name: str) -> None:
        """价差下拉切换后立即刷新（不需要再点"查看"）。"""
        if self.mode_combo.currentText() == VIEW_SPREAD:
            self.show_current()

    def export_spreads(self) -> None:
        """打开导出对话框：勾选价差、按交易日后台导出 CSV。"""
        from tickbrowser.ui.export_dialog import SpreadExportDialog
        from utils.spread_config import load_spread_configs

        try:
            spreads = load_spread_configs()
        except Exception as exc:  # noqa: BLE001 - 配置坏了只提示，不影响行情/交易
            self.status_label.setText(f"读取价差配置失败：{exc!r}")
            return

        dialog = getattr(self, "_export_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return

        dialog = SpreadExportDialog(self, spreads=spreads)
        dialog.setModal(False)      # 非模态 + 后台线程：导出时照常看行情、下单
        dialog.show()
        self._export_dialog = dialog

    def show_current(self) -> None:
        mode = self.mode_combo.currentText()
        limit = int(self.limit_spin.value())

        if mode == VIEW_SPREAD:
            self.show_spread(limit)
        elif mode == VIEW_DAY:
            self.show_days()
        else:
            self.show_ticks(limit)

    def show_ticks(self, limit: int) -> None:
        symbol, exchange = self.current_symbol()
        if not symbol:
            self.set_table(TICK_HEADERS, [])
            return

        rows = self.engine.load_recent(symbol, exchange, limit)
        display = [
            (str(row[0]), self.num(row[1]), self.num(row[2]), self.num(row[3]),
             self.num(row[4], digits=0), self.num(row[5], digits=0), self.num(row[6], digits=0))
            for row in rows
        ]
        self.set_table(TICK_HEADERS, display)
        self.status_label.setText(
            f"{symbol}.{exchange} 最近 {len(rows)} 条（时间倒序）｜ {self.engine.describe_store()}"
        )

    def show_days(self) -> None:
        days = self.engine.list_days()
        display = [(day.day, f"{day.symbol}.{day.exchange}", str(day.count)) for day in days]
        self.set_table(DAY_HEADERS, display)
        total = sum(day.count for day in days)
        self.status_label.setText(f"按天汇总：{len(days)} 行，合计 {total} 条 tick")

    def show_spread(self, limit: int) -> None:
        name = self.spread_combo.currentText().strip()
        if not name:
            self.set_table(SPREAD_HEADERS, [])
            self.status_label.setText("config/spreads.json 里还没有配置价差")
            return

        rows, summary = self.engine.preview_spread(name, limit)
        display = [
            (str(row[0]), self.num(row[1]), self.num(row[2]), self.num(row[3]),
             self.num(row[4], digits=0))
            for row in rows
        ]
        self.set_table(SPREAD_HEADERS, display)
        self.status_label.setText(summary)

    # ------------------------------------------------------------ 表格
    def set_table(self, headers: tuple[str, ...], rows: list[tuple]) -> None:
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(list(headers))
        self.table.setRowCount(len(rows))

        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                item = QtWidgets.QTableWidgetItem(str(value))
                if column > 0:
                    item.setTextAlignment(
                        QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row_index, column, item)

        self.table.resizeColumnsToContents()

    @staticmethod
    def num(value, digits: int = 2) -> str:
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return "—"

    def show(self) -> None:      # noqa: D102 - 与 Qt 签名保持一致
        super().show()
        self.refresh_symbols()
