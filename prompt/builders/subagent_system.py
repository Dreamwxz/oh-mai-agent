"""子 Agent 系统提示词构建器。

构建子 Agent 循环的系统提示词：注入主 Agent 分派的任务意图（intent）
与可用工具列表（tool_list），角色定调与行为规则由模板静态提供。
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

from ..base import PromptBuilder, PromptContext

logger = logging.getLogger(__name__)


class SubAgentSystemBuilder(PromptBuilder):
    """构建 ``subagent_system`` 提示词。

    委托 ``self._pm.render("subagent_system", ...)`` 渲染模板；
    ``_pm`` 为 None 时直接抛出 RuntimeError。
    """

    @property
    def name(self) -> str:
        return "subagent_system"

    def build(self, ctx: PromptContext) -> str:
        logger.debug(
            "构建提示词：builder=%s，data_keys=%s",
            self.name,
            sorted(ctx.data),
        )
        if self._pm is None:
            raise RuntimeError("SubAgentSystemBuilder: PromptManager 未注入")
        # intent 来自主 Agent LLM 输出、tool_list 为工具名+描述文本，
        # 两者均嵌于模板 XML 标签内（<intent>/<tools>），须在 builder 侧
        # 转义——模板 autoescape=False，不会二次转义。
        # 缺省空串兜底：模板两个声明变量（见 templates/index.json）始终有值，
        # 缺失时 PromptManager 的变量校验会抛 ValueError。
        intent = str(ctx.data.get("intent", ""))
        tool_list = str(ctx.data.get("tool_list", ""))
        return self._pm.render(
            "subagent_system",
            intent=escape(intent),
            tool_list=escape(tool_list),
        )
