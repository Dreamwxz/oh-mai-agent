"""Planner 摘要注入模块。

通过 ``maisaka.planner.before_request`` hook 向主 Planner 的 LLM 请求注入
两类内容，采用混合方案（检查 marker + 注入），插件侧维护
``session_id → last_board_hash`` 去重：

1. 插件能力简介（``<plugin_intro>``）——每个会话首次请求注入一次，
   帮助 Planner 建立「本插件 = 后台子代理管理」的心智模型；
2. 待回复看板（``<task_board>``）——当前流存在 waiting_input（待用户
   回复）任务时注入，提醒 Planner 引导用户回复。

设计原则：hook 只推「需要 Planner 主动介入」的事件，运行中/定时/已完成
等状态快照一律不注入——用户询问任务状态由 subagent_list / subagent_status
等工具按需查询（hook 推事件，工具拉状态）。

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

# ── marker 正则（提取已注入的 session_id）─────────────────────────
_INTRO_MARKER_RE = re.compile(r'<plugin_intro\s+session="([^"]*)"')
_BOARD_MARKER_RE = re.compile(r'<task_board\s+session="([^"]*)"')


class PlannerBoard:
    """Planner 看板：插件简介 + 待回复摘要的构建、去重与注入。

    通过 ``maisaka.planner.before_request`` hook（BLOCKING + EARLY）
    在 Planner LLM 请求中注入，帮助 Planner 感知后台子代理任务中
    「需要它介入」的部分（等待用户回复）。

    去重策略（混合方案）：
    1. 简介：扫描 messages 中是否已有本 session 的 ``<plugin_intro>``
       marker，无则注入（每会话一次，不随内容变化重注入）；
    2. 待办：扫描 ``<task_board>`` marker + 插件侧 ``session_id →
       last_board_hash`` 映射，内容未变且已注入 → 跳过本轮。
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
        self._last_board_hash: dict[str, str] = {}
        self._prompt_service = prompt_service
        self._logger.info(
            "PlannerBoard 初始化完成，enabled=%s",
            self._config.enabled,
        )

    # ── 内容构建 ────────────────────────────────────────────────────

    async def build_intro(self, session_id: str) -> str:
        """构建插件能力简介 XML 块（每会话首次注入一次）。

        简介为静态文案，不依赖任务状态；经 PromptService 渲染
        ``planner_board`` 模板的 ``<plugin_intro>`` 段。

        Args:
            session_id: 聊天流 ID（如 ``"qq:g:123456"``）。

        Returns:
            简介 XML 块；prompt_service 缺失时返回空字符串。
        """
        if self._prompt_service is None:
            return ""
        return self._prompt_service.build(
            "planner_board",
            session_id=session_id,
            show_intro=True,
            waiting=[],
        )

    async def build_board(self, session_id: str) -> str:
        """构建待回复看板 XML 块（waiting_input 任务）。

        取当前流所有 WAITING_INPUT 任务，按 ``updated_at`` 升序（等待
        最久的在前）截取 ``max_waiting`` 条，包装为 ``<task_board>`` XML 块。

        Args:
            session_id: 聊天流 ID（如 ``"qq:g:123456"``）。

        Returns:
            格式化的待办摘要；该 session 无等待输入任务时返回空字符串。
        """
        waiting = await self._fetch_waiting(session_id)

        self._logger.debug(
            "看板摘要构建：session_id=%s waiting=%d",
            session_id,
            len(waiting),
        )

        if not waiting:
            return ""

        return self._prompt_service.build(
            "planner_board",
            session_id=session_id,
            show_intro=False,
            waiting=waiting,
        )

    async def _fetch_waiting(self, session_id: str) -> list[TaskRecord]:
        """查询指定流中等待用户输入的任务。

        按 ``updated_at`` 升序（等待最久的优先提醒），截取 ``max_waiting`` 条。
        """
        max_n = self._config.max_waiting
        if max_n <= 0:
            return []

        tasks = await self._store.list(
            status=TaskStatus.WAITING_INPUT,
            stream_id=session_id,
            limit=max_n,
        )
        # 防御性过滤：只保留等待输入的任务（store 查询已按状态过滤，此处兜底）
        waiting = [t for t in tasks if t.status == TaskStatus.WAITING_INPUT]
        # 等待最久的在前（store 按 created_at DESC，这里重排）
        waiting.sort(key=lambda t: t.updated_at)
        return waiting[:max_n]

    # ── Hook 处理 ──────────────────────────────────────────────────

    async def hook_before_request(self, **kwargs: Any) -> dict[str, Any]:
        """Planner ``before_request`` hook 处理函数。

        实现混合方案：先检查 marker + hash 去重，再注入简介/待办。
        任何异常都返回 ``{"action": "continue"}``，不阻断 Planner。

        流程：
        1. ``config.enabled`` 为 False → 不注入
        2. 提取 ``session_id`` / ``messages``
        3. 简介：marker 检查（本 session 未注入过 → 注入插件简介）
        4. 待办：``build_board()`` → 空则跳过；marker + hash 去重
        5. 注入 system 消息到 messages 尾部
        6. 返回 ``modified_kwargs``（含完整 ``messages`` 键，防其他插件覆盖）

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

            new_messages = list(messages)
            injected = False

            # ── 1. 插件简介：每会话首次注入（无论是否有待办）────────
            intro_done = self._extract_marker_session(messages, _INTRO_MARKER_RE) == session_id
            if not intro_done:
                intro = await self.build_intro(session_id)
                if intro:
                    new_messages.append({"role": "system", "content": intro})
                    injected = True

            # ── 2. 待办看板：非空且内容变化时注入 ───────────────────
            board = await self.build_board(session_id)
            if board:
                board_done = (
                    self._extract_marker_session(messages, _BOARD_MARKER_RE) == session_id
                )
                board_hash = hashlib.sha256(board.encode("utf-8")).hexdigest()
                last_hash = self._last_board_hash.get(session_id)

                if not board_done or board_hash != last_hash:
                    self._logger.debug("看板注入开始，session_id=%s", session_id)
                    new_messages.append({"role": "system", "content": board})
                    self._last_board_hash[session_id] = board_hash
                    injected = True

            if not injected:
                return {"action": "continue"}

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

        在配置热更新时调用，确保下次请求重新注入看板
        （即使内容与之前相同）。
        """
        self._last_board_hash.clear()

    @staticmethod
    def _extract_marker_session(
        messages: list[dict[str, Any]], pattern: re.Pattern[str]
    ) -> str | None:
        """从 messages 中提取已注入指定 marker 的 session_id。

        扫描每条消息的 content 字段，匹配 ``<tag session="...">`` 格式。

        Args:
            messages: LLM 请求消息列表。
            pattern: marker 正则（如 ``_INTRO_MARKER_RE`` / ``_BOARD_MARKER_RE``）。

        Returns:
            匹配到的 session_id，无匹配时返回 ``None``。
        """
        for msg in messages:
            content = msg.get("content", "")
            # 仅扫描字符串内容，content 为非字符串的消息（如部分 tool 结果）直接跳过
            if isinstance(content, str):
                m = pattern.search(content)
                if m:
                    return m.group(1)
        return None
