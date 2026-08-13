"""tools/mcp/presets.py — 内置 MCP 服务器预设与生效列表解析测试。

resolve_effective_servers 是纯函数：仅按配置开关与去重规则组装配置对象，
不建立任何网络连接、不 spawn 子进程，因此测试只断言返回的配置对象字段。
"""

import sys

from oh_mai_agent.config import MCPConfig, MCPServerConfig
from oh_mai_agent.tools.mcp.presets import EXA_URL, resolve_effective_servers


def _names(servers):
    return [s.name for s in servers]


def test_default_resolves_exa_then_fetch_in_order():
    servers = resolve_effective_servers(MCPConfig())

    assert _names(servers) == ["exa", "fetch"]

    exa, fetch = servers
    assert exa.transport == "http"
    assert exa.url == EXA_URL
    assert exa.name == "exa"
    assert exa.headers == {}

    assert fetch.transport == "stdio"
    assert fetch.command == sys.executable
    assert fetch.args == ["-m", "mcp_server_fetch"]
    assert fetch.env == {"PYTHONIOENCODING": "utf-8"}
    assert fetch.name == "fetch"


def test_exa_disabled_resolves_only_fetch():
    servers = resolve_effective_servers(MCPConfig(exa_enabled=False))

    assert _names(servers) == ["fetch"]


def test_fetch_disabled_resolves_only_exa():
    servers = resolve_effective_servers(MCPConfig(fetch_enabled=False))

    assert _names(servers) == ["exa"]


def test_both_disabled_resolves_empty():
    servers = resolve_effective_servers(
        MCPConfig(exa_enabled=False, fetch_enabled=False)
    )

    assert servers == []


def test_user_servers_appended_after_presets():
    servers = resolve_effective_servers(
        MCPConfig(servers=[MCPServerConfig(name="custom", command="python")])
    )

    assert _names(servers) == ["exa", "fetch", "custom"]
    assert servers[-1].command == "python"


def test_user_entry_with_exa_url_skips_exa_preset():
    user_entry = MCPServerConfig(name="websearch", transport="http", url=EXA_URL)
    servers = resolve_effective_servers(MCPConfig(servers=[user_entry]))

    assert _names(servers) == ["fetch", "websearch"]
    assert servers[1] is user_entry


def test_user_entry_named_fetch_skips_fetch_preset():
    user_entry = MCPServerConfig(name="fetch", command="custom")
    servers = resolve_effective_servers(MCPConfig(servers=[user_entry]))

    assert _names(servers) == ["exa", "fetch"]
    assert servers[1] is user_entry
    assert servers[1].command == "custom"


def test_user_entry_named_exa_with_different_url_skips_exa_preset():
    user_entry = MCPServerConfig(
        name="exa", transport="http", url="https://other.example/mcp"
    )
    servers = resolve_effective_servers(MCPConfig(servers=[user_entry]))

    assert _names(servers) == ["fetch", "exa"]
    assert servers[1] is user_entry


def test_user_entry_same_name_but_exa_disabled_keeps_user_entry():
    user_entry = MCPServerConfig(name="exa", transport="http", url="https://other")
    servers = resolve_effective_servers(
        MCPConfig(exa_enabled=False, servers=[user_entry])
    )

    assert _names(servers) == ["fetch", "exa"]
    assert servers[1] is user_entry
