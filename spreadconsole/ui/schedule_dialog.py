"""定时代理的配置对话框：编辑 ``config/schedule.json``。

只做配置编辑与校验，不直接执行任务；保存后由 ``ScheduleAgent.reload()`` 生效。
"""

from __future__ import annotations

from datetime import datetime

from PySide6 import QtWidgets

from vnpy.trader.ui import QtCore

from utils.schedule_plan import (
    ACTION_LABELS,
    ACTIONS,
    SchedulePlan,
    ScheduleTask,
    format_days,
    format_time,
    parse_days,
    parse_time,
)

COLUMNS = ("启用", "时间", "周几(1-7)", "动作", "备注")


class ScheduleDialog(QtWidgets.QDialog):
    """表格化编辑：启用 / 时间 / 周几 / 动作 / 备注。"""

    def __init__(self, plan: SchedulePlan, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("定时代理 · 自动连接 / 核对 / 启停交易")
        self.resize(720, 420)
        self.plan = SchedulePlan(tasks=list(plan.tasks), enabled=plan.enabled)

        self.enabled_box = QtWidgets.QCheckBox("启用定时代理")
        self.enabled_box.setChecked(bool(plan.enabled))
        self.next_label = QtWidgets.QLabel("")

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.enabled_box)
        top.addStretch()
        top.addWidget(self.next_label)

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)

        add_button = QtWidgets.QPushButton("添加")
        add_button.clicked.connect(lambda: self._add_row(None))
        remove_button = QtWidgets.QPushButton("删除所选")
        remove_button.clicked.connect(self._remove_selected)
        default_button = QtWidgets.QPushButton("恢复默认")
        default_button.clicked.connect(self._restore_default)

        row_buttons = QtWidgets.QHBoxLayout()
        row_buttons.addWidget(add_button)
        row_buttons.addWidget(remove_button)
        row_buttons.addWidget(default_button)
        row_buttons.addStretch()

        save_button = QtWidgets.QPushButton("保存")
        save_button.setDefault(True)
        save_button.clicked.connect(self._save)
        cancel_button = QtWidgets.QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(save_button)
        bottom.addWidget(cancel_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(QtWidgets.QLabel(
            "周几用 1=周一 … 7=周日；支持 1-5 或 1,3,5。时间格式 HH:MM。"
            "同一任务当天只执行一次；策略运行中不会重复开始/停止。"))
        layout.addWidget(self.table)
        layout.addLayout(row_buttons)
        layout.addLayout(bottom)

        for task in self.plan.tasks:
            self._add_row(task)
        self.enabled_box.toggled.connect(self._refresh_next)
        self._refresh_next()

    # ------------------------------------------------------------ 行操作
    def _add_row(self, task: ScheduleTask | None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        enabled = QtWidgets.QCheckBox()
        enabled.setChecked(True if task is None else bool(task.enabled))
        holder = QtWidgets.QWidget()
        box = QtWidgets.QHBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(enabled)
        box.setAlignment(enabled, QtCore.Qt.AlignmentFlag.AlignCenter)
        self.table.setCellWidget(row, 0, holder)

        time_item = QtWidgets.QTableWidgetItem(task.time if task else "08:50")
        self.table.setItem(row, 1, time_item)

        days_item = QtWidgets.QTableWidgetItem(
            format_days(task.days) if task else "1-5")
        self.table.setItem(row, 2, days_item)

        combo = QtWidgets.QComboBox()
        for action in ACTIONS:
            combo.addItem(ACTION_LABELS.get(action, action), action)
        if task is not None:
            index = combo.findData(task.action)
            if index >= 0:
                combo.setCurrentIndex(index)
        self.table.setCellWidget(row, 3, combo)

        note_item = QtWidgets.QTableWidgetItem(task.note if task else "")
        self.table.setItem(row, 4, note_item)

    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount():
            rows = [self.table.rowCount() - 1]
        for row in rows:
            self.table.removeRow(row)

    def _restore_default(self) -> None:
        if QtWidgets.QMessageBox.question(
                self, "恢复默认",
                "用默认计划替换当前表格？\n\n周一到周五 08:50 连接 → 08:55 核对并纠正 → 08:58 开始交易",
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.table.setRowCount(0)
        for task in SchedulePlan.default_tasks():
            self._add_row(task)

    # ------------------------------------------------------------ 读写
    def _collect(self) -> SchedulePlan:
        tasks: list[ScheduleTask] = []
        problems: list[str] = []
        for row in range(self.table.rowCount()):
            holder = self.table.cellWidget(row, 0)
            enabled = True
            if holder is not None:
                box = holder.findChild(QtWidgets.QCheckBox)
                enabled = bool(box and box.isChecked())
            time_item = self.table.item(row, 1)
            days_item = self.table.item(row, 2)
            combo = self.table.cellWidget(row, 3)
            note_item = self.table.item(row, 4)

            raw_time = time_item.text() if time_item else ""
            minute = parse_time(raw_time)
            if minute is None:
                problems.append(f"第 {row + 1} 行时间格式非法：{raw_time!r}（应为 HH:MM）")
                continue
            days = parse_days(days_item.text() if days_item else "")
            if days is None:
                problems.append(f"第 {row + 1} 行周几非法："
                                f"{(days_item.text() if days_item else '')!r}（1-7，可写 1-5 或 1,3,5）")
                continue
            action = combo.currentData() if isinstance(combo, QtWidgets.QComboBox) else None
            if action not in ACTIONS:
                problems.append(f"第 {row + 1} 行动作非法")
                continue
            tasks.append(ScheduleTask(
                time=format_time(minute),
                days=days,
                action=str(action),
                enabled=enabled,
                note=(note_item.text() if note_item else "").strip(),
            ))
        if problems:
            raise ValueError("\n".join(problems))
        if not tasks:
            raise ValueError("至少要有一条定时任务")
        return SchedulePlan(tasks=tasks, enabled=self.enabled_box.isChecked())

    def _refresh_next(self) -> None:
        preview = SchedulePlan(tasks=list(self.plan.tasks),
                               enabled=self.enabled_box.isChecked())
        moment = preview.next_at(datetime.now())
        if moment is None:
            self.next_label.setText("当前计划不会触发任何任务")
        else:
            when, task = moment
            self.next_label.setText(f"下次：{when:%m-%d %H:%M} {task.describe()}")

    def _save(self) -> None:
        try:
            plan = self._collect()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "配置有误", str(exc))
            return
        self.plan = plan
        self.accept()

    # ------------------------------------------------------------ 供调用方取用
    def result_plan(self) -> SchedulePlan:
        return self.plan


def edit_schedule(plan: SchedulePlan, parent=None) -> SchedulePlan | None:
    """打开对话框；用户取消返回 ``None``。"""
    dialog = ScheduleDialog(plan, parent)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return dialog.result_plan()
