from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import MockCtx, MockLLM
from oh_mai_agent.config import MaibotAgentConfig, PermissionConfig
from oh_mai_agent.bus.command_bus import TaskCommandBus
from oh_mai_agent.core.scheduler import TaskScheduler
from oh_mai_agent.core.task_manager import TaskManager
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.executor.base import ExecutionContext
from oh_mai_agent.permission import PermissionResolver, Role
from oh_mai_agent.prompt.builders import ALL_BUILDERS
from oh_mai_agent.prompt.manager import PromptManager
from oh_mai_agent.prompt.service import PromptService
from oh_mai_agent.tools.registry import ToolRegistry


async def _noop_executor(task: TaskRecord) -> None:
    pass


async def _make_manager(
    store: TaskStore,
    ctx: MockCtx,
    llm: MockLLM,
    role_config: PermissionConfig,
) -> TaskManager:
    await store.init()
    config = MaibotAgentConfig(permission=role_config)
    resolver = PermissionResolver(config.permission)
    command_bus = TaskCommandBus()
    scheduler = TaskScheduler(config.task, store, _noop_executor, command_bus=command_bus)
    registry = ToolRegistry()
    prompt_manager = PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates")
    prompt_service = PromptService(prompt_manager, ALL_BUILDERS)

    async def make_title(intent: str) -> str:
        result = await llm.generate(prompt=intent, model="utils")
        return result["response"]

    manager = TaskManager(
        ctx=ctx,
        store=store,
        scheduler=scheduler,
        registry=registry,
        resolver=resolver,
        config=config,
        llm_title=make_title,
        data_dir=Path(__file__).resolve().parent.parent / "data",
        prompt_manager=prompt_manager,
        prompt_service=prompt_service,
        command_bus=command_bus,
    )
    await manager.setup()
    return manager


async def _schemas_for_role(
    manager: TaskManager,
    ctx: MockCtx,
    owner: str,
    caller_role: Role,
) -> set[str]:
    ctx.llm.set_generate_response("wiring test task")
    ok, task_or_error = await manager.create_task(
        intent="run the wiring integration task",
        owner=owner,
        platform="qq",
        stream_id=f"qq:{owner.split(':', 1)[1]}",
        level=None,
        trigger=TriggerType.DELAY,
        delay_seconds=3600,
        caller_role=caller_role,
    )
    assert ok
    assert isinstance(task_or_error, TaskRecord)
    task = task_or_error
    task.force(TaskStatus.PENDING, actor="test", reason="manual executor invocation")

    ctx.llm.set_tool_response(
        "discover tools",
        [
            {
                "id": "send-schema",
                "type": "function",
                "function": {
                    "name": "get_tool_schema",
                    "arguments": json.dumps({"name": "send_message"}),
                },
            },
            {
                "id": "read-schema",
                "type": "function",
                "function": {
                    "name": "get_tool_schema",
                    "arguments": json.dumps({"name": "read"}),
                },
            },
        ],
    )
    ctx.llm.set_tool_response("finish")
    result = await manager._executor_factory.get(TaskLevel.AGENT).execute(
        ExecutionContext(
            ctx=ctx,
            store=manager._store,
            scheduler=manager._scheduler,
            config=manager._config,
            prompt_manager=manager._prompt_manager,
            prompt_service=manager._prompt_service,
        ),
        task,
    )
    assert result.status == "COMPLETED"

    calls = [call for call in ctx.llm.call_history if call["type"] == "generate_with_tools"]
    assert calls
    return {
        schema["function"]["name"]
        for call in calls
        for schema in call["tools"]
    }


@pytest.mark.asyncio
async def test_admin_agent_wiring_preserves_admin_tool_visibility(real_store: TaskStore) -> None:
    ctx = MockCtx()
    llm = ctx.llm
    manager = await _make_manager(
        real_store,
        ctx,
        llm,
        PermissionConfig(admins=["qq:10001"], users=["qq:20001"]),
    )

    names = await _schemas_for_role(manager, ctx, "qq:10001", Role.ADMIN)

    assert {"ask_user", "send_message", "read"} <= names


@pytest.mark.asyncio
async def test_user_agent_wiring_preserves_user_tool_visibility(real_store: TaskStore) -> None:
    ctx = MockCtx()
    manager = await _make_manager(
        real_store,
        ctx,
        ctx.llm,
        PermissionConfig(admins=["qq:10001"], users=["qq:20001"]),
    )

    names = await _schemas_for_role(manager, ctx, "qq:20001", Role.USER)

    assert {"ask_user", "send_message", "read"} <= names


@pytest.mark.asyncio
async def test_agent_wiring_runs_with_creator_role_and_direct_tools(real_store: TaskStore) -> None:
    """任务以创建者角色执行（_caller_role 持久化），且工具全量直接暴露。

    owner=qq:30001 解析为 guest，但创建者 caller_role=USER → 任务以 USER 执行，
    USER 可见工具（ask_user/send_message/read）全部直接暴露在 schema 中；
    不再有 list_tools / get_tool_schema 发现仪式。
    """
    ctx = MockCtx()
    manager = await _make_manager(
        real_store,
        ctx,
        ctx.llm,
        PermissionConfig(admins=["qq:10001"], users=["qq:20001"]),
    )

    names = await _schemas_for_role(manager, ctx, "qq:30001", Role.USER)

    # 直接全量暴露：无发现仪式工具
    assert "list_tools" not in names
    assert "get_tool_schema" not in names
    # 创建者角色 USER → USER 可见工具全部直接暴露（含原 guest 测试中不可见的工具）
    assert {"ask_user", "send_message", "read"} <= names
