"""离线影子层账。持久化决策输入与归属明确的事实；重放只重建投影，不重发动作。"""
from dataclasses import asdict
from .journal import Journal, Outcome
from .layers import LayerBook, Enter, Protect, RiskExit, Working, Unknown, Fill, Terminal


_TYPES = {c.__name__: c for c in (Enter, Protect, RiskExit, Working, Unknown, Fill, Terminal)}


class ShadowLayers:
    def __init__(self, journal: Journal, *, account: str, strategy: str,
                 max_layers: int, max_lots: int):
        self.book = LayerBook(max_layers=max_layers, max_lots=max_lots)
        self.journal = journal
        self.account = account
        self.strategy = strategy
        self.error = ''
        # 不能按当日过滤：上个交易日开立的层仍属该策略。
        for row in journal.replay():
            if (row.kind != 'layer_event' or row.account != account or row.identity[0] != strategy):
                continue
            if row.outcome == Outcome.CONFLICT:
                self.error = f'层账证据冲突 #{row.sequence}'
                break
            if row.outcome == Outcome.NEW:
                try:
                    payload = row.payload
                    if (max_layers, max_lots) != (payload['max_layers'], payload['max_lots']):
                        raise ValueError('回放容量配置与原事件不符')
                    self.book.apply(_TYPES[payload['type']](**payload['data']))
                except (KeyError, TypeError, ValueError) as exc:
                    self.error = f'层账事件 #{row.sequence} 无法回放：{exc}'
                    break

    def observe(self, day, event_id, event):
        if self.error:
            raise ValueError(self.error)
        if type(event) not in _TYPES.values():
            raise TypeError('未知层账事件')
        payload = {'type': type(event).__name__, 'data': asdict(event),
                   'max_layers':self.book.max_layers, 'max_lots':self.book.max_lots}
        seq, outcome = self.journal.append(self.account, day, 'layer_event',
                                            (self.strategy, event_id), payload)
        if outcome == Outcome.DUPLICATE:
            return ()
        if outcome == Outcome.CONFLICT:
            self.error = f'层账证据冲突 #{seq}'
            raise ValueError(self.error)
        try:
            return self.book.apply(event)
        except (TypeError, ValueError) as exc:
            self.error = f'层账事件 #{seq} 无法入账：{exc}'
            raise ValueError(self.error) from exc
