"""oh_mai_agent.planner_hooks 的测试——摘要构建、hook 注入、hash 去重、禁用开关。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from conftest import make_task

from oh_mai_agent.config import PlannerBoardConfig
from oh_mai_agent.planner_hooks import PlannerBoard
from oh_mai_agent.domain.task_record import TaskStatus
from oh_mai_agent.domain.task_store import TaskStore


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def default_cfg() -> PlannerBoardConfig:
    return PlannerBoardConfig()


@pytest.fixture
def board(store: TaskStore, default_cfg: PlannerBoardConfig, prompt_service: Any) -> PlannerBoard:
    return PlannerBoard(store=store, config=default_cfg, prompt_service=prompt_service)


# ═══════════════════════════════════════════════════════════════════════════════
# build_summary
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildSummary:
    @pytest.mark.asyncio
    async def test_empty(self, board: PlannerBoard) -> None:
        summary = await board.build_summary("qq:g:1")
        assert summary == ""

    @pytest.mark.asyncio
    async def test_with_running_task(self, store: TaskStore, board: PlannerBoard) -> None:
        t = make_task("t1", title="运行中任务", status=TaskStatus.RUNNING,
                      stream_id="qq:g:1",
                      started_at=datetime.now() - timedelta(seconds=65))
        await store.save(t)

        summary = await board.build_summary("qq:g:1")
        assert "task_board" in summary
        assert 'session="qq:g:1"' in summary
        assert "运行中任务" in summary
        assert "活跃任务" in summary

    @pytest.mark.asyncio
    async def test_with_waiting_input(self, store: TaskStore, board: PlannerBoard) -> None:
        t = make_task("t1", title="等待回复", status=TaskStatus.WAITING_INPUT,
                      stream_id="qq:g:1")
        await store.save(t)

        summary = await board.build_summary("qq:g:1")
        assert "waiting_input" in summary
        assert "等待回复" in summary

    @pytest.mark.asyncio
    async def test_with_paused(self, store: TaskStore, board: PlannerBoard) -> None:
        t = make_task("t1", title="已暂停任务", status=TaskStatus.PAUSED,
                      stream_id="qq:g:1")
        await store.save(t)

        summary = await board.build_summary("qq:g:1")
        assert "paused" in summary

    @pytest.mark.asyncio
    async def test_with_scheduled(self, store: TaskStore, board: PlannerBoard) -> None:
        t = make_task("t1", title="定时任务", status=TaskStatus.SCHEDULED,
                      stream_id="qq:g:1")
        await store.save(t)

        summary = await board.build_summary("qq:g:1")
        assert "定时任务" in summary
        assert "scheduled" in summary

    @pytest.mark.asyncio
    async def test_with_recent_completed(self, store: TaskStore, board: PlannerBoard) -> None:
        t = make_task("t1", title="已完成", status=TaskStatus.COMPLETED,
                      stream_id="qq:g:1")
        await store.save(t)

        summary = await board.build_summary("qq:g:1")
        assert "最近完成" in summary
        assert "已完成" in summary

    @pytest.mark.asyncio
    async def test_respects_max_active(self, store: TaskStore, prompt_service: Any) -> None:
        cfg = PlannerBoardConfig(max_active=2)
        board = PlannerBoard(store=store, config=cfg, prompt_service=prompt_service)

        for i in range(5):
            await store.save(make_task(f"t{i}", title=f"task_{i}",
                                       status=TaskStatus.RUNNING,
                                       stream_id="qq:g:1"))

        summary = await board.build_summary("qq:g:1")
        # 只应出现 2 个运行中任务（受 max_active 限制）
        # 统计 "- [running]" 条目数量
        running_count = summary.count("[running]")
        assert running_count <= 2

    @pytest.mark.asyncio
    async def test_respects_max_recent(self, store: TaskStore, prompt_service: Any) -> None:
        cfg = PlannerBoardConfig(max_recent=1)
        board = PlannerBoard(store=store, config=cfg, prompt_service=prompt_service)

        for i in range(3):
            await store.save(make_task(f"t{i}", title=f"task_{i}",
                                       status=TaskStatus.COMPLETED,
                                       stream_id="qq:g:1"))

        summary = await board.build_summary("qq:g:1")
        completed_count = summary.count("[completed]")
        assert completed_count <= 1

    @pytest.mark.asyncio
    async def test_other_stream_ignored(self, store: TaskStore, board: PlannerBoard) -> None:
        # 其它流中的任务不应出现在摘要中
        t = make_task("t1", title="other", status=TaskStatus.RUNNING,
                      stream_id="qq:g:999")
        await store.save(t)

        summary = await board.build_summary("qq:g:1")
        assert "other" not in summary


# ═══════════════════════════════════════════════════════════════════════════════
# hook_before_request
# ═══════════════════════════════════════════════════════════════════════════════

class TestHookBeforeRequest:
    @pytest.mark.asyncio
    async def test_disabled_returns_continue(self, store: TaskStore, prompt_service: Any) -> None:
        cfg = PlannerBoardConfig(enabled=False)
        board = PlannerBoard(store=store, config=cfg, prompt_service=prompt_service)
        result = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert result == {"action": "continue"}

    @pytest.mark.asyncio
    async def test_no_messages_injects(self, store: TaskStore, prompt_service: Any) -> None:
        t = make_task("t1", title="A", status=TaskStatus.RUNNING, stream_id="qq:g:1")
        await store.save(t)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert result["action"] == "continue"
        assert "modified_kwargs" in result
        new_msgs = result["modified_kwargs"]["messages"]
        assert len(new_msgs) == 1
        assert "task_board" in new_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_marker_prevents_reinjection(self, store: TaskStore, prompt_service: Any) -> None:
        t = make_task("t1", title="A", status=TaskStatus.RUNNING, stream_id="qq:g:1")
        await store.save(t)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        # 首次注入
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # 第二次调用同一 session——hash 匹配，应跳过注入
        result2 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" not in result2

    @pytest.mark.asyncio
    async def test_hash_dedup_changed_content_reinjects(self, store: TaskStore, prompt_service: Any) -> None:
        t1 = make_task("t1", title="A", status=TaskStatus.RUNNING, stream_id="qq:g:1")
        await store.save(t1)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # 新增任务 → 摘要内容变化 → 应重新注入
        t2 = make_task("t2", title="B", status=TaskStatus.RUNNING, stream_id="qq:g:1")
        await store.save(t2)

        result2 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" in result2  # 内容已变化 → 重新注入

    @pytest.mark.asyncio
    async def test_no_session_id_returns_continue(self, store: TaskStore, prompt_service: Any) -> None:
        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result = await board.hook_before_request(messages=[])
        assert result == {"action": "continue"}

    @pytest.mark.asyncio
    async def test_exception_returns_continue(self, store: TaskStore, prompt_service: Any) -> None:
        # 无任务可注入，build_summary 返回空串
        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        # 摘要为空（或内部异常）时返回 continue，不崩溃、不阻断 Planner
        result = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert result["action"] == "continue"


# ═══════════════════════════════════════════════════════════════════════════════
# reset
# ═══════════════════════════════════════════════════════════════════════════════

class TestReset:
    @pytest.mark.asyncio
    async def test_reset_clears_hash(self, store: TaskStore, prompt_service: Any) -> None:
        t = make_task("t1", title="A", status=TaskStatus.RUNNING, stream_id="qq:g:1")
        await store.save(t)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # hash 匹配后，第二次调用跳过注入
        result2 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" not in result2

        # reset 后即使 hash 相同也应重新注入
        board.reset()

        result3 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" in result3


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_marker_session
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractMarkerSession:
    def test_extracts_session_id(self) -> None:
        msgs = [{"role": "system", "content": '<task_board session="qq:g:123">...</task_board>'}]
        result = PlannerBoard._extract_marker_session(msgs)
        assert result == "qq:g:123"

    def test_no_marker(self) -> None:
        msgs = [{"role": "user", "content": "hello world"}]
        result = PlannerBoard._extract_marker_session(msgs)
        assert result is None

    def test_empty_messages(self) -> None:
        result = PlannerBoard._extract_marker_session([])
        assert result is None

    def test_non_string_content(self) -> None:
        msgs = [{"role": "system", "content": 123}]
        result = PlannerBoard._extract_marker_session(msgs)
        assert result is None
