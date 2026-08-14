"""tools/agent/task_mgmt.py — list_my_tasks / create_subtask / inject_task 行为测试。

这三个 discoverable 工具此前仅被当作工具名字符串引用，真实 handler
（含权限校验 / 参数校验 / 注入逻辑）从未被执行过。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from conftest import FakeTaskManager, make_task

from oh_mai_agent.domain.task_record import TaskLevel, TaskStatus
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.task_mgmt import build_task_mgmt_tools


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def manager(store: TaskStore) -> FakeTaskManager:
    return FakeTaskManager(store)


def _build(
    manager: FakeTaskManager,
    *,
    current_task: Any = None,
    current_role: Role = Role.USER,
    handle_injection: AsyncMock | None = None,
) -> tuple[dict[str, Any], AsyncMock]:
    """构建三个任务管理工具；返回 (name→tool 字典, handle_injection mock)。"""
    injection = handle_injection or AsyncMock(return_value=True)
    tools = build_task_mgmt_tools(
        manager._store,
        manager._sfmt,
        create_task=manager.create_task,
        handle_injection=injection,
        get_current_task=lambda: current_task,
        get_current_task_role=lambda: current_role,
    )
    return {t.name: t for t in tools}, injection


def _current(owner: str = "qq:10001", task_id: str = "cur") -> Any:
    return make_task(task_id=task_id, owner=owner, stream_id=owner)


# ═══════════════════════════════════════════════════════════════════════════════
# list_my_tasks
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMyTasks:
    @pytest.mark.asyncio
    async def test_no_current_task_returns_error(self, manager: FakeTaskManager) -> None:
        tools, _ = _build(manager, current_task=None)
        result = await tools["list_my_tasks"].handler()
        assert result == {"success": False, "error": "无当前任务上下文"}

    @pytest.mark.asyncio
    async def test_lists_only_own_owner_tasks(
        self, store: TaskStore, manager: FakeTaskManager,
    ) -> None:
        """只返回当前属主名下的任务，且摘要包含 format_status。"""
        await store.save(make_task("mine-1", owner="qq:10001", status=TaskStatus.RUNNING))
        await store.save(make_task("mine-2", owner="qq:10001", status=TaskStatus.PENDING))
        await store.save(make_task("other-1", owner="qq:99999"))

        tools, _ = _build(manager, current_task=_current(owner="qq:10001"))
        result = await tools["list_my_tasks"].handler()

        assert result["success"] is True
        assert result["count"] == 2
        ids = {t["id"] for t in result["tasks"]}
        assert ids == {"mine-1", "mine-2"}
        assert all(t["format_status"] for t in result["tasks"])


# ═══════════════════════════════════════════════════════════════════════════════
# create_subtask
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateSubtask:
    @pytest.mark.asyncio
    async def test_no_current_task_returns_error(self, manager: FakeTaskManager) -> None:
        tools, _ = _build(manager, current_task=None)
        result = await tools["create_subtask"].handler(intent="子任务")
        assert result == {"success": False, "error": "无当前任务上下文"}

    @pytest.mark.asyncio
    async def test_missing_intent_returns_error(self, manager: FakeTaskManager) -> None:
        tools, _ = _build(manager, current_task=_current())
        result = await tools["create_subtask"].handler(intent="")
        assert result == {"success": False, "error": "缺少必需参数: intent"}

    @pytest.mark.asyncio
    async def test_invalid_level_returns_error(self, manager: FakeTaskManager) -> None:
        tools, _ = _build(manager, current_task=_current())
        result = await tools["create_subtask"].handler(intent="x", level="bogus")
        assert result["success"] is False
        assert "无效级别" in result["error"]

    @pytest.mark.asyncio
    async def test_success_inherits_owner_platform_stream(
        self, manager: FakeTaskManager,
    ) -> None:
        """子任务沿用当前任务的属主/平台/流，并按 USER 权限创建。"""
        tools, _ = _build(manager, current_task=_current(owner="qq:10001"))
        result = await tools["create_subtask"].handler(intent="拆分子任务", level="instant")

        assert result["success"] is True
        assert result["task_id"] == "t-created"
        assert result["level"] == "instant"
        call = manager.calls["create_task"][0]
        assert call["owner"] == "qq:10001"
        assert call["platform"] == "qq"
        assert call["stream_id"] == "qq:10001"
        assert call["level"] == TaskLevel.INSTANT
        assert call["caller_role"] == Role.USER

    @pytest.mark.asyncio
    async def test_create_failure_propagates_error(self, manager: FakeTaskManager) -> None:
        manager.fail.add("create_task")
        tools, _ = _build(manager, current_task=_current())
        result = await tools["create_subtask"].handler(intent="x")
        assert result == {"success": False, "error": "create failed"}


# ═══════════════════════════════════════════════════════════════════════════════
# inject_task
# ═══════════════════════════════════════════════════════════════════════════════

class TestInjectTask:
    @pytest.mark.asyncio
    async def test_no_current_task_returns_error(self, manager: FakeTaskManager) -> None:
        tools, _ = _build(manager, current_task=None)
        result = await tools["inject_task"].handler(task_id="t", instruction="i")
        assert result == {"success": False, "error": "无当前任务上下文"}

    @pytest.mark.asyncio
    async def test_missing_params_returns_error(self, manager: FakeTaskManager) -> None:
        tools, _ = _build(manager, current_task=_current())
        result = await tools["inject_task"].handler(task_id="", instruction="")
        assert result == {"success": False, "error": "缺少必需参数: task_id, instruction"}

    @pytest.mark.asyncio
    async def test_target_not_found_returns_error(
        self, store: TaskStore, manager: FakeTaskManager,
    ) -> None:
        tools, _ = _build(manager, current_task=_current())
        result = await tools["inject_task"].handler(
            task_id="ghost", instruction="继续",
        )
        assert result == {"success": False, "error": "目标任务不存在: ghost"}

    @pytest.mark.asyncio
    async def test_permission_denied_for_other_owner_task(
        self, store: TaskStore, manager: FakeTaskManager,
    ) -> None:
        """非 admin 不能向他人任务注入指令。"""
        await store.save(make_task("target-1", owner="qq:99999", status=TaskStatus.RUNNING))
        tools, injection = _build(
            manager, current_task=_current(owner="qq:10001"), current_role=Role.USER,
        )
        result = await tools["inject_task"].handler(
            task_id="target-1", instruction="继续",
        )
        assert result == {"success": False, "error": "权限不足：只能向自己的任务注入指令"}
        injection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admin_can_inject_other_owner_task(
        self, store: TaskStore, manager: FakeTaskManager,
    ) -> None:
        await store.save(make_task("target-2", owner="qq:99999", status=TaskStatus.RUNNING))
        tools, injection = _build(
            manager, current_task=_current(owner="qq:10001"), current_role=Role.ADMIN,
        )
        result = await tools["inject_task"].handler(
            task_id="target-2", instruction="继续",
        )
        assert result["success"] is True
        assert "指令已注入" in result["message"]
        injection.assert_awaited_once_with("target-2", "继续")

    @pytest.mark.asyncio
    async def test_own_task_injection_succeeds(
        self, store: TaskStore, manager: FakeTaskManager,
    ) -> None:
        await store.save(make_task("mine-1", owner="qq:10001", status=TaskStatus.RUNNING))
        tools, injection = _build(manager, current_task=_current(owner="qq:10001"))
        result = await tools["inject_task"].handler(
            task_id="mine-1", instruction="换个思路",
        )
        assert result["success"] is True
        injection.assert_awaited_once_with("mine-1", "换个思路")

    @pytest.mark.asyncio
    async def test_injection_failure_reports_not_running(
        self, store: TaskStore, manager: FakeTaskManager,
    ) -> None:
        await store.save(make_task("mine-2", owner="qq:10001", status=TaskStatus.RUNNING))
        tools, _ = _build(
            manager,
            current_task=_current(owner="qq:10001"),
            handle_injection=AsyncMock(return_value=False),
        )
        result = await tools["inject_task"].handler(
            task_id="mine-2", instruction="继续",
        )
        assert result == {"success": False, "message": "注入失败（任务 mine-2 未在运行）"}


# ═══════════════════════════════════════════════════════════════════════════════
# 工具元数据
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskMgmtMetadata:
    def test_three_discoverable_user_tools(self, manager: FakeTaskManager) -> None:
        tools, _ = _build(manager)
        assert set(tools) == {"list_my_tasks", "create_subtask", "inject_task"}
        for tool in tools.values():
            assert tool.visibility == "discoverable"
            assert tool.min_role == Role.USER
