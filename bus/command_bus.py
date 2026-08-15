"""TaskCommandBus — 进程内命令总线。

维护一个 ``task_id → list[handler]`` 路由表：

- ``send(cmd)``：按 ``task_id`` 精准投递到注册在目标任务上的本地订阅处理器
  （同步按注册顺序调用，log-and-continue 韧性）；
- ``subscribe(task_id, handler)`` / ``unsubscribe(task_id)``：命令订阅生命周期。

架构要点（v0.1.0 跨进程方案回退后的进程内形态）：
- 命令路由：逐任务 ID 精准投递，只做本地分发，无传输/序列化层；
- 任务完成通知已统一为执行器直调 ``scheduler.on_task_completed``
  （同步释放并发额度），事件通道（``publish`` / ``listen_events`` /
  ``TaskEvent``）无生产者消费者，已随同删除。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .messages import TaskCommand

logger = logging.getLogger(__name__)


class TaskCommandBus:
    """基于路由表的进程内命令总线。

    用法::

        bus = TaskCommandBus()

        async def my_handler(cmd: TaskCommand) -> None:
            print(f"收到 {cmd.kind} 命令，目标任务 {cmd.task_id}")

        bus.subscribe("task-001", my_handler)

        cmd = TaskCommand(task_id="task-001", kind=CommandKind.INJECT_INSTRUCTION,
                          payload={"instruction": "stop"})
        await bus.send(cmd)
        # → my_handler(cmd) 被调用
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[TaskCommand], Awaitable[None]]]] = {}

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
                # 无守卫——若此处重新抛出，调度器检查循环会被
                # 永久终止，并发额度随之泄漏。log-and-continue。
                logger.exception(
                    "命令本地处理异常（继续分发后续订阅者）：task_id=%s kind=%s",
                    cmd.task_id,
                    cmd.kind.value,
                )
        return True

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
