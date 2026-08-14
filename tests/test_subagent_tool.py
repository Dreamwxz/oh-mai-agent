"""ask_subagent / ask_subagents 工具测试。

覆盖（对应 plan todo 5 acceptance）：
- 默认允许集 = 信息+文件+MCP 工具，不含 7 个排除名与 call_ 前缀
- tools 覆盖为合法子集可用；非法名严格拒绝（整体拒绝，不静默过滤）
- 空 intent 拒绝；intents 为空/超限拒绝
- 批量成功合并格式与 per-item 字段；批量中一项失败其余仍返回且 top-level
  success=False
- 角色过滤生效
- config_getter 热更新（每次调用读取，不缓存配置对象快照）
"""

from __future__ import annotations

from typing import Any

import pytest

from oh_mai_agent.config import SubAgentConfig
from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.subagent_tool import (
    _resolve_toolset,
    build_subagent_tool,
    build_subagents_tool,
)
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry

# 与实现保持一致的 7 个排除名（防绕过：发消息/反问/任务管理/递归/宿主命令）
_EXCLUDED_NAMES = {
    "ask_user",
    "send_message",
    "list_my_tasks",
    "create_subtask",
    "inject_task",
    "ask_subagent",
    "ask_subagents",
}


async def _ok_handler(**kwargs: Any) -> dict:
    return {"success": True}


def _make_registry() -> ToolRegistry:
    """构造样例注册表：7 个排除名 + call_ 前缀 + MCP + 信息 + 文件 + admin 专用。"""
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
    return {t.name for t in _resolve_toolset(_make_registry(), role, None)}


def _cfg(**overrides: Any) -> SubAgentConfig:
    return SubAgentConfig(**overrides)


class _CfgHolder:
    """可变配置持有者：验证 config_getter 每次调用都读新值（热更新）。"""

    def __init__(self, cfg: SubAgentConfig) -> None:
        self.cfg = cfg

    def __call__(self) -> SubAgentConfig:
        return self.cfg


def _schema_names(ctx) -> set[str]:
    """从 MockLLM call_history 提取子循环收到的工具 schema 名集合。"""
    names: set[str] = set()
    for call in ctx.llm.call_history:
        if call["type"] == "generate_with_tools":
            names = {t["function"]["name"] for t in call["tools"]}
    return names


# ── 默认允许集与排除规则 ───────────────────────────────────────────────────


def test_default_set_excludes_7_names_and_call_prefix() -> None:
    """USER 角色下默认集 = 信息+文件+MCP，不含 7 个排除名与 call_ 前缀。"""
    names = _default_tool_names(Role.USER)
    assert names == {"search_memory", "fetch_history", "read", "write", "mcp_fetch_page"}
    assert names.isdisjoint(_EXCLUDED_NAMES)
    assert not any(n.startswith("call_") for n in names)
    # 不含 admin 专用工具（USER 不可见）
    assert "admin_secret" not in names


@pytest.mark.asyncio
async def test_legal_subset_override_works(mock_ctx, prompt_service) -> None:
    """tools 覆盖为合法子集可用：子循环收到的 schema 仅含请求的工具。"""
    tool = build_subagent_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    mock_ctx.llm.set_tool_response("晴天")
    result = await tool.handler(intent="查天气", tools=["search_memory"])
    assert result["success"] is True
    assert result["answer"] == "晴天"
    assert _schema_names(mock_ctx) == {"search_memory"}


@pytest.mark.asyncio
async def test_illegal_name_strict_rejected(mock_ctx, prompt_service) -> None:
    """非法名严格拒绝：send_message / call_ 前缀 / 不存在的工具名 → 整体拒绝。"""
    tool = build_subagent_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    for bad in (["send_message"], ["call_plugin_api"], ["search_memory", "bogus"], "send_message"):
        result = await tool.handler(intent="查天气", tools=bad)
        assert result["success"] is False
        assert "invalid tools" in result["error"]
        assert any(n in result["error"] for n in ("send_message", "bogus", "call_plugin_api"))
        # 整体拒绝：绝不静默过滤后执行
        assert mock_ctx.llm.call_history == []


@pytest.mark.asyncio
async def test_empty_intent_rejected(mock_ctx, prompt_service) -> None:
    """空 intent → 拒绝。"""
    tool = build_subagent_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    assert await tool.handler(intent="") == {"success": False, "error": "缺少必需参数: intent"}
    assert await tool.handler() == {"success": False, "error": "缺少必需参数: intent"}


# ── 单个派发（happy path） ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_happy_default_set_roundtrip(mock_ctx, prompt_service) -> None:
    """默认集单轮直答：success True、answer 回传、rounds=1、5 键齐全。"""
    tool = build_subagent_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    mock_ctx.llm.set_tool_response("答案：晴天")
    result = await tool.handler(intent="查天气")
    assert result["success"] is True
    assert result["answer"] == "答案：晴天"
    assert result["rounds"] == 1
    assert result["max_rounds_reached"] is False
    assert result["error"] is None
    # 子循环收到的工具集 = 默认允许集（无排除名 / call_ 前缀）
    assert _schema_names(mock_ctx) == {
        "search_memory", "fetch_history", "read", "write", "mcp_fetch_page"
    }


def test_tool_definition_metadata() -> None:
    """ask_subagent 定义：discoverable、min_role=USER。"""
    tool = build_subagent_tool(
        object(), _make_registry(), None, _cfg, role_provider=lambda: Role.USER,
    )
    assert tool.name == "ask_subagent"
    assert tool.visibility == "discoverable"
    assert tool.min_role == Role.USER


# ── 批量派发（ask_subagents） ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_empty_intents_rejected(mock_ctx, prompt_service) -> None:
    """intents 为空 → 拒绝。"""
    tool = build_subagents_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    assert await tool.handler(intents=[]) == {"success": False, "error": "intents 不能为空"}
    assert await tool.handler() == {"success": False, "error": "intents 不能为空"}


@pytest.mark.asyncio
async def test_batch_over_limit_rejected(mock_ctx, prompt_service) -> None:
    """intents 数量超 max_parallel_subagents → 整体拒绝。"""
    tool = build_subagents_tool(
        mock_ctx, _make_registry(), prompt_service,
        lambda: _cfg(max_parallel_subagents=2),
        role_provider=lambda: Role.USER,
    )
    result = await tool.handler(intents=["查A", "查B", "查C"])
    assert result["success"] is False
    assert "intents 数量超限: 3" in result["error"]
    assert "上限 2" in result["error"]


@pytest.mark.asyncio
async def test_batch_config_hot_reload(mock_ctx, prompt_service) -> None:
    """config_getter 每次调用读取：修改上限后同一 handler 立即生效。"""
    holder = _CfgHolder(_cfg(max_parallel_subagents=1))
    tool = build_subagents_tool(
        mock_ctx, _make_registry(), prompt_service, holder,
        role_provider=lambda: Role.USER,
    )
    # 上限 1：2 个意图被拒
    result = await tool.handler(intents=["查A", "查B"])
    assert result["success"] is False
    assert "上限 1" in result["error"]
    # 热更新：无需重建工具，新上限 2 立即生效
    holder.cfg = _cfg(max_parallel_subagents=2)
    mock_ctx.llm.set_tool_response("答A")
    mock_ctx.llm.set_tool_response("答B")
    result = await tool.handler(intents=["查A", "查B"])
    assert result["success"] is True
    assert result["total_rounds"] == 2


@pytest.mark.asyncio
async def test_batch_success_merge_format(mock_ctx, prompt_service) -> None:
    """批量成功：answers 按 intents 顺序、per-item 字段齐全、total_rounds 正确。"""
    tool = build_subagents_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    mock_ctx.llm.set_tool_response("查A 的结果")
    mock_ctx.llm.set_tool_response("查B 的结果")
    result = await tool.handler(intents=["查A", "查B"])
    assert result["success"] is True
    assert result["total_rounds"] == 2
    assert result["error"] is None
    assert len(result["answers"]) == 2
    assert result["answers"][0] == {
        "intent": "查A",
        "answer": "查A 的结果",
        "rounds": 1,
        "max_rounds_reached": False,
        "success": True,
        "error": None,
    }
    assert result["answers"][1] == {
        "intent": "查B",
        "answer": "查B 的结果",
        "rounds": 1,
        "max_rounds_reached": False,
        "success": True,
        "error": None,
    }


@pytest.mark.asyncio
async def test_batch_partial_failure(mock_ctx, prompt_service) -> None:
    """批量中一项失败：其余仍返回，top-level success=False，error 聚合失败项。"""

    class _FlakyLLM:
        """第一次 generate_with_tools 抛异常，之后正常直答。"""

        def __init__(self) -> None:
            self.calls = 0

        async def generate_with_tools(self, prompt: list, tools: list, model: str = "", **kwargs: Any) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("LLM boom")
            return {"success": True, "response": "ok", "tool_calls": []}

    mock_ctx.llm = _FlakyLLM()
    tool = build_subagents_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    result = await tool.handler(intents=["查A", "查B"])
    assert result["success"] is False
    assert len(result["answers"]) == 2
    # 第一项失败：5 键齐全、error 为异常消息
    assert result["answers"][0]["intent"] == "查A"
    assert result["answers"][0]["success"] is False
    assert result["answers"][0]["error"] == "LLM boom"
    assert result["answers"][0]["rounds"] == 0
    assert result["answers"][0]["answer"] == ""
    # 第二项不受影响，正常返回
    assert result["answers"][1]["success"] is True
    assert result["answers"][1]["answer"] == "ok"
    assert result["answers"][1]["error"] is None
    # total_rounds 只统计成功项；error 聚合失败项
    assert result["total_rounds"] == 1
    assert result["error"] == "LLM boom"


def test_batch_tool_definition_metadata() -> None:
    """ask_subagents 定义：discoverable、min_role=USER。"""
    tool = build_subagents_tool(
        object(), _make_registry(), None, _cfg, role_provider=lambda: Role.USER,
    )
    assert tool.name == "ask_subagents"
    assert tool.visibility == "discoverable"
    assert tool.min_role == Role.USER


# ── 角色过滤 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_role_filtering_respected(mock_ctx, prompt_service) -> None:
    """USER 不可见 admin 专用工具（tools 覆盖被拒）；ADMIN 可见（覆盖可用）。"""
    user_tool = build_subagent_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    result = await user_tool.handler(intent="查X", tools=["admin_secret"])
    assert result["success"] is False
    assert "invalid tools" in result["error"]

    admin_tool = build_subagent_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.ADMIN,
    )
    mock_ctx.llm.set_tool_response("管理员可见")
    result = await admin_tool.handler(intent="查X", tools=["admin_secret"])
    assert result["success"] is True
    assert _schema_names(mock_ctx) == {"admin_secret"}
    # 默认集层面：ADMIN 多看到 admin_secret
    assert "admin_secret" in _default_tool_names(Role.ADMIN)


@pytest.mark.asyncio
async def test_batch_role_filtering_respected(mock_ctx, prompt_service) -> None:
    """批量工具同样按角色过滤：USER 下 tools=["admin_secret"] 整体拒绝。"""
    tool = build_subagents_tool(
        mock_ctx, _make_registry(), prompt_service, _cfg,
        role_provider=lambda: Role.USER,
    )
    result = await tool.handler(intents=["查A"], tools=["admin_secret"])
    assert result["success"] is False
    assert "invalid tools" in result["error"]
