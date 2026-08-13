"""MaiBot Agent 调度器模块。

提供任务调度核心：并发额度控制（pending 排队）、定时调度（延迟 + cron 表达式）、
超时兜底、任务启动编排。

cron 表达式解析使用成熟库 croniter（由 requirements.txt 声明，MaiBot 自动安装）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from croniter import croniter

from ..config import TaskConfig
from ..bus.messages import CommandKind, TaskCommand, EventKind
from ..domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# TaskScheduler
# ═══════════════════════════════════════════════════════════════════════


class TaskScheduler:
    """任务调度器：并发额度控制、定时调度、超时兜底。

    调度器内部维护 pending 队列（按 priority 降序）、
    running 集合，以及一个每秒轮询的后台检查循环
    （处理 SCHEDULED 到期与超时检测）。
    """

    def __init__(
        self,
        config: TaskConfig,
        store: "TaskStore",  # 前向引用 — 接口契约：save/get/list_active
        executor: Callable[[TaskRecord], Awaitable[None]],
        *,
        command_bus: Any,
    ) -> None:
        """初始化调度器。

        Args:
            config: 任务调度配置（max_concurrent_tasks / max_runtime_min）。
            store: 任务持久化存储（TaskStore 接口契约）。
            executor: 任务实际执行回调。
            command_bus: TaskCommandBus 实例。调度器通过总线事件
               （TaskEvent COMPLETED/FAILED）释放并发额度。
        """
        self._config = config
        self._store = store
        self._executor = executor
        self._command_bus = command_bus

        self._running: set[str] = set()
        self._pending: list[TaskRecord] = []

        self._check_task: asyncio.Task[Any] | None = None
        self._event_listener_task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()

    # ── 配置热更新 ──────────────────────────────────────────────────

    def update_config(self, config: TaskConfig) -> None:
        """热更新调度器配置（并发上限/超时等）。

        若当前 running 数超过新上限，不强制停止（等自然完成）；
        新任务按新上限排队。超时检测在下一轮询周期生效。
        """
        self._config = config
        logger.info(
            "调度器配置已更新（max_concurrent=%d, max_runtime=%d）",
            config.max_concurrent_tasks,
            config.max_runtime_min,
        )

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动调度器。

        启动后台轮询循环，每秒检查 SCHEDULED 任务是否到点以及运行中任务是否超时。
        若提供 command_bus，同时启动事件监听（TaskEvent COMPLETED/FAILED）。
        """
        if self._check_task is not None:
            logger.debug("调度器已在运行，跳过重复启动")
            return
        self._stop_event.clear()
        self._check_task = asyncio.create_task(self._check_loop())
        self._event_listener_task = asyncio.create_task(
            self._command_bus.listen_events(self._on_task_event)
        )
        logger.info("调度器已启动（max_concurrent=%d）", self._config.max_concurrent_tasks)

    async def stop(self) -> None:
        """停止调度器。

        取消后台检查循环和事件监听，把所有 RUNNING 任务标记为 PAUSED 落盘。
        """
        logger.info("调度器正在停止...")

        # 1) 停止检查循环
        self._stop_event.set()
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
            self._check_task = None

        # 1.5) 停止事件监听
        if self._event_listener_task:
            self._event_listener_task.cancel()
            try:
                await self._event_listener_task
            except asyncio.CancelledError:
                pass
            self._event_listener_task = None

        # 2) 把所有 RUNNING 任务标记为 PAUSED
        for tid in list(self._running):
            try:
                task = await self._store.get(tid)
            except Exception:
                logger.warning("停止时获取运行中任务 %s 失败", tid)
                continue
            if task is None:
                continue
            try:
                if task.status == TaskStatus.RUNNING:
                    task.metadata.pop("_coop_paused", None)
                    task.force(TaskStatus.PAUSED, actor="scheduler", reason="paused_during_stop")
                    task.metadata["_paused_by_stop"] = True
                    await self._store.save(task)
                    logger.info("任务 %s 在关闭时已暂停", tid)
            except Exception:
                logger.warning("停止时暂停任务 %s 失败", tid, exc_info=True)

        self._running.clear()
        self._pending.clear()
        logger.info("调度器已停止")

    # ── 入队 ──────────────────────────────────────────────────────────

    async def enqueue(self, task: TaskRecord) -> None:
        """提交任务到调度队列。

        - NOW：立即尝试启动（额度满则进入 PENDING 排队）。
        - DELAY：计算 scheduled_at = now + delay_seconds，进入 SCHEDULED
        - CRON：scheduled_at = croniter(expr, now).get_next(datetime)，进入 SCHEDULED

        Args:
            task: 待调度的任务。
        """
        now = datetime.now()

        try:
            if task.trigger_type == TriggerType.NOW:
                task.scheduled_at = None
                self._ensure_status(task, TaskStatus.PENDING)
                await self._store.save(task)
                self._push_pending(task)
                await self._try_dispatch()
                logger.info("任务 %s 已入队（即时执行）", task.id)

            elif task.trigger_type == TriggerType.DELAY:
                delay = task.delay_seconds or 0
                task.scheduled_at = now + timedelta(seconds=delay)
                self._ensure_status(task, TaskStatus.SCHEDULED)
                await self._store.save(task)
                logger.info(
                    "任务 %s 已入队（延迟 %d 秒，%s 执行）",
                    task.id,
                    delay,
                    task.scheduled_at.isoformat(),
                )

            elif task.trigger_type == TriggerType.CRON:
                # cron 表达式为空时默认每分钟触发一次
                expr = task.cron_expr or "* * * * *"
                try:
                    itr = croniter(expr, now)
                    nxt = itr.get_next(datetime)
                except ValueError as e:
                    logger.error("任务 %s 的 cron 表达式无效: %s", task.id, e)
                    task.transition(TaskStatus.FAILED)
                    await self._store.save(task)
                    return
                task.scheduled_at = nxt
                task.cron_expr = expr
                self._ensure_status(task, TaskStatus.SCHEDULED)
                await self._store.save(task)
                logger.info(
                    "任务 %s 已入队（cron=%s，下次触发 %s）",
                    task.id,
                    expr,
                    nxt.isoformat(),
                )

        except Exception:
            logger.exception("入队任务 %s 时发生异常", task.id)

    @staticmethod
    def _ensure_status(task: TaskRecord, target: TaskStatus) -> None:
        """安全地将任务设置到目标初始状态。

        若任务已处于目标状态则跳过；否则执行状态机转换。
        """
        if task.status == target:
            return
        task.transition(target)

    def _push_pending(self, task: TaskRecord) -> bool:
        """将任务追加到 pending 队列（按 id 去重）。返回是否实际入队。"""
        if any(t.id == task.id for t in self._pending):
            logger.debug("任务 %s 已在 pending 队列，跳过重复入队", task.id)
            return False
        self._pending.append(task)
        self._pending.sort(key=lambda t: t.priority, reverse=True)
        return True

    # ── 任务控制 ──────────────────────────────────────────────────────

    async def cancel(self, task_id: str) -> bool:
        """取消任务。

        - SCHEDULED / PENDING → CANCELLED 落盘
        - RUNNING / WAITING_INPUT → 标记取消（通知 executor 协作）
        - PAUSED → CANCELLED 落盘

        Returns:
            是否成功取消。
        """
        try:
            task = await self._store.get(task_id)
        except Exception:
            logger.exception("获取待取消任务 %s 失败", task_id)
            return False
        if task is None or task.is_terminal():
            return False

        if task.status in (TaskStatus.SCHEDULED, TaskStatus.PENDING, TaskStatus.PAUSED):
            task.transition(TaskStatus.CANCELLED)
            await self._store.save(task)
            # 从 pending 队列移除
            self._pending = [t for t in self._pending if t.id != task_id]
            logger.info("任务 %s 已取消", task_id)
            return True

        if task.status in (TaskStatus.RUNNING, TaskStatus.WAITING_INPUT):
            if task.level == TaskLevel.INSTANT:
                task.force(
                    TaskStatus.CANCELLED,
                    actor="scheduler",
                    reason="cancelled_by_user",
                )
                await self._store.save(task)
                self._running.discard(task_id)
                logger.info("任务 %s 已取消", task_id)
            else:
                await self._command_bus.send(
                    TaskCommand(task_id=task.id, kind=CommandKind.CANCEL),
                )
                logger.info("任务 %s 已请求取消（等待执行器协作处理）", task_id)
            return True

        return False

    async def pause(self, task_id: str) -> bool:
        """暂停 RUNNING 任务。

        RUNNING → PAUSED，释放并发额度并触发重新调度。

        Returns:
            操作是否成功。
        """
        try:
            task = await self._store.get(task_id)
        except Exception:
            logger.exception("获取待暂停任务 %s 失败", task_id)
            return False
        if task is None or task.status != TaskStatus.RUNNING:
            return False

        task.metadata["_coop_paused"] = True
        await self._store.save(task)
        await self._command_bus.send(
            TaskCommand(task_id=task.id, kind=CommandKind.PAUSE),
        )
        logger.info("任务 %s 已暂停", task_id)
        return True

    async def resume(self, task_id: str) -> bool:
        """恢复 PAUSED 任务。

        PAUSED → PENDING，重新排队。

        Returns:
            操作是否成功。
        """
        try:
            task = await self._store.get(task_id)
        except Exception:
            logger.exception("获取待恢复任务 %s 失败", task_id)
            return False
        if task is None:
            return False

        if task.status == TaskStatus.RUNNING:
            if not task.metadata.get("_coop_paused"):
                return False
            task.metadata.pop("_coop_paused", None)
            task.started_at = datetime.now()
            await self._store.save(task)
            await self._command_bus.send(
                TaskCommand(task_id=task.id, kind=CommandKind.RESUME),
            )
            logger.info("任务 %s 已恢复", task_id)
            return True

        if task.status != TaskStatus.PAUSED:
            return False

        task.transition(TaskStatus.PENDING)
        await self._store.save(task)
        self._push_pending(task)
        await self._try_dispatch()
        logger.info("任务 %s 已恢复", task_id)
        return True

    # ── 调度核心 ──────────────────────────────────────────────────────

    async def on_task_completed(self, task: TaskRecord) -> None:
        """任务结束回调（直接调用路径，向后兼容）。

        从 _running 集合中移除，释放并发额度，触发下一 pending 任务启动。

        Args:
            task: 已完成（终态）的任务。
        """
        await self._do_on_task_completed(task)

    async def _do_on_task_completed(self, task: TaskRecord) -> None:
        """共享完成处理逻辑（直接回调 + 事件监听共用）。

        从 _running 集合中移除，释放并发额度。对于成功的 CRON 任务，
        计算下次触发时间并重新调度（循环执行）。
        仅 COMPLETED 的 CRON 任务循环；FAILED/CANCELLED 不循环，避免死循环。
        """
        self._running.discard(task.id)
        # 对于成功的 CRON 任务，计算下次触发时间并重新调度（循环执行）。
        # 仅 COMPLETED 的 CRON 任务循环；FAILED/CANCELLED 不循环，避免死循环。
        if (
            task.status == TaskStatus.COMPLETED
            and task.trigger_type == TriggerType.CRON
            and task.cron_expr
        ):
            await self._reschedule_cron(task)
        await self._try_dispatch()

    async def _reschedule_cron(self, task: TaskRecord) -> None:
        """计算并持久化 CRON 任务的下一次执行时间。"""
        try:
            itr = croniter(task.cron_expr, datetime.now())
            next_time = itr.get_next(datetime)
            task.scheduled_at = next_time
            # COMPLETED is terminal; cron rescheduling is the explicit force escape hatch.
            task.force(TaskStatus.SCHEDULED, actor="scheduler", reason="")
            # 守卫保存：仅当持久化记录仍为 COMPLETED 时才写 SCHEDULED——
            # 若事件与重排之间发生并发强制取消/删除，本次写入被原子拒绝，
            # 避免复活一个已被终态覆盖的 CRON 任务。
            await self._store.save(task, expected_status=TaskStatus.COMPLETED)
        except Exception:
            logger.warning(
                "重新调度 cron 任务 %s 失败", task.id, exc_info=True
            )

    async def _on_task_event(self, event: Any) -> None:
        """事件总线监听器 — 处理 TaskEvent(COMPLETED/FAILED) 释放并发额度。"""
        from ..bus.messages import EventKind, TaskEvent

        if not isinstance(event, TaskEvent):
            return
        if event.kind not in (EventKind.COMPLETED, EventKind.FAILED, EventKind.CANCELLED):
            return

        task = await self._store.get(event.task_id)
        if task is None:
            logger.warning(
                "收到 TaskEvent %s，但任务 %s 不存在", event.kind.value, event.task_id,
            )
            return
        await self._do_on_task_completed(task)

    async def _try_start(self, task: TaskRecord) -> bool:
        """检查额度后启动任务。

        Returns:
            是否成功启动（False 表示额度不足，任务留在 pending）。
        """
        # 1) 并发额度：_running 集合大小即当前占用额度，达到上限则任务留在 pending
        if len(self._running) >= self._config.max_concurrent_tasks:
            return False

        # 2) 启动执行
        task.started_at = datetime.now()
        try:
            task.transition(TaskStatus.RUNNING)
        except Exception:
            logger.exception("任务 %s 转换到 RUNNING 状态失败", task.id)
            return False

        # 在第一个 await 之前同步预留额度：_try_dispatch 可能被检查循环、
        # 事件监听、enqueue、resume 并发调用，若等 save 完成后再 add，
        # 两个并发派发可同时通过额度检查 → 超过 max_concurrent_tasks。
        self._running.add(task.id)
        try:
            await self._store.save(task)
        except Exception:
            self._running.discard(task.id)
            logger.exception("任务 %s 保存 RUNNING 状态失败", task.id)
            # 内存状态回滚到 PENDING：否则 _try_dispatch 把任务放回队首后，
            # 重试时 transition(RUNNING) 因 running→running 非法抛异常，
            # 任务永久卡死队首，阻塞其后全部 pending 任务。
            task.force(TaskStatus.PENDING, actor="scheduler", reason="save_failed_rollback")
            return False

        asyncio.create_task(self._safe_execute(task, self._executor))

        logger.info("任务 %s 已启动（level=%s, stream=%s）", task.id, task.level.value, task.stream_id)
        return True

    async def _safe_execute(
        self,
        task: TaskRecord,
        callback: Callable[[TaskRecord], Awaitable[None]],
    ) -> None:
        """安全执行回调，捕获异常避免未处理异常污染事件循环。"""
        try:
            await callback(task)
        except asyncio.CancelledError:
            logger.info("任务 %s 执行已被取消", task.id)
        except Exception:
            logger.exception("执行任务 %s 时发生未处理异常", task.id)

    async def _try_dispatch(self) -> None:
        """尝试从 pending 队列中取出任务并启动。

        按 priority 降序处理，直到额度满或 pending 队列为空。
        """
        while self._pending and len(self._running) < self._config.max_concurrent_tasks:
            task = self._pending.pop(0)
            success = await self._try_start(task)
            if not success:
                # 放回队首（priority 高的优先重试）
                self._pending.insert(0, task)
                break

    def active_count(self) -> int:
        """当前 running 任务数。"""
        return len(self._running)

    # ── 后台检查循环 ──────────────────────────────────────────────────

    async def _check_loop(self) -> None:
        """后台轮询循环。

        每秒执行：
        1. 检查所有 SCHEDULED 任务是否到点 → 移入 PENDING
        2. 检查 RUNNING 任务是否超时（max_runtime_min）
        3. 触发 _try_dispatch
        """
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise

                now = datetime.now()

                # ── 1) SCHEDULED → PENDING ──
                try:
                    active = await self._store.list_active()
                except Exception:
                    logger.debug("list_active 查询失败，下个周期重试", exc_info=True)
                    active = []

                for t in active:
                    if t.status != TaskStatus.SCHEDULED:
                        continue
                    # 无触发时间（理论上不应出现）则跳过，避免本轮误触发
                    if t.scheduled_at is None:
                        continue
                    # 未到触发时间，等待后续轮询
                    if t.scheduled_at > now:
                        continue

                    try:
                        t.transition(TaskStatus.PENDING)
                        await self._store.save(t)
                        self._push_pending(t)
                        logger.info(
                            "任务 %s 已触发（原定于 %s 执行）",
                            t.id,
                            t.scheduled_at.isoformat(),
                        )
                    except Exception:
                        logger.warning(
                            "触发定时任务 %s 失败", t.id, exc_info=True
                        )
                        continue

                # ── 2) 超时检测 ──
                timeout_min = self._config.max_runtime_min
                if timeout_min > 0:
                    for tid in list(self._running):
                        try:
                            t = await self._store.get(tid)
                        except Exception:
                            continue
                        # 已不在 RUNNING（如已暂停/完成但未收到事件）→ 从运行集清理，释放额度
                        if t is None or t.status != TaskStatus.RUNNING:
                            self._running.discard(tid)
                            continue
                        if t.metadata.get("_coop_paused"):
                            continue
                        # started_at 缺失（如恢复场景）则无法计时，跳过本轮
                        if t.started_at is None:
                            continue
                        runtime = (now - t.started_at).total_seconds() / 60.0
                        if runtime > timeout_min:
                            logger.warning(
                                "任务 %s 已超时（运行 %.1f 分钟，上限 %d 分钟）",
                                tid,
                                runtime,
                                timeout_min,
                            )
                            # 超时兜底：标记 FAILED，移出运行集，并通知执行器协作停止。
                            # 守卫保存（expected_status=RUNNING）：若 get 之后、save 之前
                            # 循环已把任务持久化为终态（COMPLETED/CANCELLED），本次 FAILED
                            # 写入被原子拒绝，避免超时降级覆盖并发完成的终态记录。
                            try:
                                t.transition(TaskStatus.FAILED)
                                saved = await self._store.save(
                                    t, expected_status=TaskStatus.RUNNING,
                                )
                                if saved:
                                    logger.warning("任务 %s 超时，已强制降级为 FAILED", tid)
                                else:
                                    logger.warning(
                                        "任务 %s 超时降级被并发终态拦截（持久化已非 RUNNING），跳过",
                                        tid,
                                    )
                            except Exception:
                                logger.exception("将任务 %s 标记为 FAILED 失败", tid)
                            self._running.discard(tid)
                            await self._command_bus.send(
                                TaskCommand(task_id=tid, kind=CommandKind.CANCEL),
                            )

                # ── 3) 触发分发 ──
                await self._try_dispatch()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("调度器检查循环发生致命错误")


# ═══════════════════════════════════════════════════════════════════════
# TaskStore 接口类型（用于类型注解，实际存储由 task_store.py 提供）
# ═══════════════════════════════════════════════════════════════════════

from typing import Protocol


class TaskStore(Protocol):
    """TaskStore 接口契约。

    task_store.py 实现以下方法：
    - save(task) → None：持久化任务
    - get(task_id) → TaskRecord | None：按 ID 获取任务
    - list_active() → list[TaskRecord]：列出所有非终态任务
    """

    async def save(self, task: TaskRecord) -> None: ...

    async def get(self, task_id: str) -> TaskRecord | None: ...

    async def list_active(self) -> list[TaskRecord]: ...
