"""tools/agent/file_tools.py — FileAccessPolicy 沙箱与 read_file / write_file 行为测试。

此前仅 sandbox 集成测试覆盖 happy path；本文件补齐 guest 拒绝、
路径逃逸、admin_open=False 回退、role_provider=None 兜底、>200KB 截断、
目录读取 / 写入、参数校验等分支。

注意：FileAccessPolicy.resolve 按 cwd 解析相对路径，测试统一使用
workspace 下的绝对路径（与 Agent 运行时行为一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import MockCtx

from oh_mai_agent.permission import Role
from oh_mai_agent.tools.agent.file_tools import FileAccessPolicy, build_file_tools

# 读取上限（与实现保持一致）
_READ_LIMIT_BYTES = 200 * 1024


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "files"


def _policy(workspace: Path, *, admin_open: bool = True) -> FileAccessPolicy:
    p = FileAccessPolicy(user_workspace=workspace, admin_open=admin_open)
    p.ensure_user_workspace()
    return p


def _file_tools(
    workspace: Path,
    *,
    admin_open: bool = True,
    role_provider: Any = None,
) -> dict[str, Any]:
    tools = build_file_tools(
        MockCtx(), user_workspace=workspace,
        admin_open=admin_open, role_provider=role_provider,
    )
    return {t.name: t for t in tools}


# ═══════════════════════════════════════════════════════════════════════════════
# FileAccessPolicy.resolve
# ═══════════════════════════════════════════════════════════════════════════════

class TestFileAccessPolicy:
    def test_guest_always_denied(self, workspace: Path) -> None:
        ok, err = _policy(workspace).resolve(Role.GUEST, str(workspace / "a.txt"))
        assert ok is False
        assert "guest" in err

    def test_user_inside_workspace_allowed(self, workspace: Path) -> None:
        ok, resolved = _policy(workspace).resolve(Role.USER, str(workspace / "notes" / "a.txt"))
        assert ok is True
        assert str(resolved).endswith("notes/a.txt")

    def test_user_outside_workspace_denied(self, workspace: Path, tmp_path: Path) -> None:
        ok, err = _policy(workspace).resolve(Role.USER, str(tmp_path / "outside.txt"))
        assert ok is False
        assert "沙箱外" in err

    def test_user_dotdot_escape_denied(self, workspace: Path, tmp_path: Path) -> None:
        """../ 逃逸在 resolve 阶段被归一化后拦截。"""
        ok, err = _policy(workspace).resolve(
            Role.USER, str(workspace / "sub" / ".." / ".." / "outside.txt"),
        )
        assert ok is False
        assert "沙箱外" in err

    def test_user_prefix_sibling_not_matched(self, workspace: Path, tmp_path: Path) -> None:
        """/tmp/files_extra 不因前缀相似而通过 /tmp/files 沙箱。"""
        sibling = tmp_path / "files_extra"
        sibling.mkdir()
        ok, err = _policy(workspace).resolve(Role.USER, str(sibling / "secret.txt"))
        assert ok is False

    def test_admin_unrestricted_when_open(self, workspace: Path) -> None:
        ok, resolved = _policy(workspace).resolve(Role.ADMIN, "/etc/hostname")
        assert ok is True
        assert str(resolved) == "/etc/hostname"

    def test_admin_falls_back_to_user_check_when_closed(
        self, workspace: Path, tmp_path: Path,
    ) -> None:
        policy = _policy(workspace, admin_open=False)
        # 沙箱内仍允许
        ok, _ = policy.resolve(Role.ADMIN, str(workspace / "notes" / "a.txt"))
        assert ok is True
        # 沙箱外被拒绝
        ok, err = policy.resolve(Role.ADMIN, str(tmp_path / "outside.txt"))
        assert ok is False
        assert "沙箱外" in err

    def test_invalid_path_type_denied(self, workspace: Path) -> None:
        ok, err = _policy(workspace).resolve(Role.USER, 123)
        assert ok is False
        assert "无效路径" in err

    def test_nonexistent_path_with_existing_ancestor_allowed_for_create(
        self, workspace: Path,
    ) -> None:
        """文件不存在但祖先目录存在 → 允许（write_file 创建场景）。"""
        ok, resolved = _policy(workspace).resolve(Role.USER, str(workspace / "deep" / "new" / "file.txt"))
        assert ok is True
        assert str(resolved).endswith("deep/new/file.txt")

    def test_ensure_user_workspace_creates_tree(self, tmp_path: Path) -> None:
        ws = tmp_path / "a" / "b" / "files"
        policy = FileAccessPolicy(user_workspace=ws)
        policy.ensure_user_workspace()
        assert ws.is_dir()


# ═══════════════════════════════════════════════════════════════════════════════
# read_file
# ═══════════════════════════════════════════════════════════════════════════════

class TestReadFile:
    @pytest.mark.asyncio
    async def test_read_success(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        (workspace / "notes").mkdir(exist_ok=True)
        (workspace / "notes" / "a.txt").write_text("hello", encoding="utf-8")
        result = await tools["read_file"].handler(path=str(workspace / "notes" / "a.txt"))
        assert result == {"success": True, "content": "hello"}

    @pytest.mark.asyncio
    async def test_missing_path_param(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        result = await tools["read_file"].handler()
        assert result == {"success": False, "error": "缺少必需参数: path (string)"}

    @pytest.mark.asyncio
    async def test_non_string_path_param(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        result = await tools["read_file"].handler(path=123)
        assert result == {"success": False, "error": "缺少必需参数: path (string)"}

    @pytest.mark.asyncio
    async def test_sandbox_denied(self, workspace: Path, tmp_path: Path) -> None:
        (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        result = await tools["read_file"].handler(path=str(tmp_path / "secret.txt"))
        assert result["success"] is False
        assert "沙箱外" in result["error"]

    @pytest.mark.asyncio
    async def test_guest_denied(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.GUEST)
        (workspace / "a.txt").write_text("x", encoding="utf-8")
        result = await tools["read_file"].handler(path=str(workspace / "a.txt"))
        assert result["success"] is False
        assert "guest" in result["error"]

    @pytest.mark.asyncio
    async def test_default_role_provider_is_guest(self, workspace: Path) -> None:
        """role_provider 缺省 → GUEST → 全部拒绝（对测试安全）。"""
        tools = _file_tools(workspace)
        (workspace / "a.txt").write_text("x", encoding="utf-8")
        result = await tools["read_file"].handler(path=str(workspace / "a.txt"))
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_directory_read_returns_error(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        (workspace / "sub").mkdir(exist_ok=True)
        result = await tools["read_file"].handler(path=str(workspace / "sub"))
        assert result["success"] is False
        assert "目录" in result["error"]

    @pytest.mark.asyncio
    async def test_large_file_truncated(self, workspace: Path) -> None:
        """超过 200KB 的文件被截断并标记 truncated。"""
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        big = "x" * (_READ_LIMIT_BYTES + 1024)
        (workspace / "big.txt").write_text(big, encoding="utf-8")
        result = await tools["read_file"].handler(path=str(workspace / "big.txt"))
        assert result["success"] is True
        assert result["truncated"] is True
        assert result["original_size_bytes"] == len(big)
        assert len(result["content"]) == _READ_LIMIT_BYTES


# ═══════════════════════════════════════════════════════════════════════════════
# write_file
# ═══════════════════════════════════════════════════════════════════════════════

class TestWriteFile:
    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        result = await tools["write_file"].handler(
            path=str(workspace / "sub" / "dir" / "f.txt"), content="hi",
        )
        assert result["success"] is True
        assert (workspace / "sub" / "dir" / "f.txt").read_text(encoding="utf-8") == "hi"

    @pytest.mark.asyncio
    async def test_missing_params(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        assert (await tools["write_file"].handler())["success"] is False
        assert (await tools["write_file"].handler(path="a.txt"))["success"] is False

    @pytest.mark.asyncio
    async def test_write_to_directory_returns_error(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        result = await tools["write_file"].handler(path=str(workspace), content="x")
        assert result["success"] is False
        assert "目录" in result["error"]

    @pytest.mark.asyncio
    async def test_sandbox_denied(self, workspace: Path, tmp_path: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.USER)
        result = await tools["write_file"].handler(
            path=str(tmp_path / "outside.txt"), content="x",
        )
        assert result["success"] is False
        assert "沙箱外" in result["error"]

    @pytest.mark.asyncio
    async def test_guest_denied(self, workspace: Path) -> None:
        tools = _file_tools(workspace, role_provider=lambda: Role.GUEST)
        result = await tools["write_file"].handler(path=str(workspace / "a.txt"), content="x")
        assert result["success"] is False
