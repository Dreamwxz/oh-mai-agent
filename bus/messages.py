"""TaskCommand / TaskEvent 协议 — 命令总线消息类型。

所有消息类型均为纯 JSON 可序列化 dataclass。不包含任何运行时对象
（队列、Event、AgentLoop 引用）——这些只存在于运行时对象中。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 命令类型（CommandKind）
# ═══════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════
# 事件类型（EventKind）
# ═══════════════════════════════════════════════════════════════════════


class EventKind(str, Enum):
    """运行中任务向调度器广播的状态变更事件。

    每种枚举值对应一个生命周期节点：
    - WAITING_INPUT：任务等待用户输入（ask_user）
    - COMPLETED：任务正常完成
    - FAILED：任务执行失败
    - CANCELLED：任务已取消
    """

    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ═══════════════════════════════════════════════════════════════════════
# 辅助函数（时间与解析）
# ═══════════════════════════════════════════════════════════════════════


def _ensure_utc(value: datetime | None) -> datetime:
    """返回带时区的 UTC 时间，必要时将 naive datetime 转为 UTC。"""
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    """解析 ISO 格式字符串或 datetime 对象；解析失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        logger.debug("解析时间戳失败，返回 None：%r", str(value)[:80])
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ═══════════════════════════════════════════════════════════════════════
# 任务命令消息（TaskCommand）
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class TaskCommand:
    """针对特定任务的可序列化命令。

    由调度器（或用户）发送到运行中的 AgentLoop。``payload`` 字典内容
    随 ``kind`` 变化：
    * ``INJECT_INSTRUCTION`` → ``{"instruction": "..."}``
    * ``RESUME_REPLY`` → ``{"reply": "..."}``
    * ``CANCEL``, ``PAUSE``, ``TIMEOUT`` → ``{}``
    """

    task_id: str
    kind: CommandKind
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "command",
            "task_id": self.task_id,
            "kind": self.kind.value,
            "payload": self.payload,
            "ts": self.ts.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskCommand:
        return cls(
            task_id=str(data.get("task_id", "")),
            kind=CommandKind(data.get("kind", CommandKind.INJECT_INSTRUCTION.value)),  # 缺省 kind 时按注入指令解析
            payload=_coerce_payload(data.get("payload")),
            ts=_parse_datetime(data.get("ts")) or datetime.now(timezone.utc),
        )


# ═══════════════════════════════════════════════════════════════════════
# 任务事件消息（TaskEvent）
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class TaskEvent:
    """运行中任务向外广播的可序列化事件。

    由 AgentLoop 发出，通知调度器及其他下游监听者（如 Planner 看板）
    任务状态的变更。
    """

    task_id: str
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "event",
            "task_id": self.task_id,
            "kind": self.kind.value,
            "payload": self.payload,
            "ts": self.ts.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskEvent:
        return cls(
            task_id=str(data.get("task_id", "")),
            kind=EventKind(data.get("kind", EventKind.COMPLETED.value)),
            payload=_coerce_payload(data.get("payload")),
            ts=_parse_datetime(data.get("ts")) or datetime.now(timezone.utc),
        )


# ═══════════════════════════════════════════════════════════════════════
# 内部工具函数
# ═══════════════════════════════════════════════════════════════════════


def _coerce_payload(value: Any) -> dict[str, Any]:
    """将 payload 转为纯字典；非字典值回退为空字典。"""
    if isinstance(value, dict):
        return value
    return {}


# ═══════════════════════════════════════════════════════════════════════
# 帧级工具（供 transport / command_bus 使用）
# ═══════════════════════════════════════════════════════════════════════

# 帧区分符（"command" / "event"）→ 消息类的分派表，供 decode_frame 按 type 反序列化
_MESSAGE_REGISTRY: dict[str, type[TaskCommand | TaskEvent]] = {
    "command": TaskCommand,
    "event": TaskEvent,
}


def decode_frame(frame: bytes) -> TaskCommand | TaskEvent:
    """将原始传输帧解码为类型化消息。

    帧必须是 UTF-8 编码的 JSON，且包含 ``"type"`` 区分符
    （``"command"`` 或 ``"event"``）。

    Raises:
        ValueError: 帧不是合法 JSON，或 ``"type"`` 区分符缺失/未知。
    """
    data = json.loads(frame.decode("utf-8"))
    msg_type = data.get("type")
    cls = _MESSAGE_REGISTRY.get(msg_type)
    if cls is None:
        logger.warning("未知消息类型：%r", msg_type)
        raise ValueError(f"Unknown message type: {msg_type!r}")
    return cls.from_dict(data)
