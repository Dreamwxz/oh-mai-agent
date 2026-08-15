"""MCPManager — 编排多个 MCP 服务器连接。

发现所有已配置服务器的工具，并构建可注册到 Agent 工具注册中心
的 ``ToolDefinition`` 对象。

范围（有意限制）：
    - 仅静态配置；不支持运行时动态增删服务器。
    - 仅工具（不支持 resources、prompts、sampling、roots）。
    - ``build_tool_definitions()`` 返回列表；由调用方负责通过
      ``ToolRegistry`` 注册它们。这样保持了 manager 与注册中心的解耦。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import Any

from ...config import MCPConfig, MCPServerConfig
from ...permission import Role
from ..registry import ToolDefinition, ToolRegistry

from .connection import MCPConnection
from .presets import resolve_effective_servers

logger = logging.getLogger(__name__)

#: MCPManager.start 的整体启动超时上限（秒）。硬编码常量，不暴露到配置：
#: 任何环境都不应因某个 MCP 服务器无响应而拖垮插件加载（supervisor 就绪窗口
#: 为 30s，整体上限须低于该窗口并留余量）。
_STARTUP_TIMEOUT_S: float = 25.0

#: 单个 MCP 服务器的启动预算（秒）：connect + initialize + list_tools 受其约束，
#: 坏服务器只浪费此预算而非整个整体启动上限。远程 HTTP 服务器（如 exa）在
#: 网络波动时 tools/list 响应可能达数秒，预算不宜过紧。硬编码常量，不暴露到配置。
_PER_SERVER_STARTUP_TIMEOUT_S: float = 15.0


def _stdio_module_available(server_cfg: MCPServerConfig) -> bool:
    """预检 stdio 服务器的 ``-m <module>`` 模块是否可在当前解释器导入。

    预检用当前解释器的 ``importlib.util.find_spec`` 判定，仅当
    ``command == sys.executable``（内置 fetch 预设即此情形）时精确；
    自定义其他解释器（如 npx 拉起的 node 服务器）的服务器不保证精确，
    属已知局限。非 ``-m`` 形态的命令不做预检，直接放行。
    """
    args = list(server_cfg.args or [])
    if len(args) < 2 or args[0] != "-m":
        return True
    module = args[1]
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        logger.warning(
            "MCP 服务器 '%s'：Python 模块 '%s' 未安装（%s -m %s），已跳过该服务器；请安装对应 Python 模块或关闭/移除该服务器配置",
            server_cfg.name,
            module,
            server_cfg.command,
            module,
        )
        return False
    return True


def unregister_stale_mcp_tools(registry: ToolRegistry, new_tool_names: set[str]) -> None:
    """注销不再存在的 mcp_* 工具（热更新启用/停用服务器后清理注册表残留）。"""
    for name in registry.all_names():
        if name.startswith("mcp_") and name not in new_tool_names:
            registry.unregister(name)


class MCPManager:
    """管理多个 MCP 服务器连接并暴露工具。

    用法示例::

        mgr = MCPManager(config.mcp)
        await mgr.start()
        tools = mgr.get_all_tools()
        for td in mgr.build_tool_definitions():
            tool_registry.register(td)
        result = await mgr.call_tool("myserver", "echo", {"text": "hi"})
        await mgr.stop()

    Parameters:
        config: 来自 ``config.MCPConfig`` 的 MCP 配置。
        timeout_ms: 每次请求超时时间（毫秒），默认 30000。
        startup_timeout_s: ``start()`` 整体启动超时上限（秒），默认
            ``_STARTUP_TIMEOUT_S``（25 秒）；超时关闭未完成握手的连接并跳过剩余服务器。
        per_server_timeout_s: 单个服务器的启动预算（秒），默认
            ``_PER_SERVER_STARTUP_TIMEOUT_S``（15 秒）；connect + initialize +
            list_tools 受其约束，超时的服务器被跳过。
    """

    def __init__(
        self,
        config: MCPConfig,
        *,
        timeout_ms: int = 30000,
        startup_timeout_s: float = _STARTUP_TIMEOUT_S,
        per_server_timeout_s: float = _PER_SERVER_STARTUP_TIMEOUT_S,
    ) -> None:
        self._config = config
        self._timeout_ms = timeout_ms
        self._startup_timeout_s = startup_timeout_s
        self._per_server_timeout_s = per_server_timeout_s
        self._connections: dict[str, MCPConnection] = {}
        self._tools: list[dict[str, Any]] = []

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """连接所有启用的服务器，完成 initialize 握手，并收集工具列表。

        整体启动受 ``startup_timeout_s`` 上限约束（默认 25 秒）：超时时
        关闭未完成握手的连接并保留已初始化的连接，不向上抛异常。连接或
        初始化失败的服务器会被关闭并跳过（失败日志由 ``MCPConnection`` 内部
        记录），manager 不会因此崩溃；工具列表获取失败时按空列表处理，
        该连接仍会保留。

        MCP 未启用（``enabled=False``）时直接返回，不连接任何服务器。
        """
        if not self._config.enabled:
            logger.info("MCP 未启用，跳过服务器连接")
            return

        try:
            await asyncio.wait_for(self._start_all(), timeout=self._startup_timeout_s)
        except asyncio.TimeoutError:
            logger.error(
                "MCP 启动超时（%.1fs），已关闭未完成握手的连接并保留已初始化的连接",
                self._startup_timeout_s,
            )
            for name, conn in list(self._connections.items()):
                if not conn.is_initialized:
                    await conn.close()
                    self._connections.pop(name, None)

    async def _start_all(self) -> None:
        """按配置顺序连接所有服务器并聚合工具（受 ``start()`` 整体超时约束）。

        每个服务器的启动（``_start_server``）另有 ``per_server_timeout_s``
        独立预算：超时的服务器被关闭并从 ``_connections`` 移除，继续下一个。
        """
        for server_cfg in resolve_effective_servers(self._config):
            if not server_cfg.name:
                logger.warning("MCP 服务器配置缺少名称，已跳过")
                continue

            if server_cfg.transport == "stdio" and not _stdio_module_available(server_cfg):
                continue

            conn = MCPConnection(server_cfg, timeout_ms=self._timeout_ms)
            # 在 connect/initialize 之前注册，超时取消时由 start() 的超时分支
            # 经 close 清理，避免取消态清理竞态
            self._connections[server_cfg.name] = conn
            try:
                ready = await asyncio.wait_for(
                    self._start_server(conn, server_cfg), timeout=self._per_server_timeout_s
                )
            except asyncio.TimeoutError:
                logger.error(
                    "MCP 服务器 '%s' 启动超时（%.1fs），已跳过",
                    server_cfg.name, self._per_server_timeout_s,
                )
                await conn.close()
                self._connections.pop(server_cfg.name, None)
                continue
            if not ready:
                await conn.close()
                self._connections.pop(server_cfg.name, None)
                continue

    async def _start_server(self, conn: MCPConnection, server_cfg: MCPServerConfig) -> bool:
        """启动单个 MCP 服务器：connect + initialize + list_tools 并聚合工具。

        Returns:
            启动成功（工具列表可空）返回 ``True``；连接或初始化失败返回
            ``False``（由调用方负责关闭并移除连接）。
        """
        if not await conn.connect():
            logger.warning("MCP 服务器 '%s' 连接失败，已跳过", server_cfg.name)
            return False
        if not await conn.initialize():
            logger.warning("MCP 服务器 '%s' 初始化失败，已跳过", server_cfg.name)
            return False

        tools = await conn.list_tools()
        # list_tools 失败时按契约返回 []；此处判空为防御性兜底
        if tools is None:
            tools = []
        logger.debug(
            "MCP 服务器 '%s' 获取到 %d 个工具，开始聚合工具列表",
            server_cfg.name,
            len(tools),
        )

        logger.info("MCP 服务器 '%s' 已注册连接", server_cfg.name)
        for tool in tools:
            self._tools.append(
                {
                    "server": server_cfg.name,
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    # 服务器未提供 inputSchema 时回退为空对象 schema
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                }
            )

        logger.info(
            "MCP 服务器 '%s' 初始化完成，发现 %d 个工具",
            server_cfg.name,
            len(tools),
        )
        return True

    async def stop(self) -> None:
        """关闭所有连接并清空已发现的工具列表。幂等操作，可安全地多次调用。"""
        names = list(self._connections.keys())
        for name in names:
            conn = self._connections.pop(name, None)
            if conn is not None:
                try:
                    await conn.close()
                except Exception as exc:
                    logger.warning("MCP 服务器 '%s' 关闭失败: %s", name, exc)
        self._tools.clear()
        if names:
            logger.info("MCP 已关闭 %d 个服务器连接并清空工具列表", len(names))

    # ── 工具访问 ──────────────────────────────────────────────────────────

    def get_all_tools(self) -> list[dict[str, Any]]:
        """以扁平 dict 形式返回所有已发现的工具。

        每条 dict 包含：
            ``server`` — 服务器名称
            ``name`` — 工具名（由服务器报告）
            ``description`` — 人类可读描述
            ``inputSchema`` — 工具参数的 JSON Schema

        Returns:
            工具信息 dict 列表（可能为空）。
        """
        return list(self._tools)

    def connection_count(self) -> int:
        """当前已建立连接的服务器数量（供外层日志/统计使用，不暴露内部映射）。"""
        return len(self._connections)

    async def call_tool(self, server: str, name: str, arguments: dict[str, Any]) -> dict:
        """将工具调用路由到指定服务器。

        Args:
            server: MCP 服务器名称（必须与已配置的服务器匹配）。
            name: 工具名称（由 ``get_all_tools()`` 返回）。
            arguments: 工具参数字典。

        Returns:
            遵守约定的 dict：
            成功时 ``{"success": True, "content": [...]}``，
            失败时 ``{"success": False, "error": "..."}``。
        """
        logger.debug("调用 MCP 工具: server='%s', tool='%s'", server, name)
        conn = self._connections.get(server)
        if conn is None:
            return {"success": False, "error": f"MCP server not connected: {server}"}

        result = await conn.call_tool(name, arguments)
        # 如果连接层已经返回了错误 dict，直接透传。
        if isinstance(result, dict) and result.get("success") is False:
            return result
        return {"success": True, "content": result}

    # ── ToolDefinition 构建 ────────────────────────────────────────────────

    def build_tool_definitions(self) -> list[ToolDefinition]:
        """为所有已发现的 MCP 工具构建 ``ToolDefinition`` 对象。

        工具名以 ``mcp_{server}_{tool}`` 为前缀，避免与内置工具冲突。
        调用方应通过 ``ToolRegistry`` 注册它们。

        Returns:
            可直接注册的 ``ToolDefinition`` 列表。
        """
        definitions: list[ToolDefinition] = []
        for tool in self._tools:
            server = tool["server"]
            tool_name = tool["name"]
            prefixed = f"mcp_{server}_{tool_name}"

            # 闭包中按值捕获，避免延迟绑定问题
            def _make_handler(srv: str, tn: str) -> Any:
                async def handler(**kwargs: object) -> dict:
                    # 解包元参数 "arguments"（由 ToolRegistry.execute 传入时使用），
                    # 否则将所有 kwargs 直接作为工具参数传递。
                    if "arguments" in kwargs and isinstance(kwargs["arguments"], dict):
                        args: dict[str, Any] = kwargs["arguments"]  # type: ignore[assignment]
                    else:
                        args = {k: v for k, v in kwargs.items()}
                    return await self.call_tool(srv, tn, args)

                handler.__name__ = f"mcp_handler_{srv}_{tn}"
                return handler

            definitions.append(
                ToolDefinition(
                    name=prefixed,
                    description=tool["description"],
                    parameters=tool["inputSchema"],
                    handler=_make_handler(server, tool_name),
                    visibility="discoverable",
                    min_role=Role.USER,
                )
            )

        return definitions
