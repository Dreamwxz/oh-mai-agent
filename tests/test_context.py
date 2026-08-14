"""Tests for execution context utilities: current_task, make_role_provider."""

from conftest import make_task

from oh_mai_agent.config import MaibotAgentConfig, PermissionConfig
from oh_mai_agent.domain.task_record import TaskLevel, TaskStatus, TriggerType
from oh_mai_agent.executor.context import current_task, make_role_provider
from oh_mai_agent.core.task_manager import current_task as task_manager_current_task
from oh_mai_agent.executor.agent import current_task as agent_current_task
from oh_mai_agent.permission import PermissionResolver, Role


def test_current_task_is_shared_across_task_manager_and_agent_executor() -> None:
    assert current_task is task_manager_current_task is agent_current_task


class TestMakeRoleProvider:
    """make_role_provider 解析测试：私聊/群聊流角色正确。"""

    def test_private_stream_admin_resolved(self) -> None:
        """私聊流 planner 任务：owner=qq:1591625223，用户为 ADMIN → 解析为 ADMIN。"""
        config = MaibotAgentConfig(permission=PermissionConfig(admins=["qq:1591625223"]))
        resolver = PermissionResolver(config.permission)
        task = make_task(
            "t1", owner="qq:1591625223",
            stream_id="qq:1591625223", platform="qq",
        )
        provider = make_role_provider(resolver, task)
        assert provider() == Role.ADMIN

    def test_group_stream_admin_by_group(self) -> None:
        """群聊流 planner 任务：owner=planner:qq:group:xxx，群在 admin_groups → 解析为 ADMIN。"""
        config = MaibotAgentConfig(permission=PermissionConfig(admin_groups=["qq:group:123456"]))
        resolver = PermissionResolver(config.permission)
        task = make_task(
            "t1", owner="planner:qq:group:123456",
            stream_id="qq:group:123456", platform="qq",
        )
        provider = make_role_provider(resolver, task)
        # 命中群管理角色，不依赖 person_key
        assert provider() == Role.ADMIN

    def test_group_stream_not_in_group_is_guest(self) -> None:
        """群聊流 planner 任务：群不在任何授权组 → GUEST。"""
        config = MaibotAgentConfig(permission=PermissionConfig(admin_groups=["qq:group:other"]))
        resolver = PermissionResolver(config.permission)
        task = make_task(
            "t1", owner="planner:qq:group:123456",
            stream_id="qq:group:123456", platform="qq",
        )
        provider = make_role_provider(resolver, task)
        assert provider() == Role.GUEST

    def test_group_stream_placeholder_does_not_double_prefix(self) -> None:
        """群聊流：占位 user_id='planner' 不会造成 person_key 双重前缀（回归）。"""
        config = MaibotAgentConfig(permission=PermissionConfig(
            user_groups=["qq:group:123456"],
        ))
        resolver = PermissionResolver(config.permission)
        task = make_task(
            "t1", owner="planner:qq:group:123456",
            stream_id="qq:group:123456", platform="qq",
        )
        provider = make_role_provider(resolver, task)
        # person_key = qq:planner（不会匹配任意 admins/users）
        # 角色解析通过 user_groups 命中 → USER
        assert provider() == Role.USER

    def test_metadata_caller_role_takes_precedence(self) -> None:
        """set_caller_role 优先：会话 UUID 流（无法映射 owner）以创建者角色执行。

        生产场景：主 planner（ADMIN）创建的 agent 任务 owner=会话 UUID、
        platform=''，owner 解析会回落 GUEST（MCP 等 user+ 工具不可见）；
        持久化的创建者角色应直接生效。
        """
        config = MaibotAgentConfig(permission=PermissionConfig(admins=["qq:1591625223"]))
        resolver = PermissionResolver(config.permission)
        task = make_task(
            "t1", owner="96957f3c849ea3609c331319e64d97e6",
            stream_id="96957f3c849ea3609c331319e64d97e6", platform="",
        )
        task.set_caller_role("admin")
        provider = make_role_provider(resolver, task)
        assert provider() == Role.ADMIN

    def test_metadata_caller_role_falls_back_on_invalid_value(self) -> None:
        """set_caller_role 非法值 → 记警告并回退 owner 解析，不抛异常。"""
        config = MaibotAgentConfig(permission=PermissionConfig(admins=["qq:1591625223"]))
        resolver = PermissionResolver(config.permission)
        task = make_task(
            "t1", owner="qq:1591625223",
            stream_id="qq:1591625223", platform="qq",
        )
        task.set_caller_role("superuser")
        provider = make_role_provider(resolver, task)
        assert provider() == Role.ADMIN
