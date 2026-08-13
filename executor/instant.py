"""Instant 执行器 —— 润色、分割并发送意图。

从 ``TaskManager.execute_instant`` 迁移而来。Instant 任务是最简单的单步即时动作：
不需要 LLM 推理、不需要工具调用、不涉及状态机 —— 意图本身就是要发送的消息，
只需经过 PolishService 润色后按行/句切分（长回复拆成多条，见 ``splitter.py``）
直发到目标聊天流，任务即算完成。

设计要点：Instant 是三级执行体系中最轻量的执行器，零等待、零并发控制，
创建后立刻完成。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from ..config import PolishConfig
from ..domain.task_record import TaskRecord, TaskStatus, TaskStatusError
from ..prompt.manager import PromptManager
from .base import ExecutionContext, ExecutionResult, complete_and_notify
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
    用于 instant/agent 任务结果回复。
    """

    def __init__(
        self,
        *,
        ctx: Any,
        config: PolishConfig,
        use_jargon: bool = True,
        prompt_manager: PromptManager | None = None,
        prompt_service: Any | None = None,
    ):
        self.ctx = ctx
        self.config = config
        self.use_jargon = use_jargon
        self._pm = prompt_manager
        self._prompt_service = prompt_service

    # ── 公开接口 ────────────────────────────────────────────────────────────

    async def polish(
        self,
        *,
        result: str,
        stream_id: str,
        is_group: bool,
        kind: str = "reply",
        requester: str = "",
    ) -> str:
        """润色任务结果回复，结合上下文和黑话感知。

        流程：
        1. 获取目标聊天流的最近消息。
        2. 对消息做机械黑话匹配。
        3. 构建含上下文和黑话的 system prompt。
        4. 调用 LLM 润色，将 *result* 作为 user 消息传入。
        5. 任何异常时返回原始 *result*（绝不阻塞消息发送）。

        Args:
            result: 待润色的原始任务结果文本。
            stream_id: 目标聊天流 ID。
            is_group: 目标流是否为群聊。
            kind: 润色模式 reply/relay（默认 reply，转发他人之言用 relay）。
            requester: 转达委托人（仅 relay 模式有意义，缺省空串）。

        Returns:
            润色后的回复文本；失败时返回原始 *result*。
        """
        logger.debug("开始润色回复：流=%s kind=%s", stream_id, kind)
        try:
            context_texts = await self._load_context(stream_id, is_group)

            jargons: list[dict[str, str]] = []
            if self.use_jargon:
                jargons = await self._match_jargons(stream_id, context_texts)

            context_preview = (
                "\n".join(context_texts[-20:]) if context_texts else "（无最近聊天记录）"
            )
            system_prompt = self._prompt_service.build(
                "polish",
                jargon=jargons,
                context=context_preview,
                result=result,
                kind=kind,
                requester=requester,
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


# ── send_final_reply（含指数退避重试） ───────────────────────────────────────


async def send_final_reply(
    text: str,
    stream_id: str,
    ctx: Any,
    config: Any,
    prompt_manager: Any | None,
    prompt_service: Any | None = None,
    *,
    max_retries: int = 3,
    is_group: bool | None = None,
    motivation: str | None = None,
    kind: str = "reply",
    requester: str = "",
    polish: bool = True,
    split: bool | None = None,
) -> None:
    """润色、分割并发送任务的最终回复到目标聊天流（含指数退避重试）。

    润色步骤（PolishService）只执行一次，有自身回退逻辑。
    分割步骤在润色之后进行：长回复经 ``split_message`` 按行/句切成多条，
    最多 ``config.splitter.max_messages`` 条（``config.splitter.enable=False``
    时整条发送）。
    发送步骤（ctx.send.text）逐段进行，每段在失败时指数退避重试：
      1s → 2s（max_retries 次），任一段全部失败即停止后续分段并抛出最后异常。
    检测 SDK 静默掉包：ctx.send.text 返回 False/None 视为失败并重试。

    Args:
        text: 待润色和发送的原始文本。
        stream_id: 目标聊天流 ID。
        ctx: MaiBot PluginContext。
        config: MaibotAgentConfig（含 ``.polish`` 与 ``.splitter`` 子配置）。
        prompt_manager: Prompt 管理器（可选）。
        max_retries: ctx.send.text 最大重试次数（默认 3）。
        is_group: 显式指定是否为群聊；None 时从 stream_id 推导。
        motivation: 任务动机文本。非空且 prompt_service 可用时，全部分段
                    发送成功后，将 XML 上下文注释写入目标聊天流
                    （通过 ctx.maisaka.context.append）。
        kind: 润色模式 reply/relay，透传给 PolishService.polish（默认 reply）。
        requester: 转达委托人，透传给 PolishService.polish（缺省空串）。
        polish: 是否执行 LLM 润色（默认 True）。False 时跳过 PolishService
                直发原文——发送代码、命令或结构化文本等不希望被改写的内容时使用。
        split: 是否分割长文本（默认 None）。None 跟随 ``config.splitter.enable``；
               True/False 强制开启/关闭分割（发送方按场景覆盖全局配置）。
    """
    # 润色步骤 — 仅执行一次，PolishService 自身有回退逻辑
    if polish:
        svc = PolishService(
            ctx=ctx,
            config=config.polish,
            prompt_manager=prompt_manager,
            prompt_service=prompt_service,
        )
        polished = await svc.polish(
            result=text,
            stream_id=stream_id,
            is_group=is_group if is_group is not None else (":group:" in stream_id),
            kind=kind,
            requester=requester,
        )
    else:
        polished = text

    # 分割步骤 — 长回复按行/句切分（复刻 MaiBot response_splitter 思路）
    splitter = getattr(config, "splitter", None)
    use_split = split if split is not None else (splitter is not None and splitter.enable)
    if use_split:
        segments = split_message(
            polished,
            max_length=splitter.max_length,
            max_messages=splitter.max_messages,
        ) or [polished]
    else:
        segments = [polished]
    if len(segments) > 1:
        logger.debug(
            "回复文本分割为 %d 段（共 %d 字符），流=%s",
            len(segments), len(polished), stream_id,
        )

    # 发送步骤 — 逐段指数退避重试，任一段耗尽重试即停止后续分段
    last_exc: Exception | None = None
    for segment in segments:
        for attempt in range(max_retries):
            try:
                logger.debug(
                    "发送回复第 %d/%d 次尝试：流=%s（本次退避间隔 %ds）",
                    attempt + 1, max_retries, stream_id, 2 ** attempt,
                )
                result = await ctx.send.text(segment, stream_id)
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
        # 成功发送后，向上下文注入记录（每段一条纯文本记录，无 XML）
        try:
            await ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": segment}],
                visible_text=segment,
                source_kind="plugin:oh-mai-agent:task-reply",
            )
        except Exception:
            logger.warning("纯文本上下文记录写入失败，流=%s", stream_id, exc_info=True)
    else:
        # 全部分段发送成功
        # XML 动机注释（仅当 motivation 非空且 prompt_service 可用）
        if motivation and prompt_service is not None:
            try:
                note_id = f"oh-mai-agent:task-note:{int(time.time() * 1000)}"
                note_text = prompt_service.build(
                    "context_note",
                    kind="task-reply",
                    content=motivation,
                    id=note_id,
                )
                await ctx.maisaka.context.append(
                    stream_id=stream_id,
                    segments=[{"type": "text", "content": note_text}],
                    visible_text=note_text,
                    message_id=note_id,
                    source_kind="plugin:oh-mai-agent:task-reply",
                )
            except Exception:
                logger.warning("XML 动机注释写入失败，流=%s", stream_id, exc_info=True)
        return

    # 某段重试耗尽 —— 抛出最后异常交由上层标记 FAILED
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

    当 *send_message* 为 True 时，在状态变更前先向目标聊天流发送润色后的失败消息。
    消息发送使用 ``send_final_reply``（含指数退避重试），发送失败不影响任务状态更新。

    Args:
        task: 待标记为失败的任务。
        store: TaskStore 持久化。
        scheduler: TaskScheduler 通知。
        exec_ctx: 执行上下文（含 ctx、config、prompt_manager）。
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
        error_reason = task.metadata.get("_error") or "任务执行失败"
        fail_text = f"任务执行失败: {error_reason}"
        try:
            await send_final_reply(
                fail_text, task.reply_target, exec_ctx.ctx, exec_ctx.config,
                exec_ctx.prompt_manager,
                exec_ctx.prompt_service,
            )
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


# ── InstantExecutor ──────────────────────────────────────────────────────────


class InstantExecutor:
    """执行 Instant 任务：在当前进程中润色并发送。

    Instant 任务为简单的单步即时动作 —— 意图即消息，无 LLM 推理和工具调用。
    执行经 ``send_final_reply`` 完成，随后持久化完成状态并通知调度器。
    """

    async def execute(self, exec_ctx: ExecutionContext, task: TaskRecord) -> ExecutionResult:
        """润色并发送任务意图，然后完成或失败任务。"""
        try:
            requester = await self._resolve_requester(exec_ctx.ctx, task)
            await send_final_reply(
                task.intent,
                task.reply_target,
                exec_ctx.ctx,
                exec_ctx.config,
                exec_ctx.prompt_manager,
                exec_ctx.prompt_service,
                max_retries=3,
                kind="reply",
                requester=requester,
            )
            if task.reply_stream_id is not None or bool(task.metadata.get("_is_reply")):
                await self._append_motivation_note(exec_ctx, task)
            fresh_task = await exec_ctx.store.get(task.id)
            if fresh_task is None or not fresh_task.is_terminal():
                await complete_and_notify(task, exec_ctx.store, exec_ctx.scheduler)
            logger.info("Instant 任务 %s 执行成功完成", task.id)
            return ExecutionResult(status="COMPLETED", message="Instant done")
        except Exception as exc:
            logger.exception("Instant 任务 %s 执行失败", task.id)
            failure_task = task
            try:
                persisted_task = await exec_ctx.store.get(task.id)
            except Exception:
                persisted_task = None
            if persisted_task is not None:
                failure_task = persisted_task
            failure_task.metadata["_error"] = str(exc)
            await fail_task(
                failure_task,
                exec_ctx.store,
                exec_ctx.scheduler,
                exec_ctx,
                send_message=True,
            )
            return ExecutionResult(status="FAILED", message=str(exc), error=str(exc))

    # ── 内部：requester 解析 / 动机注释 ──────────────────────────────────

    async def _resolve_requester(self, ctx: Any, task: TaskRecord) -> str:
        """从 ``task.owner`` 解析转达委托人展示名。

        经 ``ctx.chat.get_all_streams`` 匹配 user_id 后取
        ``user_nickname`` / ``user_cardname``；任何失败回退空串。
        """
        try:
            owner = task.owner or ""
            if not owner or ":" not in owner:
                return ""
            user_id = owner.split(":", 1)[1]
            streams = await ctx.chat.get_all_streams(task.platform or "qq")
            for stream in streams:
                if stream.get("user_id") == user_id:
                    return (
                        stream.get("user_nickname")
                        or stream.get("user_cardname")
                        or ""
                    )
        except Exception:
            logger.warning(
                "解析转达委托人失败（回退空串）：task=%s", task.id, exc_info=True,
            )
        return ""

    async def _append_motivation_note(self, exec_ctx: ExecutionContext, task: TaskRecord) -> None:
        """跨流回复的动机 XML 上下文注释（父进程侧补齐）。

         ``send_final_reply`` 不携带 motivation（发送侧只读），
         跨流回复（``reply_stream_id`` 或 ``_is_reply``）
        的动机注释由父进程在 completed 事件后直接写入——与迁移前
        ``send_final_reply`` 的 motivation 分支语义一致（同模板、同记录）。
        """
        if task.reply_stream_id is None and not bool(task.metadata.get("_is_reply")):
            return
        prompt_service = exec_ctx.prompt_service
        if prompt_service is None:
            return
        try:
            note_id = f"oh-mai-agent:task-note:{int(time.time() * 1000)}"
            note_text = prompt_service.build(
                "context_note",
                kind="task-reply",
                content=task.intent,
                id=note_id,
            )
            await exec_ctx.ctx.maisaka.context.append(
                stream_id=task.reply_target,
                segments=[{"type": "text", "content": note_text}],
                visible_text=note_text,
                message_id=note_id,
                source_kind="plugin:oh-mai-agent:task-reply",
            )
        except Exception:
            logger.warning(
                "XML 动机注释写入失败，流=%s", task.reply_target, exc_info=True,
            )
