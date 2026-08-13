"""oh_mai_agent.executor.subagent 的单元测试 — SubAgentLoop。

覆盖（对应计划 todo 4 验收）：
  1. 单轮直答（无工具调用）
  2. 多轮含工具：并行执行 + 结果按 tool_calls 原始顺序追加
  3. 并行断言：两个工具 handler 用 asyncio.Event 互锁并记录时间区间，断言两区间重叠
  4. max_rounds 截断（含 max_rounds=1 全 tool_calls 边界）
  5. 答案截断（max_result_chars=8000）
  6. LLM 异常 → success False
  7. cancel 命中 → 提前退出（轮首 / gather 前）
  8. should_cancel=None 时不炸
  9. 幻觉工具名被执行守卫拒绝且 execute_tool 未被调用
  10. 空工具集（tools=[]）正常直答
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from oh_mai_agent.executor.subagent import SubAgentLoop
from oh_mai_agent.permission import Role
from oh_mai_agent.tools.registry import ToolDefinition


async def _echo_handler(**kwargs: Any) -> dict[str, Any]:
    return {"success": True, "echo": kwargs}


def _make_tool(name: str, handler: Any, description: str = "测试工具") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility="discoverable",
        min_role=Role.GUEST,
    )


def _make_execute_tool(
    handlers: dict[str, Any], calls: list[str] | None = None
) -> Any:
    """构造 execute_tool 适配器：按工具名分发到 handler，可选记录调用顺序。"""

    async def _execute(name: str, role: Role, args: dict) -> dict[str, Any]:
        if calls is not None:
            calls.append(name)
        return await handlers[name](**args)

    return _execute


def _build_loop(
    mock_ctx: Any,
    prompt_service: Any,
    tools: list[ToolDefinition],
    *,
    max_rounds: int = 10,
    max_result_chars: int = 8000,
    should_cancel: Any = None,
    execute_tool: Any,
    llm_timeout_ms: int = 240000,
) -> SubAgentLoop:
    return SubAgentLoop(
        ctx=mock_ctx,
        tools=tools,
        role=Role.USER,
        prompt_service=prompt_service,
        max_rounds=max_rounds,
        max_result_chars=max_result_chars,
        should_cancel=should_cancel,
        llm_timeout_ms=llm_timeout_ms,
        execute_tool=execute_tool,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 单轮直答
# ═══════════════════════════════════════════════════════════════════════════════


class TestDirectAnswer:
    @pytest.mark.asyncio
    async def test_single_round_direct_answer(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """LLM 第一轮即无工具调用 → 直接产出答案。"""
        tools = [_make_tool("echo", _echo_handler)]
        mock_ctx.llm.set_tool_response("这是最终答案", [])

        loop = _build_loop(
            mock_ctx, prompt_service, tools,
            execute_tool=_make_execute_tool({}),
        )
        result = await loop.run("查一下天气")

        assert result == {
            "success": True,
            "answer": "这是最终答案",
            "rounds": 1,
            "max_rounds_reached": False,
            "error": None,
        }
        # 系统提示词包含 intent 与工具列表；tools 参数为固定工具集 schema
        call = mock_ctx.llm.call_history[0]
        assert call["prompt"][0]["role"] == "system"
        assert "查一下天气" in call["prompt"][0]["content"]
        assert "- echo: 测试工具" in call["prompt"][0]["content"]
        assert call["tools"] == [t.to_llm_definition() for t in tools]
        assert call["model"] == "planner"
        assert call["timeout_ms"] == 240000

    @pytest.mark.asyncio
    async def test_empty_toolset_direct_answer(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """工具集为空（tools=[]）→ 不特殊处理，正常直答。"""
        mock_ctx.llm.set_tool_response("直接答案", [])

        loop = _build_loop(
            mock_ctx, prompt_service, [],
            execute_tool=_make_execute_tool({}),
        )
        result = await loop.run("意图")

        assert result["success"] is True
        assert result["answer"] == "直接答案"
        assert result["rounds"] == 1
        assert mock_ctx.llm.call_history[0]["tools"] == []

    @pytest.mark.asyncio
    async def test_should_cancel_none_ok(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """should_cancel 缺省 None → 不检查取消，正常直答。"""
        mock_ctx.llm.set_tool_response("答案", [])

        loop = _build_loop(
            mock_ctx, prompt_service, [_make_tool("echo", _echo_handler)],
            execute_tool=_make_execute_tool({}),
        )
        result = await loop.run("意图")

        assert result["success"] is True
        assert result["answer"] == "答案"
        assert result["error"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 多轮含工具：并行执行 + 保序
# ═══════════════════════════════════════════════════════════════════════════════


class TestParallelAndOrdering:
    @pytest.mark.asyncio
    async def test_parallel_execution_and_ordered_results(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """一轮内多个工具调用并行执行（时间区间重叠），结果按 tool_calls 原始顺序追加。"""
        gate_a = asyncio.Event()
        gate_b = asyncio.Event()
        intervals: dict[str, tuple[float, float]] = {}

        async def slow_a(**kwargs: Any) -> dict[str, Any]:
            start = time.monotonic()
            gate_a.set()
            await gate_b.wait()
            intervals["a"] = (start, time.monotonic())
            return {"success": True, "name": "a"}

        async def slow_b(**kwargs: Any) -> dict[str, Any]:
            await gate_a.wait()
            start = time.monotonic()
            gate_b.set()
            intervals["b"] = (start, time.monotonic())
            return {"success": True, "name": "b", "x": kwargs.get("x")}

        async def fast_c(**kwargs: Any) -> dict[str, Any]:
            return {"success": True, "name": "c"}

        tools = [
            _make_tool("t_a", slow_a),
            _make_tool("t_b", slow_b),
            _make_tool("t_c", fast_c),
        ]
        # 第 1 轮：3 个工具调用（t_a/t_b 互锁强制重叠，t_c 瞬时完成）
        mock_ctx.llm.set_tool_response("调用工具", [
            {"id": "call-1", "function": {"name": "t_a", "arguments": "{}"}},
            {"id": "call-2", "function": {"name": "t_b", "arguments": '{"x": 1}'}},
            {"id": "call-3", "function": {"name": "t_c", "arguments": "{}"}},
        ])
        mock_ctx.llm.set_tool_response("全部完成", [])

        executed: list[str] = []
        loop = _build_loop(
            mock_ctx, prompt_service, tools,
            execute_tool=_make_execute_tool(
                {"t_a": slow_a, "t_b": slow_b, "t_c": fast_c}, executed,
            ),
        )

        # wait_for 兜底：若实现退化为顺序执行，t_a 会永远等 gate_b 而挂死
        result = await asyncio.wait_for(loop.run("意图"), timeout=5)

        assert result["success"] is True
        assert result["answer"] == "全部完成"
        assert result["rounds"] == 2
        assert result["error"] is None
        # 两个互锁 handler 均执行，且执行区间重叠（真并发）
        assert "a" in intervals and "b" in intervals
        a_start, a_end = intervals["a"]
        b_start, b_end = intervals["b"]
        assert b_start < a_end and a_start < b_end

        # 第 2 轮 LLM 收到的 tool 消息按 tool_calls 原始顺序追加，tool_call_id 对应正确
        second_call = mock_ctx.llm.call_history[1]
        tool_msgs = [m for m in second_call["prompt"] if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["call-1", "call-2", "call-3"]
        assert json.loads(tool_msgs[0]["content"]) == {"success": True, "name": "a"}
        assert json.loads(tool_msgs[1]["content"]) == {"success": True, "name": "b", "x": 1}
        assert json.loads(tool_msgs[2]["content"]) == {"success": True, "name": "c"}
        # 三个工具均已执行（乱序完成也保序落消息）
        assert sorted(executed) == ["t_a", "t_b", "t_c"]

    @pytest.mark.asyncio
    async def test_execute_tool_exception_captured(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """工具 handler 抛异常 → _exec_call 全捕获为错误 dict，gather 不抛。"""
        async def exploding(**kwargs: Any) -> dict[str, Any]:
            raise ValueError("tool exploded")

        tools = [_make_tool("boom", exploding)]
        mock_ctx.llm.set_tool_response("尝试", [
            {"id": "c1", "function": {"name": "boom", "arguments": "{}"}},
        ])
        mock_ctx.llm.set_tool_response("继续完成", [])

        loop = _build_loop(
            mock_ctx, prompt_service, tools,
            execute_tool=_make_execute_tool({"boom": exploding}),
        )
        result = await loop.run("意图")

        assert result["success"] is True
        assert result["answer"] == "继续完成"
        assert result["rounds"] == 2
        second_call = mock_ctx.llm.call_history[1]
        tool_msgs = [m for m in second_call["prompt"] if m.get("role") == "tool"]
        assert json.loads(tool_msgs[0]["content"]) == {
            "success": False, "error": "tool exploded",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# max_rounds 截断
# ═══════════════════════════════════════════════════════════════════════════════


class TestMaxRounds:
    @pytest.mark.asyncio
    async def test_max_rounds_cutoff(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """LLM 每轮都返回工具调用 → max_rounds 耗尽，取最后一轮 assistant content。"""
        tools = [_make_tool("echo", _echo_handler)]
        mock_ctx.llm.set_tool_response("第一轮", [
            {"id": "c1", "function": {"name": "echo", "arguments": "{}"}},
        ])
        mock_ctx.llm.set_tool_response("第二轮", [
            {"id": "c2", "function": {"name": "echo", "arguments": "{}"}},
        ])

        loop = _build_loop(
            mock_ctx, prompt_service, tools, max_rounds=2,
            execute_tool=_make_execute_tool({"echo": _echo_handler}),
        )
        result = await loop.run("意图")

        assert result["success"] is True
        assert result["rounds"] == 2
        assert result["max_rounds_reached"] is True
        assert result["answer"] == "第二轮"
        assert result["error"] is None
        # 只调用了 2 次 LLM（未越界）
        assert len(mock_ctx.llm.call_history) == 2

    @pytest.mark.asyncio
    async def test_max_rounds_one_all_tool_calls_boundary(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """max_rounds=1 且该轮全为 tool_calls → answer 为空串、max_rounds_reached=True。"""
        tools = [_make_tool("echo", _echo_handler)]
        # 纯工具调用轮：LLM 无文本回复
        mock_ctx.llm.set_tool_response("", [
            {"id": "c1", "function": {"name": "echo", "arguments": "{}"}},
        ])

        loop = _build_loop(
            mock_ctx, prompt_service, tools, max_rounds=1,
            execute_tool=_make_execute_tool({"echo": _echo_handler}),
        )
        result = await loop.run("意图")

        assert result["success"] is True
        assert result["rounds"] == 1
        assert result["max_rounds_reached"] is True
        assert result["answer"] == ""
        assert result["error"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 答案截断
# ═══════════════════════════════════════════════════════════════════════════════


class TestAnswerTruncation:
    @pytest.mark.asyncio
    async def test_answer_truncated_at_max_result_chars(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """答案超过 max_result_chars → 截断并追加「…（已截断）」。"""
        tools = [_make_tool("echo", _echo_handler)]
        long_answer = "长" * 9000
        mock_ctx.llm.set_tool_response(long_answer, [])

        loop = _build_loop(
            mock_ctx, prompt_service, tools, max_result_chars=8000,
            execute_tool=_make_execute_tool({}),
        )
        result = await loop.run("意图")

        assert result["success"] is True
        assert len(result["answer"]) == 8000 + len("…（已截断）")
        assert result["answer"].startswith("长" * 8000)
        assert result["answer"].endswith("…（已截断）")

    @pytest.mark.asyncio
    async def test_short_answer_not_truncated(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """答案未超限 → 原样返回，不追加截断标记。"""
        mock_ctx.llm.set_tool_response("短答案", [])
        loop = _build_loop(
            mock_ctx, prompt_service, [_make_tool("echo", _echo_handler)],
            max_result_chars=8000,
            execute_tool=_make_execute_tool({}),
        )
        result = await loop.run("意图")

        assert result["success"] is True
        assert result["answer"] == "短答案"


# ═══════════════════════════════════════════════════════════════════════════════
# 异常与取消
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailurePaths:
    @pytest.mark.asyncio
    async def test_llm_exception(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """LLM 调用抛异常 → success False、error 非空、rounds 0。"""
        async def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("llm down")

        mock_ctx.llm.generate_with_tools = failing
        loop = _build_loop(
            mock_ctx, prompt_service, [_make_tool("echo", _echo_handler)],
            execute_tool=_make_execute_tool({}),
        )
        result = await loop.run("意图")

        assert result == {
            "success": False,
            "answer": "",
            "rounds": 0,
            "max_rounds_reached": False,
            "error": "llm down",
        }

    @pytest.mark.asyncio
    async def test_cancel_at_round_start(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """轮首取消检查命中 → 提前返回，LLM 与工具均未被调用。"""
        executed: list[str] = []
        loop = _build_loop(
            mock_ctx, prompt_service, [_make_tool("echo", _echo_handler)],
            should_cancel=lambda: True,
            execute_tool=_make_execute_tool({"echo": _echo_handler}, executed),
        )
        result = await loop.run("意图")

        assert result == {
            "success": False,
            "answer": "",
            "rounds": 0,
            "max_rounds_reached": False,
            "error": "cancelled",
        }
        assert mock_ctx.llm.call_history == []
        assert executed == []

    @pytest.mark.asyncio
    async def test_cancel_before_tool_gather(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """工具调用轮中、gather 前取消命中 → 工具不执行、提前返回。"""
        executed: list[str] = []
        tools = [_make_tool("echo", _echo_handler)]
        mock_ctx.llm.set_tool_response("先用工具", [
            {"id": "c1", "function": {"name": "echo", "arguments": "{}"}},
        ])

        state = {"cancelled": False}

        def should_cancel() -> bool:
            cancelled = state["cancelled"]
            state["cancelled"] = True  # 第一次检查放行，第二次（gather 前）命中
            return cancelled

        loop = _build_loop(
            mock_ctx, prompt_service, tools,
            should_cancel=should_cancel,
            execute_tool=_make_execute_tool({"echo": _echo_handler}, executed),
        )
        result = await loop.run("意图")

        assert result["success"] is False
        assert result["error"] == "cancelled"
        assert result["rounds"] == 0
        assert result["max_rounds_reached"] is False
        assert executed == []


# ═══════════════════════════════════════════════════════════════════════════════
# 执行守卫（幻觉工具名）
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecutionGuard:
    @pytest.mark.asyncio
    async def test_hallucinated_tool_name_rejected(
        self, mock_ctx: Any, prompt_service: Any,
    ) -> None:
        """幻觉工具名不在允许集 → 执行守卫拒绝，execute_tool 未被调用。"""
        executed: list[str] = []
        tools = [_make_tool("echo", _echo_handler)]
        # 第 1 轮：幻觉调用 send_message（不在允许集）
        mock_ctx.llm.set_tool_response("尝试", [
            {"id": "evil-1", "function": {"name": "send_message", "arguments": '{"text": "hack"}'}},
        ])
        mock_ctx.llm.set_tool_response("安全完成", [])

        loop = _build_loop(
            mock_ctx, prompt_service, tools,
            execute_tool=_make_execute_tool({"echo": _echo_handler}, executed),
        )
        result = await loop.run("意图")

        # 守卫拒绝后循环继续，第 2 轮直答成功
        assert result["success"] is True
        assert result["answer"] == "安全完成"
        assert result["rounds"] == 2
        assert executed == []  # execute_tool 从未被调用

        # 第 2 轮 LLM 收到的 tool 消息为错误结果
        second_call = mock_ctx.llm.call_history[1]
        tool_msgs = [m for m in second_call["prompt"] if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "evil-1"
        assert json.loads(tool_msgs[0]["content"]) == {
            "success": False,
            "error": "tool not in allowed set: send_message",
        }
