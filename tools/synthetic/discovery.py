"""Agent 循环的合成发现工具通道。

提供 ``list_tools`` 和 ``get_tool_schema`` 两个合成工具
的 LLM schema 定义与 handler 实现，供 ``executor/agent_loop.py``
导入后作为内置工具呈现给 LLM。
"""

from __future__ import annotations

import logging
from typing import Any

from ...permission import PermissionResolver

logger = logging.getLogger(__name__)

# ── 合成工具 schema ────────────────────────────────────────────────────────

_LIST_TOOLS_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "list_tools",
        "description": "列出所有可按需发现的工具（discoverable tools），返回每个工具的名称和简要描述。调用此工具后如需使用某个工具，请先调用 get_tool_schema 获取其完整参数定义。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

_GET_TOOL_SCHEMA_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_tool_schema",
        "description": "获取一个 discoverable 工具的完整 JSON Schema 定义（包括参数列表和类型）。获取 schema 后可在后续轮次中直接调用该工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要获取 schema 的工具名称（来自 list_tools 返回的列表）",
                },
            },
            "required": ["name"],
        },
    },
}


def build_discovery_schemas() -> list[dict[str, Any]]:
    """返回两个合成发现工具的 LLM schema 列表。"""
    return [_LIST_TOOLS_DEFINITION, _GET_TOOL_SCHEMA_DEFINITION]


async def handle_list_tools(registry, role) -> dict[str, Any]:
    """返回对 *role* 可见的所有 discoverable 工具的名称和描述。"""
    try:
        discoverable = registry.list_discoverable(role)
        tools = [
            {"name": d.name, "description": d.description}
            for d in discoverable
        ]
        logger.debug(
            "Agent 发现工具：角色 %s 可见 discoverable 工具 %d 个",
            role.value, len(tools),
        )
        return {"success": True, "tools": tools}
    except Exception:
        logger.exception("Agent 列出可发现工具失败：角色 %s", role.value)
        raise


async def handle_get_tool_schema(
    registry, loaded: set, role, name: str
) -> dict[str, Any]:
    """返回 discoverable 工具的完整 LLM schema。

    该工具会被加入 *loaded* 集合，在后续轮次的
    tools 参数中包含。
    """
    logger.debug("Agent 获取工具 schema：%s", name)
    try:
        td = registry.get(name)
        if td is None:
            logger.warning("Agent 获取工具 schema：工具 %s 未找到", name)
            return {"success": False, "error": f"tool not found: {name}"}
        if td.visibility != "discoverable":
            # essential 工具已随上下文始终呈现，不应（也无需）经发现机制重复加载
            logger.warning(
                "Agent 获取工具 schema：工具 %s 非 discoverable（visibility: %s）",
                name, td.visibility,
            )
            return {
                "success": False,
                "error": f"tool '{name}' is not discoverable (visibility: {td.visibility})",
            }
        # 检查角色权限
        if not PermissionResolver.require(role, td.min_role):
            logger.info(
                "Agent 获取工具 schema：角色 %s 无权访问工具 %s",
                role.value, name,
            )
            return {"success": False, "error": "permission denied"}

        loaded.add(name)
        return {"success": True, "schema": td.to_llm_definition()}
    except Exception:
        logger.exception("Agent 获取工具 schema 失败：%s", name)
        raise
