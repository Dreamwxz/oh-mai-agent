"""Agent 循环的任务管理工具通道模块。

提供 ``build_task_mgmt_tools`` 工厂函数，从 ``core/task_manager.py:_build_task_mgmt_tools``
提取而来，供 TaskManager 委托调用。

包含三个 discoverable（min_role=USER）工具：
- list_my_tasks: 列出当前用户所有任务
- create_subtask: 创建子任务（并行拆分）
- inject_task: 向其他任务注入指令（运行时干预）
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..registry import ToolDefinition
from ...permission import PermissionResolver, Role
from ...domain.task_record import TaskLevel, TaskRecord

logger = logging.getLogger(__name__)


def build_task_mgmt_tools(
    store,
    sfmt,
    *,
    create_task,
    handle_injection,
    get_current_task,
    get_current_task_role,
) -> list[ToolDefinition]:
    """构建 Agent 循环的任务管理工具定义列表。

    Args:
        store: TaskStore 实例，用于任务查询。
        sfmt: StatusFormatter 实例，用于格式化任务状态。
        create_task: 异步可调用，签名 ``(intent, owner, platform, stream_id, level, caller_role) -> (bool, TaskRecord | str)``。
        handle_injection: 异步可调用，签名 ``(task_id: str, instruction: str) -> bool``。
        get_current_task: 同步可调用，返回 ``TaskRecord | None``，从上下文变量获取当前任务。
        get_current_task_role: 同步可调用，返回 ``Role``，解析当前任务的角色。

    Returns:
        三个 ``ToolDefinition`` 实例，全部为 discoverable 级别、USER 可访问。
    """

    # ── list_my_tasks：列出当前用户的任务 ───────────────────────
    async def _list_my_tasks_handler(**kwargs: Any) -> dict:
        task = get_current_task()
        if task is None:
            logger.warning("list_my_tasks 调用失败：无当前任务上下文")
            return {"success": False, "error": "无当前任务上下文"}
        logger.debug("list_my_tasks 调用：当前任务 %s，属主 %s", task.id, task.owner)
        # 仅能查看当前属主（owner）名下的任务，最多取 100 条
        tasks = await store.list(owner=task.owner, limit=100)
        summaries = [
            {
                "id": t.id,
                "title": t.title,
                "level": t.level.value,
                "status": t.status.value,
                "format_status": sfmt.format(*t.status_info()),
                "created_at": t.created_at.isoformat(),
            }
            for t in tasks
        ]
        logger.info("list_my_tasks 完成：属主 %s 共 %d 个任务", task.owner, len(summaries))
        return {"success": True, "tasks": summaries, "count": len(summaries)}

    # ── create_subtask：创建并行子任务 ──────────────────────────
    async def _create_subtask_handler(
        intent: str = "",
        level: str = "agent",
        **kwargs: Any,
    ) -> dict:
        task = get_current_task()
        if task is None:
            logger.warning("create_subtask 调用失败：无当前任务上下文")
            return {"success": False, "error": "无当前任务上下文"}
        logger.debug(
            "create_subtask 调用：当前任务 %s，意图 %.80r，级别 %s",
            task.id,
            intent,
            level,
        )
        if not intent:
            logger.warning("create_subtask 参数校验失败：缺少意图 intent")
            return {"success": False, "error": "缺少必需参数: intent"}

        try:
            task_level = TaskLevel(level)
        except ValueError:
            logger.warning(
                "create_subtask 参数校验失败：无效级别 %s，合法值 instant/agent", level
            )
            return {"success": False, "error": f"无效级别: {level}，合法值: instant/agent"}

        # 子任务沿用当前任务的属主/平台/流，回复仍回到原会话；
        # 固定按 USER 权限调用（Agent 循环内不越权创建他人任务）
        ok, result = await create_task(
            intent=intent,
            owner=task.owner,
            platform=task.platform,
            stream_id=task.stream_id,
            level=task_level,
            caller_role=Role.USER,
        )
        if ok and isinstance(result, TaskRecord):
            logger.info(
                "create_subtask 成功：子任务 %s（%s 级）", result.id, result.level.value
            )
            return {
                "success": True,
                "task_id": result.id,
                "title": result.title,
                "level": result.level.value,
            }
        logger.warning("create_subtask 失败：%.80r", str(result))
        return {"success": False, "error": str(result)}

    # ── inject_task：向任务注入指令 ─────────────────────────────
    async def _inject_task_handler(
        task_id: str = "",
        instruction: str = "",
        **kwargs: Any,
    ) -> dict:
        current = get_current_task()
        if current is None:
            logger.warning("inject_task 调用失败：无当前任务上下文")
            return {"success": False, "error": "无当前任务上下文"}
        logger.debug(
            "inject_task 调用：当前任务 %s，目标任务 %s，指令 %.80r",
            current.id,
            task_id,
            instruction,
        )
        if not task_id or not instruction:
            logger.warning(
                "inject_task 参数校验失败：缺少必需参数 task_id/instruction"
            )
            return {"success": False, "error": "缺少必需参数: task_id, instruction"}

        target = await store.get(task_id)
        if target is None:
            logger.warning("inject_task 失败：目标任务不存在 %s", task_id)
            return {"success": False, "error": f"目标任务不存在: {task_id}"}

        # 权限：当前任务 owner 必须匹配目标 owner，或当前 role >= ADMIN
        role = get_current_task_role()
        if current.owner != target.owner and not PermissionResolver.require(role, Role.ADMIN):
            logger.info(
                "inject_task 权限不足：任务 %s 无法向 %s 注入指令",
                current.id,
                task_id,
            )
            return {"success": False, "error": "权限不足：只能向自己的任务注入指令"}

        ok = await handle_injection(task_id, instruction)
        if ok:
            logger.info("inject_task 成功：指令已注入任务 %s", task_id)
        else:
            logger.warning("inject_task 失败：任务 %s 未在运行", task_id)
        return {
            "success": ok,
            "message": f"指令已注入任务 {task_id[:8]}..." if ok
            else f"注入失败（任务 {task_id[:8]} 未在运行）",
        }

    logger.debug("构建任务管理工具定义：list_my_tasks / create_subtask / inject_task")
    return [
        ToolDefinition(
            name="list_my_tasks",
            description=(
                "列出当前用户的所有任务（包括活跃和已完成）。"
                "用于 Agent 了解同属主下的任务全景，便于并行拆分和状态感知。"
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=_list_my_tasks_handler,
            visibility="discoverable",
            min_role=Role.USER,
        ),
        ToolDefinition(
            name="create_subtask",
            description=(
                "创建子任务（并行拆分）。在当前流、当前属主下新建任务。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "子任务的意图描述",
                    },
                    "level": {
                        "type": "string",
                        "enum": ["instant", "agent"],
                        "description": "执行级别",
                        "default": "agent",
                    },
                },
                "required": ["intent"],
            },
            handler=_create_subtask_handler,
            visibility="discoverable",
            min_role=Role.USER,
        ),
        ToolDefinition(
            name="inject_task",
            description=(
                "向其他任务注入指令（运行时干预）。仅可向自己的任务注入，"
                "且目标状态必须为 running 或 waiting_input。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "目标任务 ID",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "要注入的指令文本",
                    },
                },
                "required": ["task_id", "instruction"],
            },
            handler=_inject_task_handler,
            visibility="discoverable",
            min_role=Role.USER,
        ),
    ]
