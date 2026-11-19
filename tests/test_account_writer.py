from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from trading_core.writer_guard import AccountWriter


class WriterTests(unittest.TestCase):
    def test_two_clients_share_one_account_lock_but_not_other_accounts(self):
        with TemporaryDirectory() as folder:
            a,b,c=(AccountWriter(Path(folder),x) for x in ('broker/a','broker/a','broker/b'))
            try:
                a.acquire(); a.acquire(); c.acquire()
                with self.assertRaises(sqlite3.OperationalError): b.acquire()
                a.close(); b.acquire()
                with self.assertRaises(sqlite3.OperationalError): a.acquire()
            finally:
                a.close(); b.close(); c.close()
