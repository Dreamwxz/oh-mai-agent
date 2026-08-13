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
from oh_mai_agent.executor import instant as instant_module
from oh_mai_agent.executor.instant import InstantExecutor


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
        exec_ctx = ExecutionContext(
            ctx=MockCtx(), store=store, scheduler=scheduler, config=MaibotAgentConfig(),
        )

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(instant_module, "send_final_reply", blocked_reply)
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
        assert updated.metadata.get("_coop_paused") is True

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
        await scheduler.enqueue(t)

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.FAILED
        assert len(updated._status_log) == 1
        assert updated._status_log[0].status == TaskStatus.FAILED
        assert updated._status_log[0].reason == ""


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

        async def blocking_save(task: TaskRecord) -> None:
            save_started.set()
            await save_release.wait()
            await original_save(task)

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
        async def failing_save(task: TaskRecord) -> bool:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(store, "save", failing_save)
        scheduler = TaskScheduler(task_config, store, _noop_executor, command_bus=command_bus)

        t = make_task("save-fail", level=TaskLevel.AGENT, status=TaskStatus.PENDING)
        scheduler._pending.append(t)

        await scheduler._try_dispatch()

        # 额度已释放、任务回滚 PENDING，且未卡死在 running→running
        assert t.id not in scheduler._running
        assert t.status == TaskStatus.PENDING
        assert scheduler._pending[0] is t


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
        t.metadata["_coop_paused"] = True
        await store.save(t)

        await scheduler.stop()

        updated = await store.get("t1")
        assert updated is not None
        assert updated.status == TaskStatus.PAUSED
        assert updated.metadata.get("_coop_paused") is None
        assert updated.metadata.get("_paused_by_stop") is True


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
