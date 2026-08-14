"""plugin.py _tool_send_message 的测试 — Planner 侧 send_message 上下文注入。"""

from __future__ import annotations

from typing import Any
from unittest.mock import PropertyMock, patch

import pytest

from tests.conftest import MockCtx
from oh_mai_agent.config import MaibotAgentConfig, PermissionConfig
from oh_mai_agent.permission import PermissionResolver
from oh_mai_agent.plugin import MaibotAgentPlugin
from oh_mai_agent.prompt.builders.context_note import ContextNoteBuilder
from oh_mai_agent.prompt.service import PromptService


@pytest.fixture
def plugin_with_ctx() -> MaibotAgentPlugin:
    from pathlib import Path
    from types import SimpleNamespace

    from oh_mai_agent.executor.instant import ReplySender
    from oh_mai_agent.prompt.manager import PromptManager

    p = MaibotAgentPlugin()
    mock_ctx = MockCtx()
    p._set_context(mock_ctx)
    p._pm = None
    p._pm_service = PromptService(
        manager=PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates"),
        builders=[ContextNoteBuilder()],
    )
    p._resolver = PermissionResolver(PermissionConfig())
    # 提供 sender 供 _tool_send_message 懒构建 handler 时绑定（真实 ReplySender，直发写回 mock_ctx）
    p._task_manager = SimpleNamespace(
        sender=ReplySender(ctx=mock_ctx, config_getter=lambda: MaibotAgentConfig(),
                           prompt_service=p._pm_service),
    )
    return p


@pytest.fixture
def plugin_ctx(plugin_with_ctx: MaibotAgentPlugin) -> MockCtx:
    return plugin_with_ctx.ctx  # type: ignore[return-value]


class TestToolSendMessageTwoAppends:
    @pytest.mark.asyncio
    async def test_success_produces_two_appends(self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx) -> None:
        """发送成功后产生两条上下文追加：[0] 纯文本，[1] XML 记录。"""
        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_send_message(text="你好", group_id="12345")

        assert result["success"] is True, f"result={result}"
        assert result["stream_id"] == "qq:g:12345"
        assert result["created"] is True
        assert len(plugin_ctx.maisaka.appends) == 2

        pure = plugin_ctx.maisaka.appends[0]
        assert pure["visible_text"] == "你好"
        assert "message_id" not in pure or pure.get("message_id") == ""
        assert pure["source_kind"] == "plugin:oh-mai-agent:send_message"
        assert pure["stream_id"] == "qq:g:12345"

        note = plugin_ctx.maisaka.appends[1]
        assert note["visible_text"].startswith("<plugin_context_note")
        assert note["visible_text"].endswith("</plugin_context_note>")
        assert "麦麦在此流发送了消息：你好" in note["visible_text"]
        assert note["message_id"].startswith("oh-mai-agent:send:")
        assert note["source_kind"] == "plugin:oh-mai-agent:send_message"
        assert note["stream_id"] == "qq:g:12345"

    @pytest.mark.asyncio
    async def test_escaped_text_in_xml_note(self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx) -> None:
        """文本含 XML 元字符时，XML 记录会转义，纯文本不转义。"""
        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            await plugin_with_ctx._tool_send_message(text="</plugin_context_note> hack", group_id="12345")

        assert len(plugin_ctx.maisaka.appends) == 2
        assert plugin_ctx.maisaka.appends[0]["visible_text"] == "</plugin_context_note> hack"
        note_vt = plugin_ctx.maisaka.appends[1]["visible_text"]
        assert "&lt;/plugin_context_note&gt;" in note_vt
        assert note_vt.count("</plugin_context_note>") == 1

    @pytest.mark.asyncio
    async def test_append_failure_still_returns_success(self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx) -> None:
        """ctx.maisaka.context.append 抛异常时，_tool_send_message 仍返回成功。"""
        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        async def _append_raises(**kwargs: Any) -> None:
            raise RuntimeError("context append failed")

        plugin_ctx.maisaka.context.append = _append_raises  # type: ignore[assignment]

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_send_message(text="你好", group_id="12345")

        assert result["success"] is True
        assert result["stream_id"] == "qq:g:12345"
        assert len(plugin_ctx.maisaka.appends) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# _tool_send_message — 每次发送都会调用 open_session（不做 get_stream_by_* 预检）
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolSendMessageOpenSessionCalled:
    @pytest.mark.asyncio
    async def test_user_id_calls_open_session_with_account_id_and_scope(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """传入 user_id 且存在真实会话时，open_session 携带 account_id 和 scope 被调用。"""
        plugin_ctx._chat_streams = [{
            "stream_id": "real-session",
            "user_id": "user999",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": False,
            "chat_type": "private",
        }]

        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_send_message(text="你好", user_id="user999")

        assert result["success"] is True
        assert result["stream_id"] == "qq::user999"
        assert result["created"] is True
        assert len(plugin_ctx.chat._open_session_calls) == 1
        call = plugin_ctx.chat._open_session_calls[0]
        assert call["account_id"] == "3948827829"
        assert call["scope"] == ""
        assert call["user_id"] == "user999"
        assert call["chat_type"] == "private"
        assert len(plugin_ctx.chat._stream_lookup_calls) == 0

    @pytest.mark.asyncio
    async def test_group_id_calls_open_session_with_account_id_and_scope(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """传入 group_id 且存在真实会话时，open_session 携带 account_id 和 scope 被调用。"""
        plugin_ctx._chat_streams = [{
            "stream_id": "group-real",
            "group_id": "group777",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": True,
            "chat_type": "group",
        }]

        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_send_message(text="你好", group_id="group777")

        assert result["success"] is True
        assert result["stream_id"] == "qq:g:group777"
        assert result["created"] is True
        assert len(plugin_ctx.chat._open_session_calls) == 1
        call = plugin_ctx.chat._open_session_calls[0]
        assert call["account_id"] == "3948827829"
        assert call["group_id"] == "group777"
        assert call["chat_type"] == "group"
        assert len(plugin_ctx.chat._stream_lookup_calls) == 0

    @pytest.mark.asyncio
    async def test_open_session_failure_returns_error(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """open_session 抛异常时，_tool_send_message 返回错误。"""
        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        orig_open = MockCtx._Chat.open_session
        async def _raise(chat_self, **kwargs: object) -> None:
            raise RuntimeError("session creation failed")
        MockCtx._Chat.open_session = _raise

        try:
            with (
                patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
                patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
            ):
                mock_cfg.return_value = MaibotAgentConfig()
                result = await plugin_with_ctx._tool_send_message(text="你好", user_id="user888")

            assert result["success"] is False
            assert "session creation failed" in result["error"]
        finally:
            MockCtx._Chat.open_session = orig_open


# ═══════════════════════════════════════════════════════════════════════════════
# _tool_send_message — 从 get_all_streams 推导 account_id / scope
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolSendMessageAccountIdScope:
    @pytest.mark.asyncio
    async def test_real_stream_passes_account_id_to_open_session(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """get_all_streams 返回带 account_id 的会话时，open_session 收到 account_id 和 scope。"""
        plugin_ctx._chat_streams = [{
            "stream_id": "a76770406deead4ad8ab9c389f8e36be",
            "user_id": "3783399364",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": False,
            "chat_type": "private",
        }]

        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_send_message(text="你好", user_id="3783399364")

        assert result["success"] is True
        assert len(plugin_ctx.chat._open_session_calls) == 1
        call = plugin_ctx.chat._open_session_calls[0]
        assert call["account_id"] == "3948827829"
        assert call["scope"] == ""

    @pytest.mark.asyncio
    async def test_prefers_stream_with_account_id(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """存在两条匹配会话（一条带 account_id、一条不带）时，优先选带 account_id 的。"""
        plugin_ctx._chat_streams = [
            {
                "stream_id": "orphan-stream",
                "user_id": "3783399364",
                "account_id": "",
                "scope": "",
                "is_group_session": False,
                "chat_type": "private",
            },
            {
                "stream_id": "a76770406deead4ad8ab9c389f8e36be",
                "user_id": "3783399364",
                "account_id": "3948827829",
                "scope": "",
                "is_group_session": False,
                "chat_type": "private",
            },
        ]

        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            await plugin_with_ctx._tool_send_message(text="你好", user_id="3783399364")

        assert len(plugin_ctx.chat._open_session_calls) == 1
        assert plugin_ctx.chat._open_session_calls[0]["account_id"] == "3948827829"

    @pytest.mark.asyncio
    async def test_no_matching_stream_falls_back_to_empty_account_id(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """get_all_streams 无匹配会话时，open_session 以 account_id="" 被调用。"""
        plugin_ctx._chat_streams = [{
            "stream_id": "unrelated",
            "user_id": "9999999999",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": False,
            "chat_type": "private",
        }]

        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_send_message(text="你好", user_id="3783399364")

        assert result["success"] is True
        assert len(plugin_ctx.chat._open_session_calls) == 1
        assert plugin_ctx.chat._open_session_calls[0]["account_id"] == ""

    @pytest.mark.asyncio
    async def test_empty_streams_list_still_works(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """get_all_streams 返回空列表时，handler 仍成功（保留既有行为）。"""
        plugin_ctx._chat_streams = []

        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            result = await plugin_with_ctx._tool_send_message(text="你好", user_id="67890")

        assert result["success"] is True
        assert result["stream_id"] == "qq::67890"
        assert len(plugin_ctx.chat._open_session_calls) == 1
        assert plugin_ctx.chat._open_session_calls[0]["account_id"] == ""

    @pytest.mark.asyncio
    async def test_no_stream_lookup_calls_made(
        self, plugin_with_ctx: MaibotAgentPlugin, plugin_ctx: MockCtx,
    ) -> None:
        """正常发送时不调用任何 get_stream_by_*，仅使用 open_session。"""
        plugin_ctx._chat_streams = [{
            "stream_id": "real-session",
            "user_id": "3783399364",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": False,
            "chat_type": "private",
        }]

        async def _noop(*args: Any, **kwargs: Any) -> None:
            pass

        with (
            patch.object(plugin_with_ctx._task_manager.sender, "send_polished", _noop),
            patch.object(type(plugin_with_ctx), "config", new_callable=PropertyMock) as mock_cfg,
        ):
            mock_cfg.return_value = MaibotAgentConfig()
            await plugin_with_ctx._tool_send_message(text="你好", user_id="3783399364")

        assert len(plugin_ctx.chat._stream_lookup_calls) == 0
        assert len(plugin_ctx.chat._open_session_calls) == 1
