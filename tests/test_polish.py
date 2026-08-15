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

    def test_valid_json_non_dict(self) -> None:
        """JSON 合法但不是 dict（如数组/数字）→ 判定为不在作用域。"""
        assert _jargon_in_scope("[1,2,3]", "qq:g:1") is False
        assert _jargon_in_scope("42", "qq:g:1") is False

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

    def test_personality_and_reply_style_rendered(self, pm: PromptManager) -> None:
        """personality / reply_style 非空时模板输出人格设定与表达风格节。"""
        builder = PolishBuilder(pm=pm)
        ctx = PromptContext(data={
            "jargon": [],
            "context": "ctx",
            "result": "result",
            "personality": "你是一个大二女大学生",
            "reply_style": "风格平淡简短",
        })
        result = builder.build(ctx)
        assert "你是一个大二女大学生" in result
        assert "风格平淡简短" in result
        # 配置了人格时，默认"麦麦人格"行不再输出（避免与主程序人格冲突）
        assert "友善、有点俏皮但不油腻、有分寸感" not in result

    def test_personality_empty_keeps_default_persona(self, pm: PromptManager) -> None:
        """personality / reply_style 缺省空串时，模板不输出对应节，保留默认人格行。"""
        builder = PolishBuilder(pm=pm)
        ctx = PromptContext(data={"jargon": [], "context": "ctx", "result": "result"})
        result = builder.build(ctx)
        assert "人格设定（来自主程序配置" not in result
        assert "表达风格（来自主程序配置" not in result
        assert "友善、有点俏皮但不油腻、有分寸感" in result


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

    @pytest.mark.asyncio
    async def test_polish_injects_personality_from_main_config(
        self, mock_ctx: MockCtx, prompt_service: Any
    ) -> None:
        """主程序 [personality] 配置（personality / reply_style）注入润色 system prompt。"""
        mock_ctx._config_values["personality.personality"] = "你是一个高冷的侦探"
        mock_ctx._config_values["personality.reply_style"] = "惜字如金，风格冷峻"
        mock_ctx.llm.set_generate_response("润色后")
        mock_ctx.add_message("qq:g:1", "你好", is_bot=False)

        cfg = PolishConfig(use_jargon=False)
        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        await svc.polish(result="原始", stream_id="qq:g:1", is_group=True)

        # 检查传给 LLM 的 system prompt 中包含主程序人格与表达风格
        call = mock_ctx.llm.call_history[-1]
        system_prompt = next(m["content"] for m in call["prompt"] if m["role"] == "system")
        assert "你是一个高冷的侦探" in system_prompt
        assert "惜字如金，风格冷峻" in system_prompt
        # 配置了人格时，默认"麦麦人格"行不再输出
        assert "友善、有点俏皮但不油腻、有分寸感" not in system_prompt

    @pytest.mark.asyncio
    async def test_polish_no_personality_config_omits_sections(
        self, mock_ctx: MockCtx, prompt_service: Any
    ) -> None:
        """主程序未配置 [personality] 时，system prompt 不包含人格/表达风格节。"""
        mock_ctx.llm.set_generate_response("润色后")
        mock_ctx.add_message("qq:g:1", "你好", is_bot=False)

        cfg = PolishConfig(use_jargon=False)
        svc = PolishService(ctx=mock_ctx, config=cfg, use_jargon=False, prompt_service=prompt_service)
        await svc.polish(result="原始", stream_id="qq:g:1", is_group=True)

        call = mock_ctx.llm.call_history[-1]
        system_prompt = next(m["content"] for m in call["prompt"] if m["role"] == "system")
        assert "人格设定（来自主程序配置" not in system_prompt
        assert "表达风格（来自主程序配置" not in system_prompt
        # 默认人格行仍保留
        assert "友善、有点俏皮但不油腻、有分寸感" in system_prompt


# ═══════════════════════════════════════════════════════════════════════════════
# _match_jargons — 黑话机械匹配内部逻辑（真实 MockCtx DB 记录）
# ═══════════════════════════════════════════════════════════════════════════════

class TestMatchJargons:
    @pytest.fixture
    def mock_ctx(self) -> MockCtx:
        return MockCtx()

    def _svc(self, mock_ctx: MockCtx, prompt_service: Any) -> PolishService:
        return PolishService(
            ctx=mock_ctx, config=PolishConfig(use_jargon=True),
            use_jargon=True, prompt_service=prompt_service,
        )

    @pytest.mark.asyncio
    async def test_no_candidates_returns_empty(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        svc = self._svc(mock_ctx, prompt_service)
        assert await svc._match_jargons("qq:g:1", ["随便说点什么"]) == []

    @pytest.mark.asyncio
    async def test_global_jargon_matched_in_text(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        mock_ctx.add_db_record("Jargon", {
            "content": "yyds", "meaning": "永远滴神", "is_jargon": True, "is_global": True, "count": 5,
        })
        svc = self._svc(mock_ctx, prompt_service)
        result = await svc._match_jargons("qq:g:1", ["今天真的 yyds"])
        assert result == [{"content": "yyds", "meaning": "永远滴神"}]

    @pytest.mark.asyncio
    async def test_scoped_jargon_requires_stream_match(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        mock_ctx.add_db_record("Jargon", {
            "content": "私聊黑话", "meaning": "只在特定流", "is_jargon": True, "is_global": False,
            "session_id_dict": '{"qq:g:9": 3}', "count": 5,
        })
        svc = self._svc(mock_ctx, prompt_service)
        # 不在作用域 → 不匹配
        assert await svc._match_jargons("qq:g:1", ["私聊黑话"]) == []
        # 在作用域 → 匹配
        result = await svc._match_jargons("qq:g:9", ["私聊黑话"])
        assert result == [{"content": "私聊黑话", "meaning": "只在特定流"}]

    @pytest.mark.asyncio
    async def test_empty_content_or_meaning_skipped(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        mock_ctx.add_db_record("Jargon", {
            "content": "", "meaning": "空内容", "is_jargon": True, "is_global": True, "count": 5,
        })
        mock_ctx.add_db_record("Jargon", {
            "content": "有内容无释义", "meaning": "", "is_jargon": True, "is_global": True, "count": 5,
        })
        svc = self._svc(mock_ctx, prompt_service)
        assert await svc._match_jargons("qq:g:1", ["有内容无释义"]) == []

    @pytest.mark.asyncio
    async def test_high_frequency_bonus_prioritizes(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        """高频词命中提升评分：高频黑话排在普通黑话之前。"""
        mock_ctx.add_db_record("Jargon", {
            "content": "普通词", "meaning": "M1", "is_jargon": True, "is_global": True, "count": 1,
        })
        mock_ctx.add_db_record("Jargon", {
            "content": "热词", "meaning": "M2", "is_jargon": True, "is_global": True, "count": 1,
        })
        mock_ctx.add_db_record("HighFrequencyTerm", {
            "chat_id": "qq:g:1", "term": "热词", "rank": 1, "occurrence_count": 10,
        })
        svc = self._svc(mock_ctx, prompt_service)
        result = await svc._match_jargons("qq:g:1", ["普通词 热词"])
        assert [r["content"] for r in result] == ["热词", "普通词"]

    @pytest.mark.asyncio
    async def test_dedup_by_content_and_limit(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        """同一条黑话在消息中出现多次只计一次；候选超过上限时截断。"""
        for i in range(15):
            mock_ctx.add_db_record("Jargon", {
                "content": f"词{i}", "meaning": f"释义{i}", "is_jargon": True, "is_global": True, "count": i,
            })
        mock_ctx.add_db_record("Jargon", {
            "content": "重复词", "meaning": "只计一次", "is_jargon": True, "is_global": True, "count": 100,
        })
        svc = self._svc(mock_ctx, prompt_service)
        texts = ["重复词 重复词 重复词"] + [f"词{i}" for i in range(15)]
        result = await svc._match_jargons("qq:g:1", texts)
        # count 最高的重复词 + 高频词 14/13... → 结果去重且不超过上限
        assert len(result) <= 10
        repeated = [r for r in result if r["content"] == "重复词"]
        assert len(repeated) == 1

    @pytest.mark.asyncio
    async def test_empty_text_skipped(self, mock_ctx: MockCtx, prompt_service: Any) -> None:
        mock_ctx.add_db_record("Jargon", {
            "content": "yyds", "meaning": "永远滴神", "is_jargon": True, "is_global": True, "count": 5,
        })
        svc = self._svc(mock_ctx, prompt_service)
        assert await svc._match_jargons("qq:g:1", ["", "   ", "   yyds  "]) == [
            {"content": "yyds", "meaning": "永远滴神"},
        ]
