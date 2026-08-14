"""宿主机命令执行工具（run_command）— 跨平台设计。

Windows 与 Linux/macOS 共用同一个工具，shell 在运行时按平台自动切换：

  - Windows（``os.name == "nt"``）：``cmd.exe``（经 ``COMSPEC`` 定位）。
  - POSIX（Linux / macOS）：``/bin/sh``。

实现经 ``subprocess.Popen(command, shell=True)`` 交给平台默认 shell，因此
管道、重定向、环境变量等 shell 语法在两端语义一致，Agent 无需按平台特判
命令写法（例如在 Windows 上直接写 ``dir`` / ``type``，在 Linux 上直接写
``ls`` / ``cat``）。

安全模型（三道防线，与文件工具同构）：
  1. ``min_role=Role.ADMIN`` —— 工具在 schema 呈现与执行两个阶段均被
     PermissionResolver 门控，guest / user 不可见、不可调用。
  2. handler 内部经 ``role_provider`` 二次门控（绑定 current_task ContextVar
     的实时角色），攻击者无法伪造角色。
  3. 默认 discoverable —— 不常驻上下文，需 Agent 先经 list_tools 发现。

运行期防护：
  - 超时强杀进程树：POSIX 用 ``os.killpg(SIGKILL)``（start_new_session 使
    shell 成为进程组组长），Windows 用 ``taskkill /F /T`` 递归终止，避免
    超时后残留子进程继续运行。
  - 输出截断：stdout / stderr 各自按配置上限截断，防止输出撑爆 LLM 上下文。
  - 编码兜底：优先 UTF-8，失败回退系统本地编码（Windows 中文 codepage
    如 GBK 的输出也能正确还原，errors=replace 兜底非法字节）。
"""

from __future__ import annotations

import asyncio
import locale
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...permission import PermissionResolver, Role
from ..registry import ToolDefinition

if TYPE_CHECKING:
    from ...config import ShellConfig

logger = logging.getLogger(__name__)

# ── 默认值（config_getter 缺省时使用）───────────────────────────────────────

_DEFAULT_TIMEOUT_SECONDS: int = 60
_DEFAULT_MAX_OUTPUT_CHARS: int = 8000


class _DefaultShellConfig:
    """config_getter 缺省时的静态配置替身（对齐 ShellConfig 的字段名）。"""

    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS


# ── 私有辅助 ────────────────────────────────────────────────────────────────


def _decode_output(data: bytes) -> str:
    """解码子进程输出：优先 UTF-8，失败回退系统本地编码（errors=replace）。

    Windows 下 cmd 常以本地 codepage（如 GBK）输出中文，UTF-8 严格解码会抛
    UnicodeDecodeError，回退后可还原本地编码文本，非法字节以替换符兜底。
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """按字符数截断 *text*；返回 ``(text, truncated)``。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """强杀 *proc* 及其全部子进程（整棵进程树）。

    POSIX：进程组 SIGKILL（start_new_session 保证 shell 是组组长）。
    Windows：``taskkill /F /T`` 递归终止整棵进程树。
    目标进程组已消亡时静默忽略。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("taskkill 终止进程树失败（pid=%s）：%s", proc.pid, exc)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # 进程组已消亡，无需处理


def _run_sync(
    command: str,
    *,
    timeout_seconds: int,
    max_output_chars: int,
    cwd: str | None,
) -> dict[str, Any]:
    """同步执行命令（在 to_thread 线程中运行，不阻塞事件循环）。

    Returns:
        命令完成时返回 ``{"success": True, "exit_code": N, "stdout": ...,
        "stderr": ..., "timed_out": False, ...}`` —— 退出码非 0 属于命令
        执行结果而非工具失败，由 LLM 结合输出自行判断。
        超时 / 启动失败返回 ``{"success": False, "error": ...}``。
    """
    popen_kwargs: dict[str, Any] = {
        "shell": True,  # 平台默认 shell：Windows → cmd.exe，POSIX → /bin/sh
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": cwd,
    }
    if os.name == "nt":
        # 新进程组 + 无控制台窗口：后台服务场景不弹窗，且 taskkill /T 可枚举整树
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        # 新会话：shell 成为进程组组长，超时后可按进程组整组强杀
        popen_kwargs["start_new_session"] = True

    start = time.monotonic()
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        logger.error("命令启动失败：%r, error=%s", command, exc)
        return {"success": False, "error": f"命令启动失败: {exc}"}

    timed_out = False
    try:
        out_b, err_b = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        logger.warning("命令超时（%ds），强杀进程树：%r", timeout_seconds, command)
        # communicate(timeout) 只杀了直接子进程，这里补杀整棵进程树
        _kill_process_tree(proc)
        out_b, err_b = proc.communicate()  # 回收管道并等待退出

    duration = time.monotonic() - start
    stdout_text, stdout_trunc = _truncate(_decode_output(out_b), max_output_chars)
    stderr_text, stderr_trunc = _truncate(_decode_output(err_b), max_output_chars)
    truncated = stdout_trunc or stderr_trunc

    if timed_out:
        return {
            "success": False,
            "command": command,
            "error": f"命令执行超时（{timeout_seconds} 秒），已强制终止进程树",
            "timed_out": True,
            "exit_code": proc.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "truncated": truncated,
            "duration_seconds": round(duration, 3),
        }

    logger.info(
        "命令执行完成：exit=%s, duration=%.2fs, out=%d chars, err=%d chars%s",
        proc.returncode, duration, len(stdout_text), len(stderr_text),
        ", 已截断" if truncated else "",
    )
    result: dict[str, Any] = {
        "success": True,
        "command": command,
        "exit_code": proc.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timed_out": False,
        "truncated": truncated,
        "duration_seconds": round(duration, 3),
    }
    if truncated:
        result["note"] = f"输出超过 {max_output_chars} 字符，已截断"
    return result


# ── 工厂函数 ────────────────────────────────────────────────────────────────


def build_shell_tools(
    ctx: object,
    *,
    config_getter: Callable[[], "ShellConfig"] | None = None,
    role_provider: Callable[[], Role] | None = None,
) -> list[ToolDefinition]:
    """创建宿主机命令执行工具 ``run_command``（跨平台：Windows / Linux / macOS）。

    Args:
        ctx: MaiBot 插件上下文（保留供未来使用，与文件工具签名对齐）。
        config_getter: ``() -> ShellConfig`` — 每次调用读取，配置热更新
            （timeout / 输出上限）立即生效；缺省时使用静态默认值
            （60s 超时 / 8000 字符输出上限）。
        role_provider: ``() -> Role`` — 返回**当前**调用者的角色，由 Agent
            循环经 contextvar 注入；为 ``None`` 时退回 ``Role.GUEST``
            （相当于拒绝所有访问 — 对测试安全）。

    Returns:
        ``list[ToolDefinition]``，仅含 ``run_command`` 一个工具
        （discoverable / 最低角色 ADMIN）。
    """
    if config_getter is None:
        config_getter = lambda: _DefaultShellConfig()

    _rp: Callable[[], Role]
    if role_provider is None:

        def _guest_provider() -> Role:
            return Role.GUEST

        _rp = _guest_provider
    else:
        _rp = role_provider

    async def run_command_handler(**kwargs: object) -> dict[str, object]:
        command = kwargs.get("command")
        if not isinstance(command, str) or not command.strip():
            logger.warning("run_command 参数校验失败：command 缺失或为空")
            return {"success": False, "error": "缺少必需参数: command (string)"}

        # 第二道门控：实时角色（绑定 current_task，攻击者无法伪造）
        role: Role = _rp()
        if not PermissionResolver.require(role, Role.ADMIN):
            return {"success": False, "error": "permission denied: 需要 admin 角色"}

        # ── 超时参数 ──────────────────────────────────────────────
        cfg = config_getter()
        timeout_raw = kwargs.get("timeout_seconds")
        if timeout_raw is None:
            timeout: int = int(cfg.timeout_seconds)
        elif isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
            return {"success": False, "error": "参数 timeout_seconds 必须是正数"}
        else:
            timeout = int(timeout_raw)
        if timeout <= 0:
            return {"success": False, "error": "参数 timeout_seconds 必须是正数"}

        # ── 工作目录参数 ──────────────────────────────────────────
        cwd: str | None = None
        cwd_raw = kwargs.get("cwd")
        if cwd_raw is not None:
            if not isinstance(cwd_raw, str):
                return {"success": False, "error": "参数 cwd 必须是字符串"}
            try:
                resolved_cwd = Path(cwd_raw).expanduser().resolve()
            except (TypeError, ValueError):
                return {"success": False, "error": f"无效工作目录: {cwd_raw!r}"}
            if not resolved_cwd.is_dir():
                return {"success": False, "error": f"工作目录不存在或不是目录: {resolved_cwd}"}
            cwd = str(resolved_cwd)

        max_chars: int = int(cfg.max_output_chars)
        logger.info(
            "run_command 调用：role=%s, timeout=%ds, command=%s",
            role.value, timeout, command[:200],
        )
        return await asyncio.to_thread(
            _run_sync, command,
            timeout_seconds=timeout, max_output_chars=max_chars, cwd=cwd,
        )

    logger.info("命令执行工具构建完成：run_command（discoverable / ADMIN）")
    return [
        ToolDefinition(
            name="run_command",
            description=(
                "在宿主机上执行 shell 命令：Windows 自动使用 cmd.exe，Linux/macOS 自动使用 "
                "/bin/sh，管道、重定向、环境变量等语法与本地终端一致。"
                "仅 admin 可调用。exit_code 非 0 或 stderr 有内容不代表工具失败，"
                "请结合 stdout/stderr 内容判断命令是否达成目标；输出超过上限会被截断。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "要执行的命令字符串（支持管道、重定向、环境变量等 shell 语法）"
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            "超时秒数（正整数），超时后强制终止整个进程树；缺省取配置默认值"
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "命令工作目录（绝对路径，可选）；缺省为插件进程当前目录"
                        ),
                    },
                },
                "required": ["command"],
            },
            handler=run_command_handler,
            visibility="discoverable",
            min_role=Role.ADMIN,
        ),
    ]
