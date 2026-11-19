"""A publisher is simulated only inside private test directories; no gateway or network."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from workbench.follow import PaperFollow, Sources
from trading_core.journal import Journal

ACC = '["paper","demo"]'
DAY = '20261119'


class PublishedPaper(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.now = 1000.
        self.f = Journal(self.root / 'facts.sqlite')
        self.s = Journal(self.root / 'strategy.sqlite')
        self.f.append(ACC, DAY, 'paper_session', ('PAPER',), {'mode':'paper','synthetic':True})
        self.follow = PaperFollow(self.root, clock=lambda: self.now)

    def manifest(self, gen=1, **fields):
        data = dict(version=1, generation=gen, account=ACC, day=DAY, state='running', published_at=self.now)
        data.update(fields)
        path = self.root / 'temp.json'; path.write_text(json.dumps(data), encoding='utf8')
        path.replace(self.root / 'workbench.json')

    def publish(self, gen=1, **fields):
        for journal in (self.f, self.s):
            path = self.root / f'{journal.path.stem}-{gen}.sqlite'
            with closing(sqlite3.connect(journal.path)) as src, closing(sqlite3.connect(path)) as dst:
                src.backup(dst); dst.execute('PRAGMA journal_mode=DELETE')
        self.manifest(gen, **fields)

    def fingerprint(self):
        return {p.name:(p.stat().st_mtime_ns,hashlib.sha256(p.read_bytes()).digest()) for p in self.root.iterdir()}

    def test_delta_skip_dedup_and_no_writes(self):
        a = self.follow
        self.assertTrue(a.poll()); self.assertIsNone(a.publication)
        self.publish(); before = self.fingerprint(); self.assertTrue(a.poll()); self.assertFalse(a.poll())
        self.assertEqual(self.fingerprint(), before); self.assertEqual([x[3] for x in a.tail], ['首次载入'])
        self.s.append(ACC,DAY,'layer_event',('a',),{})
        self.f.append(ACC,DAY,'fill',('f',),{}); self.f.append(ACC,DAY,'fill',('f',),{})
        self.publish(4); before = self.fingerprint(); a.poll(); a.poll()
        self.assertEqual(self.fingerprint(), before); self.assertEqual([len(f.events) for f in a.snapshot.feeds], [3,1]); self.assertEqual(len(a.tail),4)
        self.assertEqual(a.tail[-1][3], '增量'); self.assertEqual(a.snapshot.feeds[0].events[-1].outcome, 'duplicate')

    def test_time_stale_recover_finished_and_regression(self):
        a = self.follow; self.publish(); a.poll(); self.assertIn('更新中',a.status)
        self.now += 16; self.assertTrue(a.poll()); self.assertIn('过期',a.status)
        self.publish(2); a.poll(); self.assertIn('更新中',a.status)
        self.manifest(1); a.poll(); self.assertIn('过期',a.status); self.assertEqual(a.publication.generation,2)
        self.publish(3, state='finished'); a.poll(); self.now += 100; a.poll(); self.assertIn('已结束 · 历史',a.status)
        self.publish(4); a.poll(); self.assertEqual(a.publication.generation,3)

    def test_do_not_open_live_db_or_wal_and_do_not_mix_pairs(self):
        a = self.follow; self.publish(); a.poll(); accepted = a.snapshot
        self.f.append(ACC,DAY,'fill',('f',),{}); self.publish(2)
        (self.root/'strategy-2.sqlite-shm').touch(); a.poll(); self.assertIs(a.snapshot, accepted); self.assertIn('过期', a.status)
        self.manifest(3); before = self.fingerprint()
        with patch('sqlite3.connect', side_effect=AssertionError):
            a.poll()  # missing closed pair: never fall back to active facts.sqlite
        self.assertEqual(before,self.fingerprint()); self.assertIs(a.snapshot,accepted)
        shutil.copyfile(self.f.path,self.root/'facts-3.sqlite')  # WAL-mode header even when last writer closed
        with patch('sqlite3.connect', side_effect=AssertionError):
            a.poll()
        self.assertIs(a.snapshot,accepted)

    def test_actual_publisher_contract_and_failure_terminal(self):
        from trading_core.paper_publish import PaperPublisher
        pub = PaperPublisher(self.root, ACC)
        a = PaperFollow(self.root)
        pub.publish(DAY,'running'); a.poll(); self.assertIn('更新中',a.status)
        pub.publish(DAY,'failed'); a.poll(); self.assertIn('运行失败 · 历史',a.status)
        self.assertEqual(a.publication.generation,2)

    def test_static_mode_refuses_even_empty_wal_before_immutable(self):
        from workbench.model import load_snapshot
        Path(str(self.f.path)+'-wal').touch()
        with patch('sqlite3.connect', side_effect=AssertionError):
            data = load_snapshot(Sources(facts=self.f.path))
        self.assertIn('读取失败',data.feeds[0].status)

    def test_reject_nonpaper_mixed_marker_and_shadow(self):
        self.publish(); a = self.follow
        with closing(sqlite3.connect(self.root/'facts-1.sqlite')) as db, db:
            db.execute("UPDATE facts SET payload='{}'")
        a.poll(); self.assertIsNone(a.publication)
        self.s.append(ACC,DAY,'fill',('real',),{}); self.publish(2); a.poll(); self.assertIsNone(a.publication)
        self.f.append('other',DAY,'order',('foreign',),{}); self.publish(3); a.poll(); self.assertIsNone(a.publication)
        self.assertFalse(a.tail)

    def test_reject_filters_external_inputs_and_unsealed_alias(self):
        self.publish()
        a = PaperFollow(self.root, Sources(day='20261001'),clock=lambda:self.now); a.poll(); self.assertIsNone(a.publication)
        with self.assertRaises(ValueError): PaperFollow(self.root,Sources(facts=self.f.path))
        self.manifest(2); os.link(self.root/'facts-1.sqlite',self.root/'facts-2.sqlite'); self.follow.poll(); self.assertIsNone(self.follow.publication)

    def test_qt_timer_live_log_readonly_and_historical_money(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        from workbench.window import Window
        app = QApplication.instance() or QApplication([])
        self.publish(); w = Window(self.follow.snapshot, follow=self.follow); self.addCleanup(w.close)
        w.show(); app.processEvents(); self.assertTrue(w.timer.isActive()); self.assertFalse(w.draft_button.isEnabled())
        self.f.append(ACC,DAY,'account_snapshot',('a',),{'available':15}); self.publish(2)
        w.timer.timeout.emit(); self.assertIn('增量',w.pages[3].toPlainText()); self.assertIn('历史可用',w.pages[2].toPlainText())
        self.now += 16; w.timer.timeout.emit(); self.assertIn('过期',w.sub.text()); self.assertIn('account_snapshot',w.pages[3].toPlainText())
        w.close(); self.assertFalse(w.timer.isActive())


if __name__ == '__main__':
    unittest.main()
