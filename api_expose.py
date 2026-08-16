"""oh-mai-agent 跨插件 API 处理器。

暴露任务管理 API（创建/列表/获取/取消/注入/历史），
供其它 MaiBot 插件通过 ``ctx.api.call()`` 调用。

信任模型：跨插件 API 面向**受信任插件**——MaiBot 的插件均为部署者手动安装的
代码，插件间互信是架构前提，故所有 handler 统一以 ``_CALLER_ROLE = Role.ADMIN``
调用 TaskManager，不做面向用户的 owner 权限校验（config 不再声明任何暴露等级
配置，历史 ``max_level`` 字段已废弃，见 docs/features/04-permission.md）。

已知边界：经 ``ctx.api.list()`` 包装出的 Agent 侧 ``call_*`` 工具
（min_role=USER）同样可触达这些端点并以 ADMIN 执行——user 级 Agent 可列出
全部任务并取消/暂停/注入任意任务，owner 隔离被绕过。这是信任模型继承的
结果，详见 docs/features/04-permission.md 与 10-cross-plugin-api.md。
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

# 跨插件 API 调用视为可信：处理器统一以 ADMIN 角色调用 TaskManager，
# 避免面向用户的内部权限校验阻碍合法的跨插件操作。
_CALLER_ROLE = Role.ADMIN


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
        extract: ``(kwargs) -> 调用参数字典``，含类型转换与调用日志。
        call: ``(args) -> (ok, result)``，调用 TaskManager 方法。
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
    所有异常路径落在 ``error`` 字段。跨插件 API 调用内部按 ADMIN 级别处理。

    Args:
        task_manager: TaskManager 实例，用于任务操作。

    Returns:
        API 处理器描述符列表。
    """
    # ── create ──────────────────────────────────────────────────────────
    def _create_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        """创建新任务（不接受 level 参数——跨插件创建固定为 agent 级）。"""
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
            owner, platform, stream_id, intent,
        )
        return {
            "intent": intent, "owner": owner, "platform": platform,
            "stream_id": stream_id, "delay_seconds": delay_seconds,
            "cron_expr": cron_expr, "priority": priority,
            "reply_stream_id": reply_stream_id,
        }

    async def _create_call(args: dict[str, Any]) -> tuple[bool, Any]:
        return await task_manager.create_task(
            intent=args["intent"], owner=args["owner"], platform=args["platform"],
            stream_id=args["stream_id"], delay_seconds=args["delay_seconds"],
            cron_expr=args["cron_expr"], priority=args["priority"],
            reply_stream_id=args["reply_stream_id"], caller_role=_CALLER_ROLE,
        )

    def _create_map_ok(result: TaskRecord) -> dict[str, Any]:
        return {"success": True, "task_id": result.id, "title": result.title,
                "level": result.level.value}

    # ── list ────────────────────────────────────────────────────────────
    def _list_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        owner = str(kwargs.get("owner", ""))
        status_str: str | None = kwargs.get("status")
        limit: int = _to_int(kwargs.get("limit", 50)) or 50
        logger.debug(
            "跨插件 API list 调用：owner=%s、status=%s、limit=%d",
            owner, status_str, limit,
        )
        return {"owner": owner, "status_str": status_str, "limit": limit}

    async def _list_call(args: dict[str, Any]) -> tuple[bool, Any]:
        status_str = args["status_str"]
        status = _parse_status(status_str)
        if status_str and status is None:
            return False, f"无效状态: {status_str}"
        return True, await task_manager.list_tasks(
            caller_role=_CALLER_ROLE, owner=args["owner"], status=status,
            limit=args["limit"],
        )

    def _list_map_ok(result: list) -> dict[str, Any]:
        return {"success": True, "tasks": result, "count": len(result)}

    # ── get ─────────────────────────────────────────────────────────────
    def _get_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        owner = str(kwargs.get("owner", ""))
        logger.debug("跨插件 API get 调用：task_id=%s、owner=%s", task_id, owner)
        return {"task_id": task_id, "owner": owner}

    async def _get_call(args: dict[str, Any]) -> tuple[bool, Any]:
        return await task_manager.get_task(
            task_id=args["task_id"], caller_role=_CALLER_ROLE, owner=args["owner"],
        )

    def _get_map_ok(result: TaskRecord) -> dict[str, Any]:
        return {"success": True, "task": _task_detail(result)}

    # ── cancel ──────────────────────────────────────────────────────────
    def _cancel_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        owner = str(kwargs.get("owner", ""))
        logger.debug("跨插件 API cancel 调用：task_id=%s、owner=%s", task_id, owner)
        return {"task_id": task_id, "owner": owner}

    async def _cancel_call(args: dict[str, Any]) -> tuple[bool, Any]:
        return await task_manager.cancel_task(
            task_id=args["task_id"], caller_role=_CALLER_ROLE, owner=args["owner"],
        )

    def _cancel_map_ok(result: str) -> dict[str, Any]:
        return {"success": True, "message": result}

    # ── inject ──────────────────────────────────────────────────────────
    def _inject_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        instruction = str(kwargs.get("instruction", ""))
        owner = str(kwargs.get("owner", ""))
        logger.debug(
            "跨插件 API inject 调用：task_id=%s、owner=%s、instruction=%.80r",
            task_id, owner, instruction,
        )
        return {"task_id": task_id, "owner": owner, "instruction": instruction}

    async def _inject_call(args: dict[str, Any]) -> tuple[bool, Any]:
        return await task_manager.modify_task(
            task_id=args["task_id"], caller_role=_CALLER_ROLE, owner=args["owner"],
            inject_instruction=args["instruction"],
        )

    def _inject_map_ok(result: str) -> dict[str, Any]:
        return {"success": True, "message": result}

    # ── history ─────────────────────────────────────────────────────────
    def _history_extract(kwargs: dict[str, Any]) -> dict[str, Any]:
        task_id = str(kwargs.get("task_id", ""))
        owner = str(kwargs.get("owner", ""))
        limit: int = _to_int(kwargs.get("limit", 50)) or 50
        logger.debug(
            "跨插件 API history 调用：task_id=%s、owner=%s、limit=%d",
            task_id, owner, limit,
        )
        return {"task_id": task_id, "owner": owner, "limit": limit}

    async def _history_call(args: dict[str, Any]) -> tuple[bool, Any]:
        return await task_manager.task_history(
            task_id=args["task_id"], caller_role=_CALLER_ROLE, owner=args["owner"],
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
