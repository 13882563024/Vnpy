"""价差 CSV 导出对话框：勾选要导的价差，后台线程执行，界面不卡。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from vnpy.trader.ui import QtCore, QtWidgets

DEFAULT_OUTPUT_ROOT = Path("exports") / "spread_csv"


class ExportWorker(QtCore.QThread):
    """后台导出线程：只读库 + 写 CSV，不碰 Qt 对象，进度用信号回主线程。"""

    progressed = QtCore.Signal(int, int, str)     # 已完成, 总数, 当前价差名
    done = QtCore.Signal(object)                  # ExportReport
    failed = QtCore.Signal(str)

    def __init__(self, day: date, names: list[str] | None, out_dir: str,
                 source: str = "auto", parent=None) -> None:
        super().__init__(parent)
        self.day = day
        self.names = list(names) if names else None
        self.out_dir = out_dir
        self.source = source

    def run(self) -> None:                        # noqa: D102 - QThread 入口
        try:
            from utils.spread_export import export_spread_day

            report = export_spread_day(
                self.day,
                self.out_dir or None,
                names=self.names,
                source=self.source,
                progress=self._report_progress,
                output=lambda *_args, **_kwargs: None,
            )
        except Exception as exc:  # noqa: BLE001 - 线程里必须自己兜住，否则静默死掉
            self.failed.emit(repr(exc))
            return
        self.done.emit(report)

    def _report_progress(self, done: int, total: int, name: str) -> None:
        self.progressed.emit(done, total, name)


class SpreadExportDialog(QtWidgets.QDialog):
    """选择交易日 + 勾选价差 → 后台导出 CSV。"""

    def __init__(self, parent=None, spreads: dict | None = None,
                 default_day: date | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出价差 CSV")
        self.setMinimumSize(760, 640)
        self._spreads = dict(spreads or {})
        self._worker: ExportWorker | None = None
        self._build_ui(default_day)
        self._fill_spreads()

    # ------------------------------------------------------------ 界面
    def _build_ui(self, default_day: date | None) -> None:
        self.date_edit = QtWidgets.QDateEdit(
            QtCore.QDate(default_day.year, default_day.month, default_day.day)
            if default_day else QtCore.QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setToolTip("按交易日导出：夜盘归到下一交易日（周一会带上周五夜盘）")

        self.out_edit = QtWidgets.QLineEdit(str(DEFAULT_OUTPUT_ROOT))
        browse = QtWidgets.QPushButton("浏览…")
        browse.clicked.connect(self._choose_dir)

        out_row = QtWidgets.QHBoxLayout()
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(browse)

        form = QtWidgets.QFormLayout()
        form.addRow("交易日", self.date_edit)
        form.addRow("输出目录", out_row)

        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setToolTip("勾选要导出的价差；默认全选")

        pick_all = QtWidgets.QPushButton("全选")
        pick_all.clicked.connect(lambda: self._set_all(True))
        pick_none = QtWidgets.QPushButton("全不选")
        pick_none.clicked.connect(lambda: self._set_all(False))
        pick_row = QtWidgets.QHBoxLayout()
        pick_row.addWidget(pick_all)
        pick_row.addWidget(pick_none)
        pick_row.addStretch()
        pick_row.addWidget(QtWidgets.QLabel("按交易日导出；已落库的价差优先，缺失时现合成"))

        self.progress = QtWidgets.QProgressBar()
        self.progress.setValue(0)
        self.progress.setFormat("%v / %m")
        self.progress_label = QtWidgets.QLabel("待导出")

        self.result = QtWidgets.QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMinimumHeight(150)

        self.start_button = QtWidgets.QPushButton("开始导出")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self._start)
        self.close_button = QtWidgets.QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.close_button)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.list, 2)
        layout.addLayout(pick_row)
        layout.addWidget(self.progress)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.result, 1)
        layout.addLayout(buttons)
        self.setLayout(layout)

    def _fill_spreads(self) -> None:
        if not self._spreads:
            item = QtWidgets.QListWidgetItem("（config/spreads.json 里没有价差）")
            item.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
            self.start_button.setEnabled(False)
            return
        for name in sorted(self._spreads):
            config = self._spreads[name]
            label = config.display_name or config.exchange_spread or name
            remark = f"  ·  {config.remark}" if getattr(config, "remark", "") else ""
            item = QtWidgets.QListWidgetItem(f"{label}{remark}    [{name}]")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked)
            self.list.addItem(item)
        self.progress.setMaximum(max(1, self.list.count()))

    # ------------------------------------------------------------ 交互
    def _choose_dir(self) -> None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择导出目录", self.out_edit.text() or str(Path.cwd()))
        if chosen:
            self.out_edit.setText(chosen)

    def _set_all(self, checked: bool) -> None:
        state = (QtCore.Qt.CheckState.Checked if checked
                 else QtCore.Qt.CheckState.Unchecked)
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(state)

    def selected_names(self) -> list[str]:
        names = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                names.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
        return names

    def _start(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        names = self.selected_names()
        if not names:
            self.progress_label.setText("没有勾选任何价差")
            return
        day = self.date_edit.date().toPython()
        self.result.clear()
        self.progress.setMaximum(len(names))
        self.progress.setValue(0)
        self.progress_label.setText(f"正在导出 {len(names)} 个价差…")
        self._set_running(True)

        self._worker = ExportWorker(day, names, self.out_edit.text().strip(), parent=self)
        self._worker.progressed.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        # 线程跑完再销毁对象，避免"QThread: Destroyed while thread is still running"
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.close_button.setEnabled(not running)
        self.list.setEnabled(not running)
        self.date_edit.setEnabled(not running)
        self.out_edit.setEnabled(not running)

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        self.progress_label.setText(f"[{done}/{total}] 正在导出 {name} …")

    def _on_done(self, report) -> None:
        self._set_running(False)
        self.progress.setValue(self.progress.maximum())
        self.progress_label.setText(
            f"完成：{len(report.exported)}/{len(report.items)} 个价差有数据，"
            f"合计 {report.total_rows} 行")
        self.result.setPlainText(report.describe())

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        self.progress_label.setText("导出失败")
        self.result.setPlainText(f"导出失败：{message}")

    def closeEvent(self, event) -> None:          # noqa: N802 - Qt 命名
        if self._worker is not None and self._worker.isRunning():
            self.progress_label.setText("导出进行中，完成后再关闭（不会卡住主界面）")
            event.ignore()
            return
        super().closeEvent(event)
