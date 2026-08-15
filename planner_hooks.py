"""Planner 摘要注入模块。

通过 ``maisaka.planner.before_request`` hook 向主 Planner 的 LLM 请求注入
任务摘要（活跃任务 + 定时任务 + 最近完成），采用混合方案
（检查 marker + 注入），插件侧维护 ``session_id → last_hash`` 去重。

去重与注入逻辑详见下方 ``PlannerBoard.hook_before_request`` 内的行内注释。
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from .config import PlannerBoardConfig
from .domain.task_record import TaskRecord, TaskStatus
from .domain.task_store import TaskStore

# ── marker 正则（提取已注入 task_board 的 session_id）───────────────
_MARKER_RE = re.compile(r'<task_board\s+session="([^"]*)"')


class PlannerBoard:
    """Planner 看板：执行摘要构建、去重与注入。

    通过 ``maisaka.planner.before_request`` hook（BLOCKING + EARLY）
    在 Planner LLM 请求中注入当前流任务摘要，帮助 Planner 感知
    后台 Agent 正在做什么。

    去重策略（混合方案）：
    1. 扫描 messages 中是否已有本 session 的 ``<task_board>`` marker
    2. 插件侧 ``session_id → last_hash`` 映射防重复注入
    3. 内容未变且已注入 → 跳过本轮
    """

    def __init__(
        self,
        *,
        store: TaskStore,
        config: PlannerBoardConfig,
        logger: Any = None,
        prompt_service: Any | None = None,
    ) -> None:
        """初始化 PlannerBoard。

        Args:
            store: 任务持久化存储。
            config: Planner 看板配置。
            logger: 可选日志器。
            prompt_service: 可选 PromptService 实例。
        """
        self._store = store
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._last_hash: dict[str, str] = {}
        self._prompt_service = prompt_service
        self._logger.info(
            "PlannerBoard 初始化完成，enabled=%s",
            self._config.enabled,
        )

    # ── 摘要构建 ────────────────────────────────────────────────────

    async def build_summary(self, session_id: str) -> str:
        """为指定聊天流构建任务摘要文本块。

        分别取活跃任务（RUNNING/WAITING_INPUT/PAUSED）、
        定时任务（SCHEDULED）、最近完成（COMPLETED/FAILED/CANCELLED），
        按 config 条数上限截断，包装为 ``<task_board>`` XML 块。

        Args:
            session_id: 聊天流 ID（如 ``"qq:g:123456"``）。

        Returns:
            格式化的摘要文本；当该 session 无任何相关任务时返回空字符串。
        """
        # ── 活跃任务 + 定时任务（list_active 返回非终端任务）────────
        all_active = await self._store.list_active()
        stream_tasks = [t for t in all_active if t.stream_id == session_id]

        running: list[TaskRecord] = []
        waiting: list[TaskRecord] = []
        paused: list[TaskRecord] = []
        scheduled: list[TaskRecord] = []

        for t in stream_tasks:
            if t.status == TaskStatus.RUNNING:
                running.append(t)
            elif t.status == TaskStatus.WAITING_INPUT:
                waiting.append(t)
            elif t.status == TaskStatus.PAUSED:
                paused.append(t)
            elif t.status == TaskStatus.SCHEDULED:
                scheduled.append(t)

        # 活跃任务排序：running > waiting_input > paused，各子类保持原序
        active = (running + waiting + paused)[: self._config.max_active]
        scheduled = scheduled[: self._config.max_scheduled]

        # ── 最近完成的终端任务 ─────────────────────────────────────
        recent = await self._fetch_recent_terminal(session_id)

        # 构建内部日志（高频路径，使用 DEBUG）
        self._logger.debug(
            "看板摘要构建：active=%d scheduled=%d recent=%d",
            len(active),
            len(scheduled),
            len(recent),
        )

        # ── 全空则跳过注入 ─────────────────────────────────────────
        if not active and not scheduled and not recent:
            return ""

        # 摘要经 PromptService 渲染 planner_board 模板，产出 <task_board> XML 块
        return self._prompt_service.build(
            "planner_board",
            session_id=session_id,
            active=active,
            scheduled=scheduled,
            recent=recent,
        )

    async def _fetch_recent_terminal(self, session_id: str) -> list[TaskRecord]:
        """查询指定流最近完成的终端任务。

        对 COMPLETED / FAILED / CANCELLED 三种终态分别查询，
        合并后按 ``updated_at`` 倒序取前 ``max_recent`` 条。
        """
        max_n = self._config.max_recent
        if max_n <= 0:
            return []

        results: list[TaskRecord] = []
        for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            tasks = await self._store.list(
                status=status,
                stream_id=session_id,
                limit=max_n,
            )
            # 防御性过滤：确保只有终端任务被纳入
            results.extend(t for t in tasks if t.is_terminal())

        results.sort(key=lambda t: t.updated_at, reverse=True)
        return results[:max_n]

    # ── Hook 处理 ──────────────────────────────────────────────────

    async def hook_before_request(self, **kwargs: Any) -> dict[str, Any]:
        """Planner ``before_request`` hook 处理函数。

        实现混合方案：先检查 marker + hash 去重，再注入摘要。
        任何异常都返回 ``{"action": "continue"}``，不阻断 Planner。

        流程：
        1. ``config.enabled`` 为 False → 不注入
        2. 提取 ``session_id`` / ``messages``
        3. ``build_summary()`` → 空则跳过
        4. marker 检查（扫描 messages 中是否已有本 session 的 task_board）
        5. hash 去重（内容未变且已注入 → 跳过）
        6. 注入 system 消息到 messages 尾部
        7. 返回 ``modified_kwargs``（含完整 ``messages`` 键，防其他插件覆盖）

        Args:
            **kwargs: Maisaka hook 透传参数，含 ``session_id`` / ``messages`` 等。

        Returns:
            始终包含 ``{"action": "continue"}``；
            注入时附加 ``"modified_kwargs": {"messages": [...]}``。
        """
        try:
            if not self._config.enabled:
                return {"action": "continue"}

            session_id = str(kwargs.get("session_id", ""))
            messages: list[dict[str, Any]] = kwargs.get("messages", [])

            if not session_id:
                return {"action": "continue"}

            summary = await self.build_summary(session_id)
            if not summary:
                return {"action": "continue"}

            # marker 检查：messages 中是否已含本 session 的 task_board
            marker_session = self._extract_marker_session(messages)

            # hash 去重：内容无变化且已注入 → 跳过
            summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
            last_hash = self._last_hash.get(session_id)

            if marker_session == session_id and summary_hash == last_hash:
                return {"action": "continue"}

            # 注入：构造 system 消息追加到 messages 尾部
            self._logger.debug("看板注入开始，session_id=%s", session_id)
            new_messages = list(messages)
            new_messages.append({"role": "system", "content": summary})

            self._last_hash[session_id] = summary_hash
            self._logger.debug(
                "看板注入成功，session_id=%s，摘要长度=%d",
                session_id,
                len(summary),
            )

            return {
                "action": "continue",
                "modified_kwargs": {"messages": new_messages},
            }

        except Exception:
            # 兜底：任何异常都只告警，绝不阻断主 Planner 请求
            self._logger.warning(
                "PlannerBoard hook 处理异常，已跳过看板注入，不阻断 Planner 请求",
                exc_info=True,
            )
            return {"action": "continue"}

    # ── 工具方法 ───────────────────────────────────────────────────

    def reset(self) -> None:
        """清空 hash 状态映射。

        在配置热更新时调用，确保下次请求重新注入摘要
        （即使内容与之前相同）。
        """
        self._last_hash.clear()

    @staticmethod
    def _extract_marker_session(messages: list[dict[str, Any]]) -> str | None:
        """从 messages 中提取已注入 task_board 的 session_id。

        扫描每条消息的 content 字段，匹配 ``<task_board session="...">`` 格式。

        Args:
            messages: LLM 请求消息列表。

        Returns:
            匹配到的 session_id，无匹配时返回 ``None``。
        """
        for msg in messages:
            content = msg.get("content", "")
            # 仅扫描字符串内容，content 为非字符串的消息（如部分 tool 结果）直接跳过
            if isinstance(content, str):
                m = _MARKER_RE.search(content)
                if m:
                    return m.group(1)
        return None
