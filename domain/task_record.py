"""TaskRecord — 纯持久化数据模型。

TaskRecord 是一个 dataclass，将所有任务字段收纳在单个 JSON 可序列化的
字典中。运行时状态（队列、Event、AgentLoop 引用）一律放在 ``TaskRuntime``，不在此处。

枚举与工具函数直接定义在本文件中，不依赖外部模块。
任务记录定义在本文件末尾，用于保持现有代码兼容。
"""

from __future__ import annotations

import logging
import uuid
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
# metadata 契约 — 跨组件隐式协作键的唯一定义
# ═══════════════════════════════════════════════════════════════════════
# metadata 是 TaskRecord 唯一可落库的扩展槽；本段集中声明全部保留键的名称
# 与值类型，并统一经 TaskRecord 的类型化访问器读写。仓库其他位置不得再以
# 字符串字面量直接访问这些键——新增协作键必须先在此声明并补充访问器。
# 值类型约定：caller_role 存 Role.value 字符串；inject_queue 存 list[str]；
# user_reply 存 str；coop_paused / paused_by_stop / is_reply /
# recovered_from_running 存 bool；last_history_id 存 int；error 存 str。

META_CALLER_ROLE = "_caller_role"                       # str — 任务创建者角色（Role.value），执行期角色解析优先使用
META_INJECT_QUEUE = "_inject_queue"                     # list[str] — 待注入指令队列（INJECT_INSTRUCTION 命令追加，每轮 LLM 调用前消费）
META_USER_REPLY = "_user_reply"                         # str — ask_user 挂起期间收到的用户回复
META_COOP_PAUSED = "_coop_paused"                       # bool — 协作暂停标记（调度器与 AgentLoop 双方维护，暂停中跳过超时计时）
META_PAUSED_BY_STOP = "_paused_by_stop"                 # bool — 插件关闭时被暂停的标记（恢复机制仅记录）
META_IS_REPLY = "_is_reply"                             # bool — 回复投递任务标记（_dispatch_reply_instant 创建时设置）
META_LAST_HISTORY_ID = "_last_history_id"               # int — 持久化历史水位（append_history 返回的自增 id）
META_ERROR = "_error"                                   # str — 失败原因（发送失败消息时读取）
META_RECOVERED_FROM_RUNNING = "_recovered_from_running" # bool — 重启时由 RUNNING 降级重排的标记（区分“恢复重排”与“正常排队”）

# 已知键的期望值类型（from_dict 加载时校验告警用；不阻断反序列化）
_META_EXPECTED_TYPES: dict[str, type] = {
    META_CALLER_ROLE: str,
    META_INJECT_QUEUE: list,
    META_USER_REPLY: str,
    META_COOP_PAUSED: bool,
    META_PAUSED_BY_STOP: bool,
    META_IS_REPLY: bool,
    META_LAST_HISTORY_ID: int,
    META_ERROR: str,
    META_RECOVERED_FROM_RUNNING: bool,
}


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
    # metadata 类型化访问器（键定义见本文件顶部 META_* 常量）
    # ─────────────────────────────────────────────────────────────────
    # metadata 仍是自由扩展槽（可存放自定义键），但全部保留键必须经下方
    # 访问器读写：访问器负责键名收口、值类型归一与历史脏数据容错。

    # ── 注入指令队列 ──

    def push_injection(self, instruction: str) -> None:
        """追加一条待注入指令（INJECT_INSTRUCTION 命令处理时调用）。

        队列在 metadata 中以 ``list[str]`` 存储；历史脏数据非 list 时重置为空队列。
        """
        queue = self.metadata.get(META_INJECT_QUEUE)
        if not isinstance(queue, list):
            queue = []
            self.metadata[META_INJECT_QUEUE] = queue
        queue.append(instruction)

    def take_injections(self) -> list[str]:
        """弹出并返回全部待注入指令（每轮 LLM 调用前消费一次）。"""
        queue = self.metadata.pop(META_INJECT_QUEUE, [])
        if not isinstance(queue, list):
            logger.warning(
                "任务 %s：metadata[%s] 类型异常（期望 list，实际 %s），按空队列处理",
                self.id, META_INJECT_QUEUE, type(queue).__name__,
            )
            return []
        return [str(item) for item in queue]

    # ── 用户回复（ask_user 挂起唤醒）──

    def set_user_reply(self, reply: str) -> None:
        """记录 ask_user 挂起期间收到的用户回复（TaskControl 与 AgentLoop 双写）。"""
        self.metadata[META_USER_REPLY] = str(reply)

    def user_reply(self) -> str:
        """读取已记录的用户回复；无则返回空串（不消费）。"""
        value = self.metadata.get(META_USER_REPLY)
        return str(value) if value is not None else ""

    def take_user_reply(self) -> str:
        """弹出并返回用户回复；无则返回空串。"""
        value = self.metadata.pop(META_USER_REPLY, None)
        return str(value) if value is not None else ""

    # ── 协作暂停 ──

    def set_coop_paused(self, flag: bool = True) -> None:
        """设置/清除协作暂停标记（调度器 pause/stop 与 AgentLoop PAUSE 分支维护）。

        清除（flag=False）时移除键，保持持久化记录干净（与历史 ``pop`` 语义一致）。
        """
        if flag:
            self.metadata[META_COOP_PAUSED] = True
        else:
            self.metadata.pop(META_COOP_PAUSED, None)

    def is_coop_paused(self) -> bool:
        """是否处于协作暂停（暂停中的任务跳过超时检测，恢复需先清标记）。"""
        return bool(self.metadata.get(META_COOP_PAUSED, False))

    def mark_paused_by_stop(self) -> None:
        """标记任务在插件关闭时被暂停（恢复机制仅记录，不自动处理）。"""
        self.metadata[META_PAUSED_BY_STOP] = True

    def was_paused_by_stop(self) -> bool:
        """是否因插件关闭而被暂停。"""
        return bool(self.metadata.get(META_PAUSED_BY_STOP, False))

    # ── 回复任务标记 ──

    def mark_as_reply(self) -> None:
        """标记本任务为回复投递任务（_dispatch_reply_instant 创建时设置）。"""
        self.metadata[META_IS_REPLY] = True

    def is_reply_task(self) -> bool:
        """是否为回复投递任务（跨流回复的动机注释判断依赖此标记）。"""
        return bool(self.metadata.get(META_IS_REPLY, False))

    # ── 历史回放水位 ──

    def set_last_history_id(self, history_id: int) -> None:
        """记录持久化历史水位（append_history 返回的自增 id）。"""
        self.metadata[META_LAST_HISTORY_ID] = int(history_id)

    def last_history_id(self) -> int | None:
        """读取历史水位；无或非法时返回 None（bool 是 int 子类，防御性排除）。"""
        value = self.metadata.get(META_LAST_HISTORY_ID)
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            logger.warning(
                "任务 %s：metadata[%s] 非法（%r），按无水位处理",
                self.id, META_LAST_HISTORY_ID, value,
            )
            return None

    # ── 失败原因 ──

    def set_error(self, message: str) -> None:
        """记录失败原因（失败消息发送时读取）。"""
        self.metadata[META_ERROR] = str(message)

    def error(self) -> str | None:
        """读取失败原因；无则返回 None。"""
        value = self.metadata.get(META_ERROR)
        return str(value) if value is not None else None

    # ── 恢复标记 ──

    def mark_recovered_from_running(self) -> None:
        """标记任务由 RUNNING 在重启时降级重排（区分“恢复重排”与“正常排队”）。"""
        self.metadata[META_RECOVERED_FROM_RUNNING] = True

    def was_recovered_from_running(self) -> bool:
        """是否由 RUNNING 在重启时降级重排。"""
        return bool(self.metadata.get(META_RECOVERED_FROM_RUNNING, False))

    # ── 创建者角色 ──

    def set_caller_role(self, role: object) -> None:
        """记录任务创建者角色（存储 Role.value 字符串；执行期角色解析优先使用）。

        Args:
            role: Role 枚举或 Role.value 字符串；None 忽略。
        """
        value = getattr(role, "value", role)
        if value is None:
            return
        self.metadata[META_CALLER_ROLE] = str(value)

    def caller_role(self) -> str | None:
        """读取创建者角色（Role.value 字符串）；无则返回 None。"""
        value = self.metadata.get(META_CALLER_ROLE)
        return str(value) if value is not None else None

    # ── 加载校验 ──

    def _warn_metadata_types(self) -> None:
        """加载时校验已知 metadata 键的值类型，非法值仅告警不阻断（兼容历史脏数据）。

        由 ``from_dict`` 反序列化完成后调用；运行期经访问器读写不会再产生脏值。
        """
        for key, expected in _META_EXPECTED_TYPES.items():
            if key not in self.metadata:
                continue
            value = self.metadata[key]
            if value is None or isinstance(value, expected):
                continue
            logger.warning(
                "任务 %s：metadata[%s] 类型异常（期望 %s，实际 %s），相关功能可能失效",
                self.id, key, expected.__name__, type(value).__name__,
            )

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

        record._warn_metadata_types()

        return record


def new_task_record(
    *,
    title: str,
    intent: str,
    level: TaskLevel,
    owner: str,
    stream_id: str,
    platform: str,
    priority: int = 0,
    trigger: TriggerType = TriggerType.NOW,
    delay_seconds: int | None = None,
    cron_expr: str | None = None,
    reply_stream_id: str | None = None,
) -> TaskRecord:
    """构造新任务记录（PENDING + NOW 触发 + 新 uuid），供各创建路径复用。

    各创建入口（``TaskCrud.create_task``、回复任务分发等）经此构造器统一
    字段默认值，避免多处手工拼装导致新增字段时遗漏。
    """
    return TaskRecord(
        id=str(uuid.uuid4()),
        title=title,
        intent=intent,
        level=level,
        owner=owner,
        stream_id=stream_id,
        platform=platform,
        status=TaskStatus.PENDING,
        trigger_type=trigger,
        delay_seconds=delay_seconds,
        cron_expr=cron_expr,
        scheduled_at=None,
        priority=priority,
        reply_stream_id=reply_stream_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
