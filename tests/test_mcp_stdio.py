"""oh-mai-agent tools/mcp 中 MCP stdio 传输的测试。

覆盖范围：
    - 换行分隔 JSON 帧格式的完整流程（initialize → tools/list → tools/call）
    - 服务器在 stdout 上打印非 JSON 噪音行时的容错
    - 服务器先发送无 id 的通知消息时的跳过逻辑
    - 服务器启动即退出（不响应）时的降级行为

测试用**真实的假 MCP 服务器子进程**（``sys.executable -c <inline script>``），
按 MCP 规范 stdio 传输逐行读取 stdin 并以 ``json + "\\n"`` 回应，
并用 stderr 记录客户端发送的原始行以断言帧格式。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# 与 conftest.py 相同的导入路径注入：将插件根目录挂载为 oh_mai_agent 包
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT))
import types

_pkg = types.ModuleType("oh_mai_agent")
_pkg.__path__ = [str(_PLUGIN_ROOT)]
sys.modules["oh_mai_agent"] = _pkg

from oh_mai_agent.config import MCPServerConfig
from oh_mai_agent.tools.mcp.connection import MCPConnection


# ═══════════════════════════════════════════════════════════════════════════════
# 假 MCP 服务器子进程（内联脚本，经 sys.executable -c 启动）
# ═══════════════════════════════════════════════════════════════════════════════

# 服务器主循环体：逐行读取 stdin，把收到的原始行回显到 stderr（供断言帧格式），
# 再对带 id 的请求以 ``json + "\n"`` 回应。初始化前可注入噪音/通知/立即退出行为。
_LOOP_BODY = """\
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            # 回显客户端发送的原始行，供测试端断言换行分隔 JSON 帧格式
            sys.stderr.write("RECV: " + line + "\\n")
            sys.stderr.flush()
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            method = msg.get("method", "")
            req_id = msg.get("id")
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo back the input",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                            },
                        }
                    ]
                }
            elif method == "tools/call":
                params = msg.get("params", {})
                args = params.get("arguments", {})
                text = args.get("text", "")
                result = {"content": [{"type": "text", "text": f"ECHO: {text}"}]}
            else:
                result = {}
            if req_id is not None:
                send({"jsonrpc": "2.0", "id": req_id, "result": result})
"""


def _server_script(
    *,
    noise: bool = False,
    notification: bool = False,
    exit_immediately: bool = False,
) -> str:
    """构建假 MCP 服务器内联脚本。

    Args:
        noise: 进入主循环前先向 stdout 打印一行非 JSON 噪音。
        notification: 进入主循环前先发送一条无 id 的通知消息。
        exit_immediately: 服务器启动后立即退出，不读取 stdin、不响应。
    """
    prelude: list[str] = []
    if noise:
        prelude.append('        sys.stdout.write("fake banner noise\\n")')
        prelude.append("        sys.stdout.flush()")
    if notification:
        prelude.append('        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})')
    if exit_immediately:
        prelude.append("        sys.exit(0)")

    head = """\
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


def main():
"""
    body = _LOOP_BODY
    if prelude:
        head += "\n".join(prelude) + "\n" + body
    else:
        head += body
    return head + "main()\n"


def _make_stdio_config(script: str, name: str = "fake-mcp") -> MCPServerConfig:
    """构建指向 *script* 的 stdio MCPServerConfig。"""
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=["-c", script],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 测试：完整流程 + 真实帧断言
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_connection_full_stdio_flow():
    """MCPConnection（transport=stdio）执行 initialize → tools/list → tools/call。"""
    script = _server_script()
    cfg = _make_stdio_config(script)
    conn = MCPConnection(cfg, timeout_ms=10000)
    try:
        ok = await conn.connect()
        assert ok is True

        initialized = await conn.initialize()
        assert initialized is True

        tools = await conn.list_tools()
        assert [t["name"] for t in tools] == ["echo"]

        echo_result = await conn.call_tool("echo", {"text": "hello"})
        assert echo_result.get("content", [{}])[0].get("text") == "ECHO: hello"

        # 真实帧断言：服务器把收到的原始行回显到 stderr。
        # 客户端共发送 4 条消息（initialize + initialized 通知 + tools/list + tools/call），
        # 每条都必须能作为单行 JSON 解析——若客户端仍用 Content-Length 帧，
        # 服务器 json.loads 会失败且不会响应，initialize 早已返回 False。
        assert conn._process is not None and conn._process.stderr is not None
        recv_lines: list[bytes] = []
        for _ in range(4):
            line = await asyncio.wait_for(conn._process.stderr.readline(), timeout=5)
            assert line.startswith(b"RECV: ")
            recv_lines.append(line[len(b"RECV: ") :].rstrip(b"\r\n"))

        assert all(b"Content-Length" not in line for line in recv_lines)
        for line in recv_lines:
            msg = json.loads(line)  # 单行 JSON 可解析 → 证明是换行分隔帧
            assert msg.get("jsonrpc") == "2.0"
    finally:
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 测试：非 JSON 噪音行容错
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_stdio_skips_non_json_stdout_noise():
    """服务器在 stdout 上打印非 JSON 噪音行 → 客户端跳过它并正常拿到响应。"""
    script = _server_script(noise=True)
    cfg = _make_stdio_config(script)
    conn = MCPConnection(cfg, timeout_ms=10000)
    try:
        ok = await conn.connect()
        assert ok is True

        # 噪音行先于响应到达 stdout；客户端必须丢弃它而不是报错
        initialized = await conn.initialize()
        assert initialized is True

        echo_result = await conn.call_tool("echo", {"text": "hi"})
        assert echo_result.get("content", [{}])[0].get("text") == "ECHO: hi"
    finally:
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 测试：无 id 通知消息跳过
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_stdio_skips_notification_message():
    """服务器先发送无 id 的通知消息 → 客户端跳过它并继续读取真正的响应。"""
    script = _server_script(notification=True)
    cfg = _make_stdio_config(script)
    conn = MCPConnection(cfg, timeout_ms=10000)
    try:
        ok = await conn.connect()
        assert ok is True

        # 通知（无 id）先于 initialize 响应到达；客户端必须跳过它
        initialized = await conn.initialize()
        assert initialized is True

        tools = await conn.list_tools()
        assert [t["name"] for t in tools] == ["echo"]
    finally:
        await conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 测试：服务器立即退出
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_stdio_server_exits_immediately():
    """服务器启动即退出（不响应）→ connect 仍返回 True，initialize 返回 False。"""
    script = _server_script(exit_immediately=True)
    cfg = _make_stdio_config(script)
    conn = MCPConnection(cfg, timeout_ms=10000)
    try:
        ok = await conn.connect()
        assert ok is True

        # 进程已死：写 stdin 或读 stdout 会抛 RuntimeError，被 initialize 吞掉
        initialized = await conn.initialize()
        assert initialized is False
    finally:
        await conn.close()
