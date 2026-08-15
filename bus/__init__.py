"""任务命令总线 — 进程内命令路由。

提供：
  - ``CommandKind`` — 命令类型枚举
  - ``TaskCommand`` — 命令数据类（纯数据，无序列化）
  - ``TaskCommandBus`` — 命令按 task_id 精准路由

> 完成通知统一为执行器直调 ``scheduler.on_task_completed`` 后，事件通道
> （``TaskEvent`` / ``EventKind`` / ``publish`` / ``listen_events``）已删除。
"""

from __future__ import annotations

from .command_bus import TaskCommandBus
from .messages import (
    CommandKind,
    TaskCommand,
)

__all__ = [
    "CommandKind",
    "TaskCommand",
    "TaskCommandBus",
]
