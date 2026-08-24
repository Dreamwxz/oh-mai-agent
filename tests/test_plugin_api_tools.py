"""tools/agent/plugin_api_tools.py — 动态跨插件 API 工具行为测试。

覆盖：API 列表归一化、handler 闭包（透传 / 包装 / 异常 / 身份键剥离）、
显式允许名单（默认不暴露）、最低角色 ADMIN、自身端点排除。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.plugin_api_tools import (
    MIN_API_TOOL_ROLE,
    PLUGIN_ID,
    _build_handler,
    _normalize_api_list,
    _is_own_api,
    refresh_plugin_api_tools,
)


def _api_entry(api_name: str = "weather.get", desc: str = "查天气", version: str = "2") -> dict:
    return {"api_name": api_name, "description": desc, "version": version}


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeApiList:
    def test_list_passthrough(self) -> None:
        raw = [{"api_name": "a"}]
        assert _normalize_api_list(raw) is raw

    def test_dict_with_apis_list(self) -> None:
        apis = [{"api_name": "a"}]
        assert _normalize_api_list({"apis": apis}) is apis

    def test_dict_with_non_list_apis(self) -> None:
        assert _normalize_api_list({"apis": "junk"}) == []

    def test_other_shapes(self) -> None:
        assert _normalize_api_list(None) == []
        assert _normalize_api_list("junk") == []


class TestIsOwnApi:
    def test_own_plugin_id_matches(self) -> None:
        assert _is_own_api({"api_name": "create", "plugin_id": PLUGIN_ID}) is True

    def test_foreign_plugin_id_not_matched(self) -> None:
        assert _is_own_api({"api_name": "create", "plugin_id": "other.plugin"}) is False

    def test_no_plugin_id_short_name_fallback(self) -> None:
        """无 plugin_id（mock/旧形态）时按端点短名 / 完整名前缀兜底匹配。"""
        assert _is_own_api({"api_name": "cancel"}) is True
        assert _is_own_api({"name": "inject"}) is True
        assert _is_own_api({"api_name": f"{PLUGIN_ID}.create"}) is True
        assert _is_own_api({"api_name": "weather.get"}) is False
        assert _is_own_api({"api_name": "other.create"}) is False

    def test_dotted_foreign_not_matched_without_plugin_id(self) -> None:
        """外部 API 即使以 get/list 等短名结尾也不误判为本插件端点。"""
        assert _is_own_api({"api_name": "news.list"}) is False
        assert _is_own_api({"api_name": "weather.get"}) is False


class TestBuildHandler:
    @pytest.mark.asyncio
    async def test_dict_result_passed_through(self) -> None:
        ctx_api = SimpleNamespace(call=AsyncMock(return_value={"success": True, "data": 1}))
        handler = _build_handler("weather.get", ctx_api)
        result = await handler(args={"city": "北京"})
        assert result == {"success": True, "data": 1}
        ctx_api.call.assert_awaited_once_with("weather.get", city="北京")

    @pytest.mark.asyncio
    async def test_non_dict_result_wrapped(self) -> None:
        ctx_api = SimpleNamespace(call=AsyncMock(return_value="raw"))
        handler = _build_handler("ping", ctx_api)
        result = await handler(args={})
        assert result == {"success": True, "result": "raw"}

    @pytest.mark.asyncio
    async def test_non_dict_args_falls_back_empty(self) -> None:
        """LLM 传错 args 类型时容错为空 dict。"""
        ctx_api = SimpleNamespace(call=AsyncMock(return_value={"success": True}))
        handler = _build_handler("ping", ctx_api)
        result = await handler(args="not-a-dict")
        assert result["success"] is True
        ctx_api.call.assert_awaited_once_with("ping")

    @pytest.mark.asyncio
    async def test_exception_returns_error(self) -> None:
        ctx_api = SimpleNamespace(call=AsyncMock(side_effect=RuntimeError("api down")))
        handler = _build_handler("ping", ctx_api)
        result = await handler(args={})
        assert result == {"success": False, "error": "api down"}

    @pytest.mark.asyncio
    async def test_caller_identity_keys_stripped(self) -> None:
        """保留身份参数（_caller_*）不得经工具通道透传伪造身份。"""
        ctx_api = SimpleNamespace(call=AsyncMock(return_value={"success": True}))
        handler = _build_handler("weather.get", ctx_api)
        result = await handler(args={
            "city": "北京",
            "_caller_role": "admin",
            "_caller_owner": "qq:99999",
        })
        assert result["success"] is True
        # 身份键被剥离，仅普通参数透传。
        ctx_api.call.assert_awaited_once_with("weather.get", city="北京")


# ═══════════════════════════════════════════════════════════════════════════════
# refresh_plugin_api_tools（异步扫描，task_manager 注册路径）
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefreshPluginApiTools:
    @pytest.mark.asyncio
    async def test_default_allowlist_exposes_nothing(self) -> None:
        """默认（无允许名单）不暴露任何 call_* 工具。"""
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            _api_entry("news.list", "查新闻", "1"),
        ]))
        tools = await refresh_plugin_api_tools(ctx_api)
        assert tools == []

    @pytest.mark.asyncio
    async def test_empty_allowlist_exposes_nothing(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            _api_entry("news.list", "查新闻", "1"),
        ]))
        tools = await refresh_plugin_api_tools(ctx_api, allowed_apis=[])
        assert tools == []

    @pytest.mark.asyncio
    async def test_allowlist_wraps_only_listed_apis_at_admin(self) -> None:
        """仅包装允许名单内的 API，且最低角色为 ADMIN。"""
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            _api_entry("news.list", "查新闻", "1"),
            _api_entry("weather.get", "查天气", "2"),
        ]))
        tools = await refresh_plugin_api_tools(ctx_api, allowed_apis=["weather.get"])
        assert [t.name for t in tools] == ["call_weather_get"]
        assert "查天气" in tools[0].description
        assert tools[0].min_role == Role.ADMIN
        assert tools[0].min_role == MIN_API_TOOL_ROLE

    @pytest.mark.asyncio
    async def test_own_endpoints_never_wrapped(self) -> None:
        """本插件自身 6 个端点即使出现在允许名单也不包装。"""
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            # Host 形态：带 plugin_id
            {"api_name": "create", "plugin_id": PLUGIN_ID, "description": "创建任务"},
            {"name": "inject", "plugin_id": PLUGIN_ID, "description": "注入指令"},
            # mock 形态：无 plugin_id，短名命中
            _api_entry("cancel", "取消任务"),
            # 外部插件同短名端点（带其他 plugin_id）：可包装
            {"api_name": "create", "plugin_id": "other.plugin", "description": "外部创建"},
        ]))
        tools = await refresh_plugin_api_tools(
            ctx_api,
            allowed_apis=["create", "inject", "cancel"],
        )
        assert [t.name for t in tools] == ["call_create"]

    @pytest.mark.asyncio
    async def test_name_key_compat(self) -> None:
        """Host 序列化条目用 name/full_name，而非 api_name。"""
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            {"name": "translate.run", "description": "翻译", "version": "1"},
        ]))
        tools = await refresh_plugin_api_tools(ctx_api, allowed_apis=["translate.run"])
        assert [t.name for t in tools] == ["call_translate_run"]

    @pytest.mark.asyncio
    async def test_scan_failure_falls_back_empty(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(side_effect=RuntimeError("scan down")))
        assert await refresh_plugin_api_tools(ctx_api, allowed_apis=["a.b"]) == []

    @pytest.mark.asyncio
    async def test_invalid_entries_skipped(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            "junk",
            {"api_name": ""},
            _api_entry("a.b"),
        ]))
        tools = await refresh_plugin_api_tools(ctx_api, allowed_apis=["a.b"])
        assert [t.name for t in tools] == ["call_a_b"]
