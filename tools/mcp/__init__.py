"""oh-mai-agent MCP 客户端 — 基于 Python 标准库的精简 MCP 实现。

范围：
    - 仅支持工具调用（tools/list、tools/call）。
    - 支持 stdio / http / sse 三种传输方式。
    - 不支持 resources、prompts、sampling、roots（v0.2）。

导出：
    MCPManager — 管理多个服务器连接，并为 agent 工具注册表
    构建 ``ToolDefinition`` 对象。
"""

from .provider import MCPManager

__all__ = ["MCPManager"]
