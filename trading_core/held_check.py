"""专户、单交易日、单向 1:1 组合的有仓门禁。只读，不推测昨仓或缺失回报。

在账户锁内传入 QueryCollector 的完整快照，查询委托和成交须先由 append_report 入账。
此处不认证快照来源/时效、不发送或持久化许可；调用端仍须 seal/consume。
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from math import isfinite

from .ctp_facts import account_key
from .intents import OrderIntent
from .journal import Journal, Outcome
from .paired_fills import matched_fills
from .preflight import Evidence, GateDenied
from .query_round import QuerySnapshot


@dataclass(frozen=True)
class HeldLimits:
    near: str
    far: str
    exchange: str
    max_open_lots: int  # 总容量，不是调用方计算的剩余额度
    margin_per_lot: float
    signal_current: bool
    quotes_current: bool
    session_open: bool


def _require(ok, reason):
    if not ok:
        raise GateDenied(reason)


def _number(n):
    return type(n) in (int, float) and isfinite(n)


def _lots(n, minimum=0):
    return type(n) is int and n >= minimum


def _text(s):
    return type(s) is str and bool(s.strip())


def _valid_intent(i):
    _require(all(_text(x) for x in (i.tier_id, i.decision_id, i.strategy, i.config_version))
             and i.side in ('buy', 'sell') and _lots(i.lots, 1) and _number(i.price)
             and isinstance(i.reason, dict) and i.reason, '意图字段未知')


def _local(row):
    return str(row['FrontID']), str(row['SessionID']), row['OrderRef'].strip()


def _journal(intent, journal, exchange):
    intents, bindings, orders, fills = {}, {}, defaultdict(list), {}
    for row in journal.replay():  # 刻意不按日过滤；前日未决、归属不明不能消失
        if row.account != intent.account:
            continue
        _require(row.outcome != Outcome.CONFLICT, '账户流水冲突')
        if row.outcome == Outcome.DUPLICATE or row.kind in ('shadow_input', 'layer_event', 'account_snapshot', 'paper_session', 'paper_fill_fee', 'settled_fill_fee'):
            continue
        _require(row.trading_day == intent.day, '跨交易日流水缺少连续基线')
        key, p = row.identity, row.payload
        if row.kind == 'order_intent':
            old = OrderIntent(**p)
            _valid_intent(old)
            _require(key == (old.strategy, old.decision_id) and old.account == intent.account
                     and old.day == intent.day and old.strategy == intent.strategy
                     and old.symbol == intent.symbol and old.config_version == intent.config_version
                     and old.decision_id != intent.decision_id, '意图归属、配置不符或决策号已用')
            intents[old.decision_id] = old
        elif row.kind == 'order_binding':
            decision = p['decision_id']
            _require(len(key) == 3 and decision in intents and decision not in bindings.values()
                     and p['strategy'] == intent.strategy and p['exchange'] == exchange, '绑定不唯一或归属错误')
            bindings[key] = decision
        elif row.kind == 'order':
            _require(len(key) == 6 and key[:3] in bindings, '流水有未绑定委托')
            orders[key[:3]].append(row)
        elif row.kind == 'fill':
            fills[key] = row
        else:
            raise GateDenied(f'未解决的拒单或未知流水：{row.kind}')
    _require(set(bindings.values()) == set(intents) and set(orders) == set(bindings), '存在未决意图或绑定')
    return intents, bindings, orders, fills


def _snapshot_orders(snapshot, limits, intents, bindings, history):
    result, systems = {}, {}
    for row in snapshot.orders:
        key = _local(row)
        _require(key in bindings and key not in result, '查询有未绑定或重复的委托')
        old = intents[bindings[key]]
        sys_id = row['OrderSysID'].strip()
        side = '0' if old.side == 'buy' else '1'
        offsets = ('0', '00') if old.purpose in ('open', 'add') else ('1', '11', '3', '33')
        offset, status, traded = row['CombOffsetFlag'], row['OrderStatus'], row['VolumeTraded']
        _require(row['ExchangeID'] == limits.exchange and sys_id and sys_id not in systems
                 and row['InstrumentID'] == old.symbol and row['Direction'] == side
                 and offset in offsets and row['LimitPrice'] == old.price
                 and row['VolumeTotalOriginal'] == old.lots and _lots(traded) and traded <= old.lots
                 and row['OrderPriceType'] == '2' and row['TimeCondition'] == '3'
                 and row['VolumeCondition'] == '1' and row['OrderSubmitStatus'] == '3', '委托与绑定意图/受理状态不符')
        valid = {'0': traded == old.lots, '1': 0 < traded < old.lots, '2': 0 < traded < old.lots,
                 '3': traded == 0, '4': traded == 0, '5': traded < old.lots}
        _require(valid.get(status), '委托状态与累计成交不符')
        current = False
        for fact in history[key]:
            p = fact.payload
            _require(p['symbol'] == old.symbol and p['direction'] == side and p['volume'] == old.lots
                     and p['price'] == old.price and p['offset'] == offset
                     and p['exchange'] in ('', limits.exchange) and p['order_sys_id'] in ('', sys_id)
                     and 0 <= p['traded'] <= traded, '历史委托事实不符')
            st, submit = fact.identity[3:5]
            if status in ('1', '3'):
                _require(submit not in ('1', '2', '4', '5', '6'), '活动委托存在撤改、拒单或未知终结')
            if st in ('0', '2', '4', '5'):
                _require(st == status and p['traded'] == traded, '终态矛盾')
            if (st, submit, p['traded'], p['order_sys_id'], p['exchange'], p['order_price_type'],
                    p['time_condition'], p['volume_condition']) == (
                    status, '3', traded, sys_id, limits.exchange, '2', '3', '1'):
                current = True
        _require(current, '当前委托尚未入账')
        result[key] = row
        systems[sys_id] = key
    _require(set(result) == set(bindings), '查询缺少已绑定的历史委托')
    return result, systems


def _snapshot_fills(snapshot, limits, facts, systems, orders):
    grouped, seen = defaultdict(list), set()
    for row in snapshot.trades:
        key = row['ExchangeID'], row['TradeID'].strip(), row['InstrumentID']
        sys_id = row['OrderSysID'].strip()
        _require(key[0] == limits.exchange and key in facts and key not in seen
                 and sys_id in systems, '成交缺少唯一绑定或流水')
        order_key = systems[sys_id]
        p = facts[key].payload
        ref = (row.get('OrderRef') or '').strip()
        _require((p['symbol'], p['order_sys_id'], p['order_ref'], p['direction'], p['offset'], p['price'], p['volume'])
                 == (key[2], sys_id, ref, row['Direction'], row['OffsetFlag'], row['Price'], row['Volume'])
                 and ref in ('', order_key[2]) and row['OffsetFlag'] == orders[order_key]['CombOffsetFlag'][0],
                 '成交查询与流水或开平不符')
        seen.add(key)
        grouped[order_key].append(facts[key])
    _require(seen == set(facts), '查询缺少历史成交流水')
    return grouped


def _project(intent, limits, intents, bindings, orders, grouped):
    keys = {decision: key for key, decision in bindings.items()}
    layers, active, direction = {}, [], None
    for decision, old in intents.items():  # 顺序是意图持久化顺序，不按成交号或层名排序
        key = keys[decision]
        o = orders[key]
        m = matched_fills(grouped[key], exchange=limits.exchange, sys_id=o['OrderSysID'].strip(),
                          near=limits.near, far=limits.far, combo=intent.symbol,
                          buy=old.side == 'buy', max_lots=old.lots)
        _require(not (m.residual_near or m.residual_far) and m.lots == o['VolumeTraded'], '未配对腿或累计不符')
        opening = old.purpose in ('open', 'add')
        if opening:
            if not any(layers.values()) and not active:
                direction = None
            _require(old.tier_id not in layers and direction in (None, old.side), '层重用或开仓方向不一致')
            direction = old.side
            layers[old.tier_id] = m.lots
        else:
            _require(old.purpose in ('take_profit', 'risk_exit') and old.tier_id in layers
                     and old.side != direction and old.lots <= layers[old.tier_id], '历史平仓不属于同层')
            layers[old.tier_id] -= m.lots
        if o['OrderStatus'] in ('1', '3'):
            _require(old.purpose == 'take_profit', '存在活动开仓或风险平仓单')
            active.append((old.tier_id, old.lots - m.lots))
    live = [tier for tier, qty in layers.items() if qty]
    if intent.purpose == 'open':
        _require(not live and not active and intent.tier_id not in layers,
                 '再次首仓前要求全部层已平、无活单且使用新的永久层号')
        return intent.side, 0, 0, 0
    _require(live, '没有已配对的实仓层')
    top = live[-1]
    if active:
        _require(active == [(top, layers[top])], '活动止盈不是唯一顶层全额保护')
    if intent.purpose == 'add':
        _require(active and intent.tier_id not in layers and intent.side == direction, '加仓须新层且已有顶层活动止盈')
    else:
        _require(not active and intent.tier_id == top and intent.side != direction
                 and intent.lots <= layers[top], '平仓须无活单且属于当前顶层可平量')
    return direction, sum(layers.values()), layers[top], sum(q for _, q in active)


def _positions(snapshot, limits, side, held, reserved):
    # CTP: long position 的卖平占用 ShortFrozen，short position 的买平占用 LongFrozen。
    expected = {(limits.near, '2' if side == 'buy' else '3'),
                (limits.far, '3' if side == 'buy' else '2')}
    amounts, freezes = Counter(), Counter()
    seen = set()
    for p in snapshot.positions:
        key = p['InstrumentID'], p['PosiDirection']
        qty, lf, sf = p['Position'], p['LongFrozen'], p['ShortFrozen']
        _require(all(_lots(x) for x in (qty, lf, sf)), '持仓/冻结数量未知')
        if not (qty or lf or sf):
            continue
        _require(key in expected and key not in seen and p['ExchangeID'] == limits.exchange
                 and p['HedgeFlag'] == '1' and p['TodayPosition'] == qty and p['YdPosition'] == 0,
                 '非本组合投机今仓、重复持仓行或混有外部持仓')
        seen.add(key)
        frozen, opposite = (sf, lf) if key[1] == '2' else (lf, sf)
        _require(opposite == 0, '不明反向冻结')
        amounts[key] += qty
        freezes[key] += frozen
    _require(all(amounts[k] == held and freezes[k] == reserved for k in expected), '持仓或冻结与层账不符')


def held_evidence(intent: OrderIntent, snapshot: QuerySnapshot, journal: Journal, limits: HeldLimits) -> Evidence:
    """返回仅供本轮封存的证据；未知事实、额度不足/不适用均抛 GateDenied。"""
    try:
        return _held(intent, snapshot, journal, limits)
    except GateDenied:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError) as exc:
        raise GateDenied(f'有仓核对事实缺失或异常：{exc}') from exc


def _held(intent, snapshot, journal, limits):
    r = snapshot.round
    _valid_intent(intent)
    _require(intent.purpose in ('open', 'add', 'take_profit', 'risk_exit') and
             (intent.account, intent.day, intent.strategy, intent.decision_id) ==
             (r.account, r.day, r.strategy, r.decision), '意图与有仓查询轮次不符')
    prefixes = {'DCE': 'SP', 'CZCE': 'SPD'}  # 本适配器已知的交易所内跨期 1:1 组合
    _require(all(_text(x) for x in (limits.exchange, limits.near, limits.far)) and limits.near != limits.far
             and limits.exchange in prefixes
             and intent.symbol == f'{prefixes[limits.exchange]} {limits.near}&{limits.far}', '配置与组合腿不符')
    _require(all(x is True for x in (limits.signal_current, limits.quotes_current, limits.session_open)),
             '信号、行情或时段无效')
    _require(_lots(limits.max_open_lots, 1), '容量未知')
    _require(len(snapshot.accounts) == 1 and snapshot.accounts[0]['CurrencyID'] == 'CNY', '非唯一人民币账户')
    for rows, investor in ((snapshot.accounts, 'AccountID'), (snapshot.positions, 'InvestorID'),
                           (snapshot.orders, 'InvestorID'), (snapshot.trades, 'InvestorID')):
        for row in rows:
            _require(account_key(row['BrokerID'], row[investor]) == r.account and row['TradingDay'] == r.day,
                     '快照账户或交易日不符')
    cash = snapshot.accounts[0]['Available']
    _require(_number(cash), '资金未知')
    intents, bindings, history, facts = _journal(intent, journal, limits.exchange)
    orders, systems = _snapshot_orders(snapshot, limits, intents, bindings, history)
    grouped = _snapshot_fills(snapshot, limits, facts, systems, orders)
    side, held, top_qty, reserved = _project(intent, limits, intents, bindings, orders, grouped)
    _positions(snapshot, limits, side, held, reserved)
    capacity = max(0, limits.max_open_lots - held)
    margin = 0
    if intent.purpose in ('open','add'):
        _require(_number(limits.margin_per_lot) and limits.margin_per_lot > 0, '新增保证金未知')
        margin = limits.margin_per_lot * intent.lots
        _require(capacity >= intent.lots and _number(margin) and cash >= margin, '总额度或资金不足')
    return Evidence(r, True, True, True, True, True, True, True, False,
                    capacity, top_qty - reserved, cash, margin)
