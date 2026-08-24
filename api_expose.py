"""oh-mai-agent 跨插件 API 处理器。

暴露任务管理 API（创建/列表/获取/取消/注入/历史），
供其它 MaiBot 插件通过 ``ctx.api.call()`` 调用。

信任模型与调用方身份：
  - 跨插件 API 面向**受信任插件**——MaiBot 的插件均为部署者手动安装的代码，
    插件间互信是架构前提。SDK 的 ``api.call`` 通道（Host 侧 ``_cap_api_call``）
    不携带调用方身份，因此**无身份参数的调用按 ADMIN 处理**（历史信任模型）。
  - 调用方可在 kwargs 中显式携带身份参数 ``_caller_role`` / ``_caller_owner`` /
    ``_caller_stream_id``（保留键，见 ``_resolve_caller``），handler 必须
    **校验并使用**该身份而非一律 ADMIN：身份低于 ADMIN 时按身份锁定
    owner / stream_id（不能以他人身份查看/控制任务，create 不能为他人或
    跨流建任务，执行角色即调用者身份、不可自升）。
  - 低权限身份到达本层的路径已封闭：Agent 侧 call_* 工具默认不暴露
    （显式允许名单 + 最低 ADMIN 角色，见 tools/agent/plugin_api_tools.py），
    且本插件自身 6 个端点永远不包装为 call_* 工具——Agent 操作任务一律走
    原生 owner 隔离工具。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .core.task_manager import TaskManager
from .domain.status_formatter import StatusFormatter
from .domain.task_record import TaskRecord, TaskStatus
from .permission import Role

logger = logging.getLogger(__name__)

# ── 调用方身份契约 ──────────────────────────────────────────────────────────
# 身份参数经 kwargs 传入（SDK 通道对 kwargs 原样透传）。键名以单下划线前缀
# 保留，禁止作为普通 API 参数使用；call_* 工具包装会在透传前剥离这些键
# （tools/agent/plugin_api_tools.py），防止经工具通道伪造身份。

CALLER_ROLE_KEY = "_caller_role"          # Role.value 字符串（"guest"/"user"/"admin"）
CALLER_OWNER_KEY = "_caller_owner"        # 调用方自身 owner（如 "qq:10001"）
CALLER_STREAM_KEY = "_caller_stream_id"   # 调用方自身聊天流（如 "qq:10001"）

#: 低权限身份下 create 的参数锁定错误标记（extract → call 内部传递）。
_IDENTITY_ERROR = "_identity_error"

#: 跨插件调用缺省角色：SDK 通道不携带调用方身份，无身份参数的调用视为
#: 受信任插件，按 ADMIN 处理（见模块 docstring 信任模型）。
_DEFAULT_CALLER_ROLE = Role.ADMIN


def _resolve_caller(kwargs: dict[str, Any]) -> tuple[Role, str, str]:
    """解析本次调用的真实调用者身份，返回 ``(role, owner, stream_id)``。

    优先级：
      1. 调用方显式携带的身份参数（``_caller_role`` / ``_caller_owner`` /
         ``_caller_stream_id``）：解析并校验；**非法身份值按 GUEST 处理**
         （fail-closed：宁可拒绝，不可按缺省 ADMIN 放大权限）。
      2. 无身份参数：SDK 跨插件通道不携带调用方身份，视为受信任插件，
         按 ADMIN 处理（历史信任模型，部署前提：只安装可信插件）。

    返回的 owner / stream_id 是调用方**自身归属**；低权限身份下各 handler
    据此锁定操作范围（list/get/cancel/inject/history 强制按自身 owner 过滤，
    create 拒绝为他人 / 跨流建任务）。
    """
    raw_role = kwargs.get(CALLER_ROLE_KEY)
    role: Role | None = None
    if raw_role is not None:
        value = getattr(raw_role, "value", raw_role)
        try:
            role = Role(str(value))
        except ValueError:
            logger.warning(
                "跨插件 API 收到非法调用者身份 %r，按 GUEST 拒绝（fail-closed）",
                raw_role,
            )
            role = Role.GUEST
    if role is None:
        role = _DEFAULT_CALLER_ROLE
    owner = str(kwargs.get(CALLER_OWNER_KEY, "") or "")
    stream_id = str(kwargs.get(CALLER_STREAM_KEY, "") or "")
    return role, owner, stream_id


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


def _task_detail(task: TaskRecord) -> dict[str, Any]:
    """构建跨插件 API 的 task 详情 DTO。

    只暴露稳定、用户可见的字段，**不透传** ``TaskRecord.to_dict()`` 的持久化
    内部结构（``_status_log`` 审计日志、``metadata`` 内部协作键如待注入队列/
    用户回复/协作暂停标记等属于实现细节，不构成对外契约）。领域模型
    后续演进不会破坏跨插件 API 的响应结构。
    """
    sfmt = StatusFormatter()
    status, ts = task.status_info()
    return {
        "id": task.id,
        "title": task.title,
        "intent": task.intent,
        "level": task.level.value,
        "status": task.status.value,
        "format_status": sfmt.format(status, ts),
        "owner": task.owner,
        "stream_id": task.stream_id,
        "platform": task.platform,
        "reply_stream_id": task.reply_stream_id,
        "trigger_type": task.trigger_type.value,
        "priority": task.priority,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
    }


def _wrap_handler(
    name: str,
    *,
    extract: Callable[[dict[str, Any]], dict[str, Any]],
    call: Callable[[dict[str, Any]], Awaitable[tuple[bool, Any]]],
    map_ok: Callable[[Any], dict[str, Any]],
    map_err: Callable[[Any], dict[str, Any]] | None = None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """构建统一结构的 API handler：提取参数 → 调用 → 结果映射 → 异常兜底。

    - 成功：``map_ok(result)``；
    - ``call`` 返回 ``(False, msg)``：用 ``map_err(msg)``
      （缺省 ``{"success": False, "error": str(msg)}``）；
    - 任何异常：``{"success": False, "error": str(exc)}``，不向上抛。

    Args:
        name: 端点名（日志用）。
        extract: ``(kwargs) -> 调用参数字典``，含类型转换、调用者身份解析
            与低权限身份锁定，并调用日志。
        call: ``(args) -> (ok, result)``，调用 TaskManager 方法
            （args 中携带解析后的调用者角色）。
        map_ok: ``(result) -> dict``，成功结果映射。
        map_err: ``(msg) -> dict``，业务失败映射（缺省 error 字段）。
    """

    async def handler(**kwargs: Any) -> dict[str, Any]:
        try:
            args = extract(kwargs)
            ok, result = await call(args)
            if ok:
                logger.info("跨插件 API %s 成功", name)
                return map_ok(result)
            logger.warning("跨插件 API %s 失败：%.80r", name, str(result))
            if map_err is not None:
                return map_err(result)
            return {"success": False, "error": str(result)}
        except Exception as exc:
            logger.exception("跨插件 API %s 调用异常：%.80r", name, str(exc))
            return {"success": False, "error": str(exc)}

    return handler


def build_api_handlers(task_manager: TaskManager) -> list[dict[str, Any]]:
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

    调用者身份：handler 经 ``_resolve_caller`` 解析并校验调用方身份
    （``_caller_role`` 等保留参数）；无身份参数时按受信任插件 ADMIN 处理。

    Args:
        task_manager: TaskManager 实例，用于任务操作。

    Returns:
        API 处理器描述符列表。
    """
    # ── create ──────────────────────────────────────────────────────────
    def _create_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        """创建新任务（不接受 level 参数——跨插件创建固定为 agent 级）。

        低权限身份（非 ADMIN）锁定 owner / stream_id 为调用者自身：
        传入他人 owner / 其他流 stream_id 直接拒绝；执行角色即调用者身份，
        不可自升（create_task 以解析后的角色持久化）。
        """
        intent = str(kwargs.get("intent", ""))
        owner = str(kwargs.get("owner", ""))
        platform = str(kwargs.get("platform", ""))
        stream_id = str(kwargs.get("stream_id", ""))
        delay_seconds: int | None = _to_int(kwargs.get("delay_seconds")) or None
        cron_expr: str | None = kwargs.get("cron_expr")
        priority: int = _to_int(kwargs.get("priority", 0))
        reply_stream_id: str | None = kwargs.get("reply_stream_id")

        caller_role, caller_owner, caller_stream = _resolve_caller(kwargs)
        if caller_role != Role.ADMIN:
            # 低权限身份锁定：不得为他人 / 跨流创建任务。
            if caller_owner and owner and owner != caller_owner:
                logger.warning(
                    "跨插件 API create 被拒：低权限身份 %s 尝试以 owner=%s 建任务"
                    "（自身 owner=%s）", caller_role.value, owner, caller_owner,
                )
                return {_IDENTITY_ERROR: "权限不足：只能以本人身份创建任务"}
            if caller_stream and stream_id and stream_id != caller_stream:
                logger.warning(
                    "跨插件 API create 被拒：低权限身份 %s 尝试跨流建任务"
                    "（stream=%s，自身流=%s）", caller_role.value, stream_id, caller_stream,
                )
                return {_IDENTITY_ERROR: "权限不足：只能在本会话流创建任务"}
            # 身份为权威：缺省或冲突时一律以调用者自身归属为准。
            if caller_owner:
                owner = caller_owner
            if caller_stream:
                stream_id = caller_stream

        logger.debug(
            "跨插件 API create 调用：caller_role=%s、owner=%s、platform=%s、"
            "stream_id=%s、intent=%.80r",
            caller_role.value, owner, platform, stream_id, intent,
        )
        return {
            "intent": intent, "owner": owner, "platform": platform,
            "stream_id": stream_id, "delay_seconds": delay_seconds,
            "cron_expr": cron_expr, "priority": priority,
            "reply_stream_id": reply_stream_id,
            "_caller_role": caller_role,
        }

    async def _create_call(args: dict[str, Any]) -> tuple[bool, Any]:
        identity_error = args.pop(_IDENTITY_ERROR, None)
        if identity_error:
            return False, identity_error
        caller_role: Role = args.pop("_caller_role", _DEFAULT_CALLER_ROLE)
        return await task_manager.create_task(
            intent=args["intent"], owner=args["owner"], platform=args["platform"],
            stream_id=args["stream_id"], delay_seconds=args["delay_seconds"],
            cron_expr=args["cron_expr"], priority=args["priority"],
            reply_stream_id=args["reply_stream_id"], caller_role=caller_role,
        )

    def _create_map_ok(result: TaskRecord) -> dict[str, Any]:
        return {"success": True, "task_id": result.id, "title": result.title,
                "level": result.level.value}

    # ── list ────────────────────────────────────────────────────────────
    def _list_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        caller_role, caller_owner, _ = _resolve_caller(kwargs)
        owner = str(kwargs.get("owner", ""))
        if caller_role != Role.ADMIN:
            # 低权限身份：只能查看自己名下的任务（身份为权威，忽略调用方 owner）。
            owner = caller_owner
        status_str: str | None = kwargs.get("status")
        limit: int = _to_int(kwargs.get("limit", 50)) or 50
        logger.debug(
            "跨插件 API list 调用：caller_role=%s、owner=%s、status=%s、limit=%d",
            caller_role.value, owner, status_str, limit,
        )
        return {
            "owner": owner, "status_str": status_str, "limit": limit,
            "_caller_role": caller_role,
        }

    async def _list_call(args: dict[str, Any]) -> tuple[bool, Any]:
        status_str = args["status_str"]
        status = _parse_status(status_str)
        if status_str and status is None:
            return False, f"无效状态: {status_str}"
        caller_role: Role = args.pop("_caller_role", _DEFAULT_CALLER_ROLE)
        return True, await task_manager.list_tasks(
            caller_role=caller_role, owner=args["owner"], status=status,
            limit=args["limit"],
        )

    def _list_map_ok(result: list) -> dict[str, Any]:
        return {"success": True, "tasks": result, "count": len(result)}

    # ── get ─────────────────────────────────────────────────────────────
    def _get_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        caller_role, caller_owner, _ = _resolve_caller(kwargs)
        task_id = str(kwargs.get("task_id", ""))
        owner = str(kwargs.get("owner", ""))
        if caller_role != Role.ADMIN:
            owner = caller_owner  # 低权限身份：只能查看自己的任务
        logger.debug(
            "跨插件 API get 调用：caller_role=%s、task_id=%s、owner=%s",
            caller_role.value, task_id, owner,
        )
        return {"task_id": task_id, "owner": owner, "_caller_role": caller_role}

    async def _get_call(args: dict[str, Any]) -> tuple[bool, Any]:
        caller_role: Role = args.pop("_caller_role", _DEFAULT_CALLER_ROLE)
        return await task_manager.get_task(
            task_id=args["task_id"], caller_role=caller_role, owner=args["owner"],
        )

    def _get_map_ok(result: TaskRecord) -> dict[str, Any]:
        return {"success": True, "task": _task_detail(result)}

    # ── cancel ──────────────────────────────────────────────────────────
    def _cancel_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        caller_role, caller_owner, _ = _resolve_caller(kwargs)
        task_id = str(kwargs.get("task_id", ""))
        owner = str(kwargs.get("owner", ""))
        if caller_role != Role.ADMIN:
            owner = caller_owner  # 低权限身份：只能取消自己的任务
        logger.debug(
            "跨插件 API cancel 调用：caller_role=%s、task_id=%s、owner=%s",
            caller_role.value, task_id, owner,
        )
        return {"task_id": task_id, "owner": owner, "_caller_role": caller_role}

    async def _cancel_call(args: dict[str, Any]) -> tuple[bool, Any]:
        caller_role: Role = args.pop("_caller_role", _DEFAULT_CALLER_ROLE)
        return await task_manager.cancel_task(
            task_id=args["task_id"], caller_role=caller_role, owner=args["owner"],
        )

    def _cancel_map_ok(result: str) -> dict[str, Any]:
        return {"success": True, "message": result}

    # ── inject ──────────────────────────────────────────────────────────
    def _inject_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        caller_role, caller_owner, _ = _resolve_caller(kwargs)
        task_id = str(kwargs.get("task_id", ""))
        instruction = str(kwargs.get("instruction", ""))
        owner = str(kwargs.get("owner", ""))
        if caller_role != Role.ADMIN:
            owner = caller_owner  # 低权限身份：只能向自己的任务注入指令
        logger.debug(
            "跨插件 API inject 调用：caller_role=%s、task_id=%s、owner=%s、"
            "instruction=%.80r",
            caller_role.value, task_id, owner, instruction,
        )
        return {
            "task_id": task_id, "owner": owner, "instruction": instruction,
            "_caller_role": caller_role,
        }

    async def _inject_call(args: dict[str, Any]) -> tuple[bool, Any]:
        caller_role: Role = args.pop("_caller_role", _DEFAULT_CALLER_ROLE)
        return await task_manager.modify_task(
            task_id=args["task_id"], caller_role=caller_role, owner=args["owner"],
            inject_instruction=args["instruction"],
        )

    def _inject_map_ok(result: str) -> dict[str, Any]:
        return {"success": True, "message": result}

    # ── history ─────────────────────────────────────────────────────────
    def _history_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        caller_role, caller_owner, _ = _resolve_caller(kwargs)
        task_id = str(kwargs.get("task_id", ""))
        owner = str(kwargs.get("owner", ""))
        if caller_role != Role.ADMIN:
            owner = caller_owner  # 低权限身份：只能查看自己任务的历史
        limit: int = _to_int(kwargs.get("limit", 50)) or 50
        logger.debug(
            "跨插件 API history 调用：caller_role=%s、task_id=%s、owner=%s、limit=%d",
            caller_role.value, task_id, owner, limit,
        )
        return {
            "task_id": task_id, "owner": owner, "limit": limit,
            "_caller_role": caller_role,
        }

    async def _history_call(args: dict[str, Any]) -> tuple[bool, Any]:
        caller_role: Role = args.pop("_caller_role", _DEFAULT_CALLER_ROLE)
        return await task_manager.task_history(
            task_id=args["task_id"], caller_role=caller_role, owner=args["owner"],
            limit=args["limit"],
        )

    def _history_map_ok(result: list) -> dict[str, Any]:
        return {"success": True, "history": result}

    # ── 端点描述表 ─────────────────────────────────────────────────────
    # cancel/inject 的业务失败沿用 TaskManager 的 (bool, str) 语义，落在 message 字段
    _message_err: Callable[[Any], dict[str, Any]] = (
        lambda msg: {"success": False, "message": str(msg)}
    )
    endpoints = [
        {
            "name": "create",
            "description": "创建任务",
            "extract": _create_extract,
            "call": _create_call,
            "map_ok": _create_map_ok,
        },
        {
            "name": "list",
            "description": "列出任务摘要",
            "extract": _list_extract,
            "call": _list_call,
            "map_ok": _list_map_ok,
        },
        {
            "name": "get",
            "description": "查看任务详情",
            "extract": _get_extract,
            "call": _get_call,
            "map_ok": _get_map_ok,
        },
        {
            "name": "cancel",
            "description": "取消任务",
            "extract": _cancel_extract,
            "call": _cancel_call,
            "map_ok": _cancel_map_ok,
            "map_err": _message_err,
        },
        {
            "name": "inject",
            "description": "向运行中任务注入指令",
            "extract": _inject_extract,
            "call": _inject_call,
            "map_ok": _inject_map_ok,
            "map_err": _message_err,
        },
        {
            "name": "history",
            "description": "查看任务执行历史",
            "extract": _history_extract,
            "call": _history_call,
            "map_ok": _history_map_ok,
        },
    ]

    logger.info(
        "构建跨插件 API 处理器完成，共 %d 个端点：%s",
        len(endpoints),
        "/".join(e["name"] for e in endpoints),
    )
    return [
        {
            "name": e["name"],
            "description": e["description"],
            "version": "1",
            "public": True,
            "handler": _wrap_handler(
                e["name"],
                extract=e["extract"],
                call=e["call"],
                map_ok=e["map_ok"],
                map_err=e.get("map_err"),
            ),
        }
        for e in endpoints
    ]
