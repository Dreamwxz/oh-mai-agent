"""Planner tool 元数据测试：description、visibility、enum_values、stream_id 透传。

覆盖 planner 侧（v0.1.0 重构）的验收标准：
- 11 个 @Tool 的 description 非空且含定位句；
- 工具名统一为 subagent_*（后台子代理管理心智）；
- subagent_create / subagent_schedule 的 description 含对方工具名（互为区分）；
- subagent_status 与 subagent_history 的 description 互相区分；
- subagent_schedule 已升级为 visible（高频定时诉求，省去 tool_search 激活）；
- subagent_list 的 status 参数 enum_values 为 8 个 TaskStatus 值；
- subagent_create / subagent_schedule 的 level 参数 enum_values 为 ["instant","agent"]；
- _task_list 调用 list_tasks 时传了 stream_id。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from maibot_sdk.components import collect_components

from oh_mai_agent.domain.task_record import TaskLevel, TaskStatus
from oh_mai_agent.plugin import MaibotAgentPlugin
from oh_mai_agent.tools.planner.task_tools import build_task_tools


def _collect() -> list[dict]:
    return collect_components(MaibotAgentPlugin())


# ══════════════════════════════════════════════════════════════════════════════
# 11 个 description 语义检查
# ══════════════════════════════════════════════════════════════════════════════

EXPECTED_TOOL_NAMES = {
    "subagent_create", "subagent_list", "subagent_status", "subagent_modify",
    "subagent_delete", "subagent_history", "subagent_schedule",
    "search_users", "send_message",
    "list_mcp_tools", "call_mcp_tool",
}


def test_all_tool_descriptions_non_empty() -> None:
    """11 个 @Tool 全部有非空 description。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    for name in EXPECTED_TOOL_NAMES:
        desc = tools.get(name, "")
        assert desc, f"Tool {name!r} 的 description 为空"


def test_subagent_create_mentions_schedule_and_status() -> None:
    """subagent_create 的 description 含 subagent_schedule 和 subagent_status（互为区分）。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["subagent_create"]
    assert "subagent_schedule" in desc, "subagent_create 应提及 subagent_schedule"
    assert "subagent_status" in desc, "subagent_create 应提及 subagent_status"


def test_subagent_schedule_mentions_subagent_create() -> None:
    """subagent_schedule 的 description 含 subagent_create（互为区分）。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["subagent_schedule"]
    assert "subagent_create" in desc, "subagent_schedule 应提及 subagent_create"


def test_subagent_status_mentions_subagent_history() -> None:
    """subagent_status 的 description 含 subagent_history（互为区分）。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["subagent_status"]
    assert "subagent_history" in desc, "subagent_status 应提及 subagent_history"


def test_subagent_history_mentions_subagent_status() -> None:
    """subagent_history 的 description 含 subagent_status（互为区分）。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["subagent_history"]
    assert "subagent_status" in desc, "subagent_history 应提及 subagent_status"


def test_subagent_create_mentions_background_agent() -> None:
    """subagent_create 的 description 建立「后台子代理」心智模型。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["subagent_create"]
    assert "后台" in desc and "Agent" in desc, "subagent_create 应说明后台 Agent 执行模型"


# ══════════════════════════════════════════════════════════════════════════════
# visibility 检查
# ══════════════════════════════════════════════════════════════════════════════


def _get_tool_visibility(name: str) -> str:
    """从 collect_components 结果中提取指定工具的 visibility 元数据。"""
    components = _collect()
    for c in components:
        if c["type"] == "TOOL" and c["name"] == name:
            return str(c["metadata"].get("visibility", ""))
    return ""


def test_visible_tools() -> None:
    """高频操作保持每轮可见：create/list/status/delete/schedule。"""
    for name in ("subagent_create", "subagent_list", "subagent_status",
                 "subagent_delete", "subagent_schedule"):
        assert _get_tool_visibility(name) == "visible", f"{name} 应为 visible"


def test_deferred_tools() -> None:
    """低频/进阶操作保持 deferred（经 tool_search 发现）。"""
    for name in ("subagent_modify", "subagent_history", "search_users",
                 "send_message", "list_mcp_tools", "call_mcp_tool"):
        assert _get_tool_visibility(name) == "deferred", f"{name} 应为 deferred"


# ══════════════════════════════════════════════════════════════════════════════
# enum_values 检查
# ══════════════════════════════════════════════════════════════════════════════


def _get_tool_params(name: str) -> list[dict]:
    """从 collect_components 结果中提取指定工具的 parameters 元数据。"""
    components = _collect()
    for c in components:
        if c["type"] == "TOOL" and c["name"] == name:
            return c["metadata"].get("parameters", []) or []
    return []


def _get_param(tool_name: str, param_name: str) -> dict | None:
    """从工具参数列表中按 name 查找参数。"""
    for p in _get_tool_params(tool_name):
        if p.get("name") == param_name:
            return p
    return None


def test_subagent_list_status_enum_values() -> None:
    """subagent_list.status 参数 enum_values 为 8 个 TaskStatus 值。"""
    param = _get_param("subagent_list", "status")
    assert param is not None, "subagent_list 应有 status 参数"
    ev = param.get("enum_values")
    assert ev is not None, "subagent_list.status 应有 enum_values"
    expected = [s.value for s in TaskStatus]
    assert set(ev) == set(expected), f"enum_values 应为 {expected}，实际 {ev}"
    assert len(ev) == 8, f"enum_values 应为 8 个值，实际 {len(ev)}"


def test_subagent_create_level_enum_values() -> None:
    """subagent_create.level 参数 enum_values 为 ["instant","agent"]。"""
    param = _get_param("subagent_create", "level")
    assert param is not None, "subagent_create 应有 level 参数"
    ev = param.get("enum_values")
    assert ev is not None, "subagent_create.level 应有 enum_values"
    expected = [l.value for l in TaskLevel]
    assert set(ev) == set(expected), f"enum_values 应为 {expected}，实际 {ev}"
    assert len(ev) == 2, f"enum_values 应为 2 个值，实际 {len(ev)}"


def test_subagent_schedule_level_enum_values() -> None:
    """subagent_schedule.level 参数 enum_values 为 ["instant","agent"]。"""
    param = _get_param("subagent_schedule", "level")
    assert param is not None, "subagent_schedule 应有 level 参数"
    ev = param.get("enum_values")
    assert ev is not None, "subagent_schedule.level 应有 enum_values"
    expected = [l.value for l in TaskLevel]
    assert set(ev) == set(expected), f"enum_values 应为 {expected}，实际 {ev}"
    assert len(ev) == 2, f"enum_values 应为 2 个值，实际 {len(ev)}"


def test_stream_id_param_semantics() -> None:
    """stream_id 参数描述应传达「任务所属聊天流」语义。"""
    param = _get_param("subagent_create", "stream_id")
    assert param is not None, "subagent_create 应有 stream_id 参数"
    desc = param.get("description", "")
    assert "所属聊天流" in desc, f"stream_id 描述应含『所属聊天流』，实际：{desc}"


# ══════════════════════════════════════════════════════════════════════════════
# _task_list 传 stream_id 测试
# ══════════════════════════════════════════════════════════════════════════════


class FakeTaskManager:
    """仅捕获 list_tasks 调用 kwargs 的 fake。"""

    def __init__(self) -> None:
        self.list_tasks_calls: list[dict] = []

    async def list_tasks(self, **kwargs: Any) -> list[dict]:
        self.list_tasks_calls.append(kwargs)
        return []


@pytest.mark.asyncio
async def test_subagent_list_passes_stream_id() -> None:
    """_task_list handler 向 list_tasks 传入 stream_id。"""
    fake = FakeTaskManager()
    tools = build_task_tools(fake)
    handler = tools["subagent_list"]

    result = await handler(stream_id="qq:group:123", status=None)
    assert result["success"] is True

    assert len(fake.list_tasks_calls) == 1
    call_kwargs = fake.list_tasks_calls[0]
    assert "stream_id" in call_kwargs, "list_tasks 应收到 stream_id 参数"
    assert call_kwargs["stream_id"] == "qq:group:123"


@pytest.mark.asyncio
async def test_subagent_list_passes_stream_id_empty() -> None:
    """stream_id 为空字符串时，list_tasks 仍应收到 stream_id=''。"""
    fake = FakeTaskManager()
    tools = build_task_tools(fake)
    handler = tools["subagent_list"]

    result = await handler(stream_id="", status=None)
    assert result["success"] is True

    assert len(fake.list_tasks_calls) == 1
    call_kwargs = fake.list_tasks_calls[0]
    assert call_kwargs.get("stream_id") == ""
