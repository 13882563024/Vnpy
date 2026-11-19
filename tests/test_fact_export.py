from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from tools.export_fact_snapshot import export_snapshot
from trading_core.journal import Journal


class ExportTests(unittest.TestCase):
    def test_live_wal_commits_captured_without_overwriting(self):
        with TemporaryDirectory() as folder:
            src=Path(folder)/'facts.db'; out=src.with_name('snapshot.db'); j=Journal(src)
            with closing(sqlite3.connect(src)) as reader:
                reader.execute('BEGIN'); reader.execute('SELECT count(*) FROM facts').fetchone()
                j.append('a','d','fill',('id',),{'lots':1})
                self.assertGreater(Path(str(src)+'-wal').stat().st_size,0)
                export_snapshot(src,out)
                with self.assertRaises(FileExistsError): export_snapshot(src,out)
                self.assertFalse(Path(str(out)+'-wal').exists())
                self.assertEqual(list(Journal.open_readonly(out).replay())[0].payload,{'lots':1})
            with self.assertRaises(FileExistsError): export_snapshot(src,src)
