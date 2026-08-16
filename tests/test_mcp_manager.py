"""tools/mcp/provider.py — MCPManager 与 mcp_* 工具注销测试。

MCPManager 集成测试仅覆盖全禁用路径：不建立任何网络连接、
不 spawn 子进程。启用路径的服务器解析行为由 test_mcp_presets.py
（resolve_effective_servers 纯函数）覆盖。
"""

from __future__ import annotations

import asyncio
import logging
import sys

import pytest

from oh_mai_agent.config import MCPConfig, MCPServerConfig
from oh_mai_agent.permission import Role
from oh_mai_agent.tools.mcp.provider import (
    MCPManager,
    _stdio_module_available,
    unregister_stale_mcp_tools,
)
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry


@pytest.mark.asyncio
async def test_start_with_all_disabled_connects_nothing() -> None:
    """全禁用路径：start() 不连接任何服务器，工具列表为空。"""
    cfg = MCPConfig(enabled=True, fetch_enabled=False, exa_enabled=False)
    mgr = MCPManager(cfg)
    try:
        await mgr.start()
        assert mgr._connections == {}
        assert mgr.get_all_tools() == []
    finally:
        await mgr.stop()


def test_unregister_stale_mcp_tools_removes_absent_mcp_tools() -> None:
    """注销不在新列表中的 mcp_* 工具；保留仍在生效的与非 mcp_ 工具。"""

    async def _placeholder(**kwargs: object) -> dict:
        return {"success": True}

    reg = ToolRegistry()
    for name in ("mcp_a_x", "mcp_b_y", "subagent_create"):
        reg.register(
            ToolDefinition(
                name=name,
                description="",
                parameters={},
                handler=_placeholder,
                visibility="discoverable",
                min_role=Role.USER,
            )
        )

    unregister_stale_mcp_tools(reg, {"mcp_b_y"})
    assert reg.get("mcp_a_x") is None
    assert reg.get("mcp_b_y") is not None
    assert reg.get("subagent_create") is not None

    # 空列表：全部 mcp_* 工具注销，非 mcp_ 工具保留
    unregister_stale_mcp_tools(reg, set())
    assert reg.get("mcp_b_y") is None
    assert reg.get("subagent_create") is not None


def test_stdio_module_available_positive_and_negative() -> None:
    """预检：-m 模块可导入放行、模块缺失跳过、非 -m 形态不预检直接放行。"""
    ok = MCPServerConfig(
        name="ok",
        transport="stdio",
        command=sys.executable,
        args=["-m", "croniter"],
    )
    assert _stdio_module_available(ok) is True

    missing = MCPServerConfig(
        name="missing",
        transport="stdio",
        command=sys.executable,
        args=["-m", "oh_mai_agent_missing_module_xyz"],
    )
    assert _stdio_module_available(missing) is False

    no_module_flag = MCPServerConfig(
        name="serve",
        transport="stdio",
        command="npx",
        args=["serve"],
    )
    assert _stdio_module_available(no_module_flag) is True


@pytest.mark.asyncio
async def test_start_skips_stdio_server_with_missing_module(caplog: pytest.LogCaptureFixture) -> None:
    """stdio 服务器 -m 模块缺失时被预检快速跳过：不 spawn 子进程、无连接、无工具。"""
    cfg = MCPConfig(
        enabled=True,
        fetch_enabled=False,
        exa_enabled=False,
        servers=[
            MCPServerConfig(
                name="bad",
                transport="stdio",
                command=sys.executable,
                args=["-m", "oh_mai_agent_missing_module_xyz"],
            )
        ],
    )
    mgr = MCPManager(cfg)
    try:
        with caplog.at_level(
            logging.WARNING, logger="oh_mai_agent.tools.mcp.provider"
        ):
            await mgr.start()
        assert mgr._connections == {}
        assert mgr.get_all_tools() == []
        # 判别器：预检 warning 出现即证明预检被真实执行（而非 spawn 快速失败）
        assert any(
            "Python 模块 'oh_mai_agent_missing_module_xyz' 未安装" in r.message
            for r in caplog.records
        )
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_start_startup_timeout_closes_partial_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() 超过 startup_timeout_s 时关闭已建连接并跳过剩余，不向上抛异常。"""
    from oh_mai_agent.tools.mcp.provider import MCPConnection

    async def hang(self: MCPConnection) -> None:
        await asyncio.sleep(30)

    # patch 的是类对象上的方法：provider 与 connection 模块共享同一类对象
    monkeypatch.setattr(MCPConnection, "initialize", hang)

    cfg = MCPConfig(
        enabled=True,
        fetch_enabled=False,
        exa_enabled=False,
        servers=[
            MCPServerConfig(name="hang", transport="http", url="http://127.0.0.1:1")
        ],
    )
    mgr = MCPManager(cfg, startup_timeout_s=0.3)
    try:
        await mgr.start()  # 超时分支正常返回，不抛异常
        assert mgr._connections == {}
        assert mgr.get_all_tools() == []
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_start_startup_timeout_keeps_healthy_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整体启动超时时只关闭未完成握手的连接，已初始化的健康连接保留可用。"""
    from oh_mai_agent.tools.mcp.provider import MCPConnection

    async def fake_initialize(self: MCPConnection) -> bool:
        if self.name == "ok":
            self._initialized = True  # 模拟握手成功（无网络）
            return True
        await asyncio.sleep(30)

    monkeypatch.setattr(MCPConnection, "initialize", fake_initialize)

    cfg = MCPConfig(
        enabled=True,
        fetch_enabled=False,
        exa_enabled=False,
        servers=[
            MCPServerConfig(name="ok", transport="http", url="http://127.0.0.1:1"),
            MCPServerConfig(name="hang", transport="http", url="http://127.0.0.1:1"),
        ],
    )
    # 总上限先触发（0.3s < 每服务器 10s 预算），per-server 不介入
    mgr = MCPManager(cfg, startup_timeout_s=0.3, per_server_timeout_s=10.0)
    try:
        await mgr.start()  # 超时分支正常返回，不抛异常
        # 健康连接（ok）保留，未完成握手的连接（hang）被关闭
        assert "ok" in mgr._connections
        assert "hang" not in mgr._connections
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_start_per_server_timeout_skips_hanging_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个服务器启动超时（per_server_timeout_s）时跳过该服务器并继续，不抛异常。"""
    from oh_mai_agent.tools.mcp.provider import MCPConnection

    async def fake_initialize(self: MCPConnection) -> bool:
        await asyncio.sleep(30)

    monkeypatch.setattr(MCPConnection, "initialize", fake_initialize)

    cfg = MCPConfig(
        enabled=True,
        fetch_enabled=False,
        exa_enabled=False,
        servers=[
            MCPServerConfig(name="hang", transport="http", url="http://127.0.0.1:1")
        ],
    )
    # per-server 预算先触发（0.3s < 总上限 10s）
    mgr = MCPManager(cfg, startup_timeout_s=10.0, per_server_timeout_s=0.3)
    try:
        await mgr.start()  # ~0.3s 内返回，不抛异常
        assert mgr._connections == {}
        assert mgr.get_all_tools() == []
    finally:
        await mgr.stop()
