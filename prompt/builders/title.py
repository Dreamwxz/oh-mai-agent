"""标题生成提示词构建器。

构建用于 LLM 根据意图生成一句话任务标题的提示词。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..base import PromptBuilder, PromptContext

if TYPE_CHECKING:
    from ..manager import PromptManager

logger = logging.getLogger(__name__)


class TitleBuilder(PromptBuilder):
    """构建 ``title`` 提示词。

    委托 ``self._pm.render("title", intent=...)`` 渲染模板；
    ``_pm`` 为 None 时直接抛出 RuntimeError。
    """

    @property
    def name(self) -> str:
        return "title"

    def build(self, ctx: PromptContext) -> str:
        logger.debug(
            "构建提示词：builder=%s, task=%s, data_keys=%s",
            self.name,
            "有" if ctx.task is not None else "无",
            sorted(ctx.data),
        )
        if self._pm is None:
            raise RuntimeError("TitleBuilder: PromptManager 未注入")
        # 从上下文提取用户意图文本；缺省空串保证模板声明的 intent 变量始终有值
        # （render 会校验变量与 index.json 声明一致，缺失或多余都会抛 ValueError）。
        intent: str = ctx.data.get("intent", "")
        # 渲染 title 模板，驱动 LLM 根据意图生成一句话任务标题（模板要求 15 字以内）；
        # 意图原文直接填入、不做截断，长度压缩交由 LLM 完成。
        return self._pm.render("title", intent=intent)
