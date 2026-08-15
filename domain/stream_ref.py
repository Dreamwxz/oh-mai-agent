"""聊天流与归属人标识的值对象/工具模块 —— 身份语义的唯一定义处。

流 ID 与 owner 字符串以口口相传的格式约定散落全仓（``":group:" in x``、
``owner.split(":", 1)``、``"planner:{stream_id}"`` 拼接等），任何格式演进
都要求地毯式修改。本模块把三类身份语义收敛到单一实现：

- **流类型判定**：``is_group_stream`` / ``group_id_of`` / ``platform_of``；
- **owner 解析**（``platform:user_id``）：``Owner.parse`` / ``Owner.join`` /
  ``Owner.user_id``；
- **Planner 复合 owner**：``planner_owner``（群聊流无单一委托用户时的
  ``planner:{stream_id}`` 语义）。

格式约定（与 permission.py 的解析规则一致）：

- 群聊流：``"{platform}:group:{group_id}"``（如 ``"qq:group:123"``）；
- 私聊流：``"{platform}:{user_id}"``（如 ``"qq:10001"``）；
- 部分宿主 session_id 为不带平台前缀的裸 UUID——所有解析函数对
  非标准格式都安全降级（返回空/None/原串），不抛异常。
"""

from __future__ import annotations

#: 群聊流 ID 的中间段标记（``platform:group:group_id`` 格式）。
GROUP_MARKER = "group"

#: 无单一委托用户的任务 owner 前缀（群聊流中 Planner 创建的任务）。
PLANNER_PREFIX = "planner"


def is_group_stream(stream_id: str) -> bool:
    """*stream_id* 是否为群聊流（含 ``:group:`` 段）。"""
    return ":group:" in stream_id


def platform_of(stream_id: str) -> str:
    """提取 *stream_id* 的平台前缀（冒号前段）；无冒号返回空串。"""
    return stream_id.split(":", 1)[0] if ":" in stream_id else ""


def group_id_of(stream_id: str) -> str | None:
    """从群聊流 ID 中提取 group_id；非群聊流返回 None。

    ``"qq:group:123"`` → ``"123"``；``"qq:10001"`` → ``None``。
    """
    parts = stream_id.split(":", 2)
    if len(parts) == 3 and parts[1] == GROUP_MARKER:
        return parts[2]
    return None


def planner_owner(stream_id: str) -> str:
    """Planner 调用的 owner 标识。

    私聊流（无 ``:group:`` 段）→ 委托用户即 owner（如 ``qq:1591625223``）；
    群聊流（含 ``:group:`` 段）→ ``planner:{stream_id}``（无单一委托用户，
    保留 Planner 语境，回复匹配时视作"群内任何人回复都有效"）。
    """
    if is_group_stream(stream_id):
        return f"{PLANNER_PREFIX}:{stream_id}"
    return stream_id


class Owner:
    """``platform:user_id`` owner 字符串的解析与构造。"""

    @staticmethod
    def parse(owner: str) -> tuple[str, str] | None:
        """解析 owner 为 ``(platform, user_id)``；格式非法返回 None。

        非法判定：无冒号、用户段为空、任一段含多余冒号
        （如 ``unknown:{stream_id}`` 兜底前缀）。
        """
        platform, sep, user_id = owner.partition(":")
        if not sep or not user_id or ":" in platform or ":" in user_id:
            return None
        return platform, user_id

    @staticmethod
    def join(platform: str, user_id: str) -> str:
        """构造 ``platform:user_id``。"""
        return f"{platform}:{user_id}"

    @staticmethod
    def user_id(owner: str) -> str:
        """提取 owner 中的 user_id 段；无冒号时原样返回（裸 UUID 形态）。"""
        return owner.split(":", 1)[1] if ":" in owner else owner
