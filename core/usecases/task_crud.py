"""CRUD use cases for persisted tasks."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from ...config import MaibotAgentConfig
from ...domain.status_formatter import StatusFormatter
from ...domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType
from ...domain.task_store import TaskStore
from ...permission import PermissionResolver, Role
from ..scheduler import TaskScheduler

logger = logging.getLogger(__name__)


class TaskCrud:
    """Create, query, modify, and control persisted tasks."""

    def __init__(
        self,
        *,
        store: TaskStore,
        scheduler: TaskScheduler,
        resolver: PermissionResolver,
        sfmt: StatusFormatter,
        llm_title: Callable[[str], Awaitable[str]] | None,
        config: MaibotAgentConfig,
        inject_instruction: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._resolver = resolver
        self._sfmt = sfmt
        self._llm_title = llm_title
        self._config = config
        self._inject_instruction = inject_instruction

    def update_config(self, config: MaibotAgentConfig) -> None:
        """Replace the configuration used by CRUD operations."""
        self._config = config

    async def create_task(
        self,
        *,
        intent: str,
        owner: str,
        platform: str,
        stream_id: str,
        level: TaskLevel | None = None,
        trigger: TriggerType = TriggerType.NOW,
        delay_seconds: int | None = None,
        cron_expr: str | None = None,
        priority: int = 0,
        reply_stream_id: str | None = None,
        caller_role: Role,
    ) -> tuple[bool, TaskRecord | str]:
        """Create, persist, and enqueue a task."""
        if not PermissionResolver.require(caller_role, Role.USER):
            logger.info("创建任务被拒绝：guest 角色 %s 无法创建任务", owner)
            return False, "guest 无法创建任务"

        if level is None:
            # INSTANT 仅由定时任务与 Agent 内部显式创建（消息投递），
            # 用户/API 入口未指定级别时默认 agent。
            level = TaskLevel.AGENT
        logger.info("任务「%s」的级别：%s", intent[:60], level.value)

        if self._llm_title is not None:
            try:
                title = await self._llm_title(intent)
            except Exception:
                logger.warning("LLM 标题生成失败，使用截断意图作为标题")
                title = intent[:40]
        else:
            title = intent[:40]
        title = title.strip()[:80]

        task = TaskRecord(
            id=str(uuid.uuid4()), title=title, intent=intent, level=level,
            owner=owner, stream_id=stream_id, platform=platform,
            status=TaskStatus.PENDING, trigger_type=trigger,
            delay_seconds=delay_seconds, cron_expr=cron_expr,
            scheduled_at=None, priority=priority, reply_stream_id=reply_stream_id,
            created_at=datetime.now(), updated_at=datetime.now(),
        )
        # 持久化创建者角色：执行期角色解析（make_role_provider）在 owner 为
        # 会话 UUID 等无法映射平台/用户形态时无法还原创建者权限（会回落 guest，
        # 导致 MCP 等 user+ 工具不可见）。planner / API 创建者均为 ADMIN，
        # 任务理应以创建者角色执行。
        task.set_caller_role(caller_role)
        await self._store.save(task)
        await self._scheduler.enqueue(task)
        logger.info(
            "任务 %s 创建成功：title=%s level=%s owner=%s trigger=%s",
            task.id, task.title, task.level.value, task.owner, task.trigger_type.value,
        )
        return True, task

    async def list_tasks(
        self,
        *,
        caller_role: Role,
        owner: str,
        status: TaskStatus | None = None,
        stream_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return task summaries, newest first."""
        owner_filter = owner if caller_role != Role.ADMIN else (owner if owner else None)
        logger.debug(
            "查询任务列表：role=%s owner=%s status=%s stream_id=%s limit=%d",
            caller_role.value, owner_filter, status.value if status else None, stream_id, limit,
        )
        tasks = await self._store.list(
            status=status, owner=owner_filter, stream_id=stream_id, limit=limit,
        )
        return [
            {
                "id": task.id, "title": task.title, "level": task.level.value,
                "status": task.status.value,
                "format_status": self._sfmt.format(*task.status_info()),
                "owner": task.owner, "created_at": task.created_at.isoformat(),
            }
            for task in tasks
        ]

    async def _resolve_task_by_id(self, task_id: str) -> tuple[bool, TaskRecord | str]:
        """Resolve an exact task ID or a unique ID prefix."""
        task = await self._store.get(task_id)
        if task is not None:
            return True, task
        candidates = await self._store.get_by_prefix(task_id)
        if not candidates:
            return False, "任务不存在"
        if len(candidates) > 1:
            ids = ", ".join(candidate.id[:8] for candidate in candidates)
            return False, f"ID 前缀匹配到多个任务: {ids}，请使用完整 ID"
        return True, candidates[0]

    async def get_task(
        self, task_id: str, *, caller_role: Role, owner: str,
    ) -> tuple[bool, TaskRecord | str]:
        """Return a task if the caller may view it."""
        logger.debug("查询任务详情：task_id=%s role=%s owner=%s", task_id, caller_role.value, owner)
        ok, resolved = await self._resolve_task_by_id(task_id)
        if not ok:
            return False, resolved
        if caller_role != Role.ADMIN and resolved.owner != owner:
            logger.info("拒绝查看任务 %s：%s 无权访问（owner=%s）", resolved.id, owner, resolved.owner)
            return False, "权限不足：只能查看自己的任务"
        return True, resolved

    async def modify_task(
        self,
        task_id: str,
        *,
        caller_role: Role,
        owner: str,
        new_intent: str | None = None,
        inject_instruction: str | None = None,
        priority: int | None = None,
    ) -> tuple[bool, str]:
        """Modify intent, inject an instruction, or change priority."""
        ok, resolved = await self._resolve_task_by_id(task_id)
        if not ok:
            return False, resolved
        if caller_role != Role.ADMIN and resolved.owner != owner:
            return False, "权限不足：只能修改自己的任务"
        if inject_instruction and caller_role != Role.ADMIN:
            return False, "权限不足：仅管理员可注入指令"
        modified = False
        if inject_instruction:
            if resolved.status not in (TaskStatus.RUNNING, TaskStatus.WAITING_INPUT):
                return False, f"任务当前状态为「{self._sfmt.format(*resolved.status_info())}」，无法注入指令（仅 running/waiting_input 有效）"
            if self._inject_instruction is not None:
                await self._inject_instruction(resolved.id, inject_instruction)
            modified = True
        if new_intent:
            resolved.intent = new_intent
            if self._llm_title is not None:
                try:
                    resolved.title = await self._llm_title(new_intent)
                except Exception:
                    resolved.title = new_intent[:40]
            else:
                resolved.title = new_intent[:40]
            resolved.title = resolved.title.strip()[:80]
            resolved.updated_at = datetime.now()
            modified = True
        if priority is not None:
            resolved.priority = priority
            resolved.updated_at = datetime.now()
            modified = True
        if modified:
            await self._store.save(resolved)
            logger.info("任务 %s 已修改 (intent=%s inject=%s pri=%s)", resolved.id, bool(new_intent), bool(inject_instruction), priority)
        return True, "修改成功" if modified else "无变更"

    async def _control_task(self, task_id: str, caller_role: Role, owner: str, action: str) -> tuple[bool, str]:
        ok, resolved = await self._resolve_task_by_id(task_id)
        if not ok:
            return False, resolved
        if caller_role != Role.ADMIN and resolved.owner != owner:
            permissions = {
                "cancel": "权限不足：只能取消自己的任务",
                "pause": "权限不足：只能暂停自己的任务",
                "resume": "权限不足：只能恢复自己的任务",
            }
            return False, permissions[action]
        method = getattr(self._scheduler, action)
        if await method(resolved.id):
            logger.info("任务 %s 已被 %s %s", resolved.id, owner, action)
            messages = {"cancel": f"任务 {resolved.id[:8]} 已取消", "pause": "已暂停", "resume": "已恢复"}
            return True, messages[action]
        failures = {"cancel": "取消失败（任务可能已处于终态）", "pause": "暂停失败（任务可能已处于终态或非 RUNNING）", "resume": "恢复失败（任务可能已处于终态或非 PAUSED）"}
        return False, failures[action]

    async def cancel_task(self, task_id: str, *, caller_role: Role, owner: str) -> tuple[bool, str]:
        """Cancel a task owned by the caller or accessible to an admin."""
        return await self._control_task(task_id, caller_role, owner, "cancel")

    async def pause_task(self, task_id: str, *, caller_role: Role, owner: str) -> tuple[bool, str]:
        """Pause a task owned by the caller or accessible to an admin."""
        return await self._control_task(task_id, caller_role, owner, "pause")

    async def resume_task(self, task_id: str, *, caller_role: Role, owner: str) -> tuple[bool, str]:
        """Resume a task owned by the caller or accessible to an admin."""
        return await self._control_task(task_id, caller_role, owner, "resume")

    async def task_history(
        self, task_id: str, *, caller_role: Role, owner: str, limit: int = 50,
    ) -> tuple[bool, list | str]:
        """Return execution history if the caller may view the task."""
        ok, resolved = await self._resolve_task_by_id(task_id)
        if not ok:
            return False, resolved
        if caller_role != Role.ADMIN and resolved.owner != owner:
            logger.info("拒绝查看任务 %s 历史：%s 无权访问（owner=%s）", resolved.id, owner, resolved.owner)
            return False, "权限不足：只能查看自己任务的历史"
        return True, await self._store.get_history(resolved.id, limit=limit)
