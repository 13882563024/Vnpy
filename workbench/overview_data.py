"""Read-only projection of the terminal, with optional offline layer/PnL data."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from math import isfinite
from time import monotonic
from types import SimpleNamespace

from utils.calendar import trading_day_str

UNKNOWN = '未知'
MAX_AGE = 300  # Same five-minute window as the reconciliation console.
# 平时约 10 秒刷新一次，给逐笔核验和请求排队留出余量。
VIEW_MAX_AGE = 20


class OverviewCacheBusy(RuntimeError):
    """A writer is publishing; this is not an unavailable or disconnected cabinet."""


def _number(value):
    if type(value) in (int, float) and isfinite(value) and abs(value) < 1e100:
        return value
    return None


def _money(value):
    return UNKNOWN if _number(value) is None else f'{value:,.2f}'


def _display_name(cfg, name):
    """扫描用短名：品种简称加组合腿，缺省才退回配置的完整显示名。"""
    legs = cfg.get('legs') or ()
    combo = '&'.join(leg.rsplit('.', 1)[0] for leg in legs if isinstance(leg, str))
    remark = cfg.get('remark')
    if isinstance(remark, str) and remark.strip() and combo:
        return f'{remark.strip()} {combo}'
    return cfg.get('display_name') or name


def _cached(engine, method):
    getter = getattr(engine, method, None)
    return tuple(getter()) if callable(getter) else ()


def _gateway(main):
    # Don't use MainEngine.get_gateway: the vn.py getter logs missing gateways.
    gateways = getattr(main, 'gateways', {})
    candidates = [(name, g) for name, g in gateways.copy().items() if getattr(g, 'td_api', None)]
    return candidates[0] if len(candidates) == 1 else ('', None)


def _fresh(td, field, now, max_age=MAX_AGE):
    stamp = _number(getattr(td, field, None))
    return stamp is not None and stamp > 0 and 0 <= now - stamp <= max_age


def _view_sides(td, now, scope):
    """展示用持仓：每次完整查询都会刷新，不受委托/成交回报作废的影响。"""
    sides = getattr(td, 'position_view_sides', None)
    if (isinstance(sides, dict) and getattr(td, 'position_view_account', None) == scope
            and _fresh(td, 'position_view_at', now, VIEW_MAX_AGE)):
        return sides
    return None


def _state(order):
    return getattr(getattr(order, 'status', None), 'name', '')


def _cabinet(td, now):
    """Take a coherent copy of already-published gateway data under its writer lock."""
    broker, user = getattr(td, 'brokerid', ''), getattr(td, 'userid', '')
    day = getattr(td, 'cabinet_trading_day', '')
    login = getattr(td, 'login_status', None)
    scope = (broker, user, day)
    scoped = login is True and all(scope) and getattr(td, '_login_scope', scope) == scope
    healthy = scoped and not getattr(td, 'reconciliation_event_error', '')
    sides = _view_sides(td, now, (broker, user)) if healthy else None
    if sides is None:
        # 视图快照不可用时退回核对快照（更严：要求账户/时效/无在途查询）。
        sides = getattr(td, 'position_snapshot_sides', None)
        if not (healthy and isinstance(sides, dict) and getattr(td, 'contract_inited', False)
                and not vars(td).get('_position_query_pending', vars(td).get('position_query_pending', False))
                and not getattr(td, '_position_query_failed', False)
                and getattr(td, 'position_snapshot_account', None) == (broker, user)
                and _fresh(td, 'position_snapshot_at', now)):
            sides = None
    ids = getattr(td, 'orders_snapshot', None)
    complete = bool(healthy and isinstance(ids, (set, frozenset)) and getattr(td, 'orders_snapshot_ok', False)
                    and not getattr(td, 'orders_query_pending', False) and _fresh(td, 'orders_snapshot_at', now))
    confirmed = set(ids) if complete else set()
    if healthy:
        confirmed.update(getattr(td, 'live_seen_orderids', ()))
    cache = {o.vt_orderid: o for o in getattr(td, '_order_cache', {}).copy().values()} if healthy else {}
    return scope if scoped else None, login, sides, complete, confirmed, cache


def _legs(symbols, sides):
    if len(symbols) != 2 or len(set(symbols)) != 2 or sides is None or '' in sides:
        return UNKNOWN, None
    parts, flat = [], True
    for leg in symbols:
        symbol = leg.rsplit('.', 1)[0]
        side = sides.get(symbol, {'long': 0, 'short': 0, 'unknown': 0})
        if not isinstance(side, dict):
            return UNKNOWN, None
        long, short, unknown = (_number(side.get(k)) for k in ('long', 'short', 'unknown'))
        if unknown != 0 or long is None or short is None or min(long, short) < 0:
            return UNKNOWN, None
        flat = flat and long == short == 0
        parts.append(f'{symbol} 多{long:g}/空{short:g}')
    return '；'.join(parts), flat


_PURPOSE = {'open': '开仓', 'add': '加仓', 'take_profit': '止盈',
            'close': '平仓', 'risk_exit': '风险平仓'}


def _order_purpose(order):
    """从委托引用里读出这笔单是干什么的；手工单单独标注。"""
    reference = str(getattr(order, 'reference', '') or '')
    if not reference.startswith('CrossPeriod'):
        return '手工'
    # 用途名自己带下划线（take_profit / risk_exit），只能用后缀匹配，不能 rsplit。
    for key in ('take_profit', 'risk_exit', 'close', 'open', 'add'):
        if reference.endswith('_' + key):
            return _PURPOSE[key]
    return '策略'


def _order_line(order):
    """一笔活动委托的可读描述：合约、方向开平、手数成交、价格、状态、时间、用途、单号。"""
    parts = [getattr(order, 'vt_symbol', '') or getattr(order, 'symbol', '') or '未知合约']
    direction = getattr(getattr(order, 'direction', None), 'value', '')
    offset = getattr(getattr(order, 'offset', None), 'value', '')
    parts.append(f'{direction}{offset}')
    volume = _number(getattr(order, 'volume', None))
    traded = _number(getattr(order, 'traded', None))
    if volume is not None:
        text = f'{volume:g}手'
        if traded is not None:
            text += f'（成交{traded:g}）'
        parts.append(text)
    price = _number(getattr(order, 'price', None))
    if price is not None and price > 0:
        parts.append(f'@{price:g}')
    parts.append(getattr(getattr(order, 'status', None), 'value', '') or _state(order) or '状态未知')
    moment = getattr(order, 'datetime', None)
    if hasattr(moment, 'strftime'):
        parts.append(moment.strftime('%H:%M:%S'))
    parts.append(_order_purpose(order))
    parts.append(str(getattr(order, 'vt_orderid', '') or ''))
    return ' · '.join(part for part in parts if part)


def _live_spread(spread):
    """实时价差：有双边报价取中间价，否则退最新价。"""
    if spread is None:
        return UNKNOWN
    bid = _number(getattr(spread, 'bid_price', None))
    ask = _number(getattr(spread, 'ask_price', None))
    if bid and ask:
        return f'{(bid + ask) / 2:g}'
    last = _number(getattr(spread, 'last_price', None))
    return f'{last:g}' if last else UNKNOWN


def _top_layer(strategy):
    layers = [layer for layer in (getattr(strategy, 'layers', ()) or ())
              if getattr(layer, 'is_open', True)]
    if not layers:
        return None
    return max(layers, key=lambda layer: getattr(layer, 'layer_index', 0) or 0)


def _take_profit_spread(strategy):
    """预挂止盈价差：顶层那一层算出来的止盈目标价。"""
    top = _top_layer(strategy)
    if top is None:
        return UNKNOWN
    value = _number(getattr(top, 'target_spread', None))
    return f'{value:g}' if value else UNKNOWN


def _add_spread(strategy, active):
    """加仓价差：优先真实的挂单价；没有挂单就用策略算出的"下一层触发价"。

    这个策略不预挂加仓单（触发时按对手价直接发），所以盘上通常没有价格可读；
    这里按它自己的入场规则回推：下一层阈值 = entry_k + layer_step × 当前层数，
    触发价 = mean ∓ 阈值 × sd。
    """
    for order in active.values():
        if getattr(getattr(order, 'offset', None), 'name', '') == 'OPEN':
            value = _number(getattr(order, 'price', None))
            if value:
                return f'{value:g}'
    pending = getattr(strategy, 'pending_open', None)
    if pending is not None and getattr(pending, 'kind', '') == 'open':
        value = _number(getattr(pending, 'price', None))
        if value:
            return f'{value:g}'
    if strategy is None:
        return UNKNOWN
    if getattr(strategy, 'paused', False):
        return '已暂停'
    if getattr(strategy, 'close_only', False):
        return '只平'
    layers = [layer for layer in (getattr(strategy, 'layers', ()) or ())
              if getattr(layer, 'is_open', True)]
    cap = _number(getattr(strategy, 'max_positions', None))
    if cap and len(layers) >= int(cap):
        return '已满层'
    stat = getattr(strategy, 'stat', None)
    mean = stat.mean() if callable(getattr(stat, 'mean', None)) else None
    sd = stat.sd() if callable(getattr(stat, 'sd', None)) else None
    if _number(mean) is None or _number(sd) is None or sd <= 0:
        return UNKNOWN
    top = max((getattr(layer, 'layer_index', 0) or 0 for layer in layers), default=0)
    units = (float(getattr(strategy, 'entry_k', 0) or 0)
             + float(getattr(strategy, 'layer_step', 0) or 0) * top)
    band = units * sd
    direction = getattr(strategy, 'direction', 0) or 0
    if direction > 0:
        return f'{mean - band:g}'
    if direction < 0:
        return f'{mean + band:g}'
    return f'{mean - band:g} / {mean + band:g}'


def _layer_legs(strategy, legs):
    """当前（顶层）那一层的成交价：层号/方向手数、价差均价、两腿当时的成交价。"""
    top = _top_layer(strategy)
    if top is None:
        return '无仓'
    side = '多' if (getattr(top, 'direction', 0) or 0) > 0 else '空'
    parts = [f'第{getattr(top, "layer_index", "?")}层', f'{side}{getattr(top, "volume", 0):g}手']
    average = _number(getattr(top, 'avg_spread', None))
    if average is not None:
        parts.append(f'价差{average:g}')
    prices = []
    for index, symbol in enumerate(tuple(legs)[:2]):
        name = str(symbol).rsplit('.', 1)[0]
        value = _number(getattr(top, 'near_price' if index == 0 else 'far_price', None))
        prices.append(f'{name} {value:g}' if value else f'{name} —')
    if prices:
        parts.append(' / '.join(prices))
    return ' · '.join(parts)


def _pnl_text(value):
    number = _number(value)
    return UNKNOWN if number is None else f'{number:+,.2f}'


def _latest_log(strategy):
    """策略自己留的最后一条日志（跳过每秒刷的"状态"行）。"""
    getter = getattr(strategy, 'latest_log', None)
    if not callable(getter):
        return UNKNOWN
    try:
        text = ' '.join(str(getter() or '').split())
    except Exception:
        return UNKNOWN
    return text or UNKNOWN


def _permission_text(by_name):
    """顶部权限行的真实口径：按各价差当前的实盘开关统计，而不是一句恒定的"未知"。"""
    if not by_name:
        return '只读 · 未取得策略状态'
    live = close_only = paused = off = 0
    for strategy in by_name.values():
        if getattr(strategy, 'paused', False):
            paused += 1
        elif getattr(strategy, 'close_only', False):
            close_only += 1
        elif getattr(strategy, 'live_trading', None) is True:
            live += 1
        else:
            off += 1
    parts = [f'实盘发单 {live}']
    if close_only:
        parts.append(f'只平 {close_only}')
    if paused:
        parts.append(f'暂停 {paused}')
    if off:
        parts.append(f'未开启 {off}')
    return ' · '.join(parts) + '（本页只读）'


def _order_rows(symbols, combo, orders, confirmed, complete, gateway):
    active = {}
    unresolved = False
    for oid in confirmed | orders.keys():
        order = orders.get(oid)
        if order is None or getattr(order, 'gateway_name', None) != gateway:
            unresolved = True
            continue
        state = _state(order)
        if state in ('ALLTRADED', 'CANCELLED', 'REJECTED'):
            continue
        vt_symbol = getattr(order, 'vt_symbol', '')
        if not vt_symbol:
            unresolved = True
        elif vt_symbol in (*symbols, combo):
            if state in ('NOTTRADED', 'PARTTRADED') and oid in confirmed:
                active[oid] = order
            else:
                unresolved = True
    rows = sorted(active.values(), key=lambda o: (
        getattr(o, 'datetime', None) is None,
        getattr(o, 'datetime', None) or datetime.min,
        str(getattr(o, 'vt_orderid', '') or '')))
    lines = [_order_line(o) for o in rows]
    certain = complete and not unresolved
    if not certain:
        lines.append('活动委托未核全 · 未知')
    return active, certain, '\n'.join(lines) or '无活动委托（柜台快照）'


def _strategy_row(strategy, active, certain, combo, flat, algo_map, cfg):
    config_only = cfg.get('strategy') or {}
    opening = '配置：只平' if config_only.get('close_only') is True else '实盘权限未知'
    if strategy is None:
        return UNKNOWN, '无仓' if flat is True else UNKNOWN, opening, 2, '策略记录未知', ''
    if getattr(strategy, 'paused', False):
        opening = '策略已暂停'
    elif getattr(strategy, 'close_only', False):
        opening = '策略只平'
    elif getattr(strategy, 'live_trading', None) is False:
        opening = '策略未开启实盘'
    elif getattr(strategy, 'live_trading', None) is True:
        opening = '实盘可开仓'
    if getattr(strategy, 'pending_open', None) or getattr(strategy, 'reserved_open_orders', None):
        opening += ' · 开仓未决'
    layers = getattr(strategy, 'layers', None)
    if not isinstance(layers, (tuple, list)):
        return UNKNOWN, UNKNOWN, opening, 2, '层记录未知', ''
    layers = tuple(layers)
    cap = getattr(strategy, 'max_positions', config_only.get('max_positions', UNKNOWN))
    direction = {1: '多', -1: '空'}.get(getattr(strategy, 'direction', None), '')
    summary = f'本地 {direction}{len(layers)}/{cap} 层'
    reserved = getattr(strategy, 'reserved_exit_orders', {}).copy()
    pending = bool(getattr(strategy, 'pending_close', None) or getattr(strategy, 'pause_reconciliation_required', False))
    if not layers:
        if pending or reserved or getattr(strategy, 'exit_algoid', ''):
            return summary, '平仓未决', opening, 1, '平仓记录待核对', ''
        if flat is True:
            return summary, '无仓', opening, 3 if certain and not active else 2, '柜台两腿无仓', ''
        return summary, UNKNOWN, opening, 1 if flat is False else 2, '柜台持仓归属未知', ''
    indices = [getattr(layer, 'layer_index', None) for layer in layers]
    ids = [getattr(layer, 'tier_id', '') for layer in layers]
    if any(type(i) is not int or i <= 0 for i in indices) or len(set(indices)) != len(indices) or not all(ids) or len(set(ids)) != len(ids):
        return summary, '顶层未知', opening, 1, '层身份待核对', ''
    top = layers[indices.index(max(indices))]
    lower = '；低层按策略不单独挂止盈' if len(layers) > 1 else ''
    detail = f'本地顶层 {top.layer_index} · {top.tier_id}{lower}'
    for layer in sorted(layers, key=lambda item: item.layer_index, reverse=True):
        cost = _money(getattr(layer, 'avg_spread', None))
        target = _money(getattr(layer, 'target_spread', None))
        opened = getattr(layer, 'open_datetime', None) or getattr(layer, 'open_trading_day', '') or UNKNOWN
        detail += f'\n第{layer.layer_index}层 · {layer.tier_id} · 成本 {cost} · 目标 {target} · 开层 {opened}'
    expected = _number(getattr(top, 'volume', None))
    detail += f'\n顶层应挂 {expected if expected is not None else UNKNOWN} 手（按本地层量）'
    top_ids = [oid for oid, tier in reserved.items() if tier == top.tier_id]
    if flat is True:
        return summary, '层仓不符', opening, 0, '本地有层、柜台两腿无仓', detail
    if pending or any(tier != top.tier_id for tier in reserved.values()):
        return summary, '平仓未决', opening, 1, '平仓意图或层归属待核对', detail
    job = getattr(strategy, 'jobs', {}).get(getattr(top, 'algoid', ''))
    if (job and getattr(job, 'kind', '') == 'take_profit' and not getattr(job, 'done', False)
            and getattr(job, 'tier_id', None) == top.tier_id):
        top_ids.extend(oid for oid, algo in algo_map.items() if getattr(algo, 'algoid', None) == top.algoid)
    for oid in set(top_ids):
        order = active.get(oid)
        if (order and getattr(order, 'vt_symbol', '') == combo and getattr(getattr(order, 'offset', None), 'name', '') in
                ('CLOSE', 'CLOSETODAY', 'CLOSEYESTERDAY')):
            remaining = (_number(getattr(order, 'volume', None)), _number(getattr(order, 'traded', None)))
            if None not in remaining:
                detail += f'\n顶层已归属实挂 {remaining[0] - remaining[1]:g} 手 · {oid}'
            direction = {1: 'SHORT', -1: 'LONG'}.get(getattr(top, 'direction', None))
            if (certain and flat is False and set(top_ids) == {oid} and set(active) == {oid}
                    and direction and getattr(getattr(order, 'direction', None), 'name', '') == direction
                    and None not in remaining and remaining[0] - remaining[1] == getattr(top, 'volume', None)
                    and _number(getattr(order, 'price', None)) is not None and order.price == getattr(top, 'target_spread', None)):
                return summary, '顶层止盈已挂', opening, 3, '顶层止盈单已核对', detail
            return summary, '顶层有平仓挂单', opening, 1, '数量、方向与止盈价待核对', detail
    if top_ids or getattr(top, 'algoid', '') or getattr(strategy, 'exit_algoid', '') or active:
        return summary, '顶层止盈未决', opening, 1, '挂单与顶层关系待核对', detail
    if certain:
        return summary, '顶层未见止盈单', opening, 0, '顶层缺少活动止盈单', detail
    return summary, '顶层止盈未知', opening, 2, '未取得完整委托证据', detail


def _local_layers(rows):
    layers = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get('state') == 'CLOSED':
            continue
        index, volume, direction = (row.get(k) for k in ('layer_index', 'volume', 'direction'))
        if type(index) is not int or index < 1 or _number(volume) is None or volume <= 0:
            continue
        if type(direction) is not int or direction not in (-1, 1):
            continue
        layers.append(SimpleNamespace(**row))
    return layers


def overview_view(configs: dict, main_engine=None, spread_engine=None, *, read_local=None) -> dict:
    """Build display strings; priority 0 is most urgent. Config never grants permission."""
    view = dict.fromkeys(('account', 'day', 'connection', 'balance', 'available', 'margin', 'realized', 'unrealized'), UNKNOWN)
    gateway, gw = _gateway(main_engine)
    td = getattr(gw, 'td_api', None)
    lock = getattr(td, '_send_lock', None)
    if lock is not None and not lock.acquire(blocking=False):
        raise OverviewCacheBusy('网关缓存正在更新')
    try:
        scope, login, sides, complete, confirmed, orders = _cabinet(td, monotonic())
    finally:
        if lock is not None:
            lock.release()
    view.update(source='仅配置' if main_engine is None and spread_engine is None else
                ('纸面柜台（模拟）' if gateway == 'PAPER' else f'{gateway} 终端缓存' if gateway else '本地引擎缓存'),
                permission='只读 · 未取得策略状态', updated=datetime.now().strftime('视图 %H:%M:%S'), rows=[])
    if login is not None:
        view['connection'] = '交易端已登录' if login is True else '交易端未登录'
    if not scope and login is not True and (main_engine is None or gateway):
        # Not logged in: one plain statement beats five bare "unknown" figures.
        for key in ('balance', 'available', 'margin', 'realized', 'unrealized'):
            view[key] = '未连接柜台'
        if login is None:
            view['connection'] = '柜台状态未知'
    if scope:
        broker, user, view['day'] = scope
        view['account'] = f'{broker} / {user}'
        accounts = [a for a in _cached(main_engine, 'get_all_accounts') if getattr(a, 'gateway_name', None) == gateway
                    and getattr(a, 'accountid', None) == user]
        if len(accounts) == 1:
            a = accounts[0]
            for key, field in [('balance', 'balance'), ('available', 'available'), ('margin', 'curr_margin'),
                               ('realized', 'close_profit'), ('unrealized', 'position_profit')]:
                view[key] = _money(getattr(a, field, None))
    offline = not scope and login is not True and read_local is not None
    layer_book, pnl_book = read_local() if offline else ({}, {})
    local_day = trading_day_str(datetime.now()) if offline else ''
    if offline:
        view['source'] = '本地缓存（未连接柜台）'
        view['connection'] = '交易端未登录'
        for key in ('balance', 'available', 'margin', 'realized', 'unrealized'):
            view[key] = '未连接柜台'
    strategies = getattr(getattr(spread_engine, 'strategy_engine', None), 'strategies', {}).copy()
    strategy_by_name: dict[str, object] = {}
    for name in configs:
        candidates = [s for k, s in strategies.items() if getattr(s, 'spread_name', None) == name or
                      (not getattr(s, 'spread_name', None) and k == f'CrossPeriodMRStrategy_{name}')]
        if len(candidates) == 1:
            strategy_by_name[name] = candidates[0]
    view['permission'] = '只读 · 离线' if offline else _permission_text(strategy_by_name)
    leg_counts = Counter(s for cfg in configs.values() for s in cfg.get('legs', ()))
    for name, cfg in configs.items():
        legs = tuple(cfg.get('legs', ()))
        shared = any(leg_counts[s] > 1 for s in legs)
        combo = f"{cfg.get('exchange_spread', '')}.{cfg.get('exchange', '')}"
        active, certain, order_text = _order_rows(legs, combo, orders, confirmed, complete, gateway)
        leg_text, flat = _legs(legs, sides)
        strategy = strategy_by_name.get(name)
        getter = getattr(spread_engine, 'get_spread', None)
        spread = getter(name) if callable(getter) else None
        algo_map = getattr(getattr(spread_engine, 'algo_engine', None), 'order_algo_map', {}).copy()
        local = tuple(getattr(strategy, 'layers', ()) or ())
        held = False if flat is True and not local else None
        if flat is False and not shared and local:
            volumes = [_number(getattr(x, 'volume', None)) for x in local]
            dirs = {getattr(x, 'direction', None) for x in local}
            if len(dirs) == 1 and dirs <= {-1, 1} and all(v is not None and v > 0 for v in volumes):
                total = sum(volumes)
                a, b = (sides.get(s.rsplit('.', 1)[0], {}) for s in legs)
                buy, sell = ('long', 'short') if dirs == {1} else ('short', 'long')
                if a.get(buy) == b.get(sell) == total and a.get(sell) == b.get(buy) == 0:
                    held = True
        layers, protection, opening, priority, status, extra = _strategy_row(strategy, active, certain, combo, flat, algo_map, cfg)
        if protection == '顶层止盈已挂' and held is not True:
            protection, priority, status = '顶层有平仓挂单', 1, '柜台腿仓归属待核对'
        detail = '\n'.join(filter(None, ('配置腿：' + ' / '.join(legs), '柜台腿仓：' + leg_text,
                                      '共享合约：账户腿仓不能归属本价差' if shared else '', extra)))
        if '未决' in opening and priority > 1:
            status, priority = '开仓未决', 1
        leg_label = (f'近 {legs[0]} / 远 {legs[1]} · 仓位未知'
                     if len(legs) == 2 and leg_text == UNKNOWN else leg_text)
        prices = _layer_legs(strategy, legs)
        live_price = _live_spread(spread)
        target = _take_profit_spread(strategy)
        add = _add_spread(strategy, active)
        total = getattr(strategy, 'total_realized', None)
        today = getattr(strategy, 'daily_realized', None)
        if offline:
            cached_layers = _local_layers(layer_book.get(name)) if name in layer_book else local
            display_book = SimpleNamespace(layers=cached_layers)
            known_empty = layer_book.get(name) == []
            layers = f'本地 {len(cached_layers)} 层' if cached_layers or known_empty else UNKNOWN
            prices = _layer_legs(display_book, legs) if cached_layers else UNKNOWN
            target = _take_profit_spread(display_book)
            live_price = add = UNKNOWN
            opening, protection, priority = '离线', '柜台未核实', 2
            status = '本地层账 · 待核对' if cached_layers else '离线 · 待核对'
            entry = pnl_book.get(name)
            total = entry.get('total') if isinstance(entry, dict) else None
            today = entry.get('today') if isinstance(entry, dict) and entry.get('day') == local_day else None
            detail = '\n'.join(('配置腿：' + ' / '.join(legs), '柜台持仓未核实；以下为本地记录。',
                               *(_layer_legs(SimpleNamespace(layers=[layer]), legs)
                                 for layer in sorted(cached_layers, key=lambda x: x.layer_index, reverse=True))))
            order_text = '未连接柜台，无法确认当前活动委托。'
        else:
            order_text += '\n仅列活动委托，不含成交明细'
        view['rows'].append(dict(name=name, display=_display_name(cfg, name), held=held, layers=layers,
                                 legs=prices, live=live_price, take_profit=target, add=add,
                                 log=_latest_log(strategy),
                                 total=_pnl_text(total), today=_pnl_text(today),
                                 cabinet=leg_label, protection=protection, opening=opening,
                                 priority=priority, status=status,
                                 detail=detail, orders=order_text))
    return view
