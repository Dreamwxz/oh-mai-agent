"""plugin.py 中 _tool_search_users 的测试 —— Planner 侧多来源搜索。"""
from __future__ import annotations

from typing import Any
from unittest.mock import PropertyMock, patch

import pytest

from tests.conftest import MockCtx
from oh_mai_agent.config import MaibotAgentConfig, PermissionConfig
from oh_mai_agent.permission import PermissionResolver
from oh_mai_agent.plugin import MaibotAgentPlugin


def _make_stream(
    index: int,
    chat_type: str = "group",
    user_nickname: str = "",
    group_name: str = "",
) -> dict:
    return {
        "group_id": f"g{index:06d}",
        "user_id": f"u{index:06d}",
        "user_nickname": user_nickname or f"user_{index}",
        "user_cardname": f"card_{index}",
        "group_name": group_name or f"TestGroup_{index}",
        "chat_type": chat_type,
        "platform": "qq",
        "stream_id": f"qq:g:{index:06d}",
        "active": True,
    }


@pytest.fixture
def plugin_with_ctx() -> MaibotAgentPlugin:
    p = MaibotAgentPlugin()
    mock_ctx = MockCtx()
    p._set_context(mock_ctx)
    p._resolver = PermissionResolver(PermissionConfig())
    return p


@pytest.fixture
def plugin_ctx(plugin_with_ctx: MaibotAgentPlugin) -> MockCtx:
    return plugin_with_ctx.ctx  # type: ignore[return-value]


class TestToolSearchUsersMultiSource:
    @pytest.mark.asyncio
    async def test_keyword_matches_stream_persons_knowledge_attempted(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """当流匹配关键词时，persons/knowledge 也会被查询。"""
        plugin_ctx._chat_streams = [_make_stream(0, user_nickname="Alice")]
        plugin_ctx._person_data["Alice"] = "person-alice"

        with patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg:
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_search_users(keyword="Alice")

        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["streams"]) == 1
        assert result["streams"][0]["user_nickname"] == "Alice"
        assert len(result["persons"]) == 1
        assert result["persons"][0]["person_id"] == "person-alice"
        assert len(result["knowledge"]) == 1
        assert "Alice" in result["knowledge"][0]["query"]

    @pytest.mark.asyncio
    async def test_keyword_not_in_streams_but_person_found(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """关键词不在流中，person.get_id_by_name 返回 id —— persons 被填充。"""
        plugin_ctx._chat_streams = [_make_stream(0, user_nickname="Bob")]
        plugin_ctx._person_data["空格"] = "person-spaces"

        with patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg:
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_search_users(keyword="空格")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["streams"] == []
        assert len(result["persons"]) == 1
        assert result["persons"][0]["person_id"] == "person-spaces"
        assert result["persons"][0]["matched_by"] == "exact_name"

    @pytest.mark.asyncio
    async def test_empty_string_person_id_not_appended(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """get_id_by_name 返回空串（真实宿主查无此名的形态）—— persons 不产生假命中。"""
        plugin_ctx._chat_streams = []
        plugin_ctx._person_data["空格"] = ""

        with patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg:
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_search_users(keyword="空格")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["streams"] == []
        assert result["persons"] == []

    @pytest.mark.asyncio
    async def test_keyword_not_found_anywhere(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """关键词任何来源都未命中 —— 全部为空，count 为 0。"""
        plugin_ctx._chat_streams = [_make_stream(0, user_nickname="Alice")]
        plugin_ctx._capability_responses = {"Nobody": {"success": True, "content": ""}}

        with patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg:
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_search_users(keyword="Nobody")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["streams"] == []
        assert result["persons"] == []
        assert result["knowledge"] == []

    @pytest.mark.asyncio
    async def test_knowledge_search_raises_does_not_break(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """knowledge.search 抛异常时，streams/persons 仍正常返回。"""
        plugin_ctx._chat_streams = [_make_stream(0, user_nickname="Alice")]
        plugin_ctx._person_data["Alice"] = "person-alice"

        def _raise(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("knowledge search down")

        plugin_ctx._capability_responses["Alice"] = _raise

        with patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg:
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_search_users(keyword="Alice")

        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["persons"]) == 1
        assert result["persons"][0]["person_id"] == "person-alice"
        assert result["knowledge"] == []

    @pytest.mark.asyncio
    async def test_no_keyword_no_extra_lookups(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """关键词为空时，persons 和 knowledge 保持为空。"""
        plugin_ctx._chat_streams = [_make_stream(i) for i in range(5)]

        with patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg:
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_search_users()

        assert result["success"] is True
        assert result["count"] == 5
        assert result["persons"] == []
        assert result["knowledge"] == []

    @pytest.mark.asyncio
    async def test_result_keys_backward_compatible(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """关键词命中时，结果包含全部预期键：success、streams、persons、knowledge、count。"""
        plugin_ctx._chat_streams = [_make_stream(0, user_nickname="Alice")]

        with patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg:
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_search_users(keyword="Alice")

        assert result["success"] is True
        assert "streams" in result
        assert "persons" in result
        assert "knowledge" in result
        assert "count" in result
        # count 只统计 streams
        assert result["count"] == len(result["streams"])
