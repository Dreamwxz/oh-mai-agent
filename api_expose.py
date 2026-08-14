"""oh-mai-agent 跨插件 API 处理器。

暴露任务管理 API（创建/列表/获取/取消/注入/历史），
供其它 MaiBot 插件通过 ``ctx.api.call()`` 调用。

设计意图：API 默认暴露等级为 user+，config.api_expose.max_level
用于声明最大暴露等级。

当前实现（与设计意图有差距）：
  - build_api_handlers() 不读取 config.api_expose.max_level，
    全部 6 个端点均以 public=True 注册，无等级过滤；
  - check_api_call_permission() 已实现角色/等级比较逻辑，
    但全仓库无任何调用点，属未接入的死代码。
"""

from __future__ import annotations

import logging
from typing import Any

from .config import MaibotAgentConfig
from .permission import PermissionResolver, Role
from .core.task_manager import TaskManager
from .domain.task_record import TaskRecord, TaskStatus

logger = logging.getLogger(__name__)


def check_api_call_permission(role: Role, max_level: str) -> bool:
    """检查 *role* 是否满足 *max_level* 的 API 暴露要求。

    供调用方插件或 SDK 注册层用于跨插件 API 访问门控。

    Args:
        role: 调用方的解析后角色（GUEST/USER/ADMIN）。
        max_level: 配置中的最大暴露等级
            （``"guest"`` / ``"user"`` / ``"admin"``）。

    Returns:
        若 ``role >= max_level`` 则返回 ``True``。

    Example:
        >>> check_api_call_permission(Role.USER, "user")
        True
        >>> check_api_call_permission(Role.GUEST, "user")
        False
    """
    _order: dict[str, int] = {"guest": 0, "user": 1, "admin": 2}
    role_val = _order.get(role.value, -1)
    max_val = _order.get(max_level, 999)
    allowed = role_val >= max_val
    if allowed:
        logger.debug("API 调用允许：角色 %s 满足最大暴露等级 %s", role.value, max_level)
    else:
        logger.info("API 调用被拒绝：角色 %s 不满足最大暴露等级 %s", role.value, max_level)
    return allowed


def _to_int(val: Any, default: int = 0) -> int:
    """安全地将 *val* 转为 int，失败时返回 *default*。"""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_status(status_str: str | None) -> TaskStatus | None:
    """将状态字符串解析为 TaskStatus，空字符串或非法值返回 None。"""
    if not status_str:
        return None
    try:
        return TaskStatus(str(status_str))
    except ValueError:
        return None


def build_api_handlers(
    task_manager: TaskManager,
    resolver: PermissionResolver,
    config: MaibotAgentConfig,
) -> list[dict[str, Any]]:
    """构建跨插件 API 处理器描述符列表。

    返回字典列表，每个字典描述一个公开 API 端点，
    供 SDK 注册（通过 ``register_dynamic_api`` 或手动组件注册）。
    每个字典包含：

    - ``name``：API 名称（如 ``"create"``）。
    - ``description``：人类可读描述。
    - ``version``：API 版本（``"1"``）。
    - ``public``：``True``（跨插件可见）。
    - ``handler``：异步可调用对象 ``async def handler(**kwargs) -> dict``。

    所有处理器成功时返回 ``{"success": True, ...}``。
    失败时返回 ``{"success": False, ...}``：cancel/inject 的失败信息经
    TaskManager 的 ``(bool, str)`` 返回，落在 ``message`` 字段；其余端点及
    所有异常路径落在 ``error`` 字段。

    跨插件 API 调用内部按 ADMIN 级别处理。
    注意：外部权限门控函数 :func:`check_api_call_permission` 已定义，
    但当前未在注册或调用链路中实际执行 — 全部端点硬编码 ``public=True``。

    Args:
        task_manager: TaskManager 实例，用于任务操作。
        resolver: 权限解析器（保留；内部未使用 — 当前实现不做等级门控）。
        config: 完整插件配置（当前实现未读取 ``api_expose.max_level``；
            该配置键声明但未强制执行）。

    Returns:
        API 处理器描述符列表。
    """
    # 跨插件 API 调用视为可信：处理器统一以 ADMIN 角色调用 TaskManager，
    # 避免面向用户的内部权限校验阻碍合法的跨插件操作。
    _caller_role = Role.ADMIN

    # ── create ──────────────────────────────────────────────────────────
    async def _create(**kwargs: Any) -> dict[str, Any]:
        """创建新任务。

        期望 kwargs：
            intent (str): 任务意图描述。
            owner (str): 任务所有者（必须由调用方提供）。
            platform (str): 平台标识符。
            stream_id (str): 目标聊天流 ID。
            delay_seconds (int, 可选): 执行前延迟的秒数。
            cron_expr (str, 可选): 定时任务 cron 表达式。
            priority (int, 可选): 任务优先级（数值越高越紧急）。
            reply_stream_id (str, 可选): 回复目标聊天流 ID
                （缺省时回复到 stream_id 指定的流）。

        注意：本端点不接受 ``level`` 参数——INSTANT 仅由定时任务与
        Agent 模型显式创建，跨插件 API 创建的任务固定为 agent 级。
        """
        try:
            intent = str(kwargs.get("intent", ""))
            owner = str(kwargs.get("owner", ""))
            platform = str(kwargs.get("platform", ""))
            stream_id = str(kwargs.get("stream_id", ""))
            delay_seconds: int | None = _to_int(kwargs.get("delay_seconds")) or None
            cron_expr: str | None = kwargs.get("cron_expr")
            priority: int = _to_int(kwargs.get("priority", 0))
            reply_stream_id: str | None = kwargs.get("reply_stream_id")

            logger.debug(
                "跨插件 API create 调用：owner=%s、platform=%s、stream_id=%s、intent=%.80r",
                owner,
                platform,
                stream_id,
                intent,
            )

            ok, result = await task_manager.create_task(
                intent=intent,
                owner=owner,
                platform=platform,
                stream_id=stream_id,
                delay_seconds=delay_seconds,
                cron_expr=cron_expr,
                priority=priority,
                reply_stream_id=reply_stream_id,
                caller_role=_caller_role,
            )
            if ok and isinstance(result, TaskRecord):
                logger.info(
                    "跨插件 API create 成功：任务 %s 已创建（level=%s）",
                    result.id,
                    result.level.value,
                )
                return {
                    "success": True,
                    "task_id": result.id,
                    "title": result.title,
                    "level": result.level.value,
                }
            logger.warning("跨插件 API create 失败：%.80r", str(result))
            return {"success": False, "error": str(result)}
        except Exception as exc:
            logger.exception("跨插件 API create 调用异常：%.80r", str(exc))
            return {"success": False, "error": str(exc)}

    # ── list ────────────────────────────────────────────────────────────
    async def _list(**kwargs: Any) -> dict[str, Any]:
        """列出任务摘要。

        期望 kwargs：
            owner (str): 按任务所有者筛选。
            status (str, 可选): 按任务状态筛选。
            limit (int, 可选): 最大结果数（默认 50）。
        """
        try:
            owner = str(kwargs.get("owner", ""))
            status_str: str | None = kwargs.get("status")
            limit: int = _to_int(kwargs.get("limit", 50)) or 50

            logger.debug(
                "跨插件 API list 调用：owner=%s、status=%s、limit=%d",
                owner,
                status_str,
                limit,
            )
            status = _parse_status(status_str)
            if status_str and status is None:
                logger.warning("跨插件 API list 参数校验失败：无效状态 %s", status_str)
                return {"success": False, "error": f"无效状态: {status_str}"}

            tasks = await task_manager.list_tasks(
                caller_role=_caller_role,
                owner=owner,
                status=status,
                limit=limit,
            )
            logger.info(
                "跨插件 API list 成功：共 %d 个任务（owner=%s、status=%s）",
                len(tasks),
                owner,
                status_str,
            )
            return {"success": True, "tasks": tasks, "count": len(tasks)}
        except Exception as exc:
            logger.exception("跨插件 API list 调用异常：%.80r", str(exc))
            return {"success": False, "error": str(exc)}

    # ── get ─────────────────────────────────────────────────────────────
    async def _get(**kwargs: Any) -> dict[str, Any]:
        """获取单个任务的完整详情。

        期望 kwargs：
            task_id (str): 任务 ID。
            owner (str): 调用方 owner ID，用于权限检查。
        """
        try:
            task_id = str(kwargs.get("task_id", ""))
            owner = str(kwargs.get("owner", ""))

            logger.debug("跨插件 API get 调用：task_id=%s、owner=%s", task_id, owner)
            ok, result = await task_manager.get_task(
                task_id=task_id,
                caller_role=_caller_role,
                owner=owner,
            )
            if ok and isinstance(result, TaskRecord):
                logger.info("跨插件 API get 成功：任务 %s 详情已返回", task_id)
                return {"success": True, "task": result.to_dict()}
            logger.warning("跨插件 API get 失败：任务 %s：%.80r", task_id, str(result))
            return {"success": False, "error": str(result)}
        except Exception as exc:
            logger.exception("跨插件 API get 调用异常：%.80r", str(exc))
            return {"success": False, "error": str(exc)}

    # ── cancel ──────────────────────────────────────────────────────────
    async def _cancel(**kwargs: Any) -> dict[str, Any]:
        """取消任务。

        期望 kwargs：
            task_id (str): 任务 ID。
            owner (str): 调用方 owner ID，用于权限检查。
        """
        try:
            task_id = str(kwargs.get("task_id", ""))
            owner = str(kwargs.get("owner", ""))

            logger.debug("跨插件 API cancel 调用：task_id=%s、owner=%s", task_id, owner)
            ok, msg = await task_manager.cancel_task(
                task_id=task_id,
                caller_role=_caller_role,
                owner=owner,
            )
            if ok:
                logger.info("跨插件 API cancel 成功：任务 %s 已取消", task_id)
            else:
                logger.warning("跨插件 API cancel 失败：任务 %s：%.80r", task_id, str(msg))
            return {"success": ok, "message": msg}
        except Exception as exc:
            logger.exception("跨插件 API cancel 调用异常：%.80r", str(exc))
            return {"success": False, "error": str(exc)}

    # ── inject ──────────────────────────────────────────────────────────
    async def _inject(**kwargs: Any) -> dict[str, Any]:
        """向运行中任务注入指令。

        期望 kwargs：
            task_id (str): 目标任务 ID。
            instruction (str): 要注入的指令文本。
            owner (str): 调用方 owner ID，用于权限检查。
        """
        try:
            task_id = str(kwargs.get("task_id", ""))
            instruction = str(kwargs.get("instruction", ""))
            owner = str(kwargs.get("owner", ""))

            logger.debug(
                "跨插件 API inject 调用：task_id=%s、owner=%s、instruction=%.80r",
                task_id,
                owner,
                instruction,
            )
            ok, msg = await task_manager.modify_task(
                task_id=task_id,
                caller_role=_caller_role,
                owner=owner,
                inject_instruction=instruction,
            )
            if ok:
                logger.info("跨插件 API inject 成功：任务 %s 指令已注入", task_id)
            else:
                logger.warning("跨插件 API inject 失败：任务 %s：%.80r", task_id, str(msg))
            return {"success": ok, "message": msg}
        except Exception as exc:
            logger.exception("跨插件 API inject 调用异常：%.80r", str(exc))
            return {"success": False, "error": str(exc)}

    # ── history ─────────────────────────────────────────────────────────
    async def _history(**kwargs: Any) -> dict[str, Any]:
        """获取任务执行历史。

        期望 kwargs：
            task_id (str): 任务 ID。
            owner (str): 调用方 owner ID，用于权限检查。
            limit (int, 可选): 最大历史条目数（默认 50）。
        """
        try:
            task_id = str(kwargs.get("task_id", ""))
            owner = str(kwargs.get("owner", ""))
            limit: int = _to_int(kwargs.get("limit", 50)) or 50

            logger.debug(
                "跨插件 API history 调用：task_id=%s、owner=%s、limit=%d",
                task_id,
                owner,
                limit,
            )
            ok, result = await task_manager.task_history(
                task_id=task_id,
                caller_role=_caller_role,
                owner=owner,
                limit=limit,
            )
            if ok:
                logger.info(
                    "跨插件 API history 成功：任务 %s 历史已返回（%d 条）",
                    task_id,
                    len(result),
                )
                return {"success": True, "history": result}
            logger.warning("跨插件 API history 失败：任务 %s：%.80r", task_id, str(result))
            return {"success": False, "error": str(result)}
        except Exception as exc:
            logger.exception("跨插件 API history 调用异常：%.80r", str(exc))
            return {"success": False, "error": str(exc)}

    logger.info(
        "构建跨插件 API 处理器完成，共 %d 个端点：create/list/get/cancel/inject/history",
        6,
    )
    return [
        {
            "name": "create",
            "description": "创建任务",
            "version": "1",
            "public": True,
            "handler": _create,
        },
        {
            "name": "list",
            "description": "列出任务摘要",
            "version": "1",
            "public": True,
            "handler": _list,
        },
        {
            "name": "get",
            "description": "查看任务详情",
            "version": "1",
            "public": True,
            "handler": _get,
        },
        {
            "name": "cancel",
            "description": "取消任务",
            "version": "1",
            "public": True,
            "handler": _cancel,
        },
        {
            "name": "inject",
            "description": "向运行中任务注入指令",
            "version": "1",
            "public": True,
            "handler": _inject,
        },
        {
            "name": "history",
            "description": "查看任务执行历史",
            "version": "1",
            "public": True,
            "handler": _history,
        },
    ]
