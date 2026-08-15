"""状态格式化器。

提供 ``StatusFormatter`` 将 ``(TaskStatus, datetime|None)``
转为中文时间描述字符串。

用法::

    from .status_formatter import StatusFormatter

    fmt = StatusFormatter()
    print(fmt.format(TaskStatus.RUNNING, datetime.now() - timedelta(seconds=65)))
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .task_record import TaskStatus

logger = logging.getLogger(__name__)


class StatusFormatter:
    """将 (TaskStatus, timestamp) 转为中文状态描述字符串。

    Args:
        now: 可选的默认参考时间；为 None 时每次 ``format()`` 调用都取
             ``datetime.now()``（长期持有的实例不会冻结时钟）。单元测试
             可注入固定时间戳以保持确定性。
    """

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now  # 可为 None：format() 时再取当前时间

    # ── 状态格式化 ────────────────────────────────────────────────

    def format(self, status: TaskStatus, relevant_ts: datetime | None = None) -> str:
        """将 (status, relevant_ts) 转为中文状态描述。

        Args:
            status: 任务状态。
            relevant_ts: 关联时间戳（RUNNING→started_at,
                         WAITING_INPUT→updated_at, SCHEDULED→scheduled_at）。

        Returns:
            中文字符串。例如：

            - RUNNING + started_at → ``"已运行 3 分钟"``
            - SCHEDULED + scheduled_at → ``"5 分钟后开始"``
            - WAITING_INPUT + updated_at → ``"已等待 1 分钟"``
            - 静态状态 → ``"排队中"``
        """
        # 参考时间每次调用时取值：长期持有的实例（如 TaskManager 单例）不会
        # 以构造时刻为基准计算相对时长；测试注入的固定时间仍优先。
        now = self._now or datetime.now()

        if status == TaskStatus.RUNNING:
            if relevant_ts is not None:
                elapsed = max((now - relevant_ts).total_seconds(), 0)  # 时间戳超前时钳制为 0，避免负时长
                duration = self._format_duration(elapsed)
                return f"已运行 {duration}"
            return "运行中"

        if status == TaskStatus.SCHEDULED:
            if relevant_ts is not None:
                delta = (relevant_ts - now).total_seconds()
                if delta <= 0:  # 触发时刻已过但仍为 SCHEDULED（待调度器拉起）→ 显示「即将开始」
                    return "即将开始"
                if delta < 60:
                    return f"{int(delta)} 秒后开始"
                if delta < 3600:
                    return f"{int(delta // 60)} 分钟后开始"
                if delta < 86400:
                    return f"{int(delta // 3600)} 小时后开始"
                # 已超 24 小时：目标落在明天自然日（次日零点至再日零点）→ 显示「明天 HH:MM」，否则按整天数
                tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                if relevant_ts < tomorrow + timedelta(days=1):
                    return f"明天 {relevant_ts.strftime('%H:%M')} 开始"
                days = max((relevant_ts - now).days, 1)  # .days 向下取整，兜底至少 1 天
                return f"{days} 天后开始"
            return "等待触发"

        if status == TaskStatus.WAITING_INPUT:
            if relevant_ts is not None:
                elapsed = max((now - relevant_ts).total_seconds(), 0)  # 时间戳超前时钳制为 0，避免负时长
                duration = self._format_duration(elapsed)
                return f"已等待 {duration}"
            return "等待回复"

        # 静态状态兜底表（动态状态已在上方提前返回，此处条目为防御性冗余；未知状态回退枚举原值）
        _STATIC: dict[TaskStatus, str] = {
            TaskStatus.PENDING: "排队中",
            TaskStatus.RUNNING: "运行中",
            TaskStatus.WAITING_INPUT: "等待回复",
            TaskStatus.PAUSED: "已暂停",
            TaskStatus.COMPLETED: "已完成",
            TaskStatus.FAILED: "失败",
            TaskStatus.CANCELLED: "已取消",
            TaskStatus.SCHEDULED: "等待触发",
        }
        return _STATIC.get(status, status.value)

    # ── 相对时间格式化 ────────────────────────────────────────────

    def format_relative(self, seconds: float) -> str:
        """将秒数格式化为中文相对时间描述。

        Args:
            seconds: 已过去的秒数（非负）。

        Returns:
            中文相对时间，如 ``"3 分钟前"``。
        """
        if seconds < 10:  # 10 秒内归为「刚刚」
            return "刚刚"
        elif seconds < 60:
            return f"{int(seconds)} 秒前"
        elif seconds < 3600:
            return f"{int(seconds // 60)} 分钟前"
        elif seconds < 86400:
            return f"{int(seconds // 3600)} 小时前"
        else:
            return f"{int(seconds // 86400)} 天前"

    # ── 内部辅助 ──────────────────────────────────────────────────

    def _format_duration(self, elapsed_seconds: float) -> str:
        """将秒数转为中文纯持续时间字符串（无前缀、无"前"字后缀）。

        例如 ``"1 分钟"``。注意：超过 24 小时仍以小时计，不折算为天。
        """
        s = elapsed_seconds
        if s < 60:
            return f"{int(s)} 秒"
        elif s < 3600:
            return f"{int(s // 60)} 分钟"
        else:
            return f"{int(s // 3600)} 小时"
