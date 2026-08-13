"""oh_mai_agent.core.task_manager 的测试 —— 任务创建与级别分类、
按角色过滤任务列表、指令注入、取消、插件 API 工具。

回归测试：
  1. plugin_api_tools 双重包装（flat 返回）
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from conftest import MockCtx, make_task

from oh_mai_agent.config import MaibotAgentConfig
from oh_mai_agent.permission import PermissionResolver, Role
from oh_mai_agent.core.scheduler import TaskScheduler
from oh_mai_agent.core.task_manager import TaskManager
from oh_mai_agent.core.usecases.task_control import TaskControl
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.executor.base import ExecutionContext
from oh_mai_agent.executor.instant import fail_task
from oh_mai_agent.permission import PermissionResolver, Role
from oh_mai_agent.prompt.builders.context_note import ContextNoteBuilder
from oh_mai_agent.prompt.service import PromptService
from oh_mai_agent.tools.planner.task_tools import _planner_owner
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry

import oh_mai_agent.tools.agent.plugin_api_tools as plugin_api_tools


def _prompt_service() -> PromptService:
    """使用真实 PromptManager 构造 PromptService，使 context_note 通过模板渲染。"""
    from pathlib import Path

    from oh_mai_agent.prompt.manager import PromptManager

    return PromptService(
        manager=PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates"),
        builders=[ContextNoteBuilder()],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助工具
# ═══════════════════════════════════════════════════════════════════════════════

async def _noop_executor(task: TaskRecord) -> None:
    pass


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def mock_ctx() -> MockCtx:
    return MockCtx()


@pytest.fixture
def config() -> MaibotAgentConfig:
    return MaibotAgentConfig()


@pytest.fixture
def resolver(config: MaibotAgentConfig) -> PermissionResolver:
    return PermissionResolver(config.permission)


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def scheduler(store: TaskStore, config: MaibotAgentConfig, command_bus: Any) -> TaskScheduler:
    return TaskScheduler(config.task, store, _noop_executor, command_bus=command_bus)


@pytest.fixture
def manager(
    mock_ctx: MockCtx, store: TaskStore, scheduler: TaskScheduler,
    registry: ToolRegistry, resolver: PermissionResolver, config: MaibotAgentConfig,
    prompt_service: Any, command_bus: Any,
) -> TaskManager:
    return TaskManager(
        ctx=mock_ctx, store=store, scheduler=scheduler,
        registry=registry, resolver=resolver, config=config,
        prompt_service=prompt_service, command_bus=command_bus,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 安装与工具注册
# ═══════════════════════════════════════════════════════════════════════════════

class TestSetup:
    @pytest.mark.asyncio
    async def test_setup_registers_tools(self, manager: TaskManager, registry: ToolRegistry) -> None:
        await manager.setup()
        names = registry.all_names()
        # 至少应注册任务管理类工具
        assert "list_my_tasks" in names
        assert "create_subtask" in names
        assert "inject_task" in names

    @pytest.mark.asyncio
    async def test_setup_registers_info_and_file_tools(
        self, manager: TaskManager, registry: ToolRegistry,
    ) -> None:
        await manager.setup()
        names = registry.all_names()
        # 信息类工具
        assert any("time" in n or "date" in n or "info" in n for n in names) or len(names) > 3


# ═══════════════════════════════════════════════════════════════════════════════
# 任务创建
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateTask:
    @pytest.mark.asyncio
    async def test_create_task_permission_denied_for_guest(
        self, manager: TaskManager,
    ) -> None:
        ok, result = await manager.create_task(
            intent="test", owner="qq:1", platform="qq",
            stream_id="qq:1", caller_role=Role.GUEST,
        )
        assert ok is False
        assert "guest" in str(result).lower()

    @pytest.mark.asyncio
    async def test_create_task_with_explicit_level(
        self, store: TaskStore, manager: TaskManager,
    ) -> None:
        ok, result = await manager.create_task(
            intent="测试任务", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.AGENT,
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.level == TaskLevel.AGENT
        assert result.owner == "qq:1"

        # 任务应已持久化到 store
        saved = await store.get(result.id)
        assert saved is not None

    @pytest.mark.asyncio
    async def test_create_task_with_llm_classification(
        self, mock_ctx: MockCtx, manager: TaskManager,
    ) -> None:
        """level 为 None 时由 LLM 分级；mock 返回 "ok" 未命中任何级别关键词，回退为 INSTANT。"""
        # 默认 MockLLM.generate 返回 {"response": "ok"} → 未命中 instant/agent 关键词 → 回退为 INSTANT
        ok, result = await manager.create_task(
            intent="多步骤复杂任务", owner="qq:1", platform="qq",
            stream_id="qq:1", level=None,
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        # LLM 返回 "ok" → 未命中 instant/agent → 回退为 INSTANT
        assert result.level == TaskLevel.INSTANT

    @pytest.mark.asyncio
    async def test_create_task_title_truncation(
        self, manager: TaskManager,
    ) -> None:
        long_intent = "A" * 100
        ok, result = await manager.create_task(
            intent=long_intent, owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.AGENT,
            caller_role=Role.USER,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert len(result.title) <= 80

    @pytest.mark.asyncio
    async def test_create_cron_task(
        self, manager: TaskManager,
    ) -> None:
        ok, result = await manager.create_task(
            intent="定时提醒", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.INSTANT,
            trigger=TriggerType.CRON, cron_expr="0 * * * *",
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.trigger_type == TriggerType.CRON
        assert result.cron_expr == "0 * * * *"

    @pytest.mark.asyncio
    async def test_create_delay_task(
        self, manager: TaskManager,
    ) -> None:
        ok, result = await manager.create_task(
            intent="延迟任务", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.INSTANT,
            trigger=TriggerType.DELAY, delay_seconds=300,
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.trigger_type == TriggerType.DELAY
        assert result.delay_seconds == 300

    @pytest.mark.asyncio
    async def test_create_task_accepts_reply_stream_id(
        self, manager: TaskManager,
    ) -> None:
        """传入 reply_stream_id 时，create_task 创建携带该字段的 TaskRecord。"""
        ok, result = await manager.create_task(
            intent="回复到别的流", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.INSTANT,
            reply_stream_id="qq:g:2",
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.reply_stream_id == "qq:g:2"

    @pytest.mark.asyncio
    async def test_create_task_reply_stream_id_defaults_none(
        self, manager: TaskManager,
    ) -> None:
        """未传入 reply_stream_id 时，TaskRecord.reply_stream_id 保持 None（向后兼容）。"""
        ok, result = await manager.create_task(
            intent="默认回复流", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.INSTANT,
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.reply_stream_id is None

    @pytest.mark.asyncio
    async def test_create_task_persists_to_real_store(
        self, mock_ctx: MockCtx, real_store: Any, registry: ToolRegistry,
         resolver: PermissionResolver, config: MaibotAgentConfig, command_bus: Any,
    ) -> None:
        """创建任务 → 验证已持久化到真实 SQLite store。"""
        await real_store.init()
        scheduler = TaskScheduler(config.task, real_store, _noop_executor, command_bus=command_bus)
        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=scheduler,
            registry=registry, resolver=resolver, config=config, command_bus=command_bus,
        )
        ok, result = await mgr.create_task(
            intent="持久化测试", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.AGENT,
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.level == TaskLevel.AGENT

        saved = await real_store.get(result.id)
        assert saved is not None
        assert saved.intent == "持久化测试"
        assert saved.level == TaskLevel.AGENT


# ═══════════════════════════════════════════════════════════════════════════════
# 任务列表（按角色过滤）
# ═══════════════════════════════════════════════════════════════════════════════

class TestListTasks:
    @pytest.mark.asyncio
    async def test_admin_sees_all(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1"))
        await store.save(make_task("t2", owner="qq:2"))

        tasks = await manager.list_tasks(caller_role=Role.ADMIN, owner="")
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_admin_sees_specific_owner(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1"))
        await store.save(make_task("t2", owner="qq:2"))

        tasks = await manager.list_tasks(caller_role=Role.ADMIN, owner="qq:1")
        assert len(tasks) == 1
        assert tasks[0]["owner"] == "qq:1"

    @pytest.mark.asyncio
    async def test_user_sees_only_own(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1"))
        await store.save(make_task("t2", owner="qq:2"))

        tasks = await manager.list_tasks(caller_role=Role.USER, owner="qq:1")
        assert len(tasks) == 1
        assert tasks[0]["owner"] == "qq:1"

    @pytest.mark.asyncio
    async def test_user_cannot_see_others(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1"))
        await store.save(make_task("t2", owner="qq:2"))

        tasks = await manager.list_tasks(caller_role=Role.USER, owner="qq:1")
        for t in tasks:
            assert t["owner"] == "qq:1"

    @pytest.mark.asyncio
    async def test_status_filter(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", status=TaskStatus.PENDING, owner="qq:1"))
        await store.save(make_task("t2", status=TaskStatus.COMPLETED, owner="qq:1"))

        tasks = await manager.list_tasks(
            caller_role=Role.ADMIN, owner="", status=TaskStatus.PENDING,
        )
        assert len(tasks) == 1
        assert tasks[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_format_status_included(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", status=TaskStatus.PENDING, owner="qq:1"))

        tasks = await manager.list_tasks(caller_role=Role.ADMIN, owner="")
        assert "format_status" in tasks[0]
        assert tasks[0]["format_status"] == "排队中"

    @pytest.mark.asyncio
    async def test_stream_id_filter(self, store: TaskStore, manager: TaskManager) -> None:
        """list_tasks(stream_id=...) 只返回该 stream 的任务。"""
        await store.save(make_task("t1", owner="qq:1", stream_id="qq:1"))
        await store.save(make_task("t2", owner="qq:1", stream_id="qq:2"))
        await store.save(make_task("t3", owner="qq:2", stream_id="qq:1"))

        tasks = await manager.list_tasks(
            caller_role=Role.ADMIN, owner="", stream_id="qq:1",
        )
        assert len(tasks) == 2
        assert {t["id"] for t in tasks} == {"t1", "t3"}

    @pytest.mark.asyncio
    async def test_stream_id_none_returns_all(self, store: TaskStore, manager: TaskManager) -> None:
        """list_tasks 不传 stream_id 时返回全部（默认行为不变）。"""
        await store.save(make_task("t1", owner="qq:1", stream_id="qq:1"))
        await store.save(make_task("t2", owner="qq:2", stream_id="qq:2"))

        tasks = await manager.list_tasks(caller_role=Role.ADMIN, owner="")
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_stream_id_no_match_returns_empty(
        self, store: TaskStore, manager: TaskManager,
    ) -> None:
        """无匹配 stream 的任务时返回空列表，不抛错。"""
        await store.save(make_task("t1", owner="qq:1", stream_id="qq:1"))

        tasks = await manager.list_tasks(
            caller_role=Role.ADMIN, owner="", stream_id="qq:999",
        )
        assert tasks == []

    @pytest.mark.asyncio
    async def test_stream_id_with_owner_filter(self, store: TaskStore, manager: TaskManager) -> None:
        """stream_id 与 owner 过滤叠加：只返回属于该用户且在该 stream 的任务。"""
        await store.save(make_task("t1", owner="qq:1", stream_id="qq:1"))
        await store.save(make_task("t2", owner="qq:2", stream_id="qq:1"))

        tasks = await manager.list_tasks(
            caller_role=Role.USER, owner="qq:1", stream_id="qq:1",
        )
        assert len(tasks) == 1
        assert tasks[0]["owner"] == "qq:1"

    @pytest.mark.asyncio
    async def test_crud_list_tasks_stream_id_filter(
        self, store: TaskStore, manager: TaskManager,
    ) -> None:
        """TaskCrud.list_tasks(stream_id=...) 直接过滤（数据层断言）。"""
        await store.save(make_task("t1", owner="qq:1", stream_id="qq:1"))
        await store.save(make_task("t2", owner="qq:1", stream_id="qq:2"))

        tasks = await manager._crud.list_tasks(
            caller_role=Role.ADMIN, owner="", stream_id="qq:2",
        )
        assert len(tasks) == 1
        assert tasks[0]["id"] == "t2"


# ═══════════════════════════════════════════════════════════════════════════════
# 获取任务
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_own_task(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1"))

        ok, result = await manager.get_task("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is True
        assert isinstance(result, TaskRecord)

    @pytest.mark.asyncio
    async def test_get_others_task_denied(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:2"))

        ok, result = await manager.get_task("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is False
        assert "权限" in str(result)

    @pytest.mark.asyncio
    async def test_admin_can_get_any(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:2"))

        ok, result = await manager.get_task("t1", caller_role=Role.ADMIN, owner="qq:1")
        assert ok is True

    @pytest.mark.asyncio
    async def test_not_found(self, manager: TaskManager) -> None:
        ok, result = await manager.get_task("no-such", caller_role=Role.ADMIN, owner="qq:1")
        assert ok is False
        assert "不存在" in str(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 修改任务
# ═══════════════════════════════════════════════════════════════════════════════

class TestModifyTask:
    @pytest.mark.asyncio
    async def test_modify_own_task(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1", intent="旧意图", level=TaskLevel.AGENT))

        ok, msg = await manager.modify_task(
            "t1", caller_role=Role.USER, owner="qq:1",
            new_intent="新意图",
        )
        assert ok is True

        updated = await store.get("t1")
        assert updated is not None
        assert updated.intent == "新意图"

    @pytest.mark.asyncio
    async def test_modify_others_task_denied(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:2"))

        ok, msg = await manager.modify_task(
            "t1", caller_role=Role.USER, owner="qq:1",
            new_intent="新意图",
        )
        assert ok is False
        assert "权限" in msg

    @pytest.mark.asyncio
    async def test_modify_update_priority(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1", priority=0))

        ok, msg = await manager.modify_task(
            "t1", caller_role=Role.ADMIN, owner="qq:1",
            priority=10,
        )
        assert ok is True

        updated = await store.get("t1")
        assert updated is not None
        assert updated.priority == 10

    @pytest.mark.asyncio
    async def test_inject_instruction_only_running(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1", status=TaskStatus.PENDING))

        ok, msg = await manager.modify_task(
            "t1", caller_role=Role.ADMIN, owner="qq:1",
            inject_instruction="指令",
        )
        assert ok is False
        assert "无法注入" in msg

    @pytest.mark.asyncio
    async def test_modify_persists_to_real_store(
        self, mock_ctx: MockCtx, real_store: Any, registry: ToolRegistry,
         resolver: PermissionResolver, config: MaibotAgentConfig, command_bus: Any,
    ) -> None:
        """修改任务 → 验证已持久化到真实 SQLite store。"""
        await real_store.init()
        scheduler = TaskScheduler(config.task, real_store, _noop_executor, command_bus=command_bus)
        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=scheduler,
            registry=registry, resolver=resolver, config=config, command_bus=command_bus,
        )
        task = make_task("t1", owner="qq:1", intent="旧意图", level=TaskLevel.AGENT)
        await real_store.save(task)

        ok, msg = await mgr.modify_task(
            "t1", caller_role=Role.USER, owner="qq:1",
            new_intent="新意图",
        )
        assert ok is True

        updated = await real_store.get("t1")
        assert updated is not None
        assert updated.intent == "新意图"


# ═══════════════════════════════════════════════════════════════════════════════
# 取消任务
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelTask:
    @pytest.mark.asyncio
    async def test_cancel_own_task(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1", status=TaskStatus.PENDING))

        ok, msg = await manager.cancel_task("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is True
        assert "取消" in msg

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_others_denied(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:2"))

        ok, msg = await manager.cancel_task("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is False
        assert "权限" in msg

    @pytest.mark.asyncio
    async def test_cancel_persists_to_real_store(
        self, mock_ctx: MockCtx, real_store: Any, registry: ToolRegistry,
         resolver: PermissionResolver, config: MaibotAgentConfig, command_bus: Any,
    ) -> None:
        """取消任务 → 验证已持久化到真实 SQLite store。"""
        await real_store.init()
        scheduler = TaskScheduler(config.task, real_store, _noop_executor, command_bus=command_bus)
        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=scheduler,
            registry=registry, resolver=resolver, config=config, command_bus=command_bus,
        )
        task = make_task("t1", owner="qq:1", status=TaskStatus.PENDING)
        await real_store.save(task)

        ok, msg = await mgr.cancel_task("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is True
        assert "取消" in msg

        updated = await real_store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.CANCELLED


# ═══════════════════════════════════════════════════════════════════════════════
# 任务历史
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskHistory:
    @pytest.mark.asyncio
    async def test_own_history(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1"))
        await store.append_history("t1", {"round": 1})

        ok, result = await manager.task_history("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is True
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_history_others_denied(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:2"))

        ok, result = await manager.task_history("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is False
        assert "权限" in str(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 暂停 / 恢复
# ═══════════════════════════════════════════════════════════════════════════════

class TestPauseResumeTask:
    @pytest.mark.asyncio
    async def test_pause_own_running_task(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1", status=TaskStatus.RUNNING))

        ok, msg = await manager.pause_task("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is True

    @pytest.mark.asyncio
    async def test_resume_own_paused_task(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("t1", owner="qq:1", status=TaskStatus.PAUSED))

        ok, msg = await manager.resume_task("t1", caller_role=Role.USER, owner="qq:1")
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════════
# 插件 API 工具 — 回归测试：双重包装
# ═══════════════════════════════════════════════════════════════════════════════

class TestPluginApiTools:
    @pytest.mark.asyncio
    async def test_flat_return_not_double_wrapped(self) -> None:
        """回归测试：ctx.api.call() 直接返回目标 API 的结果；handler 原样透传，避免双重包装。"""

        class MockApi:
            async def list(self):
                return [{"api_name": "test.echo", "description": "Echo API"}]

            async def call(self, api_name, **kwargs):
                return {"success": True, "result": "data"}

        mock_api = MockApi()
        tools = await plugin_api_tools.refresh_plugin_api_tools(mock_api)

        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "call_test_echo"

        # 通过 handler 执行
        result = await tool.handler(args={"msg": "hello"})
        assert result == {"success": True, "result": "data"}

    def test_normalize_api_list_list(self) -> None:
        raw = [{"api_name": "a"}, {"api_name": "b"}]
        result = plugin_api_tools._normalize_api_list(raw)
        assert len(result) == 2

    def test_normalize_api_list_dict(self) -> None:
        raw = {"apis": [{"api_name": "a"}]}
        result = plugin_api_tools._normalize_api_list(raw)
        assert len(result) == 1

    def test_normalize_api_list_invalid(self) -> None:
        assert plugin_api_tools._normalize_api_list(None) == []
        assert plugin_api_tools._normalize_api_list("invalid") == []
        assert plugin_api_tools._normalize_api_list({}) == []


# ═══════════════════════════════════════════════════════════════════════════════
# LLM 标题生成
# ═══════════════════════════════════════════════════════════════════════════════

class TestLLMTitleGeneration:
    @pytest.mark.asyncio
    async def test_custom_title_callback(self, mock_ctx: MockCtx, store: TaskStore,
                                          scheduler: TaskScheduler, registry: ToolRegistry,
                                           resolver: PermissionResolver, config: MaibotAgentConfig, command_bus: Any) -> None:
        async def _title_cb(intent: str) -> str:
            return f"自定义标题: {intent[:20]}"

        mgr = TaskManager(
            ctx=mock_ctx, store=store, scheduler=scheduler,
            registry=registry, resolver=resolver, config=config,
             llm_title=_title_cb, command_bus=command_bus,
        )

        ok, result = await mgr.create_task(
            intent="测试标题生成", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.AGENT,
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.title == "自定义标题: 测试标题生成"

    @pytest.mark.asyncio
    async def test_llm_title_injected_and_fallback(
        self, mock_ctx: MockCtx, store: TaskStore,
        scheduler: TaskScheduler, registry: ToolRegistry,
         resolver: PermissionResolver, config: MaibotAgentConfig, command_bus: Any,
    ) -> None:
        """给定：llm_title 回调调用 mock_ctx.llm.generate
           当：LLM 返回标题
           则：task.title 与返回的标题一致
           当：LLM 抛出异常
           则：标题回退为 intent[:40]
        """
        async def _llm_title_cb(intent: str) -> str:
            try:
                result = await mock_ctx.llm.generate(
                    prompt=f"Generate title for: {intent}",
                    model="utils",
                    timeout_ms=60000,
                )
                response = str(result.get("response", "")).strip()
                return response if response else intent[:40]
            except Exception:
                return intent[:40]

        # ── 场景 1：LLM 成功 ──────────────────────────────────
        mock_ctx.llm.set_generate_response("整理的聊天记录摘要")
        mgr = TaskManager(
            ctx=mock_ctx, store=store, scheduler=scheduler,
            registry=registry, resolver=resolver, config=config,
             llm_title=_llm_title_cb, command_bus=command_bus,
        )
        ok, result = await mgr.create_task(
            intent="整理聊天记录", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.AGENT,
            caller_role=Role.ADMIN,
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.title == "整理的聊天记录摘要"

        # ── 场景 2：LLM 抛异常 → 回退 intent[:40] ─────────────
        async def _failing_cb(intent: str) -> str:
            raise RuntimeError("LLM unavailable")

        mgr2 = TaskManager(
            ctx=mock_ctx, store=store, scheduler=scheduler,
            registry=registry, resolver=resolver, config=config,
             llm_title=_failing_cb, command_bus=command_bus,
        )
        ok2, result2 = await mgr2.create_task(
            intent="整理聊天记录", owner="qq:1", platform="qq",
            stream_id="qq:1", level=TaskLevel.AGENT,
            caller_role=Role.ADMIN,
        )
        assert ok2 is True
        assert isinstance(result2, TaskRecord)
        assert result2.title == "整理聊天记录"  # 回退为 intent[:40]


# ═══════════════════════════════════════════════════════════════════════════════
# FakeScheduler — 用于 instant/agent 执行测试的最小调度器 mock
# ═══════════════════════════════════════════════════════════════════════════════

class FakeScheduler:
    """用于测试 execute_instant / execute_agent 的 TaskScheduler 最小 fake 实现。"""

    def __init__(self) -> None:
        self.completed: list[str] = []
        self.enqueued: list[TaskRecord] = []
        self._running: set[str] = set()

    async def on_task_completed(self, task: TaskRecord) -> None:
        self.completed.append(task.id)
        self._running.discard(task.id)

    async def enqueue(self, task: TaskRecord) -> None:
        self.enqueued.append(task)

    async def cancel(self, task_id: str) -> bool:
        return False

    async def pause(self, task_id: str) -> bool:
        return False

    async def resume(self, task_id: str) -> bool:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# instant / agent 执行
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstantExecution:
    @pytest.mark.asyncio
    async def test_execute_instant_sends_polished_message(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
         real_store: TaskStore, command_bus: Any,
    ) -> None:
        await real_store.init()
        sched = FakeScheduler()
        reg = ToolRegistry()
        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=sched,
            registry=reg, resolver=resolver, config=config,
            prompt_service=_prompt_service(), command_bus=command_bus,
        )
        task = make_task(
            "l1", title="提醒", intent="该喝水了",
            level=TaskLevel.INSTANT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            status=TaskStatus.RUNNING,
        )
        await real_store.save(task)
        await mgr.execute_instant(task)

        assert mock_ctx._sent_messages, "Instant should send a message"
        assert isinstance(mock_ctx._sent_messages[0]["text"], str) and len(mock_ctx._sent_messages[0]["text"]) > 0, \
            "Sent text should be non-empty"
        assert sched.completed == ["l1"], sched.completed
        saved = await real_store.get("l1")
        assert saved is not None and saved.status == TaskStatus.COMPLETED, \
            f"Expected COMPLETED, got {saved.status if saved else 'None'}"

    @pytest.mark.asyncio
    async def test_execute_instant_routes_reply_to_reply_stream_id(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
         real_store: TaskStore, command_bus: Any,
    ) -> None:
        """reply_stream_id 覆盖 stream_id：InstantExecutor 应把消息发到目标流。"""
        await real_store.init()
        sched = FakeScheduler()
        reg = ToolRegistry()
        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=sched,
            registry=reg, resolver=resolver, config=config,
            prompt_service=_prompt_service(), command_bus=command_bus,
        )
        task = make_task(
            "l2", title="提醒", intent="该喝水了",
            level=TaskLevel.INSTANT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            reply_stream_id="qq:g:2",
            status=TaskStatus.RUNNING,
        )
        await real_store.save(task)
        await mgr.execute_instant(task)

        assert mock_ctx._sent_messages, "Instant should send a message"
        assert mock_ctx._sent_messages[0]["stream_id"] == "qq:g:2", \
            f"Expected reply to qq:g:2, got {mock_ctx._sent_messages[0]['stream_id']}"

    @pytest.mark.asyncio
    async def test_dispatch_reply_instant_routes_to_reply_stream_id(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
         real_store: TaskStore, command_bus: Any,
    ) -> None:
        """_dispatch_reply_instant 创建的 reply 任务应携带 reply_stream_id 作为目标流。"""
        await real_store.init()
        sched = FakeScheduler()
        control = TaskControl(
            store=real_store,
            scheduler=sched,
            command_bus=command_bus,
            executor_factory=MagicMock(),
            config=config,
            prompt_manager=None,
            prompt_service=_prompt_service(),
            ctx=mock_ctx,
        )
        task = make_task(
            "l3", title="回复", intent="收到",
            level=TaskLevel.AGENT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            reply_stream_id="qq:g:2",
        )
        await control._dispatch_reply_instant(task, "已处理")

        assert sched.enqueued, "Reply instant should be enqueued"
        reply_task = sched.enqueued[0]
        assert reply_task.stream_id == "qq:g:2", \
            f"Expected reply task stream qq:g:2, got {reply_task.stream_id}"
        assert reply_task.owner == "qq:1"
        assert reply_task.platform == "qq"

    @pytest.mark.asyncio
    async def test_dispatch_reply_instant_marks_reply_metadata(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
         real_store: TaskStore, command_bus: Any,
    ) -> None:
        """_dispatch_reply_instant 创建的 reply 任务 metadata 中包含 _is_reply=True。"""
        await real_store.init()
        sched = FakeScheduler()
        control = TaskControl(
            store=real_store,
            scheduler=sched,
            command_bus=command_bus,
            executor_factory=MagicMock(),
            config=config,
            prompt_manager=None,
            prompt_service=_prompt_service(),
            ctx=mock_ctx,
        )
        task = make_task(
            "reply-meta", title="回复", intent="收到",
            level=TaskLevel.AGENT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            reply_stream_id="qq:g:2",
        )
        await control._dispatch_reply_instant(task, "已处理")

        assert sched.enqueued, "Reply instant should be enqueued"
        reply_task = sched.enqueued[0]
        assert reply_task.metadata.get("_is_reply") is True, \
            f"Expected metadata._is_reply=True, got {reply_task.metadata}"

    @pytest.mark.asyncio
    async def test_execute_instant_cross_stream_appends_context(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
         real_store: TaskStore, command_bus: Any,
    ) -> None:
        """Cross-stream reply (reply_stream_id set) → motivation 传递给 send_final_reply → context.append 被调用（两条：纯文本 + XML 注释）。"""
        await real_store.init()
        sched = FakeScheduler()
        reg = ToolRegistry()
        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=sched,
            registry=reg, resolver=resolver, config=config,
            prompt_service=_prompt_service(), command_bus=command_bus,
        )
        task = make_task(
            "reply-cross", title="回复", intent="因小泽委托处理完毕",
            level=TaskLevel.INSTANT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            reply_stream_id="qq:g:2",
            status=TaskStatus.RUNNING,
        )
        await real_store.save(task)
        await mgr.execute_instant(task)

        assert len(mock_ctx.maisaka.appends) == 2, \
            f"Expected 2 context.appends (pure text + XML note), got {len(mock_ctx.maisaka.appends)}"
        entry = mock_ctx.maisaka.appends[1]
        assert "因小泽委托处理完毕" in entry["visible_text"], \
            f"visible_text should contain intent: {entry['visible_text']}"
        assert entry["stream_id"] == "qq:g:2", \
            f"context note should go to reply_target: {entry['stream_id']}"
        assert entry["source_kind"] == "plugin:oh-mai-agent:task-reply"

    @pytest.mark.asyncio
    async def test_execute_instant_no_reply_stream_no_context_append(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
         real_store: TaskStore, command_bus: Any,
    ) -> None:
        """Plain instant 任务 (无 reply_stream_id, 无 _is_reply) → motivation=None → 仅有纯文本 context.append。"""
        await real_store.init()
        sched = FakeScheduler()
        reg = ToolRegistry()
        # worker 内 PolishService 使用全量 builders（润色成功），LLM 响应即发送文本
        mock_ctx.llm.set_generate_response("该喝水了")
        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=sched,
            registry=reg, resolver=resolver, config=config,
            prompt_service=_prompt_service(), command_bus=command_bus,
        )
        task = make_task(
            "plain-instant", title="提醒", intent="该喝水了",
            level=TaskLevel.INSTANT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            status=TaskStatus.RUNNING,
        )
        await real_store.save(task)
        await mgr.execute_instant(task)

        assert mock_ctx._sent_messages, "Plain instant should still send a message"
        assert len(mock_ctx.maisaka.appends) == 1, \
            f"Expected 1 context.append (pure text only), got {len(mock_ctx.maisaka.appends)}"
        entry = mock_ctx.maisaka.appends[0]
        assert entry["visible_text"] == "该喝水了"
        assert "message_id" not in entry or entry.get("message_id") == ""
        assert entry["source_kind"] == "plugin:oh-mai-agent:task-reply"

    @pytest.mark.asyncio
    async def test_fail_task_sends_failure_to_reply_stream_id(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
         real_store: TaskStore, command_bus: Any,
    ) -> None:
        """fail_task(send_message=True) 的失败消息应发到 reply_stream_id 而非原流。"""
        await real_store.init()
        sched = FakeScheduler()
        reg = ToolRegistry()
        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=sched,
            registry=reg, resolver=resolver, config=config,
            prompt_service=_prompt_service(), command_bus=command_bus,
        )
        task = make_task(
            "l4", title="失败", intent="boom",
            level=TaskLevel.INSTANT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            reply_stream_id="qq:g:2",
            status=TaskStatus.RUNNING,
        )
        task.metadata["_error"] = "boom"
        await real_store.save(task)
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=real_store, scheduler=sched, config=config,
        )
        await fail_task(task, real_store, sched, exec_ctx, send_message=True)

        assert mock_ctx._sent_messages, "fail_task should send a message"
        assert mock_ctx._sent_messages[0]["stream_id"] == "qq:g:2", \
            f"Expected failure to qq:g:2, got {mock_ctx._sent_messages[0]['stream_id']}"
        saved = await real_store.get("l4")
        assert saved is not None and saved.status == TaskStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 任务 ID 前缀解析
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolveTaskById:
    @pytest.mark.asyncio
    async def test_exact_match(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", owner="qq:1"))
        ok, result = await manager.get_task(
            "aaaaaaaa-1111-2222-3333-444444444444",
            caller_role=Role.ADMIN,
            owner="",
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.id == "aaaaaaaa-1111-2222-3333-444444444444"

    @pytest.mark.asyncio
    async def test_unique_prefix(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", title="B"))
        ok, result = await manager.get_task(
            "aaaabbbb", caller_role=Role.ADMIN, owner="",
        )
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.title == "B"

    @pytest.mark.asyncio
    async def test_ambiguous_prefix(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444", title="A"))
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", title="B"))
        ok, result = await manager.get_task("aaaa", caller_role=Role.ADMIN, owner="")
        assert ok is False
        assert "多个" in str(result)

    @pytest.mark.asyncio
    async def test_no_match(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaaaaaa-1111-2222-3333-444444444444"))
        ok, result = await manager.get_task("zzzz", caller_role=Role.ADMIN, owner="")
        assert ok is False
        assert "不存在" in str(result)

    @pytest.mark.asyncio
    async def test_get_task_with_prefix(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", title="B", owner="qq:1"))
        ok, result = await manager.get_task("aaaabbbb", caller_role=Role.ADMIN, owner="qq:1")
        assert ok is True
        assert isinstance(result, TaskRecord)
        assert result.title == "B"

    @pytest.mark.asyncio
    async def test_get_task_prefix_permission_denied(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", title="B", owner="qq:2"))
        ok, result = await manager.get_task("aaaabbbb", caller_role=Role.USER, owner="qq:1")
        assert ok is False
        assert "权限" in str(result)

    @pytest.mark.asyncio
    async def test_cancel_task_with_prefix(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", owner="qq:1", status=TaskStatus.PENDING))
        ok, msg = await manager.cancel_task("aaaabbbb", caller_role=Role.USER, owner="qq:1")
        assert ok is True

    @pytest.mark.asyncio
    async def test_history_with_prefix(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", owner="qq:1"))
        await store.append_history("aaaabbbb-1111-2222-3333-444444444444", {"round": 1})
        ok, result = await manager.task_history("aaaabbbb", caller_role=Role.USER, owner="qq:1")
        assert ok is True
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_modify_task_with_prefix(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", owner="qq:1", intent="旧"))
        ok, msg = await manager.modify_task(
            "aaaabbbb", caller_role=Role.USER, owner="qq:1", new_intent="新")
        assert ok is True
        updated = await store.get("aaaabbbb-1111-2222-3333-444444444444")
        assert updated is not None
        assert updated.intent == "新"

    @pytest.mark.asyncio
    async def test_pause_task_with_prefix(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", owner="qq:1", status=TaskStatus.RUNNING))
        ok, msg = await manager.pause_task("aaaabbbb", caller_role=Role.USER, owner="qq:1")
        assert ok is True

    @pytest.mark.asyncio
    async def test_resume_task_with_prefix(self, store: TaskStore, manager: TaskManager) -> None:
        await store.save(make_task("aaaabbbb-1111-2222-3333-444444444444", owner="qq:1", status=TaskStatus.PAUSED))
        ok, msg = await manager.resume_task("aaaabbbb", caller_role=Role.USER, owner="qq:1")
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════════
# instant 失败时发送消息
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstantFailSend:
    @pytest.mark.asyncio
    async def test_instant_fail_sends_message(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
         real_store: TaskStore, command_bus: Any,
    ) -> None:
        """当 instant 任务失败时（如持久化错误），应发送失败消息。"""
        await real_store.init()
        sched = FakeScheduler()

        save_count = [0]
        _original_save = real_store.save

        async def _failing_save(task: TaskRecord) -> None:
            save_count[0] += 1
            if save_count[0] == 1:
                raise RuntimeError("persistence failure")
            await _original_save(task)

        real_store.save = _failing_save

        mgr = TaskManager(
            ctx=mock_ctx, store=real_store, scheduler=sched,
            registry=ToolRegistry(), resolver=resolver, config=config, command_bus=command_bus,
        )
        task = make_task(
            "l1", title="提醒", intent="该喝水了",
            level=TaskLevel.INSTANT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            status=TaskStatus.RUNNING,
        )
        await _original_save(task)
        save_count[0] = 0

        await mgr.execute_instant(task)

        # execute_instant 捕获保存异常后调用 fail_task（来自 executor/instant.py）
        assert len(mock_ctx._sent_messages) >= 1, (
            f"instant fail should send at least one message: {mock_ctx._sent_messages}"
        )
        saved = await real_store.get("l1")
        assert saved is not None and saved.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_fail_task_sends_message_directly(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
        real_store: TaskStore,
    ) -> None:
        """fail_task（executor.instant）在 send_message=True 时发送润色后的失败消息。"""
        await real_store.init()
        sched = FakeScheduler()

        # 让 LLM 原样返回提示词文本（不真正润色）
        mock_ctx.llm.set_generate_response("任务失败了：测试错误原因")

        task = make_task(
            "l1", title="提醒", intent="test",
            level=TaskLevel.INSTANT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            status=TaskStatus.RUNNING,
        )
        task.metadata["_error"] = "测试错误原因"
        await real_store.save(task)

        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=real_store, scheduler=sched, config=config,
        )
        await fail_task(task, real_store, sched, exec_ctx, send_message=True)

        assert mock_ctx._sent_messages, "send_message=True should send"
        assert "失败" in mock_ctx._sent_messages[0]["text"], (
            f"Should contain 失败: {mock_ctx._sent_messages}"
        )
        saved = await real_store.get("l1")
        assert saved is not None and saved.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_fail_task_without_send_message(
        self, mock_ctx: MockCtx, resolver: PermissionResolver, config: MaibotAgentConfig,
        real_store: TaskStore,
    ) -> None:
        """fail_task（executor.instant）在 send_message=False 时不发送任何消息。"""
        await real_store.init()
        sched = FakeScheduler()

        task = make_task(
            "l1", title="提醒", intent="test",
            level=TaskLevel.INSTANT, owner="qq:1",
            stream_id="qq:g:1", platform="qq",
            status=TaskStatus.RUNNING,
        )
        await real_store.save(task)

        sent_before = len(mock_ctx._sent_messages)
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=real_store, scheduler=sched, config=config,
        )
        await fail_task(task, real_store, sched, exec_ctx, send_message=False)

        assert len(mock_ctx._sent_messages) == sent_before, (
            "send_message=False should not send"
        )
        saved = await real_store.get("l1")
        assert saved is not None and saved.status == TaskStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# _planner_owner 语义
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlannerOwner:
    """_planner_owner 流类型感知测试。"""

    def test_private_stream_returns_stream_id(self) -> None:
        """私聊流：返回 stream_id 本身（委托用户即 owner）。"""
        assert _planner_owner("qq:1591625223") == "qq:1591625223"

    def test_group_stream_returns_planner_prefix(self) -> None:
        """群聊流：返回 planner:{stream_id}（保留 Planner 语境）。"""
        assert _planner_owner("qq:group:123456") == "planner:qq:group:123456"


# ═══════════════════════════════════════════════════════════════════════════════
# 修改任务权限
# ═══════════════════════════════════════════════════════════════════════════════


class TestModifyTaskPermission:
    """modify_task 权限收紧测试：inject_instruction 仅 ADMIN。"""

    @pytest.mark.asyncio
    async def test_inject_instruction_user_denied(self, store: TaskStore, manager: TaskManager) -> None:
        """USER 角色调用 inject_instruction 被拒。"""
        await store.save(make_task("t1", owner="qq:1", status=TaskStatus.RUNNING))
        ok, msg = await manager.modify_task(
            "t1", caller_role=Role.USER, owner="qq:1",
            inject_instruction="指令",
        )
        assert ok is False
        assert "仅管理员" in msg

    @pytest.mark.asyncio
    async def test_inject_instruction_admin_allowed(self, store: TaskStore, manager: TaskManager) -> None:
        """ADMIN 角色调用 inject_instruction 成功。"""
        await store.save(make_task("t1", owner="qq:1", status=TaskStatus.RUNNING))
        ok, msg = await manager.modify_task(
            "t1", caller_role=Role.ADMIN, owner="qq:1",
            inject_instruction="指令",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_new_intent_by_user_still_allowed(self, store: TaskStore, manager: TaskManager) -> None:
        """USER 角色修改 new_intent 仍可（现状不回归）。"""
        await store.save(make_task("t1", owner="qq:1", intent="旧"))
        ok, msg = await manager.modify_task(
            "t1", caller_role=Role.USER, owner="qq:1",
            new_intent="新",
        )
        assert ok is True
        updated = await store.get("t1")
        assert updated is not None
        assert updated.intent == "新"


# ═══════════════════════════════════════════════════════════════════════════════
# handle_user_reply 私聊流匹配
# ═══════════════════════════════════════════════════════════════════════════════


class TestHandleUserReplyPrivateStream:
    """私聊流 planner 任务 WAITING_INPUT → 委托用户回复匹配。"""

    @pytest.mark.asyncio
    async def test_private_stream_owner_matches(
        self, real_store: TaskStore, command_bus: Any,
        mock_ctx: MockCtx, resolver: PermissionResolver,
        config: MaibotAgentConfig,
    ) -> None:
        """私聊流：owner=qq:1591625223，用户回复 qq:1591625223 → 匹配成功。"""
        await real_store.init()
        sched = FakeScheduler()
        control = TaskControl(
            store=real_store, scheduler=sched,
            command_bus=command_bus, executor_factory=MagicMock(),
            config=config, prompt_manager=None,
            prompt_service=None, ctx=mock_ctx,
        )
        task = make_task(
            "t1", owner="qq:1591625223",
            stream_id="qq:1591625223", platform="qq",
            status=TaskStatus.WAITING_INPUT,
        )
        await real_store.save(task)

        # 委托用户回复
        await control.handle_user_reply(
            stream_id="qq:1591625223",
            user_id="1591625223",
            reply="继续",
        )

        # 任务 metadata 已写入 _user_reply
        updated = await real_store.get("t1")
        assert updated is not None
        assert updated.metadata.get("_user_reply") == "继续"
