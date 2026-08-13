"""内置 MCP 服务器预设与生效列表解析。"""
from __future__ import annotations

import logging
import sys

from ...config import MCPConfig, MCPServerConfig

logger = logging.getLogger(__name__)

EXA_URL = "https://mcp.exa.ai/mcp?tools=web_search_exa"


def _exa_preset() -> MCPServerConfig:
    return MCPServerConfig(
        name="exa",
        transport="http",
        url=EXA_URL,
        headers={},
    )


def _fetch_preset() -> MCPServerConfig:
    return MCPServerConfig(
        name="fetch",
        transport="stdio",
        command=sys.executable,
        args=["-m", "mcp_server_fetch"],
        env={"PYTHONIOENCODING": "utf-8"},
    )


def resolve_effective_servers(cfg: MCPConfig) -> list[MCPServerConfig]:
    """按开关与去重规则组装最终 MCP 服务器列表。

    顺序：内置 exa（启用且用户列表无同名/同 URL 项时）→ 内置 fetch（启用且用户列表无同名项时）
    → 追加用户自定义 servers（同名/同 URL = 用户条目覆盖预设）。
    """
    servers: list[MCPServerConfig] = []
    if cfg.exa_enabled and not any(
        s.name == "exa" or s.url == EXA_URL for s in cfg.servers
    ):
        servers.append(_exa_preset())
    if cfg.fetch_enabled and not any(s.name == "fetch" for s in cfg.servers):
        servers.append(_fetch_preset())
    servers.extend(cfg.servers)
    return servers
