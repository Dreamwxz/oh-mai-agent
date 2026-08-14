"""Control-plane use cases for task execution and user interaction."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from ...bus import CommandKind, TaskCommand
from ...config import MaibotAgentConfig
from ...domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType
from ...domain.task_store import TaskStore
from ...executor import ExecutionContext, ExecutorFactory
from ...prompt.manager import PromptManager
from ..scheduler import TaskScheduler

logger = logging.getLogger(__name__)


class TaskControl:
    """Handle task execution dispatch, replies, and instruction injection."""

    def __init__(
        self,
        *,
        store: TaskStore,
        scheduler: TaskScheduler,
        command_bus: Any,
        executor_factory: ExecutorFactory,
        config: MaibotAgentConfig,
        prompt_manager: PromptManager | None,
        prompt_service: Any | None,
        ctx: Any,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._command_bus = command_bus
        self._executor_factory = executor_factory
        self._config = config
        self._prompt_manager = prompt_manager
        self._prompt_service = prompt_service
        self._ctx = ctx

    def update_config(self, config: MaibotAgentConfig) -> None:
        """Replace the configuration used when creating execution contexts."""
        self._config = config

    async def _dispatch_reply_instant(self, task: TaskRecord, text: str) -> None:
        """Persist and enqueue a reply as an instant task."""
        reply_task = TaskRecord(
            id=str(uuid.uuid4()),
            title=f"Reply: {task.title[:60]}",
            intent=text,
            level=TaskLevel.INSTANT,
            owner=task.owner,
            stream_id=task.reply_target,
            platform=task.platform,
            status=TaskStatus.PENDING,
            trigger_type=TriggerType.NOW,
            priority=task.priority,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        reply_task.mark_as_reply()
        await self._store.save(reply_task)
        await self._scheduler.enqueue(reply_task)
        logger.info(
            "回复 instant 任务 %s 已分发，原始任务 %s",
            reply_task.id,
            task.id,
        )

    async def execute_instant(self, task: TaskRecord) -> None:
        """Execute an instant task through the executor factory."""
        exec_ctx = ExecutionContext(
            ctx=self._ctx,
            store=self._store,
            scheduler=self._scheduler,
            config=self._config,
            prompt_manager=self._prompt_manager,
            prompt_service=self._prompt_service,
        )
        await self._executor_factory.get(TaskLevel.INSTANT).execute(exec_ctx, task)

    async def handle_user_reply(
        self,
        *,
        stream_id: str,
        user_id: str,
        reply: str,
    ) -> None:
        """Match a user reply to a waiting task and resume it."""
        platform = stream_id.split(":", 1)[0] if ":" in stream_id else ""
        full_owner = f"{platform}:{user_id}"

        tasks = await self._store.list(
            status=TaskStatus.WAITING_INPUT,
            stream_id=stream_id,
            limit=50,
        )
        for task in tasks:
            if task.owner != full_owner:
                continue
            fresh = await self._store.get(task.id)
            if fresh is None:
                continue
            if fresh.status != TaskStatus.WAITING_INPUT:
                continue
            fresh.set_user_reply(reply)
            await self._store.save(fresh)
            await self._command_bus.send(TaskCommand(
                task_id=fresh.id,
                kind=CommandKind.RESUME_REPLY,
                payload={"reply": reply},
            ))
            logger.info(
                "任务 %s 已收到来自 %s 的用户回复，已恢复执行",
                fresh.id,
                full_owner,
            )
            return

        logger.debug("没有匹配到等待回复的任务：stream=%s owner=%s", stream_id, full_owner)

    async def handle_injection(self, task_id: str, instruction: str) -> bool:
        """Forward an instruction to a running task through the command bus."""
        await self._command_bus.send(TaskCommand(
            task_id=task_id,
            kind=CommandKind.INJECT_INSTRUCTION,
            payload={"instruction": instruction},
        ))
        logger.info("Bus-INJECT 注入指令到任务 %s：%s", task_id, instruction[:80])
        return True
