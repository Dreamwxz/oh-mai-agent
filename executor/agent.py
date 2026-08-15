"""Agent 执行器 —— AgentLoop.run() 的薄封装。

AgentExecutor 不修改 AgentLoop 内部逻辑，仅负责将
``_build_agent_loop`` 工厂注入的依赖打包，在 ``execute()`` 内创建
AgentLoop 实例并调用其 ``run()`` 方法。

Agent 是两级执行体系（instant / agent）中最重的执行器：每个 Agent 任务拥有完整的 Agent 循环
（LLM 推理 + 工具调用 + 多轮对话），可运行数分钟到数十分钟。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import Token
from typing import Any

from .agent_loop import AgentLoop
from .context import current_cancel_check, current_task, make_role_provider
from ..permission import Role
from ..domain.task_record import TaskRecord
from .base import ExecutionContext, ExecutionResult

logger = logging.getLogger(__name__)

class AgentExecutor:
    """执行 Agent 任务：创建并运行 AgentLoop。

    所有原本由 ``_build_agent_loop`` 注入的 AgentLoop 依赖在构造时接收，
    在 ``execute()`` 内部转发给 ``AgentLoop.__init__``。

    Agent 任务对应离线长时 Agent 循环 —— 有完整的 LLM 推理链路、
    工具注册表（registry）、用户提问回调（on_ask）、指令注入总线
    （command_bus）等。
    """

    def __init__(
        self,
        *,
        registry: Any,
        on_ask: Callable[[str, str], Any] | None = None,
        send_final: Callable[[TaskRecord, str], Any] | None = None,
        prompt_manager: Any | None = None,
        prompt_service: Any | None = None,
        command_bus: Any | None = None,
        resolver: Any | None = None,
    ) -> None:
        """创建 Agent 执行器。

        Args:
            registry: ToolRegistry —— Agent 可用的工具注册表。
            on_ask: ask_user 回调 —— Agent 执行中向用户提问时调用。
            send_final: 最终回复的回调 —— 润色并发送任务完成消息。
            prompt_manager: PromptManager —— Agent 系统提示词管理。
            prompt_service: PromptService —— builder 模式提示词构建。
            command_bus: 可选的 TaskCommandBus —— 用于指令注入/恢复等事件。
            resolver: 可选的 PermissionResolver —— 用于角色解析。
        """
        self._registry = registry
        self._on_ask = on_ask
        self._send_final = send_final
        self._prompt_manager = prompt_manager
        self._prompt_service = prompt_service
        self._command_bus = command_bus
        self._resolver = resolver

    def update_resolver(self, resolver: Any) -> None:
        """Replace the permission resolver used by the agent executor."""
        self._resolver = resolver

    async def execute(self, ctx: ExecutionContext, task: TaskRecord) -> ExecutionResult:
        """运行 Agent 循环。

        设置 ``_current_task`` 上下文变量，使工具代码能解析当前任务
        （与原来 ``_build_agent_loop`` 的行为一致）。

        任务的状态流转、持久化与调度器通知均由 ``AgentLoop.run()`` 内部完成，
        本方法仅返回执行结果摘要（COMPLETED / FAILED）。
        """
        # 将当前任务写入上下文变量；finally 中恢复，避免泄漏到其他任务。
        token = current_task.set(task)
        logger.info("开始执行 Agent 任务 %s（等级 %s）", task.id, task.level.value)
        cc_token: Token | None = None
        try:
            role_provider = self._make_role_provider(task)

            # 统一完成通知通道：任务进入终态后经 scheduler.on_task_completed
            # 直接调用（同步释放并发额度 + CRON 重排），不经过事件总线。
            scheduler = getattr(ctx, "scheduler", None)
            on_task_done = None
            if scheduler is not None and callable(getattr(scheduler, "on_task_completed", None)):
                on_task_done = scheduler.on_task_completed

            # 将构造时注入的依赖转发给 AgentLoop。
            loop = AgentLoop(
                ctx=ctx.ctx,
                registry=self._registry,
                store=ctx.store,
                on_ask=self._on_ask,
                role_provider=role_provider,
                send_final=self._send_final,
                on_task_done=on_task_done,
                prompt_manager=self._prompt_manager or ctx.prompt_manager,
                prompt_service=self._prompt_service or ctx.prompt_service,
                command_bus=self._command_bus,
            )
            # run() 内部完成状态流转（终态）与落库，并触发调度器通知；
            # 正常返回即任务已成功结束。
            # 将取消检查回调写入上下文变量，供子 Agent 循环读取主循环取消状态。
            cc_token = current_cancel_check.set(lambda: loop.is_cancelled)
            logger.info("AgentLoop 启动：任务 %s", task.id)
            await loop.run(task)
            logger.info("任务 %s 执行完成（AgentLoop 退出）", task.id)
            return ExecutionResult(status="COMPLETED", message="Agent done")
        except Exception as exc:
            # 兜底：run() 内部已捕获循环内异常并转入 FAILED，此分支覆盖
            # 构造阶段（role_provider / AgentLoop 构建）的异常。
            logger.exception("任务 %s 执行失败：%s", task.id, exc)
            return ExecutionResult(status="FAILED", message=str(exc), error=str(exc))
        finally:
            # 恢复上下文变量，避免泄漏到并发中的其他任务。
            current_task.reset(token)
            if cc_token is not None:
                current_cancel_check.reset(cc_token)

    # ── 辅助方法 ────────────────────────────────────────────────────────

    def _make_role_provider(
        self, task: TaskRecord,
    ) -> Callable[[], Role]:
        """为 *task* 构建角色解析回调。

        将注入的 PermissionResolver 交给共享角色 provider；未注入时回退为访客。
        """
        if self._resolver is None:
            return lambda: Role.GUEST
        return make_role_provider(self._resolver, task)
