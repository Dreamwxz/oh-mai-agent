"""tools/agent/info_tools.py — search_users 之外的 5 个信息工具行为测试。

search_users 已有独立测试（test_search_users.py）；
本文件补齐 search_memory / fetch_history / query_person / get_frequency /
list_plugin_tools 的 handler 行为（含各容错分支）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import MockCtx

from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.info_tools import build_info_tools


@pytest.fixture
def tools(mock_ctx: MockCtx) -> dict[str, Any]:
    """name → ToolDefinition 字典。"""
    return {t.name: t for t in build_info_tools(mock_ctx)}


# ═══════════════════════════════════════════════════════════════════════════════
# 元数据
# ═══════════════════════════════════════════════════════════════════════════════

class TestInfoToolsMetadata:
    def test_six_discoverable_guest_tools(self, tools: dict[str, Any]) -> None:
        assert set(tools) == {
            "search_memory", "fetch_history", "query_person",
            "search_users", "get_frequency", "list_plugin_tools",
        }
        for tool in tools.values():
            assert tool.visibility == "discoverable"
            assert tool.min_role == Role.GUEST


# ═══════════════════════════════════════════════════════════════════════════════
# search_memory
# ═══════════════════════════════════════════════════════════════════════════════

class TestSearchMemory:
    @pytest.mark.asyncio
    async def test_capability_dict_result_passed_through(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx._capability_responses["周末安排"] = {
            "success": True, "content": "打篮球",
        }
        result = await tools["search_memory"].handler(
            query="周末安排", chat_id="qq:10001",
        )
        assert result == {"success": True, "content": "打篮球"}

    @pytest.mark.asyncio
    async def test_non_dict_result_wrapped(self, mock_ctx: MockCtx, tools: dict[str, Any]) -> None:
        mock_ctx._capability_responses["x"] = "plain text"
        result = await tools["search_memory"].handler(query="x", chat_id="c")
        assert result == {"success": True, "content": "plain text"}

    @pytest.mark.asyncio
    async def test_extra_filters_forwarded(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        """person_name / time_start / time_end 透传给 knowledge.search。"""
        captured: dict[str, Any] = {}

        async def _fake_capability(capability: str, **kw: Any) -> Any:
            captured.update(kw)
            return {"success": True}

        mock_ctx.call_capability = _fake_capability  # type: ignore[method-assign]
        await tools["search_memory"].handler(
            query="q", chat_id="c", person_name="Alice",
            time_start="2025-01-01", time_end="2025-02-01",
        )
        assert captured["person_name"] == "Alice"
        assert captured["time_start"] == "2025-01-01"
        assert captured["time_end"] == "2025-02-01"

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        async def _boom(capability: str, **kw: Any) -> Any:
            raise RuntimeError("cap down")

        mock_ctx.call_capability = _boom  # type: ignore[method-assign]
        result = await tools["search_memory"].handler(query="q", chat_id="c")
        assert result == {"success": False, "error": "cap down"}


# ═══════════════════════════════════════════════════════════════════════════════
# fetch_history
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchHistory:
    @pytest.mark.asyncio
    async def test_returns_recent_messages(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.add_message("qq:10001", "你好")
        mock_ctx.add_message("qq:10001", "在吗")
        result = await tools["fetch_history"].handler(chat_id="qq:10001", limit=50)
        assert result["success"] is True
        assert result["count"] == 2
        assert [m["content"] for m in result["messages"]] == ["你好", "在吗"]

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, mock_ctx: MockCtx, tools: dict[str, Any], monkeypatch: Any,
    ) -> None:
        monkeypatch.setattr(
            MockCtx._Message, "get_recent",
            AsyncMock(side_effect=RuntimeError("msg down")),
        )
        result = await tools["fetch_history"].handler(chat_id="c")
        assert result == {"success": False, "error": "msg down"}


# ═══════════════════════════════════════════════════════════════════════════════
# query_person
# ═══════════════════════════════════════════════════════════════════════════════

class TestQueryPerson:
    @pytest.mark.asyncio
    async def test_str_person_id_resolved(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx._person_data["Alice"] = "person-alice"
        # 聚合检索返回非 success 结构 → handler 包装为 {person_id, profile}
        mock_ctx._capability_responses[""] = "画像摘要文本"
        result = await tools["query_person"].handler(person_name="Alice")
        assert result["success"] is True
        assert result["person_id"] == "person-alice"
        assert result["profile"] == "画像摘要文本"

    @pytest.mark.asyncio
    async def test_dict_person_id_resolved(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx._person_data["Bob"] = {"person_id": "person-bob"}
        mock_ctx._capability_responses[""] = "画像摘要文本"
        result = await tools["query_person"].handler(person_name="Bob")
        assert result["success"] is True
        assert result["person_id"] == "person-bob"

    @pytest.mark.asyncio
    async def test_unresolved_person_returns_error(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx._person_data["Ghost"] = None
        result = await tools["query_person"].handler(person_name="Ghost")
        assert result == {"success": False, "error": "无法解析人物: Ghost"}

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.person.get_id_by_name = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        result = await tools["query_person"].handler(person_name="X")
        assert result == {"success": False, "error": "boom"}


# ═══════════════════════════════════════════════════════════════════════════════
# get_frequency
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetFrequency:
    @pytest.mark.asyncio
    async def test_returns_frequency_value(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.frequency = SimpleNamespace(
            get_current_talk_value=AsyncMock(return_value=3),
        )
        result = await tools["get_frequency"].handler(chat_id="qq:10001")
        assert result == {"success": True, "chat_id": "qq:10001", "value": 3}

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.frequency = SimpleNamespace(
            get_current_talk_value=AsyncMock(side_effect=RuntimeError("freq down")),
        )
        result = await tools["get_frequency"].handler(chat_id="c")
        assert result == {"success": False, "error": "freq down"}


# ═══════════════════════════════════════════════════════════════════════════════
# list_plugin_tools
# ═══════════════════════════════════════════════════════════════════════════════

class TestListPluginTools:
    @pytest.mark.asyncio
    async def test_dict_form_with_definitions(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.tool = SimpleNamespace(get_definitions=AsyncMock(return_value={
            "tools": [
                {"name": "search_users", "definition": {"description": "搜用户"}},
                {"name": "send_message"},
            ],
        }))
        result = await tools["list_plugin_tools"].handler(query="")
        assert result["success"] is True
        assert result["count"] == 2
        assert result["tools"][0] == {"name": "search_users", "description": "搜用户"}
        assert result["tools"][1] == {"name": "send_message", "description": ""}

    @pytest.mark.asyncio
    async def test_list_form_supported(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.tool = SimpleNamespace(get_definitions=AsyncMock(return_value=[
            {"name": "a"}, {"name": "b"},
        ]))
        result = await tools["list_plugin_tools"].handler()
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_query_filters_by_name_case_insensitive(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.tool = SimpleNamespace(get_definitions=AsyncMock(return_value={
            "tools": [{"name": "Search_Users"}, {"name": "send_message"}],
        }))
        result = await tools["list_plugin_tools"].handler(query="SEARCH")
        assert result["count"] == 1
        assert result["tools"][0]["name"] == "Search_Users"

    @pytest.mark.asyncio
    async def test_unexpected_shape_returns_empty(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.tool = SimpleNamespace(get_definitions=AsyncMock(return_value="junk"))
        result = await tools["list_plugin_tools"].handler()
        assert result == {"success": True, "tools": [], "count": 0}

    @pytest.mark.asyncio
    async def test_exception_returns_error(
        self, mock_ctx: MockCtx, tools: dict[str, Any],
    ) -> None:
        mock_ctx.tool = SimpleNamespace(
            get_definitions=AsyncMock(side_effect=RuntimeError("tool down")),
        )
        result = await tools["list_plugin_tools"].handler()
        assert result == {"success": False, "error": "tool down"}
