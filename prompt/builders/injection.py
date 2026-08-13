"""消息注入提示词构建器。

构建用户/管理者指令注入时附加的 system 消息文本。
"""

from __future__ import annotations

import logging
import time
from xml.sax.saxutils import escape

from ..base import PromptBuilder, PromptContext

logger = logging.getLogger(__name__)


class InjectionMessageBuilder(PromptBuilder):
    """构建注入指令的 system 消息。

    渲染格式：XML 标签包裹的注入指令。
    """

    @property
    def name(self) -> str:
        return "injection"

    def build(self, ctx: PromptContext) -> str:
        instruction: str = ctx.data.get("instruction", "")
        # 优先取 task_id，兼容调用方只传 id 的情况；两者皆无时留空，
        # 交由下方 note_id 按时间戳生成唯一 id。
        task_id: str = str(ctx.data.get("task_id") or ctx.data.get("id") or "")
        # note_id 作为模板 XML 标签的 id 属性：有 task_id 时复用便于溯源，
        # 否则按毫秒时间戳生成唯一 id，保证多次注入不冲突。
        note_id = task_id or f"oh-mai-agent:inject:{int(time.time() * 1000)}"
        logger.debug(
            "构建注入消息：builder=%s，上下文键=%s",
            self.name,
            sorted(ctx.data),
        )

        if self._pm is None:
            logger.error("InjectionMessageBuilder：PromptManager 未注入，无法渲染注入消息")
            raise RuntimeError("InjectionMessageBuilder: PromptManager 未注入")

        # XML 转义保留在 builder：instruction 为用户/管理者自由输入，
        # 可能含 XML 特殊字符，传入模板前已转义；模板 autoescape=False，
        # 不会二次转义。note_id 通常为系统生成的 id（task_id 或时间戳），
        # 不含 XML 特殊字符，故未转义。
        rendered = self._pm.render(
            "injection",
            instruction=escape(instruction),
            note_id=note_id,
        )
        logger.debug(
            "注入消息渲染完成：builder=%s，长度=%d",
            self.name,
            len(rendered),
        )
        return rendered
