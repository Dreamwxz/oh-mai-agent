"""tools/agent/shell_tools.py — run_command 跨平台命令执行工具测试。

覆盖：
  - 工具元数据（discoverable / min_role=ADMIN / 参数 schema）
  - 跨平台执行（stdout / stderr / 退出码 / 管道 / 中文输出 / 非 UTF-8 输出兜底）
  - 超时强杀（含 POSIX 进程组级后代进程验证）
  - 输出截断
  - cwd 参数
  - 参数校验
  - 权限门控（registry 角色过滤 + role_provider 二次门控 + 缺省 GUEST）
  - TaskManager.setup 注册开关（[shell] enabled）

测试命令一律经 ``sys.executable`` 拼装（``-X utf8`` 保证两端编码一致），
避免 Windows cmd 与 POSIX sh 的差异。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from conftest import MockCtx

from oh_mai_agent.config import MaibotAgentConfig
from oh_mai_agent.core.scheduler import TaskScheduler
from oh_mai_agent.core.task_manager import TaskManager
from oh_mai_agent.domain.task_record import TaskRecord
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.shell_tools import build_shell_tools
from oh_mai_agent.tools.registry import ToolRegistry


def _py(code: str) -> str:
    """拼装 ``python -X utf8 -c <code>`` 命令字符串（跨平台）。"""
    return f'"{sys.executable}" -X utf8 -c "{code}"'


class _FakeShellConfig:
    """静态配置替身（对齐 ShellConfig 字段名）。"""

    def __init__(self, timeout_seconds: int = 60, max_output_chars: int = 8000) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars


def _tool(
    *,
    timeout: int = 60,
    max_chars: int = 8000,
    role: Role = Role.ADMIN,
) -> Any:
    """构建 run_command 工具（默认 admin 角色 provider + 静态配置）。"""
    cfg = _FakeShellConfig(timeout_seconds=timeout, max_output_chars=max_chars)
    tools = build_shell_tools(
        object(),
        config_getter=lambda: cfg,
        role_provider=lambda: role,
    )
    assert len(tools) == 1
    return tools[0]


# ═══════════════════════════════════════════════════════════════════════════════
# 配置默认值
# ═══════════════════════════════════════════════════════════════════════════════

def test_shell_config_defaults() -> None:
    cfg = MaibotAgentConfig()
    assert cfg.shell.enabled is True
    assert cfg.shell.timeout_seconds == 60
    assert cfg.shell.max_output_chars == 8000


# ═══════════════════════════════════════════════════════════════════════════════
# 工具元数据
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolMeta:
    def test_meta_discoverable_admin(self) -> None:
        tool = _tool()
        assert tool.name == "run_command"
        assert tool.visibility == "discoverable"
        assert tool.min_role == Role.ADMIN
        assert tool.parameters["required"] == ["command"]
        assert tool.parameters["properties"]["command"]["type"] == "string"


# ═══════════════════════════════════════════════════════════════════════════════
# happy path（跨平台）
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecution:
    @pytest.mark.asyncio
    async def test_stdout_captured(self) -> None:
        result = await _tool().handler(command=_py("print('hello-agent-42')"))
        assert result["success"] is True
        assert result["exit_code"] == 0
        assert result["timed_out"] is False
        assert "hello-agent-42" in result["stdout"]

    @pytest.mark.asyncio
    async def test_stderr_captured(self) -> None:
        result = await _tool().handler(
            command=_py("import sys; print('boom-err', file=sys.stderr)"),
        )
        assert result["success"] is True
        assert result["exit_code"] == 0
        assert result["stdout"] == ""
        assert "boom-err" in result["stderr"]

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_is_command_result_not_failure(self) -> None:
        """退出码非 0 属于命令执行结果，工具仍返回 success=True。"""
        result = await _tool().handler(command=_py("import sys; sys.exit(3)"))
        assert result["success"] is True
        assert result["exit_code"] == 3

    @pytest.mark.asyncio
    async def test_pipeline_shell_syntax_works(self) -> None:
        """管道语法在两端 shell（cmd / sh）都可用，无需平台特判。"""
        command = (
            _py("print('hi')")
            + " | "
            + _py("import sys; print(sys.stdin.read().strip().upper())")
        )
        result = await _tool().handler(command=command)
        assert result["success"] is True
        assert result["exit_code"] == 0
        assert "HI" in result["stdout"]

    @pytest.mark.asyncio
    async def test_unicode_output_decoded(self) -> None:
        result = await _tool().handler(command=_py("print('你好世界')"))
        assert result["success"] is True
        assert "你好世界" in result["stdout"]

    @pytest.mark.asyncio
    async def test_non_utf8_bytes_fallback_decode(self) -> None:
        """非 UTF-8 字节输出不抛异常，回退本地编码 + replace 兜底。"""
        result = await _tool().handler(
            command=_py("import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\x80')"),
        )
        assert result["success"] is True
        assert isinstance(result["stdout"], str)


# ═══════════════════════════════════════════════════════════════════════════════
# 超时与进程树强杀
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_kills_and_reports(self) -> None:
        start = time.monotonic()
        result = await _tool(timeout=1).handler(command=_py("import time; time.sleep(30)"))
        elapsed = time.monotonic() - start
        assert result["success"] is False
        assert result["timed_out"] is True
        assert "超时" in result["error"]
        assert elapsed < 15, "超时后应迅速返回，实际耗时 %.1fs" % elapsed

    @pytest.mark.asyncio
    async def test_per_call_timeout_override_applied(self) -> None:
        """单次调用 timeout_seconds 覆盖配置默认值（60s → 1s）。"""
        result = await _tool(timeout=60).handler(
            command=_py("import time; time.sleep(30)"), timeout_seconds=1,
        )
        assert result["timed_out"] is True

    @pytest.mark.skipif(os.name == "nt", reason="进程组验证仅 POSIX 语义明确")
    @pytest.mark.asyncio
    async def test_timeout_kills_descendant_processes(self, tmp_path: Path) -> None:
        """超时后整棵进程树被强杀：外层 python 的子进程不得残留。"""
        pid_file = tmp_path / "child.pid"
        script = (
            "import subprocess, sys, time; "
            f"p = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
            "time.sleep(30)"
        )
        result = await _tool(timeout=1).handler(command=_py(script))
        assert result["timed_out"] is True

        child_pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"子进程 {child_pid} 在超时后仍存活（进程组强杀失效）")


# ═══════════════════════════════════════════════════════════════════════════════
# 输出截断
# ═══════════════════════════════════════════════════════════════════════════════

class TestOutputLimit:
    @pytest.mark.asyncio
    async def test_stdout_truncated(self) -> None:
        result = await _tool(max_chars=50).handler(command=_py("print('x' * 300)"))
        assert result["success"] is True
        assert result["truncated"] is True
        assert len(result["stdout"]) <= 50
        assert "截断" in result["note"]

    @pytest.mark.asyncio
    async def test_short_output_not_truncated(self) -> None:
        result = await _tool(max_chars=50).handler(command=_py("print('tiny')"))
        assert result["success"] is True
        assert result["truncated"] is False
        assert "note" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# cwd 参数
# ═══════════════════════════════════════════════════════════════════════════════

class TestCwd:
    @pytest.mark.asyncio
    async def test_cwd_applied(self, tmp_path: Path) -> None:
        result = await _tool().handler(
            command=_py("import os; print(os.getcwd())"), cwd=str(tmp_path),
        )
        assert result["success"] is True
        assert str(tmp_path) in result["stdout"]

    @pytest.mark.asyncio
    async def test_nonexistent_cwd_rejected(self) -> None:
        result = await _tool().handler(
            command=_py("print('x')"), cwd="/definitely/not/exist/xyz",
        )
        assert result["success"] is False
        assert "工作目录" in result["error"]

    @pytest.mark.asyncio
    async def test_non_string_cwd_rejected(self) -> None:
        result = await _tool().handler(command=_py("print('x')"), cwd=123)
        assert result["success"] is False
        assert "cwd" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# 参数校验
# ═══════════════════════════════════════════════════════════════════════════════

class TestParamValidation:
    @pytest.mark.asyncio
    async def test_missing_command(self) -> None:
        assert (await _tool().handler())["success"] is False

    @pytest.mark.asyncio
    async def test_blank_command(self) -> None:
        assert (await _tool().handler(command="   "))["success"] is False

    @pytest.mark.asyncio
    async def test_non_string_command(self) -> None:
        assert (await _tool().handler(command=123))["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_timeout_rejected(self) -> None:
        r1 = await _tool().handler(command=_py("print('x')"), timeout_seconds="abc")
        assert r1["success"] is False
        assert "timeout_seconds" in r1["error"]
        r2 = await _tool().handler(command=_py("print('x')"), timeout_seconds=0)
        assert r2["success"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 权限门控
# ═══════════════════════════════════════════════════════════════════════════════

class TestPermissionGates:
    def test_registry_hides_from_user(self) -> None:
        reg = ToolRegistry()
        reg.register(_tool())
        assert "run_command" not in reg.names(Role.GUEST)
        assert "run_command" not in reg.names(Role.USER)
        assert "run_command" in reg.names(Role.ADMIN)

    @pytest.mark.asyncio
    async def test_registry_execute_denies_user(self) -> None:
        reg = ToolRegistry()
        reg.register(_tool())
        result = await reg.execute("run_command", Role.USER, command=_py("print('x')"))
        assert result == {"success": False, "error": "permission denied"}

    @pytest.mark.asyncio
    async def test_registry_execute_allows_admin(self) -> None:
        reg = ToolRegistry()
        reg.register(_tool())
        result = await reg.execute("run_command", Role.ADMIN, command=_py("print('via-registry')"))
        assert result["success"] is True
        assert "via-registry" in result["stdout"]

    @pytest.mark.asyncio
    async def test_role_provider_second_gate(self) -> None:
        """role_provider 返回 USER → handler 内部二次门控拒绝（防伪造角色）。"""
        result = await _tool(role=Role.USER).handler(command=_py("print('x')"))
        assert result["success"] is False
        assert "permission denied" in result["error"]

    @pytest.mark.asyncio
    async def test_role_provider_default_denies(self) -> None:
        """role_provider 缺省 → GUEST → 全部拒绝（对测试安全）。"""
        tools = build_shell_tools(object())
        result = await tools[0].handler(command=_py("print('x')"))
        assert result["success"] is False
        assert "permission denied" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# TaskManager.setup 注册开关
# ═══════════════════════════════════════════════════════════════════════════════

async def _noop_executor(task: TaskRecord) -> None:
    pass


@pytest_asyncio.fixture
async def manager(
    tmp_path: Path,
    command_bus: Any,
    prompt_service: Any,
    default_config: MaibotAgentConfig,
    default_resolver: Any,
) -> tuple[TaskManager, ToolRegistry]:
    store = TaskStore(str(tmp_path / "test.db"))
    await store.init()
    sched = TaskScheduler(default_config.task, store, _noop_executor, command_bus=command_bus)
    registry = ToolRegistry()
    tm = TaskManager(
        ctx=MockCtx(), store=store, scheduler=sched,
        registry=registry, resolver=default_resolver, config=default_config,
        prompt_service=prompt_service, command_bus=command_bus,
    )
    return tm, registry


class TestRegistration:
    @pytest.mark.asyncio
    async def test_setup_registers_run_command_when_enabled(
        self, manager: tuple[TaskManager, ToolRegistry],
    ) -> None:
        tm, registry = manager
        await tm.setup()
        assert "run_command" in registry.all_names()
        tool = registry.get("run_command")
        assert tool is not None
        assert tool.min_role == Role.ADMIN

    @pytest.mark.asyncio
    async def test_setup_skips_run_command_when_disabled(
        self, manager: tuple[TaskManager, ToolRegistry], default_config: MaibotAgentConfig,
    ) -> None:
        default_config.shell.enabled = False
        tm, registry = manager
        await tm.setup()
        assert "run_command" not in registry.all_names()
