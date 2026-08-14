"""lifecycle.py 与 plugin.py 生命周期测试。

覆盖：
- ``load_plugin``：插件唯一组装点（TaskStore / Registry / Scheduler / TaskManager /
  MCP / PlannerBoard / 动态 API 全套初始化）。
- ``apply_config_update``：配置热更新传播（权限、调度器、任务管理器、MCP、看板）。
- ``llm_title``：LLM 标题生成与降级路径。
- ``recover_active_tasks``：重启后 SCHEDULED / RUNNING / WAITING_INPUT 的恢复策略。
- ``reload_mcp_if_changed``：MCP 配置变更检测与重建。
- ``plugin.on_load / on_unload / on_config_update`` 薄壳委托。

约定与项目一致：mock LLM 与 transport，不 mock 持久化（真实 sqlite）；
MCP 配置禁用（避免测试 spawn MCP stdio 子进程）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from conftest import MockCtx, make_task

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF

from oh_mai_agent.bus.command_bus import TaskCommandBus
from oh_mai_agent.bus.transport import LoopbackTransport
from oh_mai_agent.config import MaibotAgentConfig, MCPConfig, TaskConfig
from oh_mai_agent.core.scheduler import TaskScheduler
from oh_mai_agent.core.task_manager import TaskManager
from oh_mai_agent.domain.task_record import TaskStatus, TriggerType
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.lifecycle import (
    apply_config_update,
    llm_title,
    load_plugin,
    recover_active_tasks,
    reload_mcp_if_changed,
)
from oh_mai_agent.permission import PermissionResolver
from oh_mai_agent.planner_hooks import PlannerBoard
from oh_mai_agent.plugin import MaibotAgentPlugin
from oh_mai_agent.prompt.manager import PromptManager
from oh_mai_agent.prompt.service import PromptService
from oh_mai_agent.tools.mcp.provider import MCPManager
from oh_mai_agent.tools.registry import ToolRegistry


def _mcp_disabled_config() -> MaibotAgentConfig:
    """MCP 禁用的完整配置：load_plugin 中不 spawn 任何 MCP 子进程。"""
    return MaibotAgentConfig(mcp=MCPConfig(enabled=False))


@pytest.fixture
def plugin(tmp_path: Path) -> MaibotAgentPlugin:
    """注入 MockCtx 与禁用 MCP 配置的真实 MaibotAgentPlugin 实例。"""
    p = MaibotAgentPlugin()
    mock_ctx = MockCtx()
    mock_ctx.paths = SimpleNamespace(data_dir=tmp_path)
    mock_ctx.api = SimpleNamespace(
        replace_dynamic_apis=AsyncMock(return_value=True),
        list=AsyncMock(return_value=[]),
    )
    p._set_context(mock_ctx)
    p._plugin_config_instance = _mcp_disabled_config()
    return p


@pytest_asyncio.fixture
async def loaded_plugin(plugin: MaibotAgentPlugin) -> MaibotAgentPlugin:
    """执行过 load_plugin 的插件；拆除时停止调度器 / MCP 并关闭存储。"""
    await load_plugin(plugin)
    yield plugin
    if hasattr(plugin, "_scheduler") and plugin._scheduler is not None:
        await plugin._scheduler.stop()
    if hasattr(plugin, "_mcp") and plugin._mcp is not None:
        await plugin._mcp.stop()
    if hasattr(plugin, "_store") and plugin._store is not None:
        await plugin._store.close()


# ═══════════════════════════════════════════════════════════════════════════════
# load_plugin — 完整组装路径
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadPlugin:
    @pytest.mark.asyncio
    async def test_load_plugin_assembles_all_components(
        self, loaded_plugin: MaibotAgentPlugin,
    ) -> None:
        """load_plugin 后所有运行时组件就位且相互连通。"""
        p = loaded_plugin

        # 1. 存储 / 权限 / 注册表
        assert isinstance(p._store, TaskStore)
        assert isinstance(p._registry, ToolRegistry)
        assert isinstance(p._resolver, PermissionResolver)

        # 2. 命令总线
        assert isinstance(p._transport, LoopbackTransport)
        assert isinstance(p._command_bus, TaskCommandBus)

        # 3. 调度器已启动
        assert isinstance(p._scheduler, TaskScheduler)
        assert p._scheduler._check_task is not None

        # 4. 提示词服务
        assert isinstance(p._pm, PromptManager)
        assert isinstance(p._pm_service, PromptService)

        # 5. 任务管理器 + 工具注册（setup 已注册全套 Agent 工具）
        assert isinstance(p._task_manager, TaskManager)
        names = p._registry.all_names()
        for expected in (
            "list_my_tasks", "create_subtask", "inject_task",
            "search_memory", "read", "ask_user", "send_message",
        ):
            assert expected in names, f"工具 {expected} 未注册"

        # 6. MCP（禁用配置 → 无连接）
        assert isinstance(p._mcp, MCPManager)
        assert p._mcp._connections == {}
        assert p._mcp_config is not None

        # 7. Planner 看板
        assert isinstance(p._planner_board, PlannerBoard)

        # 8. 动态 API 已注册（6 个端点）
        components = p.get_dynamic_api_components()
        names_ = {c["name"] for c in components}
        assert names_ == {"create", "list", "get", "cancel", "inject", "history"}

        # 9. 存储真实可用（不 mock 持久化）
        t = make_task("boot-check", status=TaskStatus.PENDING)
        await p._store.save(t)
        assert await p._store.get("boot-check") is not None

    @pytest.mark.asyncio
    async def test_on_load_delegates_to_load_plugin(self, plugin: MaibotAgentPlugin) -> None:
        """plugin.on_load 薄壳调用 load_plugin（启动真实装配路径）。"""
        await plugin.on_load()
        assert plugin._store is not None
        assert plugin._task_manager is not None
        assert plugin._scheduler._check_task is not None
        await plugin._scheduler.stop()
        await plugin._mcp.stop()
        await plugin._store.close()

    @pytest.mark.asyncio
    async def test_on_unload_stops_components(
        self, loaded_plugin: MaibotAgentPlugin,
    ) -> None:
        """on_unload 停止调度器 / MCP / 存储（不抛异常）。"""
        p = loaded_plugin
        await p.on_unload()
        assert p._scheduler._check_task is None
        await p._store.close()  # close 幂等可调用

    @pytest.mark.asyncio
    async def test_on_unload_guards_missing_mcp_and_store(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """未初始化 _mcp / _store 时 on_unload 不崩溃（hasattr 守卫）。"""
        p = plugin
        p._scheduler = SimpleNamespace(stop=AsyncMock())
        await p.on_unload()  # 不应抛异常
        p._scheduler.stop.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# apply_config_update — 配置热更新
# ═══════════════════════════════════════════════════════════════════════════════

class TestApplyConfigUpdate:
    @pytest.mark.asyncio
    async def test_scope_other_than_self_is_noop(
        self, loaded_plugin: MaibotAgentPlugin,
    ) -> None:
        """非 SELF 范围的配置更新直接返回，不触碰任何组件。"""
        p = loaded_plugin
        old_resolver = p._resolver
        old_scheduler_cfg = p._scheduler._config
        await apply_config_update(p, "bot", {}, "v1")
        assert p._resolver is old_resolver
        assert p._scheduler._config is old_scheduler_cfg

    @pytest.mark.asyncio
    async def test_self_scope_propagates_config(
        self, loaded_plugin: MaibotAgentPlugin,
    ) -> None:
        """SELF 范围：权限解析器重建、调度器与任务管理器配置引用更新。"""
        p = loaded_plugin
        old_resolver = p._resolver
        await apply_config_update(p, CONFIG_RELOAD_SCOPE_SELF, {}, "v1")
        # 新配置实例 → 解析器重建、各组件配置引用指向 plugin.config
        assert p._resolver is not old_resolver
        assert p._scheduler._config is p.config.task
        assert p._task_manager._config is p.config

    @pytest.mark.asyncio
    async def test_config_update_rebuilds_planner_board(
        self, loaded_plugin: MaibotAgentPlugin,
    ) -> None:
        """热更新后 PlannerBoard 重建（新实例替换旧实例）。"""
        p = loaded_plugin
        old_board = p._planner_board
        await apply_config_update(p, CONFIG_RELOAD_SCOPE_SELF, {}, "v1")
        assert p._planner_board is not old_board
        assert isinstance(p._planner_board, PlannerBoard)

    @pytest.mark.asyncio
    async def test_update_failure_is_swallowed(
        self, loaded_plugin: MaibotAgentPlugin, monkeypatch: Any,
    ) -> None:
        """热更新中任一步失败仅记日志，不向 SDK 抛异常。"""
        p = loaded_plugin

        def _boom() -> None:
            raise RuntimeError("scheduler boom")

        monkeypatch.setattr(p._scheduler, "update_config", _boom)
        # 不应抛异常
        await apply_config_update(p, CONFIG_RELOAD_SCOPE_SELF, {}, "v1")


# ═══════════════════════════════════════════════════════════════════════════════
# llm_title — LLM 标题生成
# ═══════════════════════════════════════════════════════════════════════════════

class TestLlmTitle:
    @pytest.mark.asyncio
    async def test_success_returns_llm_title(
        self, plugin: MaibotAgentPlugin, prompt_service: PromptService,
    ) -> None:
        plugin._pm_service = prompt_service
        plugin.ctx.llm.set_generate_response("帮我整理聊天记录")
        assert await llm_title(plugin, "整理聊天记录") == "帮我整理聊天记录"

    @pytest.mark.asyncio
    async def test_empty_response_falls_back_to_intent(
        self, plugin: MaibotAgentPlugin, prompt_service: PromptService,
    ) -> None:
        plugin._pm_service = prompt_service
        plugin.ctx.llm.set_generate_response("   ")  # strip 后为空
        intent = "x" * 100
        assert await llm_title(plugin, intent) == intent[:40]

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_intent(
        self, plugin: MaibotAgentPlugin, prompt_service: PromptService,
        monkeypatch: Any,
    ) -> None:
        plugin._pm_service = prompt_service

        async def _boom(prompt: Any, model: str = "", **kw: Any) -> dict:
            raise RuntimeError("llm down")

        monkeypatch.setattr(plugin.ctx.llm, "generate", _boom)
        intent = "y" * 50
        assert await llm_title(plugin, intent) == intent[:40]


# ═══════════════════════════════════════════════════════════════════════════════
# recover_active_tasks — 重启恢复
# ═══════════════════════════════════════════════════════════════════════════════

class _RecoveryLogger:
    """记录 recover_active_tasks 的日志调用。"""

    def __init__(self) -> None:
        self.info_calls: list[tuple] = []
        self.debug_calls: list[tuple] = []
        self.warning_calls: list[tuple] = []

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.info_calls.append(args)

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self.debug_calls.append(args)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warning_calls.append(args)


class TestRecoverActiveTasks:
    @pytest.mark.asyncio
    async def test_recovery_strategy_per_status(
        self, real_store: TaskStore, command_bus: TaskCommandBus,
    ) -> None:
        """SCHEDULED 重新入队、RUNNING 降级 PENDING 入队、WAITING_INPUT 保持、
        PAUSED 不自动处理。"""
        store = real_store
        await store.init()

        t_sched = make_task(
            "sched", status=TaskStatus.SCHEDULED, trigger_type=TriggerType.DELAY,
            scheduled_at=datetime.now() + timedelta(hours=1),
        )
        t_running = make_task("run", status=TaskStatus.RUNNING, trigger_type=TriggerType.NOW)
        t_waiting = make_task("wait", status=TaskStatus.WAITING_INPUT)
        t_paused = make_task("paused", status=TaskStatus.PAUSED)
        for t in (t_sched, t_running, t_waiting, t_paused):
            await store.save(t)

        executed: list[str] = []

        async def _noop_executor(task: Any) -> None:
            executed.append(task.id)

        scheduler = TaskScheduler(
            TaskConfig(max_concurrent_tasks=2), store, _noop_executor,
            command_bus=command_bus,
        )
        plugin = SimpleNamespace(_store=store, _scheduler=scheduler)
        logger = _RecoveryLogger()

        await recover_active_tasks(plugin, logger)

        assert (await store.get("sched")).status == TaskStatus.SCHEDULED
        # RUNNING 恢复为 PENDING 后被立即重新派发（额度空闲）→ 回到 RUNNING 并执行
        assert (await store.get("run")).status == TaskStatus.RUNNING
        assert "run" in executed
        assert (await store.get("wait")).status == TaskStatus.WAITING_INPUT
        assert (await store.get("paused")).status == TaskStatus.PAUSED
        # 恢复计数日志
        assert any("已从上次会话恢复" in str(a) for a in logger.info_calls)

    @pytest.mark.asyncio
    async def test_list_active_failure_is_tolerated(
        self, real_store: TaskStore, monkeypatch: Any,
    ) -> None:
        """获取活跃任务失败时仅记警告，不抛出。"""
        store = real_store
        await store.init()

        async def _boom() -> list:
            raise RuntimeError("db down")

        monkeypatch.setattr(store, "list_active", _boom)
        plugin = SimpleNamespace(_store=store, _scheduler=None)
        logger = _RecoveryLogger()
        await recover_active_tasks(plugin, logger)  # 不应抛异常
        assert logger.warning_calls


# ═══════════════════════════════════════════════════════════════════════════════
# reload_mcp_if_changed — MCP 配置热更新
# ═══════════════════════════════════════════════════════════════════════════════

class TestReloadMcpIfChanged:
    @pytest.mark.asyncio
    async def test_unchanged_config_skips_rebuild(
        self, loaded_plugin: MaibotAgentPlugin,
    ) -> None:
        """配置未变化时不重建 MCP 管理器。"""
        p = loaded_plugin
        old_mcp = p._mcp
        await reload_mcp_if_changed(p)
        assert p._mcp is old_mcp

    @pytest.mark.asyncio
    async def test_changed_config_rebuilds_mcp(
        self, loaded_plugin: MaibotAgentPlugin,
    ) -> None:
        """配置变化时停止旧 MCP、按新配置重建并更新 _mcp_config。"""
        p = loaded_plugin
        old_mcp = p._mcp
        p._mcp_config = None  # 模拟配置首次变更

        await reload_mcp_if_changed(p)

        assert p._mcp is not old_mcp
        assert isinstance(p._mcp, MCPManager)
        assert p._mcp_config is p.config.mcp


# ═══════════════════════════════════════════════════════════════════════════════
# plugin.on_config_update 薄壳
# ═══════════════════════════════════════════════════════════════════════════════

class TestPluginOnConfigUpdate:
    @pytest.mark.asyncio
    async def test_on_config_update_delegates(
        self, loaded_plugin: MaibotAgentPlugin,
    ) -> None:
        """plugin.on_config_update 委托 apply_config_update。"""
        p = loaded_plugin
        old_resolver = p._resolver
        await p.on_config_update(CONFIG_RELOAD_SCOPE_SELF, {}, "v1")
        assert p._resolver is not old_resolver
