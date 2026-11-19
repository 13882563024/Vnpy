"""Actual Qt rendering and controls (not mocked)."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QFontMetrics
from trading_core.journal import Journal
from workbench import Sources, load_snapshot
from workbench.window import Window


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_tabs_search_refresh_and_png(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'history.db'; j=Journal(p)
            j.append('sim','20261119','account_snapshot',('round',),{'available':0,'balance':None,'currency':'CNY'})
            w=Window(load_snapshot(Sources(facts=p))); self.addCleanup(w.close); w.show(); self.app.processEvents()
            if os.name=='nt': self.assertTrue(QFontMetrics(w.title.font()).inFontUcs4(ord('差')))
            self.assertGreater(w.list.count(),0); self.assertIn('a2701',w.pages[0].toPlainText()); self.assertIn('暂无可归属',w.pages[1].toPlainText())
            self.assertIn('历史可用',w.pages[2].toPlainText()); self.assertIn('CNY',w.pages[2].toPlainText()); self.assertIn('未知',w.pages[2].toPlainText())
            for width,height in [(1100,730),(760,520)]:
                w.resize(width,height)
                for i in range(4):
                    w.tabs.setCurrentIndex(i); self.app.processEvents(); image=w.grab(); self.assertEqual((image.width(),image.height()),(width,height)); out=Path(d)/'render.png'; self.assertTrue(image.save(str(out))); self.assertFalse(QImage(str(out)).isNull())
            w.search.setText('not-a-spread'); self.assertEqual(w.list.count(),0); self.assertIn('未找到',w.title.text()); w.search.clear(); w.refresh(); self.assertGreater(w.list.count(),0)
