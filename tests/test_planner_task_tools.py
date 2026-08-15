"""tools/planner/task_tools.py — 7 个 planner task_* handler 行为测试。

test_planner_tool_meta.py 只校验 @Tool 元数据（描述 / 枚举）；本文件
用 FakeTaskManager 补齐全部 handler 的行为路径（成功 / 校验失败 /
底层失败 / 异常）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from conftest import FakeTaskManager, make_task

from oh_mai_agent.domain.task_record import TaskLevel, TaskStatus, TriggerType
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.permission import Role
from oh_mai_agent.tools.planner.task_tools import (
    _planner_caller_role,
    _planner_owner,
    build_task_tools,
)


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def manager(store: TaskStore) -> FakeTaskManager:
    return FakeTaskManager(store)


@pytest.fixture
def tools(manager: FakeTaskManager) -> dict[str, Any]:
    return build_task_tools(manager)


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlannerHelpers:
    def test_owner_private_stream_is_stream_id(self) -> None:
        assert _planner_owner("qq:1591625223") == "qq:1591625223"

    def test_owner_group_stream_uses_planner_prefix(self) -> None:
        assert _planner_owner("qq:group:123") == "planner:qq:group:123"

    def test_caller_role_is_admin(self) -> None:
        assert _planner_caller_role() == Role.ADMIN


# ═══════════════════════════════════════════════════════════════════════════════
# task_create
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskCreate:
    @pytest.mark.asyncio
    async def test_success_creates_task(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_create"](
            intent="写周报", stream_id="qq:group:1", level="agent",
        )
        assert result["success"] is True
        assert result["task_id"] == "t-created"
        assert result["level"] == "agent"
        call = manager.calls["create_task"][0]
        assert call["owner"] == "planner:qq:group:1"
        assert call["platform"] == "qq"
        assert call["stream_id"] == "qq:group:1"
        assert call["level"] == TaskLevel.AGENT
        assert call["trigger"] == TriggerType.NOW
        assert call["caller_role"] == Role.ADMIN

    @pytest.mark.asyncio
    async def test_cron_expr_selects_cron_trigger(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        await tools["task_create"](
            intent="定时", stream_id="qq:1", cron_expr="*/5 * * * *",
        )
        call = manager.calls["create_task"][0]
        assert call["trigger"] == TriggerType.CRON
        assert call["cron_expr"] == "*/5 * * * *"

    @pytest.mark.asyncio
    async def test_delay_seconds_selects_delay_trigger(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        await tools["task_create"](intent="稍后", stream_id="qq:1", delay_seconds=60)
        call = manager.calls["create_task"][0]
        assert call["trigger"] == TriggerType.DELAY
        assert call["delay_seconds"] == 60

    @pytest.mark.asyncio
    async def test_invalid_level_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_create"](intent="x", stream_id="qq:1", level="bogus")
        assert result["success"] is False
        assert "无效级别" in result["error"]
        assert "create_task" not in manager.calls

    @pytest.mark.asyncio
    async def test_create_failure_propagates_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.fail.add("create_task")
        result = await tools["task_create"](intent="x", stream_id="qq:1")
        assert result == {"success": False, "error": "create failed"}

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.create_task = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        result = await tools["task_create"](intent="x", stream_id="qq:1")
        assert result["success"] is False
        assert "boom" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# task_list
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskList:
    @pytest.mark.asyncio
    async def test_no_tasks_returns_empty_text(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_list"](stream_id="qq:g:1")
        assert result == {"success": True, "tasks": [], "count": 0, "text": "当前没有匹配的任务。"}

    @pytest.mark.asyncio
    async def test_tasks_formatted_as_lines(
        self, store: TaskStore, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        await store.save(make_task(
            "task-abc123", title="测试任务", status=TaskStatus.RUNNING,
            stream_id="qq:group:1",
        ))
        result = await tools["task_list"](stream_id="qq:group:1")
        assert result["success"] is True
        assert result["count"] == 1
        # 行格式：[id前8位] level/status title — format_status
        assert "task-abc" in result["text"]
        assert "agent/running" in result["text"]
        assert "测试任务" in result["text"]
        assert "—" in result["text"]

    @pytest.mark.asyncio
    async def test_invalid_status_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_list"](stream_id="qq:1", status="bogus")
        assert result["success"] is False
        assert "无效状态" in result["error"]
        assert "list_tasks" not in manager.calls

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.list_tasks = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        result = await tools["task_list"](stream_id="qq:1")
        assert result["success"] is False
        assert "boom" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# task_status
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskStatus:
    @pytest.mark.asyncio
    async def test_success_returns_task_dict(
        self, store: TaskStore, tools: dict[str, Any],
    ) -> None:
        await store.save(make_task("task-abc", title="详情任务"))
        result = await tools["task_status"](task_id="task-abc", stream_id="qq:g:1")
        assert result["success"] is True
        assert result["task"]["id"] == "task-abc"

    @pytest.mark.asyncio
    async def test_title_as_task_id_resolves(
        self, store: TaskStore, tools: dict[str, Any],
    ) -> None:
        """Planner 以看板标题代替 task_id（如「系统环境检查」）时按唯一标题解析。"""
        await store.save(make_task(
            "task-abc", title="系统环境检查", stream_id="qq:g:1",
        ))
        result = await tools["task_status"](task_id="系统环境检查", stream_id="qq:g:1")
        assert result["success"] is True
        assert result["task"]["id"] == "task-abc"

    @pytest.mark.asyncio
    async def test_missing_task_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_status"](task_id="ghost", stream_id="qq:1")
        assert result == {"success": False, "error": "not found"}

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.get_task = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        result = await tools["task_status"](task_id="x", stream_id="qq:1")
        assert result["success"] is False
        assert "boom" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# task_modify / task_delete / task_history / task_schedule
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskModify:
    @pytest.mark.asyncio
    async def test_success_injects_instruction(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_modify"](
            task_id="task-1", inject_instruction="换个思路", stream_id="qq:1",
        )
        assert result == {"success": True, "message": "已注入"}
        call = manager.calls["modify_task"][0]
        assert call["task_id"] == "task-1"
        assert call["inject_instruction"] == "换个思路"

    @pytest.mark.asyncio
    async def test_failure_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.fail.add("modify_task")
        result = await tools["task_modify"](
            task_id="task-1", inject_instruction="x", stream_id="qq:1",
        )
        assert result == {"success": False, "message": "modify failed"}


class TestTaskDelete:
    @pytest.mark.asyncio
    async def test_success_cancels_task(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_delete"](task_id="task-1", stream_id="qq:1")
        assert result == {"success": True, "message": "已取消"}
        assert manager.calls["cancel_task"][0]["task_id"] == "task-1"

    @pytest.mark.asyncio
    async def test_failure_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.fail.add("cancel_task")
        result = await tools["task_delete"](task_id="task-1", stream_id="qq:1")
        assert result == {"success": False, "message": "cancel failed"}


class TestTaskHistory:
    @pytest.mark.asyncio
    async def test_success_returns_history(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_history"](task_id="task-1", stream_id="qq:1")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["history"][0]["type"] == "status_change"

    @pytest.mark.asyncio
    async def test_failure_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.fail.add("task_history")
        result = await tools["task_history"](task_id="task-1", stream_id="qq:1")
        assert result == {"success": False, "error": "history failed"}


class TestTaskSchedule:
    @pytest.mark.asyncio
    async def test_missing_cron_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_schedule"](intent="x", stream_id="qq:1", cron_expr="")
        assert result == {"success": False, "error": "cron_expr 为必填参数"}
        assert "create_task" not in manager.calls

    @pytest.mark.asyncio
    async def test_invalid_level_returns_error(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_schedule"](
            intent="x", stream_id="qq:1", cron_expr="*/5 * * * *", level="bogus",
        )
        assert result["success"] is False
        assert "无效级别" in result["error"]

    @pytest.mark.asyncio
    async def test_success_creates_cron_task(
        self, tools: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await tools["task_schedule"](
            intent="每天日报", stream_id="qq:g:1", cron_expr="0 9 * * *",
        )
        assert result["success"] is True
        assert result["cron_expr"] == "0 9 * * *"
        call = manager.calls["create_task"][0]
        assert call["trigger"] == TriggerType.CRON
        assert call["cron_expr"] == "0 9 * * *"
        assert call["owner"] == "qq:g:1"
