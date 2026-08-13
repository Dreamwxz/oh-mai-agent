"""Planner 看板提示词构建器。

将活跃任务、定时任务、最近进入终态的任务（完成/失败/取消）组装为
``<task_board>`` XML 块，注入到 Planner 的 LLM 请求中。
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
    """构建 ``planner_board`` 提示词（任务看板 XML 块）。

    委托 ``self._pm.render("planner_board", ...)`` 渲染 Jinja2 模板；
    ``_pm`` 为 None 且看板非空时抛出 RuntimeError（对齐 agent_system builder 模式）。
    """

    @property
    def name(self) -> str:
        return "planner_board"

    def build(self, ctx: PromptContext) -> str:
        session_id: str = ctx.data.get("session_id", "")
        active: list = ctx.data.get("active", [])
        scheduled: list = ctx.data.get("scheduled", [])
        recent: list = ctx.data.get("recent", [])

        # 仅记录元信息（上下文键与数量），绝不写入构建出的看板内容
        logger.debug(
            "planner_board 构建：session_id=%r, active=%d, scheduled=%d, recent=%d",
            session_id,
            len(active),
            len(scheduled),
            len(recent),
        )

        # 三组列表已由 planner_hooks 按 max_active/max_scheduled/max_recent 上限
        # 筛选与截断，此处只做格式化展示，不再做数量限制。
        # 全空短路必须在 _pm 检查之前（保证 test_empty_when_no_tasks 无 pm 仍返回 ""）
        if not active and not scheduled and not recent:
            logger.debug("planner_board 构建：无活跃/定时/最近任务，返回空看板")
            return ""

        if self._pm is None:
            logger.warning("PlannerBoardBuilder：PromptManager 未注入，无法构建看板")
            raise RuntimeError("PlannerBoardBuilder: PromptManager 未注入")

# 预格式化：将 TaskRecord 对象转为模板可直接迭代的 dict 列表。
        # now 由 builder 一次计算，避免模板内做时间运算。
        now = datetime.now()
        sfmt = StatusFormatter(now=now)

        # active / scheduled：status 直接取枚举原值；info 由 status_info()
        # 取关联时间戳，经 sfmt.format() 格式化为中文状态描述
        active_data = [
            {
                "status": t.status.value,
                "title": t.title,
                "info": sfmt.format(*t.status_info()),
            }
            for t in active
        ]
        scheduled_data = [
            {
                "status": t.status.value,
                "title": t.title,
                "info": sfmt.format(*t.status_info()),
            }
            for t in scheduled
        ]

        # recent：按 updated_at 计算相对秒数（max(..., 0) 钳制负值，防时钟超前），
        # 经 format_relative 生成中文描述（如 "3 分钟前"）
        recent_data = [
            {
                "status": t.status.value,
                "title": t.title,
                "rel": sfmt.format_relative(max((now - t.updated_at).total_seconds(), 0)),
            }
            for t in recent
        ]

        return self._pm.render(
            "planner_board",
            session_id=session_id,
            active=active_data,
            scheduled=scheduled_data,
            recent=recent_data,
        )
