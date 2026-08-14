"""tools/planner/search_users.py — planner 侧 search_users handler 分支测试。

plugin 层的多来源搜索主路径由 test_plugin_search_users.py 覆盖；
本文件补齐 dict 人物 ID、人物查找失败跳过、显式失败跳过、
非 dict 记忆线索、外层异常回退分支。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import MockCtx

from oh_mai_agent.config import MaibotAgentConfig
from oh_mai_agent.tools.planner.search_users import build_search_users_handler


def _make_stream(index: int, chat_type: str = "group") -> dict:
    return {
        "group_id": f"g{index:06d}",
        "user_id": f"u{index:06d}",
        "user_nickname": f"user_{index}",
        "chat_type": chat_type,
        "platform": "qq",
        "stream_id": f"qq:g:{index:06d}",
        "active": True,
    }


def _handler(mock_ctx: MockCtx, config: MaibotAgentConfig | None = None) -> Any:
    return build_search_users_handler(mock_ctx, config or MaibotAgentConfig())


class TestSearchUsersBranches:
    @pytest.mark.asyncio
    async def test_dict_person_id_form(self, mock_ctx: MockCtx) -> None:
        """person 返回 dict 形式（兼容 mock / 旧 SDK）→ 解析 person_id。"""
        mock_ctx._person_data["Alice"] = {"person_id": "person-alice"}
        handler = _handler(mock_ctx)
        result = await handler(keyword="Alice")
        assert result["success"] is True
        assert result["persons"] == [{"person_id": "person-alice", "matched_by": "exact_name"}]

    @pytest.mark.asyncio
    async def test_person_lookup_failure_skipped(self, mock_ctx: MockCtx) -> None:
        """人物查找抛异常 → 跳过，不中断流搜索。"""
        mock_ctx.person.get_id_by_name = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        handler = _handler(mock_ctx)
        result = await handler(keyword="Alice")
        assert result["success"] is True
        assert result["persons"] == []

    @pytest.mark.asyncio
    async def test_explicit_failure_result_skipped(self, mock_ctx: MockCtx) -> None:
        """记忆检索显式 success=False → 跳过，不写入 knowledge。"""
        mock_ctx._capability_responses["Alice"] = {"success": False, "error": "no data"}
        handler = _handler(mock_ctx)
        result = await handler(keyword="Alice")
        assert result["success"] is True
        assert result["knowledge"] == []

    @pytest.mark.asyncio
    async def test_non_dict_knowledge_result_appended(self, mock_ctx: MockCtx) -> None:
        """记忆检索返回非 dict（字符串）→ 包装为 knowledge 条目。"""
        mock_ctx._capability_responses["Alice"] = "关于 Alice 的记忆片段"
        handler = _handler(mock_ctx)
        result = await handler(keyword="Alice")
        assert result["success"] is True
        assert result["knowledge"][0]["content"] == "关于 Alice 的记忆片段"

    @pytest.mark.asyncio
    async def test_stream_failure_returns_error(self, mock_ctx: MockCtx) -> None:
        """流列表获取抛异常 → 结构化失败结果。"""
        mock_ctx.chat.get_all_streams = AsyncMock(side_effect=RuntimeError("chat down"))  # type: ignore[method-assign]
        handler = _handler(mock_ctx)
        result = await handler(keyword="Alice")
        assert result == {"success": False, "error": "chat down"}

    @pytest.mark.asyncio
    async def test_streams_filtered_and_returned(self, mock_ctx: MockCtx) -> None:
        mock_ctx._chat_streams = [_make_stream(1), _make_stream(2)]
        handler = _handler(mock_ctx)
        result = await handler(keyword="user_1", platform="qq")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["streams"][0]["user_id"] == "u000001"
