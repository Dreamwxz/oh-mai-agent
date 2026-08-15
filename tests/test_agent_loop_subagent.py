"""AgentLoop 合成工具分支测试 — ask_subagent / ask_subagents 的引擎内建执行。

「子 Agent 也是 Agent」：派发执行在 AgentLoop 合成分支（``_run_subagent`` /
``_run_subagents``），工具层（``tools/agent/subagent_tool.py``）只提供 schema
与工具集规则。本文件直接白盒调用分支入口验证：

- 单派：参数校验、工具集覆盖（schema 透传）、结果回传、角色过滤、配置读取
- 批量：intents 校验（空 / 超限）、并行合并格式（answers / total_rounds / 聚合 error）
- 取消传导：主循环取消直读 is_cancelled，子循环轮首退出

端到端（真实 AgentLoop.run 全链路）与批量取消由
``tests/test_subagent_parallel_sandbox.py`` / ``tests/test_subagent_integration.py`` 覆盖。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from conftest import MockCtx, make_task
from oh_mai_agent.bus.command_bus import TaskCommandBus
from oh_mai_agent.config import SubAgentConfig
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.executor.agent_loop import AgentLoop
from oh_mai_agent.permission import Role
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry


async def _ok_handler(**kwargs: Any) -> dict:
    return {"success": True}


def _make_tool(name: str, handler: Any = _ok_handler, min_role: Role = Role.USER) -> ToolDefinition:
    return ToolDefinition(
        name=name, description=name,
        parameters={"type": "object", "properties": {}},
        handler=handler, visibility="discoverable", min_role=min_role,
    )


def _make_loop(
    mock_ctx: MockCtx,
    reg: ToolRegistry,
    store: TaskStore,
    bus: TaskCommandBus,
    prompt_service: Any,
    *,
    role_provider: Any = None,
    subagent_config_getter: Any = None,
) -> AgentLoop:
    return AgentLoop(
        ctx=mock_ctx,
        registry=reg,
        store=store,
        command_bus=bus,
        role_provider=role_provider or (lambda: Role.USER),
        prompt_service=prompt_service,
        subagent_config_getter=subagent_config_getter,
    )


def _schema_names(ctx: MockCtx, call_index: int = -1) -> set[str]:
    """从 mock LLM call_history 提取第 *call_index* 次 generate_with_tools 的 schema 名。"""
    calls = [c for c in ctx.llm.call_history if c["type"] == "generate_with_tools"]
    return {t["function"]["name"] for t in calls[call_index]["tools"]}


def _make_task(tid: str) -> TaskRecord:
    return make_task(tid, level=TaskLevel.AGENT)


# ── 单派：参数校验 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_rejects_missing_intent(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """intent 缺失 → 明确错误。"""
    loop = _make_loop(mock_ctx, ToolRegistry(), real_store, command_bus, prompt_service)
    result = await loop._run_subagent(_make_task("s1"), {})
    assert result["success"] is False
    assert "intent" in result["error"]


@pytest.mark.asyncio
async def test_single_rejects_illegal_toolset(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """tools 含非法名 → 整体拒绝（不触发任何 LLM 调用）。"""
    reg = ToolRegistry()
    reg.register(_make_tool("read"))
    loop = _make_loop(mock_ctx, reg, real_store, command_bus, prompt_service)
    result = await loop._run_subagent(_make_task("s2"), {"intent": "x", "tools": ["nope"]})
    assert result["success"] is False
    assert "invalid tools" in result["error"]
    assert mock_ctx.llm.call_history == []


# ── 单派：schema 覆盖与结果回传 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_toolset_override_passed_to_subagent(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """tools=["read"] → 子循环 schema 仅含 read（覆盖默认集）。"""
    reg = ToolRegistry()
    reg.register(_make_tool("read"))
    reg.register(_make_tool("write"))
    loop = _make_loop(mock_ctx, reg, real_store, command_bus, prompt_service)

    mock_ctx.llm.set_tool_response("文件内容：hello")
    result = await loop._run_subagent(_make_task("s3"), {"intent": "读取文件", "tools": ["read"]})

    assert result["success"] is True
    assert result["answer"] == "文件内容：hello"
    assert result["rounds"] == 1
    # 唯一一次 LLM 调用（子循环）的 tools 仅含 read
    assert len(mock_ctx.llm.call_history) == 1
    assert _schema_names(mock_ctx) == {"read"}


@pytest.mark.asyncio
async def test_single_default_toolset_role_filtered(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """默认工具集按角色过滤：USER 下 admin 专用工具不进子循环 schema。"""
    reg = ToolRegistry()
    reg.register(_make_tool("read"))
    reg.register(_make_tool("admin_secret", min_role=Role.ADMIN))
    loop = _make_loop(mock_ctx, reg, real_store, command_bus, prompt_service)

    mock_ctx.llm.set_tool_response("ok")
    result = await loop._run_subagent(_make_task("s4"), {"intent": "x"})

    assert result["success"] is True
    assert _schema_names(mock_ctx) == {"read"}


@pytest.mark.asyncio
async def test_single_subagent_tools_run_concurrently_in_order(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """子循环单轮 3 个 tool_calls：互锁工具强制重叠、乱序完成，
    结果 tool 消息仍按 tool_calls 原始顺序追加且 tool_call_id 对应正确。"""
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()
    executed: list[str] = []
    intervals: dict[str, tuple[float, float]] = {}

    async def slow_a(**kwargs: Any) -> dict:
        executed.append("t_a")
        start = time.monotonic()
        gate_a.set()
        await gate_b.wait()  # 等 t_b 完成信号 → t_a 必然晚于 t_b 结束
        intervals["a"] = (start, time.monotonic())
        return {"success": True, "name": "a"}

    async def slow_b(**kwargs: Any) -> dict:
        executed.append("t_b")
        await gate_a.wait()  # 确保 t_a 已进入等待
        start = time.monotonic()
        gate_b.set()
        intervals["b"] = (start, time.monotonic())
        return {"success": True, "name": "b"}

    async def fast_c(**kwargs: Any) -> dict:
        executed.append("t_c")
        return {"success": True, "name": "c"}

    reg = ToolRegistry()
    reg.register(_make_tool("t_a", slow_a))
    reg.register(_make_tool("t_b", slow_b))
    reg.register(_make_tool("t_c", fast_c))
    loop = _make_loop(mock_ctx, reg, real_store, command_bus, prompt_service)

    mock_ctx.llm.set_tool_response("调用工具", [
        {"id": "call-1", "function": {"name": "t_a", "arguments": "{}"}},
        {"id": "call-2", "function": {"name": "t_b", "arguments": "{}"}},
        {"id": "call-3", "function": {"name": "t_c", "arguments": "{}"}},
    ])
    mock_ctx.llm.set_tool_response("全部完成", [])

    result = await asyncio.wait_for(
        loop._run_subagent(_make_task("s5"), {"intent": "并行调用三个工具"}),
        timeout=5,
    )
    assert result["success"] is True
    assert result["answer"] == "全部完成"
    assert result["rounds"] == 2

    # 两个互锁 handler 执行区间重叠（真并发）
    assert "a" in intervals and "b" in intervals
    a_start, a_end = intervals["a"]
    b_start, b_end = intervals["b"]
    assert b_start < a_end and a_start < b_end
    # 乱序完成（t_a 最后返回），但全部执行
    assert sorted(executed) == ["t_a", "t_b", "t_c"]

    # 第 2 轮 LLM 收到的 tool 消息按 tool_calls 原始顺序追加，id 对应正确
    calls = [c for c in mock_ctx.llm.call_history if c["type"] == "generate_with_tools"]
    tool_msgs = [m for m in calls[1]["prompt"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call-1", "call-2", "call-3"]
    assert json.loads(tool_msgs[0]["content"]) == {"success": True, "name": "a"}
    assert json.loads(tool_msgs[1]["content"]) == {"success": True, "name": "b"}
    assert json.loads(tool_msgs[2]["content"]) == {"success": True, "name": "c"}


# ── 单派：配置读取（热更新）与取消传导 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_single_reads_injected_config_hot(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """subagent_config_getter 每次派发时调用：max_rounds=1 且首轮全工具调用
    → 子循环 max_rounds 耗尽（max_rounds_reached=True，success 仍为 True）。"""
    holder: dict[str, SubAgentConfig] = {"cfg": SubAgentConfig(max_rounds=1)}
    reg = ToolRegistry()
    reg.register(_make_tool("read"))
    loop = _make_loop(
        mock_ctx, reg, real_store, command_bus, prompt_service,
        subagent_config_getter=lambda: holder["cfg"],
    )

    mock_ctx.llm.set_tool_response("调用工具", [
        {"id": "c1", "function": {"name": "read", "arguments": "{}"}},
    ])
    result = await loop._run_subagent(_make_task("s6"), {"intent": "x"})
    assert result["success"] is True
    assert result["max_rounds_reached"] is True

    # 热更新：getter 换新值后立即生效（max_rounds=10 → 正常跑 2 轮出答案）
    holder["cfg"] = SubAgentConfig(max_rounds=10)
    mock_ctx.llm.set_tool_response("调用工具", [
        {"id": "c1", "function": {"name": "read", "arguments": "{}"}},
    ])
    mock_ctx.llm.set_tool_response("答案", [])
    result = await loop._run_subagent(_make_task("s6b"), {"intent": "x"})
    assert result["success"] is True
    assert result["answer"] == "答案"
    assert result["max_rounds_reached"] is False


@pytest.mark.asyncio
async def test_single_cancel_propagates_from_main_loop(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """主循环取消（is_cancelled=True）→ 子循环轮首退出，返回 cancelled。"""
    reg = ToolRegistry()
    reg.register(_make_tool("read"))
    loop = _make_loop(mock_ctx, reg, real_store, command_bus, prompt_service)
    loop._cancelled = True  # 模拟主循环已被 CANCEL

    result = await loop._run_subagent(_make_task("s7"), {"intent": "x"})
    assert result["success"] is False
    assert result["error"] == "cancelled"
    assert result["rounds"] == 0
    # 取消命中在轮首，不触发任何 LLM 调用
    assert mock_ctx.llm.call_history == []


# ── 批量：校验 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_rejects_empty_intents(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """intents 为空 → 明确错误。"""
    loop = _make_loop(mock_ctx, ToolRegistry(), real_store, command_bus, prompt_service)
    result = await loop._run_subagents(_make_task("b1"), {})
    assert result["success"] is False
    assert "intents" in result["error"]


@pytest.mark.asyncio
async def test_batch_rejects_over_limit(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """intents 数量超 max_parallel_subagents（默认 3）→ 整体拒绝，不触发 LLM。"""
    loop = _make_loop(mock_ctx, ToolRegistry(), real_store, command_bus, prompt_service)
    result = await loop._run_subagents(_make_task("b2"), {"intents": ["1", "2", "3", "4"]})
    assert result["success"] is False
    assert "数量超限" in result["error"]
    assert mock_ctx.llm.call_history == []


@pytest.mark.asyncio
async def test_batch_rejects_illegal_toolset(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """批量 tools 含非法名 → 整体拒绝。"""
    reg = ToolRegistry()
    reg.register(_make_tool("read"))
    loop = _make_loop(mock_ctx, reg, real_store, command_bus, prompt_service)
    result = await loop._run_subagents(_make_task("b3"), {"intents": ["x"], "tools": ["nope"]})
    assert result["success"] is False
    assert "invalid tools" in result["error"]


# ── 批量：并行与合并 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_merges_answers_in_intent_order(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """2 个意图并行派发：answers 按 intents 顺序、total_rounds 求和、success 取与。"""
    reg = ToolRegistry()
    reg.register(_make_tool("read"))
    loop = _make_loop(mock_ctx, reg, real_store, command_bus, prompt_service)

    mock_ctx.llm.set_tool_response("A答案")
    mock_ctx.llm.set_tool_response("B答案")
    result = await loop._run_subagents(_make_task("b4"), {"intents": ["查A", "查B"]})

    assert result["success"] is True
    assert result["total_rounds"] == 2
    assert result["error"] is None
    assert [a["intent"] for a in result["answers"]] == ["查A", "查B"]
    assert [a["answer"] for a in result["answers"]] == ["A答案", "B答案"]
    assert all(a["success"] for a in result["answers"])


@pytest.mark.asyncio
async def test_batch_aggregates_partial_failure(
    mock_ctx: MockCtx, real_store: TaskStore, command_bus: TaskCommandBus, prompt_service: Any,
) -> None:
    """批量中一项失败：其余仍返回，top-level success=False，error 以 "; " 聚合。"""
    reg = ToolRegistry()
    reg.register(_make_tool("read"))
    loop = _make_loop(mock_ctx, reg, real_store, command_bus, prompt_service)

    # 第二个子循环 LLM 抛异常（模拟子循环失败）
    async def _flaky_llm(prompt: Any, tools: Any, model: str = "", **kw: Any) -> dict:
        nonlocal_call_count[0] += 1
        if nonlocal_call_count[0] == 2:
            raise RuntimeError("子循环炸了")
        return await original(prompt, tools, model=model, **kw)

    nonlocal_call_count = [0]
    mock_ctx.llm.set_tool_response("A答案")
    original = mock_ctx.llm.generate_with_tools
    mock_ctx.llm.generate_with_tools = _flaky_llm  # 第二次调用起失败

    result = await loop._run_subagents(_make_task("b5"), {"intents": ["查A", "查B"]})
    mock_ctx.llm.generate_with_tools = original

    assert result["success"] is False
    assert result["total_rounds"] == 1
    assert result["error"] == "子循环炸了"
    assert result["answers"][0]["success"] is True
    assert result["answers"][0]["answer"] == "A答案"
    assert result["answers"][1]["success"] is False
    assert result["answers"][1]["error"] == "子循环炸了"
