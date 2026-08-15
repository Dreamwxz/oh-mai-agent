"""send_message 工具测试 —— open_session 会话创建、润色委派、参数校验。"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import MockCtx
from oh_mai_agent.permission import Role
from oh_mai_agent.prompt.builders.context_note import ContextNoteBuilder
from oh_mai_agent.prompt.service import PromptService
from oh_mai_agent.tools.send_message import build_send_tool


def _make_prompt_service() -> PromptService:
    from pathlib import Path

    from oh_mai_agent.prompt.manager import PromptManager

    return PromptService(
        manager=PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates"),
        builders=[ContextNoteBuilder()],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# build_send_tool —— 工厂产出合法的 ToolDefinition
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildSendTool:
    def test_returns_tool_definition_with_expected_name(self) -> None:
        """给定 MockCtx 与 no-op 的 send_polished，build_send_tool 返回名为 send_message 的 ToolDefinition。"""
        ctx = MockCtx()

        async def _noop(text: str, stream_id: str, **kwargs: Any) -> None:
            pass

        tool = build_send_tool(ctx, send_polished=_noop)
        assert tool.name == "send_message"
        assert tool.visibility == "discoverable"
        assert tool.min_role == Role.USER

    def test_description_contains_relay_discipline(self) -> None:
        """工具描述包含转达纪律（委托人/转达关键词）。"""
        ctx = MockCtx()

        async def _noop(text: str, stream_id: str, **kwargs: Any) -> None:
            pass

        tool = build_send_tool(ctx, send_polished=_noop)
        assert "委托人" in tool.description
        assert "转达" in tool.description


# ═══════════════════════════════════════════════════════════════════════════════
# Handler —— open_session 会话创建
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageGroupId:
    @pytest.mark.asyncio
    async def test_group_id_opens_session_with_chat_type_group(self) -> None:
        """给定 group_id='12345'，open_session 以 chat_type='group' 和 group_id='12345' 被调用。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish, prompt_service=_make_prompt_service())
        result = await tool.handler(text="Hello", group_id="12345")

        assert result["success"] is True
        assert result["stream_id"] == "qq:g:12345"
        assert result["created"] is True
        assert len(polish_calls) == 1
        assert polish_calls[0]["text"] == "Hello"
        assert polish_calls[0]["stream_id"] == "qq:g:12345"

        # 发送后追加的上下文注记（两次追加：纯文本 + XML）
        assert len(ctx.maisaka.appends) == 2
        # [0] 纯文本记录
        pure = ctx.maisaka.appends[0]
        assert pure["stream_id"] == "qq:g:12345"
        assert pure["visible_text"] == "Hello"
        assert "message_id" not in pure or pure.get("message_id") == ""
        assert pure["source_kind"] == "plugin:oh-mai-agent:send_message"
        # [1] XML 系统注记
        note = ctx.maisaka.appends[1]
        assert note["stream_id"] == "qq:g:12345"
        assert note["visible_text"].startswith("<plugin_context_note")
        assert note["visible_text"].endswith("</plugin_context_note>")
        assert "麦麦在此流发送了消息：Hello" in note["visible_text"]
        assert note["message_id"].startswith("oh-mai-agent:send:")
        assert note["source_kind"] == "plugin:oh-mai-agent:send_message"


class TestSendMessageUserId:
    @pytest.mark.asyncio
    async def test_user_id_opens_session_with_chat_type_private(self) -> None:
        """给定 user_id='67890'，open_session 以 chat_type='private' 和 user_id='67890' 被调用。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hi", user_id="67890")

        assert result["success"] is True
        assert result["stream_id"] == "qq::67890"
        assert result["created"] is True
        assert len(polish_calls) == 1


class TestSendMessagePlatform:
    @pytest.mark.asyncio
    async def test_platform_passed_to_open_session(self) -> None:
        """给定 platform='discord'，open_session 收到 platform='discord'。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Yo", group_id="g999", platform="discord")

        assert result["success"] is True
        assert result["stream_id"] == "discord:g:g999"


# ═══════════════════════════════════════════════════════════════════════════════
# Handler —— 参数校验
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageValidation:
    @pytest.mark.asyncio
    async def test_neither_id_returns_error(self) -> None:
        """既未给 group_id 也未给 user_id 时，handler 返回 success=False 及校验错误。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hello")

        assert result["success"] is False
        assert "group_id" in result["error"] or "user_id" in result["error"]
        assert len(polish_calls) == 0

    @pytest.mark.asyncio
    async def test_both_ids_returns_error(self) -> None:
        """同时给定 group_id 与 user_id 时，handler 返回 success=False 及歧义错误。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hello", group_id="12345", user_id="67890")

        assert result["success"] is False
        assert "只能提供" in result["error"]
        assert len(polish_calls) == 0

    @pytest.mark.asyncio
    async def test_stream_and_group_still_conflicts_without_chat_id(self) -> None:
        """无 chat_id（无宿主注入）时，stream_id 与 group_id 并存仍报错。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hello", stream_id="s1", group_id="12345")

        assert result["success"] is False
        assert "只能提供其一" in result["error"]
        assert len(polish_calls) == 0

    @pytest.mark.asyncio
    async def test_host_injected_stream_id_stripped(self) -> None:
        """宿主注入 stream_id=chat_id 与 LLM 的 group_id 并存 → 剥离后走 group_id。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(
            text="Hello",
            group_id="12345",
            stream_id="current-session",  # 宿主注入（== chat_id）
            chat_id="current-session",
        )

        assert result["success"] is True, f"result={result}"
        assert result["stream_id"] == "qq:g:12345"
        assert polish_calls[0]["stream_id"] == "qq:g:12345"

    @pytest.mark.asyncio
    async def test_missing_text_returns_error(self) -> None:
        """text 为空时，handler 返回 success=False。"""
        ctx = MockCtx()

        async def _noop(text: str, stream_id: str, **kwargs: Any) -> None:
            pass

        tool = build_send_tool(ctx, send_polished=_noop)
        result = await tool.handler(group_id="12345")

        assert result["success"] is False
        assert "text" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# Handler —— 错误传播
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageErrorPropagation:
    @pytest.mark.asyncio
    async def test_open_session_returns_failure_dict(self) -> None:
        """open_session 返回 success=False 时，handler 返回错误且不调用 send_polished。"""
        ctx = MockCtx()

        orig_open = MockCtx._Chat.open_session

        async def _open_fail(chat_self, platform: str, chat_type: str, **kwargs: object) -> dict:
            return {"success": False, "error": "session creation failed"}
        MockCtx._Chat.open_session = _open_fail

        try:
            polish_calls: list[dict] = []

            async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
                polish_calls.append({})

            tool = build_send_tool(ctx, send_polished=_record_polish)
            result = await tool.handler(text="Hello", group_id="12345")

            assert result["success"] is False
            assert "session creation failed" in result["error"]
            assert len(polish_calls) == 0
        finally:
            MockCtx._Chat.open_session = orig_open

    @pytest.mark.asyncio
    async def test_send_polished_raises_exception(self) -> None:
        """send_polished 抛异常时，handler 返回 success=False 及错误信息。"""
        ctx = MockCtx()

        async def _raise_err(text: str, stream_id: str, **kwargs: Any) -> None:
            raise RuntimeError("send failed")

        tool = build_send_tool(ctx, send_polished=_raise_err)
        result = await tool.handler(text="Hello", group_id="12345")

        assert result["success"] is False
        assert "send failed" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# Handler —— 上下文注记（XML 标签化的发送消息可见性）
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageContextNote:
    @pytest.mark.asyncio
    async def test_append_failure_still_returns_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ctx.maisaka.context.append 抛异常时，handler 仍返回 success=True 且不向外传播该错误。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        async def _append_raises(**kwargs: object) -> None:
            raise RuntimeError("context append failed")

        monkeypatch.setattr(ctx.maisaka.context, "append", _append_raises)

        tool = build_send_tool(ctx, send_polished=_record_polish, prompt_service=_make_prompt_service())
        result = await tool.handler(text="Hello", group_id="12345")

        assert result["success"] is True
        assert result["stream_id"] == "qq:g:12345"
        assert len(polish_calls) == 1
        # 因 append 已被打补丁为抛异常，故没有任何追加记录
        assert len(ctx.maisaka.appends) == 0

    @pytest.mark.asyncio
    async def test_escaped_text_in_xml_note(self) -> None:
        """text 包含 XML 元字符时，XML 注记条目中的这些字符会被转义。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({})

        tool = build_send_tool(ctx, send_polished=_record_polish, prompt_service=_make_prompt_service())
        await tool.handler(text="</plugin_context_note> hack", group_id="12345")

        assert len(ctx.maisaka.appends) == 2
        # [0] 纯文本：原文，不做转义
        assert ctx.maisaka.appends[0]["visible_text"] == "</plugin_context_note> hack"
        # [1] XML 注记：已转义 —— 原始 XML 闭合标签以转义实体形式出现
        note_vt = ctx.maisaka.appends[1]["visible_text"]
        assert "&lt;/plugin_context_note&gt;" in note_vt
        # 未转义的原始标签不应出现在注记正文内（只在外层包装中出现）
        assert note_vt.count("</plugin_context_note>") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Handler —— open_session 返回对象（非 dict）
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageObjectResult:
    @pytest.mark.asyncio
    async def test_open_session_returns_object_instead_of_dict(self) -> None:
        """open_session 返回带 stream_id 与 created 属性的对象时，handler 从中提取这些字段。"""
        ctx = MockCtx()

        class SessionObj:
            stream_id = "qq:g:obj999"
            session_id = "qq:g:obj999"
            created = False
            chat_type = "group"

        orig_open = MockCtx._Chat.open_session

        async def _open_obj(chat_self, platform: str, chat_type: str, **kwargs: object) -> SessionObj:
            return SessionObj()
        MockCtx._Chat.open_session = _open_obj

        try:
            polish_calls: list[dict] = []

            async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
                polish_calls.append({"text": text, "stream_id": stream_id})

            tool = build_send_tool(ctx, send_polished=_record_polish)
            result = await tool.handler(text="Hello", group_id="obj999")

            assert result["success"] is True
            assert result["stream_id"] == "qq:g:obj999"
            assert result["created"] is False
            assert len(polish_calls) == 1
        finally:
            MockCtx._Chat.open_session = orig_open


# ═══════════════════════════════════════════════════════════════════════════════
# Handler —— 每次发送都调用 open_session（无 get_stream_by_* 预检）
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageOpenSessionCalled:
    @pytest.mark.asyncio
    async def test_user_id_calls_open_session_with_account_id_and_scope(self) -> None:
        """给定 user_id，open_session 以 account_id 与 scope 被调用；无 get_stream_by_* 调用。"""
        ctx = MockCtx()
        ctx._chat_streams = [{
            "stream_id": "real-session",
            "user_id": "user123",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": False,
            "chat_type": "private",
        }]
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hello", user_id="user123")

        assert result["success"] is True
        assert result["stream_id"] == "qq::user123"
        assert result["created"] is True
        assert len(ctx.chat._open_session_calls) == 1
        call = ctx.chat._open_session_calls[0]
        assert call["account_id"] == "3948827829"
        assert call["scope"] == ""
        assert call["user_id"] == "user123"
        assert call["chat_type"] == "private"
        # 无 get_stream_by_* 调用
        assert len(ctx.chat._stream_lookup_calls) == 0

    @pytest.mark.asyncio
    async def test_group_id_calls_open_session_with_account_id_and_scope(self) -> None:
        """给定 group_id，open_session 以 account_id 与 scope 被调用；无 get_stream_by_* 调用。"""
        ctx = MockCtx()
        ctx._chat_streams = [{
            "stream_id": "group-real",
            "group_id": "group999",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": True,
            "chat_type": "group",
        }]
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hi", group_id="group999")

        assert result["success"] is True
        assert result["stream_id"] == "qq:g:group999"
        assert result["created"] is True
        assert len(ctx.chat._open_session_calls) == 1
        call = ctx.chat._open_session_calls[0]
        assert call["account_id"] == "3948827829"
        assert call["group_id"] == "group999"
        assert call["chat_type"] == "group"
        assert len(ctx.chat._stream_lookup_calls) == 0

    @pytest.mark.asyncio
    async def test_open_session_created_flag_from_result(self) -> None:
        """open_session 返回 created=False 时，handler 返回 created=False。"""
        ctx = MockCtx()

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            pass

        orig_open = MockCtx._Chat.open_session

        async def _open_existing(chat_self, platform: str, chat_type: str, **kwargs: object) -> dict:
            return {"success": True, "stream_id": "qq::user123", "session_id": "qq::user123", "created": False}
        MockCtx._Chat.open_session = _open_existing

        try:
            tool = build_send_tool(ctx, send_polished=_record_polish)
            result = await tool.handler(text="Hello", user_id="user123")

            assert result["success"] is True
            assert result["stream_id"] == "qq::user123"
            assert result["created"] is False
        finally:
            MockCtx._Chat.open_session = orig_open


# ═══════════════════════════════════════════════════════════════════════════════
# Handler —— 从 get_all_streams 推导 account_id / scope
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageAccountIdScope:
    @pytest.mark.asyncio
    async def test_real_stream_passes_account_id_to_open_session(self) -> None:
        """get_all_streams 返回含 account_id 的流时，open_session 收到 account_id 与 scope。"""
        ctx = MockCtx()
        ctx._chat_streams = [{
            "stream_id": "a76770406deead4ad8ab9c389f8e36be",
            "user_id": "3783399364",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": False,
            "chat_type": "private",
        }]
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hello", user_id="3783399364")

        assert result["success"] is True
        # open_session 应已携带 account_id/scope 被调用
        assert len(ctx.chat._open_session_calls) == 1
        call = ctx.chat._open_session_calls[0]
        assert call["account_id"] == "3948827829"
        assert call["scope"] == ""

    @pytest.mark.asyncio
    async def test_group_real_stream_passes_account_id_to_open_session(self) -> None:
        """get_all_streams 返回含 account_id 的群流时，open_session 收到 account_id。"""
        ctx = MockCtx()
        ctx._chat_streams = [{
            "stream_id": "group-real-stream",
            "group_id": "987654321",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": True,
            "chat_type": "group",
        }]
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hi", group_id="987654321")

        assert result["success"] is True
        assert len(ctx.chat._open_session_calls) == 1
        assert ctx.chat._open_session_calls[0]["account_id"] == "3948827829"

    @pytest.mark.asyncio
    async def test_prefers_stream_with_account_id(self) -> None:
        """存在两条匹配流（一条带 account_id、一条不带）时，优先选用带 account_id 的那条。"""
        ctx = MockCtx()
        ctx._chat_streams = [
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

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            pass

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hello", user_id="3783399364")

        assert result["success"] is True
        assert len(ctx.chat._open_session_calls) == 1
        assert ctx.chat._open_session_calls[0]["account_id"] == "3948827829"

    @pytest.mark.asyncio
    async def test_no_matching_stream_falls_back_to_empty_account_id(self) -> None:
        """get_all_streams 中无匹配流时，open_session 以 account_id="" 被调用。"""
        ctx = MockCtx()
        ctx._chat_streams = [{
            "stream_id": "unrelated-stream",
            "user_id": "9999999999",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": False,
            "chat_type": "private",
        }]
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hello", user_id="3783399364")

        assert result["success"] is True
        assert len(ctx.chat._open_session_calls) == 1
        assert ctx.chat._open_session_calls[0]["account_id"] == ""
        assert ctx.chat._open_session_calls[0]["scope"] == ""

    @pytest.mark.asyncio
    async def test_empty_streams_list_still_works(self) -> None:
        """get_all_streams 返回空列表时，handler 仍成功（保留既有行为）。"""
        ctx = MockCtx()
        ctx._chat_streams = []
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        result = await tool.handler(text="Hello", user_id="67890")

        assert result["success"] is True
        assert result["stream_id"] == "qq::67890"
        assert len(ctx.chat._open_session_calls) == 1
        assert ctx.chat._open_session_calls[0]["account_id"] == ""

    @pytest.mark.asyncio
    async def test_get_all_streams_exception_falls_back(self) -> None:
        """get_all_streams 抛异常时，handler 回退到空 account_id 并成功。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({})

        orig_all = MockCtx._Chat.get_all_streams

        async def _raise(chat_self, platform: str = "qq") -> None:
            raise RuntimeError("get_all_streams failed")

        MockCtx._Chat.get_all_streams = _raise

        try:
            tool = build_send_tool(ctx, send_polished=_record_polish)
            result = await tool.handler(text="Hello", user_id="67890")

            assert result["success"] is True
            assert len(ctx.chat._open_session_calls) == 1
            assert ctx.chat._open_session_calls[0]["account_id"] == ""
        finally:
            MockCtx._Chat.get_all_streams = orig_all

    @pytest.mark.asyncio
    async def test_no_stream_lookup_calls_made(self) -> None:
        """正常发送时不进行任何 get_stream_by_* 调用 —— 仅使用 open_session。"""
        ctx = MockCtx()
        ctx._chat_streams = [{
            "stream_id": "real-session",
            "user_id": "3783399364",
            "account_id": "3948827829",
            "scope": "",
            "is_group_session": False,
            "chat_type": "private",
        }]

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            pass

        tool = build_send_tool(ctx, send_polished=_record_polish)
        await tool.handler(text="Hello", user_id="3783399364")

        assert len(ctx.chat._stream_lookup_calls) == 0
        assert len(ctx.chat._open_session_calls) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# stream_id 直发 —— 发送到指定聊天流（如其他用户的流），跳过建流
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageStreamId:
    @pytest.mark.asyncio
    async def test_stream_id_sends_directly_without_open_session(self) -> None:
        """提供 stream_id 时直接发送到该流，不调用 open_session。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"text": text, "stream_id": stream_id})

        tool = build_send_tool(ctx, send_polished=_record_polish, prompt_service=_make_prompt_service())
        result = await tool.handler(text="Hello", stream_id="qq:user:99999")

        assert result["success"] is True
        assert result["stream_id"] == "qq:user:99999"
        assert result["created"] is False
        assert len(ctx.chat._open_session_calls) == 0
        assert len(polish_calls) == 1
        assert polish_calls[0]["text"] == "Hello"
        assert polish_calls[0]["stream_id"] == "qq:user:99999"
        # 上下文记录照常（纯文本 + XML）
        assert len(ctx.maisaka.appends) == 2
        assert ctx.maisaka.appends[0]["stream_id"] == "qq:user:99999"
        assert ctx.maisaka.appends[1]["stream_id"] == "qq:user:99999"

    @pytest.mark.asyncio
    async def test_stream_id_group_detection(self) -> None:
        """流 ID 含 ':group:' 时按群聊推导 is_group=True。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append({"ok": True})

        tool = build_send_tool(ctx, send_polished=_record_polish)
        await tool.handler(text="Hello", stream_id="qq:group:88888")


    @pytest.mark.asyncio
    async def test_stream_id_conflicts_with_group_user(self) -> None:
        """stream_id 与 group_id/user_id 同时提供时返回错误。"""
        ctx = MockCtx()

        async def _noop(text: str, stream_id: str, **kwargs: Any) -> None:
            pass

        tool = build_send_tool(ctx, send_polished=_noop)
        result = await tool.handler(text="Hello", stream_id="qq:user:1", group_id="2")
        assert result["success"] is False
        assert "只能提供其一" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_target_reports_error(self) -> None:
        """未提供任何目标（stream_id/group_id/user_id 全空）时返回错误。"""
        ctx = MockCtx()

        async def _noop(text: str, stream_id: str, **kwargs: Any) -> None:
            pass

        tool = build_send_tool(ctx, send_polished=_noop)
        result = await tool.handler(text="Hello")
        assert result["success"] is False
        assert "stream_id" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# polish / split 可选项 —— 透传给 send_polished 回调
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendMessageOptionalFlags:
    @pytest.mark.asyncio
    async def test_resolve_relay_forwarded_to_callback(self) -> None:
        """注入的 resolve_relay 判定结果作为 relay_from 透传给 send_polished。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append(kwargs)

        async def _resolve(stream_id: str) -> str | None:
            return "千绘莉"

        tool = build_send_tool(ctx, send_polished=_record_polish, resolve_relay=_resolve)
        await tool.handler(text="Hello", user_id="67890")

        assert polish_calls[0]["relay_from"] == "千绘莉"

    @pytest.mark.asyncio
    async def test_default_relay_from_none(self) -> None:
        """未注入 resolve_relay（如 Planner 版）时回调收到 None（本人发言）。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append(kwargs)

        tool = build_send_tool(ctx, send_polished=_record_polish)
        await tool.handler(text="Hello", user_id="67890")

        assert polish_calls[0]["relay_from"] is None

    @pytest.mark.asyncio
    async def test_resolve_relay_exception_falls_back_none(self) -> None:
        """resolve_relay 抛异常时降级为 None（本人发言），不阻塞发送。"""
        ctx = MockCtx()
        polish_calls: list[dict] = []

        async def _record_polish(text: str, stream_id: str, **kwargs: Any) -> None:
            polish_calls.append(kwargs)

        async def _broken(stream_id: str) -> str | None:
            raise RuntimeError("boom")

        tool = build_send_tool(ctx, send_polished=_record_polish, resolve_relay=_broken)
        await tool.handler(text="Hello", user_id="67890")

        assert polish_calls[0]["relay_from"] is None

    @pytest.mark.asyncio
    async def test_send_failure_returns_error(self) -> None:
        """send_polished 抛异常时返回失败结果（重试已由回调内部处理）。"""
        ctx = MockCtx()

        async def _raise_err(text: str, stream_id: str, **kwargs: Any) -> None:
            raise RuntimeError("发送失败")

        tool = build_send_tool(ctx, send_polished=_raise_err)
        result = await tool.handler(text="Hello", user_id="67890")

        assert result["success"] is False
        assert "发送失败" in result["error"]
        assert len(ctx.maisaka.appends) == 0
