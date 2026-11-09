"""跨期价差行情采集入口：把两腿实时 tick 落库，供 `run_backtest.py` 使用。

**只连接行情前置，进程内不创建任何交易 API**（`gateway/ctp_md_gateway.py`）：
不认证、不登录交易、不查资金/持仓、不报单 —— 用真实带钱账号也只会产生行情流量。

为什么需要它：`vnpy` 收到 tick 只放进内存和事件引擎，**不会自动入库**；而价差回测的
硬前置是两腿 tick 已在 `~/.vntrader/database.db`（方案 8.1）。

前置条件（缺一不可）：

1. 可用的 CTP **行情**前置地址（`~/.vntrader/ctp_connect.json`，或 `--setting 行情服务器=...`）；
2. 行情账号（`用户名` / `密码` / `经纪商代码`）；
3. 处于交易时段（收盘后订阅成功也不会来 tick）。

示例::

    # 1) 只看要订阅哪些合约，不连柜台
    .venv\\Scripts\\python.exe run_recorder.py --list

    # 2) 采集 config/spreads.json 里全部价差的两腿 tick，直到 Ctrl+C
    .venv\\Scripts\\python.exe run_recorder.py

    # 3) 只采一个价差，采 30 分钟后自动退出
    .venv\\Scripts\\python.exe run_recorder.py --spread A_2611_2701 --minutes 30

    # 4) 行情前置临时覆盖（不写入配置文件）
    .venv\\Scripts\\python.exe run_recorder.py --setting 行情服务器=tcp://1.2.3.4:41214

    # 5) 录额外合约（不限于 spreads.json 里的价差）
    .venv\\Scripts\\python.exe run_recorder.py --symbols OI701.CZCE,OI703.CZCE

退出时（Ctrl+C / `--minutes` 到期）会 flush 剩余缓冲并打印逐合约条数汇总。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.event import Event, EventEngine                              # noqa: E402
from vnpy.trader.constant import Exchange                              # noqa: E402
from vnpy.trader.engine import MainEngine                              # noqa: E402
from vnpy.trader.event import EVENT_TICK                               # noqa: E402
from vnpy.trader.object import SubscribeRequest, TickData              # noqa: E402
from vnpy.trader.utility import extract_vt_symbol, load_json            # noqa: E402

from gateway.ctp_md_gateway import CtpMdOnlyGateway                    # noqa: E402
from recorder.tick_recorder import TickRecorder                        # noqa: E402
from utils.spread_config import load_spread_configs                    # noqa: E402
from utils.spread_synth import build_spreads                           # noqa: E402

CONNECT_FILENAME = "ctp_connect.json"
MD_LOGIN_TIMEOUT = 30.0
ADDRESS_KEY = "行情服务器"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跨期价差 tick 采集（仅行情前置）")
    parser.add_argument("--spread", action="append", default=[],
                        help="只采这些价差的腿合约（可多次指定）；默认采 spreads.json 全部")
    parser.add_argument("--symbols", default="",
                        help="额外合约，逗号分隔的 vt_symbol（如 OI701.CZCE,OI703.CZCE）")
    parser.add_argument("--minutes", type=float, default=0.0,
                        help="采集时长（分钟）；0 = 一直采到 Ctrl+C")
    parser.add_argument("--batch", type=int, default=300, help="每批落库条数")
    parser.add_argument("--flush-interval", type=float, default=2.0, help="落库间隔（秒）")
    parser.add_argument("--setting", action="append", default=[],
                        help="临时覆盖连接参数 key=value（可多次，不写回配置文件）")
    parser.add_argument("--status-every", type=float, default=30.0, help="进度打印间隔（秒）")
    parser.add_argument("--no-probe", action="store_true", help="跳过行情前置 TCP 连通性预检")
    parser.add_argument("--list", action="store_true", help="只打印将要订阅的合约，然后退出")
    return parser.parse_args(argv)


def parse_overrides(items: list[str]) -> dict:
    overrides: dict = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"参数覆盖格式应为 key=value：{item}")
        key, value = item.split("=", 1)
        overrides[key.strip()] = value.strip()
    return overrides


def collect_symbols(spread_names: list[str], extra: str) -> list[str]:
    """把 spreads.json 的腿合约与 --symbols 合并去重。"""
    configs = load_spread_configs()
    symbols: list[str] = []

    for name, config in configs.items():
        if spread_names and name not in spread_names:
            continue
        symbols.extend(config.legs)

    if spread_names:
        missing = [name for name in spread_names if name not in configs]
        if missing:
            print(f"[警告] spreads.json 中没有这些价差：{missing}")

    for item in extra.split(","):
        text = item.strip()
        if text:
            symbols.append(text)

    return list(dict.fromkeys(symbols))


def parse_host_port(address: str) -> tuple[str, int] | None:
    """``tcp://1.2.3.4:41213`` → ``("1.2.3.4", 41213)``；无法解析返回 None。"""
    text = str(address or "").strip()
    if not text or text.startswith("socks"):
        return None        # socks 走代理，无法直接探测
    for prefix in ("tcp://", "ssl://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    host, _, port_text = text.rpartition(":")
    if not host:
        return None
    try:
        return host, int(port_text)
    except ValueError:
        return None


def probe_address(address: str, timeout: float = 2.5) -> bool | None:
    """TCP 连通性预检：True/False；无法解析（socks 等）返回 None。"""
    target = parse_host_port(address)
    if target is None:
        return None
    host, port = target
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def preflight(setting: dict, skip: bool) -> bool:
    """连接前先探一次 TCP，避免把「前置不通」误判成「账号错误」。"""
    if skip:
        print("已跳过行情前置连通性预检")
        return True

    address = setting.get(ADDRESS_KEY, "")
    if not address:
        print(f"[错误] 连接配置缺少「{ADDRESS_KEY}」")
        return False

    result = probe_address(address)
    if result is True:
        print(f"行情前置连通：{address}")
        return True
    if result is False:
        print(f"[错误] 行情前置不通：{address}（TCP 连接超时/被拒）")
        print(
            "\n请向期货公司索取最新行情前置地址，"
            "或用 --setting 行情服务器=tcp://x.x.x.x:port 临时覆盖。"
        )
        return False

    print(f"[提示] 无法预检 {address}（socks 代理或格式特殊），直接尝试连接")
    return True


def wait_md_login(gateway, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(gateway.md_api, "login_status", False):
            return True
        time.sleep(0.3)
    return False


def register_contracts(gateway, symbols: list[str], configs: dict) -> list[str]:
    """给每个待订阅合约注册占位合约（否则 ``vnpy_ctp`` 会丢弃行情推送）。"""
    # 先建 vt_symbol → SpreadConfig 索引，用真实乘数/最小变动填合约
    lookup: dict[str, object] = {}
    for config in configs.values():
        for vt_symbol in config.legs:
            lookup.setdefault(vt_symbol, config)

    registered: list[str] = []
    for vt_symbol in symbols:
        try:
            symbol, exchange = extract_vt_symbol(vt_symbol)
        except ValueError:
            print(f"[警告] 合约格式应为 SYMBOL.EXCHANGE，跳过：{vt_symbol}")
            continue
        if not isinstance(exchange, Exchange):
            print(f"[警告] 无法识别的交易所，跳过：{vt_symbol}")
            continue

        config = lookup.get(vt_symbol)
        gateway.register_contract(
            symbol=symbol,
            exchange=exchange,
            size=getattr(config, "size", 0) or 1.0,
            pricetick=getattr(config, "pricetick", 0) or 0.0,
        )
        registered.append(vt_symbol)
    return registered


def subscribe_all(main_engine: MainEngine, gateway_name: str, symbols: list[str]) -> list[str]:
    subscribed: list[str] = []
    for vt_symbol in symbols:
        try:
            symbol, exchange = extract_vt_symbol(vt_symbol)
        except ValueError:
            continue
        if not isinstance(exchange, Exchange):
            continue
        main_engine.subscribe(SubscribeRequest(symbol=symbol, exchange=exchange), gateway_name)
        subscribed.append(vt_symbol)
    return subscribed


def warn_off_session(selected: list[str]) -> bool:
    """非交易时段提前提示（否则只会看到"行情登录超时"，容易误判成账号问题）。"""
    from datetime import datetime as _dt

    from utils.calendar import TradingSessions
    from utils.spread_config import load_spread_configs

    now = _dt.now()
    configs = load_spread_configs()
    names = selected or list(configs.keys())
    for name in names:
        config = configs.get(name)
        if config is None:
            continue
        if TradingSessions(config.trading_sessions).is_open(now):
            return True

    print(
        f"[提示] 当前 {now:%Y-%m-%d %H:%M} 不在任何价差的交易时段内（{', '.join(names)}）。\n"
        f"       非交易时段通常订阅成功也没有 tick；有些柜台在结算/非交易时段还会拒绝登录，\n"
        f"       表现为「行情登录超时」——那不是账号或前置地址错了。"
    )
    return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    symbols = collect_symbols(args.spread, args.symbols)
    if not symbols:
        print("[错误] 没有可订阅的合约：请在 config/spreads.json 配置价差，或用 --symbols 指定")
        return 1

    if not args.list:
        warn_off_session(args.spread)

    print(f"待订阅合约（{len(symbols)} 个）：")
    for vt_symbol in symbols:
        print(f"  {vt_symbol}")
    if args.list:
        return 0

    connect_setting = load_json(CONNECT_FILENAME)
    if not connect_setting:
        print(
            f"\n[错误] 未找到 {CONNECT_FILENAME}（在 ~/.vntrader/ 下，"
            f"界面「交易 → 连接 CTP」保存后生成）"
        )
        return 1
    connect_setting.update(parse_overrides(args.setting))

    if not preflight(connect_setting, args.no_probe):
        return 1

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    gateway = main_engine.add_gateway(CtpMdOnlyGateway)
    gateway_name = gateway.gateway_name

    registered = register_contracts(gateway, symbols, load_spread_configs())
    print(f"已注册合约占位 {len(registered)} 个（仅用于让行情推送通过，乘数以 spreads.json 为准）")

    spreads = build_spreads(load_spread_configs().values())
    recorder = TickRecorder(
        symbols=symbols,
        batch_size=args.batch,
        flush_interval=args.flush_interval,
        spreads=spreads,
    )
    print(f"价差也落库：{len(spreads)} 个（与腿同一口径：0.5 秒网格，exchange=LOCAL）")

    def on_tick(event: Event) -> None:
        tick: TickData = event.data
        recorder.on_tick(tick)

    event_engine.register(EVENT_TICK, on_tick)

    print("\n正在连接行情前置……（本次不会连接交易前置）")
    main_engine.connect(connect_setting, gateway_name)

    if not wait_md_login(gateway, MD_LOGIN_TIMEOUT):
        print(f"[错误] 行情登录超时（{MD_LOGIN_TIMEOUT:g}s）：请核对行情账号/密码/前置地址")
        main_engine.close()
        return 1

    subscribed = subscribe_all(main_engine, gateway_name, registered or symbols)
    print(f"行情登录成功，已订阅 {len(subscribed)} 个合约。开始录制（Ctrl+C 结束）……")
    print("提示：非交易时段订阅成功也不会有 tick 推送。")

    started = time.time()
    next_status = started + max(1.0, args.status_every)
    deadline = started + args.minutes * 60 if args.minutes > 0 else None

    try:
        while True:
            time.sleep(0.5)
            recorder.maybe_flush()
            now = time.time()
            if now >= next_status:
                next_status = now + max(1.0, args.status_every)
                print(recorder.progress())
                if not recorder.stats.received:
                    print("  [提示] 仍未收到任何 tick：确认当前是否处于交易时段、合约是否活跃")
            if deadline and now >= deadline:
                print("采集时长已到，准备退出")
                break
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，准备退出")
    finally:
        written = recorder.flush()
        if written:
            print(f"退出前补写 {written} 条")
        print("\n===== 采集汇总 =====")
        print(recorder.summary())
        print(f"\n数据位置：{Path(os.path.expanduser('~')) / '.vntrader' / 'database.db'}")
        print("回测：.venv\\Scripts\\python.exe run_backtest.py --spread <名字> --start ... --end ...")
        main_engine.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
