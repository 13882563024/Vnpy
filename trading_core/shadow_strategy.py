"""Single-writer, offline 1:1 spread strategy. No broker, clock or live order API.

Use a dedicated shadow journal and an immutable config per account/strategy. Inputs
are the source of truth; replay projects LayerBook and can repair a missing local
intent after interruption, but never emits that intent again to the caller.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import json
from math import isfinite

from utils.calendar import trading_days_between
from utils.stat_window import StatWindow
from .intents import OrderIntent
from .journal import Journal, Outcome
from .layers import Enter, Fill, LayerBook, Protect, RiskExit, Terminal, Unknown, Working
from .paired_fills import Matched


def _number(x):
    return type(x) in (int, float) and isfinite(x)


def _id(*parts):
    return sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class ShadowConfig:
    version: str
    near: str
    far: str
    symbol: str
    tick: float
    multiplier: float
    mean_seconds: float = 180
    sd_seconds: float = 1800
    min_samples: int = 30
    min_span: float = 300
    entry_k: float = 0.3
    layer_step: float = 1
    max_layers: int = 3
    max_lots: int = 3
    layer_lots: int = 1
    width_ticks: float = 2
    max_jump_ticks: float = 20
    jump_confirm_count: int = 3
    min_depth: int = 1
    sync_seconds: float = 0.5
    stale_seconds: float = 2
    volume_ratio_max: float = 0
    roll_cutoff: int = 3
    min_exit_pnl_override: float = 0
    fee_buffer: float = 1
    roll_exit_days: int = 1  # 0 disables forced roll; separate from entry cutoff
    stop_loss: float = 10000  # CNY gross, whole-position budget divided by max_layers
    allow_stop_loss: bool = False
    max_hold_days: int = 10  # 0 disables time stop

    def __post_init__(self):
        if (not all(isinstance(v, str) and v.strip() for v in (self.version, self.near, self.far, self.symbol))
                or len({self.near, self.far, self.symbol}) != 3):
            raise ValueError('invalid version or spread definition')
        if type(self.allow_stop_loss) is not bool or any(type(x) is not int for x in (self.roll_exit_days, self.max_hold_days)):
            raise ValueError('invalid risk configuration')
        for key, val in asdict(self).items():
            if key not in ('version', 'near', 'far', 'symbol', 'allow_stop_loss') and (not _number(val) or val < 0):
                raise ValueError(f'invalid {key}')
        if (not all(type(x) is int and x > 0 for x in (self.max_layers, self.max_lots,
                    self.layer_lots, self.min_depth, self.min_samples, self.jump_confirm_count)) or self.min_samples < 2
                or min(self.tick, self.multiplier, self.mean_seconds, self.sd_seconds,
                       self.stale_seconds, self.width_ticks) <= 0 or self.fee_buffer < 1):
            raise ValueError('invalid shadow limits')


@dataclass(frozen=True)
class LegQuote:
    symbol: str
    event_id: str
    timestamp: float | None  # epoch seconds, same clock as the captured evaluation time
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    volume: float
    limit_down: float | None = None
    limit_up: float | None = None


@dataclass(frozen=True)
class EntryChecks:
    """External facts for this snapshot, not a send permit. Unknown fails closed."""
    in_session: bool
    can_open: bool
    listed: bool
    funds_ok: bool
    quota_ok: bool
    days_to_delivery: int | None  # minimum of both legs, from trading calendar
    trading_day_index: int | None = None  # stable ordinal from a single exchange calendar

    def __post_init__(self):
        if self.trading_day_index is not None and type(self.trading_day_index) is not int:
            raise ValueError('invalid trading day ordinal')


@dataclass(frozen=True)
class FeeEstimate:
    """Trusted, unbuffered CNY per lot for this layer/day (incl. close-today rate).

    Upstream uses attributed actual leg prices for ad-valorem opening fees; if
    unavailable, do not supply an estimate. Not the actual settlement charge.
    """
    tier_id: str
    open_day: str
    close_day: str
    open_near: float
    open_far: float
    close_near: float
    close_far: float
    source: str


@dataclass(frozen=True)
class Result:
    reason: str
    intent: OrderIntent | None = None
    cancels: tuple = ()  # LayerBook's local cancel requests, not confirmations


class ShadowStrategy:
    def __init__(self, journal: Journal, config: ShadowConfig, *, account: str, strategy: str):
        self.journal, self.config = journal, config
        self.account, self.strategy = account, strategy
        self.book = LayerBook(config.max_layers, config.max_lots)
        self.window = StatWindow(config.mean_seconds, config.sd_seconds)
        self.intents: dict[str, OrderIntent] = {}
        self.costs: dict[str, Decimal] = {}  # permanent tier -> actual cumulative spread VWAP
        self.open_days: dict[str, str] = {}
        self._day_indices = {}
        self._risk = None  # latched trigger evidence; never reuse the trigger quote as a new limit
        self._matched = {}
        self._last_mid = {}
        self._jumps = {}  # symbol -> (direction relative to clean baseline, count, leg event ID)
        self._last_time = None
        self._sample_day = None
        self.error = ''
        self._config_data = asdict(config)
        self._legacy_data = {k: v for k, v in self._config_data.items()
                             if k not in ('allow_stop_loss', 'stop_loss', 'roll_exit_days', 'max_hold_days')}
        self._fingerprint = _id(self._config_data)
        self._legacy_fingerprint = _id(self._legacy_data)
        self._legacy_inputs = set()
        self._legacy = False
        persisted = {}
        for row in journal.replay():
            if row.account != account or row.identity[0] != strategy:
                continue
            if row.kind not in ('shadow_input', 'order_intent'):
                continue
            if row.outcome == Outcome.CONFLICT:
                raise ValueError(f'shadow evidence conflict #{row.sequence}')
            if row.outcome == Outcome.NEW:
                if row.kind == 'shadow_input':
                    self._apply(row.trading_day, row.identity[1], row.payload)
                else:
                    persisted[row.identity[1]] = row.payload
        if any(key not in self.intents or asdict(self.intents[key]) != p for key, p in persisted.items()):
            raise ValueError('orphan or inconsistent shadow intent')
        for key, intent in self.intents.items():
            if key not in persisted:
                self._reserve(intent)

    def _reserve(self, intent):
        if intent.reserve(self.journal) == Outcome.CONFLICT:
            raise ValueError('shadow intent conflict')

    def _record(self, day, kind, key, data):
        if self.error:
            raise ValueError(self.error)
        if not isinstance(key, str) or not key.strip():
            raise ValueError('missing stable event id')
        decision = _id(self.account, self.strategy, day, kind, key)
        payload = dict(kind=kind, config=self._fingerprint, version=self.config.version, data=data)
        if decision in self._legacy_inputs:
            payload['config'] = self._legacy_fingerprint
            if kind == 'quote' and data['checks'].get('trading_day_index') is None:
                data['checks'].pop('trading_day_index', None)
        try:
            _, outcome = self.journal.append(self.account, day, 'shadow_input', (self.strategy, decision), payload)
            if outcome == Outcome.DUPLICATE:
                return Result('duplicate')
            if outcome == Outcome.CONFLICT:
                raise ValueError('conflicting shadow evidence')
            result = self._apply(day, decision, payload)
            if result.intent:
                self._reserve(result.intent)
            return result
        except Exception as exc:
            self.error = f'shadow halted: {exc}'
            raise

    def on_quote(self, day: str, near: LegQuote, far: LegQuote, *, now: float, checks: EntryChecks):
        """now/checks are captured once with the original event, not refreshed on replay."""
        if not all(isinstance(q.event_id, str) and q.event_id.strip() for q in (near, far)):
            raise ValueError('missing leg event id')
        return self._record(day, 'quote', _id(near.event_id, far.event_id), dict(
            near=asdict(near), far=asdict(far), now=now, checks=asdict(checks)))

    def protect(self, day: str, cycle_id: str, fees: FeeEstimate | None):
        return self._record(day, 'protect', cycle_id, {'fees': asdict(fees) if fees else None})

    def on_status(self, day: str, event_id: str, event: Working | Unknown | Terminal):
        if type(event) not in (Working, Unknown, Terminal):
            raise TypeError('confirmed working, unknown or terminal status required')
        return self._record(day, 'status', event_id, dict(type=type(event).__name__, **asdict(event)))

    def on_matched(self, day: str, event_id: str, order_id: str, actual: Matched):
        """Trusted adapter supplies cumulative matched_fills for this attributed order."""
        return self._record(day, 'matched', event_id, dict(order=order_id, lots=actual.lots,
            avg=None if actual.average_spread is None else str(actual.average_spread),
            near_avg=None if actual.near_average is None else str(actual.near_average),
            far_avg=None if actual.far_average is None else str(actual.far_average),
            residual_near=actual.residual_near, residual_far=actual.residual_far))

    def _apply(self, day, decision, p):
        # Old inputs must reconstruct their original intent bytes and must not
        # acquire a retroactive exit solely because the new engine is installed.
        self._legacy = p['config'] == self._legacy_fingerprint
        if self._legacy:
            self._legacy_inputs.add(decision)
        elif p['config'] != self._fingerprint:
            raise ValueError('shadow configuration differs from recorded inputs')
        d, kind = p['data'], p['kind']
        if kind == 'quote':
            return self._quote(day, decision, LegQuote(**d['near']), LegQuote(**d['far']), d['now'], EntryChecks(**d['checks']))
        if kind == 'protect':
            return self._protect(day, decision, d['fees'])
        if kind == 'status':
            cls = {'Working': Working, 'Unknown': Unknown, 'Terminal': Terminal}[d['type']]
            self.book.apply(cls(**{k: v for k, v in d.items() if k != 'type'}))
            return Result('status recorded')
        if kind != 'matched':
            raise ValueError('unsupported shadow input')
        intent = self.intents[d['order']]
        if day != intent.day:
            raise ValueError('fill trading day differs from order')
        lots, avg = d['lots'], None if d['avg'] is None else Decimal(d['avg'])
        if (not all(type(d[k]) is int and d[k] >= 0 for k in ('lots','residual_near','residual_far'))
                or lots + max(d['residual_near'], d['residual_far']) > intent.lots
                or ((avg is None) != (lots == 0)) or (avg is not None and not avg.is_finite())):
            raise ValueError('invalid paired fill')
        previous = self._matched.get(intent.decision_id)
        old_lots = previous['lots'] if previous else 0
        if previous:
            old_avg = None if previous['avg'] is None else Decimal(previous['avg'])
            if (lots < old_lots or (lots == old_lots and avg != old_avg)
                    or any(lots+d[k] < old_lots+previous[k] for k in ('residual_near','residual_far'))):
                raise ValueError('inconsistent cumulative paired fills')
        self._matched[intent.decision_id] = d
        cancels = ()
        if lots > old_lots:
            cancels = self.book.apply(Fill(intent.decision_id, decision, lots-old_lots))
            if intent.purpose in ('open', 'add'):
                self.costs[intent.tier_id] = avg
                self.open_days[intent.tier_id] = day
        return Result('paired facts recorded', cancels=cancels)

    def _unpaired(self):
        return any(v['residual_near'] or v['residual_far'] for v in self._matched.values())

    def _intent(self, day, event, price, side, reason):
        actions = self.book.apply(event)
        if not actions:
            return Result('layer book blocked')
        a, = actions
        intent = OrderIntent(self.account, day, self.strategy, a.order_id, a.tier_id,
            a.kind, self.config.symbol, side, a.lots, float(price), self.config.version,
            dict(shadow_only=True, **reason))
        self.intents[a.order_id] = intent
        return Result(a.kind, intent)

    def _quote(self, day, key, near, far, now, checks):
        c = self.config
        if checks.in_session is not True or (near.symbol, far.symbol) != (c.near, c.far):
            return Result('session or symbols')
        mids = {}
        for leg in (near, far):
            if (not all(_number(x) for x in (leg.timestamp, now, leg.bid, leg.ask, leg.bid_size,
                                             leg.ask_size, leg.volume)) or leg.bid <= 0 or leg.ask < leg.bid
                    or min(leg.bid_size, leg.ask_size, leg.volume) < 0
                    or not 0 <= now - leg.timestamp <= c.stale_seconds):
                return Result('invalid or stale leg')
            if ((leg.limit_down is not None and (not _number(leg.limit_down) or leg.bid < leg.limit_down))
                    or (leg.limit_up is not None and (not _number(leg.limit_up) or leg.ask > leg.limit_up))):
                return Result('price limits')
            mids[leg.symbol] = (leg.bid + leg.ask) / 2
        timestamp = max(near.timestamp, far.timestamp)
        if self._last_time is not None and timestamp < self._last_time:
            return Result('late quote')
        if abs(near.timestamp-far.timestamp) > c.sync_seconds:
            return Result('unsynchronized')
        if self._sample_day != day:
            self.window.clear()
            self._last_mid.clear()
            self._jumps.clear()
            self._sample_day = day
        if max(near.ask-near.bid, far.ask-far.bid) > (c.width_ticks + 0.5)*c.tick:
            return Result('width')
        jumping = False
        for leg in (near, far):
            symbol = leg.symbol
            delta = mids[symbol] - self._last_mid.get(symbol, mids[symbol])
            if c.max_jump_ticks and abs(delta) > c.max_jump_ticks*c.tick:
                direction = 1 if delta > 0 else -1
                old_direction, count, event = self._jumps.get(symbol, (0, 0, None))
                count = (count if old_direction == direction else 0) + int(event != leg.event_id)
                self._jumps[symbol] = direction, count, leg.event_id
                jumping |= count < c.jump_confirm_count
            else:
                self._jumps.pop(symbol, None)
        if jumping:
            return Result('unconfirmed leg jump')
        self._last_time, self._last_mid = timestamp, mids
        mid = mids[near.symbol] - mids[far.symbol]
        bid, ask = near.bid-far.ask, near.ask-far.bid
        self.window.push(timestamp, mid)
        if checks.trading_day_index is not None:
            if self._day_indices.setdefault(day, checks.trading_day_index) != checks.trading_day_index:
                raise ValueError('conflicting trading day ordinal')
        # Entry liquidity/funding gates and the statistical window are not risk gates.
        # Use decimal leg differences so an exact stop boundary is not lost to float subtraction.
        if not self._legacy:
            risk = self._risk_quote(day, key, checks, near, far,
                Decimal(str(near.bid))-Decimal(str(far.ask)), Decimal(str(near.ask))-Decimal(str(far.bid)))
            if risk is not None:
                return risk
        if min(near.ask_size, near.bid_size, far.ask_size, far.bid_size) < c.min_depth:
            return Result('depth')
        if c.volume_ratio_max and (min(near.volume, far.volume) <= 0 or
                max(near.volume,far.volume) > c.volume_ratio_max * min(near.volume,far.volume)):
            return Result('volume ratio')
        if (not all(v is True for v in (checks.can_open, checks.listed, checks.funds_ok, checks.quota_ok))
                or type(checks.days_to_delivery) is not int or checks.days_to_delivery <= c.roll_cutoff):
            return Result('entry checks')
        if self._unpaired():
            return Result('unpaired leg exposure')
        if not self.window.ready(min_samples=c.min_samples, min_seconds=c.min_span):
            return Result('warming up')
        mean, sd = self.window.mean(), self.window.sd()
        z = (mid-mean)/sd
        rank = len(self.book.layers)+1
        k = c.entry_k + (rank-1)*c.layer_step
        direction = 1 if z < -k else -1 if z > k else 0
        if not direction:
            return Result('below threshold')
        return self._intent(day, Enter(key, direction, c.layer_lots), ask if direction == 1 else bid,
            'buy' if direction == 1 else 'sell', dict(near_event=near.event_id, far_event=far.event_id,
            bid=bid, ask=ask, mid=mid, mean=mean, sd=sd, z=z, k=k, rank=rank, count=self.window.count,
            checks=({k: v for k, v in asdict(checks).items() if k != 'trading_day_index'}
                    if self._legacy else asdict(checks)), config=self._legacy_data if self._legacy else self._config_data))

    def _risk_quote(self, day, key, checks, near, far, bid, ask):
        layers, c = self.book.layers, self.config
        # A late paired add after a cancel belongs to the whole-position exit too.
        # Keep the latch until all relevant fill details AND terminal statuses arrive.
        if not layers and not self.book.unsettled:
            self._risk = None
        if (self._risk and self._risk['risk_rule'] == 'layer_stop' and
                self._risk['target_tier'] not in {l.tier_id for l in layers} and not self.book.unsettled):
            self._risk = None
        if type(checks.days_to_delivery) is int and c.roll_exit_days and checks.days_to_delivery <= c.roll_exit_days:
            if not (layers or self.book.unsettled):
                return Result('roll exit: flat')  # do not open, even if entry cutoff is disabled
            if not self._risk or self._risk['risk_rule'] != 'roll_exit':
                self._risk = dict(risk_rule='roll_exit', days_to_delivery=checks.days_to_delivery, trigger=key)
        if not self._risk and layers:
            exit_px = bid if layers[-1].direction == 1 else ask
            if c.allow_stop_loss and c.stop_loss > 0:
                budget = Decimal(str(c.stop_loss))
                for layer in reversed(layers):
                    cost = self.costs[layer.tier_id]
                    pnl = layer.direction * (exit_px-cost) * Decimal(str(c.multiplier)) * layer.remaining
                    if pnl*c.max_layers <= -budget:
                        self._risk = dict(risk_rule='layer_stop', target_tier=layer.tier_id, trigger=key,
                            gross_pnl=str(pnl), actual_open=str(cost), trigger_price=str(exit_px),
                            trigger_lots=layer.remaining, layer_budget=str(budget/c.max_layers))
                        break
            if not self._risk and c.max_hold_days:
                first = self.open_days[layers[0].tier_id]
                if first in self._day_indices and day in self._day_indices:
                    held = self._day_indices[day] - self._day_indices[first]
                    source = 'exchange_day_index'
                else:
                    held = trading_days_between(first, day)
                    source = 'estimated_calendar_days_times_5_over_7'
                if held is not None and held >= c.max_hold_days:
                    self._risk = dict(risk_rule='time_stop', trigger=key, first_open_day=first,
                                      held_days=held, day_count_source=source)
        if not self._risk:
            return None
        cancels = self.book.cancel_risk_conflicts()
        if self.book.unsettled:
            return Result('risk awaiting order settlement', cancels=cancels)
        if self._unpaired():
            return Result('unpaired leg exposure')
        if checks.listed is not True:
            return Result('risk exit awaits listed instruments')
        tier = self._risk.get('target_tier', layers[-1].tier_id)
        layer = next(l for l in layers if l.tier_id == tier)
        sell = layer.direction == 1
        if min((near.bid_size, far.ask_size) if sell else (near.ask_size, far.bid_size)) < 1:
            return Result('risk exit lacks executable depth')
        return self._intent(day, RiskExit(key, tier), bid if sell else ask, 'sell' if sell else 'buy',
            dict(**self._risk, near_event=near.event_id, far_event=far.event_id, bid=str(bid), ask=str(ask),
                 remaining=layer.remaining, config=asdict(c)))

    def _protect(self, day, key, fees):
        if self._risk:
            return Result('risk exit takes priority')
        if not self.book.layers or self._unpaired():
            return Result('no explainable top')
        top = self.book.layers[-1]
        if (not fees or fees['tier_id'] != top.tier_id or fees['open_day'] != self.open_days[top.tier_id]
                or fees['close_day'] != day or not isinstance(fees['source'], str) or not fees['source'].strip()):
            return Result('missing or mismatched layer fee estimate')
        edges = [fees[k] for k in ('open_near', 'open_far', 'close_near', 'close_far')]
        if not all(_number(x) and x >= 0 for x in edges):
            return Result('invalid fee estimate')
        edges = [Decimal(str(x)) for x in edges]
        c = self.config
        m, tick = Decimal(str(c.multiplier)), Decimal(str(c.tick))
        g = Decimal(str(c.min_exit_pnl_override)) or max(4*max(edges[:2]), m*tick)
        f = sum(edges)*top.remaining*Decimal(str(c.fee_buffer))
        e = self.costs[top.tier_id]
        raw = e + top.direction*(g+f)/(m*top.remaining)
        price = (raw/tick).to_integral_value(rounding=ROUND_CEILING if top.direction == 1 else ROUND_FLOOR)*tick
        return self._intent(day, Protect(key), price, 'sell' if top.direction == 1 else 'buy', dict(
            actual_open=str(e), remaining=top.remaining, raw_target=str(raw), fees=fees,
            estimated_fee=str(f), min_net=str(g), config=self._legacy_data if self._legacy else self._config_data))
