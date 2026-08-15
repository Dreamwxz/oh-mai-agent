"""Planner tool: task_* handler 工厂。

实现体从 ``plugin.py`` 的 7 个 ``_tool_task_*`` @Tool handler 中抽离；
plugin.py 现仅保留 @Tool 声明并委托本模块的 handler（见其 ``_get_planner_tool``）。
同时定义 ``_planner_owner`` / ``_planner_caller_role`` 两个辅助函数。
"""

from __future__ import annotations

import logging

from typing import Any, Callable

from ...domain.stream_ref import planner_owner, platform_of
from ...domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType
from ...permission import Role

logger = logging.getLogger(__name__)


def _planner_owner(stream_id: str) -> str:
    """Planner 调用的 owner 标识（见 ``domain.stream_ref.planner_owner``）。"""
    return planner_owner(stream_id)


def _planner_caller_role() -> Role:
    """Planner 调用统一视为 ADMIN（Planner 是 bot 的一部分）。"""
    return Role.ADMIN


def build_task_tools(task_manager: Any) -> dict[str, Callable[..., Any]]:
    """返回 7 个 task_* handler 字典。

    键名：task_create / task_list / task_status / task_modify / task_delete /
          task_history / task_schedule。
    """

    # ── task_create ────────────────────────────────────────────────────────

    async def _task_create(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：创建新任务。"""
        try:
            intent = str(kwargs.get("intent", ""))
            stream_id = str(kwargs.get("stream_id", ""))
            level_str: str | None = kwargs.get("level")
            delay_seconds: int | None = kwargs.get("delay_seconds")
            cron_expr: str | None = kwargs.get("cron_expr")
            # 优先级：空值/缺省统一按 0 处理（兼容字符串形式的数值）
            priority: int = int(kwargs.get("priority", 0)) if kwargs.get("priority") else 0
            reply_stream_id: str | None = kwargs.get("reply_stream_id")

            logger.debug(
                "Planner 调用 task_create：stream_id=%s, level=%r, delay=%r, cron=%r, intent=%.80r",
                stream_id, level_str, delay_seconds, cron_expr, intent,
            )

            # 解析 level
            level: TaskLevel | None = None
            if level_str:
                try:
                    level = TaskLevel(level_str)
                except ValueError:
                    logger.warning("task_create 参数校验失败：无效级别 %r", level_str)
                    return {"success": False, "error": f"无效级别: {level_str}，合法值: instant/agent"}

            # 确定触发方式
            trigger: TriggerType = TriggerType.NOW
            if cron_expr:
                trigger = TriggerType.CRON
            elif delay_seconds:
                trigger = TriggerType.DELAY

            # 提取平台标识：stream_id 形如 "platform:user_id"，冒号前段即平台名
            ok, result = await task_manager.create_task(
                intent=intent,
                owner=_planner_owner(stream_id),
                platform=platform_of(stream_id),
                stream_id=stream_id,
                level=level,
                trigger=trigger,
                delay_seconds=delay_seconds,
                cron_expr=cron_expr,
                priority=priority,
                reply_stream_id=reply_stream_id,
                caller_role=_planner_caller_role(),
            )
            if ok and isinstance(result, TaskRecord):
                logger.info("task_create 成功：任务 %s 已创建（level=%s）", result.id, result.level.value)
                return {
                    "success": True,
                    "task_id": result.id,
                    "title": result.title,
                    "level": result.level.value,
                    "status": result.status.value,
                }
            logger.warning("task_create 失败：%r", str(result)[:80])
            return {"success": False, "error": str(result)}
        except Exception as exc:
            logger.exception("task_create 调用异常")
            return {"success": False, "error": str(exc)}

    # ── task_list ───────────────────────────────────────────────────────────

    async def _task_list(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：列出任务摘要。"""
        try:
            stream_id = str(kwargs.get("stream_id", ""))
            status_str: str | None = kwargs.get("status")

            status: TaskStatus | None = None
            if status_str:
                try:
                    status = TaskStatus(status_str)
                except ValueError:
                    logger.warning("task_list 参数校验失败：无效状态 %r", status_str)
                    return {"success": False, "error": f"无效状态: {status_str}，可选: pending/running/waiting_input/completed/failed/cancelled/scheduled/paused"}

            logger.debug("Planner 调用 task_list：stream_id=%s, status=%r", stream_id, status_str)

            tasks = await task_manager.list_tasks(
                caller_role=_planner_caller_role(),
                owner="",  # ADMIN 且 owner 为空 = 查看全部
                status=status,
                stream_id=stream_id,
                limit=50,
            )
            # 格式化输出文本（ID 截取前 8 位便于阅读）
            if not tasks:
                return {"success": True, "tasks": [], "text": "当前没有匹配的任务。", "count": 0}
            lines: list[str] = []
            for t in tasks:
                lines.append(
                    f"[{t['id'][:8]}] {t['level']}/{t['status']} {t['title']}"
                    f" — {t['format_status']}"
                )
            logger.info("task_list 成功：返回 %d 条任务", len(tasks))
            return {
                "success": True,
                "tasks": tasks,
                "text": "\n".join(lines),
                "count": len(tasks),
            }
        except Exception as exc:
            logger.exception("task_list 调用异常")
            return {"success": False, "error": str(exc)}

    # ── task_status ─────────────────────────────────────────────────────────

    async def _task_status(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：查看任务详情。"""
        try:
            task_id = str(kwargs.get("task_id", ""))
            stream_id = str(kwargs.get("stream_id", ""))

            logger.debug("Planner 调用 task_status：task_id=%s, stream_id=%s", task_id, stream_id)

            ok, result = await task_manager.get_task(
                task_id=task_id,
                caller_role=_planner_caller_role(),
                owner=_planner_owner(stream_id),
            )
            if ok and isinstance(result, TaskRecord):
                logger.info("task_status 成功：任务 %s 详情已返回", task_id)
                return {
                    "success": True,
                    "task": result.to_dict(),
                }
            logger.warning("task_status 失败：%r", str(result)[:80])
            return {"success": False, "error": str(result)}
        except Exception as exc:
            logger.exception("task_status 调用异常")
            return {"success": False, "error": str(exc)}

    # ── task_modify ─────────────────────────────────────────────────────────

    async def _task_modify(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：注入指令到运行中任务。"""
        try:
            task_id = str(kwargs.get("task_id", ""))
            instruction = str(kwargs.get("inject_instruction", ""))
            stream_id = str(kwargs.get("stream_id", ""))

            logger.debug(
                "Planner 调用 task_modify：task_id=%s, stream_id=%s, instruction=%.80r",
                task_id, stream_id, instruction,
            )

            ok, msg = await task_manager.modify_task(
                task_id=task_id,
                caller_role=_planner_caller_role(),
                owner=_planner_owner(stream_id),
                inject_instruction=instruction,
            )
            if ok:
                logger.info("task_modify 成功：任务 %s 已注入指令", task_id)
            else:
                logger.warning("task_modify 失败：%r", str(msg)[:80])
            return {"success": ok, "message": msg}
        except Exception as exc:
            logger.exception("task_modify 调用异常")
            return {"success": False, "error": str(exc)}

    # ── task_delete ─────────────────────────────────────────────────────────

    async def _task_delete(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：取消任务。"""
        try:
            task_id = str(kwargs.get("task_id", ""))
            stream_id = str(kwargs.get("stream_id", ""))

            logger.debug("Planner 调用 task_delete：task_id=%s, stream_id=%s", task_id, stream_id)

            ok, msg = await task_manager.cancel_task(
                task_id=task_id,
                caller_role=_planner_caller_role(),
                owner=_planner_owner(stream_id),
            )
            if ok:
                logger.info("task_delete 成功：任务 %s 已取消", task_id)
            else:
                logger.warning("task_delete 失败：%r", str(msg)[:80])
            return {"success": ok, "message": msg}
        except Exception as exc:
            logger.exception("task_delete 调用异常")
            return {"success": False, "error": str(exc)}

    # ── task_history ────────────────────────────────────────────────────────

    async def _task_history(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：查看任务历史。"""
        try:
            task_id = str(kwargs.get("task_id", ""))
            stream_id = str(kwargs.get("stream_id", ""))

            logger.debug("Planner 调用 task_history：task_id=%s, stream_id=%s", task_id, stream_id)

            ok, result = await task_manager.task_history(
                task_id=task_id,
                caller_role=_planner_caller_role(),
                owner=_planner_owner(stream_id),
                limit=50,
            )
            if ok:
                logger.info("task_history 成功：任务 %s 历史返回 %d 条", task_id, len(result))
                return {"success": True, "history": result, "count": len(result)}
            logger.warning("task_history 失败：%r", str(result)[:80])
            return {"success": False, "error": str(result)}
        except Exception as exc:
            logger.exception("task_history 调用异常")
            return {"success": False, "error": str(exc)}

    # ── task_schedule ───────────────────────────────────────────────────────

    async def _task_schedule(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：创建定时任务。"""
        try:
            intent = str(kwargs.get("intent", ""))
            stream_id = str(kwargs.get("stream_id", ""))
            cron_expr = str(kwargs.get("cron_expr", ""))
            level_str: str | None = kwargs.get("level")

            logger.debug(
                "Planner 调用 task_schedule：stream_id=%s, cron=%r, level=%r, intent=%.80r",
                stream_id, cron_expr, level_str, intent,
            )

            if not cron_expr:
                logger.warning("task_schedule 参数校验失败：缺少必填参数 cron_expr")
                return {"success": False, "error": "cron_expr 为必填参数"}

            level: TaskLevel | None = None
            if level_str:
                try:
                    level = TaskLevel(level_str)
                except ValueError:
                    logger.warning("task_schedule 参数校验失败：无效级别 %r", level_str)
                    return {"success": False, "error": f"无效级别: {level_str}"}

            # 提取平台标识：stream_id 形如 "platform:user_id"，冒号前段即平台名
            ok, result = await task_manager.create_task(
                intent=intent,
                owner=_planner_owner(stream_id),
                platform=platform_of(stream_id),
                stream_id=stream_id,
                level=level,
                trigger=TriggerType.CRON,
                cron_expr=cron_expr,
                caller_role=_planner_caller_role(),
            )
            if ok and isinstance(result, TaskRecord):
                logger.info("task_schedule 成功：定时任务 %s 已创建", result.id)
                return {
                    "success": True,
                    "task_id": result.id,
                    "title": result.title,
                    "level": result.level.value,
                    "cron_expr": cron_expr,
                }
            logger.warning("task_schedule 失败：%r", str(result)[:80])
            return {"success": False, "error": str(result)}
        except Exception as exc:
            logger.exception("task_schedule 调用异常")
            return {"success": False, "error": str(exc)}

    return {
        "task_create": _task_create,
        "task_list": _task_list,
        "task_status": _task_status,
        "task_modify": _task_modify,
        "task_delete": _task_delete,
        "task_history": _task_history,
        "task_schedule": _task_schedule,
    }
