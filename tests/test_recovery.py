"""oh_mai_agent.domain.recovery（TaskRecovery 决策逻辑）的测试。"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from conftest import make_task

from oh_mai_agent.config import TaskConfig
from oh_mai_agent.core.scheduler import TaskScheduler
from oh_mai_agent.domain.recovery import RecoveryAction, TaskRecovery
from oh_mai_agent.domain.task_record import StatusChange, TaskRecord, TaskStatus
from oh_mai_agent.domain.task_record import TaskLevel, TriggerType
from oh_mai_agent.domain.task_store import TaskStore


def _make_record(status: TaskStatus) -> TaskRecord:
    return TaskRecord(
        id="task-001",
        title="test",
        intent="test",
        level=TaskLevel.AGENT,
        status=status,
        owner="u1",
        stream_id="s1",
        platform="qq",
        trigger_type=TriggerType.NOW,
    )


class TestRecoveryRunningToPending:
    """恢复期间 RUNNING → PENDING：force 强制改状态 + metadata 标记 + 审计日志。"""

    def test_returns_pending_action(self) -> None:
        """给定 RUNNING 记录，当调用 recover() 时，
        返回的 action 为 PENDING。"""
        r = _make_record(TaskStatus.RUNNING)
        assert TaskRecovery.recover(r) == RecoveryAction.PENDING

    def test_sets_status_to_pending(self) -> None:
        """给定 RUNNING 记录，当调用 recover() 时，
        record.status 变为 PENDING。"""
        r = _make_record(TaskStatus.RUNNING)
        TaskRecovery.recover(r)
        assert r.status == TaskStatus.PENDING

    def test_logs_force_entry_with_actor_and_reason(self) -> None:
        """给定 RUNNING 记录，当调用 recover() 时，
        _status_log 新增一条 force 记录，actor='recovery'、
        reason='recovered_from_running'。"""
        r = _make_record(TaskStatus.RUNNING)
        TaskRecovery.recover(r)
        assert len(r._status_log) == 1
        entry: StatusChange = r._status_log[0]
        assert entry.status == TaskStatus.PENDING
        assert entry.actor == "recovery"
        assert entry.reason == "recovered_from_running"

    def test_sets_recovered_metadata(self) -> None:
        """给定 RUNNING 记录，当调用 recover() 时，
        was_recovered_from_running() 为 True。"""
        r = _make_record(TaskStatus.RUNNING)
        TaskRecovery.recover(r)
        assert r.was_recovered_from_running()


class TestRecoveryWaitingInputKept:
    """恢复期间 WAITING_INPUT 任务原样保留，不做任何修改。"""

    def test_returns_waiting_action(self) -> None:
        """给定 WAITING_INPUT 记录，当调用 recover() 时，
        返回的 action 为 WAITING。"""
        r = _make_record(TaskStatus.WAITING_INPUT)
        assert TaskRecovery.recover(r) == RecoveryAction.WAITING

    def test_keeps_status_unchanged(self) -> None:
        """给定 WAITING_INPUT 记录，当调用 recover() 时，
        状态保持为 WAITING_INPUT。"""
        r = _make_record(TaskStatus.WAITING_INPUT)
        TaskRecovery.recover(r)
        assert r.status == TaskStatus.WAITING_INPUT

    def test_no_status_log_entries_added(self) -> None:
        """给定 WAITING_INPUT 记录，当调用 recover() 时，
        _status_log 仍为空。"""
        r = _make_record(TaskStatus.WAITING_INPUT)
        TaskRecovery.recover(r)
        assert len(r._status_log) == 0

    def test_no_metadata_mutation(self) -> None:
        """给定 WAITING_INPUT 记录，当调用 recover() 时，
        metadata 保持不变。"""
        r = _make_record(TaskStatus.WAITING_INPUT)
        initial_meta = dict(r.metadata)
        TaskRecovery.recover(r)
        assert r.metadata == initial_meta


class TestRecoveryScheduledEnqueue:
    """SCHEDULED 任务返回 ENQUEUE —— 由调用方负责重新入队。"""

    def test_returns_enqueue_action(self) -> None:
        r = _make_record(TaskStatus.SCHEDULED)
        assert TaskRecovery.recover(r) == RecoveryAction.ENQUEUE

    def test_keeps_scheduled_status(self) -> None:
        """给定 SCHEDULED 记录，当调用 recover() 时，
        状态保持 SCHEDULED（不做变更）。"""
        r = _make_record(TaskStatus.SCHEDULED)
        TaskRecovery.recover(r)
        assert r.status == TaskStatus.SCHEDULED


class TestRecoveryPausedKept:
    """PAUSED 任务返回 PAUSED —— 仅支持手动恢复。"""

    def test_returns_paused_action(self) -> None:
        r = _make_record(TaskStatus.PAUSED)
        assert TaskRecovery.recover(r) == RecoveryAction.PAUSED

    def test_keeps_paused_status(self) -> None:
        r = _make_record(TaskStatus.PAUSED)
        TaskRecovery.recover(r)
        assert r.status == TaskStatus.PAUSED


class TestRecoveryEnqueueScheduledNoError:
    """P3 回归：恢复流程对已 SCHEDULED 任务调 enqueue，幂等承接、无 error 噪音。"""

    @pytest.mark.asyncio
    async def test_recovery_enqueue_scheduled_task_no_error(
        self, real_store: TaskStore, command_bus: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def _noop(task: TaskRecord) -> None:
            pass

        await real_store.init()
        scheduler = TaskScheduler(
            TaskConfig(max_concurrent_tasks=2), real_store,
            _noop, command_bus=command_bus,
        )

        task = make_task("recover-sched", trigger_type=TriggerType.DELAY, delay_seconds=60)
        task.force(TaskStatus.SCHEDULED, actor="test", reason="seed")
        await real_store.save(task)

        with caplog.at_level(logging.ERROR, logger="oh_mai_agent.core.scheduler"):
            await scheduler.enqueue(task)

        assert task.status == TaskStatus.SCHEDULED
        updated = await real_store.get("recover-sched")
        assert updated is not None
        assert updated.status == TaskStatus.SCHEDULED
        assert not any(
            r.name == "oh_mai_agent.core.scheduler"
            and r.levelno >= logging.ERROR
            and "发生异常" in r.getMessage()
            for r in caplog.records
        )
