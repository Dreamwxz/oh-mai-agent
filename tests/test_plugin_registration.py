"""注册层测试：验证 plugin.py 的 @Command / @Tool 声明。

覆盖 todo-2（C1+C2 注册层）的验收标准：
- Command 集合恰为 7 个 maitask_* 名，无旧 task_* Command 名；
- Tool 集合含 9 个名（含 task_status，不含 task_query）；
- 4 个 Tool metadata visibility=="visible"，5 个 == "deferred"；
- Tool 与 Command 不存在同名冲突。
"""

from __future__ import annotations

from maibot_sdk.components import collect_components

from oh_mai_agent.plugin import MaibotAgentPlugin

EXPECTED_COMMANDS = {
    "maitask_create",
    "maitask_list",
    "maitask_status",
    "maitask_cancel",
    "maitask_history",
    "maitask_ask",
    "maitask_help_fallback",
}

EXPECTED_TOOLS = {
    "task_create",
    "task_list",
    "task_status",
    "task_modify",
    "task_delete",
    "task_history",
    "task_schedule",
    "search_users",
    "send_message",
}

VISIBLE_TOOLS = {"task_create", "task_list", "task_status", "task_delete"}

LEGACY_COMMAND_NAMES = {"task_create", "task_list", "task_history", "task_query"}


def _collect() -> list[dict]:
    return collect_components(MaibotAgentPlugin())


def test_command_set_is_exactly_seven_maitask_names() -> None:
    """Command 集合恰为 7 个 maitask_* 名。"""
    components = _collect()
    commands = {c["name"] for c in components if c["type"] == "COMMAND"}
    assert commands == EXPECTED_COMMANDS


def test_tool_set_has_nine_names_including_task_status() -> None:
    """Tool 集合含 9 个名（含 task_status，不含 task_query）。"""
    components = _collect()
    tools = {c["name"] for c in components if c["type"] == "TOOL"}
    assert tools == EXPECTED_TOOLS
    assert "task_status" in tools
    assert "task_query" not in tools


def test_no_legacy_command_names() -> None:
    """Command 集合不包含旧 task_* 名。"""
    components = _collect()
    commands = {c["name"] for c in components if c["type"] == "COMMAND"}
    assert not commands & LEGACY_COMMAND_NAMES


def test_visibility_metadata_split() -> None:
    """4 个 Tool visibility==visible, 5 个 == deferred。"""
    components = _collect()
    tools = [c for c in components if c["type"] == "TOOL"]
    visible = {c["name"] for c in tools if c["metadata"].get("visibility") == "visible"}
    deferred = {c["name"] for c in tools if c["metadata"].get("visibility") == "deferred"}
    assert len(tools) == 9
    assert visible == VISIBLE_TOOLS
    assert deferred == EXPECTED_TOOLS - VISIBLE_TOOLS
    assert len(visible) == 4
    assert len(deferred) == 5


def test_no_name_collision_between_tool_and_command() -> None:
    """Tool 与 Command 不存在同名冲突。"""
    components = _collect()
    names: dict[str, list[str]] = {}
    for c in components:
        names.setdefault(c["name"], []).append(c["type"])
    collisions = {name: types for name, types in names.items() if len(types) > 1}
    assert not collisions, f"Name collision(s) found: {collisions}"