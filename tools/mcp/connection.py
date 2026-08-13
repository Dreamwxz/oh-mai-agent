"""MCPConnection — 单个 MCP 服务器连接管理器。

实现一个精简的 MCP 客户端，仅使用 Python 标准库
（asyncio / json / subprocess / urllib.parse / ssl）。

范围（有意限制）：
    - 仅工具：支持 tools/list 和 tools/call。
      不支持 resources、prompts、sampling、roots（超出 v0.2 范围）。
    - HTTP/SSE：基本的请求-响应 POST JSON-RPC，发送到配置的 URL。
      同时支持 JSON（``Content-Type: application/json``）和 SSE
      （``Content-Type: text/event-stream``）两种响应格式，遵循 MCP
      Streamable HTTP 规范。
      不支持 SSE 流式推送（server→client push notifications）；
      仅支持简单的同步请求-响应轮次。
      ``transport="http"`` 和 ``transport="sse"`` 使用同一套基于 POST 的代码路径。
    - 处理 ``Transfer-Encoding: chunked`` 和传统的基于
      ``Content-Length`` 的响应体读取。
    - tools/list 不支持分页游标（仅返回第一页）。
    - JSON-RPC 2.0，请求 ID 单调递增。
    - stdio 传输采用 LSP 风格的帧格式（Content-Length 头）。

参考：MaiBot ``src/mcp_module/connection.py``（仅协议细节参考；
本实现完全独立）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from typing import Any
from urllib.parse import urlparse

from ...config import MCPServerConfig

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_NAME = "oh-mai-agent"
_CLIENT_VERSION = "0.2.0"
_READ_CHUNK = 8192


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


def _build_environ(extra: dict[str, str]) -> dict[str, str]:
    """将 *extra* 环境变量合并到当前环境的副本中。"""
    env = os.environ.copy()
    env.update(extra)
    return env


def _parse_content_length_header(line: bytes) -> int:
    """从类似 ``b'Content-Length: 42'`` 的头部行中提取 Content-Length 值。"""
    _, _, value = line.partition(b":")
    return int(value.strip())


def _truncate(value: object, limit: int = 200) -> str:
    """将 *value* JSON 序列化并截断为不超过 *limit* 字符，用于日志脱敏。

    请求参数与响应内容可能含敏感信息，日志中仅保留前缀并标注截断。
    """
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _http_endpoint(url: str) -> str:
    """从 *url* 提取脱敏的日志端点：仅协议与主机名（含非默认端口），
    不记录 query 参数，避免泄露其中可能包含的密钥。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        # 非 HTTP(S) URL（或解析失败）：退化为去掉 query 的完整 URL
        return url.split("?", 1)[0]
    host = parsed.hostname
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port and parsed.port != default_port:
        return f"{parsed.scheme}://{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}"


class _HttpTransport:
    """精简的异步 HTTP POST 传输，用于 JSON-RPC 请求。

    每次请求打开一个新的 TCP（或 TLS）连接，发送手工构造的 HTTP/1.1 POST 请求，
    然后读取响应体。

    同时支持 JSON（``application/json``）和 SSE（``text/event-stream``）
    两种响应体格式。对 SSE 响应，提取最后一条 ``data:`` payload 行
    （即 JSON-RPC 结果），忽略注释和 ``event:`` 行。

    也处理 ``Transfer-Encoding: chunked``（检测到时
    优先于 ``Content-Length`` 精确读取）。

    这是有意追求精简的实现 — 无连接池、无 keep-alive、
    无 SSE 流式推送（server→client push）。对低频 MCP 工具调用已足够。
    """

    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
        self._url = url
        self._headers = headers
        self._timeout = timeout
        parsed = urlparse(url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._path = parsed.path or "/"
        if parsed.query:
            self._path += "?" + parsed.query
        self._use_tls = parsed.scheme == "https"

    async def post(self, body: bytes, *, parse: bool = True) -> dict:
        """POST *body*（JSON 编码的字节串）并返回解析后的 JSON 响应。

        *parse* 为 ``False`` 时跳过响应体解析（用于通知等发后即忘场景），
        但仍会读完响应体，且无论响应体内容如何都返回 ``{}``；*parse* 为
        ``True`` 时对空/纯空白响应体保持严格解析（抛出
        ``json.JSONDecodeError``，由调用方处理）。

        根据 ``Content-Type`` 解析响应：
        * ``application/json`` → 直接解析为 JSON 对象。
        * ``text/event-stream``（或 ``application/stream+json``）→ SSE 体；
          提取最后一条 ``data:`` payload。

        响应体读取优先级：先按 ``Transfer-Encoding: chunked`` 分块读取，
        否则按 ``Content-Length`` 精确读取；两者皆无时读到 EOF。
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port, ssl=self._ssl_context()),
            timeout=self._timeout,
        )
        try:
            request = self._build_request(body)
            writer.write(request)
            await asyncio.wait_for(writer.drain(), timeout=self._timeout)

            # ── 读取响应头 ─────────────────────────────────────────────────
            header_data = bytearray()
            while True:
                chunk = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=self._timeout
                )
                header_data.extend(chunk)
                if header_data.endswith(b"\r\n\r\n"):
                    break

            headers_text = header_data.decode("utf-8", errors="replace")
            content_length = 0
            is_chunked = False
            content_type = ""
            for line in headers_text.split("\r\n"):
                lower = line.lower()
                if lower.startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
                elif lower.startswith("content-type:"):
                    content_type = line.split(":", 1)[1].strip().split(";")[0].strip()
                elif lower.startswith("transfer-encoding:"):
                    if "chunked" in lower:
                        is_chunked = True

            # ── 读取响应体 ─────────────────────────────────────────────────
            body_bytes: bytearray
            if is_chunked:
                body_bytes = await self._read_chunked_body(reader)
            elif content_length > 0:
                body_bytes = await self._read_fixed_body(reader, content_length)
            else:
                # 读取直到连接关闭（EOF）
                body_bytes = bytearray()
                while True:
                    chunk = await asyncio.wait_for(
                        reader.read(_READ_CHUNK), timeout=self._timeout
                    )
                    if not chunk:
                        break
                    body_bytes.extend(chunk)

            body_text = body_bytes.decode("utf-8", errors="replace")

            # ── 根据 Content-Type 解析 ──────────────────────────────────────
            if not parse:
                return {}
            if "text/event-stream" in content_type or "application/stream+json" in content_type:
                return self._parse_sse_response(body_text)
            return json.loads(body_text)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except Exception:
                pass

    async def _read_fixed_body(self, reader: asyncio.StreamReader, content_length: int) -> bytearray:
        """从 *reader* 精确读取 *content_length* 字节。"""
        body_bytes = bytearray()
        remaining = content_length
        while remaining > 0:
            chunk = await asyncio.wait_for(
                reader.read(min(remaining, _READ_CHUNK)), timeout=self._timeout
            )
            if not chunk:
                break
            body_bytes.extend(chunk)
            remaining -= len(chunk)
        return body_bytes

    async def _read_chunked_body(self, reader: asyncio.StreamReader) -> bytearray:
        """读取 HTTP chunked transfer-encoding 响应体。"""
        body_bytes = bytearray()
        while True:
            # 读取 chunk size 行（十六进制 + 可选的 ;ext → \r\n）
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self._timeout)
            # 解析十六进制大小（行尾的 \r\n 已由 rstrip 去除）
            hex_str = line.rstrip(b"\r\n").split(b";")[0].strip()
            chunk_size = int(hex_str, 16)
            if chunk_size == 0:
                # 读取零长度 chunk 后的尾 CRLF
                # （以及可选的 trailer — 跳过它们）
                while True:
                    trailer = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self._timeout)
                    if trailer in (b"\r\n", b""):
                        break
                break
            # 读取 chunk 数据
            data = await asyncio.wait_for(
                reader.readexactly(chunk_size), timeout=self._timeout
            )
            body_bytes.extend(data)
            # 读取 chunk 数据后的尾 CRLF
            await asyncio.wait_for(reader.readexactly(2), timeout=self._timeout)
        return body_bytes

    def _parse_sse_response(self, body_text: str) -> dict:
        """解析 SSE（text/event-stream）响应体。

        从每个 SSE 事件（由双换行分隔）中提取 ``data:`` payload 行，
        忽略注释（``:``）和 ``event:`` 行。
        返回**最后一条** ``data:`` JSON payload（即当前请求的 JSON-RPC 响应）。
        若未找到 ``data:`` 行则返回 ``{}``。
        """
        last_payload: dict = {}
        for event_block in body_text.split("\n\n"):
            text = event_block.strip()
            if not text:
                continue
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("data:"):
                    data_value = line[5:].strip()
                    if data_value:
                        try:
                            last_payload = json.loads(data_value)
                        except json.JSONDecodeError:
                            continue
        return last_payload

    def _build_request(self, body: bytes) -> bytes:
        """构造 HTTP/1.1 POST 请求。"""
        lines = [
            f"POST {self._path} HTTP/1.1".encode(),
            f"Host: {self._host}:{self._port}".encode(),
            b"Content-Type: application/json",
            b"Accept: application/json, text/event-stream",
            f"Content-Length: {len(body)}".encode(),
        ]
        for k, v in self._headers.items():
            lines.append(f"{k}: {v}".encode())
        lines.append(b"Connection: close")
        # 两个空串经 \r\n 连接后构成头部结束空行（\r\n\r\n）
        lines.append(b"")
        lines.append(b"")
        return b"\r\n".join(lines) + body

    def _ssl_context(self) -> ssl.SSLContext | None:
        """为 TLS 连接创建 SSL 上下文；非 TLS 时返回 None。"""
        if not self._use_tls:
            return None
        ctx = ssl.create_default_context()
        return ctx


# ── MCPConnection：MCP 连接管理器 ────────────────────────────────────────────


class MCPConnection:
    """管理单个 MCP 服务器连接。

    支持 ``stdio``（完整实现，含 LSP 风格帧格式）、
    ``http`` 和 ``sse``（仅基本的 POST JSON-RPC；详见模块文档字符串）。

    用法示例::

        conn = MCPConnection(server_config)
        await conn.connect()
        await conn.initialize()
        tools = await conn.list_tools()
        result = await conn.call_tool("echo", {"text": "hello"})
        await conn.close()

    Parameters:
        server_config: 来自 ``config.MCPServerConfig`` 的服务器配置。
        timeout_ms: 每次请求超时时间（毫秒），默认 30000。
    """

    def __init__(self, server_config: MCPServerConfig, *, timeout_ms: int = 30000) -> None:
        self._config = server_config
        self._timeout = timeout_ms / 1000.0

        # -- stdio 状态 -------------------------------------------------------
        self._process: asyncio.subprocess.Process | None = None

        # -- http 状态 --------------------------------------------------------
        self._http: _HttpTransport | None = None

        # -- 共享状态 ---------------------------------------------------------
        self._request_id: int = 0
        # 请求锁：串行化 JSON-RPC 请求，防止 stdio 帧交错
        self._lock = asyncio.Lock()
        self._initialized = False

    # ── 公开 API ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """来自配置的服务器名称。"""
        return self._config.name

    @property
    def is_initialized(self) -> bool:
        """是否已完成 initialize 握手。"""
        return self._initialized

    def _safe_endpoint(self) -> str:
        """返回脱敏的连接端点（仅协议与主机名，用于 INFO 日志）。

        stdio 记录命令名；http/sse 复用 ``_http_endpoint`` 屏蔽 query 参数。
        """
        if self._config.transport == "stdio":
            return self._config.command or "stdio"
        return _http_endpoint(self._config.url or "")

    async def connect(self) -> bool:
        """建立传输连接（握手由独立的 ``initialize()`` 完成）。

        Returns:
            成功返回 ``True``，失败返回 ``False``。
        """
        transport = self._config.transport
        logger.debug(
            "MCP 服务器 '%s'：开始连接（transport=%s, endpoint=%s）",
            self._config.name,
            transport,
            self._safe_endpoint(),
        )
        try:
            if transport == "stdio":
                await self._connect_stdio()
            elif transport in ("http", "sse"):
                # http 与 sse 共用同一套基于 POST 的传输实现
                self._connect_http()
            else:
                logger.error("MCP 服务器 '%s'：未知传输类型 '%s'", self._config.name, transport)
                return False
            logger.info(
                "MCP 服务器 '%s' 连接成功（transport=%s, endpoint=%s）",
                self._config.name,
                transport,
                self._safe_endpoint(),
            )
            return True
        except Exception as exc:
            logger.error("MCP 服务器 '%s' 连接失败：%s", self._config.name, exc)
            await self.close()
            return False

    async def initialize(self) -> bool:
        """执行 MCP ``initialize`` 握手 + ``notifications/initialized``。

        必须在 ``connect()`` 之后调用。

        Returns:
            成功返回 ``True``，失败返回 ``False``。
        """
        try:
            result = await self._send_request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
                },
            )
            if "error" in result:
                logger.error(
                    "MCP 服务器 '%s'：initialize 握手失败：%s",
                    self._config.name,
                    result["error"],
                )
                return False
            # MCP 规范：initialize 握手成功后须发送 initialized 通知；
            # 通知失败不阻断握手（HTTP 服务器可能返回 202 空体或直接忽略）
            try:
                await self._send_notification("notifications/initialized")
            except Exception as exc:
                logger.warning(
                    "MCP 服务器 '%s'：发送 initialized 通知失败（握手已完成，不影响连接）：%s",
                    self._config.name,
                    exc,
                )
            self._initialized = True
            return True
        except Exception as exc:
            logger.error("MCP 服务器 '%s'：initialize 握手异常：%s", self._config.name, exc)
            return False

    async def list_tools(self) -> list[dict]:
        """获取此 MCP 服务器暴露的工具列表。

        Returns:
            工具 dict 列表，每条包含 ``name``、``description`` 和 ``inputSchema``
            键。失败时返回 ``[]``。
        """
        try:
            result = await self._send_request("tools/list")
            if "result" in result:
                return list(result["result"].get("tools", []))
            logger.error("MCP 服务器 '%s'：tools/list 返回错误：%s", self._config.name, result.get("error"))
            return []
        except Exception as exc:
            logger.error("MCP 服务器 '%s'：tools/list 调用失败：%s", self._config.name, exc)
            return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        """调用 MCP 服务器上的一个工具。

        Args:
            name: ``list_tools()`` 返回的工具名称。
            arguments: 工具的关键字参数。

        Returns:
            原始 JSON-RPC 结果 dict，或在失败时返回
            ``{"success": False, "error": "..."}``。
        """
        try:
            result = await self._send_request("tools/call", {"name": name, "arguments": arguments})
            if "result" in result:
                return result["result"]
            error = result.get("error", {})
            error_msg = error.get("message", str(error))
            logger.error(
                "MCP 服务器 '%s'：调用工具 '%s' 返回错误响应：%s",
                self._config.name,
                name,
                error_msg,
            )
            return {"success": False, "error": error_msg}
        except Exception as exc:
            logger.error("MCP 服务器 '%s'：调用工具 '%s' 异常：%s", self._config.name, name, exc)
            return {"success": False, "error": str(exc)}

    async def close(self) -> None:
        """关闭传输并释放所有资源。幂等操作。"""
        self._initialized = False

        # 释放 HTTP 传输实例（stdio 模式下未创建，此步为空操作）
        self._http = None

        # 关闭 stdio 进程
        proc = self._process
        self._process = None
        if proc is not None:
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            except Exception as exc:
                logger.warning("MCP 服务器 '%s'：关闭 stdio 进程出错：%s", self._config.name, exc)
        logger.info("MCP 服务器 '%s' 已关闭", self._config.name)

    # ── 传输：stdio ────────────────────────────────────────────────────────

    async def _connect_stdio(self) -> None:
        """启动 MCP 服务器子进程并连接 stdin/stdout。"""
        cmd = self._config.command
        if not cmd:
            raise ValueError(f"MCP server '{self._config.name}': stdio transport requires 'command'")
        args = list(self._config.args)
        env = _build_environ(self._config.env)

        self._process = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        logger.debug("MCP 服务器 '%s'：stdio 子进程已启动（command=%s）", self._config.name, cmd)

    # ── 传输：http / sse ───────────────────────────────────────────────────

    def _connect_http(self) -> None:
        """初始化 HTTP/SSE 传输实例。"""
        url = self._config.url
        if not url:
            raise ValueError(f"MCP server '{self._config.name}': http/sse transport requires 'url'")
        self._http = _HttpTransport(
            url=url,
            headers=dict(self._config.headers),
            timeout=self._timeout,
        )
        logger.debug(
            "MCP 服务器 '%s'：HTTP 传输已初始化（endpoint=%s）",
            self._config.name,
            _http_endpoint(url),
        )

    # ── JSON-RPC 核心 ───────────────────────────────────────────────────────

    def _next_id(self) -> int:
        """生成下一个单调递增的请求 ID。"""
        self._request_id += 1
        return self._request_id

    async def _send_request(self, method: str, params: dict | None = None) -> dict:
        """发送 JSON-RPC 2.0 请求并返回解析后的响应。

        Args:
            method: JSON-RPC 方法名（例如 ``"tools/list"``）。
            params: 可选的参数字典。

        Returns:
            完整的 JSON-RPC 响应 dict（包含 ``"result"`` 或 ``"error"`` 之一）。
        """
        request_id = self._next_id()
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params

        logger.debug(
            "MCP 服务器 '%s'：发送请求 method=%s, id=%d, params=%s",
            self._config.name,
            method,
            request_id,
            _truncate(params),
        )

        async with self._lock:
            raw = await asyncio.wait_for(self._send_raw(payload, expect_response=True), timeout=self._timeout)
            logger.debug(
                "MCP 服务器 '%s'：收到响应 id=%d, method=%s：%s",
                self._config.name,
                request_id,
                method,
                _truncate(raw),
            )
            return raw

    async def _send_notification(self, method: str, params: dict | None = None) -> None:
        """发送 JSON-RPC 2.0 通知（无 ``id``，不期待响应）。

        通知是发后即忘的；写入 stdin 后不阻塞读取。
        """
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params

        async with self._lock:
            await self._send_raw(payload, expect_response=False)

    async def _send_raw(self, payload: dict[str, Any], *, expect_response: bool = True) -> dict | None:
        """编码 *payload*，通过当前传输层分发，可选解码响应。

        当 *expect_response* 为 ``False`` 时（用于通知），
        只写入 payload 而不从传输层读取响应。
        """
        if self._http is not None:
            if not expect_response:
                # HTTP 发后即忘：POST 并读取但不解析响应体（服务器可能返回 202 空体）
                await self._http.post(json.dumps(payload).encode("utf-8"), parse=False)
                return {}
            return await self._http.post(json.dumps(payload).encode("utf-8"))

        # stdio 路径
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("MCP connection not established")

        body = json.dumps(payload).encode("utf-8")
        # LSP 风格帧：Content-Length 头 + 空行 + JSON 体
        frame = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        proc.stdin.write(frame)
        try:
            await asyncio.wait_for(proc.stdin.drain(), timeout=self._timeout)
        except (BrokenPipeError, ConnectionResetError):
            raise RuntimeError("MCP server process stdin closed unexpectedly")

        if not expect_response:
            return {}

        return await self._read_stdio_response()

    async def _read_stdio_response(self) -> dict:
        """从 stdio 进程的 stdout 读取单条 JSON-RPC 消息。

        解析 LSP 风格的 ``Content-Length: N\r\n\r\n{json}`` 帧格式。
        跳过中间的无关消息（如未帧化的 stdout 噪音或通知响应），
        直到找到包含 ``id`` 字段的消息为止。
        """
        proc = self._process
        if proc is None or proc.stdout is None:
            raise RuntimeError("MCP connection not established")

        # 可能需要跳过服务器发出的通知响应（无 `id` 字段）。
        # 持续读取直到收到包含 `id` 的消息。
        while True:
            # 逐行读取头部直到遇到空行
            content_length = 0
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self._timeout)
                if not line:
                    raise RuntimeError("MCP server process stdout closed unexpectedly")
                stripped = line.rstrip(b"\r\n")
                if not stripped:
                    break  # 空行 = 头部结束
                if stripped.lower().startswith(b"content-length:"):
                    content_length = _parse_content_length_header(stripped)

            if content_length <= 0:
                logger.warning(
                    "MCP 服务器 '%s'：响应缺少 Content-Length 头，跳过该消息",
                    self._config.name,
                )
                continue

            body_bytes = await asyncio.wait_for(proc.stdout.readexactly(content_length), timeout=self._timeout)
            msg = json.loads(body_bytes.decode("utf-8"))

            # 如果消息没有 "id" 字段，则为通知（或针对我们发出的通知的响应）；
            # 跳过并继续读取下一条。
            if "id" in msg:
                return msg

            logger.debug("MCP 服务器 '%s'：跳过通知消息：%s", self._config.name, msg.get("method", "?"))
