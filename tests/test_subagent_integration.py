"""集成测试 A：主 AgentLoop 调 ask_subagent 端到端 + 排除强制 + 取消传导。

真实 AgentLoop（conftest real_store + real command_bus + real prompt_service +
MockCtx + 真实 ToolRegistry，其中注册 ask_subagent 与样例工具），Mock 仅限 LLM。

  1. 端到端：主循环第 1 轮调 ask_subagent → 子 Agent 直答 → 主循环第 2 轮
     收到含子 Agent 答案的工具消息（回传主 Agent 做判断）。
  2. 排除强制（schema 层）：ask_subagent 内部那次 generate_with_tools 的
     tools 参数不含被排除工具名 / call_ 前缀 / 发现工具。
  3. 排除强制（执行层）：子 Agent 幻觉调用 send_message → 执行守卫在
     registry.execute 之前拦截，handler 零调用、零消息发送。
  4. 取消传导：子 Agent 运行期间经真实 command_bus 发 CANCEL →
     ask_subagent 返回 success=False / error="cancelled"，主循环终止为
     CANCELLED（real_store 终态断言）。

MockLLM 队列按调用顺序消费：主循环第 1 轮 → 子 Agent 第 1 轮 → 主循环第 2 轮，
逐一排队（conftest MockLLM.set_tool_response FIFO）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio
from conftest import MockCtx, make_task
from oh_mai_agent.bus.messages import CommandKind, TaskCommand
from oh_mai_agent.config import MaibotAgentConfig, PermissionConfig, SubAgentConfig
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord, TaskStatus
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.executor.agent import AgentExecutor
from oh_mai_agent.executor.agent_loop import AgentLoop
from oh_mai_agent.executor.base import ExecutionContext
from oh_mai_agent.permission import PermissionResolver, Role
from oh_mai_agent.tools.agent.subagent_tool import build_subagent_tool
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数与夹具
# ═══════════════════════════════════════════════════════════════════════════════

async def _noop_handler(**kwargs: Any) -> dict:
    return {"success": True}


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def send_counts() -> dict[str, int]:
    """send_message handler 调用计数器（执行层守卫断言用）。"""
    return {"send_message": 0}


@pytest.fixture
def registry(
    mock_ctx: MockCtx,
    prompt_service: Any,
    send_counts: dict[str, int],
) -> ToolRegistry:
    """真实 ToolRegistry：样例工具集 + 真实 ask_subagent 工具。

    工具集覆盖三类：
      - 被排除（7 个精确名 + call_ 前缀）：ask_user / send_message /
        list_my_tasks / create_subtask / inject_task / ask_subagent /
        ask_subagents / call_plugin_api
      - 合法信息/文件/MCP 工具：search_memory / read / mcp_search
      - ask_subagent 自身（防递归）
    """
    reg = ToolRegistry()

    async def _ask_user(**kwargs: Any) -> dict:
        return {"success": True, "question": kwargs.get("question", "")}

    async def _send_message(**kwargs: Any) -> dict:
        send_counts["send_message"] += 1
        await mock_ctx.send.text("越权消息", "qq:10002")
        return {"success": True}

    reg.register(ToolDefinition(
        name="ask_user", description="向用户提问", parameters={"type": "object", "properties": {}},
        handler=_ask_user, visibility="essential", min_role=Role.USER,
    ))
    reg.register(ToolDefinition(
        name="send_message", description="发送消息", parameters={"type": "object", "properties": {}},
        handler=_send_message, visibility="essential", min_role=Role.USER,
    ))
    for name in ("list_my_tasks", "create_subtask", "inject_task"):
        reg.register(ToolDefinition(
            name=name, description=name, parameters={"type": "object", "properties": {}},
            handler=_noop_handler, visibility="discoverable", min_role=Role.USER,
        ))
    reg.register(ToolDefinition(
        name="call_plugin_api", description="跨插件 API", parameters={"type": "object", "properties": {}},
        handler=_noop_handler, visibility="discoverable", min_role=Role.USER,
    ))
    for name in ("search_memory", "read", "mcp_search"):
        reg.register(ToolDefinition(
            name=name, description=name, parameters={"type": "object", "properties": {}},
            handler=_noop_handler, visibility="discoverable", min_role=Role.USER,
        ))
    reg.register(build_subagent_tool())
    return reg


def _make_loop(
    mock_ctx: MockCtx, registry: ToolRegistry, store: TaskStore,
    command_bus: Any, prompt_service: Any,
) -> AgentLoop:
    """按 test_agent_loop.py 范本直接构建真实 AgentLoop（USER 角色）。"""
    return AgentLoop(
        ctx=mock_ctx, registry=registry, store=store,
        command_bus=command_bus, role_provider=lambda: Role.USER,
        prompt_service=prompt_service,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# (a) 端到端：ask_subagent 答案回传主 Agent 上下文
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubAgentEndToEnd:
    @pytest.mark.asyncio
    async def test_answer_round_trips_to_main_loop(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        """主循环第 1 轮调 ask_subagent → 子 Agent 直答 → 主循环第 2 轮
        收到含子答案的工具消息（回传主 Agent 做判断）。

        MockLLM 队列按调用顺序消费：main r1 → subagent r1 → main r2。
        """
        task = make_task("e2e-subagent", level=TaskLevel.AGENT, intent="主任务")

        mock_ctx.llm.set_tool_response("派发子任务", [
            {"id": "call-sub",
             "function": {"name": "ask_subagent", "arguments": '{"intent": "查X"}'}},
        ])
        mock_ctx.llm.set_tool_response("查X 的答案是 42", [])
        mock_ctx.llm.set_tool_response("根据子任务结果，任务完成", [])

        await _make_loop(mock_ctx, registry, store, command_bus, prompt_service).run(task)

        updated = await store.get(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED

        history = mock_ctx.llm.call_history
        assert [h["type"] for h in history] == ["generate_with_tools"] * 3, (
            "应为 3 次 LLM 调用：main r1 → subagent r1 → main r2"
        )

        # 子 Agent 那次调用（index 1）：system 提示含派发意图
        sub_prompt = history[1]["prompt"]
        assert sub_prompt[0]["role"] == "system"
        assert "查X" in sub_prompt[0]["content"]

        # 主循环第 2 轮（index 2）：messages 含 role="tool" 的子 Agent 结果
        main_r2_prompt = history[2]["prompt"]
        tool_msgs = [m for m in main_r2_prompt if m.get("role") == "tool"]
        assert tool_msgs, "主循环第 2 轮必须携带 ask_subagent 的工具结果消息"
        result = json.loads(tool_msgs[0]["content"])
        assert result["success"] is True
        assert result["answer"] == "查X 的答案是 42"
        assert result["rounds"] == 1
        assert result["error"] is None

        # 回传主 Agent 做判断：答案出现在主循环下一轮 LLM 收到的 messages 中
        assert any(
            "查X 的答案是 42" in m.get("content", "")
            for m in main_r2_prompt
        ), "主循环第 2 轮 LLM 收到的消息必须包含子 Agent 答案"


# ═══════════════════════════════════════════════════════════════════════════════
# (b) 排除强制（schema 层）：子 Agent 工具 schema 不含被排除工具
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubAgentSchemaExclusion:
    @pytest.mark.asyncio
    async def test_subagent_tools_schema_excludes_forbidden(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        """ask_subagent 内部那次 generate_with_tools 的 tools 参数
        （MockLLM 记录于 call_history）不含：
          - 7 个精确名（ask_user/send_message/任务管理/ask_subagent 等）
          - call_ 前缀（跨插件 API）
          - list_tools / get_tool_schema（无动态发现）
        且合法信息/文件/MCP 工具仍在。
        """
        task = make_task("schema-excl", level=TaskLevel.AGENT)

        mock_ctx.llm.set_tool_response("派发子任务", [
            {"id": "call-sub",
             "function": {"name": "ask_subagent", "arguments": '{"intent": "查X"}'}},
        ])
        mock_ctx.llm.set_tool_response("完成", [])
        mock_ctx.llm.set_tool_response("任务完成", [])

        await _make_loop(mock_ctx, registry, store, command_bus, prompt_service).run(task)

        history = mock_ctx.llm.call_history
        assert len(history) == 3
        # index 1 = ask_subagent 内部那次调用（固定工具集，无动态发现）
        sub_call = history[1]
        names = {t["function"]["name"] for t in sub_call["tools"]}

        excluded = {
            "ask_user", "send_message", "list_my_tasks", "create_subtask",
            "inject_task", "ask_subagent", "ask_subagents",
        }
        assert names.isdisjoint(excluded), f"子 Agent schema 泄漏被排除工具：{names & excluded}"
        assert not any(n.startswith("call_") for n in names), (
            f"子 Agent schema 泄漏跨插件 API 工具：{[n for n in names if n.startswith('call_')]}"
        )
        assert names.isdisjoint({"list_tools", "get_tool_schema"}), (
            "子 Agent 不得包含工具动态发现 schema"
        )

        # 合法工具仍在（信息/文件/MCP）
        assert {"search_memory", "read", "mcp_search"} <= names


# ═══════════════════════════════════════════════════════════════════════════════
# (c) 排除强制（执行层）：幻觉工具调用被守卫拦截、handler 零调用
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubAgentExecutionGuard:
    @pytest.mark.asyncio
    async def test_hallucinated_send_message_rejected_before_execution(
        self, store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any, send_counts: dict[str, int],
    ) -> None:
        """子 Agent 幻觉返回 send_message 工具调用：
          - 执行守卫拒绝：子循环内部工具消息含
            {"success": False, "error": "tool not in allowed set: send_message"}
          - send_message handler 从未被调用（计数 mock 为 0）
          - 任务不产生任何消息发送（_sent_messages 为空）
        """
        task = make_task("exec-guard", level=TaskLevel.AGENT)

        mock_ctx.llm.set_tool_response("派发子任务", [
            {"id": "call-sub",
             "function": {"name": "ask_subagent", "arguments": '{"intent": "查X"}'}},
        ])
        # 子 Agent 幻觉：调用被排除的 send_message
        mock_ctx.llm.set_tool_response("调用工具", [
            {"id": "halluc",
             "function": {"name": "send_message",
                          "arguments": '{"target": "qq:10002", "text": "越权消息"}'}},
        ])
        # 子 Agent 第 2 轮直答
        mock_ctx.llm.set_tool_response("已处理", [])
        mock_ctx.llm.set_tool_response("任务完成", [])

        await _make_loop(mock_ctx, registry, store, command_bus, prompt_service).run(task)

        updated = await store.get(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED

        history = mock_ctx.llm.call_history
        assert len(history) == 4
        # 子 Agent 第 2 轮调用（index 2）收到的 messages 含守卫拒绝的工具消息
        sub_r2_prompt = history[2]["prompt"]
        tool_msgs = [m for m in sub_r2_prompt if m.get("role") == "tool"]
        assert tool_msgs, "子循环工具结果消息必须可见"
        guard_result = json.loads(tool_msgs[0]["content"])
        assert guard_result["success"] is False
        assert guard_result["error"] == "tool not in allowed set: send_message"

        # send_message handler 从未被调用（守卫在 registry.execute 之前拦截）
        assert send_counts["send_message"] == 0
        # 任务不产生任何消息发送
        assert mock_ctx._sent_messages == []


# ═══════════════════════════════════════════════════════════════════════════════
# (d) 取消传导：CANCEL → 子 Agent 提前退出 → 主循环终止为 CANCELLED
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubAgentCancelPropagation:
    @pytest.mark.asyncio
    async def test_cancel_during_subagent_terminates_task(
        self, real_store: TaskStore, mock_ctx: MockCtx, registry: ToolRegistry,
        prompt_service: Any, command_bus: Any,
    ) -> None:
        """主任务经 AgentExecutor.execute 运行（取消经合成分支注入 is_cancelled 传导）。

        子 Agent 运行期间（阻塞在 gate_tool 内）经真实 command_bus 发送
        CANCEL（模拟 scheduler.cancel 的
        ``bus.send(TaskCommand(task_id=..., kind=CommandKind.CANCEL))``）：
          - ask_subagent 返回 success=False / error="cancelled"
            （可见于主循环第 1 轮持久化的工具消息）
          - 主循环在下一轮开始处因 _cancelled 退出，任务终止为 CANCELLED
            （real_store 终态断言）
        """
        await real_store.init()
        task = make_task("cancel-prop", level=TaskLevel.AGENT)
        gate_entered = asyncio.Event()
        release_gate = asyncio.Event()

        async def _gate(**kwargs: Any) -> dict:
            gate_entered.set()
            await release_gate.wait()
            return {"success": True, "gated": True}

        # gate_tool 合法（不在排除集），用于把子 Agent 卡在运行中
        registry.register(ToolDefinition(
            name="gate_tool", description="Gate", parameters={"type": "object", "properties": {}},
            handler=_gate, visibility="discoverable", min_role=Role.USER,
        ))

        # main r1 → ask_subagent；sub r1 → gate_tool（阻塞等待 CANCEL）
        mock_ctx.llm.set_tool_response("派发子任务", [
            {"id": "call-sub",
             "function": {"name": "ask_subagent", "arguments": '{"intent": "查X"}'}},
        ])
        mock_ctx.llm.set_tool_response("调用工具", [
            {"id": "gate-1", "function": {"name": "gate_tool", "arguments": "{}"}},
        ])

        executor = AgentExecutor(
            registry=registry, prompt_service=prompt_service, command_bus=command_bus,
            # 真实 wiring：resolver 把任务 owner 解析为 USER，否则
            # AgentExecutor 回退 GUEST，ask_subagent（min_role=USER）
            # 会因 permission denied 而不执行子循环。
            resolver=PermissionResolver(PermissionConfig(users=["qq:10001"])),
        )
        exec_ctx = ExecutionContext(
            ctx=mock_ctx, store=real_store, scheduler=None,
            config=MaibotAgentConfig(), prompt_service=prompt_service,
        )

        run_task = asyncio.create_task(executor.execute(exec_ctx, task))
        # 等待子 Agent 进入 gate_tool（子循环运行中）
        await asyncio.wait_for(gate_entered.wait(), timeout=5)
        # 模拟 scheduler.cancel：向真实 command_bus 发 CANCEL
        await command_bus.send(TaskCommand(task_id=task.id, kind=CommandKind.CANCEL))
        release_gate.set()
        await asyncio.wait_for(run_task, timeout=5)

        # ask_subagent 返回 cancelled（可见于主循环第 1 轮持久化的工具消息）
        history = await real_store.get_history(task.id)
        assert history, "主循环第 1 轮必须已持久化"
        tool_msgs = [m for m in history[0]["messages"] if m.get("role") == "tool"]
        assert tool_msgs, "主循环工具消息必须包含 ask_subagent 结果"
        sub_result = json.loads(tool_msgs[0]["content"])
        assert sub_result["success"] is False
        assert sub_result["error"] == "cancelled"

        # 主循环在下一轮开始处退出，任务终止为 CANCELLED（real_store 终态）
        persisted = await real_store.get(task.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.CANCELLED
        assert persisted.status.value == "cancelled"
