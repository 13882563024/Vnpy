"""核对/委托状态的本地持久化。

进程重启后 vn.py 不保留任何核对确认、暂停期间委托；本模块把这些“人工已知事实”
落到 ``~/.vntrader/spreadconsole_state.json``，让重启后仍有上下文可依据。
只保存本项目自己的记录，不代替柜台查询，也不改写 vn.py 的订单/持仓缓存。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATE_FILENAME = "spreadconsole_state.json"
VERSION = 1


def default_state_path() -> Path:
    return Path.home() / ".vntrader" / STATE_FILENAME


def load_state(path: Path | str | None = None) -> dict[str, Any]:
    """读回上次状态；文件缺失/损坏时返回空结构，绝不阻止程序启动。"""
    target = Path(path) if path else default_state_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    return data


def save_state(state: dict[str, Any], path: Path | str | None = None) -> bool:
    """原子写入；失败只返回 False，由调用方决定是否记日志。"""
    target = Path(path) if path else default_state_path()
    payload = dict(state)
    payload["version"] = VERSION
    temp_name = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        return True
    except (OSError, TypeError, ValueError):
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        return False
