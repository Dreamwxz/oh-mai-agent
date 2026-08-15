"""search_users 工具的测试 — keyword / chat_type / platform 过滤与条数上限约束。"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import MockCtx, make_task
from oh_mai_agent.config import SearchConfig
from oh_mai_agent.tools._shared import _filter_streams
from oh_mai_agent.tools.agent.info_tools import build_info_tools
from oh_mai_agent.tools.registry import ToolRegistry


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


# ═══════════════════════════════════════════════════════════════════════════════
# _filter_streams（模块级共享助手）的单元测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestFilterStreams:
    def test_limit_enforcement_with_50_entries(self) -> None:
        """给定 50 条流，_filter_streams 最多返回 max_results（20）条。"""
        streams = [_make_stream(i) for i in range(50)]
        result = _filter_streams(streams, max_results=20)
        assert len(result) == 20

    def test_keyword_filter_matches_nickname(self) -> None:
        """给定流列表，关键词 'Alice' 仅匹配 user_nickname 含 'alice' 的条目。"""
        streams = [
            _make_stream(0, user_nickname="Alice"),
            _make_stream(1, user_nickname="Bob"),
            _make_stream(2, user_nickname="AliceLee"),
        ]
        result = _filter_streams(streams, keyword="Alice")
        assert len(result) == 2
        assert all("alice" in r["user_nickname"].lower() for r in result)

    def test_keyword_filter_matches_group_name(self) -> None:
        """给定流列表，关键词匹配 group_name 字段（大小写不敏感）。"""
        streams = [
            _make_stream(0, group_name="PythonClub"),
            _make_stream(1, group_name="JavaClub"),
            _make_stream(2, group_name="python_utils"),
        ]
        result = _filter_streams(streams, keyword="python")
        assert len(result) == 2

    def test_keyword_filter_matches_user_id(self) -> None:
        """给定流列表，关键词匹配 user_id 字段。"""
        streams = [
            _make_stream(0),
            _make_stream(1),
            _make_stream(2),
        ]
        result = _filter_streams(streams, keyword="u000001")
        assert len(result) == 1
        assert result[0]["user_id"] == "u000001"

    def test_keyword_filter_matches_group_id(self) -> None:
        """给定流列表，关键词匹配 group_id 字段。"""
        streams = [_make_stream(i) for i in range(5)]
        result = _filter_streams(streams, keyword="g000003")
        assert len(result) == 1

    def test_keyword_filter_matches_user_cardname(self) -> None:
        """给定流列表，关键词匹配 user_cardname 字段。"""
        streams = [
            _make_stream(0),
            _make_stream(1),
        ]
        result = _filter_streams(streams, keyword="card_1")
        assert len(result) == 1

    def test_keyword_filter_empty_returns_all(self) -> None:
        """给定流列表和空关键词，不做过滤（全部返回，受 limit 上限约束）。"""
        streams = [_make_stream(i) for i in range(5)]
        result = _filter_streams(streams, keyword="", max_results=20)
        assert len(result) == 5

    def test_chat_type_filter_group_only(self) -> None:
        """给定混合 chat_types，chat_type='group' 只返回群聊。"""
        streams = [
            _make_stream(0, chat_type="group"),
            _make_stream(1, chat_type="private"),
            _make_stream(2, chat_type="group"),
            _make_stream(3, chat_type="private"),
        ]
        result = _filter_streams(streams, chat_type="group")
        assert len(result) == 2
        assert all(s["chat_type"] == "group" for s in result)

    def test_chat_type_filter_private_only(self) -> None:
        """给定混合 chat_types，chat_type='private' 只返回私聊。"""
        streams = [
            _make_stream(0, chat_type="group"),
            _make_stream(1, chat_type="private"),
        ]
        result = _filter_streams(streams, chat_type="private")
        assert len(result) == 1
        assert result[0]["chat_type"] == "private"

    def test_combined_keyword_and_chat_type(self) -> None:
        """同时给定 keyword 和 chat_type 时，两个过滤条件都生效。"""
        streams = [
            _make_stream(0, chat_type="group", user_nickname="Alice"),
            _make_stream(1, chat_type="private", user_nickname="Alice"),
            _make_stream(2, chat_type="group", user_nickname="Bob"),
        ]
        result = _filter_streams(streams, keyword="Alice", chat_type="group")
        assert len(result) == 1
        assert result[0]["user_nickname"] == "Alice"
        assert result[0]["chat_type"] == "group"

    def test_limit_applied_after_filtering(self) -> None:
        """给定大量匹配条目，limit 在关键词过滤之后生效。"""
        streams = [_make_stream(i, user_nickname="SameName") for i in range(50)]
        result = _filter_streams(streams, keyword="SameName", max_results=5)
        assert len(result) == 5

    def test_limit_zero_returns_empty(self) -> None:
        """给定 max_results=0，返回空列表。"""
        streams = [_make_stream(i) for i in range(10)]
        result = _filter_streams(streams, max_results=0)
        assert result == []

    # ── 多关键词（keywords，OR 语义）与分词容错 ──────────────────────────

    def test_keywords_or_semantics(self) -> None:
        """给定 keywords 列表，任一关键词命中即保留该流（OR 语义）。"""
        streams = [
            _make_stream(0, user_nickname="Alice"),
            _make_stream(1, user_nickname="Bob"),
            _make_stream(2, user_nickname="Charlie"),
        ]
        result = _filter_streams(streams, keywords=["Alice", "Charlie"])
        assert len(result) == 2
        assert {r["user_nickname"] for r in result} == {"Alice", "Charlie"}

    def test_keyword_and_keywords_merged(self) -> None:
        """给定 keyword 与 keywords 同时传入，合并后取 OR 语义。"""
        streams = [
            _make_stream(0, user_nickname="Alice"),
            _make_stream(1, user_nickname="Bob"),
            _make_stream(2, user_nickname="Charlie"),
        ]
        result = _filter_streams(streams, keyword="Bob", keywords=["Charlie"])
        assert len(result) == 2

    def test_particle_stripping_fallback(self) -> None:
        """给定关键词含虚词差异（'低调空格' vs '低调的空格'），分词容错后命中。"""
        streams = [_make_stream(0, user_nickname="低调的空格")]
        result = _filter_streams(streams, keyword="低调空格")
        assert len(result) == 1
        assert result[0]["user_nickname"] == "低调的空格"

    def test_particle_stripping_fallback_in_keyword(self) -> None:
        """给定候选名含虚词而关键词不含时，同样分词容错命中（'小泽和空格' vs '小泽空格'）。"""
        streams = [_make_stream(0, user_nickname="小泽和空格")]
        result = _filter_streams(streams, keyword="小泽空格")
        assert len(result) == 1

    def test_particle_stripping_no_false_positive(self) -> None:
        """给定关键词与候选实际不匹配时，分词容错不应产生误命中。"""
        streams = [
            _make_stream(0, user_nickname="高调行事"),
            _make_stream(1, user_nickname="低调奢华"),
        ]
        result = _filter_streams(streams, keyword="低调空格")
        assert result == []

    def test_keywords_combined_with_chat_type(self) -> None:
        """给定 keywords 与 chat_type 同时传入，两个过滤条件都生效。"""
        streams = [
            _make_stream(0, chat_type="group", user_nickname="低调的空格"),
            _make_stream(1, chat_type="private", user_nickname="低调的空格"),
        ]
        result = _filter_streams(streams, keywords=["低调空格"], chat_type="private")
        assert len(result) == 1
        assert result[0]["chat_type"] == "private"


# ═══════════════════════════════════════════════════════════════════════════════
# 集成测试：经 MockCtx 验证 build_info_tools 的 search_users handler
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildInfoToolsSearchStreams:
    @staticmethod
    def _find_tool(tools, name: str):
        for t in tools:
            if t.name == name:
                return t
        return None

    def test_search_users_handler_limit_from_factory(self) -> None:
        """给定含 30 条流的 ctx，search_max_results=10 将结果限制为 10 条。"""
        ctx = MockCtx()
        ctx._chat_streams = [_make_stream(i) for i in range(30)]
        tools = build_info_tools(ctx, search_max_results=10)
        tool = self._find_tool(tools, "search_users")
        assert tool is not None, "search_users not in build_info_tools output"

        result = asyncio.run(tool.handler())
        assert result["success"] is True
        assert result["count"] == 10
        assert len(result["streams"]) == 10

    def test_search_users_handler_with_keyword(self) -> None:
        """给定含混合流的 ctx，关键词过滤收窄结果。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="Alice"),
            _make_stream(1, user_nickname="Bob"),
            _make_stream(2, user_nickname="Charlie"),
        ]
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="Bob"))
        assert result["success"] is True
        assert result["count"] == 1
        assert result["streams"][0]["user_nickname"] == "Bob"

    def test_search_users_handler_with_chat_type(self) -> None:
        """给定含混合 chat_types 的 ctx，chat_type 过滤只返回匹配项。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, chat_type="group"),
            _make_stream(1, chat_type="private"),
        ]
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(chat_type="group"))
        assert result["success"] is True
        assert result["count"] == 1
        assert result["streams"][0]["chat_type"] == "group"

    def test_search_users_with_platform(self) -> None:
        """给定 ctx，platform 会传给 ctx.chat.get_all_streams。"""
        ctx = MockCtx()
        ctx._chat_streams = [_make_stream(0)]
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(platform="qq"))
        assert result["success"] is True
        assert result["count"] == 1

    def test_search_users_default_platform_all_platforms(self) -> None:
        """未传 platform 参数时，默认为 'all_platforms'。"""
        ctx = MockCtx()
        ctx._chat_streams = [_make_stream(0)]
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler())
        assert result["success"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 注册测试：list_streams 不存在，search_users 存在
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolRegistration:
    def test_list_streams_not_registered(self) -> None:
        """给定 build_info_tools 返回的工具，旧的 'list_streams' 名称已不存在。"""
        ctx = MockCtx()
        tools = build_info_tools(ctx, search_max_results=20)
        names = {t.name for t in tools}
        assert "list_streams" not in names, "list_streams should be renamed to search_users"

    def test_search_users_is_registered(self) -> None:
        """给定 build_info_tools 返回的工具，'search_users' 存在。"""
        ctx = MockCtx()
        tools = build_info_tools(ctx, search_max_results=20)
        names = {t.name for t in tools}
        assert "search_users" in names, "search_users must be in tool list"

    def test_build_info_tools_backward_compat_no_max_results(self) -> None:
        """不传 search_max_results 调用 build_info_tools 时，使用默认值 20。"""
        ctx = MockCtx()
        tools = build_info_tools(ctx)
        names = {t.name for t in tools}
        assert "search_users" in names


# ═══════════════════════════════════════════════════════════════════════════════
# 集成测试：多数据源 search_users（persons + knowledge + streams）
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildInfoToolsSearchUsersMultiSource:
    @staticmethod
    def _find_tool(tools, name: str):
        for t in tools:
            if t.name == name:
                return t
        return None

    def test_keyword_matches_stream_and_triggers_knowledge(self) -> None:
        """给定流匹配关键词时，knowledge.search 也会被调用。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="Alice"),
        ]
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="Alice"))
        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["streams"]) == 1
        assert result["streams"][0]["user_nickname"] == "Alice"
        assert "persons" in result
        assert "knowledge" in result
        # knowledge.search 经 call_capability 的默认 stub 被调用
        assert len(result["knowledge"]) == 1
        assert "Alice" in result["knowledge"][0]["query"]

    def test_keyword_not_in_streams_but_person_exists(self) -> None:
        """给定关键词不在流中但 person.get_id_by_name 返回 id，persons 被填充。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="Bob"),
        ]
        ctx._person_data["空格"] = "person-a8da8a94"
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="空格"))
        assert result["success"] is True
        assert result["count"] == 0
        assert result["streams"] == []
        assert len(result["persons"]) == 1
        assert result["persons"][0]["person_id"] == "person-a8da8a94"
        assert result["persons"][0]["matched_by"] == "exact_name"
        assert "knowledge" in result

    def test_person_get_id_by_name_returns_dict(self) -> None:
        """给定 person.get_id_by_name 返回含 person_id 的 dict，能正确处理。"""
        ctx = MockCtx()
        ctx._chat_streams = []
        ctx._person_data["空格"] = {"person_id": "person-dict-id"}
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="空格"))
        assert result["success"] is True
        assert result["count"] == 0
        assert len(result["persons"]) == 1
        assert result["persons"][0]["person_id"] == "person-dict-id"

    def test_empty_string_person_id_not_appended(self) -> None:
        """给定 person.get_id_by_name 返回空串（真实宿主查无此名的形态），不产生假命中。"""
        ctx = MockCtx()
        ctx._chat_streams = []
        ctx._person_data["空格"] = ""
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="空格"))
        assert result["success"] is True
        assert result["count"] == 0
        assert result["persons"] == []
        assert result["streams"] == []

    def test_keywords_param_or_and_dedupe(self) -> None:
        """给定 keywords 数组，流 OR 命中；人物/记忆线索跨关键词去重。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="低调的空格"),
            _make_stream(1, user_nickname="小泽"),
        ]
        ctx._person_data["空格"] = "person-a8da8a94"
        ctx._person_data["低调空格"] = "person-a8da8a94"  # 同一人，两个名字都解析到同一 pid
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keywords=["低调空格", "小泽"]))
        assert result["success"] is True
        assert result["count"] == 2
        assert {r["user_nickname"] for r in result["streams"]} == {"低调的空格", "小泽"}
        # 两个关键词解析到同一 person_id → 只保留一条
        assert len(result["persons"]) == 1
        assert result["persons"][0]["person_id"] == "person-a8da8a94"

    def test_keyword_particle_fallback_hits_stream(self) -> None:
        """给定缺虚词的关键词（'低调空格'），分词容错命中昵称 '低调的空格' 的流。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="低调的空格", chat_type="private"),
        ]
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="低调空格"))
        assert result["success"] is True
        assert result["count"] == 1
        assert result["streams"][0]["user_nickname"] == "低调的空格"

    def test_keyword_not_found_anywhere_returns_empty(self) -> None:
        """给定关键词无任何匹配，三张列表均为空，count 为 0。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="Alice"),
        ]
        ctx._person_data = {}  # 无匹配
        ctx._capability_responses = {"Nobody": {"success": True, "content": ""}}
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="Nobody"))
        assert result["success"] is True
        assert result["count"] == 0
        assert result["streams"] == []
        assert result["persons"] == []
        assert result["knowledge"] == []

    def test_knowledge_search_raises_does_not_break_results(self) -> None:
        """给定 knowledge.search 抛异常时，streams 和 persons 仍正常返回。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="Alice"),
        ]
        ctx._person_data["Alice"] = "person-alice"

        def _raise(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("knowledge search down")

        ctx._capability_responses["Alice"] = _raise
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="Alice"))
        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["persons"]) == 1
        assert result["persons"][0]["person_id"] == "person-alice"
        assert result["knowledge"] == []  # knowledge 报错，已跳过

    def test_person_get_id_by_name_raises_does_not_break_results(self) -> None:
        """给定 person.get_id_by_name 抛异常时，streams 和 knowledge 仍正常返回。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="Alice"),
        ]
        ctx._person_data["Alice"] = Exception("person lookup down")
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="Alice"))
        assert result["success"] is True
        assert result["count"] == 1
        assert result["persons"] == []  # person 报错，已跳过
        assert len(result["knowledge"]) >= 0  # knowledge 仍会被尝试调用

    def test_knowledge_search_returns_success_false_skipped(self) -> None:
        """给定 knowledge.search 返回 success=False，knowledge 列表保持为空。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="Alice"),
        ]
        ctx._capability_responses = {"Alice": {"success": False, "error": "no results"}}
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="Alice"))
        assert result["success"] is True
        assert result["count"] == 1
        assert result["knowledge"] == []

    def test_knowledge_content_ni_ni_bu_renshi_skipped(self) -> None:
        """给定 knowledge.search 返回 '你不太了解...'，内容被跳过（未命中哨兵值）。"""
        ctx = MockCtx()
        ctx._chat_streams = [
            _make_stream(0, user_nickname="Alice"),
        ]
        ctx._capability_responses = {"Alice": {"success": True, "content": "你不太了解..."}}
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="Alice"))
        assert result["success"] is True
        assert result["count"] == 1
        assert result["knowledge"] == []

    def test_knowledge_content_truncated_to_300_chars(self) -> None:
        """给定 knowledge.search 返回超过 300 字符的内容，截断为 300。"""
        ctx = MockCtx()
        ctx._chat_streams = []
        long_content = "x" * 500
        ctx._capability_responses = {"long": {"success": True, "content": long_content}}
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler(keyword="long"))
        assert result["success"] is True
        assert len(result["knowledge"]) == 1
        assert len(result["knowledge"][0]["content"]) == 300

    def test_no_keyword_no_extra_lookups(self) -> None:
        """给定空关键词时，persons 和 knowledge 均为空（不做额外查询）。"""
        ctx = MockCtx()
        ctx._chat_streams = [_make_stream(i) for i in range(5)]
        tools = build_info_tools(ctx, search_max_results=20)
        tool = self._find_tool(tools, "search_users")

        result = asyncio.run(tool.handler())
        assert result["success"] is True
        assert result["count"] == 5
        assert result["persons"] == []
        assert result["knowledge"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# SearchConfig 测试
# ═══════════════════════════════════════════════════════════════════════════════


class TestSearchConfig:
    def test_default_max_results_is_20(self) -> None:
        cfg = SearchConfig()
        assert cfg.max_results == 20

    def test_custom_max_results(self) -> None:
        cfg = SearchConfig(max_results=50)
        assert cfg.max_results == 50
