"""委托/成交记录的 CSV 导出对话框。

与「导出价差 CSV」对称：选日期范围（可选按价差/合约过滤）→ 汇总到
``exports/trading_records/<日期>/``。数据源是程序实时落盘的
``~/.vntrader/records``，只做本地读文件 + 拼接，毫秒级完成，所以同步执行即可，
不需要再引入后台线程。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from vnpy.trader.ui import QtCore, QtGui, QtWidgets

from recorder.trade_recorder import DEFAULT_RECORD_ROOT
from utils.record_export import (
    DEFAULT_OUTPUT_ROOT,
    describe_days,
    export_range,
    list_days,
)


class TradeExportDialog(QtWidgets.QDialog):
    """选择交易日范围 → 导出委托 / 成交 / 价差成交 CSV。"""

    def __init__(self, parent=None, record_root: Path | str | None = None,
                 out_root: Path | str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出交易记录 CSV")
        self.setMinimumSize(760, 620)
        self._record_root = Path(record_root) if record_root else DEFAULT_RECORD_ROOT
        self._out_root = Path(out_root) if out_root else DEFAULT_OUTPUT_ROOT
        self._build_ui()
        self._refresh_days()

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        self.start_edit = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd")
        self.end_edit = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd")

        self.match_edit = QtWidgets.QLineEdit()
        self.match_edit.setPlaceholderText("可选：价差名或合约代码子串，如 PK_701_703 / pg2701")
        self.match_edit.setClearButtonEnabled(True)

        self.out_edit = QtWidgets.QLineEdit()
        self.out_edit.setPlaceholderText(
            f"留空 = {self._out_root} / <日期>（起止不同则用 <起>_<止>）")
        browse = QtWidgets.QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        out_row = QtWidgets.QHBoxLayout()
        out_row.addWidget(self.out_edit)
        out_row.addWidget(browse)

        form = QtWidgets.QFormLayout()
        day_row = QtWidgets.QHBoxLayout()
        day_row.addWidget(self.start_edit)
        day_row.addWidget(QtWidgets.QLabel("至"))
        day_row.addWidget(self.end_edit)
        day_row.addStretch()
        form.addRow("交易日", day_row)
        form.addRow("过滤", self.match_edit)
        form.addRow("输出到", out_row)

        self.overview = QtWidgets.QPlainTextEdit()
        self.overview.setReadOnly(True)
        self.overview.setMaximumHeight(170)
        self.overview.setPlaceholderText("已记录的交易日")

        refresh = QtWidgets.QPushButton("刷新记录列表")
        refresh.clicked.connect(self._refresh_days)
        open_dir = QtWidgets.QPushButton("打开记录目录")
        open_dir.clicked.connect(self._open_record_dir)
        self.export_button = QtWidgets.QPushButton("导出")
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self._export)
        close_button = QtWidgets.QPushButton("关闭")
        close_button.clicked.connect(self.accept)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(refresh)
        buttons.addWidget(open_dir)
        buttons.addStretch()
        buttons.addWidget(self.export_button)
        buttons.addWidget(close_button)

        self.result = QtWidgets.QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("导出结果")

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QtWidgets.QLabel(
            f"记录来源：{self._record_root}（由程序运行期实时写入，重启不丢）"))
        layout.addWidget(self.overview)
        layout.addLayout(buttons)
        layout.addWidget(self.result, stretch=1)

    # ------------------------------------------------------------ 动作
    def _refresh_days(self) -> None:
        rows = list_days(self._record_root)
        self.overview.setPlainText(describe_days(rows))
        if rows:
            latest = rows[0][0]
            try:
                moment = QtCore.QDate.fromString(latest, "yyyy-MM-dd")
            except Exception:      # noqa: BLE001 - 解析失败就用今天
                moment = QtCore.QDate.currentDate()
            if moment.isValid():
                self.start_edit.setDate(moment)
                self.end_edit.setDate(moment)

    def _browse(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.out_edit.text() or str(self._out_root))
        if folder:
            self.out_edit.setText(folder)
    def _open_record_dir(self) -> None:
        if not self._record_root.exists():
            self.result.setPlainText(
                f"记录目录还不存在：{self._record_root}\n"
                "程序运行并产生委托/成交后才会生成。")
            return
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(self._record_root)))

    def _export(self) -> None:
        start = self.start_edit.date().toPython()
        end = self.end_edit.date().toPython()
        if end < start:
            self.result.setPlainText("结束日期早于开始日期")
            return
        out_text = self.out_edit.text().strip()
        if out_text:
            target = Path(out_text)
        else:
            suffix = (start.isoformat() if start == end
                      else f"{start.isoformat()}_{end.isoformat()}")
            target = self._out_root / suffix
        lines: list[str] = []
        try:
            result = export_range(start, end, root=self._record_root,
                                  out_dir=target,
                                  match=self.match_edit.text().strip(),
                                  output=lines.append)
        except (ValueError, FileNotFoundError, OSError) as exc:
            self.result.setPlainText(f"导出失败：{exc}")
            return

        if not any(result.values()):
            self.result.setPlainText(
                f"{start} ~ {end} 之间没有匹配的委托/成交记录。\n"
                "（记录从程序启动后开始收集，之前的历史请查柜台结算单）")
            return
        summary = [
            f"导出完成：{start} ~ {end}",
            f"  委托 {result['orders']} 行、成交 {result['trades']} 行、"
            f"价差成交 {result['spread_trades']} 行",
            f"  输出目录：{target}",
            "",
        ]
        self.result.setPlainText("\n".join(summary + lines))
        self._refresh_days()
