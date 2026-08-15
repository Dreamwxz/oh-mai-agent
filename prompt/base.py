"""提示词构建器基础设施。

定义 ``PromptContext`` 数据类与 ``PromptBuilder`` 抽象基类，
供各个 builder 实现统一的构建接口。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

# PromptManager 仅用于类型注解，置于 TYPE_CHECKING 下导入（与 builders 子包写法一致）。
if TYPE_CHECKING:
    from .manager import PromptManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PromptContext:
    """提示词构建上下文 — 携带任务引用与任意构建参数。

    ``task`` 携带当前任务对象（可为 None），``data`` 为任意键值参数字典。
    """

    # task 用 Any 声明：PromptContext 为通用载体，不绑定具体 TaskRecord 类型，
    # builder 经 hasattr/getattr 鸭子类型取字段（如 agent_system._get_task_attr）。
    task: Any | None = None
    # default_factory 保证各实例持有独立 dict，避免共享同一可变默认值。
    data: dict[str, Any] = field(default_factory=dict)


class PromptBuilder(ABC):
    """提示词构建器抽象基类。

    每个 builder 声明一个唯一 ``name``，并提供 ``build(ctx)`` 方法。
    可选持有 ``PromptManager`` 引用（由 ``PromptService`` 注入），
    用于委托模板渲染；若 manager 为 None，``build()`` 抛出
    RuntimeError（无内置 fallback，模板渲染依赖 PromptManager）。
    """

    def __init__(self, pm: PromptManager | None = None) -> None:
        # _pm 由 PromptService 注册时经 attach_manager 注入（构造为 None 的
        # 实例会被自动回填）；仍未注入时 build() 抛 RuntimeError。
        self._pm: PromptManager | None = pm

    def attach_manager(self, manager: PromptManager) -> None:
        """绑定 PromptManager 引用（供 PromptService 注册时回填）。

        Builder 与 manager 的关联是组合装配的一部分，经公开方法写入
        而非跨模块直改私有字段；未绑定时 ``build()`` 抛 RuntimeError。
        """
        self._pm = manager

    @property
    @abstractmethod
    def name(self) -> str:
        """Builder 唯一名称，用作注册表中的 key。"""
        ...

    @abstractmethod
    def build(self, ctx: PromptContext) -> str:
        """根据 *ctx* 构建提示词字符串。

        Args:
            ctx: 携带 task 与 data 的构建上下文。

        Returns:
            渲染后的提示词文本。
        """
        ...
