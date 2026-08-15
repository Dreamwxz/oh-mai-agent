"""Control-plane use cases for task execution and user interaction.

职责范围：任务**执行**（instant 分发）、用户回复匹配、指令注入，以及
任务的**控制**操作（cancel / pause / resume）——控制三件套从 ``TaskCrud``
归位至此，与 CRUD 查询职责分离（见设计评审"名实错位"项）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ...bus import CommandKind, TaskCommand
from ...config import MaibotAgentConfig
from ...domain.stream_ref import Owner, is_group_stream, planner_owner, platform_of
from ...domain.task_record import TaskLevel, TaskRecord, TaskStatus, new_task_record
from ...domain.task_store import TaskStore
from ...executor import ExecutionContext, ExecutorFactory, make_exec_ctx
from ...permission import Role
from ...prompt.manager import PromptManager
from ..scheduler import TaskScheduler

logger = logging.getLogger(__name__)


class TaskControl:
    """Handle task execution dispatch, replies, instruction injection, and task control."""

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
        resolve_task: Callable[[str], Awaitable[tuple[bool, TaskRecord | str]]] | None = None,
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
        # 任务解析（完整 ID → 前缀 → 唯一标题）由 TaskCrud 提供；
        # TaskManager 构造时注入绑定方法（TaskControl 不重复实现解析逻辑）。
        self._resolve_task = resolve_task

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
            platform = platform_of(stream_id)
        full_owner = Owner.join(platform, user_id) if platform else ""
        # 群聊中由 Planner 工具创建的任务 owner 为 "planner:{stream_id}"（无单一
        # 委托用户，提问对象是整个群）；这类任务视作"群内任何人回复都有效"。
        # （tools/planner/task_tools.py:_planner_owner 的群聊语义即如此。）
        planner_owner_str = planner_owner(stream_id) if is_group_stream(stream_id) else None

        tasks = await self._store.list(
            status=TaskStatus.WAITING_INPUT,
            stream_id=stream_id,
            limit=50,
        )
        for task in tasks:
            owner_matched = (
                task.owner == full_owner
                or task.owner == planner_owner_str
                # 兜底：部分宿主的 session_id 是不带平台前缀的裸 UUID（如
                # MaiBot 的 "96957f3c-..."），此时拼不出 platform:user_id；
                # 同一 stream 内用户身份唯一，按 owner 后缀 ":user_id" 匹配
                # 仍可精确命中（平台已知时不做模糊匹配，full_owner 非空即排除）。
                or (not full_owner and task.owner.endswith(f":{user_id}"))
            )
            if not owner_matched:
                continue
            if task.owner != full_owner and task.owner != planner_owner_str:
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

    # ── 任务控制（cancel / pause / resume）──────────────────────────────────
    # 控制三件套自 TaskCrud 归位至此：权限校验 + 解析后委托调度器执行。

    async def _control_task(self, task_id: str, caller_role: Role, owner: str, action: str) -> tuple[bool, str]:
        if self._resolve_task is None:
            logger.error("TaskControl 未注入 resolve_task，无法执行 %s 操作", action)
            return False, f"{action} 失败：控制层未就绪"
        ok, resolved = await self._resolve_task(task_id)
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
