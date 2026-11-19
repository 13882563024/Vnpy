"""一个共享运行目录内，每账户仅一个策略写入进程。锁由 OS/SQLite 随进程退出释放。"""
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import RLock


class AccountWriter:
    def __init__(self, directory: Path, account: str):
        if not isinstance(account,str) or not account.strip():
            raise ValueError('缺少账户标识')
        self.account = account
        self.path = Path(directory) / ('ctp-writer-'+sha256(account.encode()).hexdigest()+'.lock.sqlite')
        self._db = None
        self._lock = RLock()

    def acquire(self):
        with self._lock:
            if self._db is not None:
                return
            db = sqlite3.connect(self.path,timeout=0,check_same_thread=False,isolation_level=None)
            try:
                # 不触碰事实库，不删除锁文件。独占事务生命周期等同写入者生命周期。
                db.execute('BEGIN EXCLUSIVE')
            except Exception:
                db.close()
                raise
            self._db = db

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
