"""Agent 系统提示词构建器。

构建 agent 循环的系统提示词：注入任务元信息（标题、意图），
MaiBot 人设与行为规则由模板静态提供。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..base import PromptBuilder, PromptContext

if TYPE_CHECKING:
    from ..manager import PromptManager

logger = logging.getLogger(__name__)


class AgentSystemBuilder(PromptBuilder):
    """构建 ``agent_system`` 提示词。

    委托 ``self._pm.render("agent_system", ...)`` 渲染模板；
    ``_pm`` 为 None 时直接抛出 RuntimeError。
    """

    @property
    def name(self) -> str:
        return "agent_system"

    def build(self, ctx: PromptContext) -> str:
        logger.debug(
            "构建提示词：builder=%s, task=%s, data_keys=%s",
            self.name,
            "有" if ctx.task is not None else "无",
            sorted(ctx.data),
        )
        if self._pm is None:
            raise RuntimeError("AgentSystemBuilder: PromptManager 未注入")
        # 模板仅声明 title/intent 两个变量（见 templates/index.json）；
        # 缺省空串兜底，保证渲染时两个声明变量始终有值（缺失会抛 ValueError）。
        title = _get_task_attr(ctx, "title", "")
        intent = _get_task_attr(ctx, "intent", "")
        # title/intent 为纯文本占位（非 XML 标签内容），模板 autoescape=False，
        # 原样渲染即可，无需像 injection/context_note 那样做 XML 转义。
        return self._pm.render("agent_system", title=title, intent=intent)


def _get_task_attr(ctx: PromptContext, attr: str, default: str) -> str:
    # 鸭子类型取字段：不 import 具体任务类型，经 hasattr/getattr 读取；
    # task 为 None 或缺字段时回退 default，保证模板变量始终有值。
    if ctx.task is not None and hasattr(ctx.task, attr):
        return str(getattr(ctx.task, attr))
    return default
