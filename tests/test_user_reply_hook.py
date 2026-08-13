"""测试 chat.receive.after_process Hook 处理器，用于匹配 WAITING_INPUT 任务。

替代旧的 ON_MESSAGE EventHandler——MaiBot 1.1.3 中该事件已不再触发。
Hook 处理器从 _session_message_to_dict() 生成的 message dict 结构中
提取 stream_id / user_id / plain_text，并委托给 handle_user_reply()。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import MockCtx

from oh_mai_agent.plugin import MaibotAgentPlugin


class TestOnMessageHook:
    """chat.receive.after_process Hook 处理器 on_message() 的测试。"""

    @pytest.fixture
    def plugin(self) -> MaibotAgentPlugin:
        p = MaibotAgentPlugin()
        mock_ctx = MockCtx()
        p._set_context(mock_ctx)
        # 注入 mock task_manager，以便断言 handle_user_reply 的调用
        p._task_manager = AsyncMock()
        return p

    # ── 辅助函数 ──────────────────────────────────────────────────────

    @staticmethod
    def _make_message_kwargs(
        session_id: str = "qq:g:123",
        user_id: str = "10001",
        plain_text: str = "\u597d\u7684",
    ) -> dict[str, Any]:
        """构造与 _session_message_to_dict() 序列化后的 message dict 一致的 kwargs。"""
        return {
            "message": {
                "session_id": session_id,
                "message_info": {
                    "user_info": {"user_id": user_id},
                },
                "processed_plain_text": plain_text,
            },
        }

    # ── 正常路径 ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_normal_path_calls_handle_user_reply(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """正常路径：完整的 message dict → handle_user_reply 以正确参数被调用。"""
        kwargs = self._make_message_kwargs(
            session_id="qq:g:123", user_id="10001", plain_text="\u597d\u7684",
        )
        result = await plugin.on_message(**kwargs)

        assert result == {"action": "continue"}
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply.assert_awaited_once_with(
            stream_id="qq:g:123",
            user_id="10001",
            reply="\u597d\u7684",
        )

    @pytest.mark.asyncio
    async def test_normal_path_returns_action_continue(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """正常路径：返回 {'action': 'continue'}，使 Runner 可以继续执行。"""
        kwargs = self._make_message_kwargs()
        result = await plugin.on_message(**kwargs)
        assert result == {"action": "continue"}
        assert isinstance(result, dict)

    # ── 缺失字段 ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_missing_processed_plain_text_skips(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """当 processed_plain_text 缺失（等效为空）时，跳过 handle_user_reply 并继续。"""
        kwargs: dict[str, Any] = {
            "message": {
                "session_id": "qq:g:123",
                "message_info": {"user_info": {"user_id": "10001"}},
                # 无 processed_plain_text
            },
        }
        result = await plugin.on_message(**kwargs)

        assert result == {"action": "continue"}
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_session_id_skips(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """当 session_id 缺失时，跳过 handle_user_reply 并继续。"""
        kwargs: dict[str, Any] = {
            "message": {
                # 无 session_id
                "message_info": {"user_info": {"user_id": "10001"}},
                "processed_plain_text": "\u597d\u7684",
            },
        }
        result = await plugin.on_message(**kwargs)

        assert result == {"action": "continue"}
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_user_id_skips(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """当 user_id 缺失（user_info.user_id 为空）时，跳过并继续。"""
        kwargs: dict[str, Any] = {
            "message": {
                "session_id": "qq:g:123",
                "message_info": {"user_info": {}},  # 无 user_id
                "processed_plain_text": "\u597d\u7684",
            },
        }
        result = await plugin.on_message(**kwargs)

        assert result == {"action": "continue"}
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply.assert_not_awaited()

    # ── 非 dict 消息 ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_message_none_safe_return(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """当 message 为 None 时，安全返回，不调用 handle_user_reply。"""
        kwargs: dict[str, Any] = {"message": None}
        result = await plugin.on_message(**kwargs)

        assert result == {"action": "continue"}
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_message_empty_dict_safe_return(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """当 message 是空 dict 时，安全返回，不抛异常。"""
        kwargs: dict[str, Any] = {"message": {}}
        result = await plugin.on_message(**kwargs)

        assert result == {"action": "continue"}
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_message_string_safe_return(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """当 message 是字符串（而非 dict）时，安全返回，不抛异常。"""
        kwargs: dict[str, Any] = {"message": "not_a_dict"}
        result = await plugin.on_message(**kwargs)

        assert result == {"action": "continue"}
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply.assert_not_awaited()

    # ── kwargs 缺少 message 键 ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_message_key_safe_return(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """当 kwargs 缺少 'message' 键时，安全返回，不抛异常。"""
        result = await plugin.on_message(**{})

        assert result == {"action": "continue"}
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply.assert_not_awaited()

    # ── handle_user_reply 抛出异常 ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_handle_user_reply_raises_gracefully(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """当 handle_user_reply 抛出异常时，捕获并返回 {'action': 'continue'}。"""
        tm = plugin._task_manager  # type: ignore[attr-defined]
        tm.handle_user_reply = AsyncMock(side_effect=RuntimeError("boom"))
        kwargs = self._make_message_kwargs()
        result = await plugin.on_message(**kwargs)

        assert result == {"action": "continue"}
