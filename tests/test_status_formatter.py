"""domain/status_formatter.py — 完整分支测试。

此前仅 RUNNING / WAITING_INPUT 的时间分支与静态兜底表被间接覆盖；
本文件补齐 SCHEDULED 相对时间（秒/分/小时/明天/天数）、
format_relative 与 _format_duration 的全部分支。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from oh_mai_agent.domain.status_formatter import StatusFormatter
from oh_mai_agent.domain.task_record import TaskStatus

# 固定参考时间：2025-01-01 12:00:00（周三）
FIXED = datetime(2025, 1, 1, 12, 0, 0)


def _fmt() -> StatusFormatter:
    return StatusFormatter(now=FIXED)


def _ts(delta: timedelta) -> datetime:
    return FIXED + delta


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNING / WAITING_INPUT
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunningAndWaiting:
    def test_running_with_timestamp(self) -> None:
        assert _fmt().format(TaskStatus.RUNNING, _ts(timedelta(minutes=-3))) == "已运行 3 分钟"

    def test_running_without_timestamp(self) -> None:
        assert _fmt().format(TaskStatus.RUNNING) == "运行中"

    def test_running_future_timestamp_clamped(self) -> None:
        """时间戳超前时钳制为 0，避免负时长。"""
        assert _fmt().format(TaskStatus.RUNNING, _ts(timedelta(minutes=5))) == "已运行 0 秒"

    def test_waiting_input_with_timestamp(self) -> None:
        assert _fmt().format(TaskStatus.WAITING_INPUT, _ts(timedelta(minutes=-1))) == "已等待 1 分钟"

    def test_waiting_input_without_timestamp(self) -> None:
        assert _fmt().format(TaskStatus.WAITING_INPUT) == "等待回复"


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULED 相对时间
# ═══════════════════════════════════════════════════════════════════════════════

class TestScheduledRelativeTime:
    def test_due_or_past_shows_imminent(self) -> None:
        assert _fmt().format(TaskStatus.SCHEDULED, _ts(timedelta(seconds=-1))) == "即将开始"
        assert _fmt().format(TaskStatus.SCHEDULED, _ts(timedelta(seconds=0))) == "即将开始"

    def test_seconds_until_start(self) -> None:
        assert _fmt().format(TaskStatus.SCHEDULED, _ts(timedelta(seconds=30))) == "30 秒后开始"

    def test_minutes_until_start(self) -> None:
        assert _fmt().format(TaskStatus.SCHEDULED, _ts(timedelta(minutes=5))) == "5 分钟后开始"

    def test_hours_until_start(self) -> None:
        assert _fmt().format(TaskStatus.SCHEDULED, _ts(timedelta(hours=3))) == "3 小时后开始"

    def test_tomorrow_shows_clock_time(self) -> None:
        """目标落在明天自然日（≥24h 且未超过再日零点）→ 显示「明天 HH:MM」。"""
        ts = datetime(2025, 1, 2, 13, 0, 0)  # 距 now 25 小时
        assert _fmt().format(TaskStatus.SCHEDULED, ts) == "明天 13:00 开始"

    def test_beyond_tomorrow_shows_days(self) -> None:
        """超过明天自然日 → 按整天数显示。"""
        ts = datetime(2025, 1, 3, 13, 0, 0)  # 距 now 2 天 1 小时
        assert _fmt().format(TaskStatus.SCHEDULED, ts) == "2 天后开始"

    def test_without_timestamp(self) -> None:
        assert _fmt().format(TaskStatus.SCHEDULED) == "等待触发"

    def test_exactly_24h_ahead_is_tomorrow(self) -> None:
        """恰好 24 小时 → 落入明天分支显示「明天 HH:MM 开始」。"""
        assert _fmt().format(TaskStatus.SCHEDULED, _ts(timedelta(days=1))) == "明天 12:00 开始"


# ═══════════════════════════════════════════════════════════════════════════════
# 静态兜底表 / 未知状态 / format_relative / _format_duration
# ═══════════════════════════════════════════════════════════════════════════════

class TestStaticAndRelative:
    @pytest.mark.parametrize("status,expected", [
        (TaskStatus.PENDING, "排队中"),
        (TaskStatus.PAUSED, "已暂停"),
        (TaskStatus.COMPLETED, "已完成"),
        (TaskStatus.FAILED, "失败"),
        (TaskStatus.CANCELLED, "已取消"),
    ])
    def test_static_map(self, status: TaskStatus, expected: str) -> None:
        assert _fmt().format(status) == expected

    @pytest.mark.parametrize("seconds,expected", [
        (0, "刚刚"),
        (9, "刚刚"),
        (30, "30 秒前"),
        (300, "5 分钟前"),
        (7200, "2 小时前"),
        (200000, "2 天前"),
    ])
    def test_format_relative(self, seconds: float, expected: str) -> None:
        assert _fmt().format_relative(seconds) == expected

    @pytest.mark.parametrize("seconds,expected", [
        (5, "5 秒"),
        (300, "5 分钟"),
        (7200, "2 小时"),
        (100000, "27 小时"),  # 超过 24 小时仍以小时计
    ])
    def test_format_duration(self, seconds: float, expected: str) -> None:
        assert _fmt()._format_duration(seconds) == expected
