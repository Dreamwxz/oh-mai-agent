"""配置热重载测试 — scheduler.update_config 与 task_manager.update_config。

验证内部配置引用能被正确更新，且不影响正在运行的任务，也无需重启插件。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import MockCtx, MockLogger, make_task

from oh_mai_agent.config import MaibotAgentConfig, PermissionConfig, TaskConfig
from oh_mai_agent.domain.task_record import TaskLevel
from oh_mai_agent.permission import PermissionResolver
from oh_mai_agent.core.scheduler import TaskScheduler
from oh_mai_agent.core.task_manager import TaskManager
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.tools.registry import ToolRegistry


async def _noop_executor(task: Any) -> None:
    pass


class TestSchedulerUpdateConfig:
    @pytest.mark.asyncio
    async def test_updates_internal_config_reference(self, real_store: TaskStore, command_bus: Any) -> None:
        old_config = TaskConfig(max_concurrent_tasks=3)
        scheduler = TaskScheduler(config=old_config, store=real_store, executor=_noop_executor, command_bus=command_bus)

        new_config = TaskConfig(max_concurrent_tasks=7)
        scheduler.update_config(new_config)

        assert scheduler._config is new_config
        assert scheduler._config.max_concurrent_tasks == 7

    @pytest.mark.asyncio
    async def test_concurrent_limit_reflected(self, real_store: TaskStore, command_bus: Any) -> None:
        old_config = TaskConfig(max_concurrent_tasks=10)
        scheduler = TaskScheduler(config=old_config, store=real_store, executor=_noop_executor, command_bus=command_bus)

        # 模拟 3 个正在运行的任务
        scheduler._running.add("a")
        scheduler._running.add("b")
        scheduler._running.add("c")

        assert scheduler.active_count() == 3

        # 并发上限缩到 1 — 正在运行的 3 个任务不会被强制收缩
        new_config = TaskConfig(max_concurrent_tasks=1)
        scheduler.update_config(new_config)

        assert scheduler.active_count() == 3
        assert scheduler._config.max_concurrent_tasks == 1

    @pytest.mark.asyncio
    async def test_idempotent_same_config(self, real_store: TaskStore, command_bus: Any) -> None:
        config = TaskConfig(max_concurrent_tasks=4)
        scheduler = TaskScheduler(config=config, store=real_store, executor=_noop_executor, command_bus=command_bus)

        scheduler.update_config(config)
        assert scheduler._config is config


class TestTaskManagerUpdateConfig:
    @pytest.mark.asyncio
    async def test_updates_internal_config_reference(self, real_store: TaskStore, command_bus: Any) -> None:
        registry = ToolRegistry()
        mock_ctx = MockCtx()
        resolver = PermissionResolver(PermissionConfig())
        old_config = MaibotAgentConfig()

        scheduler = TaskScheduler(config=old_config.task, store=real_store, executor=_noop_executor, command_bus=command_bus)

        tm = TaskManager(
            ctx=mock_ctx,
            store=real_store,
            scheduler=scheduler,
            registry=registry,
            resolver=resolver,
            config=old_config,
            command_bus=command_bus,
        )

        new_config = MaibotAgentConfig()
        new_config.task.max_concurrent_tasks = 8
        tm.update_config(new_config)

        assert tm._config is new_config
        assert tm._config.task.max_concurrent_tasks == 8
        assert tm._crud._config is new_config
        assert tm._control._config is new_config

        executor = AsyncMock()
        with patch.object(tm._executor_factory, "get", return_value=executor):
            await tm.execute_instant(make_task(level=TaskLevel.INSTANT))
        assert executor.execute.await_args.args[0].config is new_config
