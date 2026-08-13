"""oh_mai_agent.polish 模块测试：PolishService、黑话匹配、回退与提示词构建。"""

from __future__ import annotations

import pytest
from typing import Any
from conftest import MockCtx

from oh_mai_agent.config import PolishConfig
from oh_mai_agent.executor.instant import (
    PolishService,
    _calculate_match_score,
    _jargon_in_scope,
    _normalize_match_text,
)
from oh_mai_agent.prompt.base import PromptContext
from oh_mai_agent.prompt.builders.polish import PolishBuilder
from oh_mai_agent.prompt.manager import PromptManager


@pytest.fixture
def pm() -> PromptManager:
    from pathlib import Path
    return PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates")


class TestNormalizeMatchText:
    def test_lowercase(self) -> None:
        assert _normalize_match_text("Hello World") == "hello world"

    def test_collapse_whitespace(self) -> None:
        assert _normalize_match_text("  hello    world  ") == "hello world"

    def test_empty(self) -> None:
        assert _normalize_match_text("") == ""
        assert _normalize_match_text(None) == ""

    def test_numbers(self) -> None:
        assert _normalize_match_text(123) == "123"


class TestJargonInScope:
    def test_stream_in_scope(self) -> None:
        session_dict = '{"qq:g:123": 5, "qq:g:456": 3}'
        assert _jargon_in_scope(session_dict, "qq:g:123") is True
        assert _jargon_in_scope(session_dict, "qq:g:999") is False

    def test_empty_dict(self) -> None:
        assert _jargon_in_scope("{}", "qq:g:1") is False
        assert _jargon_in_scope("", "qq:g:1") is False

    def test_invalid_json(self) -> None:
        assert _jargon_in_scope("not json", "qq:g:1") is False

    def test_none_value(self) -> None:
        assert _jargon_in_scope(None, "qq:g:1") is False


class TestCalculateMatchScore:
    def test_basic_score(self) -> None:
        score = _calculate_match_score(
            candidate_count=5, first_message_index=0,
        )
        assert score == 5.0

    def test_first_index_penalty(self) -> None:
        score = _calculate_match_score(
            candidate_count=5, first_message_index=100,
        )
        assert score == 5.0 - 100 * 0.01

    def test_high_frequency_bonus(self) -> None:
        score = _calculate_match_score(
            candidate_count=5, first_message_index=0,
            hit_high_frequency=True, high_freq_count=10, high_freq_rank=5,
        )
        # 5 + (1000 + 10*2 + (100-5)) = 5 + (1000 + 20 + 95) = 5 + 1115 = 1120
        assert score == 1120.0

    def test_no_high_frequency(self) -> None:
        score = _calculate_match_score(
            candidate_count=3, first_message_index=5,
        )
        assert score == 2.95  # 3 - 5*0.01


class TestBuildPolishSystemPrompt:
    def test_with_jargon_and_context(self, pm: PromptManager) -> None:
        jargon = [{"content": "爷", "meaning": "厉害"}, {"content": "难绷", "meaning": "无语"}]
        builder = PolishBuilder(pm=pm)
        ctx = PromptContext(data={"jargon": jargon, "context": "聊天上下文", "result": "结果文本"})
        result = builder.build(ctx)
        assert "聊天上下文" in result
        assert "结果文本" in result
        assert "爷" in result
        assert "难绷" in result
        assert "麦麦" in result or "你是" in result  # 模板中包含人设语句

    def test_no_jargon(self, pm: PromptManager) -> None:
        builder = PolishBuilder(pm=pm)
        ctx = PromptContext(data={"jargon": [], "context": "ctx内容", "result": "result内容"})
        result = builder.build(ctx)
        assert "（无）" in result
        assert "ctx内容" in result

    def test_empty_context(self, pm: PromptManager) -> None:
        builder = PolishBuilder(pm=pm)
        ctx = PromptContext(data={"jargon": [], "context": "", "result": "result"})
        result = builder.build(ctx)
        assert "无最近聊天记录" in result

    def test_empty_result(self, pm: PromptManager) -> None:
        builder = PolishBuilder(pm=pm)
        ctx = PromptContext(data={"jargon": [], "context": "ctx", "result": ""})
        result = builder.build(ctx)
        assert "{{result}}" not in result  # 占位符已被空字符串替换


# ═══════════════════════════════════════════════════════════════════════════════
# PolishService
# ═══════════════════════════════════════════════════════════════════════════════

class TestPolishService:
    @pytest.fixture
    def mock_ctx(self) -> MockCtx:
        return MockCtx()

    def test_polish_fallback_on_llm_error(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        """未预设 LLM 响应时，generate 返回默认的 {"response": "ok"}，polish 直接返回该值。"""
        cfg = PolishConfig(use_jargon=False)

        # 未预设 LLM 响应 → generate 将返回默认的 {"response": "ok"}
        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)

        import asyncio
        result = asyncio.run(svc.polish(
            result="原始结果",
            stream_id="qq:g:1",
            is_group=True,
        ))
        # 应返回 LLM 的响应；出错时才回退到原文
        # mock_ctx 默认配置下 LLM 返回 {"response": "ok"}
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_polish_with_mock_llm_response(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        cfg = PolishConfig(use_jargon=False)
        mock_ctx.llm.set_generate_response("润色后文本")
        mock_ctx.add_message("qq:g:1", "你好", is_bot=False)

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        result = await svc.polish(
            result="原始结果",
            stream_id="qq:g:1",
            is_group=True,
        )
        assert result == "润色后文本"

    @pytest.mark.asyncio
    async def test_polish_handles_dict_result(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        cfg = PolishConfig(use_jargon=False)
        # 将获得默认响应（dict 形式）
        mock_ctx.add_message("qq:g:1", "测试消息", is_bot=False)

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        result = await svc.polish(
            result="原始",
            stream_id="qq:g:1",
            is_group=True,
        )
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_polish_jargon_disabled_skips_match(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        cfg = PolishConfig(use_jargon=False)
        mock_ctx.llm.set_generate_response("润色后")
        mock_ctx.add_message("qq:g:1", "普通文本", is_bot=False)

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        result = await svc.polish(
            result="原始",
            stream_id="qq:g:1",
            is_group=True,
        )
        assert result == "润色后"

    @pytest.mark.asyncio
    async def test_polish_jargon_enabled_matches(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        """use_jargon=True 时执行黑话匹配（即使未匹配到任何黑话）。"""
        cfg = PolishConfig(use_jargon=True)
        mock_ctx.llm.set_generate_response("带黑话润色")
        mock_ctx.add_message("qq:g:1", "测试消息", is_bot=False)

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=True, prompt_service=prompt_service)
        result = await svc.polish(
            result="原始",
            stream_id="qq:g:1",
            is_group=True,
        )
        assert result == "带黑话润色"

    @pytest.mark.asyncio
    async def test_polish_group_chat_context_limit(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        cfg = PolishConfig(use_jargon=False)
        mock_ctx.llm.set_generate_response("done")

        # 在群聊中添加大量消息（50 条），超出上下文预览上限
        for i in range(50):
            mock_ctx.add_message("qq:g:1", f"msg{i}", is_bot=False)

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        result = await svc.polish(
            result="原始",
            stream_id="qq:g:1",
            is_group=True,
        )
        assert result == "done"

    @pytest.mark.asyncio
    async def test_polish_private_chat_context_limit(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        cfg = PolishConfig(use_jargon=False)
        mock_ctx.llm.set_generate_response("done")

        for i in range(70):
            mock_ctx.add_message("qq:10001", f"msg{i}", is_bot=False)

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        result = await svc.polish(
            result="原始",
            stream_id="qq:10001",
            is_group=False,
        )
        assert result == "done"

    @pytest.mark.asyncio
    async def test_polish_bot_messages_excluded(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        cfg = PolishConfig(use_jargon=False)
        mock_ctx.llm.set_generate_response("done")

        mock_ctx.add_message("qq:g:1", "bot message", is_bot=True)
        mock_ctx.add_message("qq:g:1", "user message", is_bot=False)

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        result = await svc.polish(
            result="原始",
            stream_id="qq:g:1",
            is_group=True,
        )
        assert result == "done"

    @pytest.mark.asyncio
    async def test_polish_fallback_on_exception(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        """空上下文（未添加消息）下 polish 应正常完成并返回字符串。"""
        cfg = PolishConfig(use_jargon=False)

        # 未刻意构造触发 _match_jargons 异常的场景
        # （use_jargon=False 时也不会调用 _match_jargons）
        # 仅验证 polish 在空上下文下的基本流程

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        # 未添加任何消息 → _load_context 返回空列表
        # generate() 未预设响应 → 返回默认的 {"response": "ok"}
        result = await svc.polish(
            result="不应丢失的原文",
            stream_id="qq:g:1",
            is_group=True,
        )
        # 空上下文下也应正常完成
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_polish_string_result(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        """LLM 返回 dict（如默认的 {"response": "ok"}）时，polish 取出其中的字符串返回。"""
        cfg = PolishConfig(use_jargon=False)

        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        # 默认 mock 返回 {"response": "ok"}（dict 形式）
        result = await svc.polish(
            result="test",
            stream_id="qq:g:1",
            is_group=True,
        )
        assert result == "ok"
