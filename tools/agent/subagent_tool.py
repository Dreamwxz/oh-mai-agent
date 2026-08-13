"""子 Agent 工具 — ask_subagent / ask_subagents（批量并行）。

本模块提供两个 Discoverable 级、USER 可访问的工具：

- ``ask_subagent``：把「去搜 X、查 Y、读/写文件 Z」这类局部工作派给一个
  子 Agent 独立完成（进程内嵌套循环，结果直接回到主 Agent 上下文）。
- ``ask_subagents``：一次并行派发多个子 Agent（上限
  ``config.subagent.max_parallel_subagents``，默认 3），全部返回后合并答案。

工具集规则（默认允许集）：当前角色可见的全部工具，排除精确名
``ask_user`` / ``send_message`` / ``list_my_tasks`` / ``create_subtask`` /
``inject_task`` / ``ask_subagent`` / ``ask_subagents`` / ``list_plugin_tools``
与 ``call_`` 前缀（跨插件 API 工具）；MCP 工具（``mcp_`` 前缀）包含。

``tools`` 参数可选：缺省/为空 → 默认允许集；传入时必须为默认允许集的子集，
任一非法名 → 整体拒绝（绝不静默过滤）。

配置热更新：``config_getter`` 为每次调用读取的 lambda，闭包内绝不缓存配置
对象快照——``max_rounds`` / ``max_result_chars`` / ``max_parallel_subagents``
修改后立即生效。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...executor.context import current_cancel_check
from ...executor.subagent import SubAgentLoop
from ...permission import Role
from ..registry import ToolDefinition

if TYPE_CHECKING:
    from ...config import SubAgentConfig

logger = logging.getLogger(__name__)

# 子 Agent 不可见的工具精确名集合（防绕过：不能发消息、反问用户、管理任务、
# 跨插件 API、再派生子 Agent 或经 list_plugin_tools 动态发现）。
_SUBAGENT_EXCLUDED = frozenset({
    "ask_user",
    "send_message",
    "list_my_tasks",
    "create_subtask",
    "inject_task",
    "ask_subagent",
    "ask_subagents",
    "list_plugin_tools",
})


def _resolve_toolset(
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


def _make_runner(
    ctx,
    registry,
    prompt_service,
    config_getter: Callable[[], "SubAgentConfig"],
    role: Role,
    requested: list[str] | str | None = None,
) -> Callable[[str], Awaitable[dict]]:
    """构造单次派发执行器 ``_run_one(intent)``。

    ``_run_one`` 每次调用都读取 ``cfg = config_getter()``（闭包不缓存配置
    对象快照，热更新立即生效），并以当前 ``current_cancel_check`` 上下文变量
    作为取消守卫；工具集经 ``_resolve_toolset`` 按 *requested* 实时解析。
    """

    async def _run_one(intent: str) -> dict:
        cfg = config_getter()  # 每次调用读取，热更新立即生效

        async def _exec(name: str, role_: Role, args: dict) -> dict:
            # registry.execute 是 **kwargs 签名，必须解包 dict 再透传。
            return await registry.execute(name, role_, **args)

        try:
            defs = _resolve_toolset(registry, role, requested)
            loop = SubAgentLoop(
                ctx,
                tools=defs,
                role=role,
                prompt_service=prompt_service,
                max_rounds=cfg.max_rounds,
                max_result_chars=cfg.max_result_chars,
                should_cancel=current_cancel_check.get(),
                execute_tool=_exec,
            )
            return await loop.run(intent)
        except Exception as exc:
            # 兜底：_run_one 永不抛异常（gather 侧无需 return_exceptions），
            # 所有返回路径都携带完整 5 键。
            logger.exception("ask_subagent 子循环异常：%s", str(exc)[:200])
            return {
                "success": False,
                "answer": "",
                "rounds": 0,
                "max_rounds_reached": False,
                "error": str(exc),
            }

    return _run_one


def build_subagent_tool(
    ctx,
    registry,
    prompt_service,
    config_getter: Callable[[], "SubAgentConfig"],
    role_provider: Callable[[], Role],
) -> ToolDefinition:
    """构建 ``ask_subagent`` 工具（单派发）。

    Args:
        ctx: 插件上下文（透传给 SubAgentLoop 供 LLM 调用）。
        registry: ToolRegistry，用于解析默认允许集与执行工具。
        prompt_service: PromptService，渲染子 Agent 系统提示。
        config_getter: ``() -> SubAgentConfig``，每次调用读取（热更新生效）。
        role_provider: ``() -> Role``，解析当前任务的调用者角色。
    """

    async def _handler(**kwargs: Any) -> dict:
        intent: str = kwargs.get("intent", "")
        if not intent:
            logger.warning("ask_subagent 参数校验失败：缺少意图 intent")
            return {"success": False, "error": "缺少必需参数: intent"}

        requested = kwargs.get("tools")
        role = role_provider()
        try:
            # 严格校验：任一非法名 → 整体拒绝，绝不静默过滤。
            _resolve_toolset(registry, role, requested)
        except ValueError as exc:
            logger.warning("ask_subagent 工具集校验失败：%s", exc)
            return {"success": False, "error": str(exc)}

        logger.info("ask_subagent 派发：意图 %.80r，角色 %s", intent, role.value)
        runner = _make_runner(
            ctx, registry, prompt_service, config_getter, role, requested=requested
        )
        # 原样透传 SubAgentLoop 结果 dict（含 max_rounds_reached）。
        return await runner(intent)

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
        handler=_handler,
        visibility="discoverable",
        min_role=Role.USER,
    )


def build_subagents_tool(
    ctx,
    registry,
    prompt_service,
    config_getter: Callable[[], "SubAgentConfig"],
    role_provider: Callable[[], Role],
) -> ToolDefinition:
    """构建 ``ask_subagents`` 工具（批量并行派发）。

    参数 ``intents`` 必填、非空、长度 ≤ ``config_getter().max_parallel_subagents``，
    超限/为空整体拒绝；``tools`` 语义同单个工具，所有子 Agent 共享同一工具集
    （不支持按 intent 单独指定）。
    """

    async def _handler(**kwargs: Any) -> dict:
        intents: Any = kwargs.get("intents", [])
        if isinstance(intents, str):
            # 容错：LLM 可能把单个意图传成字符串。
            intents = [intents] if intents else []
        if not intents:
            logger.warning("ask_subagents 参数校验失败：intents 为空")
            return {"success": False, "error": "intents 不能为空"}

        cfg = config_getter()  # 每次调用读取，热更新立即生效
        if len(intents) > cfg.max_parallel_subagents:
            logger.warning(
                "ask_subagents 参数校验失败：intents 数量 %d 超限（上限 %d）",
                len(intents),
                cfg.max_parallel_subagents,
            )
            return {
                "success": False,
                "error": f"intents 数量超限: {len(intents)}（上限 {cfg.max_parallel_subagents}）",
            }

        requested = kwargs.get("tools")
        role = role_provider()
        try:
            # 严格校验：任一非法名 → 整体拒绝，绝不静默过滤。
            _resolve_toolset(registry, role, requested)
        except ValueError as exc:
            logger.warning("ask_subagents 工具集校验失败：%s", exc)
            return {"success": False, "error": str(exc)}

        logger.info(
            "ask_subagents 批量派发：%d 个意图，角色 %s",
            len(intents),
            role.value,
        )
        runner = _make_runner(
            ctx, registry, prompt_service, config_getter, role, requested=requested
        )
        # 每个 _run_one 为独立 SubAgentLoop 实例；gather 不抛异常。
        results = await asyncio.gather(*(runner(i) for i in intents))

        failed = [r["error"] for r in results if not r["success"] and r.get("error")]
        return {
            "success": all(r["success"] for r in results),
            "answers": [
                {
                    "intent": i,
                    "answer": r["answer"],
                    "rounds": r["rounds"],
                    "max_rounds_reached": r["max_rounds_reached"],
                    "success": r["success"],
                    "error": r["error"],
                }
                for i, r in zip(intents, results)
            ],
            "total_rounds": sum(r["rounds"] for r in results),
            "error": "; ".join(failed) if failed else None,
        }

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
        handler=_handler,
        visibility="discoverable",
        min_role=Role.USER,
    )
