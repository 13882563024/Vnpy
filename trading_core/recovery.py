"""专户 1:1 组合的一步隔夜恢复审计，不写流水、不生成许可、不认领柜台仓。

两轮 QuerySnapshot 都须由受信调用方在账户锁内从 QueryCollector 领取；旧轮须为收盘
最终查询，新轮须在新日交易前。交易日历、快照来源/时效和完整流水的保管在调用端。
摘要用于重新核对证据，不是签名或完整性证明。无旧轮或更早交易事实时不推测基线。
"""
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from hashlib import sha256
import json

from .ctp_facts import account_key, trading_day
from .held_check import _snapshot_orders, _snapshot_fills, _valid_intent
from .intents import OrderIntent
from .journal import Journal, Outcome
from .paired_fills import matched_fills
from .preflight import GateDenied
from .query_round import QuerySnapshot


@dataclass(frozen=True)
class RecoveryScope:
    account: str
    strategy: str
    config_version: str
    exchange: str
    near: str
    far: str

    @property
    def symbol(self):
        prefix = {'DCE': 'SP', 'CZCE': 'SPD'}[self.exchange]
        return f'{prefix} {self.near}&{self.far}'


@dataclass(frozen=True)
class CarryLayer:
    tier_id: str
    open_decision: str
    open_day: str
    side: str
    lots: int
    spread_cost: Decimal
    near_cost: Decimal | None
    far_cost: Decimal | None


@dataclass(frozen=True)
class CarryBaseline:
    scope: RecoveryScope
    source_day: str
    layers: tuple[CarryLayer, ...]
    evidence_digest: str


@dataclass(frozen=True)
class AuditDifference:
    subject: str
    expected: str
    observed: str


@dataclass(frozen=True)
class RecoveryAudit:
    baseline_digest: str
    differences: tuple[AuditDifference, ...]
    review_steps: tuple[str, ...]

    @property
    def matched(self):
        return not self.differences


def _require(ok, reason):
    if not ok:
        raise GateDenied(reason)


def _scope_rows(scope, snap):
    r = snap.round
    _require((r.account, r.strategy) == (scope.account, scope.strategy) and
             trading_day({}, r.day) == r.day, '查询轮次账户、策略或交易日不符')
    _require(len(snap.accounts) == 1 and snap.accounts[0]['CurrencyID'] == 'CNY', '非唯一人民币账户')
    for rows, name in ((snap.accounts, 'AccountID'), (snap.orders, 'InvestorID'),
                       (snap.trades, 'InvestorID'), (snap.positions, 'InvestorID')):
        for p in rows:
            _require(account_key(p['BrokerID'], p[name]) == scope.account and p['TradingDay'] == r.day,
                     '柜台行账户或交易日不符')


def _positions(scope, layers, rows, *, yesterday):
    # 不合并重复行、不采用净额。每腿分别比较总/今/昨/买冻/卖冻。
    qty = sum(layer.lots for layer in layers)
    buy = layers[0].side == 'buy'
    keys = ((scope.near, '2' if buy else '3'), (scope.far, '3' if buy else '2'))
    expected = {k: (qty, 0 if yesterday else qty, qty if yesterday else 0, 0, 0) for k in keys}
    observed = {}
    diffs = []
    for p in rows:
        key = (p['InstrumentID'], p['PosiDirection'])
        fields = ('Position', 'TodayPosition', 'YdPosition', 'LongFrozen', 'ShortFrozen')
        values = tuple(p[k] for k in fields)
        _require(all(type(x) is int and x >= 0 for x in values), '持仓或冻结数量未知')
        if not any(values) and key not in expected:
            continue
        _require(key not in observed and p['ExchangeID'] == scope.exchange and p['HedgeFlag'] == '1',
                 f'重复持仓行、错交易所或非投机持仓：{key}')
        observed[key] = values
    for key in sorted(set(expected) | set(observed)):
        want, got = expected.get(key), observed.get(key)
        if want != got:
            diffs.append(AuditDifference(f'{key[0]}/{key[1]} 总/今/昨/买冻/卖冻', str(want), str(got)))
    return tuple(diffs)


def _encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def build_baseline(scope: RecoveryScope, prior_snapshot: QuerySnapshot, journal: Journal) -> CarryBaseline:
    """从单日完整收盘柜台行和绑定流水交叉重建；任何不确定性抛 GateDenied。"""
    try:
        return _build(scope, prior_snapshot, journal)
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, IndexError) as exc:
        raise GateDenied(f'隔夜基线无法证明：{exc}') from exc


def _build(scope, snap, journal):
    _require(all(type(v) is str and v.strip() for v in asdict(scope).values()) and scope.near != scope.far,
             '专户策略或组合配置缺失')
    _scope_rows(scope, snap)
    day = snap.round.day
    intents, bindings, history, facts = {}, {}, defaultdict(list), {}
    digest = sha256()
    for row in journal.replay():
        if row.account != scope.account:
            continue
        _require(row.outcome != Outcome.CONFLICT, f'账户事实冲突 #{row.sequence}')
        if row.kind in ('shadow_input', 'layer_event', 'account_snapshot', 'paper_session', 'paper_fill_fee', 'settled_fill_fee') or row.outcome == Outcome.DUPLICATE:
            continue
        _require(row.trading_day == day, '存在其它交易日流水，不能跳过核对')
        key, p = row.identity, row.payload
        digest.update((_encode(asdict(row)) + '\n').encode())
        if row.kind == 'order_intent':
            i = OrderIntent(**p)
            _valid_intent(i)
            _require(key == (scope.strategy, i.decision_id) and (i.account, i.day, i.strategy, i.config_version,
                     i.symbol) == (scope.account, day, scope.strategy, scope.config_version, scope.symbol), '意图归属不符')
            intents[i.decision_id] = i
        elif row.kind == 'order_binding':
            d = p['decision_id']
            _require(len(key) == 3 and d in intents and d not in bindings.values() and
                     (p['strategy'], p['exchange']) == (scope.strategy, scope.exchange), '绑定不唯一或不属于意图')
            bindings[key] = d
        elif row.kind == 'order':
            _require(len(key) == 6 and key[:3] in bindings, '未绑定的委托')
            history[key[:3]].append(row)
        elif row.kind == 'fill':
            facts[key] = row
        else:
            raise GateDenied(f'未解决的流水 {row.kind} #{row.sequence}')
    _require(set(bindings.values()) == set(intents) and set(bindings) == set(history), '存在未决意图或绑定')
    # 终态后的新活动、倒退的累计或撤改/拒绝未闭合，不能靠一次相符终态快照掩盖。
    for events in history.values():
        previous, terminal = 0, False
        for event in events:
            status, submit, encoded_qty = event.identity[3:]
            q, total = event.payload['traded'], event.payload['volume']
            allowed = {'0': q == total, '1': 0 < q < total, '2': 0 < q < total,
                       '3': q == 0, '4': q == 0, '5': q < total}
            _require(not terminal and str(float(q)) == encoded_qty and type(q) in (int, float)
                     and q == int(q) and q >= previous and allowed.get(status)
                     and submit in ('0', '3'), '历史委托状态未闭合或累计倒退')
            previous, terminal = q, status in ('0', '2', '4', '5')
    orders, systems = _snapshot_orders(snap, scope, intents, bindings, history)
    grouped = _snapshot_fills(snap, scope, facts, systems, orders)
    keys = {d: k for k, d in bindings.items()}
    layers, used = {}, set()
    for d, i in intents.items():
        key = keys[d]
        order = orders[key]
        _require(order['OrderStatus'] in ('0', '2', '4', '5'), '委托未终结')
        m = matched_fills(grouped[key], exchange=scope.exchange, sys_id=order['OrderSysID'].strip(),
                          near=scope.near, far=scope.far, combo=scope.symbol, buy=i.side == 'buy', max_lots=i.lots)
        _require(m.lots == order['VolumeTraded'] and not (m.residual_near or m.residual_far), '腿量或累计成交不闭合')
        if i.purpose in ('open', 'add'):
            _require(i.tier_id not in used and all(l.side == i.side for l in layers.values()), '层号重用或双向混合')
            used.add(i.tier_id)
            if m.lots:
                layers[i.tier_id] = CarryLayer(i.tier_id, d, day, i.side, m.lots, m.average_spread,
                                               m.near_average, m.far_average)
        else:
            l = layers.get(i.tier_id)
            _require(i.purpose in ('take_profit', 'risk_exit') and l and i.side != l.side and i.lots <= l.lots,
                     '平仓层归属或手数不符')
            if m.lots == l.lots:
                del layers[i.tier_id]
            else:
                layers[i.tier_id] = replace(l, lots=l.lots - m.lots)
    live = tuple(layers.values())
    _require(live, '没有归属已证明且有结余的层')
    diff = _positions(scope, live, snap.positions, yesterday=False)
    _require(not diff, f'上一日柜台今仓不符：{diff}')
    # 绑定查询身份和所有原始字段；同一查询行的顺序不参与证明。
    digest.update(_encode([asdict(scope), asdict(snap.round), *[sorted(_encode(dict(p)) for p in rows)
                          for rows in (snap.orders, snap.trades, snap.positions, snap.accounts)]]).encode())
    return CarryBaseline(scope, day, live, digest.hexdigest())


def audit_rollover(baseline: CarryBaseline, prior_snapshot: QuerySnapshot, current_snapshot: QuerySnapshot,
                   journal: Journal, *, previous_trading_day: str) -> RecoveryAudit:
    """previous_trading_day 来自当前交易日的权威交易日历，不用自然日差/夜盘日期推算。"""
    try:
        _require(build_baseline(baseline.scope, prior_snapshot, journal) == baseline, '旧基线与事实重放不一致')
        _scope_rows(baseline.scope, current_snapshot)
        _require(baseline.source_day == previous_trading_day < current_snapshot.round.day, '不是经日历确认的前一交易日')
        _require(not current_snapshot.orders and not current_snapshot.trades, '新交易日已有委托或成交，拒绝纯隔夜恢复')
        diffs = _positions(baseline.scope, baseline.layers, current_snapshot.positions, yesterday=True)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, ArithmeticError) as exc:
        diffs = (AuditDifference('证据链', '旧日重放闭合、新日完整且无活动', str(exc)),)
    steps = ('保持停用；重新获取整轮查询并按腿核对原始回报和绑定。',
             '人工复核差异，若需纠正须另行审批并追加引用原证据的新记录；不得修改旧事实。') if diffs else ()
    return RecoveryAudit(baseline.evidence_digest, diffs, steps)
