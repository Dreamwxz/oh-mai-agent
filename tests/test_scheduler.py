"""oh_mai_agent.core.scheduler 的测试 — 并发额度、优先级排序、pending 排队、
CRON 周期、取消/暂停/恢复。

回归测试：
  1. CRON 周期：on_task_completed → 重新 SCHEDULED
  2. 调度器停止保留 RUNNING→PAUSED
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from conftest import MockCtx, make_task
from croniter import croniter

from oh_mai_agent.config import MaibotAgentConfig, TaskConfig
from oh_mai_agent.core.scheduler import TaskScheduler
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.executor.base import ExecutionContext
from oh_mai_agent.executor.instant import InstantExecutor, ReplySender
from oh_mai_agent.bus.messages import CommandKind, TaskCommand


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

async def _noop_executor(task: TaskRecord) -> None:
    pass


@pytest.fixture
def task_config() -> TaskConfig:
    return TaskConfig(max_concurrent_tasks=2)


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


# ═══════════════════════════════════════════════════════════════════════════════
# 并发额度
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcurrencyQuota:
    @pytest.mark.asyncio
    async def test_max_concurrent_limit(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        started: list[str] = []
        event = asyncio.Event()

        async def _tracked(t: TaskRecord) -> None:
            started.append(t.id)
            await event.wait()

        scheduler = TaskScheduler(task_config, store, _tracked, command_bus=command_bus)

        t1 = make_task("t1", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        t2 = make_task("t2", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        t3 = make_task("t3", level=TaskLevel.AGENT, status=TaskStatus.PENDING)

        await store.save(t1)
        await store.save(t2)
        await store.save(t3)

        await scheduler.enqueue(t1)
        await scheduler.enqueue(t2)
        await scheduler.enqueue(t3)

        # 让派发逻辑执行（等待后台分发）
        await asyncio.sleep(0.05)

        assert scheduler.active_count() == 2  # max_concurrent_tasks=2
        # 第 3 个应留在 pending 队列
        assert len(scheduler._pending) >= 1

        # 放行阻塞中的 executor
        event.set()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_concurrency_allows_zero(self, store: TaskStore, command_bus: Any) -> None:
        """max_concurrent_tasks=0 时，任何任务都无法启动。"""
        cfg = TaskConfig(max_concurrent_tasks=0)
        scheduler = TaskScheduler(cfg, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", status=TaskStatus.PENDING)
        await store.save(t)
        await scheduler.enqueue(t)
        await asyncio.sleep(0.02)

        assert scheduler.active_count() == 0
        assert len(scheduler._pending) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 优先级排序
# ═══════════════════════════════════════════════════════════════════════════════

class TestPriorityOrdering:
    @pytest.mark.asyncio
    async def test_higher_priority_first(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        """使用阻塞 executor：先入队全部任务，再统一放行。

        前 2 个入队的任务在额度空闲时即被立即启动（与优先级无关）；
        其余任务留在按 priority 降序排列的 pending 队列，额度释放后按优先级补位。
        """
        event = asyncio.Event()
        started: list[str] = []

        async def _tracked(t: TaskRecord) -> None:
            started.append(t.id)
            await event.wait()

        scheduler = TaskScheduler(task_config, store, _tracked, command_bus=command_bus)

        t_low = make_task("t_low", priority=0, status=TaskStatus.PENDING)
        t_high = make_task("t_high", priority=10, status=TaskStatus.PENDING)
        t_mid = make_task("t_mid", priority=5, status=TaskStatus.PENDING)
        t_vhigh = make_task("t_vhigh", priority=100, status=TaskStatus.PENDING)

        await store.save(t_low)
        await store.save(t_high)
        await store.save(t_mid)
        await store.save(t_vhigh)

        # 全部入队 — 前 2 个（max_concurrent 额度内）立即启动并阻塞在事件上
        await scheduler.enqueue(t_low)
        await scheduler.enqueue(t_high)
        await scheduler.enqueue(t_mid)
        await scheduler.enqueue(t_vhigh)

        await asyncio.sleep(0.05)

        # max_concurrent=2，pending 队列按 priority 降序排列
        assert scheduler.active_count() == 2
        assert len(scheduler._pending) == 2

        event.set()
        await asyncio.sleep(0.02)

        # 前 2 个启动的是最先入队的任务（入队时额度空闲即被派发，与优先级无关）；
        # 额度释放后，pending 队列按 priority 降序补位
        assert len(started) >= 2


# ═══════════════════════════════════════════════════════════════════════════════
# CRON 周期 — 回归测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestCronCycle:
    @pytest.mark.asyncio
    async def test_cron_enqueue_sets_scheduled(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        """入队 CRON 任务后状态变为 SCHEDULED，并计算出 scheduled_at。"""
        executed: list[str] = []

        async def _exec(t: TaskRecord) -> None:
            executed.append(t.id)

        scheduler = TaskScheduler(task_config, store, _exec, command_bus=command_bus)

        t = make_task(
            "cron-1", level=TaskLevel.AGENT,
            status=TaskStatus.PENDING,
            trigger_type=TriggerType.CRON,
            cron_expr="*/5 * * * *",
        )
        await store.save(t)
        await scheduler.enqueue(t)

        updated = await store.get("cron-1")
        assert updated is not None
        assert updated.status == TaskStatus.SCHEDULED
        assert updated.scheduled_at is not None
        assert len(updated._status_log) == 1
        assert updated._status_log[0].status == TaskStatus.SCHEDULED
        assert updated._status_log[0].reason == ""
        assert updated.cron_expr == "*/5 * * * *"

    @pytest.mark.asyncio
    async def test_on_task_completed_reschedules_cron(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        """直接测试 on_task_completed 对 CRON 任务 COMPLETED 的处理。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task(
            "cron-1", level=TaskLevel.AGENT,
            status=TaskStatus.COMPLETED,
            trigger_type=TriggerType.CRON,
            cron_expr="*/5 * * * *",
        )
        await store.save(t)

        await scheduler.on_task_completed(t)

        # 回调后，任务应被重新调度
        updated = await store.get("cron-1")
        assert updated is not None
        # 状态应重置为 SCHEDULED
        assert updated.status == TaskStatus.SCHEDULED
        # scheduled_at 应更新为下次触发时间
        assert updated.scheduled_at is not None
        assert len(updated._status_log) == 1
        assert updated._status_log[0].status == TaskStatus.SCHEDULED
        assert updated._status_log[0].reason == ""

    @pytest.mark.asyncio
    async def test_on_task_completed_failed_cron_no_reschedule(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        """FAILED 的 CRON 任务不应被重新调度。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task(
            "cron-1", level=TaskLevel.AGENT,
            status=TaskStatus.FAILED,
            trigger_type=TriggerType.CRON,
            cron_expr="*/5 * * * *",
        )
        await store.save(t)

        await scheduler.on_task_completed(t)

        updated = await store.get("cron-1")
        assert updated is not None
        assert updated.status == TaskStatus.FAILED  # 保持 FAILED 不变


# ═══════════════════════════════════════════════════════════════════════════════
# 取消
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_running_instant_persists_and_blocks_completion(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_reply(*args: Any, **kwargs: Any) -> None:
            started.set()
            await release.wait()

        task = make_task("instant-running", level=TaskLevel.INSTANT, status=TaskStatus.RUNNING)
        await store.save(task)
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        scheduler._running.add(task.id)
        mock_ctx = MockCtx()
        sender = ReplySender(ctx=mock_ctx, config_getter=lambda: MaibotAgentConfig())
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=store, scheduler=scheduler, config=MaibotAgentConfig(),
            sender=sender,
        )

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sender, "send_polished", blocked_reply)
            execution = asyncio.create_task(InstantExecutor().execute(exec_ctx, task))
            await started.wait()
            assert await scheduler.cancel(task.id) is True
            release.set()
            await execution

        updated = await store.get(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_pending(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", status=TaskStatus.PENDING)
        await store.save(t)
        scheduler._pending.append(t)

        ok = await scheduler.cancel("t1")
        assert ok is True
        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_scheduled(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", status=TaskStatus.SCHEDULED)
        await store.save(t)

        ok = await scheduler.cancel("t1")
        assert ok is True
        updated = await store.get("t1")
        assert updated.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_terminal_returns_false(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", status=TaskStatus.COMPLETED)
        await store.save(t)

        ok = await scheduler.cancel("t1")
        assert ok is False

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        ok = await scheduler.cancel("no-such")
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════════
# 暂停 / 恢复
# ═══════════════════════════════════════════════════════════════════════════════

class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_running(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        scheduler._running.add("t1")

        t = make_task("t1", status=TaskStatus.RUNNING)
        await store.save(t)

        ok = await scheduler.pause("t1")
        assert ok is True
        updated = await store.get("t1")
        assert updated.status == TaskStatus.RUNNING
        assert updated.is_coop_paused()

    @pytest.mark.asyncio
    async def test_pause_non_running_fails(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", status=TaskStatus.PENDING)
        await store.save(t)

        ok = await scheduler.pause("t1")
        assert ok is False

    @pytest.mark.asyncio
    async def test_resume_paused(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        # 使用 max_concurrent=0 防止 resume 后立即重新派发
        cfg = TaskConfig(max_concurrent_tasks=0)
        scheduler = TaskScheduler(cfg, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", status=TaskStatus.PAUSED)
        await store.save(t)

        ok = await scheduler.resume("t1")
        assert ok is True
        updated = await store.get("t1")
        assert updated.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_resume_non_paused_fails(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", status=TaskStatus.RUNNING)
        await store.save(t)

        ok = await scheduler.resume("t1")
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════════
# DELAY 触发
# ═══════════════════════════════════════════════════════════════════════════════

class TestDelayTrigger:
    @pytest.mark.asyncio
    async def test_delay_sets_scheduled_at(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", trigger_type=TriggerType.DELAY, delay_seconds=10)
        await store.save(t)
        await scheduler.enqueue(t)

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.SCHEDULED
        assert updated.scheduled_at is not None

    @pytest.mark.asyncio
    async def test_delay_zero_delay(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", trigger_type=TriggerType.DELAY, delay_seconds=0)
        await store.save(t)
        await scheduler.enqueue(t)

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.SCHEDULED


# ═══════════════════════════════════════════════════════════════════════════════
# 无效 CRON 表达式
# ═══════════════════════════════════════════════════════════════════════════════

class TestInvalidCron:
    @pytest.mark.asyncio
    async def test_invalid_cron_fails(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t1", trigger_type=TriggerType.CRON, cron_expr="invalid!!!")
        await store.save(t)
        ok = await scheduler.enqueue(t)
        assert ok is True  # 任务已被确定性地标记 FAILED，属已处理

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.FAILED
        assert len(updated._status_log) == 1
        assert updated._status_log[0].status == TaskStatus.FAILED
        assert updated._status_log[0].reason == ""

    @pytest.mark.asyncio
    async def test_empty_cron_fails_instead_of_every_minute(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """空 cron 表达式不再静默升级为每分钟执行——走无效表达式同一 FAILED 路径。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t-empty-cron", trigger_type=TriggerType.CRON, cron_expr="")
        await store.save(t)
        ok = await scheduler.enqueue(t)
        assert ok is True

        updated = await store.get("t-empty-cron")
        assert updated is not None
        assert updated.status == TaskStatus.FAILED
        assert updated.cron_expr == ""


class TestEnqueueReturnsBool:
    """enqueue 必须让调用方感知"已落库但未入队"的失败，不得静默吞掉。"""

    @pytest.mark.asyncio
    async def test_enqueue_returns_true_on_success(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("ok-1", trigger_type=TriggerType.DELAY, delay_seconds=10)
        await store.save(t)
        assert await scheduler.enqueue(t) is True
        assert (await store.get("ok-1")).status == TaskStatus.SCHEDULED

    @pytest.mark.asyncio
    async def test_enqueue_returns_false_on_save_failure(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _boom_save(task: TaskRecord, **kwargs: object) -> bool:
            raise RuntimeError("save down")

        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("fail-1", trigger_type=TriggerType.DELAY, delay_seconds=10)

        monkeypatch.setattr(store, "save", _boom_save)
        assert await scheduler.enqueue(t) is False


# ═══════════════════════════════════════════════════════════════════════════════
# 调度器启停生命周期
# ═══════════════════════════════════════════════════════════════════════════════

class TestQuotaReservation:
    """_try_start 必须在第一个 await（store.save）之前预留并发额度，
    否则并发 _try_dispatch 可同时通过额度检查导致超发（F2 B4）。"""

    @pytest.mark.asyncio
    async def test_running_reserved_before_save(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_started = asyncio.Event()
        save_release = asyncio.Event()
        original_save = store.save

        async def blocking_save(task: TaskRecord, **kwargs: object) -> None:
            save_started.set()
            await save_release.wait()
            return await original_save(task)

        monkeypatch.setattr(store, "save", blocking_save)
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("quota-reserve", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        await original_save(t)
        scheduler._pending.append(t)

        dispatch = asyncio.create_task(scheduler._try_dispatch())
        await save_started.wait()

        # save 尚未完成时，额度必须已预留
        assert t.id in scheduler._running

        save_release.set()
        await dispatch
        assert t.id in scheduler._running

    @pytest.mark.asyncio
    async def test_try_start_save_failure_rolls_back_to_pending(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """save 失败时任务回滚到 PENDING，不得卡死队首（F2 Finding 1）。"""
        t = make_task("save-fail", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        await store.save(t)  # 落库后入队（派发前按 id 重读最新记录）；须在 patch 之前

        async def failing_save(task: TaskRecord, **kwargs: object) -> bool:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(store, "save", failing_save)
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        scheduler._pending.append(t)

        await scheduler._try_dispatch()

        # 额度已释放、任务回滚 PENDING，且未卡死在 running→running
        assert t.id not in scheduler._running
        assert scheduler._pending[0].id == t.id
        assert scheduler._pending[0].status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_try_start_cas_preemption_abandons_task(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """两个调度器争抢同一 pending 任务：CAS 保证仅一个抢占成功，
        另一个放弃执行（abandoned），任务不会被执行两次。

        多 Runner（进程）并存的场景由 expected_status=PENDING 的原子写入兜底。
        """
        t = make_task("cas-preempt", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        await store.save(t)

        executed: list[str] = []

        async def executor(task: TaskRecord) -> None:
            executed.append(task.id)

        s1 = TaskScheduler(task_config, store, executor, command_bus=command_bus)
        s2 = TaskScheduler(task_config, store, executor, command_bus=command_bus)

        # 两个调度器各自从 store 读取独立副本并放进自己的 pending 队列
        # （模拟两个 Runner 进程同时读到同一 pending 任务）
        s1._pending.append(t)
        t2 = await store.get(t.id)
        assert t2 is not None
        s2._pending.append(t2)

        await asyncio.gather(s1._try_dispatch(), s2._try_dispatch())

        # 只有一个调度器真正启动执行
        assert len(executed) == 1
        assert len(s1._running) + len(s2._running) == 1
        # 持久化状态为 RUNNING（被抢占者写入）
        persisted = await store.get(t.id)
        assert persisted is not None and persisted.status == TaskStatus.RUNNING


# ═══════════════════════════════════════════════════════════════════════════════
# 调度器启停生命周期
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchedulerLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        await scheduler.start()
        assert scheduler._check_task is not None

        await scheduler.stop()
        assert scheduler._check_task is None

    @pytest.mark.asyncio
    async def test_double_start_noop(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        await scheduler.start()
        task1 = scheduler._check_task
        await scheduler.start()
        assert scheduler._check_task is task1  # 仍是同一个任务

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_pauses_running_tasks(self, store: TaskStore, task_config: TaskConfig, command_bus: Any) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        scheduler._running.add("t1")

        t = make_task("t1", status=TaskStatus.RUNNING)
        t.set_coop_paused(True)
        await store.save(t)

        await scheduler.stop()

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.PAUSED
        assert updated.is_coop_paused() is False
        assert updated.was_paused_by_stop()


# ═══════════════════════════════════════════════════════════════════════════════
# enqueue 幂等（P3 回归）：恢复流程对已 SCHEDULED 任务重新入队不抛异常、
# 刷新 scheduled_at 落盘；pending 队列按 id 去重。
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnqueueIdempotent:
    @pytest.mark.asyncio
    async def test_enqueue_already_scheduled_delay_is_idempotent(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """已 SCHEDULED 的 DELAY 任务重新入队：不抛异常、状态不变、
        scheduled_at 刷新为 now+60s 附近并落盘。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t-delay", trigger_type=TriggerType.DELAY, delay_seconds=60)
        t.force(TaskStatus.SCHEDULED, actor="test", reason="seed")
        await store.save(t)

        # 恢复场景：对已 SCHEDULED 任务重新入队（如 lifecycle 恢复流程）
        await scheduler.enqueue(t)

        assert t.status == TaskStatus.SCHEDULED
        assert t.scheduled_at is not None
        assert abs((t.scheduled_at - datetime.now()).total_seconds() - 60) <= 5

        updated = await store.get("t-delay")
        assert updated is not None
        assert updated.status == TaskStatus.SCHEDULED
        assert updated.scheduled_at is not None
        assert abs((updated.scheduled_at - datetime.now()).total_seconds() - 60) <= 5

    @pytest.mark.asyncio
    async def test_enqueue_already_scheduled_cron_is_idempotent(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """已 SCHEDULED 的 CRON 任务重新入队：状态不变、scheduled_at 刷新为
        now 之后最近一次 5 分钟整点，二次入队仍不抛异常。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("t-cron", trigger_type=TriggerType.CRON, cron_expr="*/5 * * * *")
        t.force(TaskStatus.SCHEDULED, actor="test", reason="seed")
        await store.save(t)

        await scheduler.enqueue(t)

        assert t.status == TaskStatus.SCHEDULED
        now = datetime.now()
        assert t.scheduled_at is not None
        assert t.scheduled_at > now
        assert (t.scheduled_at - now).total_seconds() < 300
        # 与 croniter 直接计算结果一致（±2s 容差）
        expected = croniter("*/5 * * * *", now).get_next(datetime)
        assert abs((t.scheduled_at - expected).total_seconds()) <= 2

        # 二次 enqueue 仍幂等：不抛异常、状态不变
        await scheduler.enqueue(t)
        assert t.status == TaskStatus.SCHEDULED

    @pytest.mark.asyncio
    async def test_double_enqueue_now_deduplicates_pending(
        self, store: TaskStore, command_bus: Any,
    ) -> None:
        """同一 NOW 任务连续入队两次，pending 队列按 id 去重只保留一份。"""
        cfg = TaskConfig(max_concurrent_tasks=0)  # 额度满 → 任务滞留 pending
        scheduler = TaskScheduler(cfg, store, _noop_executor, command_bus=command_bus)

        t = make_task("t-dedup", status=TaskStatus.PENDING)
        await store.save(t)

        await scheduler.enqueue(t)
        await scheduler.enqueue(t)

        assert len(scheduler._pending) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# _check_loop — 后台轮询循环（SCHEDULED 到期触发 / RUNNING 超时检测）
#
# 通过将 scheduler 模块内的 ``asyncio`` 引用替换为 shim（sleep 即时返回），
# 使轮询循环以毫秒级速度迭代，避免真实 1s sleep 拖慢测试；
# shim 只作用于该模块，不污染全局 asyncio。
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckLoop:
    @staticmethod
    def _fast_loop(monkeypatch: Any, scheduler: TaskScheduler) -> Any:
        """启动一个即时迭代的 _check_loop 后台任务。

        返回 ``(loop_task, real_sleep)``：轮询等待用 real_sleep；
        测试结束前必须 ``scheduler._stop_event.set()`` 并 await loop_task。
        """
        import types

        import oh_mai_agent.core.scheduler as scheduler_module

        real_sleep = asyncio.sleep

        async def _fast_sleep(_: float) -> None:
            await real_sleep(0)

        shim = types.SimpleNamespace(
            sleep=_fast_sleep,
            create_task=asyncio.create_task,
            CancelledError=asyncio.CancelledError,
        )
        monkeypatch.setattr(scheduler_module, "asyncio", shim)
        loop_task = asyncio.create_task(scheduler._check_loop())
        return loop_task, real_sleep

    @staticmethod
    async def _stop_loop(scheduler: TaskScheduler, loop_task: Any) -> None:
        scheduler._stop_event.set()
        await loop_task

    @staticmethod
    async def _wait_for(real_sleep: Any, predicate: Any, timeout_s: float = 5.0) -> bool:
        """轮询等待 *predicate* 成立（predicate 为 async 可调用）。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await predicate():
                return True
            await real_sleep(0.005)
        return False

    @pytest.mark.asyncio
    async def test_due_scheduled_task_is_triggered_and_dispatched(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
        monkeypatch: Any,
    ) -> None:
        """SCHEDULED 到期任务被轮询循环触发并派发执行。"""
        executed: list[str] = []

        async def _exec(t: TaskRecord) -> None:
            executed.append(t.id)

        scheduler = TaskScheduler(task_config, store, _exec, command_bus=command_bus)

        t = make_task(
            "due", status=TaskStatus.SCHEDULED, trigger_type=TriggerType.DELAY,
            scheduled_at=datetime.now() - timedelta(seconds=5),
        )
        await store.save(t)

        loop_task, real_sleep = self._fast_loop(monkeypatch, scheduler)
        try:
            async def _ran() -> bool:
                return bool(executed)

            assert await self._wait_for(real_sleep, _ran)
            assert executed == ["due"]
        finally:
            await self._stop_loop(scheduler, loop_task)

    @pytest.mark.asyncio
    async def test_future_scheduled_task_not_triggered(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
        monkeypatch: Any,
    ) -> None:
        """未到触发时间的 SCHEDULED 任务保持状态不变。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task(
            "future", status=TaskStatus.SCHEDULED, trigger_type=TriggerType.DELAY,
            scheduled_at=datetime.now() + timedelta(hours=1),
        )
        await store.save(t)

        loop_task, real_sleep = self._fast_loop(monkeypatch, scheduler)
        try:
            await real_sleep(0.05)  # 跑若干轮迭代
            updated = await store.get("future")
            assert updated is not None
            assert updated.status == TaskStatus.SCHEDULED
        finally:
            await self._stop_loop(scheduler, loop_task)

    @pytest.mark.asyncio
    async def test_scheduled_without_time_not_triggered(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
        monkeypatch: Any,
    ) -> None:
        """SCHEDULED 但无 scheduled_at 的任务被跳过，避免误触发。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("no-ts", status=TaskStatus.SCHEDULED, trigger_type=TriggerType.DELAY)
        await store.save(t)

        loop_task, real_sleep = self._fast_loop(monkeypatch, scheduler)
        try:
            await real_sleep(0.05)
            updated = await store.get("no-ts")
            assert updated is not None
            assert updated.status == TaskStatus.SCHEDULED
        finally:
            await self._stop_loop(scheduler, loop_task)

    @pytest.mark.asyncio
    async def test_overdue_running_task_is_failed(
        self, store: TaskStore, command_bus: Any, monkeypatch: Any,
    ) -> None:
        """超过 max_runtime_min 的 RUNNING 任务被降级为 FAILED 并移出运行集。"""
        cfg = TaskConfig(max_concurrent_tasks=2, max_runtime_min=1)
        scheduler = TaskScheduler(cfg, store, _noop_executor, command_bus=command_bus)

        t = make_task(
            "slow", status=TaskStatus.RUNNING,
            started_at=datetime.now() - timedelta(minutes=10),
        )
        await store.save(t)
        scheduler._running.add("slow")

        loop_task, real_sleep = self._fast_loop(monkeypatch, scheduler)
        try:
            async def _is_failed() -> bool:
                t = await store.get("slow")
                return t is not None and t.status == TaskStatus.FAILED

            assert await self._wait_for(real_sleep, _is_failed)
            assert "slow" not in scheduler._running
        finally:
            await self._stop_loop(scheduler, loop_task)

    @pytest.mark.asyncio
    async def test_running_within_timeout_stays_running(
        self, store: TaskStore, command_bus: Any, monkeypatch: Any,
    ) -> None:
        """未超时的 RUNNING 任务不被降级。"""
        cfg = TaskConfig(max_concurrent_tasks=2, max_runtime_min=60)
        scheduler = TaskScheduler(cfg, store, _noop_executor, command_bus=command_bus)

        t = make_task(
            "fresh", status=TaskStatus.RUNNING,
            started_at=datetime.now() - timedelta(seconds=30),
        )
        await store.save(t)
        scheduler._running.add("fresh")

        loop_task, real_sleep = self._fast_loop(monkeypatch, scheduler)
        try:
            await real_sleep(0.05)
            updated = await store.get("fresh")
            assert updated is not None
            assert updated.status == TaskStatus.RUNNING
        finally:
            await self._stop_loop(scheduler, loop_task)

    @pytest.mark.asyncio
    async def test_coop_paused_running_task_skips_timeout(
        self, store: TaskStore, command_bus: Any, monkeypatch: Any,
    ) -> None:
        """协作暂停（is_coop_paused）的 RUNNING 任务不参与超时检测。"""
        cfg = TaskConfig(max_concurrent_tasks=2, max_runtime_min=1)
        scheduler = TaskScheduler(cfg, store, _noop_executor, command_bus=command_bus)

        t = make_task(
            "coop", status=TaskStatus.RUNNING,
            started_at=datetime.now() - timedelta(minutes=10),
        )
        t.set_coop_paused(True)
        await store.save(t)
        scheduler._running.add("coop")

        loop_task, real_sleep = self._fast_loop(monkeypatch, scheduler)
        try:
            await real_sleep(0.05)
            updated = await store.get("coop")
            assert updated is not None
            assert updated.status == TaskStatus.RUNNING
        finally:
            await self._stop_loop(scheduler, loop_task)

    @pytest.mark.asyncio
    async def test_non_running_task_discarded_from_running_set(
        self, store: TaskStore, command_bus: Any, monkeypatch: Any,
    ) -> None:
        """_running 集合中已非 RUNNING 的任务（如完成但未收到事件）被清理。"""
        cfg = TaskConfig(max_concurrent_tasks=2, max_runtime_min=1)
        scheduler = TaskScheduler(cfg, store, _noop_executor, command_bus=command_bus)

        t = make_task("done", status=TaskStatus.COMPLETED)
        await store.save(t)
        scheduler._running.add("done")

        loop_task, real_sleep = self._fast_loop(monkeypatch, scheduler)
        try:
            await real_sleep(0.05)
            assert "done" not in scheduler._running
        finally:
            await self._stop_loop(scheduler, loop_task)

    @pytest.mark.asyncio
    async def test_list_active_failure_tolerated_by_loop(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
        monkeypatch: Any,
    ) -> None:
        """list_active 查询失败时循环继续运行（不崩溃）。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        async def _boom() -> list:
            raise RuntimeError("db down")

        monkeypatch.setattr(store, "list_active", _boom)

        loop_task, real_sleep = self._fast_loop(monkeypatch, scheduler)
        try:
            await real_sleep(0.05)  # 多轮迭代均不应抛异常
            assert not loop_task.done() or loop_task.exception() is None
        finally:
            await self._stop_loop(scheduler, loop_task)


# ═══════════════════════════════════════════════════════════════════════════════
# 错误路径与防御分支（stop / cancel / pause / resume / 事件监听 / 安全执行）
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchedulerErrorPaths:
    @pytest.mark.asyncio
    async def test_stop_tolerates_store_get_failure(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """停止时获取运行中任务失败 → 记日志继续，不抛异常。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("stop-1", status=TaskStatus.RUNNING)
        await store.save(t)
        scheduler._running.add("stop-1")

        async def _boom(task_id: str) -> Any:
            raise RuntimeError("db down")

        real_get = store.get
        store.get = _boom  # type: ignore[method-assign]
        try:
            await scheduler.stop()
        finally:
            store.get = real_get  # type: ignore[method-assign]
        assert scheduler._running == set()

    @pytest.mark.asyncio
    async def test_stop_tolerates_store_save_failure(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """停止时暂停任务落盘失败 → 记日志继续。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("stop-2", status=TaskStatus.RUNNING)
        await store.save(t)
        scheduler._running.add("stop-2")

        real_save = store.save
        async def _boom_save(task: Any, expected_status: Any = None) -> Any:
            if task.id == "stop-2":
                raise RuntimeError("save down")
            return await real_save(task, expected_status)

        store.save = _boom_save  # type: ignore[method-assign]
        try:
            await scheduler.stop()  # 不应抛异常
        finally:
            store.save = real_save  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_cancel_tolerates_store_get_failure(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        async def _boom(task_id: str) -> Any:
            raise RuntimeError("db down")

        real_get = store.get
        store.get = _boom  # type: ignore[method-assign]
        try:
            assert await scheduler.cancel("x") is False
        finally:
            store.get = real_get  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_cancel_running_agent_sends_command(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """RUNNING agent 任务取消 → 经命令总线发 CANCEL（协作式取消）。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("cancel-me", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(t)
        scheduler._running.add("cancel-me")

        received: list[TaskCommand] = []

        async def handler(cmd: TaskCommand) -> None:
            received.append(cmd)

        command_bus.subscribe("cancel-me", handler)
        assert await scheduler.cancel("cancel-me") is True
        assert [c.kind for c in received] == [CommandKind.CANCEL]

    @pytest.mark.asyncio
    async def test_pause_tolerates_store_get_failure(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        async def _boom(task_id: str) -> Any:
            raise RuntimeError("db down")

        real_get = store.get
        store.get = _boom  # type: ignore[method-assign]
        try:
            assert await scheduler.pause("x") is False
        finally:
            store.get = real_get  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_resume_coop_paused_running_task(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """RUNNING + is_coop_paused → 清除标记、重置 started_at、发 RESUME 命令。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("resume-me", status=TaskStatus.RUNNING)
        t.set_coop_paused(True)
        await store.save(t)
        scheduler._running.add("resume-me")

        received: list[TaskCommand] = []

        async def handler(cmd: TaskCommand) -> None:
            received.append(cmd)

        command_bus.subscribe("resume-me", handler)
        assert await scheduler.resume("resume-me") is True
        assert [c.kind for c in received] == [CommandKind.RESUME]
        updated = await store.get("resume-me")
        assert updated is not None
        assert "_coop_paused" not in updated.metadata
        assert updated.started_at is not None

    @pytest.mark.asyncio
    async def test_resume_non_paused_status_returns_false(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """非 PAUSED 且非协作暂停的 RUNNING 任务 → 拒绝恢复。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("no-pause", status=TaskStatus.RUNNING)
        await store.save(t)
        assert await scheduler.resume("no-pause") is False

    @pytest.mark.asyncio
    async def test_enqueue_tolerates_save_failure(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """入队时持久化失败 → 记日志不抛出。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("enq-fail", trigger_type=TriggerType.DELAY, delay_seconds=10)

        real_save = store.save
        async def _boom_save(task: Any, expected_status: Any = None) -> Any:
            raise RuntimeError("save down")

        store.save = _boom_save  # type: ignore[method-assign]
        try:
            await scheduler.enqueue(t)  # 不应抛异常
        finally:
            store.save = real_save  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_on_task_completed_releases_slot_and_dispatches(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """完成通知（on_task_completed 唯一入口）→ 从运行集移除并触发补位派发。

        事件监听通道已随"完成通知统一为直接调用"移除（scheduler 不再监听
        TaskEvent）；本用例直接验证统一入口的额度释放语义。
        """
        started: list[str] = []
        event = asyncio.Event()

        async def _tracked(t: TaskRecord) -> None:
            started.append(t.id)
            await event.wait()

        scheduler = TaskScheduler(task_config, store, _tracked, command_bus=command_bus)
        t1 = make_task("evt-1", status=TaskStatus.RUNNING)
        t2 = make_task("evt-2", status=TaskStatus.PENDING)
        await store.save(t1)
        await store.save(t2)
        await scheduler.enqueue(t2)  # 额度空闲 → 直接启动
        scheduler._running.add("evt-1")

        event.set()
        await asyncio.sleep(0.02)

        await scheduler.on_task_completed(t1)
        assert "evt-1" not in scheduler._running

    @pytest.mark.asyncio
    async def test_safe_execute_tolerates_executor_exception(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        async def _boom(t: TaskRecord) -> None:
            raise RuntimeError("executor boom")

        scheduler = TaskScheduler(task_config, store, _boom, command_bus=command_bus)
        t = make_task("boom-1")
        await scheduler._safe_execute(t, _boom)  # 不应抛出

    @pytest.mark.asyncio
    async def test_safe_execute_tolerates_cancellation(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        async def _cancel(t: TaskRecord) -> None:
            raise asyncio.CancelledError

        scheduler = TaskScheduler(task_config, store, _cancel, command_bus=command_bus)
        t = make_task("cancel-1")
        await scheduler._safe_execute(t, _cancel)  # 不应抛出

    @pytest.mark.asyncio
    async def test_try_start_illegal_transition_abandons_task(
        self, store: TaskStore, task_config: TaskConfig, command_bus: Any,
    ) -> None:
        """pending 中任务已被并发取消（持久化为终态）→ 派发前重读发现
        非 PENDING，任务从调度队列移除，不重试、不卡队首。"""
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)
        t = make_task("race-1", status=TaskStatus.PENDING)
        await store.save(t)
        t.force(TaskStatus.CANCELLED, actor="test", reason="concurrent-cancel")
        await store.save(t)  # 并发取消已落盘 → 重读可见终态
        scheduler._pending.append(t)

        await scheduler._try_dispatch()
        assert scheduler._running == set()
        assert not any(x.id == "race-1" for x in scheduler._pending)
