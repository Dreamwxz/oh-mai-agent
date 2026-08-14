"""oh-mai-agent 权限判定模块。

角色模型：
  ─ GUEST（访客）：默认角色，仅可只读查看任务列表/历史。
  ─ USER （用户）：可创建/调度任务、修改自己的任务、沙箱文件操作。
  ─ ADMIN（管理员）：完全访问 — 所有任务、宿主机文件系统、插件配置。

角色按聊天流上下文解析。同一人在不同聊天流中可能拥有不同角色
（私聊 vs 群聊）。

解析规则（优先级从高到低）：
  1. 按人匹配 admin（config.admins）：platform:user_id
     - 私聊 → 无条件 ADMIN（私聊保证）
     - 群聊 → admin_in_group_chats=True 时为 ADMIN，否则降级为 USER
  2. 按群匹配 admin（config.admin_groups）：platform:group:group_id
     - 群内所有人均为 ADMIN（无视身份，不受开关影响）
  3. 按人匹配 user（config.users）：platform:user_id → USER
  4. 按群匹配 user（config.user_groups）：platform:group:group_id → USER
     - 群内所有人均为 USER（无视身份）
  5. 否则 → GUEST
"""

import logging
from enum import Enum

from .config import PermissionConfig

logger = logging.getLogger(__name__)


class Role(str, Enum):
    """用户角色，按权限升序排列：GUEST < USER < ADMIN。"""

    GUEST = "guest"
    USER = "user"
    ADMIN = "admin"


class PermissionResolver:
    """根据 PermissionConfig 解析和检查用户角色。

    用法::

        cfg = PermissionConfig(admins=["qq:10001"], ...)
        resolver = PermissionResolver(cfg)
        role = resolver.resolve_role(
            platform="qq", user_id="10001",
            stream_id="qq:10001", is_group=False,
        )
    """

    def __init__(self, config: PermissionConfig) -> None:
        """使用给定权限配置初始化解析器。"""
        self._config = config
        logger.info(
            "PermissionResolver 初始化：admins=%d、admin_groups=%d、"
            "users=%d、user_groups=%d、admin_in_group_chats=%s",
            len(config.admins),
            len(config.admin_groups),
            len(config.users),
            len(config.user_groups),
            config.admin_in_group_chats,
        )

    # ── 辅助方法 ──────────────────────────────────────────────────────────

    @staticmethod
    def _extract_group_id(stream_id: str) -> str | None:
        """从聊天流标识符中提取 group_id。

        stream_id 格式：
          - 群聊：  ``"{platform}:group:{group_id}"``
          - 私聊：  ``"{platform}:{user_id}"``

        如果是群聊流则返回 group_id，否则返回 None。
        """
        parts = stream_id.split(":", 2)
        if len(parts) == 3 and parts[1] == "group":
            return parts[2]
        return None

    @staticmethod
    def require(role: Role, minimum: Role) -> bool:
        """检查 *role* 是否达到或超过 *minimum*。

        >>> PermissionResolver.require(Role.USER, Role.GUEST)
        True
        >>> PermissionResolver.require(Role.GUEST, Role.USER)
        False
        """
        order: dict[Role, int] = {Role.GUEST: 0, Role.USER: 1, Role.ADMIN: 2}
        # 未知角色按 -1、未知下限按 999 处理，保证任何未知值都判定为不通过
        return order.get(role, -1) >= order.get(minimum, 999)

    @staticmethod
    def describe(role: Role) -> str:
        """返回 *role* 的中文显示名称。"""
        names: dict[Role, str] = {
            Role.GUEST: "访客",
            Role.USER: "用户",
            Role.ADMIN: "管理员",
        }
        return names.get(role, "未知")

    # ── 主 API ──────────────────────────────────────────────────────────

    def resolve_role(
        self,
        *,
        platform: str,
        user_id: str,
        stream_id: str,
        is_group: bool,
    ) -> Role:
        """解析 *user_id* 在给定聊天流中的角色。

        Args:
            platform: 平台名称（如 ``"qq"``、``"discord"``）。
            user_id: 用户的平台内 ID。
            stream_id: 完整聊天流标识符
                （如 ``"qq:group:123"`` 或 ``"qq:10001"``）。
            is_group: 该流是否为群聊（True）或私聊（False）。
                调用方应根据流类型传入。

        Returns:
            该用户在当前流中的解析后角色。
        """
        person_key = f"{platform}:{user_id}"
        group_id = self._extract_group_id(stream_id)
        group_key = f"{platform}:group:{group_id}" if group_id else None
        logger.debug(
            "解析角色：platform=%s, user_id=%s, stream_id=%s, "
            "is_group=%s, person_key=%s, group_key=%s",
            platform,
            user_id,
            stream_id,
            is_group,
            person_key,
            group_key,
        )

        # ── 1. 按人匹配的管理员 ────────────────────────────────────────────
        if person_key in self._config.admins:
            if not is_group:
                logger.debug("按人命中管理员（私聊保证），角色=%s", Role.ADMIN.value)
                return Role.ADMIN  # 私聊场景保证为管理员
            if self._config.admin_in_group_chats:
                logger.debug("按人命中管理员（群聊开关开启），角色=%s", Role.ADMIN.value)
                return Role.ADMIN
            logger.warning(
                "按人命中管理员但 admin_in_group_chats 关闭，角色降级=%s",
                Role.USER.value,
            )
            return Role.USER  # 降级：开关关闭时

        # ── 2. 基于群的管理员（无视身份，不受开关影响） ─
        if group_key and group_key in self._config.admin_groups:
            logger.debug("按群命中管理群，角色=%s", Role.ADMIN.value)
            return Role.ADMIN

        # ── 3. 按人匹配的用户 ────────────────────────────────────
        if person_key in self._config.users:
            logger.debug("按人命中用户，角色=%s", Role.USER.value)
            return Role.USER

        # ── 4. 基于群的用户（无视身份） ────────────────────
        if group_key and group_key in self._config.user_groups:
            logger.debug("按群命中用户群，角色=%s", Role.USER.value)
            return Role.USER

        # ── 5. 默认 ──────────────────────────────────────────────────
        logger.debug("未命中任何规则，默认角色=%s", Role.GUEST.value)
        return Role.GUEST
