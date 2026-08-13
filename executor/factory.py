"""执行器工厂 —— 将 TaskLevel 映射到具体 TaskExecutor 实现。

TaskManager 持有单一的 ExecutorFactory 实例。调用方通过任务等级
（TaskLevel.INSTANT / AGENT）获取对应的执行器，获得统一的 ``TaskExecutor``
协议接口。

设计要点：
  - instant 执行器是无状态的，在构造时直接创建，通过工厂 dict 复用。
  - 所有执行器依赖（ToolRegistry、回调、PromptManager 等）在工厂
    构造时注入，调用方无需了解内部细节。
"""

from __future__ import annotations

import logging
from typing import Any

from ..domain.task_record import TaskLevel
from .base import TaskExecutor
from .instant import InstantExecutor
from .agent import AgentExecutor

logger = logging.getLogger(__name__)


class ExecutorFactory:
    """将 ``TaskLevel`` 映射到 ``TaskExecutor``。

    用法::

        factory = ExecutorFactory(
            registry=reg, on_ask=ask_cb,
        )
        executor = factory.get(TaskLevel.INSTANT)
        result = await executor.execute(ctx, task)
    """

    def __init__(
        self,
        *,
        registry: Any | None = None,
        on_ask: Any | None = None,
        send_final: Any | None = None,
        prompt_manager: Any | None = None,
        prompt_service: Any | None = None,
        command_bus: Any | None = None,
        resolver: Any | None = None,
    ) -> None:
        """创建工厂，注入所有执行器需要的依赖。

        Args:
            registry: ToolRegistry（仅 agent 使用）。
            on_ask: ask_user 回调（仅 agent 使用）。
            send_final: send_final 回调（仅 agent 使用）。
            prompt_manager: PromptManager 实例。
            prompt_service: PromptService 实例。
            command_bus: 可选的 TaskCommandBus，用于指令注入/恢复事件。
            resolver: 可选的 PermissionResolver，用于 Agent 任务角色解析。
        """
        # 等级 → 执行器映射表：构造时一次性创建，之后 get() 直接查表复用。
        # instant 无状态、零依赖，直接实例化；agent 的全部依赖在此注入。
        self._executors: dict[TaskLevel, TaskExecutor] = {
            TaskLevel.INSTANT: InstantExecutor(),
            TaskLevel.AGENT: AgentExecutor(
                registry=registry,
                on_ask=on_ask,
                send_final=send_final,
                prompt_manager=prompt_manager,
                prompt_service=prompt_service,
                command_bus=command_bus,
                resolver=resolver,
            ),
        }

    def get(self, level: TaskLevel) -> TaskExecutor:
        """返回与 *level* 对应的执行器。

        Raises:
            KeyError: 若该等级未注册执行器。
        """
        # 未注册的等级会抛出 KeyError，由调用方兜底处理。
        logger.debug("获取任务等级 %s 对应的执行器", level)
        try:
            executor = self._executors[level]
        except KeyError:
            logger.error("未知任务等级 %s，未注册对应执行器", level)
            raise
        logger.info("任务等级 %s 选择执行器 %s", level, type(executor).__name__)
        return executor
