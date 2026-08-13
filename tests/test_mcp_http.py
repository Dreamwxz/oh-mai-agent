"""oh-mai-agent tools/mcp 中 MCP HTTP/SSE 传输的测试。

覆盖范围：
    - _HttpTransport 的 JSON 响应（Content-Length）
    - _HttpTransport 的 SSE 响应（Content-Length + chunked）
    - 经 SSE 的完整 MCPConnection 流程（initialize → tools/list → tools/call）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Callable

import pytest

# 与 conftest.py 相同的导入路径注入：将插件根目录挂载为 oh_mai_agent 包
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT))
import types

_pkg = types.ModuleType("oh_mai_agent")
_pkg.__path__ = [str(_PLUGIN_ROOT)]
sys.modules["oh_mai_agent"] = _pkg

from oh_mai_agent.config import MCPServerConfig
from oh_mai_agent.tools.mcp.connection import MCPConnection, _HttpTransport


# ═══════════════════════════════════════════════════════════════════════════════
# Mock HTTP/SSE 服务器辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

_HTTP_REQUEST: tuple[str, ...] = ("method", "path", "headers", "body")


async def _parse_http_request(reader: asyncio.StreamReader, timeout: float = 5.0) -> dict:
    """从 *reader* 读取并解析一个 HTTP/1.1 请求。

    返回包含以下键的字典：``method``、``path``、``headers``（dict）、
    ``body``（str）。
    """
    # 读取请求行和请求头，直到 \r\n\r\n
    header_buf = bytearray()
    while True:
        chunk = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        header_buf.extend(chunk)
        if header_buf.endswith(b"\r\n\r\n"):
            break

    header_text = header_buf.decode("utf-8", errors="replace")
    raw_lines = header_text.split("\r\n")

    # 解析请求行
    method, path, _version = raw_lines[0].split(" ", 2)

    # 解析请求头
    headers: dict[str, str] = {}
    for line in raw_lines[1:]:
        if not line.strip():
            break
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip().lower()] = val.strip()

    # 若存在 Content-Length，则读取请求体
    content_length = int(headers.get("content-length", "0"))
    body_bytes = b""
    if content_length > 0:
        # 头部数据块可能已包含部分请求体
        header_end_idx = header_buf.find(b"\r\n\r\n") + 4
        leftover = header_buf[header_end_idx:]
        body_bytes = leftover
        remaining = content_length - len(leftover)
        while remaining > 0:
            more = await asyncio.wait_for(reader.read(min(remaining, 8192)), timeout=timeout)
            if not more:
                break
            body_bytes += more
            remaining -= len(more)

    return {
        "method": method,
        "path": path,
        "headers": headers,
        "body": body_bytes.decode("utf-8", errors="replace"),
    }


def _build_http_response(
    status_code: int,
    headers: dict[str, str],
    body: str,
) -> bytes:
    """由各部分构建一个完整的 HTTP/1.1 响应。"""
    header_lines = [f"HTTP/1.1 {status_code} OK"]
    for k, v in headers.items():
        header_lines.append(f"{k}: {v}")
    header_lines.append("Connection: close")
    header_blob = "\r\n".join(header_lines) + "\r\n\r\n"
    return header_blob.encode("utf-8") + body.encode("utf-8")


def _sse_wrap(payload: dict) -> str:
    """将 JSON 负载包装为单个 SSE 事件。"""
    json_str = json.dumps(payload)
    return f"data: {json_str}\n\n"


def _chunked_encode(data: bytes) -> bytes:
    """使用 HTTP chunked 传输编码对 *data* 进行编码。"""
    chunks: list[bytes] = []
    offset = 0
    while offset < len(data):
        chunk_data = data[offset : offset + 64]
        offset += len(chunk_data)
        size_hex = f"{len(chunk_data):X}\r\n".encode()
        chunks.append(size_hex + chunk_data + b"\r\n")
    chunks.append(b"0\r\n\r\n")
    return b"".join(chunks)


# ── 处理器工厂 ────────────────────────────────────────────────────────────────

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], object]


def _make_json_handler(response_body: dict) -> Handler:
    """返回一个以 ``application/json`` 响应的处理器。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _parse_http_request(reader)
        raw = _build_http_response(
            200,
            {"Content-Type": "application/json", "Content-Length": str(len(json.dumps(response_body).encode()))},
            json.dumps(response_body),
        )
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return handle


def _make_sse_handler(
    payload: dict,
    *,
    use_chunked: bool = False,
) -> Handler:
    """返回一个以 ``text/event-stream`` 响应的处理器。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _parse_http_request(reader)
        sse_body = _sse_wrap(payload)
        if use_chunked:
            body_bytes = _chunked_encode(sse_body.encode("utf-8"))
            headers = {
                "Content-Type": "text/event-stream",
                "Transfer-Encoding": "chunked",
            }
            raw = _build_http_response(200, headers, "")
            # 去掉头部结束空行 \r\n\r\n，改为直接拼接 chunked 编码的响应体
            raw = raw[: raw.rfind(b"\r\n") + 2] + body_bytes
        else:
            body_bytes = sse_body.encode("utf-8")
            headers = {
                "Content-Type": "text/event-stream",
                "Content-Length": str(len(body_bytes)),
            }
            raw = _build_http_response(200, headers, sse_body)
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return handle


def _make_mcp_handler() -> Handler:
    """返回一个模拟完整 MCP 服务器（SSE 响应）的处理器。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        req = await _parse_http_request(reader)
        body = req.get("body", "")
        try:
            rpc = json.loads(body)
        except json.JSONDecodeError:
            rpc = {}
        method_name = rpc.get("method", "")
        req_id = rpc.get("id", 0)

        if method_name == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-mock", "version": "1.0.0"},
            }
        elif method_name == "tools/list":
            result = {
                "tools": [
                    {"name": "echo", "description": "Echo back the input", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                    {"name": "add", "description": "Add two numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
                ]
            }
        elif method_name == "tools/call":
            params = rpc.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            if tool_name == "echo":
                result = {"content": [{"type": "text", "text": f"ECHO: {args.get('text', '')}"}]}
            elif tool_name == "add":
                result = {"content": [{"type": "text", "text": str(args.get("a", 0) + args.get("b", 0))}]}
            else:
                result = {"content": [{"type": "text", "text": f"unknown tool: {tool_name}"}]}
        else:
            result = {}

        response = {"jsonrpc": "2.0", "id": req_id, "result": result}
        sse_body = _sse_wrap(response)
        body_bytes = sse_body.encode("utf-8")
        raw = _build_http_response(
            200,
            {"Content-Type": "text/event-stream", "Content-Length": str(len(body_bytes))},
            sse_body,
        )
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return handle


def _make_mcp_handler_with_202_notifications() -> Handler:
    """模拟 MCP 服务器：对通知（无 ``id``）返回 202 空体，其余同 ``_make_mcp_handler``。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        req = await _parse_http_request(reader)
        body = req.get("body", "")
        try:
            rpc = json.loads(body)
        except json.JSONDecodeError:
            rpc = {}
        if "id" not in rpc:
            # 通知（如 notifications/initialized）：返回 202 空体，不构造响应
            raw = _build_http_response(202, {"Content-Length": "0"}, "")
            writer.write(raw)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        method_name = rpc.get("method", "")
        req_id = rpc.get("id", 0)

        if method_name == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-mock", "version": "1.0.0"},
            }
        elif method_name == "tools/list":
            result = {
                "tools": [
                    {"name": "echo", "description": "Echo back the input", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                    {"name": "add", "description": "Add two numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
                ]
            }
        else:
            result = {}

        response = {"jsonrpc": "2.0", "id": req_id, "result": result}
        sse_body = _sse_wrap(response)
        body_bytes = sse_body.encode("utf-8")
        raw = _build_http_response(
            200,
            {"Content-Type": "text/event-stream", "Content-Length": str(len(body_bytes))},
            sse_body,
        )
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return handle


# ── fixture：临时 TCP 服务器 + 构建 _HttpTransport 的辅助 ─────────────────────


async def _run_server(handler: Handler) -> tuple[asyncio.AbstractServer, int]:
    """在随机端口上以 *handler* 启动一个 TCP 服务器。"""
    server = await asyncio.start_server(handler, host="127.0.0.1", port=0)
    addr = server.sockets[0].getsockname()
    port: int = addr[1]
    return server, port


def _make_transport(port: int) -> _HttpTransport:
    """构建一个指向 localhost:*port* 的 _HttpTransport。"""
    return _HttpTransport(f"http://127.0.0.1:{port}/mcp", {}, 10.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 测试：_HttpTransport JSON
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_transport_json_response():
    """JSON（Content-Type + Content-Length）→ 返回正确的字典。"""
    expected = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    handler = _make_json_handler(expected)
    server, port = await _run_server(handler)
    try:
        transport = _make_transport(port)
        result = await transport.post(json.dumps({"method": "echo"}).encode())
        assert result == expected
    finally:
        server.close()
        await server.wait_closed()


# ═══════════════════════════════════════════════════════════════════════════════
# 测试：_HttpTransport SSE
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_transport_sse_content_length():
    """带 Content-Length 的 SSE 响应 → 提取 data: JSON。"""
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"greeting": "hello"}}
    handler = _make_sse_handler(payload, use_chunked=False)
    server, port = await _run_server(handler)
    try:
        transport = _make_transport(port)
        result = await transport.post(json.dumps({"method": "echo"}).encode())
        assert result == payload
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_transport_sse_chunked():
    """带 Transfer-Encoding: chunked 的 SSE 响应 → 提取 data: JSON。"""
    payload = {"jsonrpc": "2.0", "id": 2, "result": {"value": 42}}
    handler = _make_sse_handler(payload, use_chunked=True)
    server, port = await _run_server(handler)
    try:
        transport = _make_transport(port)
        result = await transport.post(json.dumps({"method": "calc"}).encode())
        assert result == payload
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_transport_sse_multi_data_returns_last():
    """含多个 data: 事件的 SSE → 返回最后一个 JSON 负载。"""
    payload = {"jsonrpc": "2.0", "id": 3, "result": {"final": True}}
    sse_body = (
        'event: progress\ndata: {"progress": 0.5}\n\n'
        'data: {"jsonrpc": "2.0", "id": 3, "result": {"final": true}}\n\n'
    )

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _parse_http_request(reader)
        body_bytes = sse_body.encode("utf-8")
        raw = _build_http_response(
            200,
            {"Content-Type": "text/event-stream", "Content-Length": str(len(body_bytes))},
            sse_body,
        )
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _run_server(handler)
    try:
        transport = _make_transport(port)
        result = await transport.post(b"{}")
        assert result == payload
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_transport_sse_no_data_returns_empty():
    """仅含注释/事件行（无 data:）的 SSE → 返回 {}。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _parse_http_request(reader)
        sse_body = ": heartbeat\n\n"
        body_bytes = sse_body.encode("utf-8")
        raw = _build_http_response(
            200,
            {"Content-Type": "text/event-stream", "Content-Length": str(len(body_bytes))},
            sse_body,
        )
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _run_server(handler)
    try:
        transport = _make_transport(port)
        result = await transport.post(b"{}")
        assert result == {}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_transport_empty_body_returns_empty():
    """通知（parse=False）遇到 202 空响应体 → 返回 {} 不抛。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _parse_http_request(reader)
        raw = _build_http_response(202, {"Content-Length": "0"}, "")
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _run_server(handler)
    try:
        transport = _make_transport(port)
        result = await transport.post(
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}', parse=False
        )
        assert result == {}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_transport_empty_body_parse_true_raises():
    """请求（parse=True）遇到 202 空响应体 → 抛 json.JSONDecodeError，不掩盖服务器错误。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _parse_http_request(reader)
        raw = _build_http_response(202, {"Content-Length": "0"}, "")
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _run_server(handler)
    try:
        transport = _make_transport(port)
        with pytest.raises(json.JSONDecodeError):
            await transport.post(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}')
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_transport_notification_parse_false():
    """parse=False 的通知 POST：空响应体 → 返回 {} 且不解析。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _parse_http_request(reader)
        raw = _build_http_response(202, {"Content-Length": "0"}, "")
        writer.write(raw)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _run_server(handler)
    try:
        transport = _make_transport(port)
        result = await transport.post(
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}', parse=False
        )
        assert result == {}
    finally:
        server.close()
        await server.wait_closed()


# ═══════════════════════════════════════════════════════════════════════════════
# 测试：完整 MCPConnection SSE 流程
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_connection_full_sse_flow():
    """MCPConnection（transport=sse）执行 initialize → tools/list → tools/call。"""
    handler = _make_mcp_handler()
    server, port = await _run_server(handler)
    cfg = MCPServerConfig(
        name="test-mock",
        transport="sse",
        url=f"http://127.0.0.1:{port}/mcp",
    )
    conn = MCPConnection(cfg, timeout_ms=10000)
    try:
        ok = await conn.connect()
        assert ok is True

        initialized = await conn.initialize()
        assert initialized is True

        tools = await conn.list_tools()
        assert len(tools) == 2
        tool_names = {t["name"] for t in tools}
        assert tool_names == {"echo", "add"}

        echo_result = await conn.call_tool("echo", {"text": "hello-world"})
        assert echo_result.get("content", [{}])[0].get("text") == "ECHO: hello-world"

        add_result = await conn.call_tool("add", {"a": 1, "b": 2})
        assert add_result.get("content", [{}])[0].get("text") == "3"
    finally:
        await conn.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_mcp_connection_initialize_tolerates_202_notification():
    """服务器对 initialized 通知返回 202 空体时，initialize 仍成功且 list_tools 正常。"""
    handler = _make_mcp_handler_with_202_notifications()
    server, port = await _run_server(handler)
    cfg = MCPServerConfig(
        name="test-mock",
        transport="http",
        url=f"http://127.0.0.1:{port}/mcp",
    )
    conn = MCPConnection(cfg, timeout_ms=10000)
    try:
        ok = await conn.connect()
        assert ok is True

        initialized = await conn.initialize()
        assert initialized is True

        tools = await conn.list_tools()
        assert len(tools) == 2
        tool_names = {t["name"] for t in tools}
        assert tool_names == {"echo", "add"}
    finally:
        await conn.close()
        server.close()
        await server.wait_closed()
