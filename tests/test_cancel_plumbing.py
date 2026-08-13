"""取消传导管线测试：current_cancel_check ContextVar 的 set/reset 生命周期。

覆盖 AgentExecutor.execute() 在 AgentLoop 构造后写入取消检查回调
（``lambda: loop.is_cancelled``）、finally 中恢复的行为：

  1. happy — 执行期间（假 run 注入 _cancelled=True）回调为 True，结束后 ContextVar 为 None
  2. 未执行时 get() 返回 None（默认值）
  3. AgentLoop 构造失败路径（cc_token 未赋值）不触发 reset、不抛异常
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import MockCtx, make_task
from oh_mai_agent.config import MaibotAgentConfig
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord
from oh_mai_agent.executor.agent import AgentExecutor
from oh_mai_agent.executor.agent_loop import AgentLoop
from oh_mai_agent.executor.base import ExecutionContext
from oh_mai_agent.executor.context import current_cancel_check
from oh_mai_agent.tools.registry import ToolRegistry


def _exec_ctx(mock_ctx: MockCtx, store: Any, prompt_service: Any) -> ExecutionContext:
    return ExecutionContext(
        ctx=mock_ctx,
        store=store,
        scheduler=None,
        config=MaibotAgentConfig(),
        prompt_service=prompt_service,
    )


def _make_executor(mock_ctx: MockCtx, store: Any, prompt_service: Any) -> AgentExecutor:
    return AgentExecutor(
        registry=ToolRegistry(),
        prompt_service=prompt_service,
    )


def test_current_cancel_check_defaults_to_none_before_execute() -> None:
    """未执行任何任务时，取消检查 ContextVar 返回 None（默认值）。"""
    assert current_cancel_check.get() is None


@pytest.mark.asyncio
async def test_execute_exposes_loop_cancel_state_and_resets_after(
    real_store: Any, mock_ctx: MockCtx, prompt_service: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute() 执行期间可读到主循环取消状态，结束后 ContextVar 恢复为 None。"""
    await real_store.init()
    task = make_task("cancel-plumbing", level=TaskLevel.AGENT)

    async def fake_run(self: AgentLoop, _task: TaskRecord) -> None:
        check = current_cancel_check.get()
        assert check is not None, "执行期间必须已设置取消检查回调"
        assert check() is False, "初始未取消"
        self._cancelled = True
        assert check() is True, "注入取消后回调必须反映最新状态"

    monkeypatch.setattr(AgentLoop, "run", fake_run)

    executor = _make_executor(mock_ctx, real_store, prompt_service)
    result = await executor.execute(_exec_ctx(mock_ctx, real_store, prompt_service), task)

    assert result.status == "COMPLETED"
    assert current_cancel_check.get() is None, "execute() 结束后必须恢复 ContextVar"


@pytest.mark.asyncio
async def test_loop_construction_failure_resets_safely(
    real_store: Any, mock_ctx: MockCtx, prompt_service: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentLoop 构造抛异常时（cc_token 未赋值）不触发 reset、不抛异常。"""
    await real_store.init()
    task = make_task("cancel-plumbing-fail", level=TaskLevel.AGENT)

    def failing_init(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(AgentLoop, "__init__", failing_init)

    executor = _make_executor(mock_ctx, real_store, prompt_service)
    result = await executor.execute(_exec_ctx(mock_ctx, real_store, prompt_service), task)

    assert result.status == "FAILED"
    assert "boom" in (result.error or "")
    assert current_cancel_check.get() is None
