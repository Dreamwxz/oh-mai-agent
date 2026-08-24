"""api_expose.py — 跨插件 API 端点 handler 行为测试。

test_api_expose_wiring.py 只验证 build_api_handlers 的结构注册；
本文件补齐 6 个端点（create/list/get/cancel/inject/history）的完整
行为路径，以及 _to_int / _parse_status 辅助函数。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from conftest import FakeTaskManager, make_task

from oh_mai_agent.api_expose import (
    _parse_status,
    _to_int,
    build_api_handlers,
)
from oh_mai_agent.config import MaibotAgentConfig
from oh_mai_agent.domain.task_record import TaskStatus
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.permission import PermissionResolver, Role


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def manager(store: TaskStore) -> FakeTaskManager:
    return FakeTaskManager(store)


@pytest.fixture
def handlers(
    manager: FakeTaskManager, default_resolver: PermissionResolver,
    default_config: MaibotAgentConfig,
) -> dict[str, Any]:
    """name → handler 描述符 字典。"""
    built = build_api_handlers(manager)
    return {h["name"]: h for h in built}


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseHelpers:
    def test_to_int(self) -> None:
        assert _to_int("5") == 5
        assert _to_int(3) == 3
        assert _to_int(None) == 0
        assert _to_int("abc") == 0
        assert _to_int("abc", default=7) == 7

    def test_parse_status(self) -> None:
        assert _parse_status("running") == TaskStatus.RUNNING
        assert _parse_status("") is None
        assert _parse_status(None) is None
        assert _parse_status("bogus") is None


# ═══════════════════════════════════════════════════════════════════════════════
# create
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateHandler:
    @pytest.mark.asyncio
    async def test_success_creates_task(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await handlers["create"]["handler"](
            intent="整理笔记", owner="qq:1", platform="qq",
            stream_id="qq:1",
        )
        assert result["success"] is True
        assert result["task_id"] == "t-created"
        # create 不接受 level 参数：任务固定为 agent 级（默认），不向 create_task 透传 level
        assert result["level"] == "agent"
        call = manager.calls["create_task"][0]
        assert call["caller_role"] == Role.ADMIN  # 跨插件调用按 ADMIN 处理
        assert "level" not in call

    @pytest.mark.asyncio
    async def test_passed_level_is_ignored(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        """调用方即使传入 level（如 instant）也被忽略，任务仍为 agent 级。"""
        result = await handlers["create"]["handler"](
            intent="x", owner="qq:1", platform="qq", stream_id="qq:1", level="instant",
        )
        assert result["success"] is True
        assert result["level"] == "agent"
        assert "level" not in manager.calls["create_task"][0]

    @pytest.mark.asyncio
    async def test_create_failure_returns_error(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.fail.add("create_task")
        result = await handlers["create"]["handler"](
            intent="x", owner="qq:1", platform="qq", stream_id="qq:1",
        )
        assert result == {"success": False, "error": "create failed"}

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.create_task = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        result = await handlers["create"]["handler"](
            intent="x", owner="qq:1", platform="qq", stream_id="qq:1",
        )
        assert result["success"] is False
        assert "boom" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# 调用方身份契约（_resolve_caller + 低权限身份锁定）
# ═══════════════════════════════════════════════════════════════════════════════

class TestCallerIdentity:
    @pytest.mark.asyncio
    async def test_create_with_user_identity_forces_own_owner_and_stream(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        """低权限身份（user）创建任务：未指定归属时以调用者自身
        owner/stream_id 创建，执行角色为用户、不持久化管理员角色。"""
        result = await handlers["create"]["handler"](
            intent="整理笔记", platform="qq",
            _caller_role="user", _caller_owner="qq:10001",
            _caller_stream_id="qq:10001",
        )
        assert result["success"] is True
        call = manager.calls["create_task"][0]
        assert call["owner"] == "qq:10001"  # 身份为权威，锁定为调用者自身
        assert call["stream_id"] == "qq:10001"
        assert call["caller_role"] == Role.USER  # 不升权

    @pytest.mark.asyncio
    async def test_create_with_user_identity_accepts_matching_owner(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        """低权限身份指定与自身一致的 owner/stream_id 时放行。"""
        result = await handlers["create"]["handler"](
            intent="x", owner="qq:10001", platform="qq", stream_id="qq:10001",
            _caller_role="user", _caller_owner="qq:10001",
            _caller_stream_id="qq:10001",
        )
        assert result["success"] is True
        call = manager.calls["create_task"][0]
        assert call["owner"] == "qq:10001"
        assert call["caller_role"] == Role.USER

    @pytest.mark.asyncio
    async def test_create_with_user_identity_rejects_other_owner(
        self, handlers: dict[str, Any],
    ) -> None:
        """低权限身份不能为他人创建任务（显式指定他人 owner 时拒绝）。"""
        result = await handlers["create"]["handler"](
            intent="x", owner="qq:99999", platform="qq", stream_id="qq:10001",
            _caller_role="user", _caller_owner="qq:10001",
            _caller_stream_id="qq:10001",
        )
        assert result == {"success": False, "error": "权限不足：只能以本人身份创建任务"}

    @pytest.mark.asyncio
    async def test_create_with_user_identity_rejects_other_stream(
        self, handlers: dict[str, Any],
    ) -> None:
        """低权限身份不能跨流创建任务。"""
        result = await handlers["create"]["handler"](
            intent="x", owner="qq:10001", platform="qq", stream_id="qq:other",
            _caller_role="user", _caller_owner="qq:10001",
            _caller_stream_id="qq:10001",
        )
        assert result == {"success": False, "error": "权限不足：只能在本会话流创建任务"}

    @pytest.mark.asyncio
    async def test_create_with_invalid_identity_falls_back_guest(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        """非法身份值按 GUEST 处理（fail-closed）：不按缺省 ADMIN 放大权限。

        handler 层把 GUEST 传给 create_task；真实的 guest 拒绝发生在
        TaskCrud.create_task（core/usecases/task_crud.py）。"""
        result = await handlers["create"]["handler"](
            intent="x", owner="qq:10001", platform="qq", stream_id="qq:10001",
            _caller_role="superadmin",
        )
        assert result["success"] is True  # FakeTaskManager 不执行 guest 拒绝
        assert manager.calls["create_task"][0]["caller_role"] == Role.GUEST

    @pytest.mark.asyncio
    async def test_create_with_admin_identity_keeps_full_control(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        """admin 身份保留完整指定能力（可为他流 / 他人建任务）。"""
        result = await handlers["create"]["handler"](
            intent="x", owner="qq:99999", platform="qq", stream_id="qq:other",
            _caller_role="admin", _caller_owner="qq:10001",
            _caller_stream_id="qq:10001",
        )
        assert result["success"] is True
        call = manager.calls["create_task"][0]
        assert call["owner"] == "qq:99999"
        assert call["stream_id"] == "qq:other"
        assert call["caller_role"] == Role.ADMIN

    @pytest.mark.asyncio
    async def test_list_with_user_identity_forces_own_owner(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        """低权限身份 list：owner 强制为调用者自身，不能按他人 owner 过滤查看。"""
        result = await handlers["list"]["handler"](
            owner="qq:99999", _caller_role="user", _caller_owner="qq:10001",
        )
        assert result["success"] is True
        call = manager.calls["list_tasks"][0]
        assert call["owner"] == "qq:10001"
        assert call["caller_role"] == Role.USER

    @pytest.mark.asyncio
    async def test_get_with_user_identity_forces_own_owner(
        self, store: TaskStore, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        await store.save(make_task("t1", owner="qq:10001"))
        result = await handlers["get"]["handler"](
            task_id="t1", owner="qq:99999", _caller_role="user",
            _caller_owner="qq:10001",
        )
        assert result["success"] is True
        call = manager.calls["get_task"][0]
        assert call["owner"] == "qq:10001"
        assert call["caller_role"] == Role.USER

    @pytest.mark.asyncio
    async def test_cancel_with_user_identity_forces_own_owner(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        """低权限身份 cancel：owner 锁定为自身，不能取消他人任务。"""
        result = await handlers["cancel"]["handler"](
            task_id="t1", owner="qq:99999", _caller_role="user",
            _caller_owner="qq:10001",
        )
        assert result == {"success": True, "message": "已取消"}
        call = manager.calls["cancel_task"][0]
        assert call["owner"] == "qq:10001"
        assert call["caller_role"] == Role.USER

    @pytest.mark.asyncio
    async def test_inject_with_user_identity_forces_own_owner(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await handlers["inject"]["handler"](
            task_id="t1", instruction="继续", owner="qq:99999",
            _caller_role="user", _caller_owner="qq:10001",
        )
        assert result == {"success": True, "message": "已注入"}
        call = manager.calls["modify_task"][0]
        assert call["owner"] == "qq:10001"
        assert call["caller_role"] == Role.USER

    @pytest.mark.asyncio
    async def test_history_with_user_identity_forces_own_owner(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await handlers["history"]["handler"](
            task_id="t1", owner="qq:99999", _caller_role="user",
            _caller_owner="qq:10001",
        )
        assert result["success"] is True
        call = manager.calls["task_history"][0]
        assert call["owner"] == "qq:10001"
        assert call["caller_role"] == Role.USER

    @pytest.mark.asyncio
    async def test_admin_identity_keeps_caller_provided_owner(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        """admin 身份（或显式 admin）保留调用方指定 owner 的能力。"""
        result = await handlers["list"]["handler"](
            owner="qq:99999", _caller_role="admin", _caller_owner="qq:10001",
        )
        assert result["success"] is True
        call = manager.calls["list_tasks"][0]
        assert call["owner"] == "qq:99999"
        assert call["caller_role"] == Role.ADMIN


# ═══════════════════════════════════════════════════════════════════════════════
# list
# ═══════════════════════════════════════════════════════════════════════════════

class TestListHandler:
    @pytest.mark.asyncio
    async def test_success_lists_tasks(
        self, store: TaskStore, handlers: dict[str, Any],
    ) -> None:
        await store.save(make_task("api-1", title="A"))
        await store.save(make_task("api-2", title="B"))
        result = await handlers["list"]["handler"](owner="qq:10001")
        assert result["success"] is True
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_invalid_status_returns_error(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await handlers["list"]["handler"](owner="qq:1", status="bogus")
        assert result["success"] is False
        assert "无效状态" in result["error"]
        assert "list_tasks" not in manager.calls

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.list_tasks = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        result = await handlers["list"]["handler"](owner="qq:1")
        assert result["success"] is False
        assert "boom" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# get / cancel / inject / history
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetHandler:
    @pytest.mark.asyncio
    async def test_success_returns_task_dict(
        self, store: TaskStore, handlers: dict[str, Any],
    ) -> None:
        await store.save(make_task("api-get", title="详情"))
        result = await handlers["get"]["handler"](task_id="api-get", owner="qq:10001")
        assert result["success"] is True
        assert result["task"]["id"] == "api-get"

    @pytest.mark.asyncio
    async def test_missing_task_returns_error(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await handlers["get"]["handler"](task_id="ghost", owner="qq:1")
        assert result == {"success": False, "error": "not found"}


class TestCancelHandler:
    @pytest.mark.asyncio
    async def test_success_cancels_task(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await handlers["cancel"]["handler"](task_id="t1", owner="qq:1")
        assert result == {"success": True, "message": "已取消"}

    @pytest.mark.asyncio
    async def test_failure_returns_message(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.fail.add("cancel_task")
        result = await handlers["cancel"]["handler"](task_id="t1", owner="qq:1")
        assert result == {"success": False, "message": "cancel failed"}


class TestInjectHandler:
    @pytest.mark.asyncio
    async def test_success_injects_instruction(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await handlers["inject"]["handler"](
            task_id="t1", instruction="继续", owner="qq:1",
        )
        assert result == {"success": True, "message": "已注入"}
        assert manager.calls["modify_task"][0]["inject_instruction"] == "继续"

    @pytest.mark.asyncio
    async def test_failure_returns_message(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.fail.add("modify_task")
        result = await handlers["inject"]["handler"](
            task_id="t1", instruction="x", owner="qq:1",
        )
        assert result == {"success": False, "message": "modify failed"}


class TestHistoryHandler:
    @pytest.mark.asyncio
    async def test_success_returns_history(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        result = await handlers["history"]["handler"](task_id="t1", owner="qq:1")
        assert result["success"] is True
        assert result["history"][0]["type"] == "status_change"

    @pytest.mark.asyncio
    async def test_failure_returns_error(
        self, handlers: dict[str, Any], manager: FakeTaskManager,
    ) -> None:
        manager.fail.add("task_history")
        result = await handlers["history"]["handler"](task_id="t1", owner="qq:1")
        assert result == {"success": False, "error": "history failed"}


# ═══════════════════════════════════════════════════════════════════════════════
# 端点元数据
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandlerMetadata:
    def test_six_public_endpoints(self, handlers: dict[str, Any]) -> None:
        assert set(handlers) == {"create", "list", "get", "cancel", "inject", "history"}
        for h in handlers.values():
            assert h["public"] is True
            assert h["version"] == "1"
            assert h["description"]
            assert callable(h["handler"])
