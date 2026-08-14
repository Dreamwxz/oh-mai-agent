"""执行器基础类型 —— TaskExecutor 协议、ExecutionContext 依赖注入束、ExecutionResult。

定义所有执行器共用的契约。工厂按 ``TaskLevel`` 将请求路由到对应实现。
ExecutionContext 是依赖注入的核心载体；执行器不直接 import 插件实例，
所有外部依赖（LLM、存储、调度器、配置、Prompt 管理器）都通过 ctx 注入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..domain.task_record import TaskRecord, TaskStatus, TaskStatusError

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 协议
# ═══════════════════════════════════════════════════════════════════════════


@runtime_checkable
class TaskExecutor(Protocol):
    """执行契约：每个等级只需实现一个 ``execute`` 入口。

    instant（即时）、agent（循环）两个等级都遵循此协议，
    工厂可返回统一的接口。
    """

    async def execute(self, ctx: ExecutionContext, record: TaskRecord) -> ExecutionResult:
        """执行任务并返回结构化结果。

        Args:
            ctx: 所有外部依赖（LLM、存储、调度器等），通过依赖注入传入。
            record: 待执行的任务。

        Returns:
            描述执行结果的 ``ExecutionResult``。
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════
# 上下文与结果数据类
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ExecutionContext:
    """依赖注入束，传入每个执行器的 ``execute()`` 方法。

    设计意图：执行器不依赖插件实例 —— 它们所需的一切都通过此束传入。
    这为未来将 executor 放入子进程运行提供了可能（所有依赖可序列化传递）。

    ``ctx`` 提供 PluginContext（llm.generate、send.text 等）。
    ``scheduler`` 仅用于 ``on_task_completed`` 通知（调度器负责后续调度决策）。
    """

    ctx: Any
    """MaiBot PluginContext —— llm.generate、send.text 等。"""

    store: Any
    """TaskStore —— ``save(task)``、``get(task_id)`` 等持久化操作。"""

    scheduler: Any
    """TaskScheduler —— ``on_task_completed(task)`` 通知（非调度决策，仅通知）。"""

    config: Any
    """MaibotAgentConfig —— ``task``、``polish`` 等配置子节。"""

    prompt_manager: Any | None = None
    """PromptManager —— 润色 / Agent 系统提示词管理。"""

    prompt_service: Any | None = None
    """PromptService —— builder 模式提示词构建。"""

    sender: Any | None = None
    """ReplySender —— 统一发送出口（send_raw / send_polished / 上下文注释）。"""


@dataclass(slots=True)
class ExecutionResult:
    """单次任务执行的结构化结果。"""

    status: str = "COMPLETED"
    """``"COMPLETED"``（默认）或 ``"FAILED"``。"""

    message: str = ""
    """人类可读的结果描述（失败场景下随失败消息发送给用户）。"""

    error: str | None = None
    """当 status 为 ``"FAILED"`` 时的异常信息（成功时为 None）。"""


# ═══════════════════════════════════════════════════════════════════════════
# 共享辅助函数 —— instant 执行器完成时的收尾逻辑（标记 COMPLETED → 落库 → 通知调度器）。
# （agent 在 AgentLoop.run() 内部有自己的终止逻辑。）
# ═══════════════════════════════════════════════════════════════════════════


def _complete_task(task: TaskRecord) -> None:
    """将 *task* 状态转换为 COMPLETED；状态竞争时用 ``force`` 兜底。"""
    if task.is_terminal():
        return
    try:
        task.transition(TaskStatus.COMPLETED)
    except TaskStatusError:
        if not task.is_terminal():
            task.force(TaskStatus.COMPLETED, actor="executor", reason="complete_task_fallback")
            logger.debug("任务 %s 状态机拒绝迁移，已用 force 兜底置为 COMPLETED", task.id)
    task.updated_at = datetime.now()


async def complete_and_notify(task: TaskRecord, store: Any, scheduler: Any) -> None:
    """标记任务为 COMPLETED，持久化，并通知调度器。"""
    _complete_task(task)
    await store.save(task)
    await scheduler.on_task_completed(task)
    logger.debug("任务 %s 已完成并通知调度器", task.id)
