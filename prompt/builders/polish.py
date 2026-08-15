"""润色提示词构建器。

构建用于 LLM 润色任务回复的 system prompt，结合上下文与黑话感知。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..base import PromptBuilder, PromptContext

if TYPE_CHECKING:
    from ..manager import PromptManager

logger = logging.getLogger(__name__)


class PolishBuilder(PromptBuilder):
    """构建 ``polish`` 润色提示词。

    将黑话列表格式化为编号列表（无则 "（无）"），
    上下文空则替换为 "（无最近聊天记录）"，
    然后委托 ``self._pm.render("polish", ...)`` 渲染模板；
    ``_pm`` 为 None 时直接抛出 RuntimeError。

    ctx.data 参数：requester 为转达委托人，缺省空串，非空（由 send_polished 的
    ``relay_from`` 传入）时模板输出转达纪律并点名委托人；personality / reply_style
    为主程序 [personality] 配置（由 PolishService 读取传入），缺省空串，
    非空时模板输出人格设定与表达风格节。
    """

    @property
    def name(self) -> str:
        return "polish"

    def build(self, ctx: PromptContext) -> str:
        # 只记录元信息（builder 名 + 上下文键名），不记录提示词内容或参数值
        logger.debug("构建 %s 提示词：上下文键 %s", self.name, sorted(ctx.data))
        # 未注入 PromptManager 时无法渲染模板，直接抛错（由 PromptService 注入）
        if self._pm is None:
            raise RuntimeError("PolishBuilder: PromptManager 未注入")
        # 输入语义：jargon=黑话表（content/meaning 字典）、context=最近聊天记录、
        # result=待润色的原始回复文本；键缺失时统一按空值兜底。
        jargons: list[dict[str, str]] = ctx.data.get("jargon", [])
        context_preview: str = ctx.data.get("context", "")
        result: str = ctx.data.get("result", "")
        # 转达委托人：缺省为空串；str() 兜底非字符串取值
        requester: str = str(ctx.data.get("requester", ""))
        # 主程序人格与表达风格（MaiBot [personality] 配置）：缺省为空串
        personality: str = str(ctx.data.get("personality", ""))
        reply_style: str = str(ctx.data.get("reply_style", ""))

        # 黑话表格式化为 "序号. 黑话：释义"；空表返回占位符 "（无）"
        jargon_text = _format_jargon(jargons)
        # 聊天记录为空时替换为占位文本，避免模板上下文章节为空
        context = context_preview or "（无最近聊天记录）"

        return self._pm.render(
            "polish",
            context=context,
            jargon=jargon_text,
            # result 再次归一为空串，避免 None/空值进入模板
            result=result or "",
            requester=requester,
            personality=personality,
            reply_style=reply_style,
        )


def _format_jargon(jargon: list[dict[str, str]]) -> str:
    # 空黑话表直接返回占位符 "（无）"
    if not jargon:
        return "（无）"
    lines = [
        # 每条格式："序号. 黑话内容：释义"，全角冒号分隔
        f"{i}. {j['content']}：{j['meaning']}"
        for i, j in enumerate(jargon, start=1)
    ]
    return "\n".join(lines)
