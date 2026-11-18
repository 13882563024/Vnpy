"""监控面板「时间」列加日期的补丁：格式、毫秒与时区都不能丢。"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.trader.ui import QtWidgets  # noqa: E402

from utils.ui_patch import TIME_CELL_FORMAT, patch_time_cell_with_date  # noqa: E402


@unittest.skipUnless(os.environ.get("QT_QPA_PLATFORM") == "offscreen", "需显式 offscreen Qt")
class TestTimeCellPatch(unittest.TestCase):
    def setUp(self) -> None:
        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.assertTrue(patch_time_cell_with_date())

    def _cell_text(self, moment: datetime) -> str:
        from vnpy.trader.ui.widget import TimeCell

        return TimeCell(moment, None)._text

    def test_time_cell_includes_date(self) -> None:
        text = self._cell_text(datetime(2026, 11, 17, 10, 30, 15, 123000))
        self.assertIn("11-17", text, "日期必须出现")
        self.assertIn("10:30:15", text)
        self.assertTrue(text.endswith(".123"), f"毫秒丢失：{text}")

    def test_night_and_day_sessions_are_distinguishable(self) -> None:
        """夜盘 22:00 与次日日盘 10:00 改前都是不同时间、改后能看出跨日。"""
        night = self._cell_text(datetime(2026, 11, 16, 22, 0, 0))
        day = self._cell_text(datetime(2026, 11, 17, 10, 0, 0))
        self.assertTrue(night.startswith("11-16"), night)
        self.assertTrue(day.startswith("11-17"), day)

    def test_zero_millisecond_pads_to_three_digits(self) -> None:
        text = self._cell_text(datetime(2026, 11, 17, 9, 0, 0, 0))
        self.assertTrue(text.endswith(".000"), text)

    def test_patch_is_idempotent(self) -> None:
        self.assertTrue(patch_time_cell_with_date())
        text = self._cell_text(datetime(2026, 11, 17, 10, 30, 15))
        self.assertIn("11-17", text)

    def test_format_constant_is_date_plus_time(self) -> None:
        self.assertIn("%m", TIME_CELL_FORMAT)
        self.assertIn("%H", TIME_CELL_FORMAT)


if __name__ == "__main__":
    unittest.main()
