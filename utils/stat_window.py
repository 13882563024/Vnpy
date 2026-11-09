"""按 tick 时间戳滑动的统计窗口：最近 N 分钟均值 + 最近 M 分钟标准差。

刻意**不做 bar 聚合**（方案 2.2）：信号要保留 tick 级过滤条件，且均值/标准差
需要随实时价连续变化。窗口用增量维护 ``sum`` / ``sum_sq``，均摊 O(1)。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


class RollingWindow:
    """按秒滑动的时间窗口，支持均值与样本标准差。"""

    def __init__(self, seconds: float) -> None:
        self.seconds: float = float(seconds)
        self._items: deque[tuple[float, float]] = deque()
        self._sum: float = 0.0
        self._sum_sq: float = 0.0
        self.last_timestamp: float = 0.0

    def clear(self) -> None:
        self._items.clear()
        self._sum = 0.0
        self._sum_sq = 0.0
        self.last_timestamp = 0.0

    def push(self, timestamp: float, value: float) -> None:
        """追加一条（时间戳应单调不减）。"""
        self._items.append((timestamp, value))
        self._sum += value
        self._sum_sq += value * value
        self.last_timestamp = timestamp
        self._expire(timestamp)

    def _expire(self, now: float) -> None:
        cutoff = now - self.seconds
        while self._items and self._items[0][0] < cutoff:
            _, value = self._items.popleft()
            self._sum -= value
            self._sum_sq -= value * value

    def prune(self, now: float) -> None:
        """不新增数据、只按当前时间淘汰过期点（心跳/无行情时用）。"""
        self._expire(now)

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def first_timestamp(self) -> float:
        return self._items[0][0] if self._items else 0.0

    def mean(self) -> float | None:
        if not self._items:
            return None
        return self._sum / len(self._items)

    def std(self, ddof: int = 1) -> float | None:
        """样本标准差（``ddof=1``）。"""
        count = len(self._items)
        if count <= ddof:
            return None
        mean = self._sum / count
        variance = max(self._sum_sq / count - mean * mean, 0.0)
        if ddof:
            return (variance * count / (count - ddof)) ** 0.5
        return variance ** 0.5

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class StatWindow:
    """``mean`` 取最近 ``mean_seconds`` 的均值，``sd`` 取最近 ``sd_seconds`` 的标准差。"""

    mean_seconds: float = 180.0
    sd_seconds: float = 1800.0
    _mean_window: RollingWindow = field(init=False)
    _sd_window: RollingWindow = field(init=False)

    def __post_init__(self) -> None:
        self._mean_window = RollingWindow(self.mean_seconds)
        self._sd_window = RollingWindow(self.sd_seconds)

    def push(self, timestamp: float, spread: float) -> None:
        self._mean_window.push(timestamp, spread)
        self._sd_window.push(timestamp, spread)

    def mean(self) -> float | None:
        return self._mean_window.mean()

    def sd(self) -> float | None:
        return self._sd_window.std(ddof=1)

    def ready(self, *, min_samples: int = 0, min_seconds: float = 0.0) -> bool:
        """是否具备最小可用样本。

        ``min_samples`` 是最少 tick 数；``min_seconds`` 是样本实际覆盖的最短时长。
        只检查“窗口里有数”是不够的：刚开盘两三个 tick 也能算出 sd，但阈值没有意义。
        """
        mean, sd = self.mean(), self.sd()
        if mean is None or sd is None or sd <= 0:
            return False
        if min_samples and self.count < int(min_samples):
            return False
        if min_seconds and self.span() < float(min_seconds):
            return False
        return True

    def span(self) -> float:
        """标准差窗口内样本实际覆盖的秒数（最后一条 − 最早一条）。"""
        if not self._sd_window.count:
            return 0.0
        return max(self._sd_window.last_timestamp - self._sd_window.first_timestamp, 0.0)

    @property
    def count(self) -> int:
        """标准差窗口的样本数（预热进度的分母）。"""
        return self._sd_window.count

    def clear(self) -> None:
        self._mean_window.clear()
        self._sd_window.clear()
