"""tools/synthetic/discovery.py — list_tools / get_tool_schema 分支测试。

agent_loop 集成测试已覆盖发现工具的 happy path；
本文件补齐 tool-not-found / 非 discoverable / 权限不足 / 异常重抛分支。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from oh_mai_agent.permission import Role
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry
from oh_mai_agent.tools.synthetic.discovery import (
    build_discovery_schemas,
    handle_get_tool_schema,
    handle_list_tools,
)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="read",
        description="读取文件",
        parameters={},
        handler=AsyncMock(return_value={"success": True}),
        visibility="discoverable",
        min_role=Role.USER,
    ))
    reg.register(ToolDefinition(
        name="ask_user",
        description="提问",
        parameters={},
        handler=AsyncMock(return_value={"success": True}),
        visibility="essential",
        min_role=Role.USER,
    ))
    return reg


# ═══════════════════════════════════════════════════════════════════════════════
# list_tools
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandleListTools:
    @pytest.mark.asyncio
    async def test_returns_discoverable_tools_for_role(
        self, registry: ToolRegistry,
    ) -> None:
        result = await handle_list_tools(registry, Role.USER)
        assert result["success"] is True
        names = {t["name"] for t in result["tools"]}
        assert names == {"read"}  # essential 工具不出现
        assert result["tools"][0]["description"] == "读取文件"

    @pytest.mark.asyncio
    async def test_list_discoverable_failure_reraises(
        self, registry: ToolRegistry, monkeypatch: Any,
    ) -> None:
        def _boom(role: Role) -> list:
            raise RuntimeError("registry down")

        monkeypatch.setattr(registry, "list_discoverable", _boom)
        with pytest.raises(RuntimeError, match="registry down"):
            await handle_list_tools(registry, Role.USER)


# ═══════════════════════════════════════════════════════════════════════════════
# get_tool_schema
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandleGetToolSchema:
    @pytest.mark.asyncio
    async def test_success_loads_schema(self, registry: ToolRegistry) -> None:
        loaded: set[str] = set()
        result = await handle_get_tool_schema(registry, loaded, Role.USER, "read")
        assert result["success"] is True
        assert result["schema"]["function"]["name"] == "read"
        assert "read" in loaded

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, registry: ToolRegistry) -> None:
        result = await handle_get_tool_schema(registry, set(), Role.USER, "ghost")
        assert result == {"success": False, "error": "tool not found: ghost"}

    @pytest.mark.asyncio
    async def test_essential_tool_rejected(self, registry: ToolRegistry) -> None:
        """essential 工具无需（也不应）经发现机制重复加载。"""
        result = await handle_get_tool_schema(registry, set(), Role.USER, "ask_user")
        assert result["success"] is False
        assert "not discoverable" in result["error"]

    @pytest.mark.asyncio
    async def test_permission_denied_for_guest(self, registry: ToolRegistry) -> None:
        result = await handle_get_tool_schema(registry, set(), Role.GUEST, "read")
        assert result == {"success": False, "error": "permission denied"}

    @pytest.mark.asyncio
    async def test_registry_failure_reraises(
        self, registry: ToolRegistry, monkeypatch: Any,
    ) -> None:
        def _boom(name: str) -> ToolDefinition | None:
            raise RuntimeError("registry down")

        monkeypatch.setattr(registry, "get", _boom)
        with pytest.raises(RuntimeError, match="registry down"):
            await handle_get_tool_schema(registry, set(), Role.USER, "read")


# ═══════════════════════════════════════════════════════════════════════════════
# schema 构建
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildDiscoverySchemas:
    def test_two_schemas_with_expected_names(self) -> None:
        schemas = build_discovery_schemas()
        assert [s["function"]["name"] for s in schemas] == ["list_tools", "get_tool_schema"]
