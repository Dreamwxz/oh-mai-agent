"""任务命令总线 — 命令/事件消息系统。

提供进程内通信抽象。DB 是唯一共享状态，命令和事件通过消息总线传递。

提供：
  - ``CommandKind`` / ``EventKind`` — 消息类型枚举
  - ``TaskCommand`` / ``TaskEvent`` — JSON 可序列化 dataclass
  - ``Transport`` — 异步传输协议（抽象基类）
  - ``LoopbackTransport`` — 进程内传输实现（基于 ``asyncio.Queue``）
  - ``TaskCommandBus`` — 命令发送 / 事件发布 / 处理器订阅
"""

from __future__ import annotations

from .messages import (
    CommandKind,
    EventKind,
    TaskCommand,
    TaskEvent,
)
from .transport import (
    LoopbackTransport,
    Transport,
)
from .command_bus import (
    TaskCommandBus,
)

__all__ = [
    "CommandKind",
    "EventKind",
    "LoopbackTransport",
    "TaskCommand",
    "TaskCommandBus",
    "TaskEvent",
    "Transport",
]
