"""TaskCommandBus — 进程内命令/事件总线。

维护一个 ``task_id → list[handler]`` 路由表，配合内部事件队列：

- ``send(cmd)``：按 ``task_id`` 精准投递到注册在目标任务上的本地订阅处理器
  （同步按注册顺序调用，log-and-continue 韧性）；
- ``publish(event)``：fire-and-forget，事件写入内部队列，由
  ``listen_events`` 循环消费并分发给事件监听者。

架构要点（v0.1.0 跨进程方案回退后的进程内形态）：
- 命令路由：逐任务 ID 精准投递，只做本地分发，无传输/序列化层；
- 事件广播：fire-and-forget，经内部 ``asyncio.Queue`` 解耦，生产方不阻塞在
  消费方处理上。任务完成通知已统一为执行器直调
  ``scheduler.on_task_completed``（同步释放并发额度），当前无内部事件
  生产者/消费者；``publish`` / ``listen_events`` 作为通用机制保留。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .messages import TaskCommand, TaskEvent

logger = logging.getLogger(__name__)


class TaskCommandBus:
    """基于路由表 + 事件队列的进程内总线。

    用法::

        bus = TaskCommandBus()

        async def my_handler(cmd: TaskCommand) -> None:
            print(f"收到 {cmd.kind} 命令，目标任务 {cmd.task_id}")

        bus.subscribe("task-001", my_handler)

        cmd = TaskCommand(task_id="task-001", kind=CommandKind.INJECT_INSTRUCTION,
                          payload={"instruction": "stop"})
        await bus.send(cmd)
        # → my_handler(cmd) 被调用

        event = TaskEvent(task_id="task-001", kind=EventKind.COMPLETED)
        await bus.publish(event)
        # → 事件入队，由 listen_events 循环消费
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[TaskCommand], Awaitable[None]]]] = {}
        self._event_queue: asyncio.Queue[TaskEvent] = asyncio.Queue()

    # ── send ──────────────────────────────────────────────────────────

    async def send(self, cmd: TaskCommand) -> bool:
        """分发 *cmd* 到注册在 ``cmd.task_id`` 上的全部本地订阅处理器。

        处理器按注册顺序同步调用；单处理器异常 log-and-continue，
        不中断后续订阅者。

        Returns:
            ``True`` 表示发送成功。
        """
        logger.info(
            "命令发布：task_id=%s kind=%s payload=%s",
            cmd.task_id,
            cmd.kind.value,
            str(cmd.payload)[:80],
        )
        for handler in self._subscribers.get(cmd.task_id, ()):
            try:
                await handler(cmd)
            except Exception:
                # 单处理器异常不得杀死调度基础设施：RESUME 分支含
                # store 写入（agent_loop），超时路径的 bus.send(CANCEL)
                # 无守卫——若此处重新抛出，检查循环 / 事件监听会被
                # 永久终止，并发额度随之泄漏。log-and-continue。
                logger.exception(
                    "命令本地处理异常（继续分发后续订阅者）：task_id=%s kind=%s",
                    cmd.task_id,
                    cmd.kind.value,
                )
        return True

    # ── publish ───────────────────────────────────────────────────────

    async def publish(self, event: TaskEvent) -> None:
        """广播 *event*（fire-and-forget）：写入内部事件队列。

        事件不按路由表分发（命令 → 本地订阅者，事件 → 队列监听者），
        由 ``listen_events`` 循环消费。
        """
        logger.debug(
            "事件广播：task_id=%s kind=%s payload=%s",
            event.task_id,
            event.kind.value,
            str(event.payload)[:80],
        )
        await self._event_queue.put(event)

    # ── subscribe ─────────────────────────────────────────────────────

    def subscribe(
        self,
        task_id: str,
        handler: Callable[[TaskCommand], Awaitable[None]],
    ) -> None:
        """为 *task_id* 注册命令处理器 *handler*。

        处理器在 ``send()`` 内部同步（按注册顺序）调用，
        仅在命令的 task_id 匹配时触发。
        """
        self._subscribers.setdefault(task_id, []).append(handler)
        logger.info("订阅注册：task_id=%s", task_id)

    # ── 辅助方法 ──────────────────────────────────────────────────────

    def unsubscribe(self, task_id: str) -> None:
        """移除 *task_id* 上所有已注册的处理器。"""
        self._subscribers.pop(task_id, None)
        logger.info("取消订阅：task_id=%s", task_id)

    # ── listen_events ──────────────────────────────────────────────────

    async def listen_events(
        self,
        handler: Callable[[TaskEvent], Awaitable[None]],
    ) -> None:
        """持续消费内部事件队列，将 *event* 分发给 *handler*。

        这是一个阻塞循环——应作为 ``asyncio.Task`` 运行，
        任务被取消时干净退出。单事件处理异常 log-and-continue，
        保证监听循环不因单个坏事件退出。
        """
        logger.info("事件监听启动")
        while True:
            try:
                event = await self._event_queue.get()
            except asyncio.CancelledError:
                logger.info("事件监听停止（任务被取消）")
                return
            try:
                await handler(event)
            except Exception:
                # 单事件处理异常不得终止监听循环：调度器的事件监听
                # 任务若被杀死，COMPLETED/FAILED 事件将不再释放
                # 并发额度，后续任务全部排队。log-and-continue。
                logger.exception(
                    "事件处理异常（继续监听）：task_id=%s kind=%s",
                    event.task_id,
                    event.kind.value,
                )
            logger.debug(
                "事件分发：task_id=%s kind=%s", event.task_id, event.kind.value
            )
