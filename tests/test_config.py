"""oh_mai_agent.config 的测试 — Pydantic 配置模型的默认值与字段结构。"""

from __future__ import annotations

import pytest

from oh_mai_agent.config import (
    ApiExposeConfig,
    MaibotAgentConfig,
    MCPConfig,
    MCPServerConfig,
    PermissionConfig,
    PlannerBoardConfig,
    PolishConfig,
    TaskConfig,
)


class TestPermissionConfig:
    def test_defaults(self) -> None:
        cfg = PermissionConfig()
        assert cfg.admins == []
        assert cfg.admin_groups == []
        assert cfg.users == []
        assert cfg.user_groups == []
        assert cfg.admin_in_group_chats is False

    def test_custom_admins(self) -> None:
        cfg = PermissionConfig(admins=["qq:10001", "qq:10002"])
        assert len(cfg.admins) == 2
        assert "qq:10001" in cfg.admins

    def test_custom_groups(self) -> None:
        cfg = PermissionConfig(admin_groups=["qq:group:123"], user_groups=["qq:group:456"])
        assert cfg.admin_groups == ["qq:group:123"]
        assert cfg.user_groups == ["qq:group:456"]

    def test_admin_in_group_chats_on(self) -> None:
        cfg = PermissionConfig(admin_in_group_chats=True)
        assert cfg.admin_in_group_chats is True


class TestTaskConfig:
    def test_defaults(self) -> None:
        cfg = TaskConfig()
        assert cfg.max_concurrent_tasks == 4
        assert cfg.max_runtime_min == 0
        assert cfg.default_timeout_min == 10
        assert cfg.persist_history is True

    def test_custom_concurrency(self) -> None:
        cfg = TaskConfig(max_concurrent_tasks=8, max_runtime_min=30)
        assert cfg.max_concurrent_tasks == 8
        assert cfg.max_runtime_min == 30

class TestPlannerBoardConfig:
    def test_defaults(self) -> None:
        cfg = PlannerBoardConfig()
        assert cfg.enabled is True
        assert cfg.max_active == 5
        assert cfg.max_scheduled == 3
        assert cfg.max_recent == 3

    def test_disabled(self) -> None:
        cfg = PlannerBoardConfig(enabled=False)
        assert cfg.enabled is False

    def test_custom_limits(self) -> None:
        cfg = PlannerBoardConfig(max_active=10, max_scheduled=5, max_recent=5)
        assert cfg.max_active == 10
        assert cfg.max_scheduled == 5
        assert cfg.max_recent == 5


class TestPolishConfig:
    def test_defaults(self) -> None:
        cfg = PolishConfig()
        assert cfg.use_jargon is True

    def test_jargon_disabled(self) -> None:
        cfg = PolishConfig(use_jargon=False)
        assert cfg.use_jargon is False


class TestMCPServerConfig:
    def test_defaults(self) -> None:
        cfg = MCPServerConfig()
        assert cfg.name == ""
        assert cfg.transport == "stdio"
        assert cfg.command == ""
        assert cfg.args == []
        assert cfg.env == {}
        assert cfg.url == ""
        assert cfg.headers == {}

    def test_stdio_server(self) -> None:
        cfg = MCPServerConfig(
            name="test-server",
            command="python",
            args=["-m", "mymod"],
            env={"KEY": "val"},
        )
        assert cfg.name == "test-server"
        assert cfg.transport == "stdio"
        assert cfg.command == "python"
        assert cfg.args == ["-m", "mymod"]
        assert cfg.env == {"KEY": "val"}

    def test_http_server(self) -> None:
        cfg = MCPServerConfig(
            name="http-srv",
            transport="http",
            url="http://localhost:8080",
            headers={"Authorization": "Bearer xyz"},
        )
        assert cfg.transport == "http"
        assert cfg.url == "http://localhost:8080"
        assert cfg.headers == {"Authorization": "Bearer xyz"}


class TestMCPConfig:
    def test_defaults(self) -> None:
        cfg = MCPConfig()
        assert cfg.enabled is True
        assert cfg.fetch_enabled is True
        assert cfg.exa_enabled is True
        assert cfg.servers == []

    def test_with_servers(self) -> None:
        srv = MCPServerConfig(name="srv", command="python")
        cfg = MCPConfig(servers=[srv])
        assert len(cfg.servers) == 1
        assert cfg.servers[0].name == "srv"


class TestApiExposeConfig:
    def test_defaults(self) -> None:
        cfg = ApiExposeConfig()
        assert cfg.max_level == "user"

    def test_admin_level(self) -> None:
        cfg = ApiExposeConfig(max_level="admin")
        assert cfg.max_level == "admin"

    def test_guest_level(self) -> None:
        cfg = ApiExposeConfig(max_level="guest")
        assert cfg.max_level == "guest"


class TestMaibotAgentConfig:
    def test_defaults(self) -> None:
        cfg = MaibotAgentConfig()
        assert isinstance(cfg.permission, PermissionConfig)
        assert isinstance(cfg.task, TaskConfig)
        assert isinstance(cfg.planner_board, PlannerBoardConfig)
        assert isinstance(cfg.polish, PolishConfig)
        assert isinstance(cfg.mcp, MCPConfig)
        assert isinstance(cfg.api_expose, ApiExposeConfig)

    def test_nested_config_defaults(self) -> None:
        cfg = MaibotAgentConfig()
        assert cfg.permission.admin_in_group_chats is False
        assert cfg.task.max_concurrent_tasks == 4
        assert cfg.planner_board.enabled is True
        assert cfg.polish.use_jargon is True
        assert cfg.mcp.enabled is True
        assert cfg.api_expose.max_level == "user"

    def test_custom_nested_config(self) -> None:
        cfg = MaibotAgentConfig(
            task=TaskConfig(max_concurrent_tasks=10),
            planner_board=PlannerBoardConfig(enabled=False),
            polish=PolishConfig(use_jargon=False),
        )
        assert cfg.task.max_concurrent_tasks == 10
        assert cfg.planner_board.enabled is False
        assert cfg.polish.use_jargon is False
        # 其余配置节保持默认值不变
        assert cfg.permission.admin_in_group_chats is False
        assert cfg.mcp.enabled is True
