"""启动期的 UI 补丁（不改 site-packages，升级 vnpy 后重新打即可）。

目前只有一个：给内置监控面板的「时间」列补上日期。

vnpy 的 ``TimeCell`` 只格式化 ``%H:%M:%S``，委托 / 成交 / 日志面板都复用它。
跨日复盘时 ``22:00`` 与次日 ``10:00`` 看不出区别，夜盘成交和日盘成交混在一起。
这里把格式换成 ``11-17 10:30:15.123``，保留毫秒与时区转换。
"""

from __future__ import annotations

import logging

#: 想改回只显示时间，把这里换成 ``"%H:%M:%S"`` 即可
TIME_CELL_FORMAT = "%m-%d %H:%M:%S"

_log = logging.getLogger(__name__)


def patch_time_cell_with_date() -> bool:
    """给 ``TimeCell`` 的文本加上日期；返回是否打成功（重复调用幂等）。

    失败只记日志：UI 补丁不该拦住交易终端启动（例如 vnpy 升级后类名变了）。
    """
    try:
        from vnpy.trader.ui.widget import TimeCell
    except Exception as exc:      # noqa: BLE001 - 拿不到就跳过，保持原样
        _log.warning("时间列补丁未生效（导入 TimeCell 失败）：%s", exc)
        return False

    if getattr(TimeCell, "_date_patched", False):
        return True

    def set_content(self, content, data) -> None:      # noqa: ANN001 - 与库签名一致
        if content is None:
            return
        content = content.astimezone(self.local_tz)
        moment = content.strftime(TIME_CELL_FORMAT)
        millisecond = int(content.microsecond / 1000)
        timestamp = f"{moment}.{millisecond:03d}"
        self._text = timestamp
        self.setText(timestamp)
        self._data = data

    TimeCell.set_content = set_content
    TimeCell._date_patched = True
    return True
