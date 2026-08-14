"""任务命令总线 — 进程内命令路由与事件广播。

提供：
  - ``CommandKind`` / ``EventKind`` — 消息类型枚举
  - ``TaskCommand`` / ``TaskEvent`` — 消息数据类（纯数据，无序列化）
  - ``TaskCommandBus`` — 命令按 task_id 精准路由 + 事件队列广播
"""

from __future__ import annotations

from .command_bus import TaskCommandBus
from .messages import (
    CommandKind,
    EventKind,
    TaskCommand,
    TaskEvent,
)

__all__ = [
    "CommandKind",
    "EventKind",
    "TaskCommand",
    "TaskCommandBus",
    "TaskEvent",
]
