"""层账落盘：重启后按原样恢复，不再用柜台均价重建成本。

之前层只存在内存里，进程一退就没了；重启/核对时只能按柜台两腿**均价**接管，
于是真实的开仓价、双腿成交价、层号全部丢失，同一层每次重启都会拿到新的
tier 和新的成本。这个模块只做最小的持久化：按价差存一份层数组。

任何读写失败都不抛给调用方——层账坏了不能让交易停摆；策略侧读不到就按原来的
"从柜台接管"流程处理。
"""
from __future__ import annotations

import json
from pathlib import Path


def _path() -> Path:
    from vnpy.trader.utility import get_file_path

    return Path(get_file_path("spread_layers.json"))


def read_all() -> dict:
    """读取完整本地文件，供只读界面一次构建所有行。"""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load(name: str) -> list[dict]:
    rows = read_all().get(name)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def save(name: str, layers: list[dict]) -> None:
    data = read_all()
    data[name] = list(layers)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def clear(name: str) -> None:
    data = read_all()
    if data.pop(name, None) is not None:
        path = _path()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
