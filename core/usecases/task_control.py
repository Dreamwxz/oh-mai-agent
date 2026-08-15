"""Control-plane use cases for task execution and user interaction."""

from __future__ import annotations

import logging
from typing import Any

from ...bus import CommandKind, TaskCommand
from ...config import MaibotAgentConfig
from ...domain.task_record import TaskLevel, TaskRecord, TaskStatus, new_task_record
from ...domain.task_store import TaskStore
from ...executor import ExecutionContext, ExecutorFactory, make_exec_ctx
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
        sender: Any = None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._command_bus = command_bus
        self._executor_factory = executor_factory
        self._config = config
        self._prompt_manager = prompt_manager
        self._prompt_service = prompt_service
        self._ctx = ctx
        self._sender = sender

    def update_config(self, config: MaibotAgentConfig) -> None:
        """Replace the configuration used when creating execution contexts."""
        self._config = config

    async def dispatch_reply_instant(self, task: TaskRecord, text: str) -> None:
        """Persist and enqueue a reply as an instant task."""
        reply_task = new_task_record(
            title=f"Reply: {task.title[:60]}",
            intent=text,
            level=TaskLevel.INSTANT,
            owner=task.owner,
            stream_id=task.reply_target,
            platform=task.platform,
            priority=task.priority,
        )
        reply_task.mark_as_reply()
        await self._store.save(reply_task)
        enqueued = await self._scheduler.enqueue(reply_task)
        if enqueued is False:
            logger.error("回复 instant 任务 %s 入队失败（已保存），原始任务 %s 的回复将不发送", reply_task.id, task.id)
            return
        logger.info(
            "回复 instant 任务 %s 已分发，原始任务 %s",
            reply_task.id,
            task.id,
        )

    async def execute_instant(self, task: TaskRecord) -> None:
        """Execute an instant task through the executor factory."""
        exec_ctx = make_exec_ctx(
            ctx=self._ctx,
            store=self._store,
            scheduler=self._scheduler,
            config=self._config,
            prompt_manager=self._prompt_manager,
            prompt_service=self._prompt_service,
            sender=self._sender,
        )
        await self._executor_factory.get(TaskLevel.INSTANT).execute(exec_ctx, task)

    async def handle_user_reply(
        self,
        *,
        stream_id: str,
        user_id: str,
        reply: str,
        platform: str | None = None,
    ) -> None:
        """Match a user reply to a waiting task and resume it."""
        # 平台优先取调用方显式传入（MaiBot 消息 dict 自带 platform 字段）；
        # 未传入时退化为从 stream_id 前缀推断（如 "qq:1591625223"）。
        if not platform:
            platform = stream_id.split(":", 1)[0] if ":" in stream_id else ""
        full_owner = f"{platform}:{user_id}" if platform else ""
        # 群聊中由 Planner 工具创建的任务 owner 为 "planner:{stream_id}"（无单一
        # 委托用户，提问对象是整个群）；这类任务视作"群内任何人回复都有效"。
        # （tools/planner/task_tools.py:_planner_owner 的群聊语义即如此。）
        planner_owner = f"planner:{stream_id}" if ":group:" in stream_id else None

        tasks = await self._store.list(
            status=TaskStatus.WAITING_INPUT,
            stream_id=stream_id,
            limit=50,
        )
        for task in tasks:
            owner_matched = (
                task.owner == full_owner
                or task.owner == planner_owner
                # 兜底：部分宿主的 session_id 是不带平台前缀的裸 UUID（如
                # MaiBot 的 "96957f3c-..."），此时拼不出 platform:user_id；
                # 同一 stream 内用户身份唯一，按 owner 后缀 ":user_id" 匹配
                # 仍可精确命中（平台已知时不做模糊匹配，full_owner 非空即排除）。
                or (not full_owner and task.owner.endswith(f":{user_id}"))
            )
            if not owner_matched:
                continue
            if task.owner != full_owner and task.owner != planner_owner:
                logger.debug(
                    "用户回复按 owner 后缀匹配：task=%s owner=%s user_id=%s",
                    task.id, task.owner, user_id,
                )
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
