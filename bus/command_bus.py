"""TaskCommandBus — 命令总线，将命令/事件序列化后通过
``Transport`` 分发。

总线维护一个路由表，将 ``task_id`` 映射到 ``list[handler]``。
当调用 ``send(cmd)`` 时，命令先被序列化为 JSON 并通过
Transport 推送到通道中，**同时**也会分发给注册在目标
``task_id`` 上的本地订阅处理器。

事件（``publish``）同样被序列化并通过 Transport 推送，
但**不**通过路由表分发——事件监听者改为在 Transport 的接收侧订阅。

架构要点：
- 命令路由：逐任务 ID 精准投递，send 触发本地订阅处理器
- 事件广播：fire-and-forget，通过 Transport 通道广播
- 传输解耦：本地 dispatch 服务于当前进程内的订阅者，
  传输细节由 Transport 层负责
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .messages import TaskCommand, TaskEvent, decode_frame
from .transport import Transport

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# TaskCommandBus
# ═══════════════════════════════════════════════════════════════════════


class TaskCommandBus:
    """基于 ``Transport`` 的命令/事件总线。

    用法::

        transport = LoopbackTransport()
        bus = TaskCommandBus(transport)

        async def my_handler(cmd: TaskCommand) -> None:
            print(f"收到 {cmd.kind} 命令，目标任务 {cmd.task_id}")

        bus.subscribe("task-001", my_handler)

        cmd = TaskCommand(task_id="task-001", kind=CommandKind.INJECT_INSTRUCTION,
                          payload={"instruction": "stop"})
        await bus.send(cmd)
        # → transport.send(frame) + my_handler(cmd) 被调用

        event = TaskEvent(task_id="task-001", kind=EventKind.COMPLETED)
        await bus.publish(event)
        # → 仅 transport.send(frame)，不触发 my_handler
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._subscribers: dict[str, list[Callable[[TaskCommand], Awaitable[None]]]] = {}

    # ── send ──────────────────────────────────────────────────────────

    async def send(self, cmd: TaskCommand) -> bool:
        """序列化 *cmd*，推入 Transport，并分发给注册在
        ``cmd.task_id`` 上的本地订阅处理器。

        Returns:
            ``True`` 表示发送成功。
        """
        logger.info(
            "命令发布：task_id=%s kind=%s payload=%s",
            cmd.task_id,
            cmd.kind.value,
            str(cmd.payload)[:80],
        )
        frame = json.dumps(cmd.to_dict()).encode("utf-8")
        await self._transport.send(frame)

        # 本地分发：调用注册在该 task_id 下的所有处理器
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
        """序列化 *event* 并通过 Transport 推送（广播）。

        事件是 fire-and-forget 模式：此方法**不**通过路由表分发
        （命令 → 本地订阅者，事件 → Transport 监听者）。
        """
        logger.debug(
            "事件广播：task_id=%s kind=%s payload=%s",
            event.task_id,
            event.kind.value,
            str(event.payload)[:80],
        )
        frame = json.dumps(event.to_dict()).encode("utf-8")
        await self._transport.send(frame)

    # ── subscribe ─────────────────────────────────────────────────────

    def subscribe(
        self,
        task_id: str,
        handler: Callable[[TaskCommand], Awaitable[None]],
    ) -> None:
        """为 *task_id* 注册命令处理器 *handler*。

        处理器在 ``send()`` 内部**同步**（按注册顺序）调用，
        仅在命令的 task_id 匹配时触发。
        """
        self._subscribers.setdefault(task_id, []).append(handler)
        logger.info("订阅注册：task_id=%s", task_id)

    # ── 辅助方法 ──────────────────────────────────────────────────────

    def unsubscribe(self, task_id: str) -> None:
        """移除 *task_id* 上所有已注册的处理器。"""
        self._subscribers.pop(task_id, None)
        logger.info("取消订阅：task_id=%s", task_id)

    def has_subscribers(self, task_id: str) -> bool:
        """检查 *task_id* 是否有已注册的处理器。"""
        return task_id in self._subscribers and len(self._subscribers[task_id]) > 0

    # ── listen_events ──────────────────────────────────────────────────

    async def listen_events(
        self,
        handler: Callable[[TaskEvent], Awaitable[None]],
    ) -> None:
        """持续从 Transport 读取帧，将 ``TaskEvent`` 消息分发给
        *handler*。

        非事件帧（命令）会被静默忽略。这是一个阻塞循环——应作为
        ``asyncio.Task`` 运行，不需要时通过 cancel 停止。

        循环在 Transport 返回 ``None``（已关闭）或任务被取消时
        干净退出。
        """
        import asyncio

        logger.info("事件监听启动")
        while True:
            try:
                frame = await self._transport.receive()
            except asyncio.CancelledError:
                logger.info("事件监听停止（任务被取消）")
                return
            if frame is None:
                logger.info("事件监听停止（传输已关闭）")
                return
            try:
                msg = decode_frame(frame)
            except ValueError:
                logger.warning("解码帧失败，跳过：frame=%s", frame[:80])
                continue
            if isinstance(msg, TaskEvent):
                try:
                    await handler(msg)
                except Exception:
                    # 单事件处理异常不得终止监听循环：调度器的事件监听
                    # 任务若被杀死，COMPLETED/FAILED 事件将不再释放
                    # 并发额度，后续任务全部排队。log-and-continue。
                    logger.exception(
                        "事件处理异常（继续监听）：task_id=%s kind=%s",
                        msg.task_id,
                        msg.kind.value,
                    )
                logger.debug(
                    "事件分发：task_id=%s kind=%s", msg.task_id, msg.kind.value
                )
            else:
                logger.debug(
                    "忽略非事件帧：task_id=%s kind=%s", msg.task_id, msg.kind.value
                )
