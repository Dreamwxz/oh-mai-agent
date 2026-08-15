"""子 Agent 工具域测试 — schema 定义、工具集规则、执行守卫。

执行（子 Agent 派发）已迁入 AgentLoop 合成工具分支（见
``tests/test_agent_loop_subagent.py``）；本文件只测工具层职责：

- ``resolve_toolset`` 工具集规则：默认允许集 / 排除名 / call_ 前缀 /
  角色过滤 / 显式子集校验（非法名整体拒绝，绝不静默过滤）
- 两个工具 schema 定义（name / description / parameters / 可见性 / 角色门槛）
- 守卫 handler：registry.execute 直调返回明确错误（正常路径由 AgentLoop
  分发处拦截，不经过 handler）
"""

from __future__ import annotations

from typing import Any

import pytest

from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.subagent_tool import (
    build_subagent_tool,
    build_subagents_tool,
    resolve_toolset,
)
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry

# 与实现保持一致的排除名（防绕过：发消息/反问/任务管理/递归/宿主命令）
_EXCLUDED_NAMES = {
    "ask_user",
    "send_message",
    "list_my_tasks",
    "create_subtask",
    "inject_task",
    "ask_subagent",
    "ask_subagents",
    "run_command",
}


async def _ok_handler(**kwargs: Any) -> dict:
    return {"success": True}


def _make_registry() -> ToolRegistry:
    """构造样例注册表：排除名 + call_ 前缀 + MCP + 信息 + 文件 + admin 专用。"""
    reg = ToolRegistry()
    for name in sorted(_EXCLUDED_NAMES):
        reg.register(ToolDefinition(name=name, description="x", parameters={}, handler=_ok_handler))
    for name in ["call_plugin_api", "mcp_fetch_page", "search_memory", "fetch_history", "read", "write"]:
        reg.register(ToolDefinition(name=name, description="x", parameters={}, handler=_ok_handler))
    reg.register(
        ToolDefinition(
            name="admin_secret", description="x", parameters={},
            handler=_ok_handler, min_role=Role.ADMIN,
        )
    )
    return reg


def _default_tool_names(role: Role) -> set[str]:
    return {t.name for t in resolve_toolset(_make_registry(), role, None)}


# ── 工具集规则（resolve_toolset）─────────────────────────────────────────────


def test_default_set_excludes_names_and_call_prefix() -> None:
    """USER 角色下默认集 = 信息+文件+MCP，不含排除名与 call_ 前缀。"""
    names = _default_tool_names(Role.USER)
    assert names == {"search_memory", "fetch_history", "read", "write", "mcp_fetch_page"}
    assert names.isdisjoint(_EXCLUDED_NAMES)
    assert not any(n.startswith("call_") for n in names)
    # 不含 admin 专用工具（USER 不可见）
    assert "admin_secret" not in names


def test_admin_role_sees_admin_tools() -> None:
    """ADMIN 角色下默认集包含 admin 专用工具。"""
    assert "admin_secret" in _default_tool_names(Role.ADMIN)


def test_legal_subset_returns_requested_only() -> None:
    """显式 tools 为合法子集时仅返回请求的工具（保持注册顺序）。"""
    reg = _make_registry()
    result = resolve_toolset(reg, Role.USER, ["read", "search_memory"])
    assert [t.name for t in result] == ["search_memory", "read"]


def test_string_request_tolerated() -> None:
    """LLM 把单个工具名传成字符串时按单元素列表处理。"""
    reg = _make_registry()
    result = resolve_toolset(reg, Role.USER, "read")
    assert [t.name for t in result] == ["read"]


@pytest.mark.parametrize("bad", ["send_message", "call_plugin_api", "ask_subagent", "nope", "run_command"])
def test_illegal_name_strictly_rejected(bad: str) -> None:
    """非法名（排除名 / call_ 前缀 / 不存在 / 宿主命令）→ ValueError 整体拒绝。"""
    with pytest.raises(ValueError, match="invalid tools"):
        resolve_toolset(_make_registry(), Role.USER, [bad])


def test_mixed_valid_and_invalid_rejected() -> None:
    """合法名 + 非法名混用也整体拒绝（绝不静默过滤非法名）。"""
    with pytest.raises(ValueError, match="invalid tools"):
        resolve_toolset(_make_registry(), Role.USER, ["read", "nope"])


# ── schema 定义 ──────────────────────────────────────────────────────────────


def test_ask_subagent_schema_shape() -> None:
    """ask_subagent：discoverable、USER 门槛、intent 必填、tools 可选。"""
    tool = build_subagent_tool()
    assert tool.name == "ask_subagent"
    assert tool.visibility == "discoverable"
    assert tool.min_role == Role.USER
    assert "子 Agent" in tool.description
    props = tool.parameters["properties"]
    assert set(props) == {"intent", "tools"}
    assert tool.parameters["required"] == ["intent"]


def test_ask_subagents_schema_shape() -> None:
    """ask_subagents：discoverable、USER 门槛、intents 必填数组。"""
    tool = build_subagents_tool()
    assert tool.name == "ask_subagents"
    assert tool.visibility == "discoverable"
    assert tool.min_role == Role.USER
    props = tool.parameters["properties"]
    assert set(props) == {"intents", "tools"}
    assert props["intents"]["type"] == "array"
    assert tool.parameters["required"] == ["intents"]


# ── 执行守卫（registry.execute 直调兜底）────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_subagent_guard_rejects_direct_execute() -> None:
    """registry.execute 直调 ask_subagent → 明确错误（执行在 AgentLoop 合成分支）。"""
    result = await build_subagent_tool().handler(intent="查天气")
    assert result["success"] is False
    assert "Agent 引擎内建执行" in result["error"]


@pytest.mark.asyncio
async def test_ask_subagents_guard_rejects_direct_execute() -> None:
    """registry.execute 直调 ask_subagents → 明确错误（执行在 AgentLoop 合成分支）。"""
    result = await build_subagents_tool().handler(intents=["查A", "查B"])
    assert result["success"] is False
    assert "Agent 引擎内建执行" in result["error"]
