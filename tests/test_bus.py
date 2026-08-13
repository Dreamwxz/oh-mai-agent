"""oh_mai_agent.bus 的测试——消息、transport、命令总线。"""

from __future__ import annotations

import asyncio
import json

import pytest

from oh_mai_agent.bus import (
    CommandKind,
    EventKind,
    LoopbackTransport,
    TaskCommand,
    TaskCommandBus,
    TaskEvent,
)
from oh_mai_agent.bus.messages import decode_frame


# ═══════════════════════════════════════════════════════════════════════
# 测试辅助
# ═══════════════════════════════════════════════════════════════════════


def _json_dumps(value: object) -> str:
    return json.dumps(value, default=str)


# ═══════════════════════════════════════════════════════════════════════
# 消息 — 往返
# ═══════════════════════════════════════════════════════════════════════


class TestTaskCommandRoundtrip:
    """给定 TaskCommand，经 to_dict 再 from_dict 后，
    结果与原始对象一致（type、task_id、kind、payload）。"""

    def test_roundtrip_inject_instruction(self) -> None:
        cmd = TaskCommand(
            task_id="task-001",
            kind=CommandKind.INJECT_INSTRUCTION,
            payload={"instruction": "stop immediately"},
        )
        data = cmd.to_dict()

        # 必须是合法的 JSON
        raw = _json_dumps(data)
        parsed = json.loads(raw)
        assert parsed["type"] == "command"
        assert parsed["task_id"] == "task-001"

        # 往返还原
        restored = TaskCommand.from_dict(data)
        assert restored.task_id == cmd.task_id
        assert restored.kind == cmd.kind
        assert restored.payload == cmd.payload

    def test_roundtrip_cancel(self) -> None:
        cmd = TaskCommand(task_id="t2", kind=CommandKind.CANCEL)
        restored = TaskCommand.from_dict(cmd.to_dict())
        assert restored.task_id == "t2"
        assert restored.kind == CommandKind.CANCEL
        assert restored.payload == {}

    def test_roundtrip_all_command_kinds(self) -> None:
        for kind in CommandKind:
            cmd = TaskCommand(task_id="t", kind=kind, payload={"x": 1})
            restored = TaskCommand.from_dict(cmd.to_dict())
            assert restored.kind == kind


class TestTaskEventRoundtrip:
    def test_roundtrip_completed(self) -> None:
        evt = TaskEvent(
            task_id="task-001",
            kind=EventKind.COMPLETED,
            payload={"result": "done"},
        )
        data = evt.to_dict()
        raw = _json_dumps(data)
        parsed = json.loads(raw)
        assert parsed["type"] == "event"

        restored = TaskEvent.from_dict(data)
        assert restored.task_id == evt.task_id
        assert restored.kind == evt.kind
        assert restored.payload == evt.payload

    def test_roundtrip_all_event_kinds(self) -> None:
        for kind in EventKind:
            evt = TaskEvent(task_id="t", kind=kind)
            restored = TaskEvent.from_dict(evt.to_dict())
            assert restored.kind == kind


class TestDecodeFrame:
    """给定原始 frame 字节，调用 decode_frame 后，
    返回正确类型的消息。"""

    def test_decode_command_frame(self) -> None:
        cmd = TaskCommand(task_id="t1", kind=CommandKind.INJECT_INSTRUCTION)
        frame = _json_dumps(cmd.to_dict()).encode()
        result = decode_frame(frame)
        assert isinstance(result, TaskCommand)
        assert result.task_id == "t1"

    def test_decode_event_frame(self) -> None:
        evt = TaskEvent(task_id="t1", kind=EventKind.COMPLETED)
        frame = _json_dumps(evt.to_dict()).encode()
        result = decode_frame(frame)
        assert isinstance(result, TaskEvent)
        assert result.task_id == "t1"

    def test_decode_unknown_type_raises(self) -> None:
        frame = b'{"type":"bogus","task_id":"t1"}'
        with pytest.raises(ValueError, match="Unknown message type"):
            decode_frame(frame)

    def test_decode_missing_type_raises(self) -> None:
        frame = b'{"task_id":"t1"}'
        with pytest.raises(ValueError, match="Unknown message type"):
            decode_frame(frame)

    def test_decode_non_json_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
            decode_frame(b"not-json!!!!")


# ═══════════════════════════════════════════════════════════════════════
# 传输层 — LoopbackTransport
# ═══════════════════════════════════════════════════════════════════════


class TestLoopbackTransport:
    """给定 LoopbackTransport，先 send 再 receive，
    返回相同的字节；close 之后 receive 返回 None。"""

    @pytest.mark.asyncio
    async def test_send_receive_single_frame(self) -> None:
        t = LoopbackTransport()
        await t.send(b"hello-world")
        result = await t.receive()
        assert result == b"hello-world"

    @pytest.mark.asyncio
    async def test_send_receive_multiple_frames(self) -> None:
        t = LoopbackTransport()
        await t.send(b"frame-1")
        await t.send(b"frame-2")
        await t.send(b"frame-3")
        assert await t.receive() == b"frame-1"
        assert await t.receive() == b"frame-2"
        assert await t.receive() == b"frame-3"

    @pytest.mark.asyncio
    async def test_close_sends_none_sentinel(self) -> None:
        t = LoopbackTransport()
        await t.close()
        result = await t.receive()
        assert result is None

    @pytest.mark.asyncio
    async def test_send_after_close_is_noop(self) -> None:
        t = LoopbackTransport()
        await t.close()
        await t.send(b"should-not-appear")
        # close() 已入队哨兵 None，先消费掉它。
        assert await t.receive() is None
        # 关闭并消费哨兵后，receive 立即返回 None，
        # 因为已关闭且队列为空。
        assert await t.receive() is None


# ═══════════════════════════════════════════════════════════════════════
# TaskCommandBus 命令总线
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def transport() -> LoopbackTransport:
    return LoopbackTransport()


@pytest.fixture
def bus(transport: LoopbackTransport) -> TaskCommandBus:
    return TaskCommandBus(transport)


class TestTaskCommandBusSend:
    """给定带订阅者的 TaskCommandBus，调用 send 后，
    handler 会收到该命令。"""

    @pytest.mark.asyncio
    async def test_subscriber_receives_command(
        self, bus: TaskCommandBus,
    ) -> None:
        received: list[TaskCommand] = []

        async def handler(cmd: TaskCommand) -> None:
            received.append(cmd)

        bus.subscribe("task-001", handler)
        cmd = TaskCommand(
            task_id="task-001",
            kind=CommandKind.INJECT_INSTRUCTION,
            payload={"instruction": "reset"},
        )
        ok = await bus.send(cmd)
        assert ok
        assert len(received) == 1
        assert received[0].task_id == "task-001"
        assert received[0].kind == CommandKind.INJECT_INSTRUCTION
        assert received[0].payload == {"instruction": "reset"}

    @pytest.mark.asyncio
    async def test_send_writes_to_transport(
        self, bus: TaskCommandBus, transport: LoopbackTransport,
    ) -> None:
        cmd = TaskCommand(task_id="t", kind=CommandKind.CANCEL)
        await bus.send(cmd)
        frame = await transport.receive()
        data = json.loads(frame)
        assert data["type"] == "command"
        assert data["kind"] == "cancel"

    @pytest.mark.asyncio
    async def test_unsubscribed_task_id_no_crash(
        self, bus: TaskCommandBus,
    ) -> None:
        """向未订阅的 task_id 发送命令不会抛异常。"""
        cmd = TaskCommand(task_id="no-subscriber", kind=CommandKind.PAUSE)
        ok = await bus.send(cmd)
        assert ok

    @pytest.mark.asyncio
    async def test_multiple_subscribers_same_task(
        self, bus: TaskCommandBus,
    ) -> None:
        hits: list[str] = []

        async def h1(cmd: TaskCommand) -> None:
            hits.append("h1")

        async def h2(cmd: TaskCommand) -> None:
            hits.append("h2")

        bus.subscribe("t", h1)
        bus.subscribe("t", h2)
        await bus.send(TaskCommand(task_id="t", kind=CommandKind.RESUME))
        assert hits == ["h1", "h2"]


class TestTaskCommandBusPublish:
    """给定 TaskCommandBus，调用 publish 时事件 frame 会写入
    transport，但不会分发给订阅者。"""

    @pytest.mark.asyncio
    async def test_publish_writes_to_transport(
        self, bus: TaskCommandBus, transport: LoopbackTransport,
    ) -> None:
        evt = TaskEvent(task_id="t1", kind=EventKind.FAILED, payload={"reason": "timeout"})
        await bus.publish(evt)

        frame = await transport.receive()
        data = json.loads(frame)
        assert data["type"] == "event"
        assert data["task_id"] == "t1"
        assert data["kind"] == "failed"
        assert data["payload"] == {"reason": "timeout"}

    @pytest.mark.asyncio
    async def test_publish_does_not_dispatch_to_subscribers(
        self, bus: TaskCommandBus,
    ) -> None:
        called = False

        async def handler(cmd: TaskCommand) -> None:
            nonlocal called
            called = True

        bus.subscribe("t1", handler)
        await bus.publish(TaskEvent(task_id="t1", kind=EventKind.COMPLETED))
        assert not called


class TestTaskCommandBusHelpers:
    @pytest.mark.asyncio
    async def test_unsubscribe_removes_handlers(
        self, bus: TaskCommandBus,
    ) -> None:
        received: list[TaskCommand] = []

        async def handler(cmd: TaskCommand) -> None:
            received.append(cmd)

        bus.subscribe("t", handler)
        assert bus.has_subscribers("t")
        bus.unsubscribe("t")
        assert not bus.has_subscribers("t")

        await bus.send(TaskCommand(task_id="t", kind=CommandKind.CANCEL))
        assert len(received) == 0


# ═══════════════════════════════════════════════════════════════════════
# JSON 可序列化
# ═══════════════════════════════════════════════════════════════════════


class TestJsonSerializability:
    """所有消息类型都能在不使用自定义 encoder 的情况下通过 ``json.dumps``。"""

    def test_command_is_fully_json_serializable(self) -> None:
        cmd = TaskCommand(
            task_id="t",
            kind=CommandKind.RESUME_REPLY,
            payload={"reply": "yes"},
        )
        raw = json.dumps(cmd.to_dict())
        parsed = json.loads(raw)
        assert isinstance(parsed["ts"], str)  # datetime → ISO 字符串

    def test_event_is_fully_json_serializable(self) -> None:
        evt = TaskEvent(
            task_id="t",
            kind=EventKind.WAITING_INPUT,
            payload={"since": "2025-01-01T00:00:00Z"},
        )
        raw = json.dumps(evt.to_dict())
        parsed = json.loads(raw)
        assert isinstance(parsed["ts"], str)


# ═══════════════════════════════════════════════════════════════════════════════
# 处理器异常韧性（F2 B5）：单个处理器异常不得杀死调度基础设施
# ═══════════════════════════════════════════════════════════════════════════════

class TestBusHandlerResilience:
    """bus 分发循环必须 log-and-continue：send / listen_events
    不得因单个处理器异常而终止（否则调度器检查循环 / 事件监听
    永久死亡 → 并发额度泄漏 → 任务全部排队）。"""

    @pytest.mark.asyncio
    async def test_send_continues_after_handler_error(
        self, bus: TaskCommandBus,
    ) -> None:
        hits: list[str] = []

        async def bad(cmd: TaskCommand) -> None:
            raise RuntimeError("handler boom")

        async def good(cmd: TaskCommand) -> None:
            hits.append("good")

        bus.subscribe("t", bad)
        bus.subscribe("t", good)

        ok = await bus.send(TaskCommand(task_id="t", kind=CommandKind.CANCEL))
        assert ok
        assert hits == ["good"]

    @pytest.mark.asyncio
    async def test_send_returns_true_even_when_all_handlers_error(
        self, bus: TaskCommandBus,
    ) -> None:
        async def bad(cmd: TaskCommand) -> None:
            raise RuntimeError("handler boom")

        bus.subscribe("t", bad)
        ok = await bus.send(TaskCommand(task_id="t", kind=CommandKind.CANCEL))
        assert ok

    @pytest.mark.asyncio
    async def test_listen_events_survives_handler_error(
        self, bus: TaskCommandBus,
    ) -> None:
        received: list[TaskEvent] = []
        raised = False

        async def flaky(event: TaskEvent) -> None:
            nonlocal raised
            if not raised:
                raised = True
                raise RuntimeError("first event boom")
            received.append(event)

        listener = asyncio.create_task(bus.listen_events(flaky))
        await bus.publish(TaskEvent(task_id="t1", kind=EventKind.COMPLETED))
        await bus.publish(TaskEvent(task_id="t2", kind=EventKind.COMPLETED))
        await asyncio.sleep(0.05)
        listener.cancel()
        try:
            await listener
        except asyncio.CancelledError:
            pass

        # 第一个事件触发异常，第二个事件仍被处理 → 监听循环未死
        assert [e.task_id for e in received] == ["t2"]
