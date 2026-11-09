"""全局资金占用上限：已用资金占比达到阈值后，不再允许开仓/加仓。

配置在 ``config/risk.json``，与具体价差无关：

    {"enabled": true, "max_margin_ratio": 0.70}

“已用资金”取 ``权益 - 可用资金``（CTP 的 Balance - Available，包含占用保证金和
冻结现金/手续费），因此比只看保证金更保守。达到阈值只拦截**开仓/加仓**，不拦截
平仓、止盈或止损。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RISK_PATH = PROJECT_ROOT / "config" / "risk.json"
DEFAULT_MAX_MARGIN_RATIO = 0.70


@dataclass(frozen=True)
class RiskLimits:
    enabled: bool = True
    max_margin_ratio: float = DEFAULT_MAX_MARGIN_RATIO


@dataclass(frozen=True)
class AccountUsage:
    balance: float
    available: float
    ratio: float


def load_risk_limits(path: Path | str | None = None) -> RiskLimits:
    config_path = Path(path) if path is not None else DEFAULT_RISK_PATH
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return RiskLimits()

    if not isinstance(raw, dict):
        return RiskLimits()
    enabled = bool(raw.get("enabled", True))
    try:
        ratio = float(raw.get("max_margin_ratio", DEFAULT_MAX_MARGIN_RATIO))
    except (TypeError, ValueError):
        ratio = DEFAULT_MAX_MARGIN_RATIO
    if not (0 < ratio <= 1):
        ratio = DEFAULT_MAX_MARGIN_RATIO
    return RiskLimits(enabled=enabled, max_margin_ratio=ratio)


def save_risk_limits(limits: RiskLimits, path: Path | str | None = None) -> None:
    """原子写入全局风控配置。"""
    ratio = float(limits.max_margin_ratio)
    if not (0 < ratio <= 1):
        raise ValueError("max_margin_ratio 必须在 (0, 1] 之间")
    config_path = Path(path) if path is not None else DEFAULT_RISK_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(limits.enabled),
        "max_margin_ratio": ratio,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(
        prefix=".risk-", suffix=".tmp", dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def account_usage(main_engine, gateway_name: str = "CTP") -> AccountUsage | None:
    """汇总该网关的权益/可用资金与已用比例；没有账户数据返回 None。"""
    getter = getattr(main_engine, "get_all_accounts", None)
    if not callable(getter):
        return None
    try:
        accounts = [
            item for item in getter()
            if getattr(item, "gateway_name", "") == gateway_name
        ]
    except Exception:
        return None
    if not accounts:
        return None

    balance = sum(float(getattr(item, "balance", 0) or 0) for item in accounts)
    available = sum(float(getattr(item, "available", 0) or 0) for item in accounts)
    if balance <= 0:
        return AccountUsage(balance=balance, available=available, ratio=1.0)
    used = max(0.0, balance - available)
    return AccountUsage(
        balance=balance,
        available=available,
        ratio=min(1.0, used / balance),
    )
