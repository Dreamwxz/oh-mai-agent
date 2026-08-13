"""oh_mai_agent.core.task_manager 的子 Agent 工具注册测试。

覆盖 TaskManager.setup() 对 ask_subagent / ask_subagents 的注册：
  - happy：默认配置下两者注册成功，visibility=discoverable、min_role=USER
  - 热更新：update_config 替换配置后，已注册的 ask_subagents handler
    立即读到新上限（无需重新注册）
  - failure：config.subagent.enabled=False 时两者均不注册且 setup() 不抛错
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from conftest import MockCtx

from oh_mai_agent.config import MaibotAgentConfig, SubAgentConfig
from oh_mai_agent.permission import PermissionResolver, Role
from oh_mai_agent.core.scheduler import TaskScheduler
from oh_mai_agent.core.task_manager import TaskManager
from oh_mai_agent.domain.task_record import TaskRecord
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.tools.registry import ToolRegistry

_SUBAGENT_TOOL_NAMES = ("ask_subagent", "ask_subagents")


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
# 注册
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubagentRegistration:
    @pytest.mark.asyncio
    async def test_setup_registers_both_subagent_tools(
        self, manager: TaskManager, registry: ToolRegistry,
    ) -> None:
        """默认配置下 setup() 注册 ask_subagent 与 ask_subagents。"""
        await manager.setup()

        names = registry.all_names()
        for name in _SUBAGENT_TOOL_NAMES:
            assert name in names, f"{name} 应被 TaskManager.setup() 注册"

    @pytest.mark.asyncio
    async def test_subagent_tools_are_discoverable_user(
        self, manager: TaskManager, registry: ToolRegistry,
    ) -> None:
        """两个子 Agent 工具均为 discoverable 可见性且 min_role=USER。"""
        await manager.setup()

        for name in _SUBAGENT_TOOL_NAMES:
            tool = registry.get(name)
            assert tool is not None
            assert tool.visibility == "discoverable", (
                f"{name} 应为 discoverable，实际 {tool.visibility}"
            )
            assert tool.min_role == Role.USER, (
                f"{name} 的 min_role 应为 USER，实际 {tool.min_role}"
            )

    @pytest.mark.asyncio
    async def test_setup_with_subagent_disabled_registers_nothing(
        self, mock_ctx: MockCtx, store: TaskStore, scheduler: TaskScheduler,
        registry: ToolRegistry, resolver: PermissionResolver,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        """enabled=False 时两个工具均不注册，且 setup() 不抛异常。"""
        cfg = MaibotAgentConfig(subagent=SubAgentConfig(enabled=False))
        mgr = TaskManager(
            ctx=mock_ctx, store=store, scheduler=scheduler,
            registry=registry, resolver=resolver, config=cfg,
            prompt_service=prompt_service, command_bus=command_bus,
        )

        await mgr.setup()  # 不应抛出

        names = registry.all_names()
        for name in _SUBAGENT_TOOL_NAMES:
            assert name not in names, f"enabled=False 时 {name} 不应被注册"


# ═══════════════════════════════════════════════════════════════════════════════
# 配置热更新
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubagentHotReload:
    @pytest.mark.asyncio
    async def test_ask_subagents_limit_hot_reloads_without_reregistration(
        self, manager: TaskManager, registry: ToolRegistry,
    ) -> None:
        """update_config 后已注册的 handler 立即读到新上限（config_getter 不缓存）。"""
        await manager.setup()
        tool = registry.get("ask_subagents")
        assert tool is not None

        # 热更新：上限 3 → 2
        manager.update_config(
            MaibotAgentConfig(subagent=SubAgentConfig(max_parallel_subagents=2))
        )

        result = await tool.handler(intents=["查A", "查B", "查C"])

        assert result["success"] is False
        assert "超限" in result["error"], f"应报超限错误，实际 {result['error']}"
        assert "3" in result["error"] and "2" in result["error"], (
            f"错误信息应含数量与上限，实际 {result['error']}"
        )
