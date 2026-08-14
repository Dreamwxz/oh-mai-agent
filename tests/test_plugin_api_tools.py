"""tools/agent/plugin_api_tools.py — 动态跨插件 API 工具行为测试。

覆盖：API 列表归一化、协程同步执行（含运行中事件循环的安全失败）、
handler 闭包（透传 / 包装 / 异常）、同步与异步扫描、异常回退为空列表。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import MockCtx

from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.plugin_api_tools import (
    _build_handler,
    _normalize_api_list,
    _run_coroutine_sync,
    _scan_and_build,
    build_plugin_api_tools,
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


class TestRunCoroutineSync:
    def test_non_coroutine_passthrough(self) -> None:
        assert _run_coroutine_sync("plain") == "plain"

    def test_coroutine_without_running_loop_runs(self) -> None:
        """无运行中事件循环（同步测试）→ asyncio.run 直接执行。"""
        async def _coro() -> str:
            return "done"

        assert _run_coroutine_sync(_coro()) == "done"

    @pytest.mark.asyncio
    async def test_coroutine_with_running_loop_closed_safely(self) -> None:
        """已有运行中事件循环 → 关闭协程并返回 None（避免跨线程执行 IPC）。"""
        async def _coro() -> str:
            return "done"

        result = _run_coroutine_sync(_coro())
        assert result is None


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
# _scan_and_build / build_plugin_api_tools（同步扫描）
# ═══════════════════════════════════════════════════════════════════════════════

class TestScanAndBuild:
    def test_builds_tools_from_api_list(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            _api_entry("weather.get", "查天气", "2"),
            _api_entry("stock.query", "查股票", "1"),
        ]))
        tools = _scan_and_build(ctx_api)
        names = {t.name for t in tools}
        assert names == {"call_weather_get", "call_stock_query"}
        weather = next(t for t in tools if t.name == "call_weather_get")
        assert weather.visibility == "discoverable"
        assert weather.min_role == Role.USER
        assert "查天气" in weather.description
        assert "API 版本：2。" in weather.description

    def test_scan_failure_falls_back_empty(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(side_effect=RuntimeError("scan down")))
        assert _scan_and_build(ctx_api) == []

    def test_async_list_without_loop_runs_sync(self) -> None:
        """ctx_api.list() 返回协程（无运行中事件循环）→ 同步执行成功。"""
        async def _list() -> list[dict]:
            return [_api_entry("ping")]

        tools = _scan_and_build(SimpleNamespace(list=_list))
        assert [t.name for t in tools] == ["call_ping"]

    def test_invalid_entries_skipped(self) -> None:
        ctx_api = SimpleNamespace(list=AsyncMock(return_value=[
            "not-a-dict",
            {"description": "缺 api_name"},
            _api_entry("valid"),
        ]))
        tools = _scan_and_build(ctx_api)
        assert [t.name for t in tools] == ["call_valid"]

    def test_build_plugin_api_tools_without_api_attr(self) -> None:
        ctx = SimpleNamespace()  # 无 api 属性
        assert build_plugin_api_tools(ctx) == []

    def test_build_plugin_api_tools_with_api_none(self) -> None:
        assert build_plugin_api_tools(MockCtx()) == []  # MockCtx.api 默认 None


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
