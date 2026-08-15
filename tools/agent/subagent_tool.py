"""子 Agent 工具 schema 与工具集规则 — ask_subagent / ask_subagents。

本模块只承担**工具域**职责，不含任何执行逻辑：

- ``resolve_toolset``：子 Agent 默认允许集 / 显式子集校验（工具语义唯一实现，
  供 ``AgentLoop`` 合成分支与测试共用）；
- ``build_subagent_tool`` / ``build_subagents_tool``：两个 Discoverable 工具的
  静态 schema 定义（LLM 可见接口，构建零参数、不依赖运行时句柄）。

**执行不在本层**。「子 Agent 也是 Agent」——派发由 ``AgentLoop`` 的合成工具
分支内建执行（``executor/agent_loop.py`` 的 ``_run_subagent`` / ``_run_subagents``），
执行引擎（``SubAgentLoop``）属于 Agent 域（executor 层）。本层工具 handler
只是 ``registry.execute`` 直调路径的守卫：正常路径在 AgentLoop 分发处被拦截，
handler 不会被调用；守卫保证任何绕过分发器的直调得到明确错误而非静默行为。

依赖方向：本模块不 import executor（``executor → tools`` 单向成立）。
"""

from __future__ import annotations

import logging
from typing import Any

from ...permission import Role
from ..registry import ToolDefinition

logger = logging.getLogger(__name__)

# 子 Agent 不可见的工具精确名集合（防绕过：不能发消息、反问用户、管理任务、
# 跨插件 API、再派生子 Agent 或执行宿主机命令）。
_SUBAGENT_EXCLUDED = frozenset({
    "ask_user",
    "send_message",
    "list_my_tasks",
    "create_subtask",
    "inject_task",
    "ask_subagent",
    "ask_subagents",
    "run_command",
})


def resolve_toolset(
    registry, role: Role, requested: list[str] | str | None
) -> list[ToolDefinition]:
    """解析子 Agent 的可用工具集（严格校验，绝不静默过滤）。

    Args:
        registry: ToolRegistry 实例。
        role: 当前任务角色，用于按角色过滤工具。
        requested: 调用方显式指定的工具名列表；空/None → 默认允许集。

    Returns:
        默认允许集（或请求的合法子集）的 ``ToolDefinition`` 列表，保持注册顺序。

    Raises:
        ValueError: 请求的工具名不在默认允许集内（非法名整体拒绝）。

    供 ``AgentLoop`` 合成分支（``executor/agent_loop.py``）与工具语义测试共用；
    执行方必须处理 ``ValueError`` 并向调用方返回错误，绝不静默过滤。
    """
    allowed = [
        t
        for t in registry.list_definitions(role)
        if t.name not in _SUBAGENT_EXCLUDED and not t.name.startswith("call_")
    ]
    if isinstance(requested, str):
        # 容错：LLM 可能把单个工具名传成字符串而非列表。
        requested = [requested] if requested else []
    if not requested:
        return allowed
    allowed_names = {t.name for t in allowed}
    invalid = [name for name in requested if name not in allowed_names]
    if invalid:
        raise ValueError(f"invalid tools: {', '.join(invalid)}")
    requested_names = set(requested)
    return [t for t in allowed if t.name in requested_names]


def _guard_handler(tool_name: str):
    """registry.execute 直调路径的守卫 handler。

    正常路径（主 Agent 循环）在 ``AgentLoop.run`` 的工具分发处拦截
    ``ask_subagent`` / ``ask_subagents``，不会走到本守卫；本函数仅在
    直接 ``registry.execute`` 直调（绕过 AgentLoop）时触发，返回明确错误。
    """

    async def _handler(**kwargs: Any) -> dict:
        logger.warning(
            "%s 被 registry.execute 直调：该工具由 AgentLoop 合成工具分支内建执行，不支持直接调用",
            tool_name,
        )
        return {
            "success": False,
            "error": (
                f"{tool_name} 由 Agent 引擎内建执行（AgentLoop 合成分支），"
                "不支持直接调用"
            ),
        }

    return _handler


def build_subagent_tool() -> ToolDefinition:
    """构建 ``ask_subagent`` 工具 schema（单派发；执行在 AgentLoop 合成分支）。

    schema 为纯静态定义：构建不依赖 ctx / registry / 配置——LLM 可见接口
    （名称、描述、参数、可见性、角色门槛）与执行细节完全解耦。
    """
    description = (
        "派发一个子 Agent 独立完成局部工作（最多 10 轮），结果作为工具结果返回。"
        "子 Agent 只能使用信息检索、文件读写与 MCP 工具，"
        "不能发消息、反问用户、创建任务、调用跨插件 API 或再派生子 Agent。"
        "适合把「去搜 X、查 Y、读/写文件 Z」这类局部工作交给子 Agent 并行完成。"
    )

    return ToolDefinition(
        name="ask_subagent",
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "要交给子 Agent 完成的局部工作意图描述",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "子 Agent 可用工具名列表（可选，必须是默认允许集的子集；"
                        "缺省/为空 → 默认允许集）"
                    ),
                },
            },
            "required": ["intent"],
        },
        handler=_guard_handler("ask_subagent"),
        visibility="discoverable",
        min_role=Role.USER,
    )


def build_subagents_tool() -> ToolDefinition:
    """构建 ``ask_subagents`` 工具 schema（批量并行；执行在 AgentLoop 合成分支）。

    参数 ``intents`` 必填、非空、长度 ≤ ``config.max_parallel_subagents``，
    超限/为空整体拒绝；``tools`` 语义同单个工具，所有子 Agent 共享同一工具集
    （不支持按 intent 单独指定）。校验与执行均在 AgentLoop 合成分支完成。
    """
    description = (
        "一次并行派发多个子 Agent（上限 max_parallel_subagents，默认 3），"
        "各自独立完成后合并答案返回。所有子 Agent 共享同一工具集"
        "（信息检索、文件读写与 MCP 工具，不能发消息/反问/建任务/递归）。"
        "适合同一轮并行多路搜索后由主 Agent 统一判断。"
    )

    return ToolDefinition(
        name="ask_subagents",
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "intents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "要并行派发的子 Agent 意图列表（非空，"
                        "数量不超过 max_parallel_subagents，默认 3）"
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "所有子 Agent 共享的工具名列表（可选，必须是默认允许集的"
                        "子集；缺省/为空 → 默认允许集）"
                    ),
                },
            },
            "required": ["intents"],
        },
        handler=_guard_handler("ask_subagents"),
        visibility="discoverable",
        min_role=Role.USER,
    )
