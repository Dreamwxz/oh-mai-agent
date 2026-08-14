"""tools/agent/plugin_api_tools.py — 动态跨插件 API 工具行为测试。

覆盖：API 列表归一化、handler 闭包（透传 / 包装 / 异常）、异步扫描、
异常回退为空列表。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from oh_mai_agent.tools.agent.plugin_api_tools import (
    _build_handler,
    _normalize_api_list,
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


# ═══════════════════════════════════════════════════════════════════════════════
# refresh_plugin_api_tools（异步扫描，task_manager 注册路径）
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefreshPluginApiTools:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            _api_entry("news.list", "查新闻", "1"),
        ]))
        tools = await refresh_plugin_api_tools(ctx_api)
        assert [t.name for t in tools] == ["call_news_list"]
        assert "查新闻" in tools[0].description

    @pytest.mark.asyncio
    async def test_scan_failure_falls_back_empty(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(side_effect=RuntimeError("scan down")))
        assert await refresh_plugin_api_tools(ctx_api) == []

    @pytest.mark.asyncio
    async def test_invalid_entries_skipped(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            "junk",
            {"api_name": ""},
            _api_entry("a.b"),
        ]))
        tools = await refresh_plugin_api_tools(ctx_api)
        assert [t.name for t in tools] == ["call_a_b"]
