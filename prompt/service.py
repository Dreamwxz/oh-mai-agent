"""PromptService — 统一提示词构建入口。

将 ``PromptManager``（模板渲染）与多个 ``PromptBuilder``
（程序化构建器）组合为一个统一接口。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import PromptBuilder, PromptContext
from .manager import PromptManager

logger = logging.getLogger(__name__)


class PromptService:
    """提示词构建服务：按名称分发到对应 builder。

    Args:
        manager: PromptManager 实例（可为 None — 此时跳过 _pm 注入，
            由 builder 自行处理缺失情形）。
        builders: PromptBuilder 列表，按 name 自动注册到内部字典。
    """

    def __init__(
        self,
        manager: PromptManager | None = None,
        builders: list[PromptBuilder] | None = None,
    ) -> None:
        self._manager = manager
        # 注册表：name → builder；实例由调用方持有（模块级单例），此处仅登记引用
        self._builders: dict[str, PromptBuilder] = {}
        for b in (builders or []):
            # _pm 注入：构造时为 None 的实例自动回填 manager，共享同一渲染入口
            if b._pm is None and manager is not None:
                b._pm = manager
            self._builders[b.name] = b
        logger.info(
            "PromptService 初始化完成：注册 %d 个 builder（%s）",
            len(self._builders),
            ", ".join(sorted(self._builders)) or "无",
        )

    def build(self, name: str, *, task: Any = None, **kwargs: str) -> str:
        """按 *name* 查找 builder，构造 PromptContext 并调用 build()。

        Args:
            name: Builder 名称。
            task: 可选任务对象。
            **kwargs: 传入 PromptContext.data 的键值对。

        Returns:
            渲染后的提示词字符串。

        Raises:
            KeyError: 若 *name* 对应的 builder 不存在。
        """
        builder = self._builders.get(name)
        if builder is None:
            # 未知名兜底：抛 KeyError，交由调用方捕获处理
            logger.warning("未知 builder：%s，抛 KeyError 交由调用方处理", name)
            raise KeyError(f"Unknown prompt builder: {name}")
        logger.debug(
            "构建提示词：builder=%s，数据键=%s，task=%s",
            name,
            sorted(kwargs),
            "有" if task is not None else "无",
        )
        ctx = PromptContext(task=task, data=dict(kwargs))
        try:
            return builder.build(ctx)
        except Exception:
            logger.exception("builder %s 构建提示词失败", name)
            raise

    @property
    def builders(self) -> dict[str, PromptBuilder]:
        """注册表：name → PromptBuilder。"""
        return self._builders

    @property
    def manager(self) -> PromptManager | None:
        """持有的 PromptManager（可为 None）。"""
        return self._manager
