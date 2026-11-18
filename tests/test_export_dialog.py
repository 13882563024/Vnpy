"""导出对话框：勾选、全选/全不选、后台线程不阻塞、进度与结果回填。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.trader.constant import Direction, Offset  # noqa: E402

from recorder.trade_recorder import TradeRecorder  # noqa: E402
from tests.test_trade_recorder import make_order, make_trade  # noqa: E402
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


class TestExportDialogWorkerLifetime(unittest.TestCase):
    """回归：worker 的 C++ 对象被 deleteLater 销毁后，Python 引用还在。

    2026-11-13 报错：``RuntimeError: Internal C++ object (ExportWorker) already deleted``
    —— 第二次点「开始导出」时 ``self._worker.isRunning()`` 访问了已销毁的对象。
    """

    def setUp(self) -> None:
        from vnpy.trader.ui import QtWidgets

        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from tickbrowser.ui.export_dialog import SpreadExportDialog

        self.dialog = SpreadExportDialog(None, spreads=SPREADS,
                                         default_day=date(2026, 11, 12))

    def tearDown(self) -> None:
        self.dialog.close()

    @staticmethod
    def _deleted_thread():
        from shiboken6 import delete
        from vnpy.trader.ui import QtCore

        worker = QtCore.QThread()
        delete(worker)          # 模拟 deleteLater 已生效
        return worker

    def test_deleted_worker_counts_as_not_running(self) -> None:
        self.dialog._worker = self._deleted_thread()

        self.assertFalse(self.dialog._worker_is_running())
        self.assertIsNone(self.dialog._worker, "死引用应被清掉")

    def test_start_after_deleted_worker_does_not_raise(self) -> None:
        """死引用既不能让「开始导出」崩溃，也不能把新一轮导出挡掉。"""
        class FakeSignal:
            def connect(self, *_args, **_kwargs):
                pass

        class FakeWorker:
            def __init__(self, *_args, **_kwargs):
                self.progressed = FakeSignal()
                self.done = FakeSignal()
                self.failed = FakeSignal()
                self.finished = FakeSignal()
                self.started = False

            def start(self):
                self.started = True

            def isRunning(self):
                return False

            def deleteLater(self):
                pass

        created: list = []

        def factory(*_args, **_kwargs):
            worker = FakeWorker()
            created.append(worker)
            return worker

        self.dialog._worker = self._deleted_thread()
        with patch("tickbrowser.ui.export_dialog.ExportWorker", factory):
            self.dialog._start()            # 不应抛 RuntimeError

        self.assertEqual(len(created), 1, "死引用不应挡住新一轮导出")
        self.assertTrue(created[0].started)

    def test_close_after_deleted_worker_does_not_raise(self) -> None:
        self.dialog._worker = self._deleted_thread()

        self.dialog.close()

    def test_on_worker_finished_clears_reference(self) -> None:
        from vnpy.trader.ui import QtCore

        worker = QtCore.QThread()
        self.dialog._worker = worker

        self.dialog._on_worker_finished()

        self.assertIsNone(self.dialog._worker)


class TestTradeExportDialog(unittest.TestCase):
    """交易记录导出对话框：选日期 → 汇总成 CSV，无记录时给明确提示。"""

    def setUp(self) -> None:
        from vnpy.trader.ui import QtWidgets

        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.records = base / "records"
        self.out = base / "exports"
        recorder = TradeRecorder(root=self.records, batch_size=1000,
                                 spreads={"A_2701_2703": ("a2701.DCE", "a2703.DCE")})
        recorder.on_order(make_order("1"))
        recorder.on_trade(make_trade("T1", symbol="a2701", orderid="9",
                                     direction=Direction.LONG,
                                     offset=Offset.OPEN, price=5210.0))
        recorder.on_trade(make_trade("T2", symbol="a2703", orderid="9",
                                     direction=Direction.SHORT,
                                     offset=Offset.OPEN, price=5224.0))
        recorder.flush()

        from tickbrowser.ui.trade_export_dialog import TradeExportDialog

        self.dialog = TradeExportDialog(None, record_root=self.records,
                                        out_root=self.out)

    def tearDown(self) -> None:
        self.dialog.close()
        self.temp.cleanup()

    def test_overview_lists_recorded_days(self) -> None:
        self.assertIn("2026-11-12", self.dialog.overview.toPlainText())

    def test_export_writes_three_files(self) -> None:
        from vnpy.trader.ui import QtCore

        moment = QtCore.QDate(2026, 11, 12)
        self.dialog.start_edit.setDate(moment)
        self.dialog.end_edit.setDate(moment)

        self.dialog._export()

        text = self.dialog.result.toPlainText()
        self.assertIn("导出完成", text)
        self.assertTrue((self.out / "2026-11-12" / "orders.csv").exists())
        self.assertTrue((self.out / "2026-11-12" / "trades.csv").exists())
        self.assertTrue((self.out / "2026-11-12" / "spread_trades.csv").exists())

    def test_export_without_records_reports_clearly(self) -> None:
        from vnpy.trader.ui import QtCore

        moment = QtCore.QDate(2026, 12, 31)       # 没有记录的日子
        self.dialog.start_edit.setDate(moment)
        self.dialog.end_edit.setDate(moment)

        self.dialog._export()

        self.assertIn("没有匹配", self.dialog.result.toPlainText())

    def test_reversed_range_is_rejected(self) -> None:
        from vnpy.trader.ui import QtCore

        self.dialog.start_edit.setDate(QtCore.QDate(2026, 11, 13))
        self.dialog.end_edit.setDate(QtCore.QDate(2026, 11, 12))

        self.dialog._export()

        self.assertIn("早于", self.dialog.result.toPlainText())


if __name__ == "__main__":
    unittest.main()
