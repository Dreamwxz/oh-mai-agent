"""Instant 执行器 —— 单步即时动作的执行收口。

Instant 任务是最简单的单步即时动作：不需要 LLM 推理、不需要工具调用、
不涉及状态机 —— 意图本身就是要发送的消息，经 ``ReplySender``
（``executor/sender.py``）润色发送后任务即完成。

发送基础设施（``ReplySender`` / ``PolishService`` / ``fail_task`` /
自动转达判定）是**全插件共用**的横切能力，已独立到 ``executor/sender.py``；
本模块只保留 ``InstantExecutor`` 本身。
"""

from __future__ import annotations

import logging

from ..domain.task_record import TaskRecord
from .base import ExecutionContext, ExecutionResult, complete_and_notify
from .sender import fail_task, resolve_auto_relay

logger = logging.getLogger(__name__)


# ── InstantExecutor ──────────────────────────────────────────────────────────


class InstantExecutor:
    """执行 Instant 任务：在当前进程中润色并发送。

    Instant 任务为简单的单步即时动作 —— 意图即消息，无 LLM 推理和工具调用。
    执行经 ``ReplySender.send_polished`` 完成，随后持久化完成状态并通知调度器。
    """

    async def execute(self, exec_ctx: ExecutionContext, task: TaskRecord) -> ExecutionResult:
        """润色并发送任务意图，然后完成或失败任务。"""
        try:
            sender = exec_ctx.sender
            if sender is None:
                raise RuntimeError("ExecutionContext 缺少 sender（ReplySender）")
            # 自动转达：回复目标（私聊）与任务发起人不同 → 润色点名委托人；
            # 本人发言 / 群目标 / 显式 relay_from 由 send_message 工具处理时不触发。
            relay_from = await resolve_auto_relay(exec_ctx.ctx, task)
            await sender.send_polished(
                task.intent, task.reply_target, relay_from=relay_from,
            )
            if task.reply_stream_id is not None or task.is_reply_task():
                # 跨流回复：动机 XML 注释（对用户不可见，写入 MaiBot 上下文）
                await sender.append_motivation_note(task.reply_target, task.intent)
            fresh_task = await exec_ctx.store.get(task.id)
            if fresh_task is None or not fresh_task.is_terminal():
                await complete_and_notify(task, exec_ctx.store, exec_ctx.scheduler)
            logger.info("Instant 任务 %s 执行成功完成", task.id)
            return ExecutionResult(status="COMPLETED", message="Instant done")
        except Exception as exc:
            logger.exception("Instant 任务 %s 执行失败", task.id)
            failure_task = task
            try:
                persisted_task = await exec_ctx.store.get(task.id)
            except Exception:
                persisted_task = None
            if persisted_task is not None:
                failure_task = persisted_task
            failure_task.set_error(str(exc))
            await fail_task(
                failure_task,
                exec_ctx.store,
                exec_ctx.scheduler,
                exec_ctx,
                send_message=True,
            )
            return ExecutionResult(status="FAILED", message=str(exc), error=str(exc))
