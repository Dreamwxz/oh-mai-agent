"""内置 fetch 预设的 URL 出站安全校验（SSRF 缓解）。

背景：内置 fetch 预设（mcp-server-fetch）会抓取任意 URL，Agent 可能被提示词注入
诱导访问本机回环、内网服务或云元数据地址（如 ``http://169.254.169.254/``）。
上游服务器自身只做 robots.txt 合规检查，没有任何地址过滤
（`mcp_server_fetch` 的 ``fetch_url`` 直接用 httpx GET，``follow_redirects=True``），
本模块在插件层补一道前置校验。

策略（黑名单兜底）：拦截本机回环、私有网段、链路本地（含云元数据
169.254.169.254）、CGNAT、组播/保留段与 IPv6 回环/ULA/链路本地地址；
``localhost`` 及子域直接拦截；域名先解析再逐 IP 判定。

尽力而为边界（无法在插件层完全防御，见 docs/features/08-mcp.md）：
  - DNS 重绑定：校验与抓取是两次独立解析，攻击者可先返回公网 IP 通过校验、
    再向服务器返回内网 IP；
  - 重定向：服务器侧 ``follow_redirects=True``，公网 URL 可 302 到内网地址。
二者均需代理层或服务器侧加固才能根治。

关闭开关：``[mcp] fetch_block_internal = false``（部署者显式放行内网抓取场景）。
校验只作用于内置 fetch 预设（MCPManager 按服务器签名识别），用户自定义
MCP 服务器由部署者自行负责。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: DNS 解析超时（秒）：超时视为不可解析，放行（请求本身也会失败，无实际风险）。
_DNS_TIMEOUT_S: float = 3.0

#: 拦截网段（IPv4）。覆盖：本网 0/8、私有 10/8 与 172.16/12 与 192.168/16、
#: CGNAT 100.64/10（含阿里云元数据 100.100.100.200）、回环 127/8、
#: 链路本地 169.254/16（含云元数据 169.254.169.254）、IANA 保留 192.0.0.0/24、
#: 基准测试 198.18/15、组播 224/4、保留 240/4。
_BLOCKED_IPV4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ip_network("0.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("100.64.0.0/10"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("192.0.0.0/24"),
    ip_network("198.18.0.0/15"),
    ip_network("224.0.0.0/4"),
    ip_network("240.0.0.0/4"),
)

#: 拦截网段（IPv6）。覆盖：未指定 ::/128、回环 ::1、ULA fc00::/7、
#: 链路本地 fe80::/10、组播 ff00::/8。IPv4 映射地址（::ffff:x.x.x.x）在
#: _is_blocked_address 中解包为 IPv4 后用 IPv4 表判定。
_BLOCKED_IPV6_NETWORKS: tuple[ipaddress.IPv6Network, ...] = (
    ip_network("::/128"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
    ip_network("fe80::/10"),
    ip_network("ff00::/8"),
)


def is_blocked_address(addr: IPv4Address | IPv6Address) -> bool:
    """判定 *addr* 是否命中拦截网段（IPv4 映射地址解包为 IPv4 再判定）。"""
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, IPv4Address):
        return any(addr in net for net in _BLOCKED_IPV4_NETWORKS)
    return any(addr in net for net in _BLOCKED_IPV6_NETWORKS)


def _is_localhost_name(host: str) -> bool:
    """判定主机名是否为 localhost 或 *.localhost（大小写不敏感）。"""
    return host == "localhost" or host.endswith(".localhost")


async def _resolve(host: str) -> list[IPv4Address | IPv6Address]:
    """解析主机名的全部 IP（IPv4/IPv6）；失败或超时返回空列表。"""
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo, host, None, type=socket.SOCK_STREAM
            ),
            timeout=_DNS_TIMEOUT_S,
        )
    except (socket.gaierror, asyncio.TimeoutError):
        return []
    ips: list[IPv4Address | IPv6Address] = []
    for info in infos:
        try:
            ips.append(ip_address(info[4][0]))
        except ValueError:
            continue
    return ips


async def validate_fetch_url(url: str) -> str | None:
    """校验内置 fetch 的目标 *url*；返回错误消息或 ``None``（放行）。

    规则（按序）：
      1. 仅接受 http/https scheme；
      2. 主机名必须是 IP 字面量或可解析域名，缺主机名拒绝；
      3. ``localhost`` / ``*.localhost`` 直接拒绝；
      4. IP 字面量命中拦截网段拒绝；
      5. 域名解析结果中任一 IP 命中拦截网段拒绝（尽力而为）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"fetch 仅支持 http/https URL: {url}"
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return f"URL 缺少主机名: {url}"

    if _is_localhost_name(host):
        return (
            f"目标主机 {host} 是本机地址，已被 fetch 安全策略拦截"
            "（内网抓取需部署者将 [mcp] fetch_block_internal 设为 false）"
        )

    # IP 字面量：直接按网段判定，无需 DNS
    try:
        addr = ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        if is_blocked_address(addr):
            return (
                f"目标地址 {host} 命中内网/链路本地/云元数据网段，已被 fetch 安全策略拦截"
                "（内网抓取需部署者将 [mcp] fetch_block_internal 设为 false）"
            )
        return None

    # 域名：解析后逐 IP 判定（尽力而为，DNS 重绑定无法完全防御）
    ips = await _resolve(host)
    for addr in ips:
        if is_blocked_address(addr):
            return (
                f"目标域名 {host} 解析到内网/链路本地/云元数据地址 {addr.compressed}，"
                "已被 fetch 安全策略拦截"
                "（内网抓取需部署者将 [mcp] fetch_block_internal 设为 false）"
            )
    return None
