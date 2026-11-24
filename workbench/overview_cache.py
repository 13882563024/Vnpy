"""Local, display-only history. Never fed back into an engine or reconciliation.

Files are partitioned by cabinet, front and login identity. Only the whitelisted
observations below are stored; the connection setting, credentials and trading
permissions are never copied into this cache.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date
from math import isfinite
from pathlib import Path
from time import time
from typing import NamedTuple


METRICS = ('balance', 'available', 'margin', 'realized', 'unrealized')
ROW_FIELDS = ('cabinet', 'orders', 'live')


class CacheScope(NamedTuple):
    production: bool
    front: str
    broker: str
    user: str


def _scope(production, front, broker, user):
    if (type(production) is not bool or not all(isinstance(x, str) and x.strip()
                                                for x in (front, broker, user))):
        return None
    front = front.strip()
    if not front.startswith(('tcp://', 'ssl://', 'socks')):
        front = 'tcp://' + front
    if not front.partition('://')[2] and not front.startswith('socks'):
        return None
    return CacheScope(production, front, broker, user)


def gateway_scope(td):
    """Snapshot under the TD writer lock; unlike settings this is the active front."""
    front = getattr(td, 'overview_front', None)
    if isinstance(front, tuple) and len(front) == 2:
        address, production = front
        return _scope(production, address, getattr(td, 'brokerid', None), getattr(td, 'userid', None))
    return None


def _file_path(name):
    from vnpy.trader.utility import get_file_path

    return Path(get_file_path(name))


def configured_scope():
    """Resolve the intended login on cold start, without loading a default account."""
    setting = _read(_file_path('connect_ctp.json')) or _read(_file_path('ctp_connect.json'))
    environment = setting.get('柜台环境', '实盘')
    if environment not in ('实盘', '测试'):
        return None
    return _scope(environment == '实盘', setting.get('交易服务器'),
                  setting.get('经纪商代码'), setting.get('用户名'))


def spread_identity(config):
    """A cache row for the old legs/formula cannot follow a reused display name."""
    fields = [config.get(key) for key in ('legs', 'exchange_spread', 'exchange', 'price_formula')]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=True).encode('utf-8')).hexdigest()


def _read(path):
    try:
        with path.open(encoding='utf-8') as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _path(scope):
    key = hashlib.sha256(json.dumps(scope, ensure_ascii=True).encode('utf-8')).hexdigest()
    return _file_path(f'overview_history_v1/{key}.json')


def number(value):
    return value if type(value) in (int, float) and abs(value) < 1e100 and isfinite(value) else None


def _sample(raw, latest, *, metrics=False):
    if not isinstance(raw, dict):
        return None
    at, value = number(raw.get('at')), raw.get('value')
    if at is None or not 0 < at <= latest:
        return None
    if metrics:
        if not isinstance(value, dict):
            return None
        value = {field: n for field in METRICS if (n := number(value.get(field))) is not None}
        if not value:
            return None
    elif not isinstance(value, str) or not value or len(value) > 32768:
        return None
    return {'at': at, 'value': value}


def _sanitize(raw):
    day = raw.get('day')
    if not isinstance(day, str):
        return {}
    try:
        if date.fromisoformat(day).isoformat() != day:
            return {}
    except ValueError:
        return {}
    latest = time() + 5  # minor clock/serialization jitter; never accept arbitrarily future observations
    rows, valid_rows = raw.get('rows'), {}
    if isinstance(rows, dict):
        for name, row in rows.items():
            identity = row.get('identity') if isinstance(row, dict) else None
            if not isinstance(identity, str) or len(identity) != 64:
                continue
            fields = {k: sample for k in ROW_FIELDS if (sample := _sample(row.get(k), latest)) is not None}
            if fields:
                valid_rows[name] = {'identity': identity, **fields}
    account = _sample(raw.get('account'), latest, metrics=True)
    if not account and not valid_rows:
        return {}
    return {'day': day, 'account': account, 'rows': valid_rows}


def load(scope):
    """Return only valid history for exactly this scope, never a default scope."""
    if scope is None:
        return {}
    raw = _read(_path(scope))
    return _sanitize(raw) if raw.get('version') == 1 else {}


def _newer(current, incoming):
    if incoming and (not current or incoming['at'] > current['at']):
        return incoming
    return current


def save(scope, day, account, rows):
    """Merge newer individual observations without refreshing their timestamps.

    A newer trading day starts a fresh cache; invalid/empty updates do not erase
    prior history. No file is opened for writing when the refresh changed nothing.
    """
    if scope is None:
        return
    update = _sanitize({'day': day, 'account': account, 'rows': rows})
    if not update:
        return
    previous = load(scope)
    if previous and previous['day'] > day:
        return
    if previous.get('day') == day:
        update['account'] = _newer(previous['account'], update['account'])
        merged_rows = previous['rows'].copy()
        for name, row in update['rows'].items():
            current = merged_rows.get(name, {})
            if current.get('identity') == row['identity']:
                row = {'identity': row['identity'], **{key: sample for key in ROW_FIELDS
                       if (sample := _newer(current.get(key), row.get(key))) is not None}}
            merged_rows[name] = row
        update['rows'] = merged_rows
        if update == previous:
            return
    path = _path(scope)
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as output:
            tmp = Path(output.name)
            json.dump({'version': 1, **update}, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        tmp.replace(path)
    except OSError:
        # UI remains usable; Windows may transiently refuse replacement while a reader
        # is open, in which case the last complete snapshot is left intact.
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
