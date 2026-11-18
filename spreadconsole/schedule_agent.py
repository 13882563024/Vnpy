"""定时代理：按 ``config/schedule.json`` 在指定时刻连接、核对纠正、启停交易。

设计要点：

- 调度判断在 :class:`utils.schedule_plan.SchedulePlan` 里（纯逻辑、可单测），
  本模块只负责**执行**，所以柜台不可用时也不会影响计划解析。
- 每 :data:`CHECK_INTERVAL_MS` 检查一次；同一任务当天只执行一次（由 plan 去重），
  所以每隔 10 秒扫一遍也不会重复触发。
- 每个动作单独 try，一个失败不影响后续任务。
- 核对是异步的（CTP 查询限流），这里用 ``QTimer.singleShot`` 分三步：
  查资金 → 查持仓 → 确认并批量纠正。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6 import QtCore
from vnpy.trader.utility import get_file_path

from utils.schedule_plan import (
    ACTION_CONNECT,
    ACTION_PURGE,
    ACTION_RECONCILE,
    ACTION_START,
    ACTION_STOP,
    SchedulePlan,
    ScheduleTask,
)

GATEWAY_NAME = "CTP"
# vnpy 的连接配置落盘名是 connect_{gateway}.json（全部小写）；旧脚本用过
# ctp_connect.json，两个都认，避免"明明保存过却说找不到配置"。
CONNECT_FILENAMES = ("connect_ctp.json", "ctp_connect.json")
# 资金与持仓必须错开发送（CTP 查询接口约每秒限流一次）
QUERY_GAP_MS = 1300
# 持仓回报到齐的等待时间；不足时 confirm_reconciliation 会自己报错，不会误判为空仓
POSITION_WAIT_MS = 2500


class ScheduleAgent(QtCore.QObject):
    """按计划触发动作；``control`` 为 ``SpreadTradingControl``。"""

    CHECK_INTERVAL_MS = 10_000

    def __init__(self, control, main_engine, plan: SchedulePlan, path: Path,
                 *, auto_start: bool = True) -> None:
        super().__init__()
        self.control = control
        self.main_engine = main_engine
        self.plan = plan
        self.path = Path(path)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.check)
        if auto_start:
            self.timer.start(self.CHECK_INTERVAL_MS)

    # ------------------------------------------------------------ 计划
    def reload(self) -> SchedulePlan:
        """从磁盘重读计划（界面保存后调用）。"""
        self.plan = SchedulePlan.load(self.path)
        return self.plan

    def save(self) -> None:
        self.plan.save(self.path)

    def describe_next(self, now: datetime | None = None) -> str:
        moment = self.plan.next_at(now or datetime.now())
        if moment is None:
            return "定时代理未启用或无有效任务"
        when, task = moment
        return f"下次：{when:%m-%d %H:%M} {task.describe()}"

    # ------------------------------------------------------------ 调度
    def check(self, now: datetime | None = None) -> list[ScheduleTask]:
        """扫描一次到点任务并执行；返回本轮触发的任务（便于测试）。"""
        moment = now or datetime.now()
        fired: list[ScheduleTask] = []
        for task in self.plan.due(moment):
            self.plan.mark_done(task, moment)
            fired.append(task)
            try:
                self._dispatch(task, moment)
            except Exception as exc:      # noqa: BLE001 - 单个任务失败不阻断后续
                self._log(f"定时任务失败（{task.describe()}）：{exc}")
        return fired

    def _dispatch(self, task: ScheduleTask, now: datetime) -> None:
        handler = {
            ACTION_PURGE: self._purge,
            ACTION_CONNECT: self._connect,
            ACTION_RECONCILE: self._reconcile,
            ACTION_START: self._start,
            ACTION_STOP: self._stop,
        }.get(task.action)
        if handler is None:
            self._log(f"定时任务：未知动作 {task.action!r}，已跳过")
            return
        self._log(f"定时任务触发：{task.describe()}" + (f"（{task.note}）" if task.note else ""))
        handler(now)

    # ------------------------------------------------------------ 动作
    def _purge(self, now: datetime) -> None:
        """清掉隔夜委托缓存，避免策略误以为止盈单还挂在柜台。"""
        if self.control.master_running:
            self._log("定时任务：交易运行中，跳过缓存清理（避免清掉在途委托）")
            return
        try:
            result = self.control.purge_stale_caches(now=now)
        except Exception as exc:      # noqa: BLE001 - 清理失败不应阻断开盘流程
            self._log(f"定时任务：缓存清理失败 {exc}")
            return
        dropped = result.get("engine_orders", 0) + result.get("engine_trades", 0) \
            + result.get("reserved", 0) + result.get("algos", 0)
        if dropped:
            self._log(
                f"定时任务：缓存清理完成 —— 隔夜委托 {result.get('engine_orders', 0)} 笔、"
                f"成交 {result.get('engine_trades', 0)} 笔、策略占位 "
                f"{result.get('reserved', 0)} 条、旧算法 {result.get('algos', 0)} 个已清掉")
        else:
            self._log("定时任务：缓存清理完成，没有隔夜残留（缓存已是当日状态）")

    def _connect_setting(self) -> dict | None:
        """读 vnpy 保存的 CTP 连接配置；两个历史文件名都试。"""
        for filename in CONNECT_FILENAMES:
            path = Path(get_file_path(filename))
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict) and raw:
                return raw
        return None

    def _connect(self, now: datetime) -> None:
        if self.control.md_ready() and self.control.td_ready():
            self._log("定时任务：交易与行情均已连接，跳过重复连接")
            return
        setting = self._connect_setting()
        if not setting:
            self._log("定时任务：未找到 CTP 连接配置（~/.vntrader/connect_ctp.json），"
                      "无法自动连接；请先在「交易 → 连接 CTP」保存一次配置")
            return
        self.main_engine.connect(setting, GATEWAY_NAME)
        self._log("定时任务：已发起 CTP 连接")

    def _reconcile(self, now: datetime) -> None:
        if not (self.control.md_ready() and self.control.td_ready()):
            self._log("定时任务：交易或行情未连接，跳过核对；订单簿状态未确认前不会自动开始交易")
            return
        gateway = self.control._gateway()
        if gateway is None:
            self._log("定时任务：取不到 CTP 网关，跳过核对")
            return
        gateway.query_account()
        QtCore.QTimer.singleShot(QUERY_GAP_MS, self._reconcile_query_position)

    def _reconcile_query_position(self) -> None:
        try:
            gateway = self.control._gateway()
            if gateway is None:
                return
            gateway.query_position()
        except Exception as exc:      # noqa: BLE001
            self._log(f"定时任务：查询持仓失败 {exc}")
            return
        QtCore.QTimer.singleShot(QUERY_GAP_MS, self._reconcile_query_orders)

    def _reconcile_query_orders(self) -> None:
        """查柜台当日委托：这是判断“本地委托是否还有效”的唯一权威依据。

        CTP 委托当日有效，交易所的当日委托列表只含当前交易日；本地记录里柜台
        没有的单，就是上一交易日随结算失效的（2026-11-17 夜间遗留 6 张单占着止盈位，
        导致 A 全天挂不出止盈）。查不到就按“状态未知”处理，一条也不清。
        """
        try:
            gateway = self.control._gateway()
            if gateway is not None and hasattr(gateway, "query_orders"):
                gateway.query_orders()
        except Exception as exc:      # noqa: BLE001 - 查询失败不清本地记录
            self._log(f"定时任务：查询当日委托失败 {exc}；本地委托状态未知，不做清理")
        QtCore.QTimer.singleShot(POSITION_WAIT_MS, self._reconcile_apply)

    def _reconcile_apply(self) -> None:
        try:
            self.control.confirm_reconciliation()
        except Exception as exc:      # noqa: BLE001 - 快照不完整时拒绝确认是对的
            self._log(f"定时任务：核对未通过（{exc}）；不会自动开始交易")
            return
        if self.control.master_running:
            self._log("定时任务：核对完成；当前已在交易中，跳过纠正")
            return
        try:
            results = self.control.sync_all(force=True)
        except Exception as exc:      # noqa: BLE001
            self._log(f"定时任务：批量纠正失败 {exc}")
            return
        ok = [name for name, kind, _ in results if kind == "ok"]
        skip = [name for name, kind, _ in results if kind == "skip"]
        fails = [(name, message) for name, kind, message in results if kind == "fail"]
        self._log(f"定时任务：核对并纠正完成，成功 {len(ok)}、无需处理 {len(skip)}、"
                  f"失败 {len(fails)}")
        for name, message in fails:
            self._log(f"定时任务：{name} 纠正失败 —— {message}")

    def _start(self, now: datetime) -> None:
        if self.control.master_running:
            self._log("定时任务：已在交易中，跳过开始")
            return
        if not self.control.md_ready() or not self.control.td_ready():
            self._log("定时任务：交易或行情未连接，跳过开始交易")
            return
        try:
            started = self.control.start_all(force=True)
        except Exception as exc:      # noqa: BLE001 - 失败原因要留在日志里
            self._log(f"定时任务：开始交易失败 {exc}")
            return
        self._log(f"定时任务：已开始交易（{len(started)} 个合约）")

    def _stop(self, now: datetime) -> None:
        if not self.control.master_running:
            self._log("定时任务：当前未在交易，跳过停止")
            return
        self.control.stop_all(reason="定时任务停止交易")
        self._log("定时任务：已停止交易（已有委托保留，仍可能成交）")

    # ------------------------------------------------------------ 日志
    def _log(self, message: str) -> None:
        writer = getattr(self.main_engine, "write_log", None)
        if callable(writer):
            writer(message)
