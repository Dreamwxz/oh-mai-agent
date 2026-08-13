"""任务分级提示词构建器。

构建用于 LLM 判定任务执行级别（instant/agent）的提示词。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..base import PromptBuilder, PromptContext

if TYPE_CHECKING:
    from ..manager import PromptManager

logger = logging.getLogger(__name__)


class ClassifyLevelBuilder(PromptBuilder):
    """构建 ``classify_level`` 提示词。

    委托 ``self._pm.render("classify_level", ...)`` 渲染模板；
    ``_pm`` 为 None 时直接抛出 RuntimeError。
    """

    @property
    def name(self) -> str:
        return "classify_level"

    def build(self, ctx: PromptContext) -> str:
        logger.debug("构建 classify_level 提示词：上下文键=%s", sorted(ctx.data))
        if self._pm is None:
            logger.error("ClassifyLevelBuilder：PromptManager 未注入，无法渲染提示词")
            raise RuntimeError("ClassifyLevelBuilder: PromptManager 未注入")
        # 从上下文提取用户意图文本；缺省空串保证模板声明的 intent 变量始终有值
        # （render 会校验变量与 index.json 声明一致，缺失或多余都会抛 ValueError）。
        intent: str = ctx.data.get("intent", "")
        # 渲染 classify_level 模板，驱动 LLM 输出 instant / agent 两级判定
        return self._pm.render("classify_level", intent=intent)
