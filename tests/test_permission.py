"""oh_mai_agent.permission 的测试 —— 角色判定矩阵。

覆盖：
  - 私聊保障（私聊中基于个人的管理员恒为 ADMIN）
  - 群聊开关（admin_in_group_chats 控制个人管理员在群聊中的角色）
  - 基于群组的管理员（不看身份，不受开关影响）
  - 基于个人的用户
  - 基于群组的用户
  - 访客默认
  - require() 与 describe() 辅助函数
"""

from __future__ import annotations

import pytest

from oh_mai_agent.config import PermissionConfig
from oh_mai_agent.permission import PermissionResolver, Role


def make_resolver(
    admins: list[str] | None = None,
    admin_groups: list[str] | None = None,
    users: list[str] | None = None,
    user_groups: list[str] | None = None,
    admin_in_group_chats: bool = False,
) -> PermissionResolver:
    return PermissionResolver(
        PermissionConfig(
            admins=admins or [],
            admin_groups=admin_groups or [],
            users=users or [],
            user_groups=user_groups or [],
            admin_in_group_chats=admin_in_group_chats,
        )
    )


class TestRoleEnum:
    def test_values(self) -> None:
        assert Role.GUEST.value == "guest"
        assert Role.USER.value == "user"
        assert Role.ADMIN.value == "admin"

    def test_str_equals_value(self) -> None:
        # Role 是 str+Enum 混合类型：str() 返回枚举名模式，相等性比较基于值
        assert Role.ADMIN == "admin"
        assert Role.ADMIN.value == "admin"


class TestRequire:
    def test_admin_meets_all(self) -> None:
        assert PermissionResolver.require(Role.ADMIN, Role.GUEST) is True
        assert PermissionResolver.require(Role.ADMIN, Role.USER) is True
        assert PermissionResolver.require(Role.ADMIN, Role.ADMIN) is True

    def test_user_meets_guest_and_user(self) -> None:
        assert PermissionResolver.require(Role.USER, Role.GUEST) is True
        assert PermissionResolver.require(Role.USER, Role.USER) is True
        assert PermissionResolver.require(Role.USER, Role.ADMIN) is False

    def test_guest_only_meets_guest(self) -> None:
        assert PermissionResolver.require(Role.GUEST, Role.GUEST) is True
        assert PermissionResolver.require(Role.GUEST, Role.USER) is False
        assert PermissionResolver.require(Role.GUEST, Role.ADMIN) is False


class TestDescribe:
    def test_chinese_names(self) -> None:
        assert PermissionResolver.describe(Role.GUEST) == "访客"
        assert PermissionResolver.describe(Role.USER) == "用户"
        assert PermissionResolver.describe(Role.ADMIN) == "管理员"


class TestExtractGroupId:
    def test_group_stream(self) -> None:
        assert PermissionResolver._extract_group_id("qq:group:123456") == "123456"

    def test_private_stream(self) -> None:
        assert PermissionResolver._extract_group_id("qq:10001") is None

    def test_two_part_stream(self) -> None:
        assert PermissionResolver._extract_group_id("discord:user123") is None


class TestResolveRole:
    # ── 私聊保障 ────────────────
    def test_person_admin_private_always_admin(self) -> None:
        r = make_resolver(admins=["qq:10001"])
        assert r.resolve_role(
            platform="qq", user_id="10001", stream_id="qq:10001", is_group=False,
        ) == Role.ADMIN

    # ── 群聊：个人管理员且开关开启 ───────────────
    def test_person_admin_group_switch_on(self) -> None:
        r = make_resolver(admins=["qq:10001"], admin_in_group_chats=True)
        assert r.resolve_role(
            platform="qq", user_id="10001", stream_id="qq:group:123", is_group=True,
        ) == Role.ADMIN

    # ── 群聊：个人管理员且开关关闭 → 降级为 USER ──────────────────────
    def test_person_admin_group_switch_off_downgrade(self) -> None:
        r = make_resolver(admins=["qq:10001"], admin_in_group_chats=False)
        assert r.resolve_role(
            platform="qq", user_id="10001", stream_id="qq:group:123", is_group=True,
        ) == Role.USER

    # ── 基于群组的管理员（不看身份） ────────
    def test_group_admin_any_member_is_admin(self) -> None:
        r = make_resolver(admin_groups=["qq:group:123"])
        # 群 123 中的任意成员都是管理员
        assert r.resolve_role(
            platform="qq", user_id="99999", stream_id="qq:group:123", is_group=True,
        ) == Role.ADMIN
        # 即使该用户也出现在基于个人的用户列表中……
        r2 = make_resolver(admin_groups=["qq:group:123"], users=["qq:99999"])
        assert r2.resolve_role(
            platform="qq", user_id="99999", stream_id="qq:group:123", is_group=True,
        ) == Role.ADMIN  # 群组管理员优先于个人用户

    # ── 群组管理员不受开关影响 ────────
    def test_group_admin_immune_to_switch(self) -> None:
        # 即使 admin_in_group_chats=False，群组管理员依然生效
        r = make_resolver(admin_groups=["qq:group:123"], admin_in_group_chats=False)
        assert r.resolve_role(
            platform="qq", user_id="anyone", stream_id="qq:group:123", is_group=True,
        ) == Role.ADMIN

    # ── 基于个人的用户 ─────
    def test_person_user(self) -> None:
        r = make_resolver(users=["qq:20001"])
        assert r.resolve_role(
            platform="qq", user_id="20001", stream_id="qq:20001", is_group=False,
        ) == Role.USER

    def test_person_user_in_group(self) -> None:
        r = make_resolver(users=["qq:20001"])
        assert r.resolve_role(
            platform="qq", user_id="20001", stream_id="qq:group:123", is_group=True,
        ) == Role.USER

    # ── 基于群组的用户（不看身份） ─────────
    def test_group_user_any_member(self) -> None:
        r = make_resolver(user_groups=["qq:group:456"])
        assert r.resolve_role(
            platform="qq", user_id="random_user", stream_id="qq:group:456", is_group=True,
        ) == Role.USER

    # ── 访客默认 ───────
    def test_guest_default(self) -> None:
        r = make_resolver()
        assert r.resolve_role(
            platform="qq", user_id="stranger", stream_id="qq:stranger", is_group=False,
        ) == Role.GUEST

    def test_guest_default_in_group(self) -> None:
        r = make_resolver()
        assert r.resolve_role(
            platform="qq", user_id="stranger", stream_id="qq:group:789", is_group=True,
        ) == Role.GUEST

    # ── 优先级：个人管理员 vs 群组用户 ───────────────────────────────────────
    def test_person_admin_outranks_group_user(self) -> None:
        r = make_resolver(admins=["qq:10001"], user_groups=["qq:group:123"])
        # 开关关闭时，群聊中的个人管理员 → 降级为 USER
        # （admin_in_group_chats 默认为 False）
        role = r.resolve_role(
            platform="qq", user_id="10001", stream_id="qq:group:123", is_group=True,
        )
        # 用户虽在 admins 列表中，但 admin_in_group_chats=False
        # → 在第 1 步被降级为 USER，所以结果是 USER（而非 GUEST）。
        assert role == Role.USER

    def test_person_admin_with_switch_on_beats_group_user(self) -> None:
        r = make_resolver(admins=["qq:10001"], admin_in_group_chats=True, user_groups=["qq:group:123"])
        assert r.resolve_role(
            platform="qq", user_id="10001", stream_id="qq:group:123", is_group=True,
        ) == Role.ADMIN

    # ── 完整矩阵：同一用户命中全部列表 ───────
    def test_full_priority_chain_admin_wins(self) -> None:
        r = make_resolver(
            admins=["qq:10001"], admin_groups=["qq:group:123"],
            users=["qq:10001"], user_groups=["qq:group:123"],
        )
        # 个人管理员 → 优先被检查，因此胜出
        assert r.resolve_role(
            platform="qq", user_id="10001", stream_id="qq:10001", is_group=False,
        ) == Role.ADMIN

    # ── 跨平台 ──────────
    def test_cross_platform_no_match(self) -> None:
        r = make_resolver(admins=["qq:10001"])
        assert r.resolve_role(
            platform="discord", user_id="10001", stream_id="discord:10001", is_group=False,
        ) == Role.GUEST  # discord:10001 与 qq:10001 不匹配
