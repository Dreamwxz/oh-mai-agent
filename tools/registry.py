"""工具注册中心 — 工具定义、注册、两级呈现、权限过滤与执行分发。

两级模型：
  - Essential：始终在对话上下文中携带的工具 schema（数量受控，节省 token）。
  - Discoverable：按需发现，Agent 通过 list_tools / get_tool_schema 按需获取。

工具在**呈现**（列出 schema）和**执行**两个阶段均按调用者角色
（guest / user / admin）做过滤。角色比较统一通过 PermissionResolver.require() 判定。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field

from ..permission import PermissionResolver, Role

logger = logging.getLogger(__name__)


# ── ToolDefinition：工具定义数据类 ──────────────────────────────────────────


@dataclass(slots=True)
class ToolDefinition:
    """一条已注册的工具记录，包含元数据、处理函数、可见性与访问控制。

    Attributes:
        name: 工具唯一名（例如 ``"echo"``、``"search_memory"``）。
        description: 面向 LLM 的人类可读描述文本。
        parameters: 工具参数的 JSON Schema
            （``{"type": "object", "properties": {...}, "required": [...]}``）。
        handler: 异步可调用对象，签名为 ``async def handler(**kwargs) -> dict``。
            必须返回 dict，约定格式：
            ``{"success": True, ...}`` 表示成功，
            ``{"success": False, "error": "..."}`` 表示失败。
        visibility: ``"essential"``（始终携带）或 ``"discoverable"``（按需发现）。
        min_role: 调用此工具所需的最低角色。
    """

    name: str
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[dict]]
    visibility: str = field(default="discoverable")
    min_role: Role = field(default=Role.GUEST)

    def to_llm_definition(self) -> dict:
        """转换为 LLM 兼容的工具 schema（OpenAI function-calling 格式）。

        返回 dict 结构如下::

            {
                "type": "function",
                "function": {
                    "name": "...",
                    "description": "...",
                    "parameters": {...}
                }
            }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ── ToolRegistry：工具注册中心 ─────────────────────────────────────────────


class ToolRegistry:
    """Agent 工具的统一注册中心。

    工具按**注册顺序**存储（保证列表顺序稳定）。
    呈现方法（list_*）和执行方法（execute）均通过 PermissionResolver.require()
    按调用者角色做权限门控。

    用法示例::

        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="echo", description="...", parameters={...},
            handler=echo_handler, visibility="essential", min_role=Role.USER,
        ))
        names = reg.names(Role.ADMIN)
        result = await reg.execute("echo", Role.USER, text="hello")
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._order: list[str] = []  # 注册顺序（保证列表稳定）

    # ── 注册 ─────────────────────────────────────────────────────────────

    def register(self, tool: ToolDefinition) -> None:
        """注册一个工具；若同名已存在则覆盖。

        重新注册时保留最后一次的注册顺序（仅当名称尚未出现时才追加到顺序列表末尾）。
        """
        name = tool.name
        if name not in self._tools:
            logger.info(
                "注册工具：%s（可见性=%s，最低角色=%s）",
                name, tool.visibility, tool.min_role.value,
            )
            self._order.append(name)
        else:
            logger.warning("重复注册工具：%s，覆盖已有定义", name)
        self._tools[name] = tool

    def unregister(self, name: str) -> None:
        """按名称注销一个工具；不存在时静默跳过。"""
        if name in self._tools:
            del self._tools[name]
            if name in self._order:
                self._order.remove(name)
            logger.info("注销工具：%s", name)

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, name: str) -> ToolDefinition | None:
        """按名称返回工具定义，未找到返回 None。"""
        if name not in self._tools:
            logger.debug("按名称查找工具未命中：%s", name)
        return self._tools.get(name)

    def all_names(self) -> list[str]:
        """返回全部已注册工具的名称（不做角色过滤，供内部发现使用）。"""
        logger.debug("返回全部工具名称（共 %d 个）", len(self._order))
        return list(self._order)

    def names(self, role: Role) -> list[str]:
        """返回 *role* 角色可见的工具名称列表，保持注册顺序。"""
        logger.debug("按角色 %s 过滤工具名称", role.value)
        return [
            name
            for name in self._order
            if PermissionResolver.require(role, self._tools[name].min_role)
        ]

    # ── 列表呈现（按角色过滤） ───────────────────────────────────────────

    def list_definitions(self, role: Role) -> list[ToolDefinition]:
        """返回 *role* 角色可见的全部工具定义，保持注册顺序。"""
        logger.debug("按角色 %s 列出全部工具定义", role.value)
        return [
            self._tools[name]
            for name in self._order
            if PermissionResolver.require(role, self._tools[name].min_role)
        ]

    def list_essential(self, role: Role) -> list[ToolDefinition]:
        """返回 *role* 角色可见的**Essential 级**工具定义，保持注册顺序。

        Essential 工具 schema 始终携带在上下文里，不占用 discoverable 发现带宽。
        """
        logger.debug("按角色 %s 列出 Essential 工具定义", role.value)
        return [
            self._tools[name]
            for name in self._order
            if self._tools[name].visibility == "essential"
            and PermissionResolver.require(role, self._tools[name].min_role)
        ]

    def list_discoverable(self, role: Role) -> list[ToolDefinition]:
        """返回 *role* 角色可见的**Discoverable 级**工具定义，保持注册顺序。

        Discoverable 工具需 Agent 通过 list_tools / get_tool_schema 按需获取。
        """
        logger.debug("按角色 %s 列出 Discoverable 工具定义", role.value)
        return [
            self._tools[name]
            for name in self._order
            if self._tools[name].visibility == "discoverable"
            and PermissionResolver.require(role, self._tools[name].min_role)
        ]

    # ── 执行 ─────────────────────────────────────────────────────────────

    async def execute(self, name: str, role: Role, **kwargs: object) -> dict:
        """执行一个工具，含权限门控与异常捕获。

        Args:
            name: 工具名称。
            role: 调用者的角色，用于权限校验。
            **kwargs: 透传给工具 handler 的关键字参数。

        Returns:
            一个 dict。成功时返回 handler 的返回值（约定格式
            ``{"success": True, ...}``）。失败时返回::

                {"success": False, "error": "tool not found: ..."}
                {"success": False, "error": "permission denied"}
                {"success": False, "error": "<异常消息>"}
        """
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("执行工具失败：未找到工具 %s（调用者角色 %s）", name, role.value)
            return {"success": False, "error": f"tool not found: {name}"}

        if not PermissionResolver.require(role, tool.min_role):
            return {"success": False, "error": "permission denied"}

        try:
            result = await tool.handler(**kwargs)
            return result
        except Exception as exc:
            return {"success": False, "error": str(exc)}


# ── 模块级便捷函数 ──────────────────────────────────────────────────────────


def build_llm_tool_schemas(definitions: list[ToolDefinition]) -> list[dict]:
    """将 ToolDefinition 列表批量转换为 LLM function-calling 格式。

    这是 ToolDefinition.to_llm_definition() 的便捷封装，适合在构建
    ctx.llm.generate_with_tools() 的 tools 参数时使用。
    """
    return [tool.to_llm_definition() for tool in definitions]
