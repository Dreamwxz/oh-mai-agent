"""上下文注释提示词构建器。

构建插件在聊天流中注入的上下文记录 XML（纯文本记录由调用方自行处理）。
"""

from __future__ import annotations

import logging
import time
from xml.sax.saxutils import escape

from ..base import PromptBuilder, PromptContext

logger = logging.getLogger(__name__)


class ContextNoteBuilder(PromptBuilder):
    """构建上下文注入注释的 XML 标签。

    两种注释格式（模板按 kind 分支渲染）：
    - sent-message: 插件在此流发送了消息
    - task-reply: 插件此前在此流发送了任务消息

    ctx.data 参数：kind 必填（限以上两者）；content 缺省为空串；
    id 缺省时自动生成毫秒时间戳唯一标识（调用方通常自带）。

    由 PromptService.build("context_note", ...) 调用。
    """

    @property
    def name(self) -> str:
        return "context_note"

    def build(self, ctx: PromptContext) -> str:
        # 只记元信息（builder 名 + 上下文数据键），渲染出的完整提示词绝不写入日志
        logger.debug("构建上下文注释：builder=%s，数据键=%s", self.name, sorted(ctx.data))
        # 先做 kind 校验再做 _pm 检查：即便未注入 pm，非法 kind 也抛 ValueError 而非 RuntimeError
        kind: str = ctx.data.get("kind", "")
        if not kind:
            raise ValueError("ContextNoteBuilder requires 'kind' in ctx.data")
        if kind not in ("sent-message", "task-reply"):
            raise ValueError(
                f"ContextNoteBuilder: kind must be 'sent-message' or 'task-reply', got {kind!r}"
            )

        if self._pm is None:
            raise RuntimeError("ContextNoteBuilder: PromptManager 未注入")

        # content 缺省为空串；str() 兜底非字符串取值（如数值 id）
        content: str = str(ctx.data.get("content", ""))
        # id 缺省时回退到毫秒时间戳唯一标识（调用方通常自带，如 oh-mai-agent:send:...）
        note_id: str = str(
            ctx.data.get("id")
            or f"oh-mai-agent:note:{int(time.time() * 1000)}"
        )

        # XML 转义保留在 builder：kind/content/note_id 传入模板前均已转义，
        # 模板 autoescape=False，不会二次转义。
        # 转义的关键目的是防止 content 注入 </plugin_context_note> 等标签拆出 XML 块。
        return self._pm.render(
            "context_note",
            kind=escape(kind),
            content=escape(content),
            note_id=escape(note_id),
        )
