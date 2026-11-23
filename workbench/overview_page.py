"""Read-only account and spread overview, populated exclusively by its host."""
from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)


_METRICS = (('balance', '账户权益'), ('available', '可用资金'), ('margin', '占用保证金'),
            ('realized', '已实现收益'), ('unrealized', '未实现收益'))
_COLUMNS = (('display', '价差'), ('status', '状态'), ('layers', '层级'), ('legs', '合约腿'),
            ('live', '实时价差'), ('take_profit', '预挂止盈价差'), ('add', '预挂加仓价差'),
            ('protection', '保护状态'), ('opening', '开仓状态'),
            ('total', '累计收益'), ('today', '当天收益'), ('log', '最新策略日志'))
_STYLE = '''
QWidget#overviewBody {background:white;}
QScrollArea#overviewScroll, QScrollArea#overviewScroll > QWidget > QWidget {background:white;}
QWidget#overviewBody QWidget {font-family:"Microsoft YaHei UI","Noto Sans CJK SC",sans-serif;font-size:14px;color:#163640;}
QWidget#overviewBody QLabel {background:transparent;}
QWidget#overviewBody QLabel#title {font-size:26px;font-weight:600;}
QWidget#overviewBody QLabel#section {font-size:19px;font-weight:600;}
QWidget#overviewBody QLabel#meta {font-size:13px;color:#48616b;}
QFrame#accountBand {background:#163b49;}
QFrame#accountBand QLabel#metricLabel {color:#c4dadc;font-size:13px;}
QFrame#accountBand QLabel#metricValue {color:#ffffff;font-size:20px;font-weight:600;}
QWidget#overviewBody QPushButton {background:white;border:1px solid #adc3c6;border-radius:4px;padding:6px 12px;}
QWidget#overviewBody QPushButton:hover {background:#e6f3f1;}
QWidget#overviewBody QPushButton:disabled {color:#78888d;background:#f4f7f7;}
QWidget#overviewBody QPushButton:focus {border:2px solid #167677;padding:5px 11px;}
QTableWidget#overviewTable {background:white;alternate-background-color:#f5f8f8;border:1px solid #cbd8da;selection-background-color:#c4e9e5;selection-color:#133940;}
QTableWidget#overviewTable::item {padding:3px 7px;}
QTableWidget#overviewTable::item:selected {background:#c4e9e5;color:#133940;}
QTableWidget#overviewTable QHeaderView::section {background:#e8eff0;color:#405c64;padding:6px 7px;border:0;border-bottom:1px solid #cbd8da;font-weight:600;}
QPlainTextEdit#overviewText {background:#f4f7f7;border:0;padding:5px 8px;selection-background-color:#c4e9e5;selection-color:#133940;}
'''


def _text(value, default='未提供'):
    if isinstance(value, (tuple, list)):
        return '\n'.join(str(item) for item in value) or default
    return default if value is None or value == '' else str(value)


def _label(text='', role=''):
    label = QLabel(text)
    label.setObjectName(role)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _ensure_cjk_font():
    """Offscreen Qt ships no fonts; register an installed one in memory only."""
    import os
    from pathlib import Path
    from PySide6.QtGui import QFontDatabase, QGuiApplication
    if os.name != 'nt' or QGuiApplication.platformName() != 'offscreen':
        return
    if 'Microsoft YaHei UI' in QFontDatabase.families():
        return
    folder = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    for filename in ('msyh.ttc', 'msyhbd.ttc'):
        if (folder / filename).is_file():
            QFontDatabase.addApplicationFont(str(folder / filename))


class OverviewPage(QWidget):
    professional = Signal()
    inspect_spread = Signal(str)
    configure_spread = Signal(str)
    orders_spread = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        _ensure_cjk_font()
        self._rows = []
        self._metrics = {}
        self.setStyleSheet(_STYLE)
        scroll = QScrollArea()
        scroll.setObjectName('overviewScroll')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName('overviewBody')
        # A QWidget background in a stylesheet needs WA_StyledBackground, otherwise the
        # host window's palette (the terminal uses a dark one) shows through.
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body.setMinimumWidth(675)
        scroll.setWidget(body)
        page = QVBoxLayout(body)
        page.setContentsMargins(20, 12, 20, 10)
        page.setSpacing(5)

        header = QHBoxLayout()
        header.addWidget(_label('账户总览', 'title'), 1)
        terminal = QPushButton('专业终端')
        terminal.clicked.connect(self.professional.emit)
        header.addWidget(terminal)
        page.addLayout(header)
        self._scope = _label(role='meta')
        self._state = _label(role='meta')
        for label in (self._scope, self._state):
            label.setWordWrap(True)
            page.addWidget(label)
        band = QFrame()
        band.setObjectName('accountBand')
        figures = QHBoxLayout(band)
        figures.setContentsMargins(16, 8, 16, 9)
        figures.setSpacing(12)
        for key, caption in _METRICS:
            column = QVBoxLayout()
            column.setSpacing(2)
            column.addWidget(_label(caption, 'metricLabel'))
            value = _label(role='metricValue')
            value.setWordWrap(True)
            value.setMinimumWidth(0)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            column.addWidget(value)
            figures.addLayout(column, 1)
            self._metrics[key] = value
        page.addWidget(band)
        page.addSpacing(5)

        bar = QHBoxLayout()
        bar.addWidget(_label('价差总表', 'section'))
        bar.addStretch()
        self._count = _label(role='meta')
        bar.addWidget(self._count)
        page.addLayout(bar)
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setObjectName('overviewTable')
        self.table.setAccessibleName('价差总表，异常优先')
        self.table.setHorizontalHeaderLabels([caption for _, caption in _COLUMNS])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setMinimumSectionSize(23)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(150)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        head = self.table.horizontalHeader()
        head.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        head.setMinimumSectionSize(56)
        # 每列按内容自适应：状态、合约腿、价差这些长文案不允许被省略号截断。
        for column in range(len(_COLUMNS)):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.currentCellChanged.connect(self._selection)
        page.addWidget(self.table, 5)
        self._empty = _label('当前视图没有价差记录。', 'meta')
        page.addWidget(self._empty)
        page.addSpacing(4)

        bar = QHBoxLayout()
        self._selected = _label(role='section')
        self._selected.setWordWrap(True)
        bar.addWidget(self._selected, 1)
        self._actions = []
        for caption, signal in (('价差详情', self.inspect_spread), ('查看委托', self.orders_spread),
                                ('价差配置', self.configure_spread)):
            button = QPushButton(caption)
            button.clicked.connect(lambda checked=False, sig=signal: self._navigate(sig))
            self._actions.append(button)
            bar.addWidget(button)
        page.addLayout(bar)
        panels = QHBoxLayout()
        panels.setSpacing(12)
        self._panels = {}
        for key, caption in (('detail', '价差详情'), ('orders', '委托记录')):
            col = QVBoxLayout()
            col.addWidget(_label(caption, 'meta'))
            field = QPlainTextEdit()
            field.setObjectName('overviewText')
            field.setReadOnly(True)
            field.setTabChangesFocus(True)
            field.setAccessibleName(caption)
            field.setMinimumHeight(52)
            col.addWidget(field)
            panels.addLayout(col, 1)
            self._panels[key] = field
        page.addLayout(panels, 2)
        self.set_view({})

    def _current(self):
        idx = self.table.currentRow()
        return self._rows[idx] if 0 <= idx < len(self._rows) else {}

    def _navigate(self, signal):
        name = self._current().get('name')
        if name:
            signal.emit(str(name))

    def _selection(self, *_):
        row = self._current()
        self._selected.setText(_text(row.get('display'), '选中价差') if row else '未选择价差')
        self._selected.setToolTip(_text(row.get('name'), ''))
        for button in self._actions:
            button.setEnabled(bool(row.get('name')))
        for key, field in self._panels.items():
            content = _text(row.get(key), '当前来源未提供记录。') if row else '未选择价差。'
            if field.toPlainText() != content:
                field.setPlainText(content)

    def set_view(self, view: dict):
        """Replace the display snapshot; preserve the selected spread by key."""
        def line(fields):
            return '    ·    '.join(f'{caption}：{_text(view.get(key))}' for key, caption in fields)

        self._scope.setText(line((('source', '来源'), ('account', '账户'), ('day', '交易日'))))
        self._state.setText(line((('connection', '连接'), ('permission', '权限'), ('updated', '更新'))))
        for key, label in self._metrics.items():
            value = _text(view.get(key))
            label.setText(value)
            label.setToolTip(value)
        previous = self._current().get('name')
        previous_column = max(self.table.currentColumn(), 0)
        rows = sorted((dict(row) for row in (view.get('rows') or [])),
                      key=lambda row: row.get('priority', float('inf')))
        if rows != self._rows:
            with QSignalBlocker(self.table):
                self._rows = rows
                if self.table.rowCount() != len(rows):
                    self.table.setRowCount(len(rows))
                selected = 0
                for i, row in enumerate(rows):
                    if previous and row.get('name') == previous:
                        selected = i
                    for j, (key, _) in enumerate(_COLUMNS):
                        value = _text(row.get(key), _text(row.get('name')) if key == 'display' else '未提供')
                        tooltip = value
                        if key == 'log' and len(value) > 52:
                            value = value[:51] + '…'      # 单元格截短，悬停看全文
                        item = self.table.item(i, j)
                        if item is None:
                            item = QTableWidgetItem()
                            self.table.setItem(i, j, item)
                        if item.text() != value:
                            item.setText(value)
                        if item.toolTip() != tooltip:
                            item.setToolTip(tooltip)
                        # The terminal ships a dark global palette; set our own text colour so
                        # selected rows stay readable instead of inheriting white on mint.
                        priority = row.get('priority') if key == 'status' else None
                        color = QColor({0: '#963c26', 1: '#775515'}.get(priority, '#163640'))
                        if item.foreground().color() != color:
                            item.setForeground(color)
                        font = item.font()
                        if font.bold() != (priority in (0, 1)):
                            font.setBold(priority in (0, 1))
                            item.setFont(font)
                if rows and self.table.currentRow() != selected:
                    self.table.setCurrentCell(selected, previous_column)
        self.table.setVisible(bool(rows))
        self._empty.setVisible(not rows)
        held = sum(r.get('held') is True for r in rows)
        unknown = sum(r.get('held') is None for r in rows)
        attention = sum(r.get('priority') in (0, 1) for r in rows)
        self._count.setText(
            f'{len(rows)} 个价差  ·  已核实有仓 {held}  ·  仓位未知 {unknown}'
            f'  ·  异常或未决 {attention}')
        self._selection()
