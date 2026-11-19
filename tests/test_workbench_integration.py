"""Offline entry and draft safety, using real Qt and real exclusive file export."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow
from workbench.model import DEFAULT_CONFIG, Sources, load_snapshot
from workbench.draft_dialog import DraftDialog
from workbench.terminal import install_workbench_menu
from workbench.window import Window


class OfflineIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cli_routes_before_import_of_any_trading_module(self):
        script = """
import sys
class Guard:
    def find_spec(self, name, *args):
        if name.startswith(('vnpy','gateway','spreadconsole','utils.engine_setup')):
            raise AssertionError(name)
sys.meta_path.insert(0, Guard())
import main
main.main(['--workbench','--help'])
"""
        p = subprocess.run([sys.executable, '-B', '-c', script], cwd=DEFAULT_CONFIG.parents[1], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr); self.assertIn(b'--facts', p.stdout)

    def test_menu_opens_one_window_without_engine_or_account(self):
        host = QMainWindow(); self.addCleanup(host.close)
        action = install_workbench_menu(host); action.trigger(); action.trigger()
        windows = host.findChildren(Window); self.assertEqual(len(windows), 1)
        w = windows[0]; self.assertIsNone(w.snap.sources.credentials); self.assertIsNone(w.snap.sources.account)
        self.assertTrue(w.draft_button.isEnabled()); w.search.setText('not-a-spread'); self.assertFalse(w.draft_button.isEnabled())

    def setUp(self):
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root = Path(tmp.name)
        self.source = self.root/'source.json'; self.raw = DEFAULT_CONFIG.read_bytes(); self.source.write_bytes(self.raw)
        self.key = next(iter(json.loads(self.raw))); self.target = self.root/'out.draft.json'
        self.dialog = DraftDialog(load_snapshot(Sources(config=self.source, credentials=self.root/'reserved.draft.json')), self.key)
        self.addCleanup(self.dialog.close)

    def choose(self, name):
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(name), '')):
            self.dialog.choose_target()

    def test_paper_marker_labels_filtered_facts_even_on_another_day(self):
        from trading_core.journal import Journal
        path = self.root/'facts.sqlite'; journal = Journal(path)
        journal.append('sim', '20261118', 'paper_session', ('session',), {})
        journal.append('sim', '20261119', 'account_snapshot', ('round',), {'available': 10})
        w = Window(load_snapshot(Sources(facts=path, day='20261119'))); self.addCleanup(w.close)
        label = '纸面柜台（模拟，非真实账户）'; self.assertIn(label, w.scope.text())
        self.assertIn(label, w.pages[2].toPlainText()); self.assertIn(label, w.pages[3].toPlainText())
        self.assertEqual([r.kind for r in w.snap.feeds[0].events], ['account_snapshot'])

    def test_export_requires_unchanged_preview_and_new_file(self):
        d = self.dialog; editor = d.editors['max_positions']; self.assertNotIn('executor', d.editors)
        editor.setText('4'); d.preview_button.click(); self.assertTrue(d.export_button.isEnabled()); self.assertIn('3 → 4', d.report.toPlainText())
        editor.setText('5'); self.assertFalse(d.export_button.isEnabled()); self.choose(self.target); self.assertFalse(self.target.exists())
        d.preview(); self.choose(''); self.assertEqual(list(self.root.iterdir()), [self.source]); self.assertTrue(d.export_button.isEnabled())
        self.choose(self.root/'reserved.draft.json'); self.assertFalse((self.root/'reserved.draft.json').exists()); d.preview()
        self.choose(self.target); expected = json.loads(self.raw); expected[self.key]['strategy']['max_positions'] = 5
        self.assertEqual(json.loads(self.target.read_bytes()), expected); self.assertEqual(self.source.read_bytes(), self.raw)
        data = self.target.read_bytes(); editor.setText('4'); d.preview(); self.choose(self.target)
        self.assertEqual(self.target.read_bytes(), data); self.assertIn('已存在', d.report.toPlainText())

    def test_refuse_invalid_value_and_changed_baseline(self):
        d = self.dialog; e = d.editors['max_positions']; e.setText('NaN'); d.preview(); self.assertIsNone(d.draft)
        e.setText('-2'); d.preview(); self.choose(self.target); self.assertFalse(self.target.exists()); self.assertIsNone(d.draft)
        e.setText('4'); d.preview(); self.source.write_bytes(self.raw+b' '); self.choose(self.target)
        self.assertFalse(self.target.exists()); d.preview(); self.assertIsNone(d.draft); self.assertIn('重新读取', d.report.toPlainText())
        self.assertEqual(list(self.root.iterdir()), [self.source]); self.assertEqual(self.source.read_bytes(), self.raw+b' ')
