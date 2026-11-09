"""GUI 单字段组合合约服务：解析、原子保存及可选动态接线。

只支持明确验证过的 DCE ``SP <品种><YYMM>&<品种><YYMM>`` 语法。
配置中的 ``exchange_spread`` 保留用户所填组合代码；新增项默认不允许实盘，
柜台合约未验证时 ``size=0``，因此不会误通过 SpreadConfig.validate()。
本模块不更改 vn.py 安装包、不启动策略、不发送委托。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from utils.spread_config import DEFAULT_CONFIG_PATH, SpreadConfig, parse_spread_config

_MAX_LENGTH = 96
_DCE = re.compile(r"DCE\.(SP ([a-z]+)(\d{2})(\d{2})&([a-z]+)(\d{2})(\d{2}))\Z")


class ManagedSpreadError(ValueError):
    """输入、配置冲突或动态接线错误；供 GUI 展示错误原因。"""


@dataclass(frozen=True)
class ParsedSpread:
    id: str
    display_name: str
    exchange_spread: str
    exchange: str
    near_symbol: str
    far_symbol: str
    product: str

    @property
    def exchange_spread_vt_symbol(self) -> str:
        return f"{self.exchange_spread}.{self.exchange}"

    @property
    def legs(self) -> tuple[str, str]:
        return self.near_symbol, self.far_symbol


def parse_combination(text: str, *, today: date | None = None) -> ParsedSpread:
    """严格解析用户原样组合代码；仅剔除输入两端空白，不改写内部字符。"""
    if not isinstance(text, str) or not text.strip():
        raise ManagedSpreadError("请输入组合合约，如 DCE.SP a2701&a2703")
    display = text.strip()
    if len(display) > _MAX_LENGTH:
        raise ManagedSpreadError("组合合约输入过长")
    if not display.startswith("DCE."):
        raise ManagedSpreadError("目前只支持 DCE.SP <近月>&<远月>，不猜测其他交易所规则")
    match = _DCE.fullmatch(display)
    if not match:
        raise ManagedSpreadError("DCE 组合格式应为 DCE.SP a2701&a2703（单个 &、同品种和四位年月）")
    raw, near_product, ny, nm, far_product, fy, fm = match.groups()
    if near_product != far_product:
        raise ManagedSpreadError("组合近远腿必须属于同一品种")
    near_month, far_month = int(nm), int(fm)
    if not (1 <= near_month <= 12 and 1 <= far_month <= 12):
        raise ManagedSpreadError("合约月份必须在 01 至 12 之间")
    near = (2000 + int(ny), near_month)
    far = (2000 + int(fy), far_month)
    if near >= far:
        raise ManagedSpreadError("近月必须早于远月，且两腿不能相同")
    current = today or date.today()
    if near < (current.year, current.month):
        raise ManagedSpreadError("近月合约月份已过去；实际到期日仍须由柜台确认")
    near_code = f"{near_product}{ny}{nm}"
    far_code = f"{far_product}{fy}{fm}"
    return ParsedSpread(
        id=f"{near_product.upper()}_{ny}{nm}_{fy}{fm}",
        display_name=display,
        exchange_spread=raw,
        exchange="DCE",
        near_symbol=f"{near_code}.DCE",
        far_symbol=f"{far_code}.DCE",
        product=near_product.upper(),
    )


def _read_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedSpreadError(f"无法读取配置，拒绝覆盖旧数据：{exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in data.items()):
        raise ManagedSpreadError("配置根对象或条目格式错误，拒绝覆盖旧数据")
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """同目录临时文件 + fsync + os.replace；失败不破坏原文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as target:
            temp_name = target.name
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        with open(temp_name, "r", encoding="utf-8") as check:
            if json.load(check) != data:
                raise ManagedSpreadError("写入后的临时配置校验失败")
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _matches_existing(raw: dict[str, Any], parsed: ParsedSpread) -> bool:
    # 兼容旧注册表无 display_name / id 的条目；按真实组合代码和腿识别。
    exchange_spread = str(raw.get("exchange_spread") or "")
    if exchange_spread.endswith(".DCE"):
        exchange_spread = exchange_spread[:-4]
    return (exchange_spread == parsed.exchange_spread
            and str(raw.get("exchange") or "").upper() == parsed.exchange
            and list(raw.get("legs") or []) == list(parsed.legs))


def add_managed_spread(text: str, *, path: Path | str | None = None,
                       today: date | None = None) -> tuple[SpreadConfig, bool]:
    """保存用户组合并返回 ``(配置, 是否新建)``；重复添加返回原条目，不覆盖任何旧字段。

    独立进程同时写入需额外跨进程锁；本 API 面向单 GUI 进程串行调用。
    """
    parsed = parse_combination(text, today=today)
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    table = _read_registry(target)
    duplicates: list[tuple[str, dict[str, Any]]] = []
    for name, raw in table.items():
        same_code = (str(raw.get("exchange_spread") or "").removesuffix(".DCE")
                     == parsed.exchange_spread
                     and str(raw.get("exchange") or "").upper() == parsed.exchange)
        same_legs = list(raw.get("legs") or []) == list(parsed.legs)
        if name == parsed.id or same_code or same_legs:
            if not _matches_existing(raw, parsed):
                raise ManagedSpreadError(f"合约与已有条目 {name} 冲突，请核对组合代码及两腿")
            duplicates.append((name, raw))
    if len(duplicates) > 1:
        raise ManagedSpreadError("注册表已有多个条目指向同一组合，请人工清理重复项")
    if duplicates:
        name, raw = duplicates[0]
        return parse_spread_config(name, raw), False
    entry = {
        "display_name": parsed.display_name,
        "remark": "",
        "margin_per_lot": 0.0,
        "fee": {},
        "legs": list(parsed.legs),
        "active_symbol": parsed.near_symbol,
        "exchange_spread": parsed.exchange_spread,
        "exchange": parsed.exchange,
        "product": parsed.product,
        "size": 0,
        "pricetick": 0,
        "executor": "exchange_spread",
        "price_formula": "near - far",
        "strategy": {"live_trading": False},
        "contract_status": "pending_verification",
        "trading_enabled": False,
    }
    table[parsed.id] = entry
    _atomic_write(target, table)
    return parse_spread_config(parsed.id, entry), True


def verify_contracts(config: SpreadConfig, get_contract: Callable[[str], Any]) -> tuple[float, float, bool]:
    """检查两腿柜台元数据，返回 ``(乘数, tick, 组合合约是否已挂牌)``。

    对组合未挂牌只报告 False，调用者必须阻断组合单实盘，不自动改为两腿模式。
    """
    if len(config.legs) != 2 or not config.exchange_spread_vt_symbol:
        raise ManagedSpreadError("缺失两腿或组合代码，无法柜台验证")
    legs = [get_contract(symbol) for symbol in config.legs]
    if any(leg is None for leg in legs):
        raise ManagedSpreadError("柜台尚未返回两腿合约，请连接并等待合约查询完成")
    for symbol, contract in zip(config.legs, legs):
        if getattr(getattr(contract, "exchange", None), "value", getattr(contract, "exchange", None)) != config.exchange:
            raise ManagedSpreadError(f"柜台合约 {symbol} 的交易所不符")
    sizes = [getattr(leg, "size", None) for leg in legs]
    ticks = [getattr(leg, "pricetick", None) for leg in legs]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in sizes + ticks):
        raise ManagedSpreadError("两腿乘数或最小变动价位未知，禁止验证通过")
    if sizes[0] != sizes[1] or ticks[0] != ticks[1]:
        raise ManagedSpreadError("两腿乘数或最小变动价位不一致，禁止自动推断")
    combination = get_contract(config.exchange_spread_vt_symbol)
    if combination is not None:
        exchange = getattr(getattr(combination, "exchange", None), "value",
                           getattr(combination, "exchange", None))
        if exchange != config.exchange:
            raise ManagedSpreadError("柜台组合合约的交易所不符")
    return float(sizes[0]), float(ticks[0]), combination is not None


class ManagedSpreadService:
    """GUI 可持有的服务实例；订阅状态仅在当前实例内去重。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        self._subscriptions: set[tuple[str, str]] = set()

    def add(self, text: str, *, today: date | None = None) -> tuple[SpreadConfig, bool]:
        return add_managed_spread(text, path=self.path, today=today)

    def get(self, name: str) -> SpreadConfig | None:
        """读取指定条目；未找到返回 None，损坏的注册表直接报错。"""
        raw = _read_registry(self.path).get(name)
        return parse_spread_config(name, raw) if raw is not None else None

    def list(self) -> dict[str, SpreadConfig]:
        """列出既有和新增条目；旧条目的原始配置无需迁移即可读取。"""
        return {name: parse_spread_config(name, raw)
                for name, raw in _read_registry(self.path).items()}

    def list_raw(self) -> dict[str, dict[str, Any]]:
        """GUI 读取 display_name、contract_status 等附加字段，不丢失旧配置。"""
        return _read_registry(self.path)

    def edit_settings(self, name: str, *, strategy: dict[str, Any] | None = None,
                      stop_loss: float | None = None, vr_max: float | None = None,
                      trading_sessions: str | None = None, remark: str | None = None,
                      margin_per_lot: float | None = None,
                      fee: dict[str, Any] | None = None) -> SpreadConfig:
        """原子保存可编辑参数，保留旧条目的所有未知字段；不接收实盘解锁开关。

        不隐式修改运行中策略：调用者需先确认策略处于暂停状态，再按需调用
        ``strategy_engine.edit_strategy``；实盘开启必须经过独立门禁与人工确认。
        """
        table = _read_registry(self.path)
        if name not in table:
            raise ManagedSpreadError(f"价差 {name} 不在注册表中")
        raw = dict(table[name])
        if strategy is not None:
            if not isinstance(strategy, dict) or strategy.get("live_trading") is True:
                raise ManagedSpreadError("参数必须是对象；不能通过编辑参数启用实盘")
            from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy
            unknown = set(strategy).difference(CrossPeriodMRStrategy.parameters)
            if unknown:
                raise ManagedSpreadError(f"未知策略参数：{', '.join(sorted(unknown))}")
            current = raw.get("strategy") or {}
            if not isinstance(current, dict):
                raise ManagedSpreadError("原策略参数不是对象，拒绝覆盖")
            raw["strategy"] = {**current, **strategy, "live_trading": False}
        for key, value in (("stop_loss", stop_loss), ("vr_max", vr_max)):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 <= value < float("inf")):
                    raise ManagedSpreadError(f"{key} 必须是非负有限数值")
                raw[key] = value
        if trading_sessions is not None:
            if not isinstance(trading_sessions, str) or not trading_sessions.strip():
                raise ManagedSpreadError("trading_sessions 不能为空")
            raw["trading_sessions"] = trading_sessions.strip()
        if remark is not None:
            if not isinstance(remark, str) or len(remark) > 120:
                raise ManagedSpreadError("备注必须是不超过 120 字的文本")
            raw["remark"] = remark.strip()
        if margin_per_lot is not None:
            if (isinstance(margin_per_lot, bool)
                    or not isinstance(margin_per_lot, (int, float))
                    or not (0 <= margin_per_lot < float("inf"))):
                raise ManagedSpreadError("每手保证金必须是非负有限数值")
            raw["margin_per_lot"] = float(margin_per_lot)
        if fee is not None:
            if not isinstance(fee, dict):
                raise ManagedSpreadError("手续费必须是对象")
            if fee:
                allowed = {"unit", "exchange", "open", "close_yesterday", "close_today"}
                unknown = set(fee).difference(allowed)
                if unknown:
                    raise ManagedSpreadError(f"未知手续费字段：{', '.join(sorted(unknown))}")
                unit = str(fee.get("unit") or "元").strip()
                if unit not in ("元", "万分之"):
                    raise ManagedSpreadError("fee.unit 只能是 元 或 万分之")
                cleaned: dict[str, Any] = {"unit": unit}
                for key in ("open", "close_yesterday", "close_today"):
                    value = fee.get(key, 0)
                    if (isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not (0 <= value < float("inf"))):
                        raise ManagedSpreadError(f"fee.{key} 必须是非负有限数值")
                    cleaned[key] = float(value)
                if fee.get("exchange"):
                    cleaned["exchange"] = str(fee["exchange"]).strip().upper()
                raw["fee"] = cleaned
            else:
                raw.pop("fee", None)
        if raw == table[name]:
            return parse_spread_config(name, raw)
        table[name] = raw
        _atomic_write(self.path, table)
        return parse_spread_config(name, raw)

    def set_trading_enabled(self, name: str, enabled: bool) -> SpreadConfig:
        """持久化“允许此合约交易”，重启后仍保留；不改变 live_trading。"""
        table = _read_registry(self.path)
        if name not in table:
            raise ManagedSpreadError(f"价差 {name} 不在注册表中")
        raw = dict(table[name])
        raw["trading_enabled"] = bool(enabled)
        if raw != table[name]:
            table[name] = raw
            _atomic_write(self.path, table)
        return parse_spread_config(name, raw)

    def remove(self, name: str) -> SpreadConfig:
        """原子删除注册表条目；返回被删配置。

        只负责 ``config/spreads.json``，不碰引擎实例：删除前必须先由控制层确认
        没有在途委托、持仓和策略层账，否则会丢失对真实敞口的跟踪。
        """
        table = _read_registry(self.path)
        if name not in table:
            raise ManagedSpreadError(f"价差 {name} 不在注册表中")
        config = parse_spread_config(name, table[name])
        del table[name]
        _atomic_write(self.path, table)
        return config

    def verify_and_save(self, name: str, get_contract: Callable[[str], Any]) -> tuple[SpreadConfig, bool]:
        """验证柜台元数据并写回；组合不可用则保留交易禁用，不降级两腿。"""
        table = _read_registry(self.path)
        if name not in table:
            raise ManagedSpreadError(f"价差 {name} 不在注册表中")
        config = parse_spread_config(name, table[name])
        size, tick, listed = verify_contracts(config, get_contract)
        raw = dict(table[name])
        # 保留用户已允许交易的状态；只有组合不可用/验证失败时才取消。
        raw.update(size=size, pricetick=tick,
                   contract_status="verified" if listed else "combination_unavailable")
        if not listed:
            raw["trading_enabled"] = False
        if raw != table[name]:
            table[name] = raw
            _atomic_write(self.path, table)
        return parse_spread_config(name, raw), listed

    def register(self, spread_engine: Any, config: SpreadConfig) -> bool:
        """动态注册；已注册且腿相同则复用。未验证 size=0 仅限采集注册，不开放交易。"""
        current = spread_engine.get_spread(config.name)
        if current is not None:
            if list(current.variable_symbols.values()) != config.legs:
                raise ManagedSpreadError(f"引擎价差 {config.name} 的两腿与配置冲突")
            return False
        if len(config.legs) != 2 or config.active_symbol != config.legs[0] or config.price_formula != "near - far":
            raise ManagedSpreadError("组合腿或内部价差公式非法，拒绝注册")
        spread_engine.add_spread(name=config.name, leg_settings=config.leg_settings(),
                                 price_formula=config.price_formula,
                                 active_symbol=config.active_symbol,
                                 min_volume=config.min_volume)
        return True

    def ensure_strategy(self, spread_engine: Any, config: SpreadConfig,
                        *, class_name: str = "CrossPeriodMRStrategy") -> str:
        """仅准备/复用未启动策略实例；已有实例不编辑，以免改动运行中的策略。"""
        if spread_engine.get_spread(config.name) is None:
            raise ManagedSpreadError("价差尚未注册，拒绝建立策略实例")
        strategy_engine = spread_engine.strategy_engine
        name = f"{class_name}_{config.name}"
        if name not in strategy_engine.strategies:
            from utils.engine_setup import strategy_setting_for
            setting = strategy_setting_for(config)
            setting["live_trading"] = False
            strategy_engine.add_strategy(class_name, name, config.name, setting)
        return name

    def subscribe_legs(self, config: SpreadConfig, subscribe: Callable[[str], Any],
                       *, already_subscribed: tuple[str, ...] | list[str] = ()) -> list[str]:
        """按腿去重；将 SpreadEngine 已订阅的腿传给 already_subscribed 避免重复。

        ``subscribe`` 回调由 GUI 包装成 SubscribeRequest + gateway_name；只在回调
        成功后才记录，以便失败后重试。重连时调用 ``reset_subscriptions``。
        录制仍使用上层原 TickRecorder，不创建第二个 recorder。
        """
        known = set(already_subscribed)
        new: list[str] = []
        for symbol in config.legs:
            key = (symbol, config.exchange)
            if key not in self._subscriptions:
                if symbol not in known:
                    subscribe(symbol)
                    new.append(symbol)
                self._subscriptions.add(key)
        return new

    def reset_subscriptions(self) -> None:
        """网关断线后清理内存订阅状态，供重连时重新发起。"""
        self._subscriptions.clear()
