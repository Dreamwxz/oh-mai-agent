"""MaiBot Agent 执行引擎包 —— 按任务等级分发的执行策略。

将 instant/agent 执行逻辑组织为独立的两级执行器，
通过 ``TaskExecutor`` 协议统一接口。executor/ 包是"执行引擎策略"层，
不依赖插件实例，所有外部依赖通过 ExecutionContext 注入。
"""

from .base import ExecutionContext, ExecutionResult, TaskExecutor, make_exec_ctx
from .factory import ExecutorFactory
from .instant import InstantExecutor
from .agent import AgentExecutor

__all__ = [
    "ExecutionContext",
    "ExecutionResult",
    "ExecutorFactory",
    "InstantExecutor",
    "AgentExecutor",
    "TaskExecutor",
    "make_exec_ctx",
]
