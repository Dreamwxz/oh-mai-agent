"""TaskRecord — 纯持久化数据模型。

TaskRecord 是一个 dataclass，将所有任务字段收纳在单个 JSON 可序列化的
字典中。运行时状态（队列、Event、AgentLoop 引用）一律放在 ``TaskRuntime``，不在此处。

枚举与工具函数直接定义在本文件中，不依赖外部模块。
任务记录定义在本文件末尾，用于保持现有代码兼容。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════════════


class TaskLevel(str, Enum):
    INSTANT = "instant"   # 即时执行（原 L1）
    AGENT   = "agent"     # 离线长时（原 L3）


class TaskStatus(str, Enum):
    # 任务状态机取值：活跃态（SCHEDULED/PENDING/RUNNING/WAITING_INPUT/PAUSED）
    # 与终态（COMPLETED/FAILED/CANCELLED），合法转换表见下方 _ALLOWED_TRANSITIONS。
    SCHEDULED = "scheduled"
    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TriggerType(str, Enum):
    NOW = "now"
    DELAY = "delay"
    CRON = "cron"


# ═══════════════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════════════


class TaskStatusError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════
# 状态机
# ═══════════════════════════════════════════════════════════════════════

# 合法转换表：键为当前状态，值为允许转换到的目标状态集合；终态无任何后继。
_ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.SCHEDULED:     {TaskStatus.PENDING, TaskStatus.CANCELLED},
    TaskStatus.PENDING:       {TaskStatus.RUNNING, TaskStatus.CANCELLED,
                               TaskStatus.SCHEDULED, TaskStatus.FAILED},
    TaskStatus.RUNNING:       {TaskStatus.WAITING_INPUT, TaskStatus.PAUSED,
                               TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.WAITING_INPUT: {TaskStatus.RUNNING, TaskStatus.PAUSED,
                               TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.PAUSED:        {TaskStatus.PENDING, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED:     set(),
    TaskStatus.FAILED:        set(),
    TaskStatus.CANCELLED:     set(),
}

# 终态集合：进入终态后不可再转换，is_terminal() 据此判定。
_TERMINAL_STATUSES: set[TaskStatus] = {
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
}


# ═══════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def format_relative_time(seconds: float) -> str:
    """将秒数格式化为中文相对时间描述。

    Args:
        seconds: 已过去的秒数（非负）。

    Returns:
        中文相对时间字符串，如 ``"刚刚"``、``"3 分钟前"``、``"2 天前"``。
    """
    if seconds < 10:
        return "刚刚"
    elif seconds < 60:
        return f"{int(seconds)} 秒前"
    elif seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    elif seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    else:
        return f"{int(seconds // 86400)} 天前"


# ═══════════════════════════════════════════════════════════════════════
# StatusChange — 状态变更日志中的单条记录
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class StatusChange:
    """状态变更审计条目。

    记录一次状态转换的时间点、触发者和原因，
    由 ``transition`` / ``force`` 方法自动追加到 ``_status_log``。
    """

    timestamp: datetime
    status: TaskStatus
    actor: str = "system"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "actor": self.actor,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StatusChange:
        ts = _parse_datetime(data.get("timestamp")) or datetime.now()
        st = TaskStatus(data.get("status", TaskStatus.PENDING.value))
        actor = str(data.get("actor", "system"))
        reason = str(data.get("reason", ""))
        return cls(timestamp=ts, status=st, actor=actor, reason=reason)


# ═══════════════════════════════════════════════════════════════════════
# TaskRecord — 纯持久化状态
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class TaskRecord:
    """持久化任务状态——全部字段均可 JSON 序列化。

字段与旧版任务 dataclass 完全一致。
    可作为无缝替换。
    """

    # ── 标识 ──
    id: str
    title: str
    intent: str

    # ── 分级与状态 ──
    level: TaskLevel

    # ── 归属 ──
    owner: str
    stream_id: str
    platform: str
    reply_stream_id: str | None = None

    status: TaskStatus = TaskStatus.PENDING  # 当前状态（状态变更须走 transition()/force() 以通过校验并留痕审计）

    # ── 触发 ──
    trigger_type: TriggerType = TriggerType.NOW
    delay_seconds: int | None = None
    cron_expr: str | None = None
    scheduled_at: datetime | None = None

    # ── 时间戳与优先级 ──
    started_at: datetime | None = None
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    # ── 运行时约束 ──
    max_runtime_min: int = 0

    # ── 扩展 ──
    metadata: dict = field(default_factory=dict)

    # ── 审计 ──
    _status_log: list[StatusChange] = field(default_factory=list, init=False, repr=False)  # 状态变更审计日志（transition()/force() 自动追加，不参与 init/repr）

    # ─────────────────────────────────────────────────────────────────
    # 状态机
    # ─────────────────────────────────────────────────────────────────

    def transition(self, new_status: TaskStatus, actor: str = "system") -> None:
        """执行状态转换（受状态机约束）。

        Args:
            new_status: 目标状态。
            actor: 触发转换的组件名（用于审计日志）。

        Raises:
            TaskStatusError: 当转换不被状态机允许时。
        """
        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            logger.debug(
                "拒绝非法状态转换（业务可预期，由调用方捕获处理）：task=%s, %s → %s",
                self.id, self.status.value, new_status.value,
            )
            raise TaskStatusError(
                f"非法状态转换：{self.status.value} → {new_status.value}"
            )
        self._status_log.append(StatusChange(
            timestamp=datetime.now(),
            status=new_status,
            actor=actor,
        ))
        self.status = new_status
        self.updated_at = datetime.now()

    def force(self, new_status: TaskStatus, actor: str, reason: str) -> None:
        """强制设置状态，跳过状态机校验。

        唯一的恢复/兜底逃逸口——终态回退、重启恢复、异常兜底等场景必须
        走此方法；与直接赋值不同，force() 会追加审计记录并刷新 updated_at。

        Args:
            new_status: 目标状态。
            actor: 触发方标识（如 ``"agent_loop"``、``"scheduler"``）。
            reason: 强制原因（如 ``"cron_reschedule"``、``str(exc)``）。
        """
        logger.debug(
            "force 兜底强制状态转换：task=%s, actor=%s, %s → %s, reason=%s",
            self.id, actor, self.status.value, new_status.value, reason,
        )
        self._status_log.append(StatusChange(
            timestamp=datetime.now(),
            status=new_status,
            actor=actor,
            reason=reason,
        ))
        self.status = new_status
        self.updated_at = datetime.now()

    def _restore(self, status: TaskStatus, status_log: list[StatusChange]) -> None:
        """从持久化恢复状态，不触发校验。

        直接覆盖 status 与 _status_log；不产生新的 StatusChange，
        不校验转换合法性。

        Args:
            status: 恢复后的状态。
            status_log: 已持久化的状态变更日志（直接覆盖）。
        """
        self.status = status
        self._status_log = list(status_log)

    def is_terminal(self) -> bool:
        """是否处于终态（COMPLETED / FAILED / CANCELLED）。"""
        return self.status in _TERMINAL_STATUSES

    # ─────────────────────────────────────────────────────────────────
    # 运行时
    # ─────────────────────────────────────────────────────────────────

    def runtime_seconds(self) -> float | None:
        """返回任务已运行秒数（仅在 RUNNING 状态有效），否则返回 None。"""
        if self.status == TaskStatus.RUNNING and self.started_at is not None:
            return (datetime.now() - self.started_at).total_seconds()
        return None

    # ─────────────────────────────────────────────────────────────────
    # 状态信息（结构化）
    # ─────────────────────────────────────────────────────────────────

    def status_info(self) -> tuple[TaskStatus, datetime | None]:
        """返回 (状态, 关联时间戳) 的结构化信息。

        - RUNNING → (RUNNING, started_at)
        - WAITING_INPUT → (WAITING_INPUT, updated_at)
        - SCHEDULED → (SCHEDULED, scheduled_at)
        - 其他状态 → (status, None)
        """
        if self.status == TaskStatus.RUNNING:
            return (self.status, self.started_at)
        if self.status == TaskStatus.WAITING_INPUT:
            return (self.status, self.updated_at)
        if self.status == TaskStatus.SCHEDULED:
            return (self.status, self.scheduled_at)
        return (self.status, None)

    @property
    def reply_target(self) -> str:
        return self.reply_stream_id or self.stream_id

    # ─────────────────────────────────────────────────────────────────
    # 序列化
    # ─────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """将任务序列化为纯字典（全部值均为 JSON 可序列化类型）。"""
        return {
            "id": self.id,
            "title": self.title,
            "intent": self.intent,
            "level": self.level.value,
            "status": self.status.value,
            "owner": self.owner,
            "stream_id": self.stream_id,
            "platform": self.platform,
            "reply_stream_id": self.reply_stream_id,
            "trigger_type": self.trigger_type.value,
            "delay_seconds": self.delay_seconds,
            "cron_expr": self.cron_expr,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "priority": self.priority,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "max_runtime_min": self.max_runtime_min,
            "metadata": self.metadata,
            "_status_log": [e.to_dict() for e in self._status_log],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRecord:
        """从字典反序列化为 TaskRecord。

        状态与状态日志均直接恢复（覆盖赋值），不走状态机校验。
        """
        level_raw = data.get("level", TaskLevel.INSTANT.value)
        level: TaskLevel = TaskLevel(level_raw)

        record = cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            intent=data.get("intent", ""),
            level=level,
            status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
            owner=data.get("owner", ""),
            stream_id=data.get("stream_id", ""),
            platform=data.get("platform", ""),
            reply_stream_id=data.get("reply_stream_id"),
            trigger_type=TriggerType(data.get("trigger_type", TriggerType.NOW.value)),
            delay_seconds=data.get("delay_seconds"),
            cron_expr=data.get("cron_expr"),
            scheduled_at=_parse_datetime(data.get("scheduled_at")),
            started_at=_parse_datetime(data.get("started_at")),
            priority=data.get("priority", 0),
            created_at=_parse_datetime(data.get("created_at")) or datetime.now(),
            updated_at=_parse_datetime(data.get("updated_at")) or datetime.now(),
            max_runtime_min=data.get("max_runtime_min", 0),
            metadata=data.get("metadata", {}),
        )

        raw_log: list[dict[str, Any]] = data.get("_status_log", [])
        if raw_log:
            record._status_log = [StatusChange.from_dict(e) for e in raw_log]

        return record
