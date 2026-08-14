"""Planner tool: MCP 代理工具 handler 工厂。

提供两个供 Planner 调用的 @Tool 的 handler 逻辑体：
- ``list_mcp_tools`` — 列出所有 MCP 服务器及其可用工具
- ``call_mcp_tool`` — 调用指定 MCP 服务器的特定工具
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Awaitable
from typing import Any

logger = logging.getLogger(__name__)


def build_list_mcp_tools_handler(
    get_mcp: Callable[[], Any],
) -> Callable[..., Awaitable[dict]]:
    """返回 ``list_mcp_tools`` 的 handler 逻辑体。

    Args:
        get_mcp: 返回当前 MCPManager 实例的可调用对象（支持热更新后刷新引用）。
    """

    async def handler(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：列出所有 MCP 服务器及其可用工具。"""
        try:
            mcp = get_mcp()
            if mcp is None:
                return {
                    "success": True,
                    "servers": [],
                    "text": "MCP 未启用或尚未初始化，当前没有可用的 MCP 工具。",
                }

            tools = mcp.get_all_tools()
            if not tools:
                return {
                    "success": True,
                    "servers": [],
                    "text": "已启用 MCP，但未发现任何工具。",
                }

            # 按服务器分组
            servers: dict[str, list[dict]] = {}
            for t in tools:
                srv = t.get("server", "")
                servers.setdefault(srv, []).append({
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {}),
                })

            # 构建文本摘要
            lines: list[str] = []
            for srv_name, srv_tools in servers.items():
                lines.append(f"  [{srv_name}]")
                for st in srv_tools:
                    lines.append(f"    - {st['name']}: {st['description'][:80]}")
            text = "\n".join(lines)

            server_list = [
                {"name": srv, "tools": srv_tools}
                for srv, srv_tools in servers.items()
            ]

            logger.info(
                "list_mcp_tools: %d 个服务器, %d 个工具",
                len(server_list), len(tools),
            )
            return {
                "success": True,
                "servers": server_list,
                "text": text,
                "count": len(tools),
            }
        except Exception as exc:
            logger.exception("list_mcp_tools 调用异常")
            return {"success": False, "error": str(exc)}

    return handler


def build_call_mcp_tool_handler(
    get_mcp: Callable[[], Any],
) -> Callable[..., Awaitable[dict]]:
    """返回 ``call_mcp_tool`` 的 handler 逻辑体。

    Args:
        get_mcp: 返回当前 MCPManager 实例的可调用对象（支持热更新后刷新引用）。
    """

    async def handler(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：调用指定 MCP 服务器的特定工具。"""
        try:
            server = str(kwargs.get("server", "")).strip()
            tool = str(kwargs.get("tool", "")).strip()
            args_raw = kwargs.get("arguments", "{}")

            if not server:
                return {"success": False, "error": "缺少必填参数: server"}
            if not tool:
                return {"success": False, "error": "缺少必填参数: tool"}

            # 解析参数（兼容 JSON 字符串和 dict 两种输入）
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw) if args_raw.strip() else {}
                except json.JSONDecodeError as exc:
                    return {
                        "success": False,
                        "error": f"arguments 不是有效的 JSON: {exc}",
                    }
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}

            mcp = get_mcp()
            if mcp is None:
                return {
                    "success": False,
                    "error": "MCP 未启用或尚未初始化",
                }

            logger.debug(
                "call_mcp_tool: server=%s, tool=%s, args=%s",
                server, tool, json.dumps(args, ensure_ascii=False)[:200],
            )

            result = await mcp.call_tool(server, tool, args)

            # 结果格式化
            content = result.get("content", [])
            if isinstance(content, list):
                text_parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text_parts.append(str(item.get("text", "")))
                    elif isinstance(item, str):
                        text_parts.append(item)
                text = "\n".join(text_parts).strip()
            else:
                text = str(content) if content else ""

            logger.info(
                "call_mcp_tool 完成: server=%s, tool=%s, success=%s",
                server, tool, result.get("success", False),
            )

            return {
                "success": result.get("success", False),
                "error": result.get("error", ""),
                "content": text,
                "raw": content,
            }
        except Exception as exc:
            logger.exception("call_mcp_tool 调用异常")
            return {"success": False, "error": str(exc)}

    return handler