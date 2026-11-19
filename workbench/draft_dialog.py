"""Offline parameter worksheet. Only an explicit, reviewed export can write."""
import json
import math
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHeaderView, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QTextEdit, QVBoxLayout)
from trading_core import config_draft


LABELS = {
    'entry_k': '入场标准差倍数', 'layer_step': '加层步长', 'max_positions': '最大层数',
    'layer_volume': '每层手数', 'mean_window_minutes': '均值窗口（分钟）',
    'sd_window_minutes': '标准差窗口（分钟）', 'min_exit_pnl_override': '最低平仓收益覆盖值',
    'allow_stop_loss': '允许止损', 'max_hold_days': '最长持仓天数', 'close_only': '仅平仓',
    'exit_preplace': '预挂平仓单', 'width_ticks': '盘口宽度（跳）', 'min_depth': '最小深度',
    'sync_seconds': '同步时限（秒）', 'vr_max': '量比上限',
    'roll_entry_cutoff_days': '移仓前停止开仓天数', 'roll_exit_days': '移仓平仓天数',
}


def display(value):
    if type(value) is bool:
        return '是' if value else '否'
    return str(value)


class DraftDialog(QDialog):
    def __init__(self, snapshot, spread, parent=None):
        super().__init__(parent)
        self.snapshot, self.spread, self.draft = snapshot, spread, None
        self.params = snapshot.spreads[spread]['strategy']
        self.editors = {}
        self.setWindowTitle('编辑参数草案'); self.resize(680, 540)
        layout = QVBoxLayout(self); layout.setContentsMargins(18, 16, 18, 16)
        title = QLabel(f'{spread} · 参数草案'); title.setTextFormat(Qt.TextFormat.PlainText)
        title.setObjectName('draftTitle'); layout.addWidget(title)
        layout.addWidget(QLabel('修改仅留在本窗口。导出不会覆盖源配置，也不会改变实盘许可。'))
        keys = [k for k in LABELS if k in config_draft.EDITABLE and type(self.params.get(k)) in (bool, int, float)]
        grid = QTableWidget(len(keys), 3); grid.setHorizontalHeaderLabels(['策略参数', '源配置', '草案值'])
        grid.verticalHeader().hide(); grid.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        grid.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        grid.setAccessibleName('策略参数草案'); layout.addWidget(grid, 3)
        for i, key in enumerate(keys):
            old = self.params[key]; label = QTableWidgetItem(LABELS[key]); label.setToolTip(key)
            grid.setItem(i, 0, label); grid.setItem(i, 1, QTableWidgetItem(display(old)))
            if type(old) is bool:
                editor = QComboBox(); editor.addItem('否', False); editor.addItem('是', True)
                editor.setCurrentIndex(int(old)); editor.currentIndexChanged.connect(self.invalidate)
            else:
                editor = QLineEdit(str(old)); editor.textChanged.connect(self.invalidate)
            grid.setRowHeight(i, 36); editor.setAccessibleName(f'{LABELS[key]} 草案值'); grid.setCellWidget(i, 2, editor)
            self.editors[key] = editor
        self.report = QTextEdit(); self.report.setReadOnly(True); self.report.setAccessibleName('草案预览与结果')
        layout.addWidget(self.report, 1)
        bar = QHBoxLayout(); layout.addLayout(bar); bar.addStretch()
        self.preview_button = QPushButton('预览变更'); self.export_button = QPushButton('导出新文件…'); close = QPushButton('关闭')
        for b in (self.preview_button, self.export_button, close):
            b.setAutoDefault(False); bar.addWidget(b)
        self.preview_button.clicked.connect(self.preview); self.export_button.clicked.connect(self.choose_target)
        close.clicked.connect(self.reject); self.invalidate()

    def invalidate(self, *_):
        self.draft = None; self.export_button.setEnabled(False)
        self.report.setPlainText('编辑草案值后，点击“预览变更”核对差异。')

    def preview(self):
        self.invalidate(); updates = {}
        try:
            for k, editor in self.editors.items():
                if isinstance(editor, QComboBox):
                    value = editor.currentData()
                else:
                    try:
                        value = json.loads(editor.text().strip())
                        if type(value) not in (int, float) or not math.isfinite(value):
                            raise ValueError
                    except (ValueError, OverflowError, RecursionError):
                        editor.setFocus(); raise ValueError(f'{LABELS[k]}：请输入有限数值。') from None
                if value != self.params[k]:
                    updates[k] = value
            if not updates:
                raise ValueError('没有参数变化。请先修改草案值。')
            draft = config_draft.preview(self.snapshot.sources.config, self.spread, updates)
            if draft.before_hash != self.snapshot.config_hash:
                raise ValueError('源配置已变化。请关闭草案，重新读取后再编辑。')
        except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError) as exc:
            self.report.setPlainText(f'未生成预览：{exc}'); return
        self.draft = draft
        self.report.setPlainText('\n'.join([f'待导出 {len(draft.changes)} 项变更（源配置 → 草案值）',
            *(f'{LABELS[k]}：{display(a)} → {display(b)}' for k, a, b in draft.changes)]))
        self.export_button.setEnabled(True)

    def choose_target(self):
        if self.draft is None:
            return
        source = Path(self.snapshot.sources.config)
        target, _ = QFileDialog.getSaveFileName(self, '导出为新的草案文件',
            str(source.with_name(source.stem + '.draft.json')), '参数草案 (*.draft.json)',
            options=QFileDialog.Option.DontConfirmOverwrite)
        if not target:
            return
        try:
            dest = Path(target)
            reserved = (getattr(self.snapshot.sources, k) for k in ('config', 'credentials', 'facts', 'shadow'))
            if not dest.name.lower().endswith('.draft.json') or any(p and dest.resolve() == Path(p).resolve() for p in reserved):
                raise ValueError('请选择未使用的 *.draft.json 文件名，不能使用输入文件。')
            config_draft.export(source, dest, self.draft)
        except FileExistsError:
            self.report.append('文件已存在。请选择新文件名，不会覆盖已有文件。'); return
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError, RuntimeError) as exc:
            self.invalidate(); self.report.setPlainText(f'未导出：{exc}\n请核对参数和源配置，再重新预览。'); return
        self.invalidate(); self.report.setPlainText(f'已导出：{dest}\n源配置未变更。')
