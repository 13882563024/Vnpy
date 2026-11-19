"""Offline projections; no engines, publishers, schema migrations or strategy replay."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import stat

from trading_core.decision_view import describe_execution
from trading_core.journal import Outcome, Record
from trading_core.fee_view import Fees, order_fees
from trading_core.trace import trace_decision

UNKNOWN = '未知'
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'config' / 'spreads.json'


@dataclass(frozen=True)
class Sources:
    config: Path = DEFAULT_CONFIG
    facts: Path | None = None
    shadow: Path | None = None
    credentials: Path | None = None
    account: str | None = None
    day: str | None = None


@dataclass
class Feed:
    name: str
    path: Path | None
    status: str = '未指定'
    events: list[Record] = field(default_factory=list)


@dataclass
class Snapshot:
    sources: Sources
    spreads: dict
    config_status: str
    feeds: tuple[Feed, ...]
    credential_status: str
    config_hash: str = ''


def _creds(path):
    if path is None:
        return '未知 · 未指定凭证文件'
    try:
        mode = Path(path).stat().st_mode
        return '文件存在 · 内容未读取，有效性未知' if stat.S_ISREG(mode) else '未知 · 不是普通文件'
    except FileNotFoundError:
        return '文件未找到 · 其他来源未知'
    except OSError:
        return '未知 · 无法检查文件状态'


def _stamp(path):
    s = path.stat()
    if not stat.S_ISREG(s.st_mode):
        raise ValueError('不是普通文件')
    for ext in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + ext)
        if side.exists():
            raise ValueError('存在未合并的事务文件；请提供已关闭的完整快照')
    return s.st_size, s.st_mtime_ns, s.st_ino


def _feed(name, location, sources):
    feed = Feed(name, location)
    if location is None:
        return feed
    try:
        path = Path(location).resolve(strict=True)
        before = _stamp(path)
        # immutable prevents SQLite creating SHM/WAL sidecars. Only closed snapshots accepted.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute("SELECT type FROM sqlite_schema WHERE name='facts'").fetchone() != ('table',):
                raise ValueError('没有 facts 表')
            # Provenance describes the input file, not just the day/account currently visible.
            if name == '事实流水' and db.execute("SELECT 1 FROM facts WHERE kind='paper_session' LIMIT 1").fetchone():
                feed.name = '纸面柜台（模拟，非真实账户）'
            sql = 'SELECT seq,account,day,kind,identity,payload,outcome FROM facts WHERE (? IS NULL OR account=?) AND (? IS NULL OR day=?) ORDER BY seq'
            args = (sources.account, sources.account, sources.day, sources.day)
            for seq, acc, day, kind, ident, raw, outcome in db.execute(sql, args):
                key, payload = json.loads(ident), json.loads(raw)
                if (not isinstance(key, list) or not key or not all(isinstance(x, str) and x for x in key)
                        or not isinstance(payload, dict) or not all(isinstance(x, str) for x in (acc, day, kind))):
                    raise ValueError('流水格式错误')
                feed.events.append(Record(seq, acc, day, kind, tuple(key), payload, Outcome(outcome)))
        if before != _stamp(path):
            raise ValueError('读取期间文件有变化')
        feed.status = f'已读取 {len(feed.events)} 条'
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError) as exc:
        feed.events.clear()
        # Never echo a raw payload in an error message.
        feed.status = ('文件未找到' if isinstance(exc, FileNotFoundError) else
                       '读取失败 · 请检查文件格式，使用已关闭且无待合并事务的快照')
    return feed


def load_snapshot(sources: Sources = Sources()) -> Snapshot:
    config_hash = ''
    try:
        raw = Path(sources.config).read_bytes()
        spreads = json.loads(raw.decode('utf-8-sig'))
        config_hash = sha256(raw).hexdigest()
        if not isinstance(spreads, dict) or not all(isinstance(v, dict) for v in spreads.values()):
            raise ValueError('价差配置须为对象')
        status = f'{len(spreads)} 个配置价差'
    except (ValueError, OSError, RecursionError):
        spreads, status = {}, '配置不可读 · 检查路径或 JSON 格式'
    feeds = (_feed('事实流水', sources.facts, sources), _feed('影子流水', sources.shadow, sources))
    return Snapshot(sources, spreads, status, feeds, _creds(sources.credentials), config_hash)


STATES = {'reserved_only': '仅记录意图', 'broker_reported': '有柜台回报', 'send_result_unknown': '发送结果未知',
          'awaiting_fill_details': '缺少成交明细', 'unpaired_fills': '存在未配对腿', 'needs_review': '需核对'}
PURPOSE = {'open': '开层', 'add': '加层', 'take_profit': '止盈', 'risk_exit': '风险退出'}


def number(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = Decimal(str(v))
        return n if n.is_finite() and abs(n.adjusted()) < 100 else None
    except (InvalidOperation, ValueError):
        return None


def scoped_decisions(snapshot, key):
    """Return per-source evidence, never joining accounts, trading days or journals."""
    cfg = snapshot.spreads.get(key, {})
    symbols = {v for v in (cfg.get('exchange_spread'), cfg.get('display_name'), key) if isinstance(v, str)}
    legs = cfg.get('legs')
    legs = [x.split('.')[0] for x in legs] if isinstance(legs, list) and all(isinstance(x, str) for x in legs) else []
    rows = []
    for feed in snapshot.feeds:
        by_scope = defaultdict(list)
        for r in feed.events:
            by_scope[r.account, r.trading_day].append(r)
        for r in feed.events:
            p = r.payload
            if (r.kind != 'order_intent' or r.outcome != Outcome.NEW or not isinstance(p.get('symbol'), str)
                    or p['symbol'] not in symbols):
                continue
            scope = by_scope[r.account, r.trading_day]
            row = dict(feed=feed.name, record=r, state='需核对', issues=[], paired=None, avg=None, refs=[r.sequence],
                       near_avg=None, far_avg=None, residual_near=None, residual_far=None)
            rows.append(row)
            try:
                if len(r.identity) != 2 or any(p.get(k) != v for k, v in zip(('account','day','strategy','decision_id'),
                        (r.account, r.trading_day, *r.identity))):
                    raise ValueError('意图身份不一致')
                if (p.get('purpose') not in ('open','add','take_profit','risk_exit') or p.get('side') not in ('buy','sell')
                        or type(p.get('lots')) is not int or p['lots'] <= 0 or number(p.get('price')) is None
                        or not isinstance(p.get('tier_id'), str) or not p['tier_id']):
                    raise ValueError('意图字段错误')
                if len(legs) != 2:
                    raise ValueError('配置缺少近远腿')
                trace = trace_decision(scope, r.account, r.trading_day, *r.identity)
                view = describe_execution(trace, near=legs[0], far=legs[1])
                row.update(state=STATES[view['state']], issues=view['issues'], paired=view['paired_lots'], avg=view['avg_spread'],
                           near_avg=view['near_avg'], far_avg=view['far_avg'], residual_near=view['unpaired_near'], residual_far=view['unpaired_far'])
                row['refs'].extend(x.sequence for x in (*trace.bindings, *trace.orders, *trace.fills))
                markers = [x for x in scope if x.kind == 'paper_session' and x.outcome != Outcome.DUPLICATE]
                paper = bool(markers); row['paper'] = paper
                verified = not markers or (len(markers) == 1 and markers[0].outcome == Outcome.NEW and markers[0].payload.get('synthetic') is True)
                if not row['issues'] and verified and feed.name != '影子流水':
                    row['fee'] = order_fees(trace, scope, paper=paper, near=legs[0], far=legs[1])
                    row['refs'].extend(row['fee'].refs)
                if feed.name == '影子流水' and not trace.issues:
                    related = [x for x in scope if x.kind == 'shadow_input' and x.identity[0] == r.identity[0]
                               and isinstance(x.payload.get('data'), dict) and x.payload['data'].get('order') == r.identity[1]
                               and x.payload.get('kind') == 'matched']
                    if any(x.outcome == Outcome.CONFLICT and x.kind == 'shadow_input' and x.identity[0] == r.identity[0] for x in scope):
                        raise ValueError('同策略存在影子输入冲突')
                    prev = None
                    for x in related:
                        if x.outcome != Outcome.NEW:
                            continue
                        d = x.payload['data']; lots = d['lots']; avg = number(d['avg'])
                        if (type(lots) is not int or not 0 <= lots <= p['lots'] or (avg is None) != (lots == 0)
                                or any(type(d[k]) is not int or not 0 <= d[k] <= p['lots']-lots for k in ('residual_near','residual_far'))
                                or (prev and (lots < prev['lots'] or lots == prev['lots'] and avg != number(prev['avg'])
                                    or any(lots+d[k] < prev['lots']+prev[k] for k in ('residual_near','residual_far'))))):
                            raise ValueError('影子累计配对事实不一致')
                        prev = d
                        row.update(state='影子配对记录', paired=lots, avg=avg, near_avg=d.get('near_avg'), far_avg=d.get('far_avg'),
                                   residual_near=d['residual_near'], residual_far=d['residual_far'])
                        row['refs'].append(x.sequence)
                    statuses = [x for x in scope if x.kind == 'shadow_input' and x.identity[0] == r.identity[0]
                                and x.outcome == Outcome.NEW and x.payload.get('kind') == 'status'
                                and isinstance(x.payload.get('data'), dict) and x.payload['data'].get('order_id') == r.identity[1]]
                    if statuses:
                        latest = statuses[-1]; status = latest.payload['data'].get('type'); row['refs'].append(latest.sequence)
                        suffix = {'Working':'工作中', 'Terminal':'终结回报', 'Unknown':'状态未知'}.get(str(status), '状态未知')
                        row['state'] += ' · 影子'+suffix
            except (KeyError, TypeError, ValueError, IndexError, ArithmeticError):
                row.update(state='需核对', paired=None, avg=None, issues=['身份、配置或成交证据不完整或矛盾'])
    return rows


def tier_returns(rows):
    """Only gross P&L for a uniquely opened tier with evidenced multiplier and fills."""
    groups = defaultdict(list)
    for row in rows:
        r = row['record']; tier = r.payload.get('tier_id')
        if isinstance(tier, str) and tier:
            groups[row['feed'], r.account, r.identity[0], tier].append(row)
    result = []
    for group, group_rows in groups.items():
        opens = [x for x in group_rows if x['record'].payload.get('purpose') in ('open','add')]
        exits = [x for x in group_rows if x['record'].payload.get('purpose') in ('take_profit','risk_exit')]
        gross = remaining = None
        if len(opens) == 1 and len(exits)+1 == len(group_rows):
            o = opens[0]; p = o['record'].payload; reason = p.get('reason'); config = reason.get('config') if isinstance(reason, dict) else None
            m = number(config.get('multiplier')) if isinstance(config, dict) else None
            entry = number(o['avg']); direction = {'buy':1,'sell':-1}.get(p.get('side')) if isinstance(p.get('side'),str) else None
            if (direction and m and m > 0 and entry is not None and type(o['paired']) is int and o['paired'] > 0
                    and all(not x['issues'] and x['residual_near']==0 and x['residual_far']==0 for x in group_rows)
                    and all(type(x['paired']) is int and (number(x['avg']) is not None or x['paired'] == 0) and x['record'].sequence > o['record'].sequence
                            and x['record'].payload.get('side') == ('sell' if direction == 1 else 'buy') for x in exits)):
                closed = sum(x['paired'] for x in exits)
                if closed <= o['paired']:
                    remaining = o['paired']-closed
                    gross = sum((direction*(number(x['avg'])-entry)*m*x['paired'] for x in exits if x['paired']), Decimal(0))
        result.append((group, remaining, gross, [seq for row in group_rows for seq in row['refs']]))
    return result


def strategy_returns(rows):
    """Single-day, single-strategy evidence totals; no cross-journal/account netting.

    Opening fees are allocated pro rata to closed lots. The remainder is not a
    realized expense. Budgets on protection orders are not fill fees.
    """
    groups = defaultdict(list)
    for row in rows:
        r = row['record']
        groups[row['feed'], r.account, r.trading_day, r.identity[0]].append(row)
    result = []
    for scope, members in groups.items():
        tiers = tier_returns(members); details = []; sources = set()
        for (_, _, _, tier), remaining, gross, refs in tiers:
            matching = [r for r in members if r['record'].payload.get('tier_id') == tier]
            used = [r for r in matching if r['paired'] and not r['issues']]
            fees = None
            if gross is not None and used:
                opening = next(r for r in used if r['record'].payload['purpose'] in ('open','add'))
                if all(r.get('fee', Fees()).amount is not None for r in used):
                    fees = sum((r['fee'].amount for r in used if r is not opening), Decimal(0))
                    fees += opening['fee'].amount * (opening['paired']-remaining) / opening['paired']
                    sources.update(s for r in used for s in r['fee'].sources)
            details.append(dict(tier=tier, remaining=remaining, gross=gross, fee=fees,
                                net=gross-fees if fees is not None else None, refs=refs))
        def total(key):
            if details and all(r[key] is not None for r in details) and all(isinstance(r['record'].payload.get('tier_id'),str) and r['record'].payload['tier_id'] for r in members):
                return sum((r[key] for r in details), Decimal(0))
        budgets = []
        for row in members:
            rec = row['record']; reason = rec.payload.get('reason')
            if rec.payload.get('purpose') != 'take_profit' or not isinstance(reason, dict):
                continue
            estimate = number(reason.get('estimated_fee')); spec = reason.get('fees')
            if estimate is not None and estimate >= 0 and isinstance(spec, dict):
                budgets.append((rec.payload.get('tier_id'), rec.sequence, estimate, spec.get('source')))
        result.append(dict(scope=scope, tiers=details, budgets=budgets, gross=total('gross'), fee=total('fee'), net=total('net'),
                           paper=any(r.get('paper') for r in members), sources=sorted(sources)))
    return result
