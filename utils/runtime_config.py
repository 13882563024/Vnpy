"""``config/runtime.json``：运行期开关（是否边跑边录 tick 等）。

与 ``config/spreads.json``（合约对）分开，因为这两份东西的维护节奏不同：
价差表随换月改，运行开关基本不动。

字段：

===================  ====================================================
``record_ticks``     边跑边落库 tick。``true`` = 策略运行时同步采集
``record_symbols``   只录这些 vt_symbol；**空列表 = 录所有收到的 tick**
``record_spreads``   合成价差也落库（symbol=价差名、exchange=LOCAL）；默认开
``record_snap_ms``   落库时间网格毫秒（默认 500：01.438→01.000、01.938→01.500）
``record_batch_size``  每批落库条数（默认 300）
``record_flush_interval`` 落库间隔秒（默认 5，GUI 进程里放宽以免频繁写盘）
``record_status_every``   日志里报告录制进度的间隔秒（默认 120）
===================  ====================================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME_PATH = PROJECT_ROOT / "config" / "runtime.json"


@dataclass
class RuntimeConfig:
    """运行期开关。"""

    record_ticks: bool = True
    record_symbols: list[str] = field(default_factory=list)
    record_spreads: bool = True
    record_snap_ms: int = 500
    record_batch_size: int = 300
    record_flush_interval: float = 5.0
    record_status_every: float = 120.0

    def describe(self) -> str:
        if not self.record_ticks:
            return "不落库"
        scope = "全部收到的 tick" if not self.record_symbols else f"{len(self.record_symbols)} 个指定合约"
        grid = f"时间网格 {self.record_snap_ms}ms" if self.record_snap_ms else "原始时间戳"
        spread = "含合成价差" if self.record_spreads else "只录腿"
        return (f"落库 {scope}（{grid}、{spread}；"
                f"每 {self.record_flush_interval:g}s 或 {self.record_batch_size} 条写一次）")


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def load_runtime_config(path: Path | str | None = None) -> RuntimeConfig:
    """读取运行开关；文件缺失或非法时返回默认值（默认开启落库）。"""
    config_path = Path(path) if path else DEFAULT_RUNTIME_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    symbols = raw.get("record_symbols") or []
    if isinstance(symbols, str):
        symbols = [item.strip() for item in symbols.split(",") if item.strip()]
    symbols = [str(item).strip() for item in symbols if str(item).strip()]

    return RuntimeConfig(
        record_ticks=_as_bool(raw.get("record_ticks"), True),
        record_symbols=symbols,
        record_spreads=_as_bool(raw.get("record_spreads"), True),
        record_snap_ms=int(raw.get("record_snap_ms", 500) or 0),
        record_batch_size=int(raw.get("record_batch_size", 300) or 300),
        record_flush_interval=float(raw.get("record_flush_interval", 5.0) or 5.0),
        record_status_every=float(raw.get("record_status_every", 120.0) or 120.0),
    )
