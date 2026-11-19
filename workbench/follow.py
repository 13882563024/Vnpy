"""Read only sealed, generation-named paper publications; never open a live journal."""
from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import sqlite3
import stat
import time

from trading_core.journal import Outcome, Record
from .model import Feed, Sources, load_snapshot

STALE_SECONDS = 15
PAPER = '纸面柜台（模拟，非真实账户）'
SHADOW = '影子流水'
SHADOW_KINDS = {'shadow_input', 'layer_event', 'order_intent', 'runtime_bridge'}


def regular(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or path.resolve(strict=True) != path:
        raise ValueError('不是独立的普通发布文件')
    return s.st_ino, s.st_size, s.st_mtime_ns


@dataclass(frozen=True)
class Publication:
    generation: int
    state: str
    published_at: float
    account: str
    day: str

    @classmethod
    def read(cls, root):
        path = root / 'workbench.json'
        regular(path)
        with path.open('rb') as f:
            raw = f.read(8193)
        if len(raw) > 8192:
            raise ValueError('发布清单过大')
        obj = json.loads(raw)
        if not isinstance(obj, dict) or type(obj.get('version')) is not int or obj['version'] != 1:
            raise ValueError('不支持的发布格式')
        p = cls(**{key: obj[key] for key in cls.__dataclass_fields__})
        if (type(p.generation) is not int or not 0 < p.generation < 2**63
                or p.state not in ('running', 'finished', 'failed')
                or type(p.published_at) not in (int, float) or not math.isfinite(p.published_at)
                or not isinstance(p.day, str) or not p.day.isascii() or not p.day.isdigit() or len(p.day) != 8
                or not isinstance(p.account, str)):
            raise ValueError('发布清单字段无效')
        account = json.loads(p.account)
        if not (isinstance(account, list) and len(account) == 2 and account[0] == 'paper'
                and isinstance(account[1], str) and account[1].strip()):
            raise ValueError('不是纸面专户')
        return p


def sealed(path):
    stamp = regular(path)
    for ext in ('-wal', '-shm', '-journal'):
        if Path(str(path) + ext).exists():
            raise ValueError('发布副本仍有事务文件')
    with path.open('rb') as f:
        header = f.read(20)
    if header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01':
        raise ValueError('只接受已关闭的回滚日志格式副本')
    return stamp


def delta(path, p, previous, facts):
    stamp = sealed(path)
    cursor = previous[-1].sequence if previous else 0
    rows = []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        if db.execute("SELECT type FROM pragma_table_list WHERE schema='main' AND name='facts'").fetchone() != ('table',):
            raise ValueError('缺少事实表')
        if db.execute('SELECT 1 FROM facts WHERE account IS NOT ? OR day IS NOT ? OR seq<=0 LIMIT 1',
                      (p.account, p.day)).fetchone():
            raise ValueError('混合账户或交易日')
        if facts:
            markers = db.execute("SELECT account,day,identity,payload,outcome FROM facts WHERE kind='paper_session' LIMIT 2").fetchall()
            if len(markers) != 1:
                raise ValueError('缺少唯一纸面会话')
            account, day, key, raw, outcome = markers[0]
            payload = json.loads(raw)
            if not (account == p.account and day == p.day and json.loads(key) == ['PAPER'] and outcome == 'new'
                    and isinstance(payload, dict) and payload.get('mode') == 'paper' and payload.get('synthetic') is True):
                raise ValueError('纸面会话不匹配')
        if cursor and db.execute('SELECT count(*) FROM facts WHERE seq<=?', (cursor,)).fetchone()[0] != len(previous):
            raise ValueError('已发布历史被截断')
        for seq, account, day, kind, ident, raw, outcome in db.execute(
                'SELECT seq,account,day,kind,identity,payload,outcome FROM facts WHERE seq>? ORDER BY seq', (cursor,)):
            identity, payload = json.loads(ident), json.loads(raw)
            if (type(seq) is not int or seq <= 0 or account != p.account or day != p.day
                    or not isinstance(kind, str) or not kind or (not facts and kind not in SHADOW_KINDS)
                    or not isinstance(identity, list) or not identity or not all(isinstance(x,str) and x for x in identity)
                    or not isinstance(payload, dict)):
                raise ValueError('流水不属于本次运行或格式不符')
            rows.append(Record(seq, account, day, kind, tuple(identity), payload, Outcome(outcome)))
    if sealed(path) != stamp:
        raise ValueError('副本在读取期间改变')
    return rows


class PaperFollow:
    def __init__(self, directory, sources=Sources(), clock=time.time):
        if any((sources.facts, sources.shadow, sources.credentials)):
            raise ValueError('跟随模式不接受其他数据库或凭证')
        self.root = Path(directory).resolve()
        self.sources = sources
        self.clock = clock
        self.snapshot = load_snapshot(sources)
        self.publication = None
        self.tail = deque(maxlen=100)
        self.status = '等待首次纸面发布'

    def poll(self):
        """Atomically adopt both deltas or keep the previous verified pair. Return whether UI changed."""
        old_status = self.status
        old = self.publication
        try:
            p = Publication.read(self.root)
            now = self.clock()
            if p.published_at > now + 1 or (old and (p.generation < old.generation
                    or (p.generation == old.generation and p != old) or p.account != old.account or p.day != old.day
                    or p.published_at < old.published_at or (old.state != 'running' and p != old))):
                raise ValueError('发布回退、时钟或会话不一致')
            if ((self.sources.account is not None and self.sources.account != p.account)
                    or (self.sources.day is not None and self.sources.day != p.day)):
                raise ValueError('筛选账户或交易日不匹配')
            if p != old:
                paths = [self.root / f'{stem}-{p.generation}.sqlite' for stem in ('facts','strategy')]
                additions = [delta(path, p, feed.events, i == 0)
                             for i, (path, feed) in enumerate(zip(paths, self.snapshot.feeds))]
                feeds = []
                for path, name, feed, new in zip(paths, (PAPER, SHADOW), self.snapshot.feeds, additions):
                    events = feed.events + new if new else feed.events
                    feeds.append(Feed(name, path, f'已核对 {len(events)} 条', events))
                    for r in new[-100:]:
                        self.tail.append((p.generation, name, r, '增量' if old else '首次载入'))
                self.snapshot = replace(self.snapshot, feeds=tuple(feeds), sources=replace(
                    self.sources, facts=paths[0], shadow=paths[1], account=p.account, day=p.day))
                self.publication = p
            state = {'finished':'已结束 · 历史', 'failed':'运行失败 · 历史'}.get(p.state)
            if not state:
                state = '断更 · 数据已过期' if now - p.published_at > STALE_SECONDS else '更新中 · 纸面发布'
            self.status = f'{state}　/　第 {p.generation} 版'
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError, sqlite3.Error) as exc:
            if isinstance(exc, FileNotFoundError):
                hint = '等待完整清单和两份关闭副本'
            elif type(exc) is ValueError and str(exc) in {
                '筛选账户或交易日不匹配', '混合账户或交易日', '缺少唯一纸面会话', '纸面会话不匹配',
                '只接受已关闭的回滚日志格式副本', '发布副本仍有事务文件', '发布回退、时钟或会话不一致',
            }:
                hint = str(exc)
            else:
                hint = '请核对纸面目录及发布格式'
            self.status = ('断更 · 保留上次核对数据（过期）' if old else '等待有效纸面发布') + '　/　' + hint
        return old != self.publication or old_status != self.status
