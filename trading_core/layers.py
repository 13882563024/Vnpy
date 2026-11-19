"""Offline projection for one strategy/account/spread, driven serially in journal order.

Only confirmed *paired combination* fills enter this book (never individual legs).
The caller supplies stable decision IDs and normalized, account/day/exchange-scoped
fill keys. Returned actions describe local reservations/withdrawal requests, not
broker orders. Protect and Enter are explicit decisions, not automatic retries.
Replay policy: cancel unfilled opening remainder on first partial fill; rank surviving
layers by creation order, independent of permanent IDs. No price or gate logic here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Enter:
    decision_id: str
    direction: int  # +1 long spread, -1 short spread
    lots: int = 1


@dataclass(frozen=True)
class Protect:
    decision_id: str


@dataclass(frozen=True)
class RiskExit:
    decision_id: str
    tier_id: str  # one surviving layer, never an inferred aggregate position


@dataclass(frozen=True)
class Working:
    order_id: str  # broker confirms active


@dataclass(frozen=True)
class Unknown:
    order_id: str  # e.g. send/cancel timed out; never releases reservation


@dataclass(frozen=True)
class Fill:
    order_id: str
    key: str
    lots: int  # incremental, never cumulative


@dataclass(frozen=True)
class Terminal:
    order_id: str
    filled: int  # authoritative final cumulative quantity, including late details


@dataclass(frozen=True)
class Action:
    kind: str  # open, add, take_profit, risk_exit, cancel
    order_id: str
    tier_id: str
    lots: int


@dataclass(frozen=True)
class _Order:
    action: Action
    direction: int
    filled: int = 0
    final: int | None = None
    working: bool = False
    cancelling: bool = False

    @property
    def pending(self):
        return self.final != self.filled


@dataclass(frozen=True)
class Layer:
    tier_id: str
    direction: int
    opened: int
    remaining: int


def _lots(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError('invalid combination lots')


class LayerBook:
    """Empty action tuple means no new intent. Invalid/contradictory facts raise
    ValueError: the caller must stop replay and isolate them, never infer fills.
    Both terminal status and matching details are required to retire an order.
    """

    def __init__(self, max_layers=3, max_lots=3):
        _lots(max_layers)
        _lots(max_lots)
        self.max_layers = max_layers
        self.max_lots = max_lots
        self._orders: dict[str, _Order] = {}
        self._layers: dict[str, Layer] = {}
        self._fills: dict[str, Fill] = {}
        self._decisions: dict[str, Enter | Protect | RiskExit] = {}
        self._next_tier = 1

    @property
    def layers(self) -> tuple[Layer, ...]:
        """Surviving layers, in current staircase order; last is the true top."""
        return tuple(layer for layer in self._layers.values() if layer.remaining)

    def _pending(self, opening):
        return next((o for o in self._orders.values() if o.pending and
                     (o.action.kind in ('open', 'add')) == opening), None)

    @property
    def unsettled(self):
        """Reservations awaiting terminal status or its matching fill details."""
        return tuple(o.action for o in self._orders.values() if o.pending)

    def cancel_risk_conflicts(self):
        """Cancel each old opening / TP once. A cancel never retires a reservation."""
        return tuple(a for order in tuple(self._orders.values()) if order.pending
                     and order.action.kind != 'risk_exit' for a in self._cancel(order))

    @property
    def worst_case_lots(self):
        opening = self._pending(True)
        reserved = opening.action.lots - opening.filled if opening else 0
        return sum(layer.remaining for layer in self.layers) + reserved

    @property
    def direction(self):
        if self.layers:
            return self.layers[-1].direction
        for order in self._orders.values():
            if order.pending:
                return order.direction
        return None

    def _decision(self, event):
        key = event.decision_id
        if not isinstance(key, str) or not key.strip():
            raise ValueError('missing decision id')
        if isinstance(event, Enter):
            _lots(event.lots)
            if type(event.direction) is not int or event.direction not in (-1, 1):
                raise ValueError('invalid direction')
        if key in self._decisions:
            if self._decisions[key] != event:
                raise ValueError('conflicting decision id')
            return ()
        self._decisions[key] = event  # even a blocked decision must not revive later
        opening, tp = self._pending(True), self._pending(False)
        layers = self.layers
        if opening:
            return ()
        if isinstance(event, RiskExit):
            layer = self._layers.get(event.tier_id)
            if tp or not layer or not layer.remaining:
                return ()
            action = Action('risk_exit', key, layer.tier_id, layer.remaining)
            direction = layer.direction
        elif isinstance(event, Protect):
            if not layers or tp:
                return ()
            top = layers[-1]
            action = Action('take_profit', key, top.tier_id, top.remaining)
            direction = top.direction
        else:
            if (self.direction not in (None, event.direction) or
                    len(layers) >= self.max_layers or
                    self.worst_case_lots + event.lots > self.max_lots):
                return ()
            # An add overlaps only a confirmed working TP on the current top.
            if layers or tp:
                if not (layers and tp and tp.action.kind == 'take_profit' and tp.action.tier_id == layers[-1].tier_id
                        and tp.working and not tp.cancelling and tp.final is None
                        and tp.action.lots > tp.filled):
                    return ()
            tier_id = f'L{self._next_tier}'
            self._next_tier += 1
            action = Action('add' if layers else 'open', key, tier_id, event.lots)
            direction = event.direction
        self._orders[key] = _Order(action, direction)
        return (action,)

    def _cancel(self, order):
        if order and order.final is None and not order.cancelling:
            remaining = order.action.lots - order.filled
            if remaining:
                self._orders[order.action.order_id] = replace(order, cancelling=True)
                return (replace(order.action, kind='cancel', lots=remaining),)
        return ()

    def apply(self, event: Enter | Protect | RiskExit | Working | Unknown | Fill | Terminal) -> tuple[Action, ...]:
        if isinstance(event, (Enter, Protect, RiskExit)):
            return self._decision(event)
        order = self._orders.get(event.order_id)
        if order is None:
            raise ValueError('unattributed order')
        if isinstance(event, (Working, Unknown)):
            if order.final is None:
                self._orders[event.order_id] = replace(order, working=isinstance(event, Working))
            return ()
        if isinstance(event, Terminal):
            _lots(event.filled, minimum=0)
            if (not order.filled <= event.filled <= order.action.lots or
                    order.final not in (None, event.filled)):
                raise ValueError('contradictory terminal cumulative quantity')
            self._orders[event.order_id] = replace(order, final=event.filled)
            return ()
        if not isinstance(event, Fill):
            raise TypeError('unsupported event')
        _lots(event.lots)
        if not isinstance(event.key, str) or not event.key.strip():
            raise ValueError('missing normalized fill key')
        if event.key in self._fills:
            if self._fills[event.key] != event:
                raise ValueError('conflicting fill key')
            return ()
        total = order.filled + event.lots
        limit = order.action.lots if order.final is None else order.final
        if total > limit:
            raise ValueError('fill exceeds confirmed order quantity')
        tier = order.action.tier_id
        opens = order.action.kind in ('open', 'add')
        layer = self._layers.get(tier, Layer(tier, order.direction, 0, 0))
        if opens:
            layer = replace(layer, opened=total, remaining=layer.remaining + event.lots)
        else:
            if event.lots > layer.remaining:
                raise ValueError('fill exceeds associated layer')
            layer = replace(layer, remaining=layer.remaining - event.lots)
        self._layers[tier] = layer
        self._fills[event.key] = event
        self._orders[event.order_id] = replace(order, filled=total)
        # Facts always win, even after cancel requested. Never cancel twice.
        if opens:
            return self._cancel(self._pending(False)) + self._cancel(self._pending(True))
        return self._cancel(self._pending(True))
