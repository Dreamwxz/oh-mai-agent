"""oh_mai_agent.bus 的测试——命令路由、事件队列、订阅生命周期与韧性。

v0.1.0 跨进程方案回退后，总线不再有字节帧序列化 / Transport / decode_frame，
因此不再测试线协议，只测试进程内真正有价值的行为：
- 命令按 task_id 精准投递（同步分发）；
- 事件经内部队列 fan-out（fire-and-forget，不按路由表分发）；
- 订阅生命周期（subscribe / unsubscribe）；
- 处理器异常韧性（log-and-continue，不得杀死调度基础设施）。
"""

from __future__ import annotations

import asyncio

import pytest

from oh_mai_agent.bus import (
    CommandKind,
    EventKind,
    TaskCommand,
    TaskCommandBus,
    TaskEvent,
)


@pytest.fixture
def bus() -> TaskCommandBus:
    return TaskCommandBus()


class TestTaskCommandBusSend:
    """给定带订阅者的 TaskCommandBus，调用 send 后，
    handler 会收到该命令。"""

    @pytest.mark.asyncio
    async def test_subscriber_receives_command(self, bus: TaskCommandBus) -> None:
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
    async def test_unsubscribed_task_id_no_crash(self, bus: TaskCommandBus) -> None:
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
    """事件不按路由表分发；经内部事件队列由 listen_events 消费。"""

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

    @pytest.mark.asyncio
    async def test_listen_events_receives_published_event(
        self, bus: TaskCommandBus,
    ) -> None:
        received: list[TaskEvent] = []

        async def handler(event: TaskEvent) -> None:
            received.append(event)

        listener = asyncio.create_task(bus.listen_events(handler))
        await bus.publish(TaskEvent(task_id="t1", kind=EventKind.COMPLETED))
        await bus.publish(TaskEvent(task_id="t2", kind=EventKind.FAILED))
        await asyncio.sleep(0.05)
        listener.cancel()
        try:
            await listener
        except asyncio.CancelledError:
            pass

        assert [e.task_id for e in received] == ["t1", "t2"]
        assert [e.kind for e in received] == [EventKind.COMPLETED, EventKind.FAILED]


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


class TestBusHandlerResilience:
    """bus 分发必须 log-and-continue：send / listen_events
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
