"""oh_mai_agent.tools.registry 的测试——工具注册、权限过滤、
essential/discoverable 两级呈现与执行权限门控。"""

from __future__ import annotations

import pytest

from oh_mai_agent.permission import Role
from oh_mai_agent.tools.registry import (
    ToolDefinition,
    ToolRegistry,
    build_llm_tool_schemas,
)


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

async def _echo_handler(**kwargs) -> dict:
    return {"success": True, "echo": kwargs}


async def _secret_handler(**kwargs) -> dict:
    return {"success": True, "secret": "data"}


async def _failing_handler(**kwargs) -> dict:
    raise RuntimeError("boom")


async def _guest_handler(**kwargs) -> dict:
    return {"success": True, "role": "guest"}


# ── 测试 ─────────────────────────────────────────────────────────────────────

class TestToolDefinition:
    def test_to_llm_definition(self) -> None:
        td = ToolDefinition(
            name="echo",
            description="Echo back the input",
            parameters={"type": "object", "properties": {}},
            handler=_echo_handler,
            visibility="essential",
            min_role=Role.GUEST,
        )
        schema = td.to_llm_definition()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "echo"
        assert schema["function"]["description"] == "Echo back the input"
        assert schema["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_defaults(self) -> None:
        td = ToolDefinition(
            name="test",
            description="desc",
            parameters={},
            handler=_echo_handler,
        )
        assert td.visibility == "discoverable"
        assert td.min_role == Role.GUEST


class TestToolRegistryRegistration:
    def test_register_and_get(self, tool_registry: ToolRegistry) -> None:
        td = ToolDefinition(name="echo", description="", parameters={}, handler=_echo_handler)
        tool_registry.register(td)
        assert tool_registry.get("echo") is td
        assert tool_registry.get("no-such") is None

    def test_register_overwrite(self, tool_registry: ToolRegistry) -> None:
        td1 = ToolDefinition(name="echo", description="v1", parameters={}, handler=_echo_handler)
        td2 = ToolDefinition(name="echo", description="v2", parameters={}, handler=_echo_handler)
        tool_registry.register(td1)
        tool_registry.register(td2)
        assert tool_registry.get("echo").description == "v2"

    def test_all_names_preserves_order(self, tool_registry: ToolRegistry) -> None:
        for i in range(3):
            tool_registry.register(ToolDefinition(name=f"t{i}", description="", parameters={}, handler=_echo_handler))
        assert tool_registry.all_names() == ["t0", "t1", "t2"]

    def test_register_duplicate_name_does_not_reorder(self, tool_registry: ToolRegistry) -> None:
        tool_registry.register(ToolDefinition(name="a", description="", parameters={}, handler=_echo_handler))
        tool_registry.register(ToolDefinition(name="b", description="", parameters={}, handler=_echo_handler))
        tool_registry.register(ToolDefinition(name="a", description="new", parameters={}, handler=_echo_handler))
        assert tool_registry.all_names() == ["a", "b"]


class TestToolRegistryRoleFiltering:
    @pytest.fixture
    def populated_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        # essential 级工具
        reg.register(ToolDefinition(name="guest_tool", description="", parameters={},
                                     handler=_guest_handler, visibility="essential", min_role=Role.GUEST))
        reg.register(ToolDefinition(name="user_tool", description="", parameters={},
                                     handler=_echo_handler, visibility="essential", min_role=Role.USER))
        reg.register(ToolDefinition(name="admin_tool", description="", parameters={},
                                     handler=_secret_handler, visibility="essential", min_role=Role.ADMIN))
        # discoverable 级工具
        reg.register(ToolDefinition(name="disc_guest", description="", parameters={},
                                     handler=_guest_handler, visibility="discoverable", min_role=Role.GUEST))
        reg.register(ToolDefinition(name="disc_admin", description="", parameters={},
                                     handler=_secret_handler, visibility="discoverable", min_role=Role.ADMIN))
        return reg

    def test_names_admin_sees_all(self, populated_registry: ToolRegistry) -> None:
        admin_names = populated_registry.names(Role.ADMIN)
        assert len(admin_names) == 5
        assert "admin_tool" in admin_names
        assert "disc_admin" in admin_names

    def test_names_user_sees_guest_and_user(self, populated_registry: ToolRegistry) -> None:
        user_names = populated_registry.names(Role.USER)
        assert "guest_tool" in user_names
        assert "user_tool" in user_names
        assert "disc_guest" in user_names
        assert "admin_tool" not in user_names
        assert "disc_admin" not in user_names

    def test_names_guest_sees_only_guest(self, populated_registry: ToolRegistry) -> None:
        guest_names = populated_registry.names(Role.GUEST)
        assert guest_names == ["guest_tool", "disc_guest"]

    def test_list_definitions_role_filtered(self, populated_registry: ToolRegistry) -> None:
        defs = populated_registry.list_definitions(Role.GUEST)
        assert len(defs) == 2
        assert all(d.min_role == Role.GUEST for d in defs)

    def test_list_essential(self, populated_registry: ToolRegistry) -> None:
        ess = populated_registry.list_essential(Role.ADMIN)
        assert len(ess) == 3
        assert all(d.visibility == "essential" for d in ess)

    def test_list_essential_user_role(self, populated_registry: ToolRegistry) -> None:
        ess = populated_registry.list_essential(Role.USER)
        assert len(ess) == 2  # guest_tool + user_tool
        assert all(d.visibility == "essential" for d in ess)

    def test_list_discoverable(self, populated_registry: ToolRegistry) -> None:
        disc = populated_registry.list_discoverable(Role.ADMIN)
        assert len(disc) == 2
        assert all(d.visibility == "discoverable" for d in disc)

    def test_list_discoverable_guest_sees_only_guest(self, populated_registry: ToolRegistry) -> None:
        disc = populated_registry.list_discoverable(Role.GUEST)
        assert len(disc) == 1
        assert disc[0].name == "disc_guest"


class TestToolRegistryExecute:
    @pytest.fixture
    def exec_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(ToolDefinition(name="echo", description="", parameters={},
                                     handler=_echo_handler, min_role=Role.GUEST))
        reg.register(ToolDefinition(name="admin_only", description="", parameters={},
                                     handler=_secret_handler, min_role=Role.ADMIN))
        return reg

    @pytest.mark.asyncio
    async def test_execute_success(self, exec_registry: ToolRegistry) -> None:
        result = await exec_registry.execute("echo", Role.ADMIN, message="hello")
        assert result["success"] is True
        assert result["echo"] == {"message": "hello"}

    @pytest.mark.asyncio
    async def test_execute_not_found(self, exec_registry: ToolRegistry) -> None:
        result = await exec_registry.execute("nonexistent", Role.ADMIN)
        assert result["success"] is False
        assert "tool not found" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_permission_denied(self, exec_registry: ToolRegistry) -> None:
        result = await exec_registry.execute("admin_only", Role.GUEST)
        assert result["success"] is False
        assert result["error"] == "permission denied"

    @pytest.mark.asyncio
    async def test_execute_permission_granted(self, exec_registry: ToolRegistry) -> None:
        result = await exec_registry.execute("admin_only", Role.ADMIN)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_execute_handler_exception(self, tool_registry: ToolRegistry) -> None:
        tool_registry.register(ToolDefinition(name="failer", description="", parameters={},
                                                handler=_failing_handler, min_role=Role.GUEST))
        result = await tool_registry.execute("failer", Role.ADMIN)
        assert result["success"] is False
        assert result["error"] == "boom"


class TestToolRegistryUnregister:
    def test_unregister_removes_tool(self, tool_registry: ToolRegistry) -> None:
        tool_registry.register(
            ToolDefinition(name="echo", description="", parameters={}, handler=_echo_handler)
        )
        assert tool_registry.get("echo") is not None

        tool_registry.unregister("echo")

        assert tool_registry.get("echo") is None
        assert "echo" not in tool_registry.all_names()
        assert "echo" not in tool_registry.names(Role.USER)

    def test_unregister_missing_name_is_idempotent(self, tool_registry: ToolRegistry) -> None:
        tool_registry.register(
            ToolDefinition(name="echo", description="", parameters={}, handler=_echo_handler)
        )

        # 不存在的名字不抛异常（幂等）
        tool_registry.unregister("no-such-tool")

        assert tool_registry.all_names() == ["echo"]
        assert tool_registry.get("echo") is not None

    def test_unregister_preserves_other_tools(self, tool_registry: ToolRegistry) -> None:
        for name in ("a", "b", "c"):
            tool_registry.register(
                ToolDefinition(name=name, description="", parameters={}, handler=_echo_handler)
            )

        tool_registry.unregister("b")

        assert tool_registry.all_names() == ["a", "c"]
        assert tool_registry.get("a") is not None
        assert tool_registry.get("b") is None
        assert tool_registry.get("c") is not None


class TestBuildLLMToolSchemas:
    def test_empty_list(self) -> None:
        assert build_llm_tool_schemas([]) == []

    def test_multiple_definitions(self) -> None:
        tds = [
            ToolDefinition(name="a", description="desc_a", parameters={}, handler=_echo_handler),
            ToolDefinition(name="b", description="desc_b", parameters={}, handler=_echo_handler),
        ]
        schemas = build_llm_tool_schemas(tds)
        assert len(schemas) == 2
        assert schemas[0]["function"]["name"] == "a"
        assert schemas[1]["function"]["name"] == "b"
