"""统一发送出口 —— ReplySender / PolishService / fail_task / 自动转达判定。

全插件共用的**回复链路基础设施**（横切能力，独立于任何执行器）：

- ``ReplySender`` 提供两条发送出口：
  - ``send_raw``：直发（分割 + 重试），无润色 —— 命令回复、失败通知等确定性文本；
  - ``send_polished``：完整链路（信息获取 → 润色 → 直发）—— 任务回复、提问等；
  - 以及独立的上下文注释能力（``append_motivation_note``，对用户不可见）。
- ``PolishService``：回复润色器（上下文 + 黑话匹配 + LLM 风格优化）。
- ``fail_task``：标记任务 FAILED + 持久化 + 通知调度器（可先直发失败消息）。
- ``resolve_relay`` / ``resolve_auto_relay``：自动转达判定（目标用户 ≠ 发起人）。

发送出口纯发送（不写 context），跨流动机注释经独立能力
``append_motivation_note`` 显式写入（对用户不可见，写给 MaiBot/Planner 上下文）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..config import PolishConfig
from ..domain.stream_ref import Owner, is_group_stream
from ..domain.task_record import TaskRecord, TaskStatus, TaskStatusError
from .base import ExecutionContext
from .splitter import split_message

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────

MAX_JARGON_REFERENCE_MATCHES = 10
"""注入润色提示词的黑话条目上限（与 MaiBot 一致）。"""


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


def _normalize_match_text(text: object) -> str:
    """归一化文本用于机械匹配：转小写、合并连续空白。"""
    return " ".join(str(text or "").strip().lower().split())


def _jargon_in_scope(session_id_dict_str: str, stream_id: str) -> bool:
    """检查黑话的 session_id_dict 是否包含当前聊天流。

    复刻 ``jargon_context_matcher.py`` 中的 ``_jargon_in_scope``：
    解析 JSON 字典 ``{"session_id": count, ...}``，检查
    *stream_id* 是否在键中。
    """
    try:
        parsed: dict[str, object] = json.loads(session_id_dict_str) if session_id_dict_str else {}
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    return stream_id in parsed


def _calculate_match_score(
    *,
    candidate_count: int,
    first_message_index: int,
    high_freq_rank: int = 0,
    high_freq_count: int = 0,
    hit_high_frequency: bool = False,
) -> float:
    """复刻 MaiBot 的黑话匹配评分公式。

    来自 ``jargon_context_matcher.py:_calculate_match_score``（第 298-310 行）::

        score = count + (1000 + hf_count*2 + rank_bonus if hit_hf) - first_index*0.01
    """
    high_freq_score = 0.0
    if hit_high_frequency:
        high_freq_score = 1000.0 + high_freq_count * 2.0
        if high_freq_rank > 0:
            high_freq_score += max(0.0, 100.0 - high_freq_rank)
    return float(candidate_count) + high_freq_score - first_message_index * 0.01

# ── PolishService ────────────────────────────────────────────────────────────


class PolishService:
    """回复润色器：加载上下文 + 黑话匹配 + LLM 风格优化。

    复刻 MaiBot 的 ``jargon_context_matcher`` 评分和匹配逻辑，
    并注入主程序 ``[personality]`` 的人格与表达风格，
    用于 instant/agent 任务结果回复。
    """

    def __init__(
        self,
        *,
        ctx: Any,
        config: PolishConfig,
        use_jargon: bool = True,
        prompt_service: Any | None = None,
    ):
        self.ctx = ctx
        self.config = config
        self.use_jargon = use_jargon
        self._prompt_service = prompt_service

    # ── 公开接口 ────────────────────────────────────────────────────────────

    async def polish(
        self,
        *,
        result: str,
        stream_id: str,
        is_group: bool,
        relay_from: str | None = None,
    ) -> str:
        """润色任务结果回复，结合上下文和黑话感知。

        流程：
        1. 获取目标聊天流的最近消息。
        2. 对消息做机械黑话匹配。
        3. 读取主程序 ``[personality]`` 配置（人格与表达风格，每次读取热更新生效）。
        4. 构建含上下文、黑话、人格与表达风格的 system prompt。
        5. 调用 LLM 润色，将 *result* 作为 user 消息传入。
        6. 任何异常时返回原始 *result*（绝不阻塞消息发送）。

        Args:
            result: 待润色的原始任务结果文本。
            stream_id: 目标聊天流 ID。
            is_group: 目标流是否为群聊。
            relay_from: 转达委托人（非空 = 转达他人之言，润色点名委托人；
                        缺省 None = 本人发言）。

        Returns:
            润色后的回复文本；失败时返回原始 *result*。
        """
        logger.debug("开始润色回复：流=%s relay_from=%s", stream_id, relay_from)
        try:
            context_texts = await self._load_context(stream_id, is_group)

            jargons: list[dict[str, str]] = []
            if self.use_jargon:
                jargons = await self._match_jargons(stream_id, context_texts)

            context_preview = (
                "\n".join(context_texts[-20:]) if context_texts else "（无最近聊天记录）"
            )
            # 主程序 [personality] 配置：每次读取（热更新生效），空值归一为空串
            personality = str(
                await self.ctx.config.get("personality.personality", "") or ""
            )
            reply_style = str(
                await self.ctx.config.get("personality.reply_style", "") or ""
            )
            # 主程序 [bot].nickname：缺省空串（builder 兜底"麦麦"）
            bot_name = str(
                await self.ctx.config.get("bot.nickname", "") or ""
            )
            system_prompt = self._prompt_service.build(
                "polish",
                jargon=jargons,
                context=context_preview,
                result=result,
                requester=relay_from or "",
                personality=personality,
                reply_style=reply_style,
                bot_name=bot_name,
            )

            llm_result = await self.ctx.llm.generate(
                prompt=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": result},
                ],
                model="replyer",
                timeout_ms=120000,
            )
            if isinstance(llm_result, dict):
                return str(llm_result.get("response", result))
            return str(llm_result)
        except Exception:
            logger.warning(
                "润色失败，流 %s 的回复降级为原始文本",
                stream_id,
                exc_info=True,
            )
            return result

    # ── 内部：上下文加载 ────────────────────────────────────────────────────

    async def _load_context(self, stream_id: str, is_group: bool) -> list[str]:
        """获取目标聊天流的最近消息文本，排除 bot 自己的消息。

        上下文数量跟随 MaiBot 配置：
        ``chat.max_context_size``（群聊，默认 40）或
        ``chat.max_private_context_size``（私聊，默认 60）。

        Args:
            stream_id: 目标聊天流 ID。
            is_group: 是否为群聊。

        Returns:
            消息文本字符串列表（bot 消息已排除）。
        """
        if is_group:
            limit = await self.ctx.config.get("chat.max_context_size", 40)
        else:
            limit = await self.ctx.config.get("chat.max_private_context_size", 60)

        messages = await self.ctx.message.get_recent(chat_id=stream_id, limit=limit)
        texts: list[str] = []
        for msg in messages:
            if msg.get("is_bot", False):
                continue
            content = str(msg.get("content", "")).strip()
            if content:
                texts.append(content)
        return texts

    # ── 内部：黑话匹配 ──────────────────────────────────────────────────────

    async def _match_jargons(
        self, stream_id: str, context_texts: list[str]
    ) -> list[dict[str, str]]:
        """对上下文消息做机械黑话匹配（复刻 MaiBot 逻辑）。

        流程：
        1. 加载黑话候选（``is_jargon=True``、``meaning != ""``、
            按 ``count`` DESC 排序）。
        2. 范围过滤：``is_global`` 为 True 或 ``session_id_dict`` 包含 *stream_id*。
        3. 加载该聊天流的高频词。
        4. 对每条归一化消息文本，子串匹配每个候选。
        5. 评分并返回前 ``MAX_JARGON_REFERENCE_MATCHES`` 条。

        Args:
            stream_id: 目标聊天流 ID。
            context_texts: 待匹配的消息文本（已排除 bot 消息）。

        Returns:
            匹配度最高的黑话字典列表，含 ``content`` 和 ``meaning``。
        """
        # 1. 加载黑话候选
        jargon_records = await self.ctx.db.query("Jargon", filters={"is_jargon": True})
        candidates: list[dict[str, object]] = []
        for rec in jargon_records:
            meaning = str(rec.get("meaning", "")).strip()
            content = str(rec.get("content", "")).strip()
            if not content or not meaning:
                continue
            is_global = bool(rec.get("is_global", False))
            session_id_dict = str(rec.get("session_id_dict", "{}"))
            if not is_global and not _jargon_in_scope(session_id_dict, stream_id):
                continue
            candidates.append(
                {
                    "content": content,
                    "meaning": meaning,
                    "count": int(rec.get("count", 0) or 0),
                }
            )
        candidates.sort(key=lambda c: -c["count"])

        if not candidates:
            return []

        # 2. 加载该流的高频词
        hf_records = await self.ctx.db.query(
            "HighFrequencyTerm", filters={"chat_id": stream_id}
        )
        high_freq_by_term: dict[str, dict[str, int]] = {}
        for rec in hf_records:
            term_key = _normalize_match_text(rec.get("term", ""))
            if term_key:
                high_freq_by_term[term_key] = {
                    "rank": int(rec.get("rank", 0) or 0),
                    "count": int(rec.get("occurrence_count", 0) or 0),
                }

        # 3. 机械匹配 — 按 content_key 去重（只保留首次命中）
        matches: dict[str, dict[str, object]] = {}
        for msg_idx, text in enumerate(context_texts):
            normalized_text = _normalize_match_text(text)
            if not normalized_text:
                continue
            for candidate in candidates:
                content_key = _normalize_match_text(candidate["content"])
                if not content_key or content_key in matches:
                    continue
                if content_key not in normalized_text:
                    continue

                hf = high_freq_by_term.get(content_key)
                score = _calculate_match_score(
                    candidate_count=candidate["count"],
                    first_message_index=msg_idx,
                    high_freq_rank=hf["rank"] if hf else 0,
                    high_freq_count=hf["count"] if hf else 0,
                    hit_high_frequency=hf is not None,
                )
                matches[content_key] = {
                    "content": candidate["content"],
                    "meaning": candidate["meaning"],
                    "score": score,
                    "first_message_index": msg_idx,
                }

        # 4. 排序并返回前 MAX_JARGON_REFERENCE_MATCHES 条
        #    排序键复刻 match_jargons_for_context 的顺序。
        sorted_matches = sorted(
            matches.values(),
            key=lambda m: (
                -m["score"],
                m["first_message_index"],
                -len(str(m["content"])),
                str(m["content"]),
            ),
        )[:MAX_JARGON_REFERENCE_MATCHES]

        return [
            {"content": str(m["content"]), "meaning": str(m["meaning"])}
            for m in sorted_matches
        ]


# ── ReplySender（统一发送出口） ──────────────────────────────────────────────


class ReplySender:
    """统一发送出口：直发 / 完整（润色）两条出口 + 独立的上下文注释能力。

    设计约定：
    - **发送出口只对用户可见**（``ctx.send.text``），不做任何 ``context.append``；
    - 对用户不可见的上下文写入（动机 XML 注释等）是独立能力
      （``append_motivation_note``），由需要的地方显式调用 —— 这是让
      MaiBot / Planner 感知插件正在做什么的关键通道；
    - 基础设施（ctx / config / prompt_service）构造时注入，``config_getter``
      每次调用读取，配置热更新立即生效。
    """

    def __init__(
        self,
        *,
        ctx: Any,
        config_getter: Callable[[], Any],
        prompt_service: Any | None = None,
    ) -> None:
        """初始化发送器。

        Args:
            ctx: MaiBot PluginContext（``ctx.send.text`` 发送）。
            config_getter: ``() -> MaibotAgentConfig``，每次发送时读取（热更新生效）。
            prompt_service: PromptService（润色 / context_note 构建，可选）。
        """
        self._ctx = ctx
        self._config_getter = config_getter
        self._prompt_service = prompt_service

    @property
    def prompt_service(self) -> Any | None:
        """持有的 PromptService（供工具层构建上下文注释）。"""
        return self._prompt_service

    # ── 出口1：直发 ─────────────────────────────────────────────────────

    async def send_raw(self, text: str, stream_id: str) -> None:
        """直发原文：分割（跟随 ``config.splitter``）+ 指数退避重试。

        无润色、无上下文写入 —— 用于命令回复、失败通知等确定性文本，
        内容不能被 LLM 改写。

        Args:
            text: 待发送的原始文本。
            stream_id: 目标聊天流 ID。

        Raises:
            RuntimeError: 重试耗尽仍发送失败（含 SDK 静默掉包 False/None）。
        """
        segments = self._split(text)
        await self._send_segments(segments, stream_id)

    # ── 出口2：完整链路 ────────────────────────────────────────────────

    async def send_polished(
        self,
        text: str,
        stream_id: str,
        *,
        relay_from: str | None = None,
    ) -> None:
        """获取信息（上下文/黑话）→ 润色 → 直发（分割 + 重试）。

        润色失败时降级为原文发送（PolishService 内部回退），不阻塞发送。
        ``relay_from`` 非空 = 转达他人之言（润色点名委托人），缺省 = 本人发言。

        Args:
            text: 待润色并发送的原始文本。
            stream_id: 目标聊天流 ID。
            relay_from: 转达委托人姓名/昵称（可选）。
        """
        config = self._config_getter()
        svc = PolishService(
            ctx=self._ctx,
            config=config.polish,
            prompt_service=self._prompt_service,
        )
        polished = await svc.polish(
            result=text,
            stream_id=stream_id,
            is_group=is_group_stream(stream_id),
            relay_from=relay_from,
        )
        await self.send_raw(polished, stream_id)

    # ── 独立能力：上下文注释（对用户不可见） ─────────────────────────────

    async def append_motivation_note(self, stream_id: str, content: str) -> None:
        """向目标聊天流写入动机 XML 上下文注释（``context_note`` 模板）。

        对用户不可见，写给 MaiBot / Planner 的上下文 —— 让其了解
        "这条消息是某个任务的结果、任务意图是什么"。任何失败仅告警。

        Args:
            stream_id: 目标聊天流 ID。
            content: 动机内容（如任务意图文本）。
        """
        if not content or self._prompt_service is None:
            return
        try:
            note_id = f"oh-mai-agent:task-note:{int(time.time() * 1000)}"
            note_text = self._prompt_service.build(
                "context_note",
                kind="task-reply",
                content=content,
                id=note_id,
                # 主程序 [bot].nickname：缺省空串（builder 兜底"麦麦"）
                bot_name=str(await self._ctx.config.get("bot.nickname", "") or ""),
            )
            await self._ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": note_text}],
                visible_text=note_text,
                message_id=note_id,
                source_kind="plugin:oh-mai-agent:task-reply",
            )
        except Exception:
            logger.warning("XML 动机注释写入失败，流=%s", stream_id, exc_info=True)

    async def append_task_waiting_note(
        self, stream_id: str, title: str, question: str
    ) -> None:
        """任务进入等待输入时写入上下文注释（``context_note`` kind=task-waiting）。

        对用户不可见，写给 MaiBot / Planner 的上下文 —— 任务挂起等待
        用户回复时留痕（含任务标题与问题文本），使 Planner 即使跨多轮
        对话也能理解"某条用户回复是在回答哪个任务的提问"。任何失败仅告警。

        Args:
            stream_id: 任务所在聊天流 ID。
            title: 任务标题。
            question: 任务向用户提出的问题文本。
        """
        if self._prompt_service is None:
            return
        try:
            note_id = f"oh-mai-agent:task-waiting:{int(time.time() * 1000)}"
            note_text = self._prompt_service.build(
                "context_note",
                kind="task-waiting",
                title=title,
                question=question,
                id=note_id,
                # 主程序 [bot].nickname：缺省空串（builder 兜底"麦麦"）
                bot_name=str(await self._ctx.config.get("bot.nickname", "") or ""),
            )
            await self._ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": note_text}],
                visible_text=note_text,
                message_id=note_id,
                source_kind="plugin:oh-mai-agent:task-waiting",
            )
        except Exception:
            logger.warning(
                "任务等待注释写入失败，流=%s", stream_id, exc_info=True
            )

    # ── 内部 ───────────────────────────────────────────────────────────

    def _split(self, text: str) -> list[str]:
        """按 ``config.splitter`` 配置分割长文本；未启用或短文本原样返回。"""
        config = self._config_getter()
        splitter = getattr(config, "splitter", None)
        if splitter is not None and splitter.enable:
            segments = split_message(
                text,
                max_length=splitter.max_length,
                max_messages=splitter.max_messages,
            ) or [text]
        else:
            segments = [text]
        if len(segments) > 1:
            logger.debug(
                "回复文本分割为 %d 段（共 %d 字符）", len(segments), len(text),
            )
        return segments

    async def _send_segments(self, segments: list[str], stream_id: str) -> None:
        """逐段发送，指数退避重试（``config.send.max_retries``），静默掉包检测。

        任一段重试耗尽即停止后续分段并抛出最后异常（交由上层标记 FAILED）。
        """
        config = self._config_getter()
        send_cfg = getattr(config, "send", None)
        max_retries = send_cfg.max_retries if send_cfg is not None else 3

        last_exc: Exception | None = None
        for segment in segments:
            for attempt in range(max_retries):
                try:
                    logger.debug(
                        "发送回复第 %d/%d 次尝试：流=%s（本次退避间隔 %ds）",
                        attempt + 1, max_retries, stream_id, 2 ** attempt,
                    )
                    result = await self._ctx.send.text(segment, stream_id)
                    if result in (False, None):
                        raise RuntimeError("send.text returned False/None")
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < max_retries - 1:
                        backoff = 2 ** attempt
                        logger.warning(
                            "发送回复第 %d/%d 次尝试失败（流=%s），%ds 后重试：%s",
                            attempt + 1, max_retries, stream_id, backoff, exc,
                        )
                        await asyncio.sleep(backoff)
                    else:
                        logger.error(
                            "发送回复第 %d/%d 次尝试失败（流=%s），重试 %d 次全部失败，放弃发送",
                            attempt + 1, max_retries, stream_id, max_retries,
                            exc_info=True,
                        )
            else:
                # 本段重试耗尽 —— 停止发送后续分段
                break
        if last_exc is not None:
            raise last_exc


# ── fail_task ────────────────────────────────────────────────────────────────


async def fail_task(
    task: TaskRecord,
    store: Any,
    scheduler: Any,
    exec_ctx: ExecutionContext,
    *,
    send_message: bool = False,
) -> None:
    """标记任务为 FAILED，持久化，通知调度器。

    当 *send_message* 为 True 时，在状态变更前先向目标聊天流直发失败消息
    （经 ``ReplySender.send_raw``，确定性错误文本不润色），发送失败不影响任务状态更新。

    Args:
        task: 待标记为失败的任务。
        store: TaskStore 持久化。
        scheduler: TaskScheduler 通知。
        exec_ctx: 执行上下文（含 sender / ctx / config）。
        send_message: 是否先发送失败消息再变更状态。
    """
    if task.is_terminal():
        return
    try:
        persisted_task = await store.get(task.id)
    except Exception:
        persisted_task = None
    if persisted_task is not None and persisted_task.is_terminal():
        return

    if send_message:
        error_reason = task.error() or "任务执行失败"
        fail_text = f"任务执行失败: {error_reason}"
        sender = getattr(exec_ctx, "sender", None)
        if sender is not None:
            try:
                # 失败通知走直发出口：确定性错误文本不应被润色改写
                await sender.send_raw(fail_text, task.reply_target)
            except Exception:
                logger.warning(
                    "失败消息发送失败，任务 %s 仍将继续标记为 FAILED", task.id, exc_info=True,
                )

    try:
        task.transition(TaskStatus.FAILED)
    except TaskStatusError:
        if not task.is_terminal():
            task.force(TaskStatus.FAILED, actor="executor", reason="fail_task")
    task.updated_at = datetime.now()
    await store.save(task)
    logger.info("任务 %s 已标记为 FAILED 并持久化", task.id)
    await scheduler.on_task_completed(task)


# ── 自动转达判定 ────────────────────────────────────────────────────────────


async def resolve_relay(ctx: Any, owner: str, target: str) -> str | None:
    """自动转达判定核心：目标为私聊流且目标用户 ≠ 发起人 → 转达。

    规则（对应"传出用户和传入用户不一样就加上"）：
    - 传入用户 = ``owner``（创建入口拼的 ``platform:user_id``），
      格式异常（``unknown:`` 前缀 / 无冒号 / 用户段含冒号）不判定；
    - 传出用户 = ``target``（目标流 ID）反查流对象——宿主 session_id 是
      哈希（``utils_session.calculate_session_id`` 为 MD5），无法从 ID 直接
      解析用户，须经 ``chat.get_all_streams`` 匹配流对象；
    - 目标为群流（无单一传出用户）或流未找到 → 不自动转达
      （由 LLM 经 send_message 工具显式传 relay_from）；
    - 目标用户与发起人相同 → 本人发言；不同 → 转达，
      ``relay_from`` 取发起人昵称（``chat.get_stream_by_user_id`` 反查
      发起人私聊流的 ``user_nickname``），反查失败兜底 ``owner`` 原文。

    Args:
        ctx: MaiBot PluginContext（chat 能力）。
        owner: 任务发起人（``platform:user_id``，如 qq:10001）。
        target: 目标聊天流 ID（如私聊流 / 群流，宿主为哈希值）。

    Returns:
        委托人昵称/标识；不构成转达时返回 None。
    """
    if not owner or not target:
        return None
    # owner 格式校验：platform:user_id（如 qq:10001）；unknown: 兜底前缀不判定
    parsed = Owner.parse(owner)
    if parsed is None:
        return None
    platform, owner_user = parsed
    # 反查目标流：session_id 为哈希，需匹配流对象才能拿到目标用户与聊天类型
    try:
        streams = await ctx.chat.get_all_streams(platform=platform)
    except Exception:
        logger.warning("自动转达判定：chat.get_all_streams 失败，按本人发言处理", exc_info=True)
        return None
    target_stream = next(
        (
            s for s in streams or []
            if str(s.get("session_id", "") or s.get("stream_id", "")) == target
        ),
        None,
    )
    if target_stream is None:
        return None
    # 群流无单一传出用户：不自动转达
    if bool(target_stream.get("is_group_session")) or str(
        target_stream.get("chat_type", "")
    ) == "group":
        return None
    target_user = str(target_stream.get("user_id", "") or "")
    if not target_user or target_user == owner_user:
        return None
    # 发起人昵称：反查发起人私聊流，兜底 owner 原文
    try:
        caller_stream = await ctx.chat.get_stream_by_user_id(owner_user, platform)
        nickname = str((caller_stream or {}).get("user_nickname", "") or "")
    except Exception:
        logger.warning("自动转达判定：反查发起人昵称失败，兜底 owner=%s", owner, exc_info=True)
        nickname = ""
    logger.info(
        "自动转达判定：目标用户 %s ≠ 发起人 %s，relay_from=%r",
        target_user, owner_user, nickname or owner,
    )
    return nickname or owner


async def resolve_auto_relay(ctx: Any, task: TaskRecord) -> str | None:
    """InstantExecutor 用包装：从任务取 owner 与 reply_target 做自动转达判定。

    仅当显式 ``relay_from`` 为空时调用（send_message 工具的显式转达优先）。
    """
    return await resolve_relay(ctx, task.owner or "", task.reply_target or "")
