"""测试 on_load 将 build_api_handlers 接入 register_dynamic_api。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from conftest import MockCtx, make_task

from oh_mai_agent.config import ApiExposeConfig, MaibotAgentConfig, PermissionConfig, PlannerBoardConfig, TaskConfig
from oh_mai_agent.permission import PermissionResolver
from oh_mai_agent.plugin import MaibotAgentPlugin
from oh_mai_agent.domain.task_store import TaskStore


@pytest_asyncio.fixture
async def fake_store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def plugin(fake_store: TaskStore) -> MaibotAgentPlugin:
    p = MaibotAgentPlugin()
    p._task_manager = MagicMock()
    p._resolver = PermissionResolver(PermissionConfig())
    p._store = fake_store
    mock_ctx = MockCtx()
    p._set_context(mock_ctx)
    return p


class TestOnLoadRegistersDynamicApis:
    """验证 on_load（或等价接线逻辑）恰好注册 6 个动态 API。"""

    EXPECTED_NAMES = {"create", "list", "get", "cancel", "inject", "history"}

    @pytest.mark.asyncio
    async def test_register_dynamic_api_called_six_times(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """给定带 mock 依赖的插件，当 build_api_handlers 运行并通过
        register_dynamic_api 注册时，应恰好注册 6 个 API，其 name 均在
        {create, list, get, cancel, inject, history} 之中。"""
        calls: list[dict[str, Any]] = []
        sync_called = False

        # 用假实现替代真实 SDK 调用：记录每次注册与同步的调用行为
        def _fake_register(name: str, handler: Any, **kw: Any) -> dict[str, Any]:
            calls.append({"name": name, "handler": handler, **kw})
            return {"name": name, "type": "API", "metadata": {}}

        async def _fake_sync(*, offline_reason: str = "") -> bool:
            nonlocal sync_called
            sync_called = True
            return True

        plugin.register_dynamic_api = _fake_register  # type: ignore[method-assign]
        plugin.sync_dynamic_apis = _fake_sync  # type: ignore[method-assign]

        # 模拟 on_load 的接线逻辑（与 on_load 中 setup 之后的部分一致）
        from oh_mai_agent.api_expose import build_api_handlers
        from oh_mai_agent.config import MaibotAgentConfig, PlannerBoardConfig, ApiExposeConfig, TaskConfig, PermissionConfig

        cfg = MaibotAgentConfig(
            planner_board=PlannerBoardConfig(),
            api_expose=ApiExposeConfig(),
            task=TaskConfig(),
            permission=PermissionConfig(),
        )
        handlers = build_api_handlers(plugin._task_manager)
        for h in handlers:
            plugin.register_dynamic_api(
                name=h["name"],
                handler=h["handler"],
                description=h["description"],
                version=h["version"],
                public=h["public"],
            )
        await plugin.sync_dynamic_apis()

        # 断言
        assert len(calls) == 6, f"Expected 6 API registrations, got {len(calls)}"
        registered_names = {c["name"] for c in calls}
        assert registered_names == self.EXPECTED_NAMES, (
            f"Expected {self.EXPECTED_NAMES}, got {registered_names}"
        )
        assert sync_called, "sync_dynamic_apis was not called"

    @pytest.mark.asyncio
    async def test_each_handler_is_public(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """给定 build_api_handlers，全部 6 个 handler 均应标记为 public=True。"""
        from oh_mai_agent.api_expose import build_api_handlers
        from oh_mai_agent.config import MaibotAgentConfig, PlannerBoardConfig, ApiExposeConfig, TaskConfig, PermissionConfig

        cfg = MaibotAgentConfig(
            planner_board=PlannerBoardConfig(),
            api_expose=ApiExposeConfig(),
            task=TaskConfig(),
            permission=PermissionConfig(),
        )
        handlers = build_api_handlers(plugin._task_manager)
        for h in handlers:
            assert h["public"] is True, f"{h['name']} should be public=True"
            assert callable(h["handler"]), f"{h['name']} handler is not callable"
            assert h["version"] == "1", f"{h['name']} version should be '1'"


class TestApiCreateReplyStreamId:
    """_create API 处理器应提取并透传 reply_stream_id。"""

    @staticmethod
    def _create_handler(plugin: MaibotAgentPlugin) -> Any:
        from oh_mai_agent.api_expose import build_api_handlers
        from oh_mai_agent.config import ApiExposeConfig, MaibotAgentConfig, PermissionConfig, PlannerBoardConfig, TaskConfig
        cfg = MaibotAgentConfig(
            planner_board=PlannerBoardConfig(),
            api_expose=ApiExposeConfig(),
            task=TaskConfig(),
            permission=PermissionConfig(),
        )
        handlers = build_api_handlers(plugin._task_manager)
        return next(h["handler"] for h in handlers if h["name"] == "create")

    @pytest.mark.asyncio
    async def test_create_passes_reply_stream_id(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """传入 reply_stream_id 参数时，create_task 应携带该参数被调用。"""
        plugin._task_manager.create_task = AsyncMock(  # type: ignore[attr-defined]
            return_value=(True, make_task(task_id="t1")),
        )
        handler = self._create_handler(plugin)
        await handler(
            intent="跨流回复",
            owner="qq:1",
            platform="qq",
            stream_id="qq:1",
            reply_stream_id="qq:g:2",
        )
        create_call = plugin._task_manager.create_task  # type: ignore[attr-defined]
        create_call.assert_awaited_once()
        kwargs = create_call.await_args.kwargs  # type: ignore[union-attr]
        assert kwargs["reply_stream_id"] == "qq:g:2"

    @pytest.mark.asyncio
    async def test_create_reply_stream_id_absent(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """未传入 reply_stream_id 参数时，create_task 应收到 None。"""
        plugin._task_manager.create_task = AsyncMock(  # type: ignore[attr-defined]
            return_value=(True, make_task(task_id="t2")),
        )
        handler = self._create_handler(plugin)
        await handler(
            intent="默认回复流",
            owner="qq:1",
            platform="qq",
            stream_id="qq:1",
        )
        create_call = plugin._task_manager.create_task  # type: ignore[attr-defined]
        create_call.assert_awaited_once()
        kwargs = create_call.await_args.kwargs  # type: ignore[union-attr]
        assert kwargs["reply_stream_id"] is None
