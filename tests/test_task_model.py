"""oh_mai_agent.domain.task_record 的测试 —— 状态机、序列化、格式化。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from oh_mai_agent.domain.task_record import (
    TaskRecord,
    TaskLevel,
    TaskStatus,
    TaskStatusError,
    TriggerType,
    _ALLOWED_TRANSITIONS,
    _TERMINAL_STATUSES,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskLevel:
    def test_values(self) -> None:
        assert TaskLevel.INSTANT.value == "instant"
        assert TaskLevel.AGENT.value == "agent"

    def test_from_string(self) -> None:
        assert TaskLevel("instant") == TaskLevel.INSTANT
        assert TaskLevel("agent") == TaskLevel.AGENT


class TestTaskStatus:
    def test_values(self) -> None:
        assert TaskStatus.SCHEDULED.value == "scheduled"
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.RUNNING.value == "running"
        assert TaskStatus.WAITING_INPUT.value == "waiting_input"
        assert TaskStatus.COMPLETED.value == "completed"

    def test_terminal_statuses(self) -> None:
        assert TaskStatus.COMPLETED in _TERMINAL_STATUSES
        assert TaskStatus.FAILED in _TERMINAL_STATUSES
        assert TaskStatus.CANCELLED in _TERMINAL_STATUSES
        assert TaskStatus.PENDING not in _TERMINAL_STATUSES
        assert TaskStatus.RUNNING not in _TERMINAL_STATUSES


class TestTriggerType:
    def test_values(self) -> None:
        assert TriggerType.NOW.value == "now"
        assert TriggerType.DELAY.value == "delay"
        assert TriggerType.CRON.value == "cron"


# ═══════════════════════════════════════════════════════════════════════════════
# TaskRecord 构造与默认值
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaskConstruction:
    def test_minimal_required_fields(self) -> None:
        t = TaskRecord(id="t1", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq")
        assert t.id == "t1"
        assert t.title == "T"
        assert t.intent == "I"
        assert t.level == TaskLevel.AGENT
        assert t.owner == "qq:1"
        assert t.stream_id == "qq:1"
        assert t.platform == "qq"

    def test_defaults(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq")
        assert t.status == TaskStatus.PENDING
        assert t.trigger_type == TriggerType.NOW
        assert t.priority == 0
        assert t.max_runtime_min == 0
        assert t.metadata == {}
        assert isinstance(t.created_at, datetime)
        assert isinstance(t.updated_at, datetime)

    def test_custom_trigger(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 trigger_type=TriggerType.CRON, cron_expr="*/5 * * * *",
                 priority=10)
        assert t.trigger_type == TriggerType.CRON
        assert t.cron_expr == "*/5 * * * *"
        assert t.priority == 10

    def test_delay_task(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.INSTANT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 trigger_type=TriggerType.DELAY, delay_seconds=300)
        assert t.trigger_type == TriggerType.DELAY
        assert t.delay_seconds == 300


# ═══════════════════════════════════════════════════════════════════════════════
# 状态机转换
# ═══════════════════════════════════════════════════════════════════════════════

class TestTransitions:
    @staticmethod
    def _task(status: TaskStatus = TaskStatus.PENDING) -> TaskRecord:
        return TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                    owner="qq:1", stream_id="qq:1", platform="qq", status=status)

    # ── 合法 ───
    def test_scheduled_to_pending(self) -> None:
        t = self._task(TaskStatus.SCHEDULED)
        t.transition(TaskStatus.PENDING)
        assert t.status == TaskStatus.PENDING

    def test_scheduled_to_cancelled(self) -> None:
        t = self._task(TaskStatus.SCHEDULED)
        t.transition(TaskStatus.CANCELLED)
        assert t.status == TaskStatus.CANCELLED

    def test_pending_to_running(self) -> None:
        t = self._task(TaskStatus.PENDING)
        t.transition(TaskStatus.RUNNING)
        assert t.status == TaskStatus.RUNNING

    def test_pending_to_cancelled(self) -> None:
        t = self._task(TaskStatus.PENDING)
        t.transition(TaskStatus.CANCELLED)
        assert t.status == TaskStatus.CANCELLED

    def test_running_to_waiting_input(self) -> None:
        t = self._task(TaskStatus.RUNNING)
        t.transition(TaskStatus.WAITING_INPUT)
        assert t.status == TaskStatus.WAITING_INPUT

    def test_running_to_paused(self) -> None:
        t = self._task(TaskStatus.RUNNING)
        t.transition(TaskStatus.PAUSED)
        assert t.status == TaskStatus.PAUSED

    def test_running_to_completed(self) -> None:
        t = self._task(TaskStatus.RUNNING)
        t.transition(TaskStatus.COMPLETED)
        assert t.status == TaskStatus.COMPLETED

    def test_running_to_failed(self) -> None:
        t = self._task(TaskStatus.RUNNING)
        t.transition(TaskStatus.FAILED)
        assert t.status == TaskStatus.FAILED

    def test_waiting_input_to_running(self) -> None:
        t = self._task(TaskStatus.WAITING_INPUT)
        t.transition(TaskStatus.RUNNING)
        assert t.status == TaskStatus.RUNNING

    def test_waiting_input_to_paused(self) -> None:
        t = self._task(TaskStatus.WAITING_INPUT)
        t.transition(TaskStatus.PAUSED)
        assert t.status == TaskStatus.PAUSED

    def test_waiting_input_to_cancelled(self) -> None:
        t = self._task(TaskStatus.WAITING_INPUT)
        t.transition(TaskStatus.CANCELLED)
        assert t.status == TaskStatus.CANCELLED

    def test_waiting_input_to_failed(self) -> None:
        t = self._task(TaskStatus.WAITING_INPUT)
        t.transition(TaskStatus.FAILED)
        assert t.status == TaskStatus.FAILED

    def test_paused_to_pending(self) -> None:
        t = self._task(TaskStatus.PAUSED)
        t.transition(TaskStatus.PENDING)
        assert t.status == TaskStatus.PENDING

    def test_paused_to_cancelled(self) -> None:
        t = self._task(TaskStatus.PAUSED)
        t.transition(TaskStatus.CANCELLED)
        assert t.status == TaskStatus.CANCELLED

    # ── 非法（回归：非法转换必须抛异常） ──────────────────────
    def test_pending_to_completed_invalid(self) -> None:
        t = self._task(TaskStatus.PENDING)
        with pytest.raises(TaskStatusError, match="非法状态转换"):
            t.transition(TaskStatus.COMPLETED)

    def test_scheduled_to_running_invalid(self) -> None:
        t = self._task(TaskStatus.SCHEDULED)
        with pytest.raises(TaskStatusError):
            t.transition(TaskStatus.RUNNING)

    def test_completed_to_anything_invalid(self) -> None:
        t = self._task(TaskStatus.COMPLETED)
        with pytest.raises(TaskStatusError):
            t.transition(TaskStatus.RUNNING)
        with pytest.raises(TaskStatusError):
            t.transition(TaskStatus.PENDING)
        with pytest.raises(TaskStatusError):
            t.transition(TaskStatus.COMPLETED)

    def test_failed_to_anything_invalid(self) -> None:
        t = self._task(TaskStatus.FAILED)
        with pytest.raises(TaskStatusError):
            t.transition(TaskStatus.RUNNING)

    def test_cancelled_to_anything_invalid(self) -> None:
        t = self._task(TaskStatus.CANCELLED)
        with pytest.raises(TaskStatusError):
            t.transition(TaskStatus.PENDING)

    def test_updated_at_changes_on_transition(self) -> None:
        t = self._task(TaskStatus.PENDING)
        old_updated = t.updated_at
        import time
        time.sleep(0.001)
        t.transition(TaskStatus.RUNNING)
        assert t.updated_at > old_updated

    def test_is_terminal(self) -> None:
        t = self._task(TaskStatus.COMPLETED)
        assert t.is_terminal() is True
        t2 = self._task(TaskStatus.RUNNING)
        assert t2.is_terminal() is False


# ═══════════════════════════════════════════════════════════════════════════════
# 合法转换表完整性
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllowedTransitionsMap:
    def test_all_statuses_defined(self) -> None:
        for s in TaskStatus:
            assert s in _ALLOWED_TRANSITIONS, f"{s} missing from _ALLOWED_TRANSITIONS"

    def test_terminal_have_no_transitions(self) -> None:
        for s in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            assert _ALLOWED_TRANSITIONS[s] == set(), f"{s} should have no transitions"

    def test_non_terminal_have_transitions(self) -> None:
        for s in (TaskStatus.SCHEDULED, TaskStatus.PENDING, TaskStatus.RUNNING,
                  TaskStatus.WAITING_INPUT, TaskStatus.PAUSED):
            assert len(_ALLOWED_TRANSITIONS[s]) > 0, f"{s} should have transitions"


# ═══════════════════════════════════════════════════════════════════════════════
# status_info —— 状态信息
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatusInfo:
    def test_pending_returns_none_timestamp(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.PENDING)
        status, ts = t.status_info()
        assert status == TaskStatus.PENDING
        assert ts is None

    def test_completed_returns_none_timestamp(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.COMPLETED)
        status, ts = t.status_info()
        assert status == TaskStatus.COMPLETED
        assert ts is None

    def test_failed_returns_none_timestamp(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.FAILED)
        status, ts = t.status_info()
        assert status == TaskStatus.FAILED
        assert ts is None

    def test_cancelled_returns_none_timestamp(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.CANCELLED)
        status, ts = t.status_info()
        assert status == TaskStatus.CANCELLED
        assert ts is None

    def test_paused_returns_none_timestamp(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.PAUSED)
        status, ts = t.status_info()
        assert status == TaskStatus.PAUSED
        assert ts is None

    def test_running_returns_started_at(self) -> None:
        started = datetime.now() - timedelta(seconds=65)
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.RUNNING, started_at=started)
        status, ts = t.status_info()
        assert status == TaskStatus.RUNNING
        assert ts == started

    def test_running_without_started_at(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.RUNNING, started_at=None)
        status, ts = t.status_info()
        assert status == TaskStatus.RUNNING
        assert ts is None

    def test_scheduled_returns_scheduled_at(self) -> None:
        scheduled = datetime.now() + timedelta(seconds=30)
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.SCHEDULED, scheduled_at=scheduled)
        status, ts = t.status_info()
        assert status == TaskStatus.SCHEDULED
        assert ts == scheduled

    def test_scheduled_far_future(self) -> None:
        scheduled = datetime.now() + timedelta(days=2)
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.SCHEDULED, scheduled_at=scheduled)
        status, ts = t.status_info()
        assert status == TaskStatus.SCHEDULED
        assert ts == scheduled

    def test_scheduled_no_scheduled_at(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.SCHEDULED, scheduled_at=None)
        status, ts = t.status_info()
        assert status == TaskStatus.SCHEDULED
        assert ts is None

    def test_waiting_input_returns_updated_at(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.WAITING_INPUT)
        status, ts = t.status_info()
        assert status == TaskStatus.WAITING_INPUT
        assert ts is not None  # 构造器会设置 updated_at


# ═══════════════════════════════════════════════════════════════════════════════
# runtime_seconds —— 运行时长
# ═══════════════════════════════════════════════════════════════════════════════

class TestRuntimeSeconds:
    def test_running_with_started_at(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.RUNNING,
                 started_at=datetime.now() - timedelta(seconds=10))
        s = t.runtime_seconds()
        assert s is not None
        assert 9 <= s <= 12  # 允许少量时钟漂移

    def test_not_running_returns_none(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.PENDING,
                 started_at=datetime.now() - timedelta(seconds=10))
        assert t.runtime_seconds() is None

    def test_running_without_started_at(self) -> None:
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:1", platform="qq",
                 status=TaskStatus.RUNNING, started_at=None)
        assert t.runtime_seconds() is None


# ═══════════════════════════════════════════════════════════════════════════════
# 序列化（to_dict / from_dict）
# ═══════════════════════════════════════════════════════════════════════════════

class TestSerialization:
    @staticmethod
    def _make_task() -> TaskRecord:
        return TaskRecord(
            id="task-001",
            title="测试任务",
            intent="测试意图",
            level=TaskLevel.AGENT,
            owner="qq:10001",
            stream_id="qq:group:123",
            platform="qq",
            status=TaskStatus.PENDING,
            trigger_type=TriggerType.NOW,
            priority=5,
            metadata={"custom_key": "custom_val"},
        )

    def test_to_dict_keys(self) -> None:
        t = self._make_task()
        d = t.to_dict()
        expected_keys = {
            "id", "title", "intent", "level", "status", "owner", "stream_id",
            "platform", "reply_stream_id", "trigger_type", "delay_seconds", "cron_expr",
            "scheduled_at", "started_at", "priority", "created_at",
            "updated_at", "max_runtime_min", "metadata",
            "_status_log",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_enums_as_strings(self) -> None:
        t = self._make_task()
        d = t.to_dict()
        assert d["level"] == "agent"
        assert d["status"] == "pending"
        assert d["trigger_type"] == "now"

    def test_to_dict_json_serializable(self) -> None:
        t = self._make_task()
        d = t.to_dict()
        # 所有值都应可 JSON 序列化
        json_str = json.dumps(d, ensure_ascii=False)
        assert len(json_str) > 0

    def test_roundtrip(self) -> None:
        t = self._make_task()
        d = t.to_dict()
        t2 = TaskRecord.from_dict(d)
        assert t2.id == t.id
        assert t2.title == t.title
        assert t2.intent == t.intent
        assert t2.level == t.level
        assert t2.status == t.status
        assert t2.owner == t.owner
        assert t2.stream_id == t.stream_id
        assert t2.platform == t.platform
        assert t2.priority == t.priority
        assert t2.metadata == t.metadata

    def test_from_dict_missing_keys(self) -> None:
        d = {"id": "minimal"}
        t = TaskRecord.from_dict(d)
        assert t.id == "minimal"
        assert t.level == TaskLevel.INSTANT  # 默认值
        assert t.status == TaskStatus.PENDING  # 默认值

    def test_from_dict_with_none_datetimes(self) -> None:
        d = {
            "id": "t", "title": "", "intent": "", "level": "agent",
            "owner": "", "stream_id": "", "platform": "",
            "scheduled_at": None, "started_at": None,
        }
        t = TaskRecord.from_dict(d)
        assert t.scheduled_at is None
        assert t.started_at is None

    def test_from_dict_with_invalid_datetime(self) -> None:
        d = {
            "id": "t", "title": "", "intent": "", "level": "agent",
            "owner": "", "stream_id": "", "platform": "",
            "created_at": "not-a-date",
        }
        t = TaskRecord.from_dict(d)
        # 无效日期应回退到 datetime.now()
        assert isinstance(t.created_at, datetime)


# ═══════════════════════════════════════════════════════════════════════════════
# 状态审计日志 —— transition() 和 force() 必须留痕
# ═══════════════════════════════════════════════════════════════════════════════

class TestTransitionLogged:
    """transition() 每次合法转换都应在 _status_log 中追加一条 StatusChange。"""

    @staticmethod
    def _task(status: TaskStatus = TaskStatus.PENDING) -> TaskRecord:
        return TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                    owner="qq:1", stream_id="qq:1", platform="qq", status=status)

    def test_transition_logs_entry_with_actor(self) -> None:
        """给定一个 PENDING 任务，当以 actor="test" 调用 transition(RUNNING)，
        则 _status_log 含一条 actor 匹配的 StatusChange。"""
        t = self._task(TaskStatus.PENDING)
        assert len(t._status_log) == 0
        t.transition(TaskStatus.RUNNING, actor="test")
        assert len(t._status_log) == 1
        entry = t._status_log[0]
        assert entry.status == TaskStatus.RUNNING
        assert entry.actor == "test"
        assert entry.reason == ""
        assert isinstance(entry.timestamp, datetime)

    def test_transition_default_actor_is_system(self) -> None:
        """给定一个 PENDING 任务，当不带 actor 调用 transition(RUNNING)，
        则 _status_log 记录项的 actor 为 "system"。"""
        t = self._task(TaskStatus.PENDING)
        t.transition(TaskStatus.RUNNING)
        assert t._status_log[0].actor == "system"

    def test_transition_logs_on_each_call(self) -> None:
        """给定一个 SCHEDULED 任务，当连续两次调用 transition（SCHEDULED→PENDING→RUNNING），
        则 _status_log 有 2 条记录。"""
        t = self._task(TaskStatus.SCHEDULED)
        t.transition(TaskStatus.PENDING)
        t.transition(TaskStatus.RUNNING)
        assert len(t._status_log) == 2
        assert t._status_log[0].status == TaskStatus.PENDING
        assert t._status_log[1].status == TaskStatus.RUNNING


class TestForceLogged:
    """force() 跳过校验但必须留痕（含 actor 和 reason）。"""

    @staticmethod
    def _task(status: TaskStatus = TaskStatus.PENDING) -> TaskRecord:
        return TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                    owner="qq:1", stream_id="qq:1", platform="qq", status=status)

    def test_force_logs_with_actor_and_reason(self) -> None:
        """给定一个 PENDING 任务，当带 actor/reason 调用 force(COMPLETED)，
        则 _status_log 有 1 条 actor 与 reason 匹配的记录，
        且状态实际变为 COMPLETED。"""
        t = self._task(TaskStatus.PENDING)
        t.force(TaskStatus.COMPLETED, actor="test", reason="bypass test")
        assert t.status == TaskStatus.COMPLETED
        assert len(t._status_log) == 1
        entry = t._status_log[0]
        assert entry.status == TaskStatus.COMPLETED
        assert entry.actor == "test"
        assert entry.reason == "bypass test"
        assert isinstance(entry.timestamp, datetime)

    def test_force_bypasses_transition_validation(self) -> None:
        """给定一个 COMPLETED 任务（终态），当调用 force(FAILED)，
        则执行成功且不抛 TaskStatusError。"""
        t = self._task(TaskStatus.COMPLETED)
        t.force(TaskStatus.FAILED, actor="test", reason="reopen")
        assert t.status == TaskStatus.FAILED
        assert len(t._status_log) == 1

    def test_force_from_terminal_to_non_terminal(self) -> None:
        """给定一个 FAILED 任务，当调用 force(PENDING)，
        则执行成功（终态→非终态，状态机本禁止该转换）。"""
        t = self._task(TaskStatus.FAILED)
        t.force(TaskStatus.PENDING, actor="admin", reason="manual reopen")
        assert t.status == TaskStatus.PENDING
        assert len(t._status_log) == 1

    def test_force_multiple_calls_each_logged(self) -> None:
        """给定一个 PENDING 任务，当连续两次调用 force()，
        则 _status_log 有 2 条记录，最后一条反映最终状态。"""
        t = self._task(TaskStatus.PENDING)
        t.force(TaskStatus.RUNNING, actor="a", reason="r1")
        t.force(TaskStatus.COMPLETED, actor="b", reason="r2")
        assert len(t._status_log) == 2
        assert t._status_log[0].status == TaskStatus.RUNNING
        assert t._status_log[1].status == TaskStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# _restore —— 反序列化路径，不产生审计记录
# ═══════════════════════════════════════════════════════════════════════════════

class TestRestore:
    """_restore() 直接恢复状态和日志，不产生新的 StatusChange。"""

    @staticmethod
    def _task() -> TaskRecord:
        return TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                    owner="qq:1", stream_id="qq:1", platform="qq")

    def test_restore_sets_status_and_log(self) -> None:
        """给定一个无日志的 PENDING 任务，当调用 _restore(FAILED, [entry])，
        则状态为 FAILED 且 _status_log 与传入一致。"""
        from domain.task_record import StatusChange
        t = self._task()
        assert t.status == TaskStatus.PENDING
        entry = StatusChange(timestamp=datetime.now(), status=TaskStatus.FAILED,
                             actor="recovery", reason="restored")
        t._restore(TaskStatus.FAILED, [entry])
        assert t.status == TaskStatus.FAILED
        assert len(t._status_log) == 1
        assert t._status_log[0].actor == "recovery"

    def test_restore_does_not_create_new_entries(self) -> None:
        """给定一个 PENDING 任务，当以现有日志调用 _restore，
        则 _status_log 被原样替换（不产生额外记录）。"""
        from domain.task_record import StatusChange
        t = self._task()
        entries = [
            StatusChange(timestamp=datetime.now(), status=TaskStatus.SCHEDULED,
                         actor="cron", reason=""),
            StatusChange(timestamp=datetime.now(), status=TaskStatus.PENDING,
                         actor="scheduler", reason=""),
        ]
        t._restore(TaskStatus.PENDING, entries)
        assert len(t._status_log) == 2
        assert t.status == TaskStatus.PENDING


# ═══════════════════════════════════════════════════════════════════════════════
# reply_stream_id / reply_target —— 回复目标
# ═══════════════════════════════════════════════════════════════════════════════

class TestReplyTarget:
    """reply_target 派生属性：有 reply_stream_id 则用它，否则回退到 stream_id。"""

    def test_defaults_to_stream_id(self) -> None:
        """给定一个没有 reply_stream_id 的任务，
        当访问 reply_target 时，
        则返回 stream_id。"""
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:10001", platform="qq")
        assert t.reply_stream_id is None
        assert t.reply_target == "qq:10001"

    def test_returns_reply_stream_id_when_set(self) -> None:
        """给定一个设置了 reply_stream_id 的任务，
        当访问 reply_target 时，
        则返回 reply_stream_id 而非 stream_id。"""
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:10001", platform="qq",
                 reply_stream_id="qq:g:2")
        assert t.reply_stream_id == "qq:g:2"
        assert t.reply_target == "qq:g:2"

    def test_roundtrip_preserves_value(self) -> None:
        """给定一个 reply_stream_id="qq:g:2" 的任务，
        当经 to_dict() 序列化再由 from_dict() 反序列化，
        则 reply_stream_id 与 reply_target 保持不变。"""
        t = TaskRecord(id="t", title="T", intent="I", level=TaskLevel.AGENT,
                 owner="qq:1", stream_id="qq:10001", platform="qq",
                 reply_stream_id="qq:g:2")
        d = t.to_dict()
        assert d["reply_stream_id"] == "qq:g:2"

        t2 = TaskRecord.from_dict(d)
        assert t2.reply_stream_id == "qq:g:2"
        assert t2.reply_target == "qq:g:2"

    def test_from_dict_missing_key_backward_compat(self) -> None:
        """给定一个不含 reply_stream_id 的 dict（旧序列化数据），
        当经 from_dict() 反序列化，
        则 reply_stream_id 为 None 且 reply_target 回退到 stream_id。"""
        d = {
            "id": "t", "title": "T", "intent": "I", "level": "agent",
            "owner": "qq:1", "stream_id": "qq:10001", "platform": "qq",
        }
        t = TaskRecord.from_dict(d)
        assert t.reply_stream_id is None
        assert t.reply_target == "qq:10001"
