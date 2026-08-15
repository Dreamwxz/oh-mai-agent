"""子 Agent 集成测试 B — 并行工具执行排序 + 文件写沙箱 + 角色继承 + 工具覆盖校验。

对应计划 todo 8 验收，覆盖场景：
  (a) 并行排序：互锁工具强制重叠，子循环单轮 3 个 tool_calls 乱序完成仍按
      tool_calls 原始顺序落消息、tool_call_id 对应正确。
  (b) 文件写沙箱：真实 tmp_path data_dir + 真实 build_file_tools，主 AgentLoop
      内经 ask_subagent 写入 user 沙箱目录；越界写被 FileAccessPolicy 拒绝。
  (c) 角色继承：role_provider 解析出 ADMIN 时子 Agent 可写沙箱外路径；
      USER 角色时被拒。
  (d) 工具覆盖校验：ask_subagent(tools=["read"]) 时子循环 schema 仅含
      read。
  (e) 批量并行：ask_subagents 3 个独立 SubAgentLoop 真并发（互锁 Event 断言
      执行区间两两重叠），合并答案按 intents 顺序、total_rounds=3，主循环
      下一轮 LLM 调用 messages 含全部 3 个答案。
  (f) 批量取消：批量执行中经真实 command_bus 发 CANCEL，3 个子循环在各自
      下一轮边界前退出，合并结果每项 error="cancelled"。

纪律：不 mock 文件系统、不 mock 持久化（real_store 真 sqlite），仅 mock LLM。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import pytest_asyncio
from conftest import MockCtx, make_task
from oh_mai_agent.bus.command_bus import TaskCommandBus
from oh_mai_agent.bus.messages import CommandKind, TaskCommand
from oh_mai_agent.config import MaibotAgentConfig, PermissionConfig, SubAgentConfig
from oh_mai_agent.domain.task_record import TaskLevel, TaskStatus
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.executor.agent import AgentExecutor
from oh_mai_agent.executor.base import ExecutionContext
from oh_mai_agent.executor.context import current_task, make_role_provider
from oh_mai_agent.permission import PermissionResolver, Role
from oh_mai_agent.tools.agent.file_tools import build_file_tools
from oh_mai_agent.tools.agent.subagent_tool import (
    build_subagent_tool,
    build_subagents_tool,
)
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry


# ═══════════════════════════════════════════════════════════════════════════════
# 共享辅助
# ═══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def store(real_store: TaskStore) -> TaskStore:
    """基于临时文件的真实 SQLite TaskStore（不 mock 持久化）。"""
    await real_store.init()
    return real_store


async def _ok_handler(**kwargs: Any) -> dict:
    return {"success": True}


def _make_tool(name: str, handler: Any, *, essential: bool = False) -> ToolDefinition:
    """构造一个 discoverable（默认）或 essential 的样例工具。"""
    return ToolDefinition(
        name=name,
        description=f"工具 {name}",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility="essential" if essential else "discoverable",
        min_role=Role.USER,
    )


def _role_from_current_task(resolver: PermissionResolver) -> Callable[[], Role]:
    """复刻 task_manager._current_task_role 语义：从 current_task 上下文解析角色。

    子 Agent 工具与文件工具的 role_provider 均使用此回调——
    current_task 由 AgentExecutor.execute 设置，子 Agent 调用继承主任务角色。
    """
    def provider() -> Role:
        task = current_task.get()
        if task is None:
            return Role.GUEST
        return make_role_provider(resolver, task)()

    return provider


def _register_subagent_tools(reg: ToolRegistry) -> tuple[ToolDefinition, ToolDefinition]:
    """注册 ask_subagent / ask_subagents（schema 静态定义，零参数 builder）。

    执行在 AgentLoop 合成分支（executor/agent_loop.py 的 _run_subagent/
    _run_subagents），子 Agent 配置由 AgentExecutor 注入，装配期无需绑定。
    """
    single = build_subagent_tool()
    batch = build_subagents_tool()
    reg.register(single)
    reg.register(batch)
    return single, batch


def _build_env(
    mock_ctx: MockCtx,
    data_dir: Path,
    resolver: PermissionResolver,
    prompt_service: Any,
) -> ToolRegistry:
    """构造完整注册表：essential ask_user + 真实文件工具 + 两个子 Agent 工具。

    文件工具 role_provider 与子 Agent 工具 role_provider 均从 current_task
    上下文解析角色（与 task_manager.setup 一致）。
    """
    reg = ToolRegistry()
    # ask_user essential：主循环 schema 必备（子循环经排除集过滤，永不可见）。
    reg.register(_make_tool("ask_user", _ok_handler, essential=True))
    rp = _role_from_current_task(resolver)
    for tool in build_file_tools(
        mock_ctx,
        user_workspace=data_dir / "files",
        admin_open=True,
        role_provider=rp,
    ):
        reg.register(tool)
    _register_subagent_tools(reg)
    return reg


class _NoopScheduler:
    """ExecutionContext 占位：AgentLoop 路径不使用 scheduler。"""

    async def on_task_completed(self, task: Any) -> None:
        return None


async def _run_agent_executor(
    mock_ctx: MockCtx,
    reg: ToolRegistry,
    store: TaskStore,
    bus: TaskCommandBus,
    prompt_service: Any,
    pm: Any,
    resolver: PermissionResolver,
    task: Any,
) -> Any:
    """经 AgentExecutor.execute 运行真实 AgentLoop（设置 current_task 上下文；
    子 Agent 取消经合成分支注入 is_cancelled 传导）。"""
    executor = AgentExecutor(
        registry=reg,
        prompt_manager=pm,
        prompt_service=prompt_service,
        command_bus=bus,
        resolver=resolver,
    )
    exec_ctx = ExecutionContext(
        ctx=mock_ctx,
        store=store,
        scheduler=_NoopScheduler(),
        config=MaibotAgentConfig(),
        prompt_manager=pm,
        prompt_service=prompt_service,
    )
    return await executor.execute(exec_ctx, task)


def _tool_msgs(prompt: list[dict]) -> list[dict]:
    return [m for m in prompt if m.get("role") == "tool"]


class _ScriptedLLM:
    """按调用序号脚本化的 LLM：script 值可为 dict 或 callable(prompt) -> dict。

    记录 call_history 供断言（tools 参数、prompt 内容等）。
    """

    def __init__(self, script: dict[int, Any]) -> None:
        self._script = script
        self.calls = 0
        self.call_history: list[dict] = []

    async def generate_with_tools(
        self, prompt: list, tools: list, model: str = "", **kwargs: Any
    ) -> dict:
        self.calls += 1
        self.call_history.append({
            "type": "generate_with_tools", "prompt": prompt,
            "tools": tools, "model": model, **kwargs,
        })
        entry = self._script.get(self.calls)
        if entry is None:
            return {"success": True, "response": "done", "tool_calls": []}
        if callable(entry):
            return entry(prompt)
        return entry


class _GatedBatchLLM:
    """批量并行测试用门控 LLM。

    主循环调用（tools 含 essential ask_user）直通；子循环调用（固定工具集，
    永不含 ask_user）先 await 共享 gate —— 三个子循环同时阻塞在 LLM 调用内，
    证明真并发。gate 释放后 asyncio 按 gather 提交顺序 FIFO 恢复，队列消费
    确定性对应 intents 顺序。
    """

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.call_history: list[dict] = []
        self.gate = asyncio.Event()
        self.all_gated = asyncio.Event()
        self.gated_count = 0
        self.intervals: list[tuple[float, float]] = []
        self.release_at: float | None = None

    async def generate_with_tools(
        self, prompt: list, tools: list, model: str = "", **kwargs: Any
    ) -> dict:
        start = time.monotonic()
        self.call_history.append({
            "type": "generate_with_tools", "prompt": prompt,
            "tools": tools, "model": model, **kwargs,
        })
        names = {t["function"]["name"] for t in tools}
        if "ask_user" not in names:  # 子循环调用（主循环 schema 恒含 ask_user）
            self.gated_count += 1
            if self.gated_count == 3:
                self.all_gated.set()
            await self.gate.wait()
        if not self._responses:  # 兜底：脚本耗尽时直答，避免挂死
            resp: dict = {"success": True, "response": "done", "tool_calls": []}
        else:
            resp = self._responses.pop(0)
        self.intervals.append((start, time.monotonic()))
        return resp


def _ask_subagent_call(intent: str, call_id: str = "mc1") -> dict:
    return {
        "id": call_id,
        "function": {
            "name": "ask_subagent",
            "arguments": json.dumps({"intent": intent}, ensure_ascii=False),
        },
    }


def _write_file_call(path: str, content: str, call_id: str = "sc1") -> dict:
    return {
        "id": call_id,
        "function": {
            "name": "write",
            "arguments": json.dumps(
                {"path": path, "content": content}, ensure_ascii=False,
            ),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# (b) 文件写沙箱 — 真实 tmp_path + 真实 build_file_tools
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileWriteSandbox:
    @pytest.mark.asyncio
    async def test_subagent_write_lands_in_user_sandbox(
        self, mock_ctx: MockCtx, store: TaskStore,
        command_bus: TaskCommandBus, prompt_service: Any, pm: Any,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """主 AgentLoop 内经 ask_subagent 写文件 → 真实落盘 user 沙箱目录，
        答案回传主循环下一轮。"""
        data_dir = tmp_path / "data"
        resolver = PermissionResolver(PermissionConfig(users=["qq:10001"]))  # USER
        reg = _build_env(mock_ctx, data_dir, resolver, prompt_service)
        task = make_task("t8b-happy", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)

        ws_file = data_dir / "files" / "hello.txt"
        content = "子Agent写入的内容"
        mock_ctx.llm = _ScriptedLLM({
            1: {  # 主循环第 1 轮：派发子 Agent
                "success": True,
                "response": "派发子Agent",
                "tool_calls": [_ask_subagent_call("写入文件 hello.txt")],
            },
            2: {  # 子循环第 1 轮：write 写入沙箱内
                "success": True,
                "response": "",
                "tool_calls": [_write_file_call(str(ws_file), content)],
            },
            3: {"success": True, "response": "文件已写入 hello.txt", "tool_calls": []},
            4: {"success": True, "response": "任务完成", "tool_calls": []},
        })

        await _run_agent_executor(
            mock_ctx, reg, store, command_bus, prompt_service, pm, resolver, task,
        )

        # 文件真实落盘于 user 沙箱目录，内容正确
        assert ws_file.exists()
        assert ws_file.read_text(encoding="utf-8") == content

        # 子循环第 2 轮 LLM 收到 write 工具结果（id 对应正确）
        sub_r2 = mock_ctx.llm.call_history[2]
        sub_tool = _tool_msgs(sub_r2["prompt"])
        assert len(sub_tool) == 1
        assert sub_tool[0]["tool_call_id"] == "sc1"
        assert json.loads(sub_tool[0]["content"])["success"] is True

        # 主循环第 2 轮 LLM 收到 ask_subagent 结果（答案回传主 Agent 判断）
        main_r2 = mock_ctx.llm.call_history[3]
        main_tool = _tool_msgs(main_r2["prompt"])
        assert len(main_tool) == 1
        assert main_tool[0]["tool_call_id"] == "mc1"
        sub_result = json.loads(main_tool[0]["content"])
        assert sub_result["success"] is True
        assert sub_result["answer"] == "文件已写入 hello.txt"
        assert sub_result["rounds"] == 2

    @pytest.mark.asyncio
    async def test_out_of_sandbox_write_rejected(
        self, mock_ctx: MockCtx, store: TaskStore,
        command_bus: TaskCommandBus, prompt_service: Any, pm: Any,
        tmp_path: Path,
    ) -> None:
        """越界路径写入被 FileAccessPolicy 拒绝：error 回传子 Agent，
        文件不落盘。"""
        data_dir = tmp_path / "data"
        outside = tmp_path / "outside.txt"
        resolver = PermissionResolver(PermissionConfig(users=["qq:10001"]))  # USER
        reg = _build_env(mock_ctx, data_dir, resolver, prompt_service)
        task = make_task("t8b-reject", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)

        def echo_tool_result(prompt: list) -> dict:
            # 子循环第 2 轮：把 write 的工具结果回显为答案
            msgs = _tool_msgs(prompt)
            assert msgs, "子循环第 2 轮应收到 write 工具结果"
            return {
                "success": True,
                "response": msgs[-1]["content"],  # 结果 JSON 原样回显
                "tool_calls": [],
            }

        mock_ctx.llm = _ScriptedLLM({
            1: {
                "success": True,
                "response": "派发子Agent",
                "tool_calls": [_ask_subagent_call("写入沙箱外路径")],
            },
            2: {  # 越界写入
                "success": True,
                "response": "",
                "tool_calls": [_write_file_call(str(outside), "越界内容")],
            },
            3: echo_tool_result,
            4: {"success": True, "response": "任务完成", "tool_calls": []},
        })

        await _run_agent_executor(
            mock_ctx, reg, store, command_bus, prompt_service, pm, resolver, task,
        )

        # 文件未落盘；错误经子 Agent 回传主循环
        assert not outside.exists()
        main_r2 = mock_ctx.llm.call_history[3]
        main_tool = _tool_msgs(main_r2["prompt"])
        assert len(main_tool) == 1
        sub_result = json.loads(main_tool[0]["content"])
        assert "无权访问此路径" in sub_result["answer"]
        assert "沙箱外" in sub_result["answer"]


# ═══════════════════════════════════════════════════════════════════════════════
# (c) 角色继承 — role_provider 解析出 admin / user
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoleInheritance:
    @pytest.mark.asyncio
    async def test_admin_role_writes_outside_sandbox(
        self, mock_ctx: MockCtx, store: TaskStore,
        command_bus: TaskCommandBus, prompt_service: Any, pm: Any,
        tmp_path: Path,
    ) -> None:
        """owner 解析为 ADMIN：子 Agent 可写沙箱外路径（admin_open=True）。"""
        data_dir = tmp_path / "data"
        admin_path = data_dir / "admin.txt"
        resolver = PermissionResolver(PermissionConfig(admins=["qq:10001"]))  # ADMIN
        reg = _build_env(mock_ctx, data_dir, resolver, prompt_service)
        task = make_task("t8c-admin", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)

        mock_ctx.llm = _ScriptedLLM({
            1: {
                "success": True,
                "response": "派发子Agent",
                "tool_calls": [_ask_subagent_call("写入管理路径")],
            },
            2: {
                "success": True,
                "response": "",
                "tool_calls": [_write_file_call(str(admin_path), "admin内容")],
            },
            3: {"success": True, "response": "写入成功", "tool_calls": []},
            4: {"success": True, "response": "任务完成", "tool_calls": []},
        })

        await _run_agent_executor(
            mock_ctx, reg, store, command_bus, prompt_service, pm, resolver, task,
        )

        # ADMIN 不受沙箱限制：沙箱外文件真实落盘
        assert admin_path.exists()
        assert admin_path.read_text(encoding="utf-8") == "admin内容"
        main_r2 = mock_ctx.llm.call_history[3]
        sub_result = json.loads(_tool_msgs(main_r2["prompt"])[0]["content"])
        assert sub_result["success"] is True

    @pytest.mark.asyncio
    async def test_user_role_rejected_on_admin_path(
        self, mock_ctx: MockCtx, store: TaskStore,
        command_bus: TaskCommandBus, prompt_service: Any, pm: Any,
        tmp_path: Path,
    ) -> None:
        """owner 解析为 USER：写沙箱外 admin 路径被拒，文件不落盘。"""
        data_dir = tmp_path / "data"
        admin_path = data_dir / "admin.txt"
        resolver = PermissionResolver(PermissionConfig(users=["qq:10001"]))  # USER
        reg = _build_env(mock_ctx, data_dir, resolver, prompt_service)
        task = make_task("t8c-user", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)

        def echo_tool_result(prompt: list) -> dict:
            msgs = _tool_msgs(prompt)
            assert msgs, "子循环第 2 轮应收到 write 工具结果"
            return {
                "success": True,
                "response": msgs[-1]["content"],
                "tool_calls": [],
            }

        mock_ctx.llm = _ScriptedLLM({
            1: {
                "success": True,
                "response": "派发子Agent",
                "tool_calls": [_ask_subagent_call("写入管理路径")],
            },
            2: {
                "success": True,
                "response": "",
                "tool_calls": [_write_file_call(str(admin_path), "admin内容")],
            },
            3: echo_tool_result,
            4: {"success": True, "response": "任务完成", "tool_calls": []},
        })

        await _run_agent_executor(
            mock_ctx, reg, store, command_bus, prompt_service, pm, resolver, task,
        )

        assert not admin_path.exists()
        main_r2 = mock_ctx.llm.call_history[3]
        sub_result = json.loads(_tool_msgs(main_r2["prompt"])[0]["content"])
        assert "无权访问此路径" in sub_result["answer"]


# ═══════════════════════════════════════════════════════════════════════════════
# (e) 批量并行 — 3 个独立 SubAgentLoop 真并发 + 合并答案回传
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchParallel:
    @pytest.mark.asyncio
    async def test_three_subagents_true_concurrency_and_merge(
        self, mock_ctx: MockCtx, store: TaskStore,
        command_bus: TaskCommandBus, prompt_service: Any, pm: Any,
        tmp_path: Path,
    ) -> None:
        """ask_subagents 3 intent 真并发（执行区间两两重叠），合并答案按
        intents 顺序、total_rounds=3，主循环下一轮消息含全部 3 个答案。"""
        data_dir = tmp_path / "data"
        resolver = PermissionResolver(PermissionConfig(users=["qq:10001"]))
        reg = _build_env(mock_ctx, data_dir, resolver, prompt_service)
        task = make_task("t8e-batch", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)

        gated = _GatedBatchLLM([
            {  # 主循环第 1 轮：批量派发 3 个子 Agent
                "success": True,
                "response": "批量派发",
                "tool_calls": [{
                    "id": "mc1",
                    "function": {
                        "name": "ask_subagents",
                        "arguments": json.dumps(
                            {"intents": ["查A", "查B", "查C"]}, ensure_ascii=False,
                        ),
                    },
                }],
            },
            {"success": True, "response": "答A", "tool_calls": []},
            {"success": True, "response": "答B", "tool_calls": []},
            {"success": True, "response": "答C", "tool_calls": []},
            {"success": True, "response": "任务完成", "tool_calls": []},
        ])
        mock_ctx.llm = gated

        run_task = asyncio.create_task(_run_agent_executor(
            mock_ctx, reg, store, command_bus, prompt_service, pm, resolver, task,
        ))
        try:
            # 三个子循环全部进入 LLM 调用（同时阻塞在 gate 上）——真并发前提
            await asyncio.wait_for(gated.all_gated.wait(), timeout=5)
            assert gated.gated_count == 3
            gated.release_at = time.monotonic()
            gated.gate.set()
            await asyncio.wait_for(run_task, timeout=10)
        finally:
            if not run_task.done():
                run_task.cancel()

        # 三个子循环执行区间两两重叠；且都在 gate 释放前开始
        sub_intervals = gated.intervals[1:4]
        assert len(sub_intervals) == 3
        for i in range(3):
            for j in range(3):
                if i != j:
                    assert sub_intervals[i][0] < sub_intervals[j][1], (
                        f"子循环 {i} 与 {j} 执行区间应重叠"
                    )
        assert gated.release_at is not None
        assert all(s < gated.release_at for s, _ in sub_intervals)

        # 合并结果：answers 按 intents 顺序、total_rounds=3
        main_r2 = gated.call_history[-1]  # 主循环第 2 轮
        assert main_r2["type"] == "generate_with_tools"
        main_tool = _tool_msgs(main_r2["prompt"])
        assert len(main_tool) == 1
        merged = json.loads(main_tool[0]["content"])
        assert merged["success"] is True
        assert merged["total_rounds"] == 3
        assert merged["error"] is None
        assert [a["intent"] for a in merged["answers"]] == ["查A", "查B", "查C"]
        assert [a["answer"] for a in merged["answers"]] == ["答A", "答B", "答C"]
        assert all(a["success"] and a["rounds"] == 1 for a in merged["answers"])
        # 主循环下一轮 LLM 消息包含全部 3 个答案（回传主 Agent 判断）
        main_prompt = main_r2["prompt"]
        assert any("答A" in m.get("content", "") for m in main_prompt)
        assert any("答B" in m.get("content", "") for m in main_prompt)
        assert any("答C" in m.get("content", "") for m in main_prompt)
        # 任务正常完成
        persisted = await store.get(task.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# (f) 批量取消 — CANCEL 传导到全部子循环
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchCancel:
    @pytest.mark.asyncio
    async def test_batch_cancel_cancels_all_subagents(
        self, mock_ctx: MockCtx, store: TaskStore,
        command_bus: TaskCommandBus, prompt_service: Any, pm: Any,
        tmp_path: Path,
    ) -> None:
        """批量执行中经真实 command_bus 发 CANCEL：3 个子循环在各自下一轮
        边界（gather 前）退出，合并结果每项 error="cancelled"，任务终态
        CANCELLED。"""
        data_dir = tmp_path / "data"
        resolver = PermissionResolver(PermissionConfig(users=["qq:10001"]))
        reg = _build_env(mock_ctx, data_dir, resolver, prompt_service)
        # 防御：若取消未传导，echo 工具调用兜底返回
        reg.register(_make_tool("echo", _ok_handler))
        task = make_task("t8f-cancel", level=TaskLevel.AGENT, status=TaskStatus.RUNNING)
        await store.save(task)

        # 合并结果经主循环第 1 轮持久化的工具消息读取：ask_subagents 的执行
        # 在 AgentLoop 合成分支（不经 registry handler），无法包装 handler 捕获。
        gated = _GatedBatchLLM([
            {  # 主循环第 1 轮：批量派发
                "success": True,
                "response": "批量派发",
                "tool_calls": [{
                    "id": "mc1",
                    "function": {
                        "name": "ask_subagents",
                        "arguments": json.dumps(
                            {"intents": ["查A", "查B", "查C"]}, ensure_ascii=False,
                        ),
                    },
                }],
            },
            # 子循环第 1 轮均返回工具调用（保证取消发生在 gather 前边界）
            {"success": True, "response": "", "tool_calls": [
                {"id": "e1", "function": {"name": "echo", "arguments": "{}"}},
            ]},
            {"success": True, "response": "", "tool_calls": [
                {"id": "e2", "function": {"name": "echo", "arguments": "{}"}},
            ]},
            {"success": True, "response": "", "tool_calls": [
                {"id": "e3", "function": {"name": "echo", "arguments": "{}"}},
            ]},
        ])
        mock_ctx.llm = gated

        run_task = asyncio.create_task(_run_agent_executor(
            mock_ctx, reg, store, command_bus, prompt_service, pm, resolver, task,
        ))
        try:
            # 三个子循环已同时阻塞在 LLM 调用内 → 此刻发送 CANCEL
            await asyncio.wait_for(gated.all_gated.wait(), timeout=5)
            assert gated.gated_count == 3
            await command_bus.send(
                TaskCommand(task_id=task.id, kind=CommandKind.CANCEL),
            )
            gated.gate.set()
            await asyncio.wait_for(run_task, timeout=10)
        finally:
            if not run_task.done():
                run_task.cancel()

        # 合并结果：每项 error="cancelled"、rounds=0（下一轮边界前退出）
        history = await store.get_history(task.id)
        assert history, "主循环第 1 轮必须已持久化"
        tool_msgs = [m for m in history[0]["messages"] if m.get("role") == "tool"]
        assert tool_msgs, "主循环工具消息必须包含 ask_subagents 结果"
        merged = json.loads(tool_msgs[0]["content"])
        assert merged["success"] is False
        assert merged["total_rounds"] == 0
        assert len(merged["answers"]) == 3
        for item in merged["answers"]:
            assert item["success"] is False
            assert item["error"] == "cancelled"
            assert item["rounds"] == 0
            assert item["answer"] == ""
        assert merged["error"] == "cancelled; cancelled; cancelled"

        # 主任务终态 CANCELLED
        persisted = await store.get(task.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.CANCELLED
