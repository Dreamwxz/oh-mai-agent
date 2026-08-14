"""executor/instant.py 的测试 —— ReplySender 两条出口、重试、is_group 推导、动机注释。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import MockCtx, make_task

from oh_mai_agent.config import MaibotAgentConfig
from oh_mai_agent.domain.task_record import TaskLevel, TaskStatus, TriggerType
from oh_mai_agent.executor.base import ExecutionContext, complete_and_notify
from oh_mai_agent.executor.instant import InstantExecutor, ReplySender, fail_task
from oh_mai_agent.prompt.builders.context_note import ContextNoteBuilder
from oh_mai_agent.prompt.manager import PromptManager
from oh_mai_agent.prompt.service import PromptService


def _make_prompt_service() -> PromptService:
    return PromptService(
        manager=PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates"),
        builders=[ContextNoteBuilder()],
    )


def _make_sender(ctx: Any, config: MaibotAgentConfig, prompt_service: Any = None) -> ReplySender:
    return ReplySender(ctx=ctx, config_getter=lambda: config, prompt_service=prompt_service)


class TestCompleteAndNotifyRace:
    @pytest.mark.asyncio
    async def test_preserves_cancelled_terminal_state(self) -> None:
        task = make_task(status=TaskStatus.CANCELLED)
        store = AsyncMock()
        scheduler = AsyncMock()

        await complete_and_notify(task, store, scheduler)

        assert task.status == TaskStatus.CANCELLED
        store.save.assert_awaited_once_with(task)

    @pytest.mark.asyncio
    async def test_fail_task_preserves_cancelled_terminal_state(self) -> None:
        task = make_task(status=TaskStatus.CANCELLED)
        store = AsyncMock()
        scheduler = AsyncMock()
        exec_ctx = ExecutionContext(
            ctx=MockCtx(),
            store=store,
            scheduler=scheduler,
            config=MaibotAgentConfig(),
        )

        await fail_task(task, store, scheduler, exec_ctx)

        assert task.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_fail_task_does_not_send_for_terminal_persisted_task(self) -> None:
        task = make_task(status=TaskStatus.RUNNING)
        persisted = make_task(task_id=task.id, status=TaskStatus.FAILED)
        store = AsyncMock()
        store.get.return_value = persisted
        scheduler = AsyncMock()
        sender = AsyncMock()
        exec_ctx = ExecutionContext(
            ctx=MockCtx(), store=store, scheduler=scheduler,
            config=MaibotAgentConfig(), sender=sender,
        )

        await fail_task(task, store, scheduler, exec_ctx, send_message=True)

        sender.send_raw.assert_not_awaited()
        store.save.assert_not_awaited()
        scheduler.on_task_completed.assert_not_awaited()


class _RetryMockCtx(MockCtx):
    """MockCtx 变体：send.text 的返回值可配置。"""

    def __init__(self, send_return: bool | None = True) -> None:
        super().__init__()
        self._send_return = send_return
        self.send_attempts: int = 0

    class _RetrySendText:
        def __init__(self, ctx: "_RetryMockCtx") -> None:
            self._ctx = ctx

        async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool | None:
            self._ctx.send_attempts += 1
            return self._ctx._send_return

    @property
    def send(self):  # type: ignore[override]
        return self._RetrySendText(self)


class TestSendRawRetry:
    @pytest.mark.asyncio
    async def test_returns_false_is_treated_as_failure(self) -> None:
        """当 ctx.send.text 返回 False 时，重试耗尽后抛出 RuntimeError。"""
        ctx = _RetryMockCtx(send_return=False)
        config = MaibotAgentConfig()  # send.max_retries=3
        sender = _make_sender(ctx, config)

        with pytest.raises(RuntimeError, match="send.text returned False/None"):
            await sender.send_raw("test", "qq:group:1")

        assert ctx.send_attempts == 3

    @pytest.mark.asyncio
    async def test_returns_none_is_treated_as_failure(self) -> None:
        """当 ctx.send.text 返回 None 时，重试耗尽后抛出 RuntimeError。"""
        ctx = _RetryMockCtx(send_return=None)
        config = MaibotAgentConfig()
        sender = _make_sender(ctx, config)

        with pytest.raises(RuntimeError, match="send.text returned False/None"):
            await sender.send_raw("test", "qq:group:1")

        assert ctx.send_attempts == 3

    @pytest.mark.asyncio
    async def test_send_raw_success_one_call(self) -> None:
        """直发出口：原文直接发送，不做任何润色（不调用 LLM）。"""
        ctx = MockCtx()
        config = MaibotAgentConfig()
        sender = _make_sender(ctx, config)

        await sender.send_raw("原文内容", "qq:group:1")

        assert len(ctx._sent_messages) == 1
        assert ctx._sent_messages[0]["text"] == "原文内容"
        assert ctx._sent_messages[0]["stream_id"] == "qq:group:1"


class TestSendPolishedIsGroupDerivation:
    @pytest.mark.asyncio
    async def test_group_stream_derives_is_group_true(self) -> None:
        """流 ID 含 ":group:" 时，PolishService.polish 收到 is_group=True。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()
        captured: dict[str, Any] = {}

        async def fake_polish(
            self: Any, *, result: str, stream_id: str, is_group: bool, relay_from: str | None = None,
        ) -> str:
            captured.update(is_group=is_group)
            return "润色后文本"

        sender = _make_sender(ctx, config)
        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await sender.send_polished("test", "qq:group:1")

        assert captured["is_group"] is True

    @pytest.mark.asyncio
    async def test_private_stream_derives_is_group_false(self) -> None:
        """私聊流（无 ":group:" 段）推导出 is_group=False。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()
        captured: dict[str, Any] = {}

        async def fake_polish(
            self: Any, *, result: str, stream_id: str, is_group: bool, relay_from: str | None = None,
        ) -> str:
            captured.update(is_group=is_group)
            return "润色后文本"

        sender = _make_sender(ctx, config)
        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await sender.send_polished("test", "qq:10001")

        assert captured["is_group"] is False


class TestSendPolishedRelayFrom:
    @pytest.mark.asyncio
    async def test_relay_from_passed_to_polish(self) -> None:
        """relay_from 非空时，PolishService.polish 收到相同值（转达模式）。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()
        captured: dict[str, Any] = {}

        async def fake_polish(
            self: Any, *, result: str, stream_id: str, is_group: bool, relay_from: str | None = None,
        ) -> str:
            captured.update(relay_from=relay_from)
            return "润色后文本"

        sender = _make_sender(ctx, config)
        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await sender.send_polished("test", "qq:group:1", relay_from="张三")

        assert captured["relay_from"] == "张三"

    @pytest.mark.asyncio
    async def test_default_relay_from_none(self) -> None:
        """不传 relay_from 时，PolishService.polish 收到 None（本人发言）。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()
        captured: dict[str, Any] = {}

        async def fake_polish(
            self: Any, *, result: str, stream_id: str, is_group: bool, relay_from: str | None = None,
        ) -> str:
            captured.update(relay_from=relay_from)
            return "润色后文本"

        sender = _make_sender(ctx, config)
        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await sender.send_polished("test", "qq:group:1")

        assert captured["relay_from"] is None


class TestSendPolishedSends:
    @pytest.mark.asyncio
    async def test_send_polished_sends_polished_text(self) -> None:
        """完整出口：润色后的文本发送到目标流，且不做纯文本上下文追加。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        async def fake_polish(self: Any, **kwargs: Any) -> str:
            return "润色后文本"

        sender = _make_sender(ctx, config)
        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await sender.send_polished("test", "qq:group:1")

        assert len(ctx._sent_messages) == 1
        assert ctx._sent_messages[0]["text"] == "润色后文本"
        assert ctx._sent_messages[0]["stream_id"] == "qq:group:1"
        # 发送出口纯发送：不写任何上下文
        assert ctx.maisaka.appends == []


class TestAppendMotivationNote:
    @pytest.mark.asyncio
    async def test_content_produces_xml_note(self) -> None:
        """动机注释：写入一条 XML 上下文记录（对用户不可见）。"""
        ctx = MockCtx()
        config = MaibotAgentConfig()
        sender = _make_sender(ctx, config, _make_prompt_service())

        await sender.append_motivation_note("qq:g:1", "因小泽委托")

        assert len(ctx.maisaka.appends) == 1
        note = ctx.maisaka.appends[0]
        vt: str = note["visible_text"]
        assert vt.startswith("<plugin_context_note"), f"visible_text should start with XML tag, got: {vt[:60]}"
        assert vt.endswith("</plugin_context_note>"), f"visible_text should end with XML close tag, got: {vt[-60:]}"
        assert "因小泽委托" in vt
        assert note["message_id"].startswith("oh-mai-agent:task-note:")
        assert note["source_kind"] == "plugin:oh-mai-agent:task-reply"
        assert note["stream_id"] == "qq:g:1"

    @pytest.mark.asyncio
    async def test_empty_content_skipped(self) -> None:
        ctx = MockCtx()
        config = MaibotAgentConfig()
        sender = _make_sender(ctx, config, _make_prompt_service())

        await sender.append_motivation_note("qq:g:1", "")

        assert ctx.maisaka.appends == []

    @pytest.mark.asyncio
    async def test_without_prompt_service_skipped(self) -> None:
        ctx = MockCtx()
        config = MaibotAgentConfig()
        sender = _make_sender(ctx, config, prompt_service=None)

        await sender.append_motivation_note("qq:g:1", "因小泽委托")

        assert ctx.maisaka.appends == []

    @pytest.mark.asyncio
    async def test_append_exception_does_not_propagate(self) -> None:
        """当 ctx.maisaka.context.append 抛异常时，仅告警不抛出。"""
        ctx = MockCtx()
        config = MaibotAgentConfig()
        sender = _make_sender(ctx, config, _make_prompt_service())

        async def fake_append(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated append failure")

        ctx.maisaka.context.append = fake_append  # type: ignore[assignment]

        await sender.append_motivation_note("qq:g:1", "因小泽委托")  # 不应抛异常


class _InstantScheduler:
    def __init__(self) -> None:
        self.completed: list[str] = []

    async def on_task_completed(self, task: Any) -> None:
        self.completed.append(task.id)


class TestInstantExecutorExecute:
    @pytest.mark.asyncio
    async def test_execute_sends_polished_text_and_completes_task(
        self, mock_ctx: MockCtx, real_store: Any, default_config: MaibotAgentConfig,
    ) -> None:
        """execute 经 ReplySender.send_polished 发送润色文本并完成任务。"""
        await real_store.init()
        scheduler = _InstantScheduler()
        task = make_task(
            task_id="instant-execute",
            intent="原始意图",
            level=TaskLevel.INSTANT,
            status=TaskStatus.PENDING,
        )
        await real_store.save(task)

        async def fake_polish(self: Any, **kwargs: Any) -> str:
            return "润色后文本"

        exec_ctx = ExecutionContext(
            ctx=mock_ctx,
            store=real_store,
            scheduler=scheduler,
            config=default_config,
            sender=_make_sender(mock_ctx, default_config),
        )
        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            result = await InstantExecutor().execute(exec_ctx, task)

        saved = await real_store.get(task.id)
        assert result.status == "COMPLETED"
        assert mock_ctx._sent_messages[0]["text"] == "润色后文本"
        assert saved is not None
        assert saved.status == TaskStatus.COMPLETED
        assert scheduler.completed == [task.id]

    @pytest.mark.asyncio
    async def test_execute_failure_marks_task_failed_and_sends_failure_message(
        self, mock_ctx: MockCtx, real_store: Any, default_config: MaibotAgentConfig,
    ) -> None:
        """execute 发送失败时直发失败消息并持久化 FAILED 状态。"""
        await real_store.init()
        scheduler = _InstantScheduler()
        task = make_task(
            task_id="instant-failure",
            intent="发送失败的意图",
            level=TaskLevel.INSTANT,
            status=TaskStatus.PENDING,
        )
        await real_store.save(task)

        sender = AsyncMock()
        sender.send_polished = AsyncMock(side_effect=RuntimeError("发送失败"))
        sender.send_raw = AsyncMock(
            side_effect=lambda text, stream_id: mock_ctx._sent_messages.append(
                {"text": text, "stream_id": stream_id}
            )
        )

        exec_ctx = ExecutionContext(
            ctx=mock_ctx,
            store=real_store,
            scheduler=scheduler,
            config=default_config,
            sender=sender,
        )
        result = await InstantExecutor().execute(exec_ctx, task)

        saved = await real_store.get(task.id)
        assert result.status == "FAILED"
        assert mock_ctx._sent_messages[0]["text"] == "任务执行失败: 发送失败"
        assert saved is not None
        assert saved.status == TaskStatus.FAILED
        assert scheduler.completed == [task.id]

    @pytest.mark.asyncio
    async def test_execute_cross_stream_appends_motivation_note(
        self, mock_ctx: MockCtx, real_store: Any, default_config: MaibotAgentConfig,
    ) -> None:
        """跨流回复：发送后追加动机 XML 注释（发送出口本身不写纯文本）。"""
        await real_store.init()
        mock_ctx.llm.set_generate_response("润色后文本")
        scheduler = _InstantScheduler()
        task = make_task(
            task_id="instant-cross-stream",
            intent="因小泽委托处理完毕",
            level=TaskLevel.INSTANT,
            reply_stream_id="qq:g:2",
            status=TaskStatus.PENDING,
        )
        await real_store.save(task)

        async def fake_polish(self: Any, **kwargs: Any) -> str:
            return "润色后文本"

        exec_ctx = ExecutionContext(
            ctx=mock_ctx,
            store=real_store,
            scheduler=scheduler,
            config=default_config,
            prompt_service=_make_prompt_service(),
            sender=_make_sender(mock_ctx, default_config, _make_prompt_service()),
        )
        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            result = await InstantExecutor().execute(exec_ctx, task)

        assert result.status == "COMPLETED"
        # 仅一条 XML 动机注释（发送出口纯发送，无纯文本追加）
        assert len(mock_ctx.maisaka.appends) == 1
        note = mock_ctx.maisaka.appends[0]
        assert "因小泽委托处理完毕" in note["visible_text"]
        assert note["stream_id"] == "qq:g:2"
        assert note["message_id"].startswith("oh-mai-agent:task-note:")
        assert note["source_kind"] == "plugin:oh-mai-agent:task-reply"

    @pytest.mark.asyncio
    async def test_execute_without_sender_raises_failed(
        self, mock_ctx: MockCtx, real_store: Any, default_config: MaibotAgentConfig,
    ) -> None:
        """ExecutionContext 缺少 sender 时，任务标记 FAILED 且不崩溃。"""
        await real_store.init()
        scheduler = _InstantScheduler()
        task = make_task(
            task_id="instant-no-sender",
            intent="无 sender 的意图",
            level=TaskLevel.INSTANT,
            status=TaskStatus.PENDING,
        )
        await real_store.save(task)

        exec_ctx = ExecutionContext(
            ctx=mock_ctx,
            store=real_store,
            scheduler=scheduler,
            config=default_config,
        )
        result = await InstantExecutor().execute(exec_ctx, task)

        saved = await real_store.get(task.id)
        assert result.status == "FAILED"
        assert saved is not None
        assert saved.status == TaskStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# fail_task — 失败标记的容错分支
# ═══════════════════════════════════════════════════════════════════════════════

class TestFailTaskBranches:
    @pytest.mark.asyncio
    async def test_store_get_failure_tolerated(
        self, real_store: Any, mock_ctx: MockCtx,
    ) -> None:
        """store.get 抛异常 → 视为无持久化记录，继续标记 FAILED。"""
        from oh_mai_agent.core.scheduler import TaskScheduler
        from oh_mai_agent.config import TaskConfig

        store = real_store
        await store.init()
        scheduler = TaskScheduler(
            TaskConfig(max_concurrent_tasks=2), store,
            lambda t: asyncio.sleep(0), command_bus=AsyncMock(),
        )
        scheduler.on_task_completed = AsyncMock()

        task = make_task("ft-1", status=TaskStatus.RUNNING)
        await store.save(task)
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=store, scheduler=scheduler,
            config=MaibotAgentConfig(),
        )

        real_get = store.get
        store.get = AsyncMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]
        try:
            await fail_task(task, store, scheduler, exec_ctx)
        finally:
            store.get = real_get  # type: ignore[method-assign]

        persisted = await store.get("ft-1")
        assert persisted is not None
        assert persisted.status == TaskStatus.FAILED
        scheduler.on_task_completed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_message_failure_does_not_block(
        self, real_store: Any, mock_ctx: MockCtx,
    ) -> None:
        """send_message=True 且直发失败 → 任务仍标记 FAILED。"""
        from oh_mai_agent.core.scheduler import TaskScheduler
        from oh_mai_agent.config import TaskConfig

        store = real_store
        await store.init()
        scheduler = TaskScheduler(
            TaskConfig(max_concurrent_tasks=2), store,
            lambda t: asyncio.sleep(0), command_bus=AsyncMock(),
        )
        scheduler.on_task_completed = AsyncMock()

        task = make_task("ft-2", status=TaskStatus.RUNNING)
        task.set_error("炸了")
        await store.save(task)
        sender = AsyncMock()
        sender.send_raw = AsyncMock(side_effect=RuntimeError("send down"))
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=store, scheduler=scheduler,
            config=MaibotAgentConfig(), sender=sender,
        )

        await fail_task(task, store, scheduler, exec_ctx, send_message=True)

        persisted = await store.get("ft-2")
        assert persisted is not None
        assert persisted.status == TaskStatus.FAILED
        scheduler.on_task_completed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_illegal_transition_falls_back_to_force(
        self, real_store: Any, mock_ctx: MockCtx,
    ) -> None:
        """非法状态转换（SCHEDULED → FAILED）→ force 兜底仍标记 FAILED。"""
        from oh_mai_agent.core.scheduler import TaskScheduler
        from oh_mai_agent.config import TaskConfig

        store = real_store
        await store.init()
        scheduler = TaskScheduler(
            TaskConfig(max_concurrent_tasks=2), store,
            lambda t: asyncio.sleep(0), command_bus=AsyncMock(),
        )
        scheduler.on_task_completed = AsyncMock()

        task = make_task(
            "ft-3", status=TaskStatus.SCHEDULED, trigger_type=TriggerType.DELAY,
        )
        await store.save(task)
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=store, scheduler=scheduler,
            config=MaibotAgentConfig(),
        )
        await fail_task(task, store, scheduler, exec_ctx)

        persisted = await store.get("ft-3")
        assert persisted is not None
        assert persisted.status == TaskStatus.FAILED
