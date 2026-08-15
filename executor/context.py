"""Shared execution context for the current task."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar

from ..domain.stream_ref import Owner, is_group_stream
from ..domain.task_record import META_CALLER_ROLE, TaskRecord
from ..permission import PermissionResolver, Role

logger = logging.getLogger(__name__)

current_task: ContextVar[TaskRecord | None] = ContextVar(
    "oh_mai_agent_current_task", default=None
)


def make_role_provider(
    resolver: PermissionResolver, task: TaskRecord
) -> Callable[[], Role]:
    """Create a role resolver callback for a task.

    优先使用任务创建时持久化的创建者角色（``task.set_caller_role()``，键
    ``META_CALLER_ROLE``，由 ``TaskCrud.create`` 写入）：planner / API 创建者
    均为 ADMIN，任务以创建者角色执行，保证 MCP（user+）等工具对任务可见。
    无该元数据（历史任务 / 内部即时任务）时回退到按 owner/stream_id 解析。
    """
    caller_role = task.caller_role()
    if caller_role:
        try:
            resolved_caller_role = Role(caller_role)
        except ValueError:
            logger.warning(
                "任务 %s：metadata[%s] 非法 %r，回退 owner 解析",
                task.id, META_CALLER_ROLE, caller_role,
            )
        else:
            return lambda: resolved_caller_role

    is_group = is_group_stream(task.stream_id)
    if is_group:
        # 群聊任务：owner 为 planner:{stream_id}，无单一委托用户。
        # 用占位 user_id，角色判定落到群角色（admin_groups/user_groups）。
        user_id = "planner"
    else:
        # 私聊任务：owner 即委托用户（如 qq:1591625223），提取 user_id。
        user_id = Owner.user_id(task.owner)

    def provider() -> Role:
        return resolver.resolve_role(
            platform=task.platform,
            user_id=user_id,
            stream_id=task.stream_id,
            is_group=is_group,
        )

    return provider
