"""Planner tool 元数据测试：description、enum_values、stream_id 透传。

覆盖 todo-3（C3+C4 planner 侧）的验收标准：
- 11 个 @Tool 的 description 非空且含定位句；
- task_create / task_schedule 的 description 含对方工具名（互为区分）；
- task_status 与 task_history 的 description 互相区分；
- task_list 的 status 参数 enum_values 为 8 个 TaskStatus 值；
- task_create / task_schedule 的 level 参数 enum_values 为 ["instant","agent"]；
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
# 9 个 description 语义检查
# ══════════════════════════════════════════════════════════════════════════════

EXPECTED_TOOL_NAMES = {
    "task_create", "task_list", "task_status", "task_modify",
    "task_delete", "task_history", "task_schedule",
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


def test_task_create_mentions_task_schedule_and_status() -> None:
    """task_create 的 description 含 task_schedule 和 task_status（互为区分）。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["task_create"]
    assert "task_schedule" in desc, "task_create 应提及 task_schedule"
    assert "task_status" in desc, "task_create 应提及 task_status"


def test_task_schedule_mentions_task_create() -> None:
    """task_schedule 的 description 含 task_create（互为区分）。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["task_schedule"]
    assert "task_create" in desc, "task_schedule 应提及 task_create"


def test_task_status_mentions_task_history() -> None:
    """task_status 的 description 含 task_history（互为区分）。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["task_status"]
    assert "task_history" in desc, "task_status 应提及 task_history"


def test_task_history_mentions_task_status() -> None:
    """task_history 的 description 含 task_status（互为区分）。"""
    components = _collect()
    tools = {c["name"]: c["metadata"]["description"] for c in components if c["type"] == "TOOL"}
    desc = tools["task_history"]
    assert "task_status" in desc, "task_history 应提及 task_status"


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


def test_task_list_status_enum_values() -> None:
    """task_list.status 参数 enum_values 为 8 个 TaskStatus 值。"""
    param = _get_param("task_list", "status")
    assert param is not None, "task_list 应有 status 参数"
    ev = param.get("enum_values")
    assert ev is not None, "task_list.status 应有 enum_values"
    expected = [s.value for s in TaskStatus]
    assert set(ev) == set(expected), f"enum_values 应为 {expected}，实际 {ev}"
    assert len(ev) == 8, f"enum_values 应为 8 个值，实际 {len(ev)}"


def test_task_create_level_enum_values() -> None:
    """task_create.level 参数 enum_values 为 ["instant","agent"]。"""
    param = _get_param("task_create", "level")
    assert param is not None, "task_create 应有 level 参数"
    ev = param.get("enum_values")
    assert ev is not None, "task_create.level 应有 enum_values"
    expected = [l.value for l in TaskLevel]
    assert set(ev) == set(expected), f"enum_values 应为 {expected}，实际 {ev}"
    assert len(ev) == 2, f"enum_values 应为 2 个值，实际 {len(ev)}"


def test_task_schedule_level_enum_values() -> None:
    """task_schedule.level 参数 enum_values 为 ["instant","agent"]。"""
    param = _get_param("task_schedule", "level")
    assert param is not None, "task_schedule 应有 level 参数"
    ev = param.get("enum_values")
    assert ev is not None, "task_schedule.level 应有 enum_values"
    expected = [l.value for l in TaskLevel]
    assert set(ev) == set(expected), f"enum_values 应为 {expected}，实际 {ev}"
    assert len(ev) == 2, f"enum_values 应为 2 个值，实际 {len(ev)}"


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
async def test_task_list_passes_stream_id() -> None:
    """_task_list handler 向 list_tasks 传入 stream_id。"""
    fake = FakeTaskManager()
    tools = build_task_tools(fake)
    handler = tools["task_list"]

    result = await handler(stream_id="qq:group:123", status=None)
    assert result["success"] is True

    assert len(fake.list_tasks_calls) == 1
    call_kwargs = fake.list_tasks_calls[0]
    assert "stream_id" in call_kwargs, "list_tasks 应收到 stream_id 参数"
    assert call_kwargs["stream_id"] == "qq:group:123"


@pytest.mark.asyncio
async def test_task_list_passes_stream_id_empty() -> None:
    """stream_id 为空字符串时，list_tasks 仍应收到 stream_id=''。"""
    fake = FakeTaskManager()
    tools = build_task_tools(fake)
    handler = tools["task_list"]

    result = await handler(stream_id="", status=None)
    assert result["success"] is True

    assert len(fake.list_tasks_calls) == 1
    call_kwargs = fake.list_tasks_calls[0]
    assert call_kwargs.get("stream_id") == ""