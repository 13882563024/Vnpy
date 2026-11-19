"""SQLite 事实流水。业务身份由适配器提供，不以接收时间或价格推测。"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import sqlite3
from typing import Iterator


class Outcome(StrEnum):
    NEW = 'new'
    DUPLICATE = 'duplicate'
    CONFLICT = 'conflict'


@dataclass(frozen=True)
class Record:
    sequence: int
    account: str
    trading_day: str
    kind: str
    identity: tuple[str, ...]
    payload: dict
    outcome: Outcome


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':'))


class Journal:
    """每次写入独立连接与事务；重复及矛盾输入也保留，只有 NEW 可用于投影。

    调用方须对 CONFLICT 停止自动执行并等待核对。payload 是稳定的业务事实，
    不得混入本次接收时间。单个委托允许多个状态事件，不能仅以 orderid 为事件键。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        with closing(self._connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS facts(
                seq INTEGER PRIMARY KEY, account TEXT NOT NULL, day TEXT NOT NULL,
                kind TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('new','duplicate','conflict')),
                received_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS facts_canonical
                ON facts(account,day,kind,identity) WHERE outcome='new' ''')

    def _connect(self):
        if getattr(self, '_readonly', False):
            return sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def append(self, account: str, trading_day: str, kind: str,
               identity: tuple[str, ...], payload: dict) -> tuple[int, Outcome]:
        if (not isinstance(identity, tuple) or not identity or not isinstance(payload, dict)
                or not all(isinstance(v, str) and v.strip()
                           for v in (account, trading_day, kind, *identity))):
            raise ValueError('缺少账户、交易日、事件类型、稳定身份或有效载荷')
        key = (account, trading_day, kind, _json(identity))
        encoded = _json(payload)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('''SELECT payload FROM facts WHERE account=? AND day=?
                AND kind=? AND identity=? AND outcome='new' ''', key).fetchone()
            outcome = (Outcome.NEW if prior is None else Outcome.DUPLICATE
                       if prior[0] == encoded else Outcome.CONFLICT)
            row = db.execute('''INSERT INTO facts(account,day,kind,identity,payload,outcome)
                VALUES(?,?,?,?,?,?)''', (*key, encoded, outcome))
            return row.lastrowid, outcome

    @classmethod
    def open_readonly(cls, path: Path) -> Journal:
        """只读复盘不创建新库、表或索引；传错路径必须失败。"""
        obj = cls.__new__(cls)
        obj.path = Path(path).resolve(strict=True)
        obj._readonly = True
        return obj

    def replay(self, *, after: int = 0) -> Iterator[Record]:
        """按提交序号读取，包括冲突（重启不能忘掉冻结原因）。"""
        with closing(self._connect()) as db:
            for seq, acc, day, kind, identity, payload, result in db.execute(
                    '''SELECT seq,account,day,kind,identity,payload,outcome FROM facts
                       WHERE seq>? ORDER BY seq''', (after,)):
                yield Record(seq, acc, day, kind, tuple(json.loads(identity)),
                             json.loads(payload), Outcome(result))
