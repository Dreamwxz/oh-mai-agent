"""Shared execution context for the current task."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

from ..domain.task_record import TaskRecord
from ..permission import PermissionResolver, Role

current_task: ContextVar[TaskRecord | None] = ContextVar(
    "oh_mai_agent_current_task", default=None
)

current_cancel_check: ContextVar[Callable[[], bool] | None] = ContextVar(
    "oh_mai_agent_current_cancel_check", default=None
)


def make_role_provider(
    resolver: PermissionResolver, task: TaskRecord
) -> Callable[[], Role]:
    """Create a role resolver callback for a task."""
    is_group = ":group:" in task.stream_id
    if is_group:
        # 群聊任务：owner 为 planner:{stream_id}，无单一委托用户。
        # 用占位 user_id，角色判定落到群角色（admin_groups/user_groups）。
        user_id = "planner"
    else:
        # 私聊任务：owner 即委托用户（如 qq:1591625223），提取 user_id。
        user_id = task.owner.split(":", 1)[1] if ":" in task.owner else task.owner

    def provider() -> Role:
        return resolver.resolve_role(
            platform=task.platform,
            user_id=user_id,
            stream_id=task.stream_id,
            is_group=is_group,
        )

    return provider
