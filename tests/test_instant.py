"""executor/instant.py 的测试 —— send_final_reply 重试检测、is_group 参数。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import MockCtx, make_task

from oh_mai_agent.config import MaibotAgentConfig
from oh_mai_agent.domain.task_record import TaskLevel, TaskStatus, TriggerType
from oh_mai_agent.executor.base import ExecutionContext, complete_and_notify
from oh_mai_agent.executor.instant import InstantExecutor, fail_task, send_final_reply
from oh_mai_agent.prompt.builders.context_note import ContextNoteBuilder
from oh_mai_agent.prompt.service import PromptService


def _make_prompt_service() -> PromptService:
    from pathlib import Path

    from oh_mai_agent.prompt.manager import PromptManager

    return PromptService(
        manager=PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates"),
        builders=[ContextNoteBuilder()],
    )


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
        exec_ctx = ExecutionContext(
            ctx=MockCtx(), store=store, scheduler=scheduler,
            config=MaibotAgentConfig(),
        )

        with patch("oh_mai_agent.executor.instant.send_final_reply", new_callable=AsyncMock) as send:
            await fail_task(task, store, scheduler, exec_ctx, send_message=True)

        send.assert_not_awaited()
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


class TestSendTextReturnsFalseTriggersRetry:
    @pytest.mark.asyncio
    async def test_returns_false_is_treated_as_failure(self) -> None:
        """当 ctx.send.text 返回 False 时，重试耗尽后抛出 RuntimeError。"""
        ctx = _RetryMockCtx(send_return=False)
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        with pytest.raises(RuntimeError, match="send.text returned False/None"):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                max_retries=3,
            )

        assert ctx.send_attempts == 3


class TestSendTextReturnsNoneTriggersRetry:
    @pytest.mark.asyncio
    async def test_returns_none_is_treated_as_failure(self) -> None:
        """当 ctx.send.text 返回 None 时，重试耗尽后抛出 RuntimeError。"""
        ctx = _RetryMockCtx(send_return=None)
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        with pytest.raises(RuntimeError, match="send.text returned False/None"):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                max_retries=3,
            )

        assert ctx.send_attempts == 3


class TestExplicitIsGroupTrue:
    @pytest.mark.asyncio
    async def test_is_group_true_passed_to_polish(self) -> None:
        """send_final_reply 收到 is_group=True 时，PolishService.polish 也收到 is_group=True。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        original_polish = __import__(
            "oh_mai_agent.executor.instant", fromlist=["PolishService"]
        ).PolishService.polish
        captured_is_group: bool | None = None

        async def fake_polish(self: Any, *, result: str, stream_id: str, is_group: bool, kind: str = "reply", requester: str = "") -> str:
            nonlocal captured_is_group
            captured_is_group = is_group
            return "润色后文本"

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                max_retries=1, is_group=True,
            )

        assert captured_is_group is True

    @pytest.mark.asyncio
    async def test_is_group_absent_derives_from_stream_id(self) -> None:
        """当 is_group 缺省时，从 stream_id 推导（群聊流）。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        captured_is_group: bool | None = None

        async def fake_polish(self: Any, *, result: str, stream_id: str, is_group: bool, kind: str = "reply", requester: str = "") -> str:
            nonlocal captured_is_group
            captured_is_group = is_group
            return "润色后文本"

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            # stream_id 包含 ":group:"
            await send_final_reply(
                "test", "qq:group:1", ctx, config, None,
                max_retries=1,
            )

        assert captured_is_group is True

    @pytest.mark.asyncio
    async def test_is_group_none_private_stream(self) -> None:
        """当 is_group 为 None 时，私聊流推导出 is_group=False。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        captured_is_group: bool | None = None

        async def fake_polish(self: Any, *, result: str, stream_id: str, is_group: bool, kind: str = "reply", requester: str = "") -> str:
            nonlocal captured_is_group
            captured_is_group = is_group
            return "润色后文本"

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await send_final_reply(
                "test", "qq:10001", ctx, config, None,
                max_retries=1, is_group=None,
            )

        assert captured_is_group is False


class TestExistingSendStillPasses:
    @pytest.mark.asyncio
    async def test_send_text_returns_true_one_call_success(self) -> None:
        """当 ctx.send.text 返回 True 时，一次调用即成功。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        async def fake_polish(self: Any, *, result: str, stream_id: str, is_group: bool, kind: str = "reply", requester: str = "") -> str:
            return "润色后文本"

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                max_retries=1,
            )

        assert len(ctx._sent_messages) == 1
        assert ctx._sent_messages[0]["text"] == "润色后文本"
        assert ctx._sent_messages[0]["stream_id"] == "qq:g:1"


class TestRelayKindPassThrough:
    @pytest.mark.asyncio
    async def test_kind_and_requester_passed_to_polish(self) -> None:
        """send_final_reply 收到 kind/requester 时，PolishService.polish 也收到相同值。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()
        captured: dict[str, Any] = {}

        async def fake_polish(
            self: Any,
            *,
            result: str,
            stream_id: str,
            is_group: bool,
            kind: str = "reply",
            requester: str = "",
        ) -> str:
            captured.update(kind=kind, requester=requester)
            return "润色后文本"

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                max_retries=1, kind="relay", requester="张三",
            )

        assert captured["kind"] == "relay"
        assert captured["requester"] == "张三"

    @pytest.mark.asyncio
    async def test_default_kind_reply_requester_empty(self) -> None:
        """不传 kind/requester 时，PolishService.polish 收到缺省 reply 与空串。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()
        captured: dict[str, Any] = {}

        async def fake_polish(
            self: Any,
            *,
            result: str,
            stream_id: str,
            is_group: bool,
            kind: str = "reply",
            requester: str = "",
        ) -> str:
            captured.update(kind=kind, requester=requester)
            return "润色后文本"

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                max_retries=1,
            )

        assert captured["kind"] == "reply"
        assert captured["requester"] == ""


class TestMotivationInjectsContextNote:
    @pytest.mark.asyncio
    async def test_motivation_produces_two_appends_pure_text_and_xml(self) -> None:
        """提供 motivation 时产生两次上下文追加：[0] 纯文本，[1] XML 备注。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        async def fake_polish(self: Any, *, result: str, stream_id: str, is_group: bool, kind: str = "reply", requester: str = "") -> str:
            return "润色后文本"

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                _make_prompt_service(),
                max_retries=1, motivation="因小泽委托",
            )

        assert len(ctx.maisaka.appends) == 2
        # [0] 纯文本记录（润色后文本，无 XML）
        pure = ctx.maisaka.appends[0]
        assert pure["visible_text"] == "润色后文本"
        assert "message_id" not in pure or pure.get("message_id") == ""
        assert pure["source_kind"] == "plugin:oh-mai-agent:task-reply"
        assert pure["stream_id"] == "qq:g:1"
        # [1] XML 动机备注
        note = ctx.maisaka.appends[1]
        vt: str = note["visible_text"]
        assert vt.startswith("<plugin_context_note"), f"visible_text should start with XML tag, got: {vt[:60]}"
        assert vt.endswith("</plugin_context_note>"), f"visible_text should end with XML close tag, got: {vt[-60:]}"
        assert "麦麦此前在此流发送了任务消息：因小泽委托" in vt
        assert "不是聊天对象发言" in vt
        assert note["message_id"].startswith("oh-mai-agent:task-note:")
        assert note["source_kind"] == "plugin:oh-mai-agent:task-reply"
        assert note["stream_id"] == "qq:g:1"

    @pytest.mark.asyncio
    async def test_motivation_none_produces_one_pure_text_append(self) -> None:
        """当 motivation 为 None 时，仅产生纯文本上下文追加（无 XML）。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        async def fake_polish(self: Any, *, result: str, stream_id: str, is_group: bool, kind: str = "reply", requester: str = "") -> str:
            return "润色后文本"

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                max_retries=1, motivation=None,
            )

        assert len(ctx.maisaka.appends) == 1
        entry = ctx.maisaka.appends[0]
        assert entry["visible_text"] == "润色后文本"
        assert "message_id" not in entry or entry.get("message_id") == ""
        assert entry["source_kind"] == "plugin:oh-mai-agent:task-reply"
        assert entry["stream_id"] == "qq:g:1"

    @pytest.mark.asyncio
    async def test_append_exception_does_not_propagate(self) -> None:
        """当 ctx.maisaka.context.append 抛异常时，函数仍正常返回。"""
        ctx = MockCtx()
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        async def fake_polish(self: Any, *, result: str, stream_id: str, is_group: bool, kind: str = "reply", requester: str = "") -> str:
            return "润色后文本"

        async def fake_append(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated append failure")

        ctx.maisaka.context.append = fake_append  # type: ignore[assignment]

        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            # 不应抛异常
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                _make_prompt_service(),
                max_retries=1, motivation="因小泽委托",
            )

        # 发送仍然成功
        assert len(ctx._sent_messages) == 1

    @pytest.mark.asyncio
    async def test_send_fails_all_retries_no_append(self) -> None:
        """当 send.text 返回 False 且重试耗尽时，不发生上下文追加。"""
        ctx = _RetryMockCtx(send_return=False)
        ctx.llm.set_generate_response("润色后文本")
        config = MaibotAgentConfig()

        with pytest.raises(RuntimeError, match="send.text returned False/None"):
            await send_final_reply(
                "test", "qq:g:1", ctx, config, None,
                max_retries=3, motivation="因小泽委托",
            )

        assert ctx.send_attempts == 3
        assert len(ctx.maisaka.appends) == 0


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
        """execute 在当前进程发送润色文本并完成任务。"""
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
        """execute 发送失败时重试失败消息并持久化 FAILED 状态。"""
        await real_store.init()
        scheduler = _InstantScheduler()
        task = make_task(
            task_id="instant-failure",
            intent="发送失败的意图",
            level=TaskLevel.INSTANT,
            status=TaskStatus.PENDING,
        )
        await real_store.save(task)
        calls = 0

        async def failing_first_send(intent: str, *args: Any, **kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("发送失败")
            mock_ctx._sent_messages.append({"text": intent, "stream_id": task.reply_target})

        exec_ctx = ExecutionContext(
            ctx=mock_ctx,
            store=real_store,
            scheduler=scheduler,
            config=default_config,
        )
        with patch("oh_mai_agent.executor.instant.send_final_reply", failing_first_send):
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
        )
        with patch("oh_mai_agent.executor.instant.PolishService.polish", fake_polish):
            result = await InstantExecutor().execute(exec_ctx, task)

        assert result.status == "COMPLETED"
        assert len(mock_ctx.maisaka.appends) == 2
        note = mock_ctx.maisaka.appends[1]
        assert "因小泽委托处理完毕" in note["visible_text"]
        assert note["stream_id"] == "qq:g:2"
        assert note["message_id"].startswith("oh-mai-agent:task-note:")
        assert note["source_kind"] == "plugin:oh-mai-agent:task-reply"


class TestRequesterResolution:
    @pytest.mark.asyncio
    async def test_resolve_requester_from_chat_streams(self, mock_ctx: MockCtx) -> None:
        executor = InstantExecutor()
        mock_ctx._chat_streams = [
            {"user_id": "10001", "user_nickname": "小泽", "user_cardname": "卡名"},
            {"user_id": "20002", "user_nickname": "阿绿"},
        ]
        assert await executor._resolve_requester(
            mock_ctx, make_task("r1", owner="qq:10001", platform="qq")
        ) == "小泽"
        assert await executor._resolve_requester(
            mock_ctx, make_task("r2", owner="qq:20002", platform="qq")
        ) == "阿绿"
        assert await executor._resolve_requester(
            mock_ctx, make_task("r3", owner="qq:99999", platform="qq")
        ) == ""
        assert await executor._resolve_requester(
            mock_ctx, make_task("r4", owner="", platform="qq")
        ) == ""

    @pytest.mark.asyncio
    async def test_resolve_requester_falls_back_to_cardname(self, mock_ctx: MockCtx) -> None:
        executor = InstantExecutor()
        mock_ctx._chat_streams = [
            {"user_id": "10001", "user_cardname": "卡名"},
        ]
        assert await executor._resolve_requester(
            mock_ctx, make_task("r5", owner="qq:10001", platform="qq")
        ) == "卡名"


# ═══════════════════════════════════════════════════════════════════════════════
# fail_task — 失败标记的容错分支
# ═══════════════════════════════════════════════════════════════════════════════

class TestFailTaskBranches:
    @pytest.mark.asyncio
    async def test_store_get_failure_tolerated(
        self, real_store: Any, mock_ctx: MockCtx,
    ) -> None:
        """store.get 抛异常 → 视为无持久化记录，继续标记 FAILED。"""
        from unittest.mock import AsyncMock

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
        """send_message=True 且发送失败 → 任务仍标记 FAILED。"""
        from unittest.mock import AsyncMock

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
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=store, scheduler=scheduler,
            config=MaibotAgentConfig(),
        )

        with patch("oh_mai_agent.executor.instant.send_final_reply",
                   AsyncMock(side_effect=RuntimeError("send down"))):
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
        from unittest.mock import AsyncMock

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


# ═══════════════════════════════════════════════════════════════════════════════
# InstantExecutor — 内部方法分支
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstantExecutorBranches:
    @pytest.mark.asyncio
    async def test_resolve_requester_exception_falls_back_empty(
        self, mock_ctx: MockCtx,
    ) -> None:
        """流列表查询异常 → 委托人为空串。"""
        mock_ctx.chat.get_all_streams = AsyncMock(side_effect=RuntimeError("chat down"))  # type: ignore[method-assign]
        executor = InstantExecutor()
        task = make_task("rq-1", owner="qq:10001")
        assert await executor._resolve_requester(mock_ctx, task) == ""

    @pytest.mark.asyncio
    async def test_resolve_requester_no_colon_owner(self, mock_ctx: MockCtx) -> None:
        executor = InstantExecutor()
        task = make_task("rq-2", owner="no-colon")
        assert await executor._resolve_requester(mock_ctx, task) == ""

    @pytest.mark.asyncio
    async def test_append_motivation_note_skips_without_cross_stream(
        self, mock_ctx: MockCtx,
    ) -> None:
        """非跨流回复（无 reply_stream_id 且非 is_reply_task）→ 不写动机注释。"""
        executor = InstantExecutor()
        task = make_task("mn-1")
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=AsyncMock(), scheduler=AsyncMock(),
            config=MaibotAgentConfig(), prompt_service=_make_prompt_service(),
        )
        await executor._append_motivation_note(exec_ctx, task)
        assert mock_ctx.maisaka.appends == []

    @pytest.mark.asyncio
    async def test_append_motivation_note_skips_without_prompt_service(
        self, mock_ctx: MockCtx,
    ) -> None:
        """无 prompt_service → 不写动机注释。"""
        executor = InstantExecutor()
        task = make_task("mn-2")
        task.mark_as_reply()
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=AsyncMock(), scheduler=AsyncMock(),
            config=MaibotAgentConfig(), prompt_service=None,
        )
        await executor._append_motivation_note(exec_ctx, task)
        assert mock_ctx.maisaka.appends == []

    @pytest.mark.asyncio
    async def test_append_motivation_note_append_failure_swallowed(
        self, mock_ctx: MockCtx,
    ) -> None:
        """上下文写入异常 → 仅记日志，不抛出。"""
        executor = InstantExecutor()
        task = make_task("mn-3")
        task.mark_as_reply()
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=AsyncMock(), scheduler=AsyncMock(),
            config=MaibotAgentConfig(), prompt_service=_make_prompt_service(),
        )
        with patch.object(mock_ctx.maisaka.context, "append",
                          AsyncMock(side_effect=RuntimeError("append down"))):
            await executor._append_motivation_note(exec_ctx, task)  # 不应抛异常
