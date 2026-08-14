"""tools/agent/ask_tool.py — 真实 ask_user handler 行为测试。

此前 agent_loop 测试全部使用注册到 registry 的假 ask_user 工具，
真实 ``build_ask_tool`` 工厂的 handler（参数校验 / 上下文合并 / 发送失败
分支）从未被执行过。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.ask_tool import build_ask_tool


@pytest.fixture
def ask_callback() -> AsyncMock:
    return AsyncMock()


def _ask_tool(callback: AsyncMock, *, min_role: Role = Role.USER) -> dict[str, Any]:
    tools = build_ask_tool(object(), ask_callback=callback, min_role=min_role)
    assert len(tools) == 1
    return {"name": tools[0].name, "tool": tools[0], "handler": tools[0].handler}


class TestAskToolHandler:
    @pytest.mark.asyncio
    async def test_success_sends_question_and_returns_ok(
        self, ask_callback: AsyncMock,
    ) -> None:
        t = _ask_tool(ask_callback)
        result = await t["handler"](question="继续吗?", stream_id="qq:10001")
        assert result == {"success": True, "message": "已提问，等待用户回复"}
        ask_callback.assert_awaited_once_with("qq:10001", "继续吗?")

    @pytest.mark.asyncio
    async def test_context_is_appended_to_question(
        self, ask_callback: AsyncMock,
    ) -> None:
        """context 参数合并进问题文本，让用户看到完整信息。"""
        t = _ask_tool(ask_callback)
        await t["handler"](
            question="确认执行?", stream_id="qq:10001", context="风险较高",
        )
        ask_callback.assert_awaited_once_with(
            "qq:10001", "确认执行?\n\n[上下文]\n风险较高",
        )

    @pytest.mark.asyncio
    async def test_missing_params_still_calls_callback(
        self, ask_callback: AsyncMock,
    ) -> None:
        """参数校验仅记录日志，不改变执行流程。"""
        t = _ask_tool(ask_callback)
        result = await t["handler"](question="", stream_id="")
        assert result["success"] is True
        ask_callback.assert_awaited_once_with("", "")

    @pytest.mark.asyncio
    async def test_callback_failure_returns_error(
        self, ask_callback: AsyncMock,
    ) -> None:
        ask_callback.side_effect = RuntimeError("send down")
        t = _ask_tool(ask_callback)
        result = await t["handler"](question="q", stream_id="s")
        assert result == {"success": False, "error": "send down"}


class TestAskToolMetadata:
    def test_essential_visible_and_min_role(self) -> None:
        t = _ask_tool(AsyncMock(), min_role=Role.GUEST)
        assert t["name"] == "ask_user"
        assert t["tool"].visibility == "essential"
        assert t["tool"].min_role == Role.GUEST

    def test_parameters_require_question_and_stream_id(self) -> None:
        t = _ask_tool(AsyncMock())
        assert t["tool"].parameters["required"] == ["question", "stream_id"]
