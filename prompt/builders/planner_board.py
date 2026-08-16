"""Planner 看板提示词构建器。

将「插件能力简介」（每会话首次注入一次）与「待回复清单」（waiting_input
任务）组装为 ``<plugin_intro>`` / ``<task_board>`` XML 块，注入到 Planner
的 LLM 请求中。

看板只推送需要 Planner 主动介入的待办，不再注入运行中/定时/已完成等
状态快照——用户询问任务状态由 subagent_list / subagent_status 工具
按需查询（hook 推事件，工具拉状态）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ...domain.status_formatter import StatusFormatter
from ..base import PromptBuilder, PromptContext

if TYPE_CHECKING:
    from ..manager import PromptManager

logger = logging.getLogger(__name__)


class PlannerBoardBuilder(PromptBuilder):
    """构建 ``planner_board`` 提示词（插件简介 + 待回复看板 XML 块）。

    委托 ``self._pm.render("planner_board", ...)`` 渲染 Jinja2 模板；
    ``_pm`` 为 None 且内容非空时抛出 RuntimeError（对齐 agent_system builder 模式）。
    """

    @property
    def name(self) -> str:
        return "planner_board"

    def build(self, ctx: PromptContext) -> str:
        session_id: str = ctx.data.get("session_id", "")
        show_intro: bool = bool(ctx.data.get("show_intro", False))
        waiting: list = ctx.data.get("waiting", [])

        # 仅记录元信息（上下文键与数量），绝不写入构建出的看板内容
        logger.debug(
            "planner_board 构建：session_id=%r, show_intro=%s, waiting=%d",
            session_id,
            show_intro,
            len(waiting),
        )

        # 无简介且无待办时短路返回 ""（全空短路必须在 _pm 检查之前，
        # 保证无 pm 时仍返回 ""）。
        if not show_intro and not waiting:
            logger.debug("planner_board 构建：无简介且无待办，返回空看板")
            return ""

        if self._pm is None:
            logger.warning("PlannerBoardBuilder：PromptManager 未注入，无法构建看板")
            raise RuntimeError("PlannerBoardBuilder: PromptManager 未注入")

        # 预格式化：将 TaskRecord 对象转为模板可直接迭代的 dict 列表。
        # now 由 builder 一次计算，避免模板内做时间运算。
        now = datetime.now()
        sfmt = StatusFormatter(now=now)

        # waiting：status 直接取枚举原值；info 由 status_info() 取关联
        # 时间戳，经 sfmt.format() 格式化为中文状态描述（如"已等待 3 分钟"）；
        # id8 为 ID 前 8 位，供 Planner 直接复制到 subagent_status 等工具
        waiting_data = [
            {
                "status": t.status.value,
                "title": t.title,
                "id8": t.id[:8],
                "info": sfmt.format(*t.status_info()),
            }
            for t in waiting
        ]

        return self._pm.render(
            "planner_board",
            session_id=session_id,
            show_intro=show_intro,
            waiting=waiting_data,
        )
