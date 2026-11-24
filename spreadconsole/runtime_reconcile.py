"""Runtime position and top-layer take-profit watchdog (vn.py event thread)."""
from __future__ import annotations

from contextlib import nullcontext
from time import monotonic


class RuntimeReconciler:
    INTERVAL = 30.0
    QUERY_WINDOW = 24.0

    def __init__(self, control, *, startup_busy, clock=monotonic):
        self.control = control
        self.startup_busy = startup_busy
        self.clock = clock
        self._scope = None
        self._next = 0.0
        self._round = None
        self._messages = {}

    @property
    def busy(self) -> bool:
        return self._round is not None

    def _notice(self, name, message):
        if not message:
            self._messages.pop(name, None)
        elif self._messages.get(name) != message:
            self._messages[name] = message
            writer = getattr(self.control.main_engine, 'write_log', None)
            if callable(writer):
                writer(f'{name}〔运行兜底〕{message}')

    def on_timer(self, event=None):
        control = self.control
        td = getattr(control._gateway(), 'td_api', None)
        scope = getattr(td, 'account_cache_token', None)
        if not (control.master_running and control.td_ready() and control.md_ready()
                and scope is not None):
            self._scope = self._round = None
            self._messages.clear()
            return
        if scope is not self._scope:
            self._scope, self._round, self._next = scope, None, 0.0
            self._messages.clear()
        try:
            if self.startup_busy():
                self._round = None  # 人工核对和预启动查询优先。
            else:
                self._poll(td)
            for name in sorted(control.enabled):
                strategy = control._strategy(name)
                check = getattr(strategy, 'check_take_profit', None)
                if callable(check):
                    check()
        except Exception as exc:
            self._round = None
            self._next = self.clock() + self.INTERVAL
            self._notice('柜台核对', f'本轮未完成：{exc}')

    def _poll(self, td):
        control = self.control
        request = getattr(td, 'request_runtime_check', None)
        if not callable(request) or not getattr(td, 'contract_inited', False):
            return
        now = self.clock()
        # 仅在明确启动且至少一项策略获实盘许可时工作；不会代替人工授权或恢复停用策略。
        configs = control.service.list()
        names = [name for name in sorted(control.enabled & configs.keys())
                 if getattr(control._strategy(name), 'live_trading', False)]
        if not names:
            self._round = None
            return
        if self._round is None:
            if now < self._next:
                return
            self._next = now + self.INTERVAL
            self._round = {'start': now, 'phase': 'position'}
        r = self._round
        if now - r['start'] > self.QUERY_WINDOW:
            self._round = None
            return
        with getattr(td, '_send_lock', nullcontext()):
            if not control.master_running or td.account_cache_token is not self._scope:
                self._round = None
                return
            phase = r['phase']
            if phase == 'position':
                previous = td.position_query_generation
                if request('position'):
                    r.update(phase='orders', previous=previous)
            elif phase == 'orders':
                if td.position_query_pending:
                    return
                if (control._snapshot_problem(td)
                        or td.position_query_generation != r['previous'] + 1):
                    self._round = None
                    return
                reserved = {name: control._reserved_ids(control._strategy(name)) for name in names}
                if request('orders'):
                    r.update(phase='apply', position_at=td.position_snapshot_at,
                             activity=td._position_activity_generation, reserved=reserved)
            else:
                if td.orders_query_pending:
                    return
                self._round = None
                if (control._snapshot_problem(td) or control._orders_snapshot_problem(td)
                        or td.position_snapshot_at != r['position_at']
                        or td.orders_snapshot_at < r['position_at']
                        or r['activity'] != td._position_activity_generation
                        or r['activity'] != td.orders_snapshot_activity_generation):
                    return  # 两份快照之间或之后又有委托/成交，不能拿它们改账。
                self._notice('柜台核对', '')
                for name in names:
                    try:
                        self._apply(td, configs[name], configs, r['reserved'].get(name, ()))
                    except Exception as exc:
                        self._notice(name, f'未自动纠正：{exc}')

    def _apply(self, td, config, configs, reserved):
        control = self.control
        name = config.name
        spread = control.spread_engine.get_spread(name)
        strategy = control._strategy(name)
        if spread is None or (strategy is None or strategy.paused or not strategy.live_trading):
            return
        sides = control._cabinet_leg_differences(config, spread)
        symbols = set(config.legs + [config.exchange_spread_vt_symbol])
        active = [order for order in td.orders_snapshot_active.values() if order.vt_symbol in symbols]
        algos = getattr(strategy, '_owned_algos', lambda: [])()
        owned = {oid for algo in algos if not str(algo.algoid).startswith('recovered_')
                 for oid in getattr(algo, 'orders', {})}
        reserved = set(reserved) - owned
        # 先去掉柜台已无此笔委托的历史占位；只处理查询发起前已存在的占位，
        # 不把查询窗口中新产生的在途委托误判成“柜台没有”。
        pruned = control._prune_orders_absent_from_cabinet(
            name, config, reserved_before_query=reserved & control._reserved_ids(strategy))
        # 当日报单包含终态，终态不该继续占着止盈/开仓槽位。
        # 仅释放本轮明确核到的、查询发起前的占位，不仅凭 OMS 没有就释放。
        terminal_ids = td.orders_snapshot - td.orders_snapshot_active.keys()
        released = set()
        for field in ('reserved_exit_orders', 'reserved_open_orders'):
            slots = getattr(strategy, field, {})
            for orderid in reserved & slots.keys() & terminal_ids:
                slots.pop(orderid)
                released.add(orderid)
        if pruned or released:
            remaining_slots = control._reserved_ids(strategy)
            for layer in strategy.layers:
                label = getattr(layer, 'algoid', '')
                if label in reserved and label not in remaining_slots:
                    layer.algoid = ''
            control._remember_orders()
        discrepancy = (sides is None or any(abs(leg['difference']) > 1e-9
                       or (leg['long'] > 1e-9 and leg['short'] > 1e-9) for leg in sides)
                       or (bool(strategy.layers or getattr(spread, 'net_pos', 0))
                           and not (control._layers_match_net(spread, strategy)
                                    and control._layers_well_formed(spread, strategy))))
        if not discrepancy:
            # 外部活动委托也要占槽；未知层归属用空 tier，不假定它是策略自己的顶层保护。
            from vnpy.trader.constant import Offset
            top = max(strategy.layers, key=lambda layer: layer.layer_index, default=None)
            for order in active:
                if order.vt_orderid in owned:
                    continue
                if order.offset == Offset.OPEN:
                    strategy.reserved_open_orders.setdefault(order.vt_orderid, '')
                elif order.offset in (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY):
                    tier = strategy.reserved_exit_orders.get(order.vt_orderid)
                    if not tier or tier != getattr(top, 'tier_id', None):
                        strategy.reserved_exit_orders[order.vt_orderid] = ''
            strategy.runtime_position_blocked = False
            self._notice(name, '')
            exit_algoid = getattr(strategy, 'exit_algoid', '')
            exit_orders = {oid for algo in algos if algo.algoid == exit_algoid
                           for oid in getattr(algo, 'orders', {})}
            active_ids = {order.vt_orderid for order in active}
            self._notice(f'{name} 止盈', (
                '柜台未见策略止盈算法的活动委托；保留原订单/成交路由，待终态或成交归属确认后复查'
                if top is not None and exit_orders and exit_orders.isdisjoint(active_ids) else ''))
            return
        strategy.runtime_position_blocked = True
        if (sides is None or getattr(td, 'journal_error', '')
                or any(set(config.legs) & set(other.legs)
                       for key, other in configs.items() if key != name)):
            self._notice(name, '仓位数据无法唯一归属；已阻止本策略新发单，需人工核对')
            return
        # 不撤单、不重置在跑的算法。即使仓位差异确定，也让已有订单/成交正常回报。
        state = strategy.get_pause_state()
        if (active or state['active_algoids'] or getattr(strategy, 'pending_open', None)
                or getattr(strategy, 'pending_close', None)
                or any(o.vt_symbol in symbols for o in control.main_engine.get_all_active_orders())):
            self._notice(name, '账实不符且仍有委托或算法在途；已阻止本策略新发单，待其终结后复查')
            return
        message = control.sync_spread_position(name, force=False, runtime=True)
        if strategy.live_trading:
            strategy.runtime_position_blocked = False
        self._notice(name, message)
