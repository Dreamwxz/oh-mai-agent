"""oh_mai_agent.executor.agent_loop 的测试——mock LLM 工具调用循环、
ask_user 挂起/恢复、指令注入、历史持久化、失败处理。

回归测试：
  1. ask_user 回调可恢复（在调用 on_ask 之前创建 _resume_event）
  2. run() 在任务已处于 RUNNING 时跳过状态转换（调度器集成）
  3. 异常时转换为 FAILED
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from conftest import MockCtx, make_task
from oh_mai_agent.executor.agent_loop import AgentLoop
from oh_mai_agent.bus.messages import CommandKind, EventKind, TaskCommand
from oh_mai_agent.permission import Role
from oh_mai_agent.prompt.base import PromptContext
from oh_mai_agent.prompt.builders.agent_system import AgentSystemBuilder
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord, TaskStatus
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

async def _echo_handler(**kwargs) -> dict:
    return {"success": True, "echo": kwargs}


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def mock_ctx() -> MockCtx:
    return MockCtx()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="echo", description="Echo", parameters={"type": "object", "properties": {}},
        handler=_echo_handler, visibility="essential", min_role=Role.GUEST,
    ))
    return reg


@pytest.fixture
def completed_event() -> asyncio.Event:
    return asyncio.Event()


async def _on_completed(task: TaskRecord, event: asyncio.Event) -> None:
    event.set()


# ═══════════════════════════════════════════════════════════════════════════════
# 构建 Agent 系统提示词：build_agent_system_prompt → AgentSystemBuilder
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildAgentSystemPrompt:
    def test_build_without_pm_raises(self) -> None:
        task = make_task("t1", title="查询天气", intent="查询北京明天的天气")
        builder = AgentSystemBuilder()
        ctx = PromptContext(task=task)
        with pytest.raises(RuntimeError, match="PromptManager 未注入"):
            builder.build(ctx)


# ═══════════════════════════════════════════════════════════════════════════════
# AgentLoop — 基础循环
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLoopBasic:
    @pytest.mark.asyncio
    async def test_cancel_cleanup_falls_back_when_store_reload_fails(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = make_task("cancel-store-error", level=TaskLevel.AGENT)
        published: list[Any] = []

        async def failing_get(task_id: str) -> TaskRecord | None:
            raise RuntimeError("database unavailable")

        async def record_publish(event: Any) -> None:
            published.append(event)

        monkeypatch.setattr(store, "get", failing_get)
        monkeypatch.setattr(command_bus, "publish", record_publish)
        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            prompt_service=prompt_service,
        )

        async def cancel_during_llm(*args: Any, **kwargs: Any) -> dict[str, Any]:
            await loop._on_bus_command(
                TaskCommand(task_id=task.id, kind=CommandKind.CANCEL),
            )
            return {"success": True, "response": "cancelled", "tool_calls": []}

        mock_ctx.llm.generate_with_tools = cancel_during_llm
        await loop.run(task)

        assert task.status == TaskStatus.CANCELLED
        assert [event.kind for event in published] == [EventKind.CANCELLED]

    @pytest.mark.asyncio
    async def test_cancel_after_timeout_preserves_failed_state(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        task = make_task("timeout-cancel", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)
        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            prompt_service=prompt_service,
        )

        async def timeout_during_llm(*args: Any, **kwargs: Any) -> dict[str, Any]:
            persisted = await store.get(task.id)
            assert persisted is not None
            persisted.force(TaskStatus.FAILED, actor="scheduler", reason="timeout")
            await store.save(persisted)
            await loop._on_bus_command(
                TaskCommand(task_id=task.id, kind=CommandKind.CANCEL),
            )
            raise RuntimeError("LLM interrupted by timeout")

        mock_ctx.llm.generate_with_tools = timeout_during_llm
        await loop.run(task)

        persisted = await store.get(task.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_late_llm_response_does_not_process_after_timeout(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        task = make_task("late-response", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)
        executed = False

        async def late_tool(**kwargs: Any) -> dict[str, Any]:
            nonlocal executed
            executed = True
            return {"success": True}

        registry.register(ToolDefinition(
            name="late_tool", description="Late tool",
            parameters={"type": "object", "properties": {}},
            handler=late_tool, visibility="essential", min_role=Role.GUEST,
        ))
        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            prompt_service=prompt_service,
        )

        async def late_response(*args: Any, **kwargs: Any) -> dict[str, Any]:
            persisted = await store.get(task.id)
            assert persisted is not None
            persisted.force(TaskStatus.FAILED, actor="scheduler", reason="timeout")
            await store.save(persisted)
            await loop._on_bus_command(
                TaskCommand(task_id=task.id, kind=CommandKind.CANCEL),
            )
            return {
                "success": True,
                "response": "late",
                "tool_calls": [{
                    "id": "late-call",
                    "function": {"name": "late_tool", "arguments": "{}"},
                }],
            }

        mock_ctx.llm.generate_with_tools = late_response
        await loop.run(task)

        persisted = await store.get(task.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.FAILED
        assert executed is False
        assert await store.get_history(task.id) == []

    @pytest.mark.asyncio
    async def test_completes_with_no_tool_calls(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """LLM 返回空 tool_calls 时，Agent 直接完成。"""
        event = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING,
                         intent="简单任务")

        # LLM 返回最终回复，无工具调用
        mock_ctx.llm.set_tool_response("任务完成", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_single_tool_call(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """Agent 调用一次 echo 工具后完成。"""
        event = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        # 第 1 轮：调用 echo 工具
        # 第 2 轮：返回最终回复
        mock_ctx.llm.set_tool_response("calling echo", [
            {"id": "call-1", "function": {"name": "echo", "arguments": '{"msg":"hello"}'}}
        ])
        mock_ctx.llm.set_tool_response("done", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_persists_history(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """Agent 将每轮历史持久化到 store。"""
        event = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        mock_ctx.llm.set_tool_response("ok", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        history = await store.get_history("t1")
        assert len(history) > 0

    @pytest.mark.asyncio
    async def test_history_loaded_on_restore(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """运行已有历史的任务时，历史会被加载。"""
        # 预置一些历史记录
        await store.save(make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING))
        await store.append_history("t1", {
            "round": 0,
            "messages": [
                {"role": "system", "content": "sys prompt"},
                {"role": "user", "content": "existing context"},
            ],
            "llm_result": {"response": "", "tool_calls": []},
            "timestamp": datetime.now().isoformat(),
        })

        event = asyncio.Event()
        task = await store.get("t1")
        assert task is not None

        mock_ctx.llm.set_tool_response("response from restored context", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED

        # 重放把预置对话重建到 LLM prompt 中
        prompt = mock_ctx.llm.call_history[0]["prompt"]
        assert any(m.get("content") == "existing context" for m in prompt)

    @pytest.mark.asyncio
    async def test_restore_uses_last_round_only(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """回归测试：历史恢复只扩展最后一轮的消息，
        而不是所有轮次（防止 O(n²) 上下文膨胀）。

        预置 3 轮累计消息快照。恢复后，LLM 应只收到最后一轮的消息
        （加上新的系统消息）。
        """
        # 预置 3 轮历史，每轮累计消息逐轮增多。
        await store.save(make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING))
        await store.append_history("t1", {
            "round": 1,
            "messages": [
                {"role": "system", "content": "sys prompt"},
                {"role": "user", "content": "ctx0"},
            ],
            "llm_result": {"response": "resp0", "tool_calls": []},
            "timestamp": datetime.now().isoformat(),
        })
        await store.append_history("t1", {
            "round": 2,
            "messages": [
                {"role": "system", "content": "sys prompt"},
                {"role": "user", "content": "ctx0"},
                {"role": "assistant", "content": "resp0"},
            ],
            "llm_result": {"response": "resp1", "tool_calls": []},
            "timestamp": datetime.now().isoformat(),
        })
        await store.append_history("t1", {
            "round": 3,
            "messages": [
                {"role": "system", "content": "sys prompt"},
                {"role": "user", "content": "ctx0"},
                {"role": "assistant", "content": "resp0"},
                {"role": "user", "content": "ctx1"},
            ],
            "llm_result": {"response": "resp2", "tool_calls": []},
            "timestamp": datetime.now().isoformat(),
        })

        event = asyncio.Event()
        task = await store.get("t1")
        assert task is not None

        mock_ctx.llm.set_tool_response("restored context response", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        # 只恢复最后一轮时，LLM 调用时刻的 prompt 内容为
        # 最后一轮的累计快照（4 条消息）加上调用后追加的
        # assistant 回复：共 5 条。
        # （seed 会替换掉新的系统提示词——重放会精确重建内存中的
        # 消息状态，因此不会多出系统消息。）
        # 旧的错误行为会扩展所有轮次：1 + 2 + 3 + 4 = 10+ 条。
        prompt = mock_ctx.llm.call_history[0]["prompt"]
        assert len(prompt) == 5, (
            f"Expected 5 messages (4 last-round + 1 LLM response), "
            f"got {len(prompt)}. All-rounds restore would produce 10+."
        )

    @pytest.mark.asyncio
    async def test_restore_replays_incremental_rounds(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """增量存储恢复：第 1 轮 seed + 注入 + 后续 new_messages 增量
        按 id 顺序重放，重建精确的对话状态。"""
        await store.save(make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING))
        # 第 1 轮：完整 seed（messages 键）
        await store.append_history("t1", {
            "round": 1,
            "messages": [
                {"role": "system", "content": "sys prompt"},
                {"role": "user", "content": "ctx0"},
            ],
            "llm_result": {"response": "", "tool_calls": []},
            "timestamp": datetime.now().isoformat(),
        })
        # 注入条目穿插在轮次之间
        await store.append_history("t1", {
            "type": "injection", "instruction": "优先处理X",
            "timestamp": datetime.now().isoformat(),
        })
        # 第 2 轮：仅增量 delta（new_messages 键）
        await store.append_history("t1", {
            "round": 2,
            "new_messages": [{"role": "assistant", "content": "resp1"}],
            "llm_result": {"response": "resp1", "tool_calls": []},
            "timestamp": datetime.now().isoformat(),
        })

        event = asyncio.Event()
        task = await store.get("t1")
        assert task is not None

        mock_ctx.llm.set_tool_response("final", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        prompt = mock_ctx.llm.call_history[0]["prompt"]
        # seed(2) + 注入(1) + delta(1) + 最终追加的 assistant 回复(1) = 5
        assert len(prompt) == 5
        roles = [m.get("role") for m in prompt]
        assert roles == ["system", "user", "system", "assistant", "assistant"]
        assert "优先处理X" in prompt[2]["content"]
        assert prompt[3]["content"] == "resp1"

    @pytest.mark.asyncio
    async def test_cursor_watermark_updated(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """每追加一轮快照都会推进 metadata['_last_history_id']，
        它记录了持久化水位线（审计 / 未来增量恢复的锚点）。"""
        event = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        # 两轮工具调用后跟最终回复 → 追加 2 个快照
        mock_ctx.llm.set_tool_response("calling", [
            {"id": "call-1", "function": {"name": "echo", "arguments": '{"msg":"hi"}'}}
        ])
        mock_ctx.llm.set_tool_response("done", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        updated = await store.get("t1")
        assert updated is not None
        cursor = updated.metadata.get("_last_history_id")
        assert isinstance(cursor, int) and cursor > 0
        # 每轮快照均已持久化；cursor 指向最新一条
        hist = await store.get_history("t1")
        assert len(hist) == 2
        assert await store.get_history_after("t1", cursor) == []


# ═══════════════════════════════════════════════════════════════════════════════
# ask_user / 恢复回归测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLoopAskUser:
    @pytest.mark.asyncio
    async def test_cancel_before_ask_user_registration_does_not_wait_forever(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        task = make_task("cancel-before-ask", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            prompt_service=prompt_service,
        )
        loop._task = task

        await loop._on_bus_command(
            TaskCommand(task_id=task.id, kind=CommandKind.CANCEL),
        )
        result = await asyncio.wait_for(
            loop._handle_ask_user(task, {"question": "继续吗?"}),
            timeout=0.1,
        )

        assert result == {"success": False, "error": "cancelled"}
        await loop._finalize_cancelled(task)
        persisted = await store.get(task.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_ask_user_creates_resume_event_before_on_ask(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """回归测试：_resume_event 在调用 on_ask 之前创建，
        确保 on_ask 触发回复流程时 resume_from_wait 能找到该事件。"""
        resume_signal: dict = {}
        captured_loop: AgentLoop | None = None

        async def _on_ask(stream_id: str, question: str) -> None:
            e = captured_loop._resume_events.get("t1")  # type: ignore[union-attr]
            resume_signal["event_exists"] = e is not None

            # 模拟用户回复：保存回复并恢复任务
            task = await store.get("t1")
            assert task is not None
            task.metadata["_user_reply"] = "用户回复内容"
            await store.save(task)
            await command_bus.send(TaskCommand(
                task_id="t1", kind=CommandKind.RESUME_REPLY,
                payload={"reply": "用户回复内容"},
            ))

        event_done = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        # LLM 先调用 ask_user，第二轮返回最终回复
        ask_call = {"id": "call-ask", "function": {"name": "ask_user", "arguments": '{"question":"请确认"}'}}
        mock_ctx.llm.set_tool_response("问个问题", [ask_call])
        mock_ctx.llm.set_tool_response("收到回复，任务完成", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, prompt_service=prompt_service, on_ask=_on_ask,
            role_provider=lambda: Role.ADMIN,
        )
        captured_loop = loop
        await loop.run(task)

        assert resume_signal.get("event_exists") is True, (
            "Regression: _resume_event must exist before on_ask is called"
        )
        assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_ask_user_without_on_ask_callback(self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any) -> None:
        """未提供 on_ask 回调时，ask_user 也应能正常解决。"""
        event_done = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        ask_call = {"id": "call-ask", "function": {"name": "ask_user", "arguments": '{"question":"test"}'}}
        mock_ctx.llm.set_tool_response("question", [ask_call])
        mock_ctx.llm.set_tool_response("done", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, prompt_service=prompt_service, on_ask=None,  # 无回调
            role_provider=lambda: Role.ADMIN,
        )
        await loop.run(task)

        # 即使没有 on_ask 也应完成（事件会立即被置位）
        assert task.status == TaskStatus.COMPLETED

    def test_ask_user_serializable_bomb_repro(self) -> None:
        """回归测试（已知 bug）：在 metadata 中存放 asyncio.Event
        会导致 json.dumps(task.to_dict()) 抛出 TypeError。"""
        import json
        task = make_task("t1", level=TaskLevel.AGENT, title="t", intent="i",
                         owner="qq:1", stream_id="q:1", platform="qq",
                         status=TaskStatus.PENDING)
        task.metadata["_resume_event"] = asyncio.Event()
        try:
            json.dumps(task.to_dict(), ensure_ascii=False)
            # 若走到这里，说明事件居然可序列化——不符合预期。
            assert False, "Expected TypeError but json.dumps succeeded"
        except TypeError:
            pass  # 预期：Event 不可被 JSON 序列化

    @pytest.mark.asyncio
    async def test_ask_user_serializable_with_real_store(
        self, mock_ctx: MockCtx, registry: ToolRegistry, real_store: Any, prompt_service: Any, command_bus: Any,
    ) -> None:
        """使用真实 TaskStore 的完整 ask_user 链路——JSON 序列化不能
        抛出 TypeError（_resume_event 炸弹的回归测试）。"""
        await real_store.init()
        event_done = asyncio.Event()
        captured_loop: AgentLoop | None = None
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        # LLM：第一轮提问，第二轮给出最终回复
        ask_call = {"id": "call-ask", "function": {"name": "ask_user", "arguments": '{"question":"确认吗?"}'}}
        mock_ctx.llm.set_tool_response("question", [ask_call])
        mock_ctx.llm.set_tool_response("done", [])

        async def on_ask(stream_id: str, question: str) -> None:
            # 模拟用户回复：重新加载任务、保存回复、恢复任务
            fresh = await real_store.get("t1")
            assert fresh is not None
            fresh.metadata["_user_reply"] = "yes"
            await real_store.save(fresh)
            await command_bus.send(TaskCommand(
                task_id="t1", kind=CommandKind.RESUME_REPLY,
                payload={"reply": "yes"},
            ))

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=real_store,
            command_bus=command_bus, on_ask=on_ask,
            role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        captured_loop = loop
        await loop.run(task)

        updated = await real_store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED, (
            f"Expected COMPLETED, got {updated.status.value}"
        )
        # metadata 中绝不能包含 asyncio 对象
        assert "_resume_event" not in updated.metadata, (
            "Bug: _resume_event leaked into persisted metadata"
        )

    @pytest.mark.asyncio
    async def test_ask_user_creates_resume_event_real_store(
        self, mock_ctx: MockCtx, registry: ToolRegistry, real_store: Any, prompt_service: Any, command_bus: Any,
    ) -> None:
        """真实 SQLite：_resume_event 在调用 on_ask 之前创建。"""
        await real_store.init()
        resume_signal: dict = {}
        captured_loop: AgentLoop | None = None

        async def _on_ask(stream_id: str, question: str) -> None:
            e = captured_loop._resume_events.get("t1")  # type: ignore[union-attr]
            resume_signal["event_exists"] = e is not None

            task = await real_store.get("t1")
            assert task is not None
            task.metadata["_user_reply"] = "用户回复内容"
            await real_store.save(task)
            await command_bus.send(TaskCommand(
                task_id="t1", kind=CommandKind.RESUME_REPLY,
                payload={"reply": "用户回复内容"},
            ))

        event_done = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        ask_call = {"id": "call-ask", "function": {"name": "ask_user", "arguments": '{"question":"请确认"}'}}
        mock_ctx.llm.set_tool_response("问个问题", [ask_call])
        mock_ctx.llm.set_tool_response("收到回复，任务完成", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=real_store,
            command_bus=command_bus, on_ask=_on_ask,
            role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        captured_loop = loop
        await loop.run(task)

        assert resume_signal.get("event_exists") is True, (
            "Regression: _resume_event must exist before on_ask is called"
        )
        assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_ask_user_without_on_ask_callback_real_store(
        self, mock_ctx: MockCtx, registry: ToolRegistry, real_store: Any, prompt_service: Any, command_bus: Any,
    ) -> None:
        """真实 SQLite：未提供 on_ask 回调时，ask_user 仍能正常解决。"""
        await real_store.init()
        event_done = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        ask_call = {"id": "call-ask", "function": {"name": "ask_user", "arguments": '{"question":"test"}'}}
        mock_ctx.llm.set_tool_response("question", [ask_call])
        mock_ctx.llm.set_tool_response("done", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=real_store,
            command_bus=command_bus, on_ask=None,
            role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        assert task.status == TaskStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 指令注入
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLoopInjection:
    @pytest.mark.asyncio
    async def test_inject_instruction_consumed(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """在某轮之前注入的指令会在该轮被消费。"""
        event_done = asyncio.Event()

        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        await store.save(task)

        mock_ctx.llm.set_tool_response("processing injection", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )

        # 用一个独立任务运行循环，并在第一轮之前通过总线注入指令。
        async def _run_and_inject() -> None:
            run_task = asyncio.create_task(loop.run(task))
            await asyncio.sleep(0.02)  # 等待循环启动并完成注册
            await command_bus.send(TaskCommand(
                task_id="t1", kind=CommandKind.INJECT_INSTRUCTION,
                payload={"instruction": "新指令：请优先处理"},
            ))
            await run_task

        await _run_and_inject()

        assert task.status == TaskStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 失败 → FAILED 回归测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLoopFailure:
    @pytest.mark.asyncio
    async def test_exception_transitions_to_failed(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """循环执行中出现异常时，任务转为 FAILED。"""
        event_done = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        # 让第一次 LLM 调用抛出异常
        async def _broken(*args, **kwargs):
            raise RuntimeError("LLM failure")
        mock_ctx.llm.generate_with_tools = _broken

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        await loop.run(task)

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.FAILED
        assert "_error" in updated.metadata
        assert task.status == TaskStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 已处于 RUNNING 时跳过状态转换 — 回归测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLoopAlreadyRunning:
    @pytest.mark.asyncio
    async def test_run_skips_transition_if_already_running(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """回归测试：调度器已置为 RUNNING 时，AgentLoop.run() 跳过
        状态转换，而不是抛出 "running → running" 异常。"""
        event_done = asyncio.Event()
        # 任务已处于 RUNNING 状态（模拟调度器已置位）
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)

        mock_ctx.llm.set_tool_response("ok", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
        )
        # 不应抛出 TaskStatusError
        await loop.run(task)

        updated = await store.get("t1")
        assert updated.status == TaskStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 超过最大轮数
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLoopMaxRounds:
    @pytest.mark.asyncio
    async def test_max_rounds_exceeded_completes(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """超过 max_rounds 时，任务仍应正常完成。"""
        event_done = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        # 始终返回工具调用 → 死循环 → 触发 max_rounds
        for _ in range(5):
            mock_ctx.llm.set_tool_response("calling", [
                {"id": "c", "function": {"name": "echo", "arguments": '{}'}}
            ])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
            max_rounds=3,
        )
        await loop.run(task)

        assert task.status == TaskStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# send_final — 完成时发送结果，失败时发送原因
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLoopSendFinal:
    @pytest.mark.asyncio
    async def test_agent_completion_sends_final_result(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """Agent 任务完成时，用 Agent 的回复触发 send_final。"""
        event_done = asyncio.Event()
        sent: list[tuple[TaskRecord, str]] = []

        async def _send_final(task: TaskRecord, text: str) -> None:
            sent.append((task, text))

        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING,
                         intent="查看系统信息")
        mock_ctx.llm.set_tool_response("系统是 Linux，一切正常", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            send_final=_send_final, prompt_service=prompt_service,
        )
        await loop.run(task)

        assert task.status == TaskStatus.COMPLETED
        assert len(sent) == 1, f"Expected 1 send_final call, got {len(sent)}"
        assert "Linux" in sent[0][1], f"send_final text should contain result: {sent[0][1]}"

    @pytest.mark.asyncio
    async def test_agent_completion_send_final_fallback(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """Agent 最终回复为空时，send_final 使用兜底文案。"""
        event_done = asyncio.Event()
        sent: list[tuple[TaskRecord, str]] = []

        async def _send_final(task: TaskRecord, text: str) -> None:
            sent.append((task, text))

        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        mock_ctx.llm.set_tool_response("", [])  # 空回复

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            send_final=_send_final, prompt_service=prompt_service,
        )
        await loop.run(task)

        assert len(sent) == 1
        assert "任务完成" in sent[0][1]

    @pytest.mark.asyncio
    async def test_agent_failure_sends_reason(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """Agent 任务失败时，用错误原因触发 send_final。"""
        event_done = asyncio.Event()
        sent: list[tuple[TaskRecord, str]] = []

        async def _send_final(task: TaskRecord, text: str) -> None:
            sent.append((task, text))

        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        async def _broken(*args, **kwargs):
            raise RuntimeError("模型调用失败")
        mock_ctx.llm.generate_with_tools = _broken

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            send_final=_send_final, prompt_service=prompt_service,
        )
        await loop.run(task)

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.FAILED
        assert len(sent) == 1, f"Expected 1 send_final call on failure, got {len(sent)}"
        assert "失败" in sent[0][1], f"send_final failure text should mention 失败: {sent[0][1]}"
        assert "模型调用失败" in sent[0][1]

    @pytest.mark.asyncio
    async def test_agent_without_send_final_still_works(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry, prompt_service: Any, command_bus: Any,
    ) -> None:
        """send_final=None（默认）——向后兼容，不会崩溃。"""
        event_done = asyncio.Event()
        task = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        mock_ctx.llm.set_tool_response("done", [])

        async def on_comp(t: TaskRecord) -> None:
            await _on_completed(t, event_done)

        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN, prompt_service=prompt_service,
            # send_final=None（默认值）
        )
        await loop.run(task)

        assert task.status == TaskStatus.COMPLETED
        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_prompt_build_without_pm_raises(self) -> None:
        """未注入 pm 时 AgentSystemBuilder 抛出 RuntimeError。"""
        task = make_task("t1", title="T", intent="I")
        builder = AgentSystemBuilder()
        ctx = PromptContext(task=task)
        with pytest.raises(RuntimeError, match="PromptManager 未注入"):
            builder.build(ctx)


# ═══════════════════════════════════════════════════════════════════════════════
# F2 竞态修复回归测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestF2SendFinalWindow:
    """send_final 耗时窗口内的取消 / 超时不得被 COMPLETED 覆盖。"""

    @pytest.mark.asyncio
    async def test_cancel_during_send_final_preserves_cancelled(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        """send_final 期间收到 CANCEL → 任务保持 CANCELLED，不发布 COMPLETED。"""
        task = make_task("cancel-send-final", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)
        published: list[Any] = []
        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            prompt_service=prompt_service,
        )

        async def cancel_in_send_final(t: TaskRecord, text: str) -> None:
            # 模拟用户取消恰好在 send_final 长等待期间到达
            await loop._on_bus_command(
                TaskCommand(task_id=t.id, kind=CommandKind.CANCEL),
            )

        async def record_publish(event: Any) -> None:
            published.append(event)

        command_bus.publish = record_publish  # type: ignore[method-assign]
        mock_ctx.llm.set_tool_response("done", [])
        loop._send_final = cancel_in_send_final  # type: ignore[assignment]

        await loop.run(task)

        persisted = await store.get(task.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.CANCELLED
        assert not any(e.kind == EventKind.COMPLETED for e in published)

    @pytest.mark.asyncio
    async def test_timeout_during_send_final_preserves_failed(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        """send_final 期间调度器超时 FAILED → 任务保持 FAILED，不覆盖为 COMPLETED。"""
        task = make_task("timeout-send-final", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)
        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            prompt_service=prompt_service,
        )

        async def timeout_in_send_final(t: TaskRecord, text: str) -> None:
            persisted = await store.get(t.id)
            assert persisted is not None
            persisted.force(TaskStatus.FAILED, actor="scheduler", reason="timeout")
            await store.save(persisted)

        mock_ctx.llm.set_tool_response("done", [])
        loop._send_final = timeout_in_send_final  # type: ignore[assignment]

        await loop.run(task)

        persisted = await store.get(task.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.FAILED


class TestF2PauseFlagPreservation:
    """PAUSE 标记必须落在循环自有对象上，轮次保存不得擦除。"""

    @pytest.mark.asyncio
    async def test_pause_flag_persisted_on_loop_owned_task(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        """PAUSE 命令后，循环自有任务对象的 metadata 携带 _coop_paused，
        后续整记录保存（save(task)）不会擦除该标记。"""
        task = make_task("pause-flag", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)
        loop = AgentLoop(
            ctx=mock_ctx, registry=registry, store=store,
            command_bus=command_bus, role_provider=lambda: Role.ADMIN,
            prompt_service=prompt_service,
        )
        loop._task = task

        await loop._on_bus_command(
            TaskCommand(task_id=task.id, kind=CommandKind.PAUSE),
        )
        # 模拟一轮结束的整记录保存
        await loop._store.save(task)

        persisted = await store.get(task.id)
        assert persisted is not None
        assert persisted.metadata.get("_coop_paused") is True


class TestF2StoreGuard:
    """TaskStore.save(expected_status=...) 乐观锁守卫：终态不被旧快照覆盖。"""

    @pytest.mark.asyncio
    async def test_save_rejected_when_status_changed(
        self, store: TaskStore,
    ) -> None:
        """持久化状态已变为 FAILED 后，旧 RUNNING 快照的守卫保存被拒绝。"""
        task = make_task("guard-task", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)

        persisted = await store.get("guard-task")
        assert persisted is not None
        persisted.force(TaskStatus.FAILED, actor="scheduler", reason="timeout")
        await store.save(persisted)

        stale = make_task("guard-task", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        ok = await store.save(stale, expected_status=TaskStatus.RUNNING)
        assert ok is False

        after = await store.get("guard-task")
        assert after is not None
        assert after.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_save_succeeds_when_status_unchanged(
        self, store: TaskStore,
    ) -> None:
        """状态未变化时守卫保存正常执行。"""
        task = make_task("guard-ok", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)

        ok = await store.save(task, expected_status=TaskStatus.RUNNING)
        assert ok is True

        after = await store.get("guard-ok")
        assert after is not None
        assert after.status == TaskStatus.RUNNING
