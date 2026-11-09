"""导出对话框：勾选、全选/全不选、后台线程不阻塞、进度与结果回填。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.spread_config import SpreadConfig  # noqa: E402

A = SpreadConfig(name="A_2701_2703", display_name="DCE.SP a2701&a2703",
                 legs=["a2701.DCE", "a2703.DCE"], size=10, pricetick=1)
V = SpreadConfig(name="V_2701_2703", display_name="DCE.SP v2701&v2703",
                 remark="PVC 跨期", legs=["v2701.DCE", "v2703.DCE"], size=5, pricetick=1)
SPREADS = {A.name: A, V.name: V}


def make_report(day: date, rows: int = 2):
    from utils.spread_export import ExportReport, SpreadExport

    report = ExportReport(day=day, out_dir=Path("exports") / day.isoformat(),
                          start=day, end=day)
    report.items.append(SpreadExport(name=A.name, rows=rows, source="stored"))
    return report


class TestExportWorker(unittest.TestCase):
    def test_worker_reports_progress_and_result(self) -> None:
        from tickbrowser.ui.export_dialog import ExportWorker

        seen: list[tuple] = []
        done: list = []
        failed: list = []

        def fake_export(day, out_dir=None, **kwargs):
            kwargs["progress"](1, 2, A.name)
            kwargs["progress"](2, 2, V.name)
            return make_report(day)

        worker = ExportWorker(date(2026, 11, 12), [A.name, V.name], "exports")
        worker.progressed.connect(lambda done_i, total, name: seen.append((done_i, total, name)))
        worker.done.connect(done.append)
        worker.failed.connect(failed.append)

        with patch("utils.spread_export.export_spread_day", fake_export):
            worker.run()          # 直接跑，不等线程调度

        self.assertEqual(seen, [(1, 2, A.name), (2, 2, V.name)])
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].total_rows, 2)
        self.assertEqual(failed, [])

    def test_worker_reports_failure_instead_of_crashing(self) -> None:
        from tickbrowser.ui.export_dialog import ExportWorker

        failed: list = []

        def boom(*_args, **_kwargs):
            raise RuntimeError("数据库被占用")

        worker = ExportWorker(date(2026, 11, 12), None, "exports")
        worker.failed.connect(failed.append)
        with patch("utils.spread_export.export_spread_day", boom):
            worker.run()

        self.assertEqual(len(failed), 1)
        self.assertIn("数据库被占用", failed[0])


@unittest.skipUnless(os.environ.get("QT_QPA_PLATFORM") == "offscreen", "需显式 offscreen Qt")
class TestExportDialog(unittest.TestCase):
    def setUp(self) -> None:
        from vnpy.trader.ui import QtWidgets

        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from tickbrowser.ui.export_dialog import SpreadExportDialog

        self.dialog = SpreadExportDialog(None, spreads=SPREADS, default_day=date(2026, 11, 12))

    def tearDown(self) -> None:
        self.dialog.close()

    def test_all_spreads_checked_by_default(self) -> None:
        self.assertEqual(self.dialog.list.count(), 2)
        self.assertEqual(sorted(self.dialog.selected_names()), sorted(SPREADS))

    def test_select_all_and_none(self) -> None:
        self.dialog._set_all(False)
        self.assertEqual(self.dialog.selected_names(), [])
        self.dialog._set_all(True)
        self.assertEqual(len(self.dialog.selected_names()), 2)

    def test_uncheck_single_spread(self) -> None:
        from vnpy.trader.ui import QtCore

        item = self.dialog.list.item(0)          # 排序后 A 在前
        item.setCheckState(QtCore.Qt.CheckState.Unchecked)
        self.assertEqual(self.dialog.selected_names(), [V.name])

    def test_start_runs_worker_without_blocking_dialog(self) -> None:
        calls: list = []

        class FakeSignal:
            def connect(self, *_args, **_kwargs):
                pass

        class FakeWorker:
            def __init__(self, day, names, out_dir, parent=None):
                calls.append((day, names, out_dir))
                self.progressed = FakeSignal()
                self.done = FakeSignal()
                self.failed = FakeSignal()
                self.finished = FakeSignal()

            def isRunning(self):
                return False

            def start(self):
                calls.append("started")

            def deleteLater(self):
                pass

        with patch("tickbrowser.ui.export_dialog.ExportWorker", FakeWorker):
            self.dialog._start()

        self.assertEqual(calls[0][0], date(2026, 11, 12))
        self.assertEqual(sorted(calls[0][1]), sorted(SPREADS))
        self.assertEqual(calls[-1], "started")
        self.assertFalse(self.dialog.start_button.isEnabled(), "导出中不允许重复点")
        self.assertIn("正在导出", self.dialog.progress_label.text())

    def test_no_selection_does_not_start(self) -> None:
        self.dialog._set_all(False)
        with patch("tickbrowser.ui.export_dialog.ExportWorker") as factory:
            self.dialog._start()
        factory.assert_not_called()
        self.assertIn("没有勾选", self.dialog.progress_label.text())


if __name__ == "__main__":
    unittest.main()
