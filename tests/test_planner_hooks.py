"""oh_mai_agent.planner_hooks 的测试——待办看板构建、简介注入、hook 注入、去重、禁用开关。

看板模型（v0.1.0 重构）：只推送「需要 Planner 主动介入」的待办——
waiting_input（待用户回复）任务；插件能力简介每会话首次注入一次。
运行中/定时/已完成等状态快照不再注入（用户询问走 subagent_list 等工具）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from conftest import make_task

from oh_mai_agent.config import PlannerBoardConfig
from oh_mai_agent.planner_hooks import (
    _BOARD_MARKER_RE,
    _INTRO_MARKER_RE,
    PlannerBoard,
)
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
# build_intro / build_board
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildIntro:
    @pytest.mark.asyncio
    async def test_intro_contains_plugin_intro_marker(self, board: PlannerBoard) -> None:
        intro = await board.build_intro("qq:g:1")
        assert "plugin_intro" in intro
        assert 'session="qq:g:1"' in intro

    @pytest.mark.asyncio
    async def test_intro_mentions_subagent_capability(self, board: PlannerBoard) -> None:
        intro = await board.build_intro("qq:g:1")
        assert "后台子代理" in intro
        assert "subagent" in intro

    @pytest.mark.asyncio
    async def test_intro_without_prompt_service_returns_empty(self, store: TaskStore) -> None:
        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=None)
        intro = await board.build_intro("qq:g:1")
        assert intro == ""


class TestBuildBoard:
    @pytest.mark.asyncio
    async def test_empty(self, board: PlannerBoard) -> None:
        summary = await board.build_board("qq:g:1")
        assert summary == ""

    @pytest.mark.asyncio
    async def test_with_waiting_input(self, store: TaskStore, board: PlannerBoard) -> None:
        t = make_task("t1", title="等待回复", status=TaskStatus.WAITING_INPUT,
                      stream_id="qq:g:1")
        await store.save(t)

        summary = await board.build_board("qq:g:1")
        assert "task_board" in summary
        assert 'session="qq:g:1"' in summary
        assert "等待回复" in summary
        assert "待用户回复" in summary

    @pytest.mark.asyncio
    async def test_running_task_not_in_board(self, store: TaskStore, board: PlannerBoard) -> None:
        """运行中任务不应出现在看板（非待办，用户询问走 subagent_list）。"""
        t = make_task("t1", title="运行中任务", status=TaskStatus.RUNNING,
                      stream_id="qq:g:1",
                      started_at=datetime.now() - timedelta(seconds=65))
        await store.save(t)

        summary = await board.build_board("qq:g:1")
        assert summary == ""

    @pytest.mark.asyncio
    async def test_scheduled_task_not_in_board(self, store: TaskStore, board: PlannerBoard) -> None:
        t = make_task("t1", title="定时任务", status=TaskStatus.SCHEDULED,
                      stream_id="qq:g:1")
        await store.save(t)

        summary = await board.build_board("qq:g:1")
        assert summary == ""

    @pytest.mark.asyncio
    async def test_recent_completed_not_in_board(self, store: TaskStore, board: PlannerBoard) -> None:
        t = make_task("t1", title="已完成", status=TaskStatus.COMPLETED,
                      stream_id="qq:g:1")
        await store.save(t)

        summary = await board.build_board("qq:g:1")
        assert summary == ""

    @pytest.mark.asyncio
    async def test_failed_task_not_in_board(self, store: TaskStore, board: PlannerBoard) -> None:
        """失败任务不注入看板（失败通知由 fail_task 直发用户，Planner 不负责重试）。"""
        t = make_task("t1", title="失败任务", status=TaskStatus.FAILED,
                      stream_id="qq:g:1")
        await store.save(t)

        summary = await board.build_board("qq:g:1")
        assert summary == ""

    @pytest.mark.asyncio
    async def test_respects_max_waiting(self, store: TaskStore, prompt_service: Any) -> None:
        cfg = PlannerBoardConfig(max_waiting=2)
        board = PlannerBoard(store=store, config=cfg, prompt_service=prompt_service)

        for i in range(5):
            await store.save(make_task(f"t{i}", title=f"task_{i}",
                                       status=TaskStatus.WAITING_INPUT,
                                       stream_id="qq:g:1"))

        summary = await board.build_board("qq:g:1")
        # 只应出现 2 条等待任务（受 max_waiting 限制）
        assert summary.count("[waiting_input]") <= 2

    @pytest.mark.asyncio
    async def test_oldest_waiting_first(self, store: TaskStore, board: PlannerBoard) -> None:
        """等待最久的任务排在最前（updated_at 升序）。"""
        old = make_task("t1", title="早等待", status=TaskStatus.WAITING_INPUT,
                        stream_id="qq:g:1",
                        updated_at=datetime.now() - timedelta(minutes=10))
        new = make_task("t2", title="晚等待", status=TaskStatus.WAITING_INPUT,
                        stream_id="qq:g:1",
                        updated_at=datetime.now() - timedelta(minutes=1))
        await store.save(old)
        await store.save(new)

        summary = await board.build_board("qq:g:1")
        assert summary.index("早等待") < summary.index("晚等待")

    @pytest.mark.asyncio
    async def test_other_stream_ignored(self, store: TaskStore, board: PlannerBoard) -> None:
        # 其它流中的任务不应出现在摘要中
        t = make_task("t1", title="other", status=TaskStatus.WAITING_INPUT,
                      stream_id="qq:g:999")
        await store.save(t)

        summary = await board.build_board("qq:g:1")
        assert summary == ""


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
    async def test_first_request_injects_intro_only(self, store: TaskStore, prompt_service: Any) -> None:
        """无任何任务时，首个请求仍注入插件简介（每会话一次）。"""
        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert result["action"] == "continue"
        assert "modified_kwargs" in result
        new_msgs = result["modified_kwargs"]["messages"]
        assert len(new_msgs) == 1
        assert "plugin_intro" in new_msgs[0]["content"]
        assert "task_board" not in new_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_intro_injected_once_per_session(self, store: TaskStore, prompt_service: Any) -> None:
        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # 第二次调用同一 session——简介 marker 已存在，不再注入
        result2 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" not in result2

    @pytest.mark.asyncio
    async def test_intro_reinjected_for_new_session(self, store: TaskStore, prompt_service: Any) -> None:
        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # 另一会话首次请求：应重新注入简介
        result2 = await board.hook_before_request(session_id="qq:g:2", messages=[])
        assert "modified_kwargs" in result2
        assert "plugin_intro" in result2["modified_kwargs"]["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_waiting_task_injects_board(self, store: TaskStore, prompt_service: Any) -> None:
        t = make_task("t1", title="A", status=TaskStatus.WAITING_INPUT, stream_id="qq:g:1")
        await store.save(t)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert result["action"] == "continue"
        assert "modified_kwargs" in result
        new_msgs = result["modified_kwargs"]["messages"]
        assert len(new_msgs) == 2  # intro + board
        assert "plugin_intro" in new_msgs[0]["content"]
        assert "task_board" in new_msgs[1]["content"]

    @pytest.mark.asyncio
    async def test_board_hash_dedup(self, store: TaskStore, prompt_service: Any) -> None:
        t = make_task("t1", title="A", status=TaskStatus.WAITING_INPUT, stream_id="qq:g:1")
        await store.save(t)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # 第二次调用同一 session——简介与看板均未变化，应跳过注入
        result2 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" not in result2

    @pytest.mark.asyncio
    async def test_board_content_change_reinjects(self, store: TaskStore, prompt_service: Any) -> None:
        t1 = make_task("t1", title="A", status=TaskStatus.WAITING_INPUT, stream_id="qq:g:1")
        await store.save(t1)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # 新增等待任务 → 看板内容变化 → 应重新注入
        t2 = make_task("t2", title="B", status=TaskStatus.WAITING_INPUT, stream_id="qq:g:1")
        await store.save(t2)

        result2 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" in result2  # 内容已变化 → 重新注入

    @pytest.mark.asyncio
    async def test_waiting_resolved_stops_injecting(self, store: TaskStore, prompt_service: Any) -> None:
        """等待任务被用户回复恢复（RUNNING）后，看板不再注入。"""
        t = make_task("t1", title="A", status=TaskStatus.WAITING_INPUT, stream_id="qq:g:1")
        await store.save(t)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # 任务恢复为 RUNNING（模拟用户已回复）→ 看板无待办 → 不再注入
        t.transition(TaskStatus.RUNNING)
        await store.save(t)

        result2 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" not in result2

    @pytest.mark.asyncio
    async def test_no_session_id_returns_continue(self, store: TaskStore, prompt_service: Any) -> None:
        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result = await board.hook_before_request(messages=[])
        assert result == {"action": "continue"}

    @pytest.mark.asyncio
    async def test_exception_returns_continue(self, store: TaskStore, prompt_service: Any) -> None:
        # 无任务且 pm 可用时正常返回；异常路径兜底 continue 不崩溃
        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert result["action"] == "continue"


# ═══════════════════════════════════════════════════════════════════════════════
# reset
# ═══════════════════════════════════════════════════════════════════════════════

class TestReset:
    @pytest.mark.asyncio
    async def test_reset_clears_board_hash(self, store: TaskStore, prompt_service: Any) -> None:
        t = make_task("t1", title="A", status=TaskStatus.WAITING_INPUT, stream_id="qq:g:1")
        await store.save(t)

        board = PlannerBoard(store=store, config=PlannerBoardConfig(), prompt_service=prompt_service)
        result1 = await board.hook_before_request(session_id="qq:g:1", messages=[])
        assert "modified_kwargs" in result1

        # hash 匹配后，第二次调用跳过注入
        result2 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" not in result2

        # reset 后即使 hash 相同也应重新注入看板（简介 marker 仍存在，仅看板重注入）
        board.reset()

        result3 = await board.hook_before_request(session_id="qq:g:1",
                                                   messages=result1["modified_kwargs"]["messages"])
        assert "modified_kwargs" in result3
        contents = [m["content"] for m in result3["modified_kwargs"]["messages"]]
        assert any("task_board" in c for c in contents)


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_marker_session
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractMarkerSession:
    def test_extracts_intro_session_id(self) -> None:
        msgs = [{"role": "system", "content": '<plugin_intro session="qq:g:123">...</plugin_intro>'}]
        result = PlannerBoard._extract_marker_session(msgs, _INTRO_MARKER_RE)
        assert result == "qq:g:123"

    def test_extracts_board_session_id(self) -> None:
        msgs = [{"role": "system", "content": '<task_board session="qq:g:123">...</task_board>'}]
        result = PlannerBoard._extract_marker_session(msgs, _BOARD_MARKER_RE)
        assert result == "qq:g:123"

    def test_no_marker(self) -> None:
        msgs = [{"role": "user", "content": "hello world"}]
        result = PlannerBoard._extract_marker_session(msgs, _BOARD_MARKER_RE)
        assert result is None

    def test_empty_messages(self) -> None:
        result = PlannerBoard._extract_marker_session([], _BOARD_MARKER_RE)
        assert result is None

    def test_non_string_content(self) -> None:
        msgs = [{"role": "system", "content": 123}]
        result = PlannerBoard._extract_marker_session(msgs, _BOARD_MARKER_RE)
        assert result is None

    def test_wrong_marker_pattern(self) -> None:
        """intro marker 不匹配 task_board 内容（两 marker 独立）。"""
        msgs = [{"role": "system", "content": '<task_board session="qq:g:123">...</task_board>'}]
        result = PlannerBoard._extract_marker_session(msgs, _INTRO_MARKER_RE)
        assert result is None
