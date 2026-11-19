"""定时代理：计划解析、到点判断、去重，以及动作分派（不连柜台）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from spreadconsole.schedule_agent import ScheduleAgent  # noqa: E402
from utils.schedule_plan import (  # noqa: E402
    ACTION_CONNECT,
    ACTION_PURGE,
    ACTION_RECONCILE,
    ACTION_START,
    ACTION_STOP,
    SchedulePlan,
    ScheduleTask,
    format_days,
    parse_days,
    parse_time,
)

TRADING_DAYS = (1, 2, 3, 4, 5)


def make_plan() -> SchedulePlan:
    return SchedulePlan(tasks=[
        ScheduleTask("08:49", TRADING_DAYS, ACTION_PURGE, True, "清理缓存"),
        ScheduleTask("08:50", TRADING_DAYS, ACTION_CONNECT, True, "连接"),
        ScheduleTask("08:55", TRADING_DAYS, ACTION_RECONCILE, True, "核对"),
        ScheduleTask("08:58", TRADING_DAYS, ACTION_START, True, "开始"),
    ])


class TestTimeAndDays(unittest.TestCase):
    def test_parse_time(self) -> None:
        self.assertEqual(parse_time("08:50"), 8 * 60 + 50)
        self.assertEqual(parse_time(" 23:59 "), 23 * 60 + 59)
        self.assertIsNone(parse_time("24:00"))
        self.assertIsNone(parse_time("8:5"))
        self.assertIsNone(parse_time(""))

    def test_parse_days(self) -> None:
        self.assertEqual(parse_days("1-5"), (1, 2, 3, 4, 5))
        self.assertEqual(parse_days("1,3,5"), (1, 3, 5))
        self.assertEqual(parse_days("一,三"), (1, 3))
        self.assertEqual(parse_days("1-3,5"), (1, 2, 3, 5))
        self.assertIsNone(parse_days("0"))
        self.assertIsNone(parse_days("5-1"))
        self.assertIsNone(parse_days("abc"))

    def test_format_days(self) -> None:
        self.assertEqual(format_days((1, 2, 3, 4, 5)), "1-5")
        self.assertEqual(format_days((1, 3, 5)), "1,3,5")
        self.assertEqual(format_days((6, 7)), "6-7")


class TestScheduleTask(unittest.TestCase):
    def test_from_dict_rejects_invalid(self) -> None:
        self.assertIsNone(ScheduleTask.from_dict({"time": "8:5", "action": "start"}))
        self.assertIsNone(ScheduleTask.from_dict({"time": "25:00", "action": "start"}))
        self.assertIsNone(ScheduleTask.from_dict({"time": "08:50", "action": "nope"}))
        self.assertIsNone(ScheduleTask.from_dict({"time": "08:50", "action": "start",
                                                  "days": [0, 9]}))

    def test_from_dict_normalizes_short_hour(self) -> None:
        task = ScheduleTask.from_dict({"time": "8:50", "action": "start"})
        self.assertIsNotNone(task)
        self.assertEqual(task.time, "08:50")

    def test_from_dict_defaults_to_trading_days(self) -> None:
        task = ScheduleTask.from_dict({"time": "08:50", "action": "start"})
        self.assertIsNotNone(task)
        self.assertEqual(task.days, TRADING_DAYS)

    def test_describe_marks_bad_time(self) -> None:
        task = ScheduleTask("bad", TRADING_DAYS, ACTION_START)
        self.assertIn("非法", task.describe())


class TestSchedulePlan(unittest.TestCase):
    def test_due_matches_minute_and_weekday(self) -> None:
        plan = make_plan()
        monday = datetime(2026, 11, 9, 8, 50, 30)          # 周一
        due = plan.due(monday)
        self.assertEqual([task.action for task in due], [ACTION_CONNECT])
        self.assertEqual(plan.due(datetime(2026, 11, 9, 8, 51)), [])
        self.assertEqual(plan.due(datetime(2026, 11, 7, 8, 50)), [])   # 周六

    def test_same_task_runs_once_per_day(self) -> None:
        plan = make_plan()
        monday = datetime(2026, 11, 9, 8, 50)
        first = plan.due(monday)
        self.assertEqual(len(first), 1)
        plan.mark_done(first[0], monday)
        self.assertEqual(plan.due(monday), [])             # 同一分钟再扫不重复
        self.assertEqual(plan.due(monday.replace(second=40)), [])
        # 次日同一时刻重新触发
        self.assertEqual(len(plan.due(monday.replace(day=10))), 1)

    def test_disabled_plan_and_task(self) -> None:
        plan = make_plan()
        monday = datetime(2026, 11, 9, 8, 55)
        # 只禁用 reconcile 那一条，其余保持不变
        plan.tasks = [
            ScheduleTask(task.time, task.days, task.action,
                         False if task.action == ACTION_RECONCILE else task.enabled)
            for task in plan.tasks
        ]
        self.assertEqual(plan.due(monday), [])
        plan.enabled = False
        self.assertEqual(plan.due(datetime(2026, 11, 9, 8, 49)), [])

    def test_next_at_skips_weekend(self) -> None:
        plan = make_plan()
        friday_evening = datetime(2026, 11, 13, 20, 0)      # 周五收盘后
        moment = plan.next_at(friday_evening)
        self.assertIsNotNone(moment)
        when, task = moment
        self.assertEqual(when.isoweekday(), 1)              # 下周一
        self.assertEqual(task.action, ACTION_PURGE)         # 08:49 清理，先于 08:50 连接

    def test_next_at_none_when_disabled(self) -> None:
        plan = make_plan()
        plan.enabled = False
        self.assertIsNone(plan.next_at(datetime(2026, 11, 9, 8, 0)))

    def test_save_and_load_roundtrip(self) -> None:
        plan = make_plan()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "schedule.json"
            plan.save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(raw["tasks"]), 4)
            loaded = SchedulePlan.load(path)
        self.assertEqual([task.action for task in loaded.tasks],
                         [ACTION_PURGE, ACTION_CONNECT, ACTION_RECONCILE, ACTION_START])
        self.assertTrue(loaded.enabled)

    def test_load_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "nope.json"
            plan = SchedulePlan.load(missing)
            self.assertEqual(len(plan.tasks), 4)
            broken = Path(folder) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(len(SchedulePlan.load(broken).tasks), 4)


class TestScheduleAgent(unittest.TestCase):
    def _agent(self, plan: SchedulePlan) -> tuple[ScheduleAgent, MagicMock, MagicMock]:
        control = MagicMock()
        control.md_ready.return_value = True
        control.td_ready.return_value = True
        control.master_running = False
        main_engine = MagicMock()
        agent = ScheduleAgent(control, main_engine, plan,
                              Path(tempfile.gettempdir()) / "schedule.json",
                              auto_start=False)
        return agent, control, main_engine

    def test_fires_once_and_dispatches_start(self) -> None:
        agent, control, _main = self._agent(make_plan())
        monday = datetime(2026, 11, 9, 8, 58)

        fired = agent.check(monday)
        self.assertEqual([task.action for task in fired], [ACTION_START])
        control.start_all.assert_not_called()

        self.assertEqual(agent.check(monday), [])      # 同一分钟不重复
        control.start_all.assert_not_called()

    def test_start_skipped_when_disconnected(self) -> None:
        agent, control, _main = self._agent(make_plan())
        control.td_ready.return_value = False

        fired = agent.check(datetime(2026, 11, 9, 8, 58))

        self.assertEqual(len(fired), 1)
        control.start_all.assert_not_called()

    def test_stop_skipped_when_not_running(self) -> None:
        plan = SchedulePlan(tasks=[
            ScheduleTask("15:01", TRADING_DAYS, ACTION_STOP, True),
        ])
        agent, control, _main = self._agent(plan)

        agent.check(datetime(2026, 11, 9, 15, 1))

        control.stop_all.assert_not_called()

    def test_stop_calls_control_when_running(self) -> None:
        plan = SchedulePlan(tasks=[
            ScheduleTask("15:01", TRADING_DAYS, ACTION_STOP, True),
        ])
        agent, control, _main = self._agent(plan)
        control.master_running = True

        agent.check(datetime(2026, 11, 9, 15, 1))

        control.stop_all.assert_called_once()

    def test_action_failure_does_not_raise(self) -> None:
        plan = SchedulePlan(tasks=[
            ScheduleTask("08:58", TRADING_DAYS, ACTION_STOP, True),
        ])
        agent, control, main = self._agent(plan)
        control.master_running = True
        control.stop_all.side_effect = RuntimeError("boom")

        fired = agent.check(datetime(2026, 11, 9, 8, 58))

        self.assertEqual(len(fired), 1)
        messages = " ".join(str(call) for call in main.write_log.call_args_list)
        self.assertIn("boom", messages)

    def test_describe_next_uses_plan(self) -> None:
        agent, _control, _main = self._agent(make_plan())
        text = agent.describe_next(datetime(2026, 11, 9, 8, 0))
        self.assertIn("08:49", text)      # 最早的一条是缓存清理

    def test_purge_action_dispatches(self) -> None:
        plan = SchedulePlan(tasks=[
            ScheduleTask("08:49", TRADING_DAYS, ACTION_PURGE, True)])
        agent, control, main = self._agent(plan)
        control.purge_stale_caches.return_value = {
            "engine_orders": 3, "engine_trades": 2, "reserved": 1}

        fired = agent.check(datetime(2026, 11, 9, 8, 49))

        self.assertEqual([task.action for task in fired], [ACTION_PURGE])
        control.purge_stale_caches.assert_not_called()
        messages = " ".join(str(call) for call in main.write_log.call_args_list)
        self.assertIn("跳过缓存清理", messages)

    def test_scheduled_snapshot_cannot_confirm_or_correct(self):
        agent, control, main = self._agent(make_plan())
        td = control._gateway.return_value.td_api
        td.orders_query_pending = False
        td.orders_snapshot_ok = True
        td.orders_snapshot = {'o1'}
        td.position_snapshot_sides = {}
        control._snapshot_problem.return_value = ''
        agent._reconcile_apply()
        control.confirm_reconciliation.assert_not_called()
        control.sync_all.assert_not_called()
        self.assertIn('未授予交易许可', str(main.write_log.call_args))

    def test_purge_skipped_while_trading(self) -> None:
        plan = SchedulePlan(tasks=[
            ScheduleTask("08:49", TRADING_DAYS, ACTION_PURGE, True)])
        agent, control, main = self._agent(plan)
        control.master_running = True

        agent.check(datetime(2026, 11, 9, 8, 49))

        control.purge_stale_caches.assert_not_called()
        messages = " ".join(str(call) for call in main.write_log.call_args_list)
        self.assertIn("跳过缓存清理", messages)


if __name__ == "__main__":
    unittest.main()
