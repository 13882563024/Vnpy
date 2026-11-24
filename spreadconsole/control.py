"""价差管理窗口的交易门禁；不调用 vn.py 会撤单的 stop_strategy。"""
from __future__ import annotations

from datetime import date, datetime
from time import monotonic, time
from typing import Any

from spreadconsole.state import load_state, save_state
from utils.managed_spreads import ManagedSpreadError, ManagedSpreadService
from utils.spread_config import SpreadConfig

# 交易关闭期间柜台仓位只可能被人工改动；给足阅读/确认时间，真正的时效由
# “快照之后是否又收到委托/成交回报”保证，而不是一个很短的秒数。
SNAPSHOT_MAX_AGE = 300.0


class SpreadTradingControl:
    """一个 GUI 进程的总交易开关和逐合约许可；默认全部关闭。"""

    def __init__(self, main_engine, spread_engine, service: ManagedSpreadService | None = None,
                 state_path=None):
        self.main_engine = main_engine
        self.spread_engine = spread_engine
        self.service = service or ManagedSpreadService()
        self._state_path = state_path
        self._state: dict[str, Any] = load_state(state_path)
        # 上次会话保存的核对快照与委托记录；重启后作为“已知事实”，不冒充本次查询。
        self._saved_reconciliation: dict[str, dict] = dict(
            self._state.get("reconciliation") or {})
        self._saved_orders: dict[str, list] = dict(self._state.get("orders") or {})
        self.master_running = False
        # 逐合约核对：OI 有持仓不应阻止另一个干净的合约交易。
        self.reconciled_contracts: dict[str, datetime] = {}
        self._last_td_ready = False
        # “允许此合约交易”是用户意图，写入 spreads.json，重启后恢复。
        try:
            self.enabled: set[str] = {
                name for name, config in self.service.list().items()
                if config.trading_enabled
            }
        except Exception:
            self.enabled = set()
        self.errors: dict[str, str] = {}
        self._last_md_ready = False
        self._last_account_key = None
        self._reconciliation_snapshot_generation: int | None = None
        self._reconciliation_snapshot_account: tuple[str, str] | None = None
        self._reconciliation_snapshot_at: float | None = None
        self._registry_names: frozenset[str] = frozenset()
        # 待命合约（无截止时间）：已初始化，但 live_trading=False，不会新发单。
        self.armed: dict[str, float] = {}
        self._armed_session: tuple | None = None
        self.arm_blockers: dict[str, str] = {}
        self._startup_query_done = False
        self._orders_restored = False
        self._recovered_orderids: set[str] = set()
        # 恢复出来却查不到原始时间的委托：无法证明仍是当日有效单，按历史记录处理。
        self._moment_unknown: set[str] = set()
        self._startup_pause()

    @property
    def reconciliation_confirmed(self) -> bool:
        """兼容旧调用：只要还有任一合约处于已核对状态即为 True。"""
        return bool(self.reconciled_contracts)

    @property
    def last_reconciliation_at(self) -> datetime | None:
        return max(self.reconciled_contracts.values(), default=None)

    def cabinet_trading_day(self) -> str:
        """柜台登录回报里的权威交易日（``YYYY-MM-DD``）；拿不到时返回空串。"""
        td = getattr(self._gateway(), "td_api", None)
        value = str(getattr(td, "cabinet_trading_day", "") or "")
        return value if len(value) == 10 else ""

    def current_trading_day(self, now: datetime | None = None) -> str:
        """当前交易日：**优先用柜台回报**，缺失时才按本地日历推算。

        柜台认法定节假日，本地推算只跳周末；两者不一致时以柜台为准。
        """
        from utils.calendar import trading_day_of

        cabinet = self.cabinet_trading_day()
        if cabinet:
            return cabinet
        return trading_day_of(now or datetime.now()).isoformat()

    def is_reconciled(self, name: str) -> bool:
        # 历史确认只用于审计，不授权新会话，也不覆盖新成交造成的失效。
        return name in self.reconciled_contracts and not self._snapshot_problem()

    def _persist(self) -> None:
        """把核对快照与遗留委托写盘；失败只在日志提示，不影响当前操作。"""
        self._state["reconciliation"] = self._saved_reconciliation
        self._state["orders"] = self._saved_orders
        if not save_state(self._state, self._state_path):
            writer = getattr(self.main_engine, "write_log", None)
            if callable(writer):
                writer("提示：核对/委托状态写入本地文件失败，重启后可能丢失这些记录")

    def _engine_cache(self, attr: str):
        """取引擎的 ``orders`` / ``trades`` 缓存字典（在 OmsEngine 上，不在 MainEngine）。

        早先直接读 ``main_engine.orders`` 永远是 None，跨日清缓存从来没真正执行过
        （2026-11-17 20:49 定时清理报“隔夜委托 0 笔”，而白天还挂着 7 张未成交
        止盈单，20:55 核对因此全被判“旧算法仍在途”）。
        """
        getter = getattr(self.main_engine, "get_engine", None)
        if callable(getter):
            try:
                engine = getter("oms")
            except Exception:
                engine = None
            holder = getattr(engine, attr, None) if engine is not None else None
            if isinstance(holder, dict):
                return holder
        holder = getattr(self.main_engine, attr, None)
        return holder if isinstance(holder, dict) else None

    def purge_stale_caches(self, *, now: datetime | None = None) -> dict[str, int]:
        """清掉跨交易日的委托/成交缓存，让开盘前拿到干净状态。

        CTP 委托**当日有效**：程序跨日不重启时，引擎 OMS 与本地快照仍留着昨天的
        "未成交"委托。这些幽灵委托会让 ``_bind_existing_exit_orders`` 误以为止盈单
        还挂在柜台上，于是该层再也不补挂（2026-11-17 实测：11 个价差全天无单）。

        只丢**非当前交易日**的引擎委托；策略的柜台占位一并清空——连接后
        ``restore_orders`` / 核对会按柜台真实回报重建。
        """
        from utils.calendar import trading_day_of

        current = self.current_trading_day(now)
        engine_dropped = 0
        trade_dropped = 0
        reserved_dropped = 0
        algos_dropped = 0

        orders = self._engine_cache("orders")
        if isinstance(orders, dict):
            for orderid, order in list(orders.items()):
                moment = getattr(order, "datetime", None)
                if moment is None or getattr(moment, "tzinfo", None) is None:
                    # 时间不可信（本机时钟补的）：无法证明它属于当前交易日，按历史记录丢弃。
                    orders.pop(orderid, None)
                    engine_dropped += 1
                    continue
                if trading_day_of(moment).isoformat() == current:
                    continue
                orders.pop(orderid, None)
                engine_dropped += 1

        trades = self._engine_cache("trades")
        if isinstance(trades, dict):
            for key, trade in list(trades.items()):
                moment = getattr(trade, "datetime", None)
                if moment is not None and getattr(moment, "tzinfo", None) is not None:
                    if trading_day_of(moment).isoformat() == current:
                        continue
                trades.pop(key, None)
                trade_dropped += 1

        # 缓存清完同步本地“已恢复委托”集合，避免留下指向已删委托的 id。
        live_ids = set(orders) if isinstance(orders, dict) else set()
        self._recovered_orderids &= live_ids
        self._moment_unknown &= self._recovered_orderids

        engine = getattr(self.spread_engine, "strategy_engine", None)
        strategies = getattr(engine, "strategies", {}) if engine is not None else {}
        for strategy in list(strategies.values()):
            for field in ("reserved_exit_orders", "reserved_open_orders"):
                holder = getattr(strategy, field, None)
                if isinstance(holder, dict) and holder:
                    reserved_dropped += len(holder)
                    holder.clear()
            algos_dropped += self._archive_stale_algo_slots(strategy, current)

        self._remember_orders()      # 按清理后的 OMS 重建本地快照
        return {"engine_orders": engine_dropped, "engine_trades": trade_dropped,
                "reserved": reserved_dropped, "algos": algos_dropped}

    def _archive_stale_algo_slots(self, strategy, current: str) -> int:
        """把只剩旧交易日委托的算法占位从策略上摘掉，返回摘掉的数量。

        不清会让 ``sync_spread_position`` 永远报“旧算法仍在途或状态未知”，
        “核对并纠正”跨日后一次都成功不了。
        """
        from utils.calendar import trading_day_of

        algoids = getattr(strategy, "algoids", None)
        if not isinstance(algoids, set) or not algoids:
            return 0
        if not getattr(strategy, "paused", False):
            return 0        # 运行中的算法不动，等它自己结束
        algo_engine = getattr(self.spread_engine, "algo_engine", None)
        algos = getattr(algo_engine, "algos", None)
        total = len(algoids)
        stale: list[str] = []
        for algoid in list(algoids):
            algo = algos.get(algoid) if isinstance(algos, dict) else None
            if algo is None:
                stale.append(algoid)          # 引擎里已经没有它，只剩本地占位
                continue
            orders = list((getattr(algo, "orders", None) or {}).values())
            if not orders:
                stale.append(algoid)
                continue
            if all(trading_day_of(moment).isoformat() != current
                   for moment in (getattr(order, "datetime", None) for order in orders)
                   if moment is not None):
                stale.append(algoid)
        for algoid in stale:
            algoids.discard(algoid)
        if stale and len(stale) == total:
            archiver = getattr(strategy, "_archive_stale_algos", None)
            if callable(archiver):
                archiver()      # 全部是旧算法：连 jobs/exit_algoid 一并复位
        return len(stale)

    def _remember_orders(self) -> None:
        """保存每个合约当前已知的未终结委托，重启后仍能看到“上次会话遗留”。"""
        records: dict[str, list] = {}
        try:
            active = list(self.main_engine.get_all_active_orders())
        except Exception:
            active = []
        for name, config in self.service.list().items():
            symbols = set(config.legs + [config.exchange_spread_vt_symbol])
            rows = []
            for order in active:
                if getattr(order, "vt_symbol", "") not in symbols:
                    continue
                orderid = getattr(order, "vt_orderid", "")
                if not orderid:
                    continue
                moment = getattr(order, "datetime", None)
                rows.append({
                    "orderid": orderid,
                    "vt_symbol": order.vt_symbol,
                    "direction": getattr(getattr(order, "direction", None), "value", ""),
                    "offset": getattr(getattr(order, "offset", None), "value", ""),
                    "volume": float(getattr(order, "volume", 0) or 0),
                    "traded": float(getattr(order, "traded", 0) or 0),
                    "price": float(getattr(order, "price", 0) or 0),
                    "status": getattr(getattr(order, "status", None), "value", ""),
                    # 委托时间必须落盘：没有它就分不清“今天挂出、重启后仍在柜台”与
                    # “昨天的单、CTP 当日有效已失效”，只能一律当历史记录丢掉，
                    # 于是策略为整层重挂止盈单，撞上柜台的在途单（平仓量超过持仓量）。
                    "datetime": moment.isoformat() if moment is not None else "",
                })
            strategy = self._strategy(name)
            if strategy is not None:
                state = strategy.get_pause_state()
                seen = {row["orderid"] for row in rows}
                for orderid, report in (state.get("orders") or {}).items():
                    if orderid in seen:
                        continue
                    if report.get("status") in ("全部成交", "已撤销", "拒单",
                                                 "ALLTRADED", "CANCELLED", "REJECTED"):
                        continue
                    rows.append({"orderid": orderid, "vt_symbol": "", "direction": "",
                                 "offset": "", "volume": float(report.get("volume", 0) or 0),
                                 "traded": float(report.get("traded", 0) or 0),
                                 "status": str(report.get("status", ""))})
            if rows:
                records[name] = rows
        self._saved_orders = records
        self._persist()

    def _restored_order_moment(self, row: dict) -> tuple[datetime, bool]:
        """恢复委托用的时间戳：(时间, 是否为可信的原始时间)。

        可信 = 带时区，说明它来自交易所/柜台回报。**没有时区的时间一律不可信**：
        它要么是老版本状态文件缺字段时补的 ``datetime.now()``，要么是本机时钟写出来的。
        本机时钟可能与柜台差一天（2026-11-17 实测：日志 11-17 21:4x，CTP 回报
        11-18 21:2x），用这种时间去和柜台交易日比对，会把早已失效的隔夜单算成
        “今天挂的、仍然有效”，于是不清、还去绑定止盈位，最后一张单都挂不出来。
        """
        text = str(row.get("datetime") or "").strip()
        if text:
            try:
                moment = datetime.fromisoformat(text)
            except ValueError:
                pass
            else:
                return moment, moment.tzinfo is not None
        return datetime.now(), False

    def restore_orders(self) -> int:
        """把上次会话的未了结委托恢复进 OMS 与价差路由。

        vn.py 重启后不会向 CTP 查询历史订单，因此“本地记得这笔委托”是唯一的
        线索：恢复后它才会出现在在途委托里，后续成交/撤单回报也能匹配到价差。
        只恢复本程序自己记录过的委托；不臆造柜台状态。
        """
        if self._orders_restored:
            return 0
        self._orders_restored = True
        oms = None
        try:
            getter = getattr(self.main_engine, "get_engine", None)
            if callable(getter):
                oms = getter("oms")
        except Exception:
            oms = None
        if oms is None or not hasattr(oms, "process_order_event"):
            return 0

        from vnpy.event import Event
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.event import EVENT_ORDER
        from vnpy.trader.object import OrderData

        terminal = {"全部成交", "已撤销", "拒单", "ALLTRADED", "CANCELLED", "REJECTED"}
        restored = 0
        mapping = getattr(self.spread_engine, "order_spread_map", None)
        for name, rows in list(self._saved_orders.items()):
            spread = self.spread_engine.get_spread(name)
            if spread is None:
                continue
            for row in rows:
                if str(row.get("status", "")) in terminal:
                    continue
                vt_orderid = str(row.get("orderid", ""))
                if not vt_orderid.startswith("CTP."):
                    vt_orderid = f"CTP.{vt_orderid}"
                if vt_orderid in self._recovered_orderids:
                    continue
                # 终态先于窗口初始化到达时，磁盘上的旧活单绝不能复活它。
                current = getattr(oms, 'orders', {}).get(vt_orderid)
                if getattr(current, 'status', None) in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED):
                    continue
                vt_symbol = str(row.get("vt_symbol") or "")
                try:
                    symbol, exchange_text = vt_symbol.split(".", 1)
                    exchange = Exchange(exchange_text)
                    direction = Direction(str(row.get("direction", "")))
                    offset = Offset(str(row.get("offset", "")))
                    status = Status(str(row.get("status", "")))
                except (ValueError, KeyError):
                    continue  # 缺字段的记录只用于展示，不恢复成假委托
                moment, moment_known = self._restored_order_moment(row)
                order = OrderData(
                    gateway_name="CTP",
                    symbol=symbol,
                    exchange=exchange,
                    orderid=vt_orderid.split(".", 1)[1],
                    type=OrderType.LIMIT,
                    direction=direction,
                    offset=offset,
                    price=float(row.get("price", 0) or 0) or 0.0,
                    volume=float(row.get("volume", 0) or 0),
                    traded=float(row.get("traded", 0) or 0),
                    status=status,
                    datetime=moment,
                )
                if isinstance(current, OrderData):
                    if current.vt_symbol != order.vt_symbol:
                        self.errors[name] = '柜台与本地委托标识冲突，禁止自动恢复'
                        continue
                    order, status = current, current.status
                    moment_known = order.datetime is not None and order.datetime.tzinfo is not None
                else:
                    try:
                        oms.process_order_event(Event(EVENT_ORDER, order))
                    except Exception:
                        continue
                if not moment_known:
                    self._moment_unknown.add(order.vt_orderid)
                if isinstance(mapping, dict) and order.vt_symbol in set(
                        spread.variable_symbols.values()):
                    # 只有单腿委托能直接更新价差账；组合合约成交由后续核对处理。
                    mapping[order.vt_orderid] = spread
                self._recovered_orderids.add(order.vt_orderid)
                strategy = self._strategy(name)
                if strategy is not None and hasattr(strategy, "pause_order_reports"):
                    strategy.pause_order_reports.setdefault(
                        order.vt_orderid,
                        {"algoid": "recovered", "status": status.value,
                         "volume": order.volume, "traded": order.traded},
                    )
                self._register_recovered_algo(name, spread, strategy, order)
                restored += 1
        if restored:
            writer = getattr(self.main_engine, "write_log", None)
            if callable(writer):
                writer(f"已恢复上次会话未了结委托 {restored} 笔；请到柜台确认其真实状态")
        return restored

    def _register_recovered_algo(self, name: str, spread, strategy, order) -> None:
        """把恢复的委托挂成一个只跟踪、不发单的算法，让引擎和在途算法列表认领它。"""
        algo_engine = getattr(self.spread_engine, "algo_engine", None)
        algos = getattr(algo_engine, "algos", None)
        if not isinstance(algos, dict):
            return
        from spreadconsole.recovered_algo import RecoveredAlgo

        algoid = f"recovered_{order.vt_orderid}"
        if algoid in algos:
            return
        algo = RecoveredAlgo(algoid, spread, order)
        algos[algoid] = algo
        order_map = getattr(algo_engine, "order_algo_map", None)
        if isinstance(order_map, dict):
            order_map[order.vt_orderid] = algo
        if strategy is not None and hasattr(strategy, "algoids"):
            strategy.algoids.add(algoid)

    def _known_active_orders(self, name: str, config) -> list:
        """本合约“已知在途委托”：OMS 活跃委托 + 本地保存但引擎尚未恢复的记录。"""
        from types import SimpleNamespace
        from vnpy.trader.constant import Direction, Offset

        symbols = set(config.legs + [config.exchange_spread_vt_symbol])
        try:
            active = [
                order for order in self.main_engine.get_all_active_orders()
                if getattr(order, "vt_symbol", "") in symbols
            ]
        except Exception:
            active = []
        known = {getattr(order, "vt_orderid", "") for order in active}
        terminal = {"全部成交", "已撤销", "拒单", "ALLTRADED", "CANCELLED", "REJECTED"}
        for row in self._saved_orders.get(name, []):
            orderid = str(row.get("orderid", ""))
            vt_symbol = str(row.get("vt_symbol") or "")
            if not orderid or orderid in known or vt_symbol not in symbols:
                continue
            if str(row.get("status", "")) in terminal:
                continue
            try:
                direction = Direction(str(row.get("direction", "")))
                offset = Offset(str(row.get("offset", "")))
            except ValueError:
                continue
            active.append(SimpleNamespace(
                vt_orderid=orderid, vt_symbol=vt_symbol,
                direction=direction, offset=offset,
                volume=float(row.get("volume", 0) or 0),
                traded=float(row.get("traded", 0) or 0),
                datetime=self._saved_row_moment(row),
            ))
            known.add(orderid)
        return active

    @staticmethod
    def _saved_row_moment(row: dict):
        """本地记录里的委托时间；**没有时区**（本机时钟）时返回 None 表示不可信。"""
        text = str(row.get("datetime") or "").strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        return moment if moment.tzinfo is not None else None

    def _bind_existing_exit_orders(self, name: str, config, strategy, spread) -> int:
        """把柜台已有的组合平仓委托绑定到（最新）接管层，占住止盈位，避免重复挂单。

        **是否“当日有效”只由柜台委托快照判定**（:meth:`_prune_orders_absent_from_cabinet`
        在绑定之前先把柜台已经没有的单清掉）。这里不再用 ``trading_day_of(委托时间)``
        和柜台 TradingDay 比对：夜盘时柜台回报的 TradingDay 可能滞后一天，
        该比较会把**今晚刚挂出、确实在柜台**的单全判成“隔夜”，于是每层都重挂一张
        （2026-11-17 21:56 实测：9 个合约的新单全部被判隔夜、全部重挂）。

        唯一保留的时间判断是“有没有可信时间戳”：本机时钟写的（无时区）时间不能
        证明委托还在柜台，一律不绑定。
        """
        if strategy is None or not hasattr(strategy, "reserved_exit_orders"):
            return 0
        from vnpy.trader.constant import Direction, Offset

        target = config.exchange_spread_vt_symbol
        layers = list(getattr(strategy, "layers", []) or [])
        bound = 0
        skipped = 0
        for order in self._known_active_orders(name, config):
            if getattr(order, "vt_symbol", "") != target:
                continue
            if getattr(order, "offset", None) not in (
                    Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY):
                continue
            moment = getattr(order, "datetime", None)
            if moment is None or getattr(moment, "tzinfo", None) is None:
                # 时间不可信（本机时钟补的或缺字段）：无法证明它仍挂在柜台，不绑定。
                skipped += 1
                continue
            if self._clearly_stale(moment):
                # 快照不可用时的兜底：明显早于当前交易日的单一定是隔夜失效单。
                skipped += 1
                continue
            direction = getattr(order, "direction", None)
            sign = 1 if direction == Direction.LONG else -1
            matches = [
                layer for layer in layers
                if sign * float(getattr(layer, "direction", 0) or 0) < 0
            ]
            if not matches:
                skipped += 1        # 与任何层都不同向：不可能是平掉现有仓位的单
                continue
            layer = matches[-1]     # 只盯**最新层**：策略同一时刻只保留一张止盈单
            remaining = (float(getattr(order, "volume", 0) or 0)
                         - float(getattr(order, "traded", 0) or 0))
            layer_volume = float(getattr(layer, "volume", 0) or 0)
            if (remaining > 1e-9 and layer_volume > 1e-9
                    and remaining > layer_volume + 1e-9):
                # 剩余量大于最新层手数：这张单不是盯这一层的。绑上去会占住止盈位，
                # 却按别的层量去平，最新层反而永远等不到自己的止盈单。
                skipped += 1
                continue
            strategy.reserved_exit_orders[order.vt_orderid] = getattr(layer, "tier_id", "")
            bound += 1
        writer = getattr(self.main_engine, "write_log", None)
        if bound and callable(writer):
            writer(
                f"{name}：已把柜台 {bound} 笔平仓委托绑定到最新层止盈位，"
                f"策略不会再为这一层补挂止盈单"
                + (f"（另有 {skipped} 笔隔夜/方向或手数不匹配的单未绑定，"
                   f"请到柜台确认是否撤销）" if skipped else "")
            )
        elif skipped and callable(writer):
            writer(
                f"{name}：柜台 {skipped} 笔平仓委托未绑定（隔夜失效，或与最新层方向/手数不符）；"
                f"策略会按最新层重新挂止盈单，请到柜台确认旧单是否撤销"
            )
        return bound

    def _bind_existing_open_orders(self, name: str, config, strategy) -> int:
        """把柜台已有的建仓/加仓委托占住开仓槽位，避免启动后重复加仓。"""
        if strategy is None or not hasattr(strategy, "reserved_open_orders"):
            return 0
        from vnpy.trader.constant import Offset

        getter = getattr(self.main_engine, "get_order", None)
        # 已终结的旧占位不再拦截（其影响已由本次持仓同步体现）。
        for orderid in list(strategy.reserved_open_orders):
            order = getter(orderid) if callable(getter) else None
            if order is not None and not order.is_active():
                strategy.reserved_open_orders.pop(orderid, None)
        bound = 0
        for order in self._known_active_orders(name, config):
            if getattr(order, "offset", None) != Offset.OPEN:
                continue
            if getattr(order, "vt_orderid", "") in strategy.reserved_open_orders:
                continue
            strategy.reserved_open_orders[order.vt_orderid] = ""
            bound += 1
        if bound:
            writer = getattr(self.main_engine, "write_log", None)
            if callable(writer):
                writer(
                    f"{name}：柜台仍有 {bound} 笔建仓/加仓委托，"
                    f"已占住开仓槽位，策略不会重复加仓"
                )
        return bound

    def _prune_finished_recovered(self, name: str) -> None:
        """恢复的委托一旦在 OMS 里变成终态，就同步清掉暂停报告里的旧状态。"""
        strategy = self._strategy(name)
        if strategy is None or not self._recovered_orderids:
            return
        getter = getattr(self.main_engine, "get_order", None)
        if not callable(getter):
            return
        for orderid in list(self._recovered_orderids):
            try:
                order = getter(orderid)
            except Exception:
                order = None
            if order is not None and not order.is_active():
                if hasattr(strategy, "pause_order_reports"):
                    strategy.pause_order_reports.pop(orderid, None)
                self._drop_recovered_algo(orderid, strategy)
                self._recovered_orderids.discard(orderid)

    def _drop_recovered_algo(self, orderid: str, strategy) -> None:
        algo_engine = getattr(self.spread_engine, "algo_engine", None)
        algoid = f"recovered_{orderid}"
        algos = getattr(algo_engine, "algos", None)
        if isinstance(algos, dict):
            algos.pop(algoid, None)
        order_map = getattr(algo_engine, "order_algo_map", None)
        if isinstance(order_map, dict):
            order_map.pop(orderid, None)
        if strategy is not None and hasattr(strategy, "algoids"):
            strategy.algoids.discard(algoid)

    def _cabinet_sysid_index(self, td) -> dict[str, str]:
        """本地委托号 → OrderSysID。网关的实时映射与流水恢复映射都倒过来查。

        ``sysid_orderid_map`` / ``_journal_sysids`` 的键可能是裸 sysid，也可能是
        ``(broker, user, day, exchange, sysid)``；值的本地委托号不带 ``CTP.`` 前缀。
        """
        index: dict[str, str] = {}
        for attr in ("sysid_orderid_map", "_journal_sysids"):
            mapping = getattr(td, attr, None)
            if not isinstance(mapping, dict):
                continue
            for key, value in mapping.items():
                sysid = key[-1] if isinstance(key, tuple) else key
                if isinstance(sysid, str) and isinstance(value, str) and sysid and value:
                    index.setdefault(value, sysid)
        return index

    def _prune_orders_absent_from_cabinet(self, name: str, config, *,
                                          reserved_before_query=None) -> int:
        """以柜台**当日委托快照**为准，清掉本地记录里“柜台已经没有”的委托。

        CTP 委托当日有效，交易所的当日委托列表只含当前交易日，所以
        ``ReqQryOrder`` 的返回集合就是唯一权威清单：本地还在、柜台没有，说明它属于
        上一交易日（已随结算失效），或早已撤单/成交而回报丢失。

        快照缺失、查询失败或过旧时**一条都不清**——宁可提示“状态未知”，也不能把
        真实在途的单误删后重挂一张（那会变成超量平仓，只能等柜台用错误码 30 挡）。
        """
        td = getattr(self._gateway(), "td_api", None)
        if td is None:
            return 0
        if not getattr(td, "orders_snapshot_ok", False):
            self._log_orders_snapshot_missing(name)
            return 0
        at = float(getattr(td, "orders_snapshot_at", 0.0) or 0.0)
        if not at or monotonic() - at > SNAPSHOT_MAX_AGE:
            self._log_orders_snapshot_missing(name)
            return 0
        snapshot = set(getattr(td, "orders_snapshot", set()) or ())
        cabinet_sysids = set((getattr(td, "orders_snapshot_sysids", {}) or {}).keys())
        sysid_of = self._cabinet_sysid_index(td)

        def still_at_cabinet(orderid: str) -> bool:
            """柜台当日委托里是否还有这一笔。

            优先用交易所委托号（跨重启/重连不变）；本地还没有 sysid 的（刚报出、
            尚未被受理）退回会话号。两者都查不到才算"柜台没有"。
            """
            bare = orderid.split(".", 1)[1] if "." in orderid else orderid
            if orderid in snapshot or bare in snapshot:
                return True
            sysid = sysid_of.get(bare) or sysid_of.get(orderid)
            return bool(sysid and sysid in cabinet_sysids)

        symbols = set(config.legs + [config.exchange_spread_vt_symbol])
        oms = None
        try:
            getter = getattr(self.main_engine, "get_engine", None)
            if callable(getter):
                oms = getter("oms")
        except Exception:
            oms = None
        if reserved_before_query is not None:
            local = set(reserved_before_query)
        else:
            local = set(self._recovered_orderids)
            local |= {str(row.get("orderid", ""))
                      for row in (self._saved_orders.get(name) or [])}
        dropped: list[str] = []
        for orderid in sorted(local):
            if not orderid or still_at_cabinet(orderid):
                continue
            order = getattr(oms, "orders", {}).get(orderid) if oms is not None else None
            vt_symbol = getattr(order, "vt_symbol", "")
            if vt_symbol and vt_symbol not in symbols:
                continue        # 别的合约的委托，交给它自己的核对
            dropped.append(orderid)
        if not dropped:
            return 0
        strategy = self._strategy(name)
        for orderid in dropped:
            self._drop_recovered_algo(orderid, strategy)
            self._recovered_orderids.discard(orderid)
            self._moment_unknown.discard(orderid)
            if oms is not None:
                oms.orders.pop(orderid, None)
                oms.active_orders.pop(orderid, None)
            mapping = getattr(self.spread_engine, "order_spread_map", None)
            if isinstance(mapping, dict):
                mapping.pop(orderid, None)
            if strategy is not None:
                for field in ("reserved_exit_orders", "reserved_open_orders",
                              "pause_order_reports", "pause_trade_reports"):
                    holder = getattr(strategy, field, None)
                    if isinstance(holder, dict):
                        holder.pop(orderid, None)
        rows = [row for row in (self._saved_orders.get(name) or [])
                if str(row.get("orderid", "")) not in dropped]
        if rows:
            self._saved_orders[name] = rows
        else:
            self._saved_orders.pop(name, None)
        writer = getattr(self.main_engine, "write_log", None)
        if callable(writer):
            writer(
                f"{name}：柜台当前交易日委托里没有 {'、'.join(dropped)}，"
                f"按隔夜失效清理本地记录（CTP 当日有效）"
            )
        return len(dropped)

    def _log_orders_snapshot_missing(self, name: str) -> None:
        """快照不可用时留痕：否则“为什么没清旧单”在日志里完全看不到。"""
        now = time()
        if now - getattr(self, "_last_order_snapshot_log", 0.0) < 30:
            return
        self._last_order_snapshot_log = now
        writer = getattr(self.main_engine, "write_log", None)
        if callable(writer):
            writer(
                f"{name}：柜台当日委托快照不可用（未查询、查询失败或已过期），"
                f"本次不清理本地委托记录；请点“核对柜台状态”重新查询后再纠正"
            )

    def _prune_stale_order_records(self, name: str, config, *,
                                   check_directions: bool = False) -> int:
        """核对纠正时自动清掉不属于当前仓位的旧委托记录。

        CTP 没有委托查询接口，重启前遗留的委托不会有任何新回报；用户点
        “核对并纠正”即视为已人工核对。三种记录会被清掉：
        1. 本次登录从未收到过回报、且委托时间不在当前交易日的纯记忆记录；
        2. ``check_directions`` 时，方向与当前所有层都同向的**非当日**平仓单——
           它不可能是在平掉现有仓位（常见于早已成交的历史止盈单）。

        **当前交易日挂出、尚未终态的委托一律保留**：重启后 CTP 不会重推历史
        回报，用“本次登录见过回报”当唯一判据的话，今晚刚挂、柜台仍有效的止盈单
        必然被判 stale 清掉，策略于是为整层再挂一张，撞上柜台的在途单——
        2026-11-17 21:04 重启后 12 张止盈单全被「平仓量超过持仓量」拒掉即此故。
        """
        td = getattr(self._gateway(), "td_api", None)
        seen = set(getattr(td, "live_seen_orderids", set()) or ()) if td is not None else set()
        oms = None
        try:
            getter = getattr(self.main_engine, "get_engine", None)
            if callable(getter):
                oms = getter("oms")
        except Exception:
            oms = None
        symbols = set(config.legs + [config.exchange_spread_vt_symbol])
        saved_rows = {
            str(row.get("orderid", "")): row
            for row in (self._saved_orders.get(name) or [])
        }
        stale: list[str] = []
        for orderid in list(self._recovered_orderids):
            order = getattr(oms, "orders", {}).get(orderid) if oms is not None else None
            vt_symbol = getattr(order, "vt_symbol", "")
            if vt_symbol and vt_symbol not in symbols:
                continue
            if orderid in seen:
                continue
            if self._has_trusted_moment(order, saved_rows.get(orderid)):
                # 有柜台回报时间：它到底还在不在，由“柜台当日委托快照”判定
                # （_prune_orders_absent_from_cabinet），这里不猜。
                continue
            stale.append(orderid)
        if check_directions and oms is not None:
            from vnpy.trader.constant import Direction, Offset
            strategy = self._strategy(name)
            layers = list(getattr(strategy, "layers", []) or []) if strategy else []
            close_offsets = (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY)
            for orderid, order in list(getattr(oms, "orders", {}).items()):
                if orderid in stale:
                    continue
                if getattr(order, "vt_symbol", "") not in symbols:
                    continue
                if getattr(order, "offset", None) not in close_offsets:
                    continue
                if self._has_trusted_moment(order, saved_rows.get(orderid)):
                    continue
                if not getattr(order, "is_active", lambda: False)():
                    stale.append(orderid)
                    continue
                sign = 1 if getattr(order, "direction", None) == Direction.LONG else -1
                if layers and not any(
                        sign * float(getattr(layer, "direction", 0) or 0) < 0
                        for layer in layers):
                    # 与所有层同向：不可能是平掉当前仓位的委托，视为历史残留。
                    stale.append(orderid)
        for orderid in stale:
            self._drop_recovered_algo(orderid, self._strategy(name))
            self._recovered_orderids.discard(orderid)
            if oms is not None:
                oms.orders.pop(orderid, None)
                oms.active_orders.pop(orderid, None)
            mapping = getattr(self.spread_engine, "order_spread_map", None)
            if isinstance(mapping, dict):
                mapping.pop(orderid, None)
        # 本地记录只保留本次登录收到过回报的、或有可信时间戳的委托。
        rows = [
            row for row in (self._saved_orders.get(name) or [])
            if str(row.get("orderid", "")) in seen
            or self._saved_row_moment(row) is not None
        ]
        if rows:
            self._saved_orders[name] = rows
        else:
            self._saved_orders.pop(name, None)
        return len(stale)

    def _has_trusted_moment(self, order, row: dict | None) -> bool:
        """委托是否带着**可信**时间戳（来自柜台/交易所回报，而非本机时钟）。

        只用来区分“本机补的假时间”与“真实回报时间”：前者无法证明委托仍在柜台，
        按历史记录清理；后者交给柜台委托快照判定，绝不用 ``trading_day_of`` 与
        柜台 TradingDay 硬比——夜盘时柜台该字段可能滞后一天，比了会把今晚刚挂出的
        单全判成“隔夜”（2026-11-17 21:56 实测：9 个合约的新单全部被判隔夜）。
        """
        if row is not None:
            moment = self._saved_row_moment(row)
            return moment is not None and not self._clearly_stale(moment)
        moment = getattr(order, "datetime", None)
        if moment is None or getattr(moment, "tzinfo", None) is None:
            return False
        if self._clearly_stale(moment):
            return False
        orderid = str(getattr(order, "vt_orderid", "") or "")
        return not (orderid and orderid in self._moment_unknown)

    def _clearly_stale(self, moment: datetime) -> bool:
        """委托时间是否**明显**早于当前交易日（容差 1 个交易日）。

        夜盘时柜台回报的 TradingDay 可能滞后一天，用“同一天”做判据会误伤今晚的单；
        这里只拦截相差超过一天的情况（例如隔了好几天的历史止盈单）。真正的权威
        判定仍是柜台当日委托快照。
        """
        from utils.calendar import trading_day_of

        current = self.current_trading_day()
        if not current:
            return False
        try:
            current_date = date.fromisoformat(current)
            day = trading_day_of(moment)
        except (TypeError, ValueError):
            return False
        return (current_date - day).days > 1

    def clear_contract_orders(self, name: str) -> int:
        """只清理本机“该合约的委托记录”，不撤单、不动柜台。

        用于委托早已在柜台结束、但回报缺失导致本地一直显示在途的情况。
        """
        config = self.service.get(name)
        if config is None:
            raise ManagedSpreadError("合约不在注册表")
        symbols = set(config.legs + [config.exchange_spread_vt_symbol])
        oms = None
        try:
            getter = getattr(self.main_engine, "get_engine", None)
            if callable(getter):
                oms = getter("oms")
        except Exception:
            oms = None
        # 只清“没有任何真实算法持有”的记录；恢复占位算法也视为已结束。
        owned: set[str] = set()
        algo_engine = getattr(self.spread_engine, "algo_engine", None)
        for algoid, algo in getattr(algo_engine, "algos", {}).items():
            if str(algoid).startswith("recovered_"):
                continue
            owned.update(getattr(algo, "orders", {}) or {})
        cleared = 0
        cleared_ids: set[str] = set()
        for orderid in list(self._recovered_orderids):
            strategy = self._strategy(name)
            self._drop_recovered_algo(orderid, strategy)
            self._recovered_orderids.discard(orderid)
        if oms is not None:
            for orderid, order in list(getattr(oms, "orders", {}).items()):
                if getattr(order, "vt_symbol", "") not in symbols:
                    continue
                if orderid in owned:
                    continue
                oms.orders.pop(orderid, None)
                oms.active_orders.pop(orderid, None)
                cleared_ids.add(orderid)
                cleared += 1
        mapping = getattr(self.spread_engine, "order_spread_map", None)
        if isinstance(mapping, dict):
            for orderid in cleared_ids:
                mapping.pop(orderid, None)
        self._saved_orders.pop(name, None)
        strategy = self._strategy(name)
        if strategy is not None:
            for field in ("reserved_exit_orders", "reserved_open_orders"):
                values = getattr(strategy, field, None)
                if isinstance(values, dict):
                    values.clear()
            if hasattr(strategy, "pause_order_reports"):
                strategy.pause_order_reports.clear()
        self._persist()
        writer = getattr(self.main_engine, "write_log", None)
        if callable(writer):
            writer(f"{name}：已清理 {cleared} 条本地委托记录；柜台委托/持仓未做任何改动")
        return cleared

    def clear_leftover_orders(self) -> int:
        """清除本地记录的“上次会话遗留委托”，用于柜台已确认这些委托早已结束。"""
        oms = None
        try:
            getter = getattr(self.main_engine, "get_engine", None)
            if callable(getter):
                oms = getter("oms")
        except Exception:
            oms = None
        mapping = getattr(self.spread_engine, "order_spread_map", None)
        cleared = 0
        for orderid in list(self._recovered_orderids):
            if isinstance(mapping, dict):
                mapping.pop(orderid, None)
            for name, rows in self._saved_orders.items():
                if any(str(row.get("orderid", "")) == orderid for row in rows):
                    self._drop_recovered_algo(orderid, self._strategy(name))
                    break
            if oms is not None:
                try:
                    oms.active_orders.pop(orderid, None)
                    oms.orders.pop(orderid, None)
                except Exception:
                    pass
            cleared += 1
        self._recovered_orderids.clear()
        for name in list(self._saved_orders):
            strategy = self._strategy(name)
            if strategy is not None and hasattr(strategy, "pause_order_reports"):
                for row in self._saved_orders.get(name, []):
                    orderid = str(row.get("orderid", ""))
                    strategy.pause_order_reports.pop(orderid, None)
                    reserved = getattr(strategy, "reserved_exit_orders", None)
                    if isinstance(reserved, dict):
                        reserved.pop(orderid, None)
                    reserved_open = getattr(strategy, "reserved_open_orders", None)
                    if isinstance(reserved_open, dict):
                        reserved_open.pop(orderid, None)
        self._saved_orders = {}
        self._orders_restored = True
        self._persist()
        writer = getattr(self.main_engine, "write_log", None)
        if cleared and callable(writer):
            writer(f"已清除 {cleared} 笔本地遗留委托记录；柜台状态未做任何改动")
        return cleared

    def invalidate_reconciliation(self, *, forget: bool = True) -> None:
        """作废当前核对。

        ``forget=True``（默认）连本地保存的快照一起丢弃，用于断线/换账号/配置变化；
        只停止交易时用 ``forget=False`` 保留审计记录；重启必须重新核对。
        """
        self.reconciled_contracts.clear()
        self._reconciliation_snapshot_generation = None
        self._reconciliation_snapshot_account = None
        self._reconciliation_snapshot_at = None
        if forget and self._saved_reconciliation:
            self._saved_reconciliation.clear()
            self._persist()

    def _strategy(self, name: str):
        strategy = self.spread_engine.strategy_engine.strategies.get(
            f"CrossPeriodMRStrategy_{name}")
        if strategy is not None and getattr(strategy, "cabinet_position_check", None) is None:
            strategy.cabinet_position_check = (
                lambda direction, volume, _name=name: self._cabinet_covers(
                    _name, direction, volume))
        return strategy

    def _cabinet_covers(self, name: str, direction: int, volume: float) -> bool:
        """柜台持仓是否覆盖“平掉 volume 手方向为 direction 的价差”。

        以 OMS 持仓（成交后优先刷新，平时约 10 秒一次）为准；查不到/方向不符就返回 False，
        宁可不挂平仓单，也不发出会被“平仓量超过持仓量”拒绝的委托。

        **必须扣掉在途平仓委托**：CTP 的可用平仓量 = 持仓 − 已挂平仓未成交量。
        只看持仓会让“整层重挂”通过本地校验（2026-11-17 21:05：柜台 5 手空、
        已有 1 手止盈在途，策略仍报 5 手 → 拒单 30）。
        """
        from vnpy.trader.constant import Direction

        config = self.service.get(name)
        if config is None:
            return False
        near, far = config.legs
        try:
            positions = self.main_engine.get_all_positions()
        except Exception:
            return False
        near_long = near_short = far_long = far_short = 0.0
        for position in positions:
            symbol = getattr(position, "vt_symbol", "")
            if symbol not in (near, far):
                continue
            is_long = getattr(position, "direction", None) == Direction.LONG
            value = float(getattr(position, "volume", 0) or 0)
            if symbol == near:
                near_long += value if is_long else 0.0
                near_short += 0.0 if is_long else value
            else:
                far_long += value if is_long else 0.0
                far_short += 0.0 if is_long else value
        # 与价差引擎一致：可配对的腿仓取 min，而不是把两腿直接相加。
        # 旧写法 near_long + far_short 在正常持仓下正好是可平量的 **2 倍**，
        # 于是本地校验几乎必过，超量平仓单只能等柜台用错误码 30 挡下来。
        signed = min(near_long, far_short) - min(near_short, far_long)
        pending_buy, pending_sell = self._pending_close_volume(name, config)
        if direction > 0:
            available = signed - pending_sell      # 多头可平量 = 多头持仓 − 已挂卖出平仓
        else:
            available = -signed - pending_buy      # 空头可平量 = 空头持仓 − 已挂买入平仓
        return available + 1e-9 >= float(volume)

    def _pending_close_volume(self, name: str, config) -> tuple[float, float]:
        """在途平仓委托已冻结的可平量，返回 (买入量, 卖出量)（价差手数）。

        组合单与单腿单的手数口径一致（一腿一手），因此直接累计剩余未成交量。
        """
        from vnpy.trader.constant import Direction, Offset

        symbols = set(config.legs + [config.exchange_spread_vt_symbol])
        close_offsets = (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY)
        buy = sell = 0.0
        for order in self._known_active_orders(name, config):
            if getattr(order, "vt_symbol", "") not in symbols:
                continue
            if getattr(order, "offset", None) not in close_offsets:
                continue
            remaining = (float(getattr(order, "volume", 0) or 0)
                         - float(getattr(order, "traded", 0) or 0))
            if remaining <= 1e-9:
                continue
            if getattr(order, "direction", None) == Direction.LONG:
                buy += remaining
            else:
                sell += remaining
        return buy, sell

    def _startup_pause(self):
        # 即使注册表读取损坏，仍先暂停当前引擎所有本项目策略。
        for strategy in self.spread_engine.strategy_engine.strategies.values():
            if hasattr(strategy, "pause_strategy"):
                strategy.live_trading = False
                strategy.pause_strategy()

    def _gateway(self):
        return self.main_engine.get_gateway("CTP")

    def _account_key(self):
        gateway = self._gateway()
        td = getattr(gateway, "td_api", None)
        return (getattr(td, "brokerid", ""), getattr(td, "userid", "")) if td else None

    def md_ready(self) -> bool:
        gateway = self._gateway()
        return bool(gateway and getattr(getattr(gateway, "md_api", None), "login_status", False))

    def td_ready(self) -> bool:
        gateway = self._gateway()
        return bool(gateway and getattr(getattr(gateway, "td_api", None), "login_status", False))

    def _subscribe(self, vt_symbol: str):
        from vnpy.trader.object import SubscribeRequest
        from vnpy.trader.utility import extract_vt_symbol
        symbol, exchange = extract_vt_symbol(vt_symbol)
        if not self.md_ready():
            raise ManagedSpreadError("行情尚未登录，等待连接后订阅")
        self.main_engine.subscribe(SubscribeRequest(symbol, exchange), "CTP")

    def _record_legs(self, config: SpreadConfig):
        recorder = getattr(self.main_engine, "spread_tick_recorder", None)
        if recorder and not recorder.records_all:
            for symbol in config.legs:
                if symbol not in recorder.symbol_set:
                    recorder.symbols.append(symbol)
                    recorder.symbol_set.add(symbol)
                    recorder.stats.per_symbol.setdefault(symbol, 0)

    def ensure_subscription(self, config: SpreadConfig):
        if not self.md_ready():
            return
        # 引擎在合约事件中自动订阅；仅补订没有进入 CTP 订阅集合的腿。
        gateway = self._gateway()
        subscribed = set(getattr(getattr(gateway, "md_api", None), "subscribed", ()))
        already = tuple(symbol for symbol in config.legs if symbol.rsplit(".", 1)[0] in subscribed)
        self.service.subscribe_legs(config, self._subscribe, already_subscribed=already)
        self._record_legs(config)

    def refresh(self):
        try:
            names = frozenset(self.service.list())
        except Exception:
            names = self._registry_names
        if names != self._registry_names:
            first_seen = not self._registry_names
            self._registry_names = names
            # 首次读到注册表不算“配置变化”，不能把上次核对结果清掉。
            self.invalidate_reconciliation(forget=not first_seen)
        self.enabled.intersection_update(names)
        ready = self.md_ready()
        if not ready and self._last_md_ready:
            self.invalidate_reconciliation()
            self.service.reset_subscriptions()
        if ready and not self._last_md_ready:
            self.service.reset_subscriptions()
        self._last_md_ready = ready
        for config in self.service.list().values():
            try:
                self.service.register(self.spread_engine, config)
                self.service.ensure_strategy(self.spread_engine, config)
                strategy = self._strategy(config.name)
                if strategy and not self.master_running and config.name not in self.enabled:
                    strategy.pause_strategy()
                    strategy.live_trading = False
                self._record_legs(config)
                self.ensure_subscription(config)
                if self.errors.get(config.name, "").startswith(("注册或订阅失败：", "订阅失败：", "注册未完成：")):
                    self.errors.pop(config.name, None)
            except Exception as exc:
                self.errors[config.name] = f"注册或订阅失败：{exc}"
        td_ready = self.td_ready()
        was_td_ready = self._last_td_ready
        if td_ready and not was_td_ready:
            self._startup_query_done = False
        if (was_td_ready and not td_ready) or (
            self.reconciliation_confirmed and self._account_key() != self._last_account_key
        ):
            self.invalidate_reconciliation()
        self._last_td_ready = td_ready
        if not td_ready:
            # 尚未登录时不要丢弃上次会话的核对结果；只有“连过又断”才作废。
            self.invalidate_reconciliation(forget=was_td_ready)
        # 重启后先恢复上次会话的委托，再查持仓；两者都只是本机记录，不代表柜台。
        if td_ready and self._saved_orders:
            self.restore_orders()
        if td_ready and not self._startup_query_done:
            td = getattr(self._gateway(), "td_api", None)
            if hasattr(td, "position_snapshot_at") and getattr(td, "contract_inited", False):
                try:
                    self._gateway().query_position()
                    self._startup_query_done = True
                except Exception as exc:
                    if "合约目录" not in str(exc):
                        self._startup_query_done = True
        if self.master_running and (not td_ready or not ready):
            self.stop_all(reason="交易或行情连接断开")

    def add(self, text: str) -> tuple[SpreadConfig, bool]:
        if self.master_running:
            raise ManagedSpreadError("请先停止总交易，再添加或重试注册合约")
        config, created = self.service.add(text)
        try:
            self.service.register(self.spread_engine, config)
            self.service.ensure_strategy(self.spread_engine, config)
            self._record_legs(config)
            self.ensure_subscription(config)
            strategy = self._strategy(config.name)
            if strategy is not None:
                strategy.pause_strategy()
                strategy.live_trading = False
            self.errors.pop(config.name, None)
        except Exception as exc:
            self.errors[config.name] = f"注册未完成：{exc}；禁止交易，连接后重试"
        return config, created

    def quote_problem(self, config: SpreadConfig) -> str:
        """两腿是否已有可交易报价；返回空串表示可用，否则是可读原因。

        盘前／集合竞价期间盘口是空的，这个检查必然失败——预启动（arm）就是
        为这种情况准备的：策略先挂好，等这里返回空串的那一刻再开实盘。
        """
        from utils.filters import as_local
        if not self.md_ready() or not self.td_ready():
            return "行情或交易前置尚未登录"
        for symbol in config.legs:
            tick = self.main_engine.get_tick(symbol)
            if tick is None or not tick.bid_price_1 or not tick.ask_price_1:
                return f"{symbol} 无有效买一/卖一报价"
            timestamp = as_local(tick.datetime)
            now = as_local(datetime.now())
            if not timestamp or abs((now - timestamp).total_seconds()) > 5:
                return f"{symbol} 报价超过 5 秒，禁止交易"
        return ""

    def _validate_quote(self, config: SpreadConfig):
        problem = self.quote_problem(config)
        if problem:
            raise ManagedSpreadError(problem)


    @staticmethod
    def _orders_snapshot_problem(td) -> str:
        error = getattr(td, 'reconciliation_event_error', '')
        if error:
            return f'柜台回报待核实：{error}'
        if not hasattr(td, 'orders_snapshot_ok'):
            return ''  # 旧的非 CTP 测试/只读适配器没有此字段。
        stamp = float(getattr(td, 'orders_snapshot_at', 0) or 0)
        if (getattr(td, 'orders_query_pending', False) or not td.orders_snapshot_ok
                or not stamp or not 0 <= monotonic() - stamp <= SNAPSHOT_MAX_AGE):
            return '当日委托快照不可用或未完整返回；请重新核对柜台委托'
        return ''

    def _snapshot_problem(self, td=None) -> str:
        """返回“柜台持仓快照不可用”的具体原因；空字符串表示可用。

        这里只判断快照本身是否可信，不叠加与核对无关的门槛。持仓查询之后
        只要又收到委托/成交回报，网关会把快照时间清零，因此“快照之后没有
        新的柜台活动”由快照时间本身保证，不需要再缩短有效期。
        """
        if td is None:
            td = getattr(self._gateway(), "td_api", None)
        if td is None:
            return "交易接口不可用"
        if not hasattr(td, "position_snapshot_at"):
            return ""  # 非本项目网关：没有快照能力，沿用旧口径（不假装有快照）
        if not getattr(td, "login_status", False):
            return "交易端未登录"
        if getattr(td, "position_query_pending", False):
            return "持仓查询尚未完整返回"
        if getattr(td, "_position_query_failed", False):
            return "本次持仓回报异常，已丢弃"
        if not getattr(td, "contract_inited", False):
            return "合约目录尚未就绪"
        sides = getattr(td, "position_snapshot_sides", None)
        if not isinstance(sides, dict):
            return "柜台接口不支持完整持仓快照"
        stamp = float(getattr(td, "position_snapshot_at", 0) or 0)
        if not stamp:
            return "本次尚未取得完整持仓快照"
        if getattr(td, "position_snapshot_account", None) != self._account_key():
            return "快照账户与当前登录账户不一致"
        if monotonic() - stamp > SNAPSHOT_MAX_AGE:
            return "持仓快照已超过 5 分钟，请重新查询"
        return ""

    def _cabinet_leg_differences(self, config: SpreadConfig, spread) -> list[dict] | None:
        """只比较本次有效柜台快照；无证据返回未知，而不是沿用旧空仓。"""
        td = getattr(self._gateway(), "td_api", None)
        if self._snapshot_problem(td):
            return None
        if hasattr(td, "position_snapshot_sides"):
            sides = td.position_snapshot_sides
        else:
            return None  # 非本项目网关：没有柜台快照能力
        if not isinstance(sides, dict):
            return None
        if spread is None or not hasattr(spread, "leg_pos") or len(config.legs) != 2:
            return None
        result = []
        if list(spread.variable_symbols.values()) != config.legs:
            return None
        for symbol in config.legs:
            # 柜台未返回该合约的行，就是没有持仓（CTP 只返回非零仓位）。
            cabinet = sides.get(symbol.rsplit(".", 1)[0], {})
            if not isinstance(cabinet, dict):
                return None
            try:
                long = float(cabinet.get("long", 0) or 0)
                short = float(cabinet.get("short", 0) or 0)
                unknown = float(cabinet.get("unknown", 0) or 0)
                local = float(spread.leg_pos.get(symbol, 0) or 0)
            except (TypeError, ValueError):
                return None
            if not (0 <= long < float("inf") and 0 <= short < float("inf")):
                return None
            if unknown > 1e-9:
                return None  # 有无法区分多空的仓位，不能判断该腿是否为空。
            if not -float("inf") < local < float("inf"):
                return None
            net = long - short
            result.append({"symbol": symbol, "local": local, "cabinet": net,
                           "long": long, "short": short, "difference": net - local})
        return result

    def _align_cabinet_positions(self) -> list[str]:
        """把柜台**多出**的持仓补进本地账与策略层（只增不减），返回补过仓的价差。

        与「核对纠正」的区别：不要求人工确认，但只处理唯一安全的情形——柜台比本地多、
        两腿方向不冲突、且能取到柜台两腿均价。2026-11-11 的事故是柜台 2 手、本地只记
        1 手，多出的 1 手没有任何策略管理；这一步就是把它补回来的。
        """
        aligned: list[str] = []
        for name in sorted(self.enabled):
            config = self.service.get(name)
            spread = self.spread_engine.get_spread(name)
            if config is None or spread is None:
                continue
            try:
                strategy = self._strategy(name)
            except Exception:      # noqa: BLE001 - 取不到策略就跳过该价差
                continue
            try:
                if self._align_one_position(name, config, strategy, spread):
                    aligned.append(name)
            except Exception as exc:      # noqa: BLE001 - 单个价差失败不阻断启动
                writer = getattr(self.main_engine, "write_log", None)
                if callable(writer):
                    writer(f"{name} 仓位自动对齐失败：{exc}；请人工核对柜台持仓")
        return aligned

    def _align_one_position(self, name: str, config: SpreadConfig, strategy, spread) -> bool:
        """单个价差的差额接管：只补"柜台多、方向一致、有柜台均价"的部分。"""
        legs = self._cabinet_leg_differences(config, spread)
        if not legs:
            return False
        target: dict[str, float] = {}
        for leg in legs:
            local = float(leg["local"])
            cabinet = float(leg["cabinet"])
            if float(leg["long"]) > 1e-9 and float(leg["short"]) > 1e-9:
                return False                       # 双向持仓：不动，交人工
            if abs(cabinet - local) < 1e-9:
                continue
            if local and cabinet and (local > 0) != (cabinet > 0):
                return False                       # 方向相反：不动，交人工
            if abs(cabinet) <= abs(local):
                return False                       # 柜台不多（本地多记须人工查）
            target[leg["symbol"]] = cabinet
        if not target:
            return False

        data_engine = getattr(self.spread_engine, "data_engine", None)
        if data_engine is None or not callable(getattr(data_engine, "save_pos", None)):
            return False
        backup = {symbol: float(spread.leg_pos.get(symbol, 0) or 0) for symbol in config.legs}
        for symbol, volume in target.items():
            spread.leg_pos[symbol] = volume
        spread.calculate_pos()
        added = strategy.reconcile_legacy_position()
        if added <= 0:
            for symbol, volume in backup.items():      # 补层失败：还原本地账
                spread.leg_pos[symbol] = volume
            spread.calculate_pos()
            return False
        data_engine.save_pos()
        return True

    def reconciliation_report(self) -> dict:
        """一次性汇总所有已管理合约的账户、委托、持仓和策略层账。

        核对本身不因“有持仓/有委托”而失败：这些是账户的真实状态，不是核对错误。
        敞口放在 ``exposures`` 里提示，真正开启某个合约时再由 ``validate()``
        逐个合约拦截（OI 有持仓只拦 OI，不影响 DCE）。
        """
        try:
            active_orders = list(self.main_engine.get_all_active_orders())
            positions = list(self.main_engine.get_all_positions())
        except Exception as exc:
            raise ManagedSpreadError(f"本机委托/持仓缓存读取失败，不能确认核对：{exc}") from exc

        accounts = None
        getter = getattr(self.main_engine, "get_all_accounts", None)
        if callable(getter):
            try:
                accounts = [
                    item for item in getter()
                    if getattr(item, "gateway_name", "") == "CTP"
                ]
            except Exception:
                accounts = []

        terminal = ("全部成交", "已撤销", "拒单", "ALLTRADED", "CANCELLED", "REJECTED")
        warnings: list[str] = []
        contracts: list[dict] = []

        for contract_name, config in self.service.list().items():
            # 核对时顺带用柜台单腿合约刷新乘数/最小变动，保证所有已管理合约都完成验证，
            # 而不是等用户逐个点“允许此合约交易”才更新当前这一项。
            verify_text = "未验证"
            try:
                config, listed = self.service.verify_and_save(
                    contract_name, self.main_engine.get_contract)
                verify_text = "已验证" if listed else "组合合约未挂牌"
                if not listed:
                    warnings.append(f"{contract_name}：柜台未提供交易所组合合约，不能开启交易")
            except Exception as exc:
                verify_text = f"验证失败：{exc}"
                warnings.append(f"{contract_name}：合约验证失败（{exc}）")

            symbols = set(config.legs + [config.exchange_spread_vt_symbol])
            orders = [order for order in active_orders if order.vt_symbol in symbols]
            leg_positions = [
                item for item in positions
                if item.vt_symbol in config.legs and abs(float(getattr(item, "volume", 0) or 0)) > 0
            ]
            spread = self.spread_engine.get_spread(contract_name)
            strategy = self._strategy(contract_name)
            if strategy is not None:
                self._prune_finished_recovered(contract_name)
            state = strategy.get_pause_state() if strategy else {}
            net_pos = float(getattr(spread, "net_pos", 0) or 0)
            leg_differences = self._cabinet_leg_differences(config, spread)
            layers = list(getattr(strategy, "layers", []) or [])
            active_algos = list(state.get("active_algoids", []) or [])
            pending_orders = [
                report for report in state.get("orders", {}).values()
                if report.get("status") not in terminal
            ]

            order_details = [
                f"{order.vt_orderid} {order.vt_symbol} "
                f"{order.direction.value}/{order.offset.value} "
                f"{order.traded:g}/{order.volume:g}@{order.price:g} {order.status.value}"
                for order in orders
            ]
            position_details = [
                f"{item.vt_symbol} {item.direction.value} {item.volume:g}手 "
                f"均价{float(getattr(item, 'price', 0) or 0):g}"
                for item in leg_positions
            ]
            layer_details = [layer.describe() for layer in layers]
            algo_details = [str(algoid) for algoid in active_algos]
            pending_details = [
                f"{orderid} {report.get('status', '')} {report.get('traded', 0):g}/"
                f"{report.get('volume', 0):g}"
                for orderid, report in state.get("orders", {}).items()
                if report.get("status") not in terminal
            ]

            # “敞口”只指引擎与柜台不一致；在途委托/持仓本身是账户状态，只作提醒。
            item_exposures: list[str] = []
            item_notes: list[str] = []
            if orders:
                item_notes.append(f"{contract_name}：{len(orders)} 笔在途委托")
            if leg_positions:
                item_notes.append(f"{contract_name}：{len(leg_positions)} 条单腿持仓")
            if abs(net_pos) > 1e-9:
                item_notes.append(f"{contract_name}：引擎净仓 {net_pos:g}")
            if leg_differences is not None:
                for leg in leg_differences:
                    if abs(leg["difference"]) > 1e-9:
                        item_exposures.append(
                            f"{contract_name}：{leg['symbol']} 本地{leg['local']:g} / "
                            f"柜台净仓{leg['cabinet']:g}（多{leg['long']:g} 空{leg['short']:g}），"
                            f"本地{'少' if abs(leg['cabinet']) > abs(leg['local']) else '多或方向不符'}仓"
                        )
                    elif leg["long"] > 1e-9 and leg["short"] > 1e-9:
                        item_exposures.append(
                            f"{contract_name}：{leg['symbol']} 柜台同时持有多{leg['long']:g}/空{leg['short']:g}，"
                            "净仓相同不代表无持仓，须人工核对"
                        )
            if layers:
                item_notes.append(f"{contract_name}：策略层账 {len(layers)} 层")
            if active_algos:
                item_notes.append(f"{contract_name}：在途算法 {len(active_algos)} 个")
            if pending_orders:
                item_notes.append(
                    f"{contract_name}：暂停期间仍有未终结委托 {len(pending_orders)} 笔")
            if state.get("trades_by_order"):
                item_notes.append(f"{contract_name}：暂停后有新增成交，须核对归属及层账")
            if leg_differences is None and hasattr(getattr(self._gateway(), "td_api", None), "position_snapshot_at"):
                item_exposures.append(f"{contract_name}：柜台快照或本地逐腿仓位不可比，无法判断是否对齐")
            # 上次会话遗留、且引擎里也没有对应记录的委托：才算“记录对不齐”。
            known_orderids = {order.vt_orderid for order in orders}
            saved_leftovers = [
                row for row in (self._saved_orders.get(contract_name) or [])
                if row.get("orderid") not in known_orderids
            ]
            if saved_leftovers:
                item_exposures.append(
                    f"{contract_name}：上次会话有 {len(saved_leftovers)} 笔未了结委托，"
                    "引擎没有对应记录，须到柜台确认")
                pending_details.extend(
                    f"{row.get('orderid', '')} {row.get('status', '')} "
                    f"{float(row.get('traded', 0) or 0):g}/{float(row.get('volume', 0) or 0):g}"
                    "（上次会话）"
                    for row in saved_leftovers[:8]
                )

            contracts.append({
                "name": contract_name,
                "display": config.display_name or contract_name,
                "orders": len(orders),
                "positions": len(leg_positions),
                "net_pos": net_pos,
                "leg_differences": leg_differences,
                "layers": len(layers),
                "active_algos": len(active_algos),
                "verify": verify_text,
                "ok": not item_exposures,
                "problems": item_exposures,
                "notes": item_notes,
                "order_details": order_details,
                "position_details": position_details,
                "layer_details": layer_details,
                "algo_details": algo_details,
                "pending_details": pending_details,
            })

        exposures = [problem for item in contracts for problem in item["problems"]]
        notes = [note for item in contracts for note in item["notes"]]

        if accounts is not None and not accounts:
            warnings.append("交易端账户信息尚未返回；请确认已在主终端核对资金/持仓")
        event_error = getattr(getattr(self._gateway(), "td_api", None), "reconciliation_event_error", "")
        if event_error:
            warnings.append(f"有回报无法解析（{event_error}）；相关委托/成交请以柜台为准")

        return {
            # 核对是“查看账户状态”的动作，不因账户本来就有持仓/委托而失败。
            "ok": True,
            "exposures": exposures,
            "problems": exposures,
            "notes": notes,
            "warnings": warnings,
            "contracts": contracts,
            "accounts": [
                {
                    "gateway": getattr(item, "gateway_name", ""),
                    "available": float(getattr(item, "available", 0) or 0),
                    "balance": float(getattr(item, "balance", 0) or 0),
                }
                for item in (accounts or [])
            ],
            "account_known": accounts is not None,
        }

    def confirm_reconciliation(self):
        """一次性确认已查看所有已管理合约的账户、委托、持仓与策略层账。

        核对不再因持仓/委托而失败；这些敞口只在开启对应合约时由 ``validate()``
        拦截。账户数据未返回时会记为“人工确认”并写入日志。
        """
        if not self.md_ready() or not self.td_ready():
            raise ManagedSpreadError("行情与交易端尚未全部连接，不能确认核对")
        # 读取异常不等于无仓/无单；必须能完整生成本次报告。
        report = self.reconciliation_report()
        td = getattr(self._gateway(), "td_api", None)
        problem = self._snapshot_problem(td)
        if problem:
            self.invalidate_reconciliation()
            raise ManagedSpreadError(f"柜台持仓快照不可用：{problem}；请点“核对柜台状态”重新查询")
        problem = self._orders_snapshot_problem(td)
        if problem:
            self.invalidate_reconciliation()
            raise ManagedSpreadError(problem)
        self._reconciliation_snapshot_generation = getattr(td, "position_query_generation", None)
        self._reconciliation_snapshot_account = getattr(td, "position_snapshot_account", None)
        self._reconciliation_snapshot_at = float(getattr(td, "position_snapshot_at", 0) or 0)
        self._last_td_ready = True
        self._last_md_ready = True
        self._last_account_key = self._account_key()
        moment = datetime.now()
        targets = [item["name"] for item in report["contracts"]]
        self.reconciled_contracts.clear()
        for item in report["contracts"]:
            if item["leg_differences"] is not None or not hasattr(td, "position_snapshot_at"):
                self.reconciled_contracts[item["name"]] = moment
        self._registry_names = frozenset(self.service.list())
        # 持久化已查看的快照用于审计，不作为跨会话的发单许可。
        sides = dict(getattr(td, "position_snapshot_sides", {}) or {})
        account = list(self._account_key() or ())
        saved_at = time()
        self._saved_reconciliation = {
            item["name"]: {"at": saved_at, "account": account, "sides": sides}
            for item in report["contracts"]
            if item["name"] in self.reconciled_contracts
        }
        self._remember_orders()

        account_text = "；".join(
            f"{item['gateway']} 可用 {item['available']:.0f} 元 / 权益 {item['balance']:.0f} 元"
            for item in report["accounts"]
        ) or "账户数据未返回（按人工核对确认）"
        verified = sum(1 for item in report["contracts"] if item["verify"] == "已验证")
        warning_text = "；".join(report["warnings"])
        exposure_text = "；".join(report["exposures"])
        writer = getattr(self.main_engine, "write_log", None)
        if callable(writer):
            try:
                writer(
                    f"柜台核对完成：{account_text}；已查看全部 {len(targets)} 个合约；"
                    f"合约验证 {verified}/{len(report['contracts'])}；"
                    f"逐腿核对 {len(self.reconciled_contracts)}/{len(targets)}；"
                    f"当前敞口：{exposure_text or '无'}"
                    + (f"；注意：{warning_text}" if warning_text else "")
                )
            except Exception:
                pass  # 日志故障不撤销已经完成的人工核对。

    @staticmethod
    def _position_change_label(old: dict[str, float], target: dict[str, float], legs) -> str:
        """把本次持仓纠正归类：一致 / 补充 / 同步（减仓或反方向）。"""
        changed = False
        supplement = True
        for symbol in legs:
            before = float(old.get(symbol, 0) or 0)
            after = float(target.get(symbol, 0) or 0)
            if abs(before - after) < 1e-9:
                continue
            changed = True
            if before and after and (before > 0) != (after > 0):
                supplement = False
            elif abs(before) > abs(after):
                supplement = False
        if not changed:
            return "一致"
        return "补充" if supplement else "同步"

    def sync_spread_position(self, name: str, *, force: bool = False,
                             runtime: bool = False) -> str:
        """按柜台持仓同步本地价差账：空仓则清零，两腿匹配则接管为策略层。

        只做“本地账对齐柜台”的写操作，不发单、不撤单；核对确认状态保留，
        因为柜台事实没有改变，改的只是本机记录。在途委托只提醒，``force=True``
        时照常同步——委托正是需要纠正的常见原因。
        """
        config = self.service.get(name)
        spread = self.spread_engine.get_spread(name)
        strategy = self._strategy(name)
        if config is None or spread is None or strategy is None:
            raise ManagedSpreadError("合约/价差/策略不存在，无法纠正")
        if list(spread.variable_symbols.values()) != config.legs:
            raise ManagedSpreadError("价差两腿与配置不符，禁止纠正")
        if not self.md_ready() or not self.td_ready():
            raise ManagedSpreadError("交易端或行情端未连接，请连接后再纠正")
        td = getattr(self._gateway(), "td_api", None)
        problem = self._snapshot_problem(td)
        if problem:
            raise ManagedSpreadError(f"柜台持仓快照不可用：{problem}；请点“核对柜台状态”重新查询")
        snapshot_generation = getattr(td, 'position_query_generation', None)
        snapshot_at = getattr(td, 'position_snapshot_at', None)
        snapshot = td.position_snapshot_sides
        cabinet: dict[str, dict[str, float]] = {}
        for symbol in config.legs:
            sides = snapshot.get(symbol.rsplit(".", 1)[0], {})
            if not isinstance(sides, dict):
                raise ManagedSpreadError(f"柜台 {symbol} 仓位数据不明，禁止纠正")
            try:
                values = {key: float(sides.get(key, 0) or 0)
                          for key in ("long", "short", "unknown")}
            except (TypeError, ValueError) as exc:
                raise ManagedSpreadError(f"柜台 {symbol} 仓位数据异常，禁止纠正") from exc
            if any(not 0 <= value < float("inf") for value in values.values()):
                raise ManagedSpreadError(f"柜台 {symbol} 仓位数据异常，禁止纠正")
            if values["unknown"] > 1e-9:
                raise ManagedSpreadError(f"柜台 {symbol} 有多空不分的仓位，无法自动纠正；请人工核对")
            cabinet[symbol] = values

        near, far = config.legs
        near_long, near_short = cabinet[near]["long"], cabinet[near]["short"]
        far_long, far_short = cabinet[far]["long"], cabinet[far]["short"]
        flat = all(value < 1e-9 for value in (near_long, near_short, far_long, far_short))
        target: dict[str, float] | None = None
        if flat:
            target = {near: 0.0, far: 0.0}
        elif (abs(near_long - far_short) < 1e-9 and near_long > 1e-9
              and near_short < 1e-9 and far_long < 1e-9):
            target = {near: near_long, far: -far_short}
        elif (abs(far_long - near_short) < 1e-9 and far_long > 1e-9
              and far_short < 1e-9 and near_long < 1e-9):
            target = {near: -near_short, far: far_long}
        if target is None:
            raise ManagedSpreadError(
                f"柜台两腿仓位不匹配（{near} 多{near_long:g}/空{near_short:g}，"
                f"{far} 多{far_long:g}/空{far_short:g}），自动纠正会算错对价，禁止执行；请人工处理"
            )

        # 只暂停目标策略；已经一致且无待核状态的运行合约不打断。
        deadline = self.armed.get(name)
        was_live = self.master_running and getattr(strategy, 'live_trading', False)
        running = self.master_running and (was_live or deadline is not None
                                           or not getattr(strategy, 'paused', False))
        if running:
            order_problem = self._orders_snapshot_problem(td)
            if order_problem:
                raise ManagedSpreadError(order_problem)
            state = strategy.get_pause_state()
            leg_pos = getattr(spread, 'leg_pos', {})
            consistent = all(abs(float(leg_pos.get(symbol, 0)) - target[symbol]) < 1e-9
                             for symbol in config.legs)
            if consistent and ((flat and not strategy.layers and not state.get('trades_by_order'))
                               or (not flat and self._layers_match_net(spread, strategy)
                                   and self._layers_well_formed(spread, strategy))):
                raise ManagedSpreadError('本地账已与柜台一致，无须重复纠正；保持当前交易状态')
            # 不为一次纠正打断仍在处理委托/成交明细的真实算法；保留原策略的保护与回报路由。
            algos = getattr(getattr(self.spread_engine, 'algo_engine', None), 'algos', {}) or {}
            for algoid in state.get('active_algoids', ()):
                if str(algoid).startswith('recovered_'):
                    continue
                algo = algos.get(algoid)
                if algo is None or not callable(getattr(algo, 'is_order_finished', None)) or not algo.is_order_finished():
                    raise ManagedSpreadError('算法仍在途或状态未知，未暂停策略；待柜台委托及成交结束后再核对纠正')
            self.armed.pop(name, None)  # 防止预启动看门狗在纠正期间放行
            strategy.live_trading = False
            try:
                strategy.pause_strategy()
            except Exception as exc:
                raise ManagedSpreadError(f'{name} 暂停失败，已关闭该合约发单许可：{exc}') from exc
            if not strategy.paused:
                raise ManagedSpreadError(f'{name} 未能暂停，已关闭该合约发单许可，未执行纠正')

        symbols = set(config.legs + [config.exchange_spread_vt_symbol])
        # 权威依据：柜台当日委托快照。柜台没有的 = 上一交易日（已随结算失效）或已终结，
        # 一律清掉本地记录；快照不可用时这一步什么都不做（交给下面的兜底规则）。
        pruned = self._prune_orders_absent_from_cabinet(name, config)
        trimmed_total = 0.0
        # 运行兜底只使用本轮完整柜台证据，不走人工纠正的时间戳兜底。
        if not runtime:
            pruned += self._prune_stale_order_records(name, config)
        blockers: list[str] = []
        try:
            active_orders = [
                order for order in self.main_engine.get_all_active_orders()
                if order.vt_symbol in symbols
            ]
        except Exception as exc:
            raise ManagedSpreadError(f"读取本机委托缓存失败：{exc}") from exc
        if active_orders:
            blockers.append(f"{len(active_orders)} 笔在途委托")
        order_ids = {getattr(order, "vt_orderid", "") for order in active_orders}
        state = strategy.get_pause_state()
        terminal = {"全部成交", "已撤销", "拒单", "ALLTRADED", "CANCELLED", "REJECTED"}
        if any(order.get("status") not in terminal for order in state.get("orders", {}).values()):
            blockers.append("策略记录的未终结委托")
        settled_trades = bool(state.get("trades_by_order"))
        settled_layers = len(strategy.layers)
        if not getattr(strategy, "paused", False) or getattr(strategy, "live_trading", False):
            raise ManagedSpreadError("策略必须已暂停且未获实时交易许可")
        algoids = list(state.get("active_algoids", []))
        algos = getattr(getattr(self.spread_engine, "algo_engine", None), "algos", None)
        for algoid in algoids:
            algo = algos.get(algoid) if isinstance(algos, dict) else None
            unfinished = (
                algo is None or not getattr(algo, "paused", False)
                or not hasattr(algo, "is_order_finished") or not algo.is_order_finished()
                or any(getattr(order.status, "value", order.status) not in terminal
                       for order in getattr(algo, "orders", {}).values())
            )
            if not unfinished:
                continue
            if str(algoid).startswith("recovered_"):
                # 恢复出来的只跟踪占位算法：不会发单，也只是提醒。
                blockers.append(f"恢复的在途算法 {algoid}")
                continue
            raise ManagedSpreadError("旧算法仍在途或状态未知，不能自动纠正；先在柜台确认委托")
        if blockers and not force:
            # 在途委托正是需要纠正的常见原因，因此只提醒；确认后照常同步本地账。
            raise ManagedSpreadError(
                "仍有" + "、".join(blockers)
                + "。纠正只改本机持仓记录，不会撤单或平仓；"
                "确认后仍可强制纠正（之后若有成交会按委托路由继续更新）"
            )
        data_engine = getattr(self.spread_engine, "data_engine", None)
        if data_engine is None or not callable(getattr(data_engine, "save_pos", None)):
            raise ManagedSpreadError("价差持仓持久化不可用，拒绝只改内存")
        # 价差引擎的 leg_pos 是 defaultdict：没有键就是 0，并不代表“数据不完整”。
        # 之前用 `symbol in leg_pos` 判断会误拦尚未成交过的新价差，这里补齐为 0 再继续。
        if not hasattr(spread, "leg_pos") or not hasattr(spread.leg_pos, "__setitem__"):
            raise ManagedSpreadError("价差对象没有本地腿仓记录，无法纠正")
        for symbol in config.legs:
            if symbol not in spread.leg_pos:
                spread.leg_pos[symbol] = 0.0
        old = {symbol: spread.leg_pos[symbol] for symbol in config.legs}
        old_layers = list(getattr(strategy, "layers", []) or [])
        old_direction = getattr(strategy, "direction", 0)
        unchanged = all(abs(float(old[symbol]) - target[symbol]) < 1e-9 for symbol in config.legs)
        if unchanged:
            if not flat and old_layers and self._adopted_layers_ok(name, spread, strategy):
                raise ManagedSpreadError("本地账已与柜台一致，无须重复纠正；可直接开启交易")
            if flat and not settled_layers and not settled_trades and not algoids:
                raise ManagedSpreadError("本地账已与柜台一致，无须重复纠正；可直接开启交易")
        # 不先清历史层账：保存失败时仍须保留原来的全部策略状态。
        # 不改变总开关；目标合约仍暂停，写入之后才考虑恢复。
        # 持仓可能在对话框确认期间变化：写入前再次核对同一份快照及委托状态。
        if (self._snapshot_problem(td)
                or any(float(spread.leg_pos[symbol]) != float(old[symbol]) for symbol in config.legs)
                or getattr(strategy, "live_trading", False) or not getattr(strategy, "paused", False)
                or len(strategy.layers) != settled_layers
                or strategy.get_pause_state().get("trades_by_order", {}) != state.get("trades_by_order", {})
                or strategy.get_pause_state().get("orders", {}) != state.get("orders", {})
                or strategy.get_pause_state().get("active_algoids", []) != state.get("active_algoids", [])
                or getattr(td, 'position_query_generation', None) != snapshot_generation
                or getattr(td, 'position_snapshot_at', None) != snapshot_at
                or getattr(td, "position_snapshot_sides", None) != snapshot
                or {getattr(order, "vt_orderid", "")
                    for order in self.main_engine.get_all_active_orders()
                    if getattr(order, "vt_symbol", "") in symbols} != order_ids):
            raise ManagedSpreadError("纠正前本地或柜台状态变化，请重新核对；交易许可已保持关闭")
        try:
            for symbol, volume in target.items():
                spread.leg_pos[symbol] = volume
            spread.calculate_pos()
            if not flat:
                if strategy.layers and not self._layers_match_net(spread, strategy):
                    # 层账与柜台净仓不符：先按差额补层（只增不减）；柜台少则削层。
                    added = strategy.reconcile_legacy_position()
                    if added <= 0 or not self._layers_match_net(spread, strategy):
                        trimmer = getattr(strategy, "trim_legacy_position", None)
                        trimmed = float(trimmer() or 0) if callable(trimmer) else 0.0
                        if trimmed <= 0 or not self._layers_match_net(spread, strategy):
                            raise ManagedSpreadError(
                                "策略层账与柜台仓位不一致，需人工逐层核对；未做修改")
                        trimmed_total += trimmed
                # 净仓对得上但层结构被合并（如 5 手并成 1 层）：按每层 layer_volume 手重建，
                # 否则止盈会整层重挂、且只占 1 个层位 → 还能再开 2 层。
                self._normalize_adopted_layers(name, spread, strategy)
                if not strategy.layers and not strategy.adopt_legacy_position():
                    raise ManagedSpreadError(
                        "柜台有真实持仓，但缺少两腿持仓均价，无法生成接管层；"
                        "请在柜台确认成交均价后再处理"
                    )
            data_engine.save_pos()
        except Exception as exc:
            for symbol, volume in old.items():
                spread.leg_pos[symbol] = volume
            spread.calculate_pos()
            strategy.layers[:] = old_layers
            if hasattr(strategy, "direction"):
                strategy.direction = old_direction
            if isinstance(exc, ManagedSpreadError):
                raise
            raise ManagedSpreadError(
                f"本地持仓保存失败，已回滚内存；交易仍关闭，请检查磁盘后重试：{exc}"
            ) from exc
        from vnpy.trader.constant import Status
        if flat:
            self._sync_flat_caches(config)
            strategy.layers.clear()
            if hasattr(strategy, "direction"):
                strategy.direction = 0
            if hasattr(strategy, "reserved_exit_orders"):
                strategy.reserved_exit_orders.clear()
        else:
            # 层账已建立：先清掉方向与当前层矛盾的旧平仓单，再绑定仍然有效的。
            if not runtime:
                pruned += self._prune_stale_order_records(name, config, check_directions=True)
            self._bind_existing_exit_orders(name, config, strategy, spread)
        # 无论空仓还是有仓，柜台已有的建仓/加仓委托都占住开仓槽位。
        self._bind_existing_open_orders(name, config, strategy)
        refresh_variables = getattr(strategy, "_refresh_variables", None)
        if callable(refresh_variables):
            try:
                refresh_variables()
            except Exception as exc:
                self.errors[name] = f"持仓已保存，但策略层账刷新失败：{exc}；请重新核对"
                raise ManagedSpreadError(self.errors[name]) from exc
        save_layers = getattr(strategy, "_save_layers", None)
        if callable(save_layers):
            save_layers()
        for algoid in algoids:
            algos[algoid].status = Status.CANCELLED
            strategy.algoids.discard(algoid)
        if algoids and callable(getattr(strategy, "_archive_stale_algos", None)):
            try:
                strategy._archive_stale_algos()
            except Exception as exc:
                self.errors[name] = f"持仓已保存，但算法归档失败：{exc}；请重新核对"
                raise ManagedSpreadError(self.errors[name]) from exc
        strategy.pause_position_at = 0.0
        for field in ("pause_order_reports", "pause_trade_reports", "_pause_trade_baseline"):
            reports = getattr(strategy, field, None)
            if isinstance(reports, dict):
                reports.clear()
        writer = getattr(self.main_engine, "write_log", None)
        label = self._position_change_label(old, target, config.legs)
        if flat:
            message = (f"{name}〔{label}〕柜台确认为双腿空仓，本地价差已清零并保存；"
                       f"归档 {settled_layers} 层历史层账、{len(algoids)} 个无在途委托的旧算法"
                       + ("及已终结的成交记录；" if settled_trades else "；")
                       + "核对状态保留，可直接开启交易")
        else:
            layers = list(getattr(strategy, "layers", []) or [])
            layer_text = "；".join(layer.describe() for layer in layers) or "无"
            message = (f"{name}〔{label}〕已按柜台仓位同步本地账："
                       f"{near} {target[near]:g} / {far} {target[far]:g}；"
                       f"策略层账：{layer_text}；核对状态保留，可直接开启交易")
        if pruned:
            message += f"；同时清理了 {pruned} 条与当前仓位/回报不符的旧委托记录"
        if trimmed_total:
            message += f"；按柜台削减了 {trimmed_total:g} 手（不产生盈亏，已归档被削的层）"
        if callable(writer):
            try:
                if not running:
                    writer(message)
                if blockers and force:
                    writer(
                        f"警告：{name} 是在仍有「{'、'.join(blockers)}」时强制同步本地持仓的；"
                        f"没有撤销任何委托，后续成交会按委托路由继续更新"
                    )
            except Exception:
                pass  # 日志写入失败不能把已经完成的持仓修正伪装成失败。
        # 同步后刷新“上次会话遗留委托”，重启后仍能提示。
        self._remember_orders()
        if running:
            message = message.replace('核对状态保留，可直接开启交易', '本地账已保存')
            try:
                if not self.master_running or name not in self.enabled or not (was_live or deadline is not None):
                    raise ManagedSpreadError('该合约未获交易许可')
                config = self.validate(name)
                if was_live and not runtime:
                    problem = self.quote_problem(config)
                    if problem:
                        raise ManagedSpreadError(problem)
                if not strategy.resume_strategy(reconciled=True):
                    raise ManagedSpreadError('暂停期间订单未核对，禁止恢复')
                self.validate(name)
                if was_live:
                    strategy.runtime_position_blocked = False
                    strategy.live_trading = True
                    message += '；该合约已恢复交易'
                else:
                    self.armed[name] = deadline
                    message += '；该合约继续等待开盘报价'
            except Exception as exc:
                strategy.live_trading = False
                strategy.pause_strategy()
                message += f'；该合约保持暂停：{exc}'
            if callable(writer):
                try:
                    writer(message)
                except Exception:
                    pass
        return message

    def sync_all(self, *, force: bool = True) -> list[tuple[str, str, str]]:
        """对所有已注册合约执行“按柜台同步持仓”，返回 (合约, 结果, 说明)。

        结果取值：``ok`` 已同步、``skip`` 本来就一致、``fail`` 无法自动处理。
        """
        results: list[tuple[str, str, str]] = []
        for name in sorted(self.service.list()):
            try:
                message = self.sync_spread_position(name, force=force)
                results.append((name, "ok", message))
            except ManagedSpreadError as exc:
                text = str(exc)
                kind = "skip" if "无须重复纠正" in text else "fail"
                results.append((name, kind, text))
            except Exception as exc:  # noqa: BLE001 - 单个失败不能中断其它合约
                results.append((name, "fail", str(exc)))
        # 每个价差一行结论，方便复盘"哪些动了、哪些没动、为什么没动"。
        writer = getattr(self.main_engine, "write_log", None)
        if callable(writer):
            for name, kind, message in results:
                if kind == "ok":
                    continue        # 成功的那条已在 sync_spread_position 里写过
                label = "一致" if kind == "skip" else "未纠正"
                try:
                    writer(f"{name}〔{label}〕{message}")
                except Exception:
                    pass
        return results

    def repair_flat_spread(self, name: str, *, force: bool = False) -> str:
        """兼容旧按钮/调用：等同于“按柜台同步本地账”。"""
        return self.sync_spread_position(name, force=force)

    def _can_adopt_legacy(self, strategy) -> bool:
        checker = getattr(strategy, "can_adopt_legacy_position", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    def _adopted_layers_ok(self, name: str, spread, strategy) -> bool:
        layers = list(getattr(strategy, "layers", []) or [])
        if not layers or not all(getattr(layer, "adopted", False) for layer in layers):
            return False
        net_pos = float(getattr(spread, "net_pos", 0) or 0)
        expected = sum(float(layer.direction) * float(layer.volume) for layer in layers)
        return (abs(net_pos) > 1e-9 and abs(expected - net_pos) < 1e-6
                and not getattr(strategy, "pause_trade_reports", {})
                and self._layers_well_formed(spread, strategy))

    def _layers_well_formed(self, spread, strategy) -> bool:
        """层结构是否符合「每层 layer_volume 手」。

        净仓对得上但层结构被合并（如 5 手并成「第1层 5手」）时，挂单量、止损额
        与 ``max_positions`` 坑位全是错的：止盈会整层重挂（5 手）或只挂 1 手，
        且只占 1 个层位，策略还能再开 2 层。
        """
        import math

        layers = [layer for layer in (getattr(strategy, "layers", []) or [])
                  if getattr(layer, "is_open", True)]
        if not layers:
            return False
        net_pos = float(getattr(spread, "net_pos", 0) or 0)
        if abs(net_pos) < 1e-9:
            return False
        lot = max(1.0, float(getattr(strategy, "layer_volume", 1.0) or 1.0))
        volumes = [float(getattr(layer, "volume", 0) or 0) for layer in layers]
        if any(volume <= 1e-9 or volume > lot + 1e-9 for volume in volumes):
            return False
        if len(layers) != math.ceil(abs(net_pos) / lot - 1e-9):
            return False
        signs = {1 if float(getattr(layer, "direction", 0) or 0) > 0 else -1
                 for layer in layers}
        return signs == {1 if net_pos > 0 else -1}

    def _normalize_adopted_layers(self, name: str, spread, strategy) -> bool:
        """把“整仓并成一层”的接管层按每层 layer_volume 手重建，返回是否重建。

        只动全部由接管产生的层；策略自己开仓建立的层保持原样（每层均价不同，
        重建会改写止损/止盈基准）。
        """
        layers = [layer for layer in (getattr(strategy, "layers", []) or [])
                  if getattr(layer, "is_open", True)]
        if not layers or self._layers_well_formed(spread, strategy):
            return False
        if not all(getattr(layer, "adopted", False) for layer in layers):
            return False
        lot = max(1.0, float(getattr(strategy, "layer_volume", 1.0) or 1.0))
        before = "、".join(layer.describe() for layer in layers
                          if callable(getattr(layer, "describe", None)))
        strategy.layers.clear()
        if hasattr(strategy, "direction"):
            strategy.direction = 0
        if not strategy.adopt_legacy_position():
            return False
        writer = getattr(self.main_engine, "write_log", None)
        if callable(writer):
            writer(
                f"{name}：层结构不规范（{before}），已按每层 {lot:g} 手重建为 "
                f"{len(strategy.layers)} 层："
                + "、".join(layer.describe() for layer in strategy.layers)
            )
        return True

    def _layers_match_net(self, spread, strategy) -> bool:
        """层账合计是否与价差净仓一致（**不要求**层是接管来的）。

        与 ``_adopted_layers_ok`` 的区别：允许策略自己开仓建立的层，只要数量与
        柜台净仓对得上、且没有待归属的暂停期成交。用于"核对并纠正"——
        2026-11-11 的事故里本地是真实开仓层，`_adopted_layers_ok` 永远返回 False，
        于是纠正被拒绝，多出的手数一直没人管。
        """
        layers = [layer for layer in (getattr(strategy, "layers", []) or [])
                  if getattr(layer, "is_open", False)]
        if not layers:
            return False
        if getattr(strategy, "pause_trade_reports", {}):
            return False
        net_pos = float(getattr(spread, "net_pos", 0) or 0)
        if abs(net_pos) < 1e-9:
            return False
        expected = sum(float(layer.direction) * float(layer.volume) for layer in layers)
        return abs(expected - net_pos) < 1e-6

    def _reserved_ids(self, strategy) -> set[str]:
        """已被柜台委托占用的槽位：这些委托/算法不再作为“阻止交易”的理由。"""
        reserved: set[str] = set()
        for field in ("reserved_exit_orders", "reserved_open_orders"):
            values = getattr(strategy, field, None)
            if isinstance(values, dict):
                reserved.update(str(k) for k in values)
        return reserved

    def _unreconciled_account(self, config: SpreadConfig, *,
                              allow_adopted: bool = False) -> str:
        # 在途委托是账户状态，不算敞口；真正的敞口由逐腿比对判断。
        td = getattr(self._gateway(), "td_api", None)
        cabinet = self._cabinet_leg_differences(
            config, self.spread_engine.get_spread(config.name))
        if cabinet is not None:
            if any(abs(leg["cabinet"]) > 1e-9 or (leg["long"] > 1e-9 and leg["short"] > 1e-9)
                   for leg in cabinet):
                if allow_adopted and self._layers_reconciled(config.name):
                    return ""
                return "柜台已有单腿持仓，逐层持仓尚未对账；为防止错误开平仓，禁止恢复"
            return ""
        positions = [p for p in self.main_engine.get_all_positions()
                     if p.vt_symbol in config.legs and p.volume]
        if positions:
            if allow_adopted and self._layers_reconciled(config.name):
                return ""
            return "柜台已有单腿持仓，逐层持仓尚未对账；为防止错误开平仓，禁止恢复"
        return ""

    def _layers_reconciled(self, name: str) -> bool:
        """本地层账是否已与柜台对得上（接管层 **或** 策略自己开的层都算）。

        早先这里只认 ``_adopted_layers_ok``（要求每层都是启动时接管的）和
        ``_can_adopt_legacy``（要求一层都没有）。策略自己开仓建立的层（adopted=False）
        两条都不满足，于是即便层账与柜台净仓完全一致，也会被判成“逐层持仓尚未对账”
        而禁止开启交易（2026-11-20 A/RM 实测：1 层 1 手 vs 柜台 1 手，仍被拦）。
        """
        strategy = self._strategy(name)
        spread = self.spread_engine.get_spread(name)
        if strategy is None or spread is None:
            return False
        return (self._adopted_layers_ok(name, spread, strategy)
                or self._layers_match_net(spread, strategy)
                or self._can_adopt_legacy(strategy))

    def _sync_flat_caches(self, config: SpreadConfig) -> None:
        """柜台已确认双腿空仓：清掉本机 OMS 旧腿仓和抵扣转换器持仓。

        只改本地缓存，不碰任何柜台委托/持仓。vn.py 的空仓查询不会删除旧键，
        不对齐的话下一次开平仓会按过期的平今/平昨数量拆单。
        """
        try:
            oms = self.main_engine.get_engine("oms") if hasattr(self.main_engine, "get_engine") else None
        except Exception:
            oms = None
        if oms is None:
            return
        legs = set(config.legs)
        try:
            for key, position in list(getattr(oms, "positions", {}).items()):
                if getattr(position, "vt_symbol", "") in legs:
                    oms.positions.pop(key, None)
            for converter in getattr(oms, "offset_converters", {}).values():
                holdings = getattr(converter, "holdings", None)
                if isinstance(holdings, dict):
                    for symbol in legs:
                        holdings.pop(symbol, None)
        except Exception as exc:
            self.errors[config.name] = f"本地旧持仓缓存清理失败：{exc}；请重启后再开启交易"

    def validate(self, name: str) -> SpreadConfig:
        config = self.service.get(name)
        if config is None:
            raise ManagedSpreadError("合约不在注册表")
        if name in self.errors:
            raise ManagedSpreadError(self.errors[name])
        spread = self.spread_engine.get_spread(name)
        strategy = self._strategy(name)
        if spread is None or strategy is None:
            raise ManagedSpreadError("价差/策略尚未注册，请等待自动重试")
        self._prune_finished_recovered(name)
        if getattr(strategy, "live_trading", False) and not self.master_running:
            strategy.pause_strategy()
            strategy.live_trading = False
            raise ManagedSpreadError("发现未受总开关管理的运行策略，已暂停；请先人工核对")
        if name not in self.reconciled_contracts:
            raise ManagedSpreadError(
                f"{name} 尚未人工核对柜台委托、成交及两腿持仓；禁止交易。"
                "请先选中该合约并点顶部「核对柜台状态」"
            )
        if not self.md_ready() or not self.td_ready():
            raise ManagedSpreadError("行情或交易端已断开；请重连后重新核对柜台状态")
        if self._last_account_key is not None and self._account_key() != self._last_account_key:
            raise ManagedSpreadError("核对后登录账户已变化；请重新核对柜台状态")
        if list(spread.variable_symbols.values()) != config.legs or list(spread.variable_symbols) != ["near", "far"]:
            raise ManagedSpreadError("已注册价差的 near/far 腿与配置不符")
        leg_differences = self._cabinet_leg_differences(config, spread)
        td = getattr(self._gateway(), "td_api", None)
        problem = self._snapshot_problem(td)
        if problem:
            raise ManagedSpreadError(f"柜台持仓快照不可用：{problem}；请重新核对")
        order_problem = self._orders_snapshot_problem(td)
        if order_problem:
            raise ManagedSpreadError(order_problem)
        if leg_differences is None and hasattr(td, "position_snapshot_at"):
            raise ManagedSpreadError("柜台与本地持仓无法可靠比较；请重新核对，禁止开启交易")
        if leg_differences is not None and any(
                abs(leg["difference"]) > 1e-9 or (leg["long"] > 1e-9 and leg["short"] > 1e-9)
                for leg in leg_differences):
            details = "；".join(
                f"{leg['symbol']} 本地{leg['local']:g}/柜台净仓{leg['cabinet']:g}"
                f"（多{leg['long']:g}/空{leg['short']:g}）"
                for leg in leg_differences
                if abs(leg["difference"]) > 1e-9 or (leg["long"] > 1e-9 and leg["short"] > 1e-9)
            )
            raise ManagedSpreadError(f"本地与柜台持仓不一致或双向持仓：{details}；先核对并纠正，禁止开启交易")
        if getattr(spread, "net_pos", 0) or any(getattr(spread, "leg_pos", {}).values()):
            if not (self._adopted_layers_ok(name, spread, strategy)
                    or self._layers_match_net(spread, strategy)
                    or self._can_adopt_legacy(strategy)):
                raise ManagedSpreadError(
                    "引擎记录存在未核对的价差/单腿仓位；拒绝在空层账上重启。"
                    "若为上次遗留仓位，请确认两腿仓位/均价一致后由策略接管"
                )
        # 行情新鲜度只在真正启动交易时要求；授予交易许可不必等开盘。
        checked_config, listed = self.service.verify_and_save(name, self.main_engine.get_contract)
        if not listed:
            raise ManagedSpreadError("柜台未提供交易所组合合约；不自动降级为两腿")
        if config.executor != "exchange_spread" or (
            getattr(strategy, "_executor_explicit", False) and strategy.executor != "exchange_spread"
        ):
            raise ManagedSpreadError("GUI 仅开放经验证的组合单执行；策略不能强制改为两腿")
        account_issue = self._unreconciled_account(config, allow_adopted=True)
        if account_issue:
            raise ManagedSpreadError(account_issue)
        state = strategy.get_pause_state()
        # 已被柜台委托占住槽位的委托/算法不再阻止开启（它们不会导致重复下单）。
        reserved = self._reserved_ids(strategy)
        trades = {key: value for key, value in state.get("trades_by_order", {}).items()
                  if key not in reserved}
        if trades:
            raise ManagedSpreadError("暂停后有新成交；需核对成交归属与逐层持仓，禁止直接恢复交易")
        active_algos = [
            algoid for algoid in state["active_algoids"]
            if algoid not in reserved
            and algoid.replace("recovered_", "", 1) not in reserved
        ]
        pending = {
            key: value for key, value in state["orders"].items() if key not in reserved
        }
        if active_algos or any(
            report.get("status") not in ("全部成交", "已撤销", "拒单", "ALLTRADED", "CANCELLED", "REJECTED")
            for report in pending.values()
        ):
            raise ManagedSpreadError("旧算法或委托仍在途；请在柜台人工处置后再核对恢复")
        if strategy.layers or state["spread_net_pos_now"] or state["spread_net_pos_at_pause"]:
            if not (self._adopted_layers_ok(name, spread, strategy)
                    or (self._layers_match_net(spread, strategy) and self._layers_well_formed(spread, strategy))):
                raise ManagedSpreadError(
                    "价差存在旧持仓/策略层账；柜台与策略未完成逐层核对，禁止恢复"
                )
        if checked_config.size <= 0 or checked_config.pricetick <= 0:
            raise ManagedSpreadError("柜台合约乘数/最小变动未确认")
        if checked_config.margin_per_lot > 0:
            try:
                accounts = [
                    item for item in self.main_engine.get_all_accounts()
                    if getattr(item, "gateway_name", "") == "CTP"
                ]
                available = sum(float(getattr(item, "available", 0) or 0) for item in accounts)
                if accounts and available < checked_config.margin_per_lot:
                    self.main_engine.write_log(
                        f"保证金提示（不阻断）：{name} 可用资金 {available:g} 元 "
                        f"小于每手保证金 {checked_config.margin_per_lot:g} 元；"
                        f"交易所组合保证金可能不同，请自行确认"
                    )
            except Exception:
                pass
        if not self.is_reconciled(name):
            raise ManagedSpreadError("核对确认已失效，请重新核对当前合约的委托和两腿持仓")
        return checked_config

    def enable(self, name: str, enabled: bool, *, force: bool = False):
        if enabled:
            if self.master_running:
                raise ManagedSpreadError("请先停止总交易，再启用其他合约并统一确认启动")
            if force:
                raise ManagedSpreadError("不允许强制绕过合约核对")
            self.validate(name)
            self.service.set_trading_enabled(name, True)
            try:
                self.validate(name)  # 配置落盘时仍可能有新的柜台回报。
            except Exception:
                self.service.set_trading_enabled(name, False)
                self.enabled.discard(name)
                raise
            self.enabled.add(name)
        else:
            self.enabled.discard(name)
            strategy = self._strategy(name)
            if strategy:
                strategy.pause_strategy()  # 不能调用 on_stop；留住柜台委托
                strategy.live_trading = False
            try:
                self.service.set_trading_enabled(name, False)
            except Exception as exc:
                raise ManagedSpreadError(f"已暂停该合约，但保存允许状态失败：{exc}") from exc
            if self.master_running and not self.enabled:
                self.stop_all(reason="所有合约已暂停")

    def remove(self, name: str, *, force_offline: bool = False,
               force_exposure: bool = False):
        """删除合约：先列出所有未了结的状态，再移除策略/价差，最后原子删配置。

        默认遇到持仓、在途委托或未终结算法会拒绝并列出原因；
        ``force_exposure=True`` 表示用户已明确选择“带着这些状态删除、由自己在柜台
        管理”，此时不再拦截，但会逐条写日志提醒。不会自动撤单或平仓。
        配置写入失败时回滚引擎注册，避免“界面没了、策略还在”。
        """
        if self.master_running:
            raise ManagedSpreadError("请先停止总交易，再删除合约")
        config = self.service.get(name)
        if config is None:
            raise ManagedSpreadError("合约不在注册表")

        strategy = self._strategy(name)
        spread = self.spread_engine.get_spread(name)

        blockers: list[str] = []
        if spread is not None and (
                getattr(spread, "net_pos", 0) or any(getattr(spread, "leg_pos", {}).values())):
            blockers.append("本地价差/单腿仓位")
        state = strategy.get_pause_state() if strategy is not None else {}
        active_algos = list(state.get("active_algoids", []) or [])
        if active_algos:
            blockers.append(f"{len(active_algos)} 个在途算法")
        pending_orders = [
            orderid for orderid, report in state.get("orders", {}).items()
            if report.get("status") not in ("全部成交", "已撤销", "拒单",
                                            "ALLTRADED", "CANCELLED", "REJECTED")
        ]
        if pending_orders:
            blockers.append(f"{len(pending_orders)} 笔策略记录的未终结委托")
        if strategy is not None and (strategy.layers or state.get("spread_net_pos_now")
                                     or state.get("spread_net_pos_at_pause")):
            blockers.append(f"{len(strategy.layers)} 层策略持仓账")

        symbol_set = set(config.legs + [config.exchange_spread_vt_symbol])
        try:
            active_orders = [
                order for order in self.main_engine.get_all_active_orders()
                if order.vt_symbol in symbol_set
            ]
        except Exception as exc:
            raise ManagedSpreadError(f"读取本机委托缓存失败，无法判断是否有在途委托：{exc}") from exc
        if active_orders:
            blockers.append(f"{len(active_orders)} 笔柜台在途委托")
        try:
            leg_positions = [
                position for position in self.main_engine.get_all_positions()
                if position.vt_symbol in config.legs and abs(float(position.volume or 0)) > 1e-9
            ]
        except Exception as exc:
            raise ManagedSpreadError(f"读取本机持仓缓存失败，无法判断是否有持仓：{exc}") from exc
        if leg_positions:
            blockers.append("柜台单腿持仓")

        if self.td_ready() and not self.is_reconciled(name):
            blockers.append("尚未核对本次柜台状态")
        if blockers and not force_exposure:
            raise ManagedSpreadError(
                "；".join(blockers)
                + "。删除不会替你撤单或平仓，这些会留在柜台；"
                "确认自行管理后可选择强制删除"
            )
        if not self.td_ready() and not force_offline:
            raise ManagedSpreadError("交易端未连接，无法确认柜台持仓；如已确认无持仓，请选择离线删除")

        if strategy is not None:
            strategy.live_trading = False
            strategy.pause_strategy()
            strategy.trading = False
            try:
                removed = self.spread_engine.strategy_engine.remove_strategy(strategy.strategy_name)
            except Exception as exc:
                raise ManagedSpreadError(f"移除策略实例失败，配置未删除：{exc}") from exc
            if not removed:
                raise ManagedSpreadError("策略实例移除失败；请确认策略已暂停")

        if spread is not None:
            self.spread_engine.remove_spread(name)
            algo_cache = getattr(getattr(self.spread_engine, "algo_engine", None), "spreads", None)
            if isinstance(algo_cache, dict):
                algo_cache.pop(name, None)
            # vn.py 的 remove_spread 不会重写持仓文件；不清掉旧条目，
            # 下次重启 load_pos 会把已删合约的旧仓位又读回来。
            data_engine = getattr(self.spread_engine, "data_engine", None)
            if callable(getattr(data_engine, "save_pos", None)):
                try:
                    data_engine.save_pos()
                except Exception as exc:
                    # 残留条目在重启时因价差已不存在而被忽略，不影响正确性。
                    self.spread_engine.write_log(
                        f"提示：{name} 已删除，但持仓文件未同步清理：{exc}")

        try:
            self.service.remove(name)
        except Exception as exc:
            try:
                if spread is not None:
                    self.service.register(self.spread_engine, config)
                if strategy is not None:
                    self.service.ensure_strategy(self.spread_engine, config)
                    revived = self._strategy(name)
                    if revived is not None:
                        revived.pause_strategy()
                        revived.live_trading = False
            except Exception as rollback_exc:
                self.errors[name] = f"删除配置失败且回滚不完整：{exc}; {rollback_exc}"
            raise ManagedSpreadError(f"删除配置失败，已尝试恢复引擎注册：{exc}") from exc

        self.enabled.discard(name)
        self.errors.pop(name, None)
        self.spread_engine.write_log(
            f"合约已删除：{config.display_name or name}"
            f"（策略/价差已移除，config/spreads.json 已删；已采集的历史 tick 不受影响）"
        )
        if force_exposure and blockers:
            self.spread_engine.write_log(
                f"警告：{name} 是在仍有「{'、'.join(blockers)}」的情况下被删除的；"
                f"本程序不会替你撤销这些委托或平仓，请在柜台自行管理"
            )
        return config

    def start_all(self, *, force: bool = False, preopen: bool = False) -> list[str]:
        """启动全部已勾选合约。

        ``preopen=True`` 允许在没有盘口（未开盘／集合竞价）时先挂好：策略照常
        初始化、接管层账、绑定委托并恢复（``paused=False``、``trading=True``），
        但 ``live_trading`` 保持 False，因此不会有任何报单；等 :meth:`tick_armed`
        看到有效两腿报价且重新核验柜台事实后，才逐合约开启实盘发单。默认 ``False`` 时
        行为不变：没有有效报价直接拒绝启动。
        """
        if self.master_running:
            raise ManagedSpreadError("总交易已经启动，禁止重复执行初始化")
        if not self.enabled:
            raise ManagedSpreadError("尚未开启任何合约的交易许可")
        if force:
            raise ManagedSpreadError("不允许强制绕过柜台核对；请处理差异后普通启动")
        # 先把柜台多出的持仓补进本地账与策略层，否则 validate() 会直接以
        # “本地少仓”拒绝启动，多出来的手数就一直没有策略管（2026-11-11 事故）。
        self._align_cabinet_positions()
        checked: dict[str, SpreadConfig] = {}
        arming: list[str] = []
        for name in sorted(self.enabled):
            config = self.validate(name)
            problem = self.quote_problem(config)
            if problem:
                if not preopen:
                    raise ManagedSpreadError(problem)
                arming.append(name)
            checked[name] = config
        self.armed.clear()
        self.arm_blockers.clear()
        self._armed_session = self.startup_session()
        started: list[str] = []
        try:
            for name, config in checked.items():
                strategy = self._strategy(name)
                strategy.managed_by_console = True   # on_start 不再误报"只计算不发单"
                strategy.pause_strategy()
                strategy.live_trading = False
                if not strategy.inited:
                    # vn.py 包装层会吞下 on_init 异常后仍标记 inited=True；
                    # 因此只在本项目 GUI 内直调回调并由异常传递到统一暂停回滚。
                    strategy.on_init()
                    strategy.inited = True
                # 策略执行器默认值为 two_leg，必须在本次确认后设置为组合单。
                strategy.executor = config.executor
                strategy._load_static_config()
                strategy._refresh_contract_metrics()
                spread = self.spread_engine.get_spread(name)
                if spread is not None and abs(float(getattr(spread, "net_pos", 0) or 0)) > 1e-9:
                    # 差额接管：本地已有层时只补柜台多出的那部分（只增不减）
                    strategy.reconcile_legacy_position()
                    # 合并过的接管层在这里按每层 layer_volume 手重建（每层 1 手挂 1 张止盈）
                    self._normalize_adopted_layers(name, spread, strategy)
                    if not strategy.layers:
                        raise ManagedSpreadError(
                            f"{name} 存在旧持仓，但两腿仓位不一致或缺少持仓均价，无法自动接管"
                        )
                if spread is not None and strategy.layers:
                    # 柜台当日委托快照里没有的旧单 = 上一交易日失效，先清掉再绑，
                    # 否则它会一直占着止盈位，策略挂不出新单。
                    self._prune_orders_absent_from_cabinet(name, config)
                    # 启动前再绑一次：柜台已有平仓单时占住止盈位，避免重复挂单。
                    self._bind_existing_exit_orders(name, config, strategy, spread)
                self._bind_existing_open_orders(name, config, strategy)
                # 初始化/接管期间如果出现柜台回报或核对失效，必须在策略放行前再验一次。
                self.validate(name)
                strategy.on_start()  # 安全暂停不改 vn.py trading 标志；每次恢复重建过滤器
                strategy.trading = True
                strategy._rebuild_filters()
                if not strategy.resume_strategy(reconciled=True):
                    raise ManagedSpreadError(f"{name} 暂停期间订单未核对，禁止恢复")
                td = getattr(self._gateway(), "td_api", None)
                evidence = self._startup_snapshot_identity(td)
                self.validate(name)
                writer = getattr(strategy, "write_log", None)
                if self.startup_session() != self._armed_session:
                    raise ManagedSpreadError("启动时交易会话变化，禁止放行")
                problem = self.quote_problem(config)
                if self._startup_snapshot_identity(td) != evidence:
                    problem = "柜台查询或回报在核验期间发生变化，等待重新核验"
                if problem and not preopen:
                    raise ManagedSpreadError(problem)
                strategy.runtime_position_blocked = False
                if name in arming or problem:
                    # 待命：策略已就绪但不放行实盘，行情与柜台检查均通过才开。
                    strategy.live_trading = False
                    self.armed[name] = float("inf")
                    self.arm_blockers[name] = problem or "等待下一轮开盘核验"
                    if callable(writer):
                        writer("策略已待命，实盘发单关闭；两腿报价和柜台核验通过后自动放行")
                else:
                    strategy.live_trading = True
                    if callable(writer):
                        writer(
                            "实盘发单已开启（live_trading=True）：此后所有信号都会真实报单；"
                            "如需纸面观察请先「停止交易」"
                        )
                    started.append(name)
        except Exception:
            self.stop_all(reason="启动失败，全部策略已暂停")
            raise
        self.master_running = True
        if self.armed:
            writer = getattr(self.main_engine, "write_log", None)
            if callable(writer):
                writer(
                    f"预启动：{len(self.armed)} 个合约持续待命，等待有效报价和柜台核验"
                )
        return started

    def startup_session(self) -> tuple | None:
        """启动仅授权当前已连接的交易会话，不继承到另一个账户或重登录。"""
        if not (self.md_ready() and self.td_ready()):
            return None
        td = getattr(self._gateway(), "td_api", None)
        return (id(td), self._account_key(), getattr(td, "frontid", None),
                getattr(td, "sessionid", None), getattr(td, "account_cache_token", None))

    @staticmethod
    def _startup_snapshot_identity(td) -> tuple:
        return tuple(getattr(td, field, None) for field in (
            "position_query_generation", "position_query_pending", "position_snapshot_at",
            "orders_query_pending", "orders_snapshot_ok", "orders_snapshot_at", "reconciliation_event_error"))

    def tick_armed(self) -> list[str]:
        """持续待命；仅在当前会话、盘口和柜台核验均通过后逐个放行。"""
        if not self.master_running or not self.armed:
            return []
        if self.startup_session() != self._armed_session:
            self.stop_all(reason="交易会话已断开或变化，已取消待命")
            return []
        writer = getattr(self.main_engine, "write_log", None)
        released: list[str] = []
        for name in list(self.armed):
            config = self.service.get(name)
            strategy = self._strategy(name) if config is not None else None
            if name not in self.enabled:
                self.armed.pop(name, None)
                self.arm_blockers.pop(name, None)
                continue
            if config is None or strategy is None or not strategy.trading or strategy.paused:
                self.arm_blockers[name] = "策略未就绪，请检查策略状态"
                continue
            problem = self.quote_problem(config)
            if problem:
                self.arm_blockers[name] = problem
                continue
            try:
                td = getattr(self._gateway(), "td_api", None)
                evidence = self._startup_snapshot_identity(td)
                self.validate(name)
                spread = self.spread_engine.get_spread(name)
                # 等待期间可能出现人工委托，重新依据当日快照占位，避免重复报单。
                self._prune_orders_absent_from_cabinet(name, config)
                if spread is not None and strategy.layers:
                    self._bind_existing_exit_orders(name, config, strategy, spread)
                self._bind_existing_open_orders(name, config, strategy)
                self.validate(name)
                self._validate_quote(config)
            except ManagedSpreadError as exc:
                self.arm_blockers[name] = str(exc)
                continue
            if self.startup_session() != self._armed_session:
                self.stop_all(reason="放行时交易会话已变化，已取消待命")
                return released
            if self._startup_snapshot_identity(td) != evidence:
                self.arm_blockers[name] = "柜台查询或回报在核验期间发生变化，等待重新核验"
                continue
            strategy.runtime_position_blocked = False
            strategy.live_trading = True
            self.armed.pop(name, None)
            self.arm_blockers.pop(name, None)
            released.append(name)
            if callable(writer):
                writer(f"{name} 待命放行：两腿报价与柜台核验通过，实盘发单开关已开启")
        return released

    @property
    def armed_names(self) -> list[str]:
        """仍在等待盘口或柜台安全条件的合约（界面用）。"""
        return sorted(self.armed)

    def stop_all(self, *, reason: str = "用户停止交易"):
        self.master_running = False
        self.armed.clear()
        self.arm_blockers.clear()
        self._armed_session = None
        # 停止交易不等于核对失效：保留几分钟内的核对快照，关窗再开不必重来。
        self.invalidate_reconciliation(forget=False)
        for strategy in self.spread_engine.strategy_engine.strategies.values():
            if hasattr(strategy, "pause_strategy"):
                strategy.live_trading = False
                try:
                    strategy.pause_strategy()
                except Exception as exc:
                    strategy.paused = True
                    try:
                        for algo in strategy._owned_algos():
                            pause_algo = getattr(algo, "pause", None)
                            if pause_algo:
                                pause_algo()
                    except Exception as algo_exc:
                        self.errors[strategy.spread_name] = f"暂停回调和算法暂停均失败，立即人工处置：{algo_exc}"
                    else:
                        self.errors[strategy.spread_name] = f"暂停回调失败，已强制关闭动作：{exc}"
                finally:
                    strategy.live_trading = False
        self.errors["__notice__"] = f"{reason}：策略暂停，已有委托保留且仍可成交；不自动止损"
        self._remember_orders()
