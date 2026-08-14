"""任务命令与事件消息类型 — 总线消息契约。

仅定义进程内消息的纯数据 dataclass（无序列化方法、无传输层）。
命令按 ``task_id`` 精准投递到运行中的 AgentLoop；事件广播给事件监听者
（当前唯一监听者：``TaskScheduler``，用于释放并发额度）。

> 架构变更：v0.1.0 曾尝试跨进程传输（WorkerManager + StdioTransport），
> 后因复杂度与收益不匹配回退到进程内方案，字节帧序列化 / ``decode_frame`` /
> ``Transport`` 协议已一并移除。当前全部消息在 Runner 进程内以类型化对象传递。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CommandKind(str, Enum):
    """从调度器向运行中任务发送的控制命令。

    每种枚举值对应一个具体的控制动作：
    - INJECT_INSTRUCTION：注入用户指令到任务执行流
    - RESUME_REPLY：用户回复后唤醒等待中的任务
    - CANCEL：取消任务
    - PAUSE：暂停任务
    - RESUME：恢复任务
    """

    INJECT_INSTRUCTION = "inject_instruction"
    RESUME_REPLY = "resume_reply"
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"


class EventKind(str, Enum):
    """运行中任务向调度器广播的状态变更事件。

    每种枚举值对应一个生命周期节点，均用于调度器释放并发额度：
    - COMPLETED：任务正常完成
    - FAILED：任务执行失败
    - CANCELLED：任务已取消
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class TaskCommand:
    """针对特定任务的控制命令。

    ``payload`` 字典内容随 ``kind`` 变化：
    * ``INJECT_INSTRUCTION`` → ``{"instruction": "..."}``
    * ``RESUME_REPLY`` → ``{"reply": "..."}``
    * ``CANCEL`` / ``PAUSE`` / ``RESUME`` → ``{}``
    """

    task_id: str
    kind: CommandKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskEvent:
    """运行中任务向外广播的生命周期事件。

    由 AgentLoop 发出，通知调度器释放并发额度。
    """

    task_id: str
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
