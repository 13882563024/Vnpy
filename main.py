"""VeighNa 最小可运行交易终端。

主窗口提供：
    交易   —— CTP 网关连接、委托/成交/持仓监控、手动下单
    应用   —— CTA 策略、CTA 回测、价差交易、风控管理

同时把「跨期均值回归策略」接进价差交易面板（见 ``utils/engine_setup.py``）：
加载策略类 → 换掉默认的吃单算法 → 按 ``config/spreads.json`` 注册价差。
这三步缺一步都不会报错，只会行为不对，所以启动时做完整自检。

另外按 ``config/runtime.json`` 的开关**边跑边把 tick 落库**：与策略共用同一份行情订阅，
不需要另开进程；录下来的数据可直接给 ``run_backtest.py`` 用。
"""

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_riskmanager import RiskManagerApp

from gateway.ctp_spread_gateway import SpreadCtpGateway
from spreadconsole import SpreadConsoleApp
from tickbrowser import TickBrowserApp
from utils.engine_setup import attach_tick_recorder, flush_tick_recorder, setup_spread_trading


def main() -> None:
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 数据目录由 vnpy 决定：当前目录若存在 .vntrader，库就切到那里（看起来"数据全没了"）。
    # 启动时把实际库文件打进日志，避免误判。
    from vnpy.trader.utility import get_file_path

    main_engine.write_log(f"数据库文件：{get_file_path('database.db')}")

    # CTP 接口：连接参数在界面「交易 -> 连接 CTP」中填写，
    # 保存后可复用，落盘位置 ~/.vntrader/ctp_connect.json
    # 用带组合单回报容错的子类：组合合约可能不在合约查询结果里，
    # 原生 CtpGateway 解析 onRtnOrder 时会 KeyError，日志/策略线程会中断。
    main_engine.add_gateway(SpreadCtpGateway)

    # 应用模块
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    spread_engine = main_engine.add_app(SpreadConsoleApp)
    main_engine.add_app(RiskManagerApp)
    main_engine.add_app(TickBrowserApp)      # 功能 → 行情数据：看已采集的 tick / 合成价差

    # 把跨期策略接进价差交易面板：策略类 + 非吃单算法 + 价差注册 + 按配置建策略实例
    try:
        setup_spread_trading(spread_engine, create_strategies=True)
    except Exception as exc:  # noqa: BLE001 - 接线失败不该拦住整个终端
        main_engine.write_log(f"跨期策略接线失败，价差面板将只保留内置策略：{exc}")

    # tick 落库：与策略共用同一份订阅，边跑边存（开关在 config/runtime.json）
    recorder = None
    try:
        recorder = attach_tick_recorder(main_engine, event_engine)
    except Exception as exc:  # noqa: BLE001
        main_engine.write_log(f"tick 落库启动失败：{exc}")

    # 自有价差窗口复用同一个采集器；动态添加的腿可加入白名单。
    main_engine.spread_tick_recorder = recorder
    qapp.aboutToQuit.connect(lambda: flush_tick_recorder(recorder, main_engine.write_log))

    window = MainWindow(main_engine, event_engine)
    window.showMaximized()
    qapp.exec()


if __name__ == "__main__":
    main()
