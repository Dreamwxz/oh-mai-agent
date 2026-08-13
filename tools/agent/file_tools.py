"""文件访问控制工具。

安全模型：
  - guest：无文件访问权限（工具在 schema 层面即被隐藏，min_role=USER）。
  - user：限制在 ``user_workspace/``（通常为 ``data_dir/files/``）沙箱内。
          路径在前缀检查**之前**先 resolve，因此 ``../`` 逃逸攻击不可行。
  - admin：不受限的宿主机文件系统访问（当 admin_open=True 时）。

架构：
  ToolRegistry.execute() 执行第一道权限门控（min_role 校验）。
  role_provider 回调（由 Agent 循环注入）提供实际的调用者角色，用于 handler 内部的
  第二道门控 — 沙箱边界强制。攻击者无法伪造角色，因为回调绑定到运行时任务上下文，
  而非用户输入。

路径解析规则（所有角色通用）：
  1. ``Path(path).expanduser().resolve()`` — 始终在检查前先解析。
  2. 目标不存在但其祖先目录存在 → 允许（用于写入场景）。
  3. 完全无法解析的路径（无任何祖先存在）→ 拒绝。
  4. 已有路径的存在性检查在 I/O 时进行（读: is_file，写: mkdir）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from ...permission import Role
from ..registry import ToolDefinition

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────

_READ_LIMIT_BYTES: int = 200 * 1024  # 读取上限 200 KB


def _log_path(path: str | Path) -> str:
    """仅供日志使用：提取路径 basename，避免泄露完整文件路径。"""
    try:
        return Path(path).name or "<空>"
    except (TypeError, ValueError):
        return "<无效路径>"


# ── FileAccessPolicy：文件访问策略 ───────────────────────────────────────────


class FileAccessPolicy:
    """按角色强制执行路径沙箱边界。

    用法示例::

        policy = FileAccessPolicy(user_workspace=Path("data/files"))
        policy.ensure_user_workspace()
        ok, resolved = policy.resolve(Role.USER, "notes/todo.md")
    """

    def __init__(self, *, user_workspace: Path, admin_open: bool = True) -> None:
        self._user_workspace: Path = user_workspace
        self._user_workspace_resolved: Path = user_workspace.resolve()
        self._admin_open: bool = admin_open

    # ── 路径解析与校验 ────────────────────────────────────────────────────

    def resolve(self, role: Role, path: str | Path) -> tuple[bool, Path | str]:
        """根据 *role* 校验 *path* 并将其解析为绝对路径。

        Returns:
            ``(True, resolved_path)`` — 允许访问；*resolved_path* 是可立即用于
            I/O 的 ``Path`` 对象。

            ``(False, error_message)`` — 拒绝访问；*error_message* 是人类可读的
            ``str``。
        """
        # ── guest：一律拒绝 ─────────────────────────────────────────────
        if role is Role.GUEST:
            return False, "guest 无文件权限"

        raw: Path
        try:
            raw = Path(path)
        except TypeError:
            return False, f"无效路径: {path!r}"

        # ── 在路径前缀检查之前先 resolve（杜绝 ../ 逃逸） ──────────────────
        resolved: Path = raw.expanduser().resolve()

        # ── 不存在路径的检查 ────────────────────────────────────────────
        if not resolved.exists():
            # 向上遍历 — 如果没有任何祖先目录存在，则拒绝。
            check: Path = resolved
            while not check.exists():
                parent: Path = check.parent
                if parent == check:  # 已到达文件系统根
                    return False, f"路径不存在（无法解析到有效目录）: {raw}"
                check = parent
            # 至少有一个祖先存在 — 允许用于创建（write_file 场景）。

        # ── admin：不受限（当 admin_open=True 时） ────────────────────────
        if role is Role.ADMIN:
            if self._admin_open:
                return True, resolved
            # admin_open=False → 退回到 user 级别的沙箱逻辑。
            return self._user_check(resolved)

        # ── user：沙箱 ───────────────────────────────────────────────────
        if role is Role.USER:
            return self._user_check(resolved)

        # 安全兜底（理论上不可达）。
        return False, f"未识别的角色: {role}"

    # ── user 沙箱辅助方法 ─────────────────────────────────────────────────

    def _user_check(self, resolved: Path) -> tuple[bool, Path | str]:
        """检查 *resolved* 是否在 user workspace 内。

        is_relative_to（Python ≥ 3.9）按完整路径组件比对 —
        例如 ``/tmp/files_extra/secret`` 不会匹配 ``/tmp/files``。
        """
        ws = self._user_workspace_resolved
        if resolved.is_relative_to(ws):
            return True, resolved
        return False, f"无权访问此路径（沙箱外）: {resolved}"

    # ── workspace 初始化 ──────────────────────────────────────────────────

    def ensure_user_workspace(self) -> None:
        """创建 user workspace 目录树（递归 mkdir parents）。"""
        self._user_workspace.mkdir(parents=True, exist_ok=True)


# ── build_file_tools 工厂函数 ────────────────────────────────────────────────


def build_file_tools(
    ctx: object,
    *,
    user_workspace: Path,
    admin_open: bool = True,
    role_provider: Callable[[], Role] | None = None,
) -> list[ToolDefinition]:
    """创建 read_file / write_file 工具，由沙箱策略门控。

    Args:
        ctx: MaiBot 插件上下文（保留供未来使用，例如日志 / 配置访问）。
            由 Agent 循环在初始化时传入。
        user_workspace: user 级别文件访问的根目录。
            通常为 ``data_dir / "files"``，自动创建。
        admin_open: 若为 ``True``（默认），admin 完全绕过沙箱，拥有不受限的宿主机
            文件系统访问权限。
        role_provider: ``() -> Role`` — 返回**当前**调用者的角色。
            由 Agent 循环通过 contextvar 绑定的函数注入。
            若为 ``None``，则退回到 ``Role.GUEST``（相当于拒绝所有访问 — 对测试安全）。

    Returns:
        ``list[ToolDefinition]``，可直接通过 ``ToolRegistry`` 注册。
    """
    logger.debug("构建文件工具：workspace=%s, admin_open=%s", _log_path(user_workspace), admin_open)
    policy = FileAccessPolicy(user_workspace=user_workspace, admin_open=admin_open)
    policy.ensure_user_workspace()

    _rp: Callable[[], Role]
    if role_provider is None:

        def _guest_provider() -> Role:
            return Role.GUEST

        _rp = _guest_provider
    else:
        _rp = role_provider

    # ── read_file 处理器 ───────────────────────────────────────────────────

    async def read_file_handler(**kwargs: object) -> dict[str, object]:
        path_str = kwargs.get("path")
        if not isinstance(path_str, str):
            logger.warning("read_file 参数校验失败：path 缺失或非字符串")
            return {"success": False, "error": "缺少必需参数: path (string)"}

        role: Role = _rp()
        logger.debug("read_file 调用：file=%s, role=%s", _log_path(path_str), role.value)
        ok: bool
        result: Path | str
        ok, result = policy.resolve(role, path_str)
        if not ok:
            logger.warning("read_file 沙箱校验拒绝：role=%s, file=%s", role.value, _log_path(path_str))
            return {"success": False, "error": str(result)}

        resolved: Path = result  # type: ignore[assignment]

        def _read() -> dict[str, object]:
            try:
                raw_bytes = resolved.read_bytes()
            except PermissionError:
                logger.exception("read_file 读取失败：无权限，file=%s", _log_path(resolved))
                return {"success": False, "error": f"无权限读取: {resolved}"}
            except IsADirectoryError:
                logger.exception("read_file 读取失败：目标为目录，file=%s", _log_path(resolved))
                return {"success": False, "error": f"是一个目录，非文件: {resolved}"}
            except OSError as exc:
                logger.exception("read_file 读取失败：file=%s, error=%s", _log_path(resolved), exc)
                return {"success": False, "error": f"读取失败: {exc}"}

            logger.debug("read_file 读取成功：file=%s, size=%d", _log_path(resolved), len(raw_bytes))
            if len(raw_bytes) > _READ_LIMIT_BYTES:
                content = raw_bytes[:_READ_LIMIT_BYTES].decode("utf-8", errors="replace")
                return {
                    "success": True,
                    "content": content,
                    "truncated": True,
                    "original_size_bytes": len(raw_bytes),
                    "note": f"文件超过 {_READ_LIMIT_BYTES // 1024} KB，已截断",
                }

            content = raw_bytes.decode("utf-8", errors="replace")
            return {"success": True, "content": content}

        return await asyncio.to_thread(_read)

    # ── write_file 处理器 ──────────────────────────────────────────────────

    async def write_file_handler(**kwargs: object) -> dict[str, object]:
        path_str = kwargs.get("path")
        content = kwargs.get("content")
        if not isinstance(path_str, str):
            logger.warning("write_file 参数校验失败：path 缺失或非字符串")
            return {"success": False, "error": "缺少必需参数: path (string)"}
        if not isinstance(content, str):
            logger.warning("write_file 参数校验失败：content 缺失或非字符串")
            return {"success": False, "error": "缺少必需参数: content (string)"}

        role: Role = _rp()
        logger.debug(
            "write_file 调用：file=%s, role=%s, size=%d",
            _log_path(path_str),
            role.value,
            len(content.encode("utf-8")),
        )
        ok: bool
        result: Path | str
        ok, result = policy.resolve(role, path_str)
        if not ok:
            logger.warning("write_file 沙箱校验拒绝：role=%s, file=%s", role.value, _log_path(path_str))
            return {"success": False, "error": str(result)}

        resolved: Path = result  # type: ignore[assignment]

        def _write() -> dict[str, object]:
            try:
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(content, encoding="utf-8")
            except PermissionError:
                logger.exception("write_file 写入失败：无权限，file=%s", _log_path(resolved))
                return {"success": False, "error": f"无权限写入: {resolved}"}
            except IsADirectoryError:
                logger.exception("write_file 写入失败：目标为目录，file=%s", _log_path(resolved))
                return {"success": False, "error": f"是一个目录，无法覆盖写入: {resolved}"}
            except OSError as exc:
                logger.exception("write_file 写入失败：file=%s, error=%s", _log_path(resolved), exc)
                return {"success": False, "error": f"写入失败: {exc}"}
            logger.debug(
                "write_file 写入成功：file=%s, size=%d",
                _log_path(resolved),
                len(content.encode("utf-8")),
            )
            return {"success": True, "path": str(resolved)}

        return await asyncio.to_thread(_write)

    # ── 工具定义 ───────────────────────────────────────────────────────────

    logger.info("文件工具构建完成：read_file / write_file（admin_open=%s）", admin_open)
    return [
        ToolDefinition(
            name="read_file",
            description="读取指定路径的文件内容（UTF-8 文本）。文件最大 200 KB，超出部分截断。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要读取的文件路径（绝对路径或相对于 user workspace 的路径）。",
                    },
                },
                "required": ["path"],
            },
            handler=read_file_handler,
            visibility="discoverable",
            min_role=Role.USER,
        ),
        ToolDefinition(
            name="write_file",
            description="将内容写入指定路径的文件（UTF-8 编码）。父目录不存在时自动创建。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要写入的文件路径。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文件内容。",
                    },
                },
                "required": ["path", "content"],
            },
            handler=write_file_handler,
            visibility="discoverable",
            min_role=Role.USER,
        ),
    ]
