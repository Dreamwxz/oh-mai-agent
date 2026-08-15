"""plugin.py — @Tool task_* handler 委托、create_plugin 与 on_planner_before_request。

@Tool 声明本体由 test_plugin_registration / test_planner_tool_meta 覆盖；
本文件验证 7 个 task_* handler 的懒构建委托链（plugin → build_task_tools
→ FakeTaskManager），以及模块级工厂 create_plugin 与看板 Hook 委托。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from conftest import FakeTaskManager, MockCtx, make_task

from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.plugin import MaibotAgentPlugin, create_plugin


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def plugin_with_tm(store: TaskStore) -> MaibotAgentPlugin:
    """注入了 FakeTaskManager 的插件实例（绕过 on_load）。"""
    p = MaibotAgentPlugin()
    p._set_context(MockCtx())
    p._task_manager = FakeTaskManager(store)
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# @Tool task_* handler 委托
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolTaskHandlers:
    @pytest.mark.asyncio
    async def test_task_create_delegates(
        self, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        p = plugin_with_tm
        result = await p._tool_task_create(
            intent="写周报", stream_id="qq:group:1", level="agent",
        )
        assert result["success"] is True
        call = p._task_manager.calls["create_task"][0]
        assert call["owner"] == "planner:qq:group:1"

    @pytest.mark.asyncio
    async def test_task_list_delegates(
        self, store: TaskStore, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        await store.save(make_task("task-abc", title="T", stream_id="qq:group:1"))
        result = await plugin_with_tm._tool_task_list(stream_id="qq:group:1")
        assert result["success"] is True
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_task_status_delegates(
        self, store: TaskStore, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        await store.save(make_task("task-abc", title="T"))
        result = await plugin_with_tm._tool_task_status(
            task_id="task-abc", stream_id="qq:1",
        )
        assert result["success"] is True
        assert result["task"]["id"] == "task-abc"

    @pytest.mark.asyncio
    async def test_task_status_accepts_title(
        self, store: TaskStore, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        """Planner 传标题（而非 ID）时 task_status 仍能解析（回归：任务不存在）。"""
        await store.save(make_task("task-abc", title="系统环境检查"))
        result = await plugin_with_tm._tool_task_status(
            task_id="系统环境检查", stream_id="qq:1",
        )
        assert result["success"] is True
        assert result["task"]["id"] == "task-abc"

    @pytest.mark.asyncio
    async def test_task_modify_delegates(
        self, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        result = await plugin_with_tm._tool_task_modify(
            task_id="t1", inject_instruction="继续", stream_id="qq:1",
        )
        assert result == {"success": True, "message": "已注入"}

    @pytest.mark.asyncio
    async def test_task_delete_delegates(
        self, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        result = await plugin_with_tm._tool_task_delete(task_id="t1", stream_id="qq:1")
        assert result == {"success": True, "message": "已取消"}

    @pytest.mark.asyncio
    async def test_task_history_delegates(
        self, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        result = await plugin_with_tm._tool_task_history(task_id="t1", stream_id="qq:1")
        assert result["success"] is True
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_task_schedule_delegates(
        self, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        result = await plugin_with_tm._tool_task_schedule(
            intent="日报", stream_id="qq:1", cron_expr="0 9 * * *",
        )
        assert result["success"] is True
        assert result["cron_expr"] == "0 9 * * *"

    @pytest.mark.asyncio
    async def test_handler_cache_reused(
        self, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        """同一工具名第二次调用走缓存（懒构建只发生一次）。"""
        p = plugin_with_tm
        await p._tool_task_create(intent="x", stream_id="qq:1")
        first_cache = dict(p._planner_tool_cache)
        await p._tool_task_create(intent="y", stream_id="qq:1")
        assert p._planner_tool_cache == first_cache


# ═══════════════════════════════════════════════════════════════════════════════
# create_plugin / on_planner_before_request
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreatePlugin:
    def test_create_plugin_returns_plugin_instance(self) -> None:
        p = create_plugin()
        assert isinstance(p, MaibotAgentPlugin)
        # get_components 收集 11 Tool + 7 Command + 2 HookHandler
        components = p.get_components()
        types_ = [c["type"] for c in components]
        assert types_.count("TOOL") == 11
        assert types_.count("COMMAND") == 7
        assert types_.count("HOOK_HANDLER") == 2


class TestOnPlannerBeforeRequest:
    @pytest.mark.asyncio
    async def test_delegates_to_planner_board(
        self, plugin_with_tm: MaibotAgentPlugin,
    ) -> None:
        board = AsyncMock()
        board.hook_before_request.return_value = {"action": "continue", "board": "x"}
        plugin_with_tm._planner_board = board
        result = await plugin_with_tm.on_planner_before_request(some="kw")
        assert result == {"action": "continue", "board": "x"}
        board.hook_before_request.assert_awaited_once_with(some="kw")
