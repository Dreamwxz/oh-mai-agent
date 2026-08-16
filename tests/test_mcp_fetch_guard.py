"""tools/mcp/fetch_guard.py — 内置 fetch URL 安全校验（SSRF 缓解）测试。

纯函数路径（IP 网段判定）不依赖网络；域名解析路径 monkeypatch
``socket.getaddrinfo``，避免真实 DNS；provider 层用假连接验证
call_tool 拦截点，不 spawn 子进程。
"""

from __future__ import annotations

import socket
import sys

import pytest

from oh_mai_agent.config import MCPConfig, MCPServerConfig
from oh_mai_agent.tools.mcp import fetch_guard as fetch_guard_module
from oh_mai_agent.tools.mcp.fetch_guard import is_blocked_address, validate_fetch_url
from oh_mai_agent.tools.mcp.provider import MCPManager, _is_builtin_fetch_server


# ── is_blocked_address：网段判定 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "addr",
    [
        # IPv4：回环 / 私有 / CGNAT / 链路本地（含云元数据）/ 本网 / 基准测试 / 组播 / 保留
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.10",
        "100.100.100.200",
        "100.64.0.1",
        "169.254.169.254",
        "169.254.0.1",
        "0.0.0.0",
        "198.18.0.1",
        "224.0.0.1",
        "240.0.0.1",
        # IPv6：回环 / 未指定 / ULA / 链路本地 / 组播
        "::1",
        "::",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        # IPv4 映射地址：解包后按 IPv4 网段判定
        "::ffff:127.0.0.1",
        "::ffff:192.168.1.1",
    ],
)
def test_is_blocked_address_hits_blocklist(addr: str) -> None:
    from ipaddress import ip_address

    assert is_blocked_address(ip_address(addr)) is True


@pytest.mark.parametrize(
    "addr",
    [
        "8.8.8.8",
        "1.1.1.1",
        "9.9.9.9",
        "2001:4860:4860::8888",
        "2606:4700:4700::1111",
    ],
)
def test_is_blocked_address_allows_public(addr: str) -> None:
    from ipaddress import ip_address

    assert is_blocked_address(ip_address(addr)) is False


# ── validate_fetch_url：字面量与主机名路径 ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5:8080/admin",
        "http://192.168.1.10/page",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://[::1]/",
    ],
)
async def test_validate_blocked_ip_literals(url: str) -> None:
    err = await validate_fetch_url(url)
    assert err is not None
    assert "已被 fetch 安全策略拦截" in err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://example.com/path?q=1",
        "http://8.8.8.8/",
        "https://[2001:4860:4860::8888]/",
    ],
)
async def test_validate_allows_public_urls(url: str) -> None:
    assert await validate_fetch_url(url) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://localhost:3000/admin",
        "http://foo.localhost/",
        "http://LOCALHOST/",
        "http://a.b.localhost:8080/",
    ],
)
async def test_validate_blocks_localhost_names(url: str) -> None:
    err = await validate_fetch_url(url)
    assert err is not None
    assert "本机地址" in err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com/",
        "not-a-url",
    ],
)
async def test_validate_rejects_non_http_scheme(url: str) -> None:
    err = await validate_fetch_url(url)
    assert err is not None
    assert "仅支持 http/https" in err


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http:///path", "http://?q=1"])
async def test_validate_rejects_missing_host(url: str) -> None:
    assert await validate_fetch_url(url) is not None


# ── validate_fetch_url：域名解析路径（monkeypatch，无真实 DNS） ────────────────


def _patch_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch, ips: list[str] | type[Exception]
) -> None:
    def fake(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        if isinstance(ips, list):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips
            ]
        raise ips(host)

    # 直接对模块对象打补丁，避免字符串导入路径解析（全量测试下
    # oh_mai_agent.tools 属性可能未挂载导致 AttributeError）
    monkeypatch.setattr(fetch_guard_module.socket, "getaddrinfo", fake)


@pytest.mark.asyncio
async def test_validate_blocks_domain_resolving_to_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_getaddrinfo(monkeypatch, ["169.254.169.254"])
    err = await validate_fetch_url("http://metadata.example.test/latest/")
    assert err is not None
    assert "解析到" in err
    assert "已被 fetch 安全策略拦截" in err


@pytest.mark.asyncio
async def test_validate_blocks_domain_resolving_to_mixed_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多 IP 解析结果中任一命中内网即拦截（如 CDN 回源到内网的情形）。"""
    _patch_getaddrinfo(monkeypatch, ["10.0.0.5", "8.8.8.8"])
    err = await validate_fetch_url("http://mixed.example.test/")
    assert err is not None


@pytest.mark.asyncio
async def test_validate_allows_domain_resolving_to_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_getaddrinfo(monkeypatch, ["8.8.8.8", "2001:4860:4860::8888"])
    assert await validate_fetch_url("http://public.example.test/") is None


@pytest.mark.asyncio
async def test_validate_allows_when_dns_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析失败放行：请求本身也会失败，无实际风险（尽力而为语义）。"""
    _patch_getaddrinfo(monkeypatch, socket.gaierror)
    assert await validate_fetch_url("http://no-such-host.invalid/") is None


# ── provider 层：内置 fetch 签名识别与 call_tool 拦截点 ───────────────────────


def test_is_builtin_fetch_server_signature() -> None:
    preset = MCPServerConfig(
        name="fetch",
        transport="stdio",
        command=sys.executable,
        args=["-m", "mcp_server_fetch"],
    )
    assert _is_builtin_fetch_server(preset) is True

    other_module = MCPServerConfig(
        name="other",
        transport="stdio",
        command=sys.executable,
        args=["-m", "some_other_module"],
    )
    assert _is_builtin_fetch_server(other_module) is False

    other_command = MCPServerConfig(
        name="fetch",
        transport="stdio",
        command="npx",
        args=["-m", "mcp_server_fetch"],
    )
    assert _is_builtin_fetch_server(other_command) is False


class _FakeConn:
    """记录调用并返回固定结果的假连接；被拦截时不应被触达。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> list[dict]:
        self.calls.append((name, arguments))
        return [{"type": "text", "text": "ok"}]

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_call_tool_intercepts_blocked_fetch_url() -> None:
    """内置 fetch 服务器 + 内网 URL：连接层不被触达，直接返回拦截错误。"""
    conn = _FakeConn()
    mgr = MCPManager(MCPConfig(enabled=True, fetch_enabled=False, exa_enabled=False))
    mgr._connections = {"fetch": conn}  # type: ignore[assignment]
    mgr._guarded_fetch_servers = {"fetch"}
    try:
        result = await mgr.call_tool(
            "fetch", "fetch", {"url": "http://169.254.169.254/latest/meta-data/"}
        )
        assert result["success"] is False
        assert "已被 fetch 安全策略拦截" in result["error"]
        assert conn.calls == []
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_call_tool_passes_public_url() -> None:
    """公网 URL 正常放行到连接层。"""
    conn = _FakeConn()
    mgr = MCPManager(MCPConfig(enabled=True, fetch_enabled=False, exa_enabled=False))
    mgr._connections = {"fetch": conn}  # type: ignore[assignment]
    mgr._guarded_fetch_servers = {"fetch"}
    try:
        result = await mgr.call_tool(
            "fetch", "fetch", {"url": "https://example.com/page"}
        )
        assert result == {"success": True, "content": [{"type": "text", "text": "ok"}]}
        assert conn.calls == [("fetch", {"url": "https://example.com/page"})]
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_call_tool_guard_off_when_config_disabled() -> None:
    """fetch_block_internal=false：内网 URL 放行（部署者显式选择）。"""
    conn = _FakeConn()
    mgr = MCPManager(
        MCPConfig(
            enabled=True,
            fetch_enabled=False,
            exa_enabled=False,
            fetch_block_internal=False,
        )
    )
    mgr._connections = {"fetch": conn}  # type: ignore[assignment]
    mgr._guarded_fetch_servers = {"fetch"}
    try:
        result = await mgr.call_tool(
            "fetch", "fetch", {"url": "http://10.0.0.5/admin"}
        )
        assert result["success"] is True
        assert conn.calls == [("fetch", {"url": "http://10.0.0.5/admin"})]
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_call_tool_guard_scoped_to_fetch_tool_and_server() -> None:
    """校验只作用于内置 fetch 服务器的 fetch 工具：其他工具与自定义服务器不受限。"""
    conn = _FakeConn()
    mgr = MCPManager(MCPConfig(enabled=True, fetch_enabled=False, exa_enabled=False))
    mgr._connections = {"fetch": conn, "custom": conn}  # type: ignore[assignment]
    mgr._guarded_fetch_servers = {"fetch"}
    try:
        # 同一服务器上的非 fetch 工具不校验
        await mgr.call_tool("fetch", "other_tool", {"url": "http://127.0.0.1/"})
        # 自定义服务器（不在守卫集合）上的同名工具不校验
        await mgr.call_tool("custom", "fetch", {"url": "http://127.0.0.1/"})
        assert len(conn.calls) == 2
    finally:
        await mgr.stop()
