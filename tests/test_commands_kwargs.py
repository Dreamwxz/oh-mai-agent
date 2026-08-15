"""测试命令 kwargs 提取 —— 模拟 MaiBot 真实 kwargs 结构。

MaiBot 命令执行器（component_query.py:474-556）传入的 kwargs 包含以下键：
  text, stream_id, group_id, platform, user_id, is_local_operator,
  matched_groups, message, plugin_config

它不传入 "plain_text" 键。插件的命令处理方法必须从 "text"（完整消息）
和 "matched_groups"（正则分组）中提取参数。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from conftest import MockCtx, make_task

from oh_mai_agent.config import PermissionConfig
from oh_mai_agent.commands import (
    cmd_arg,
    cmd_ask,
    cmd_cancel,
    cmd_create,
    cmd_history,
    cmd_status,
    cmd_text,
)
from oh_mai_agent.domain.status_formatter import StatusFormatter
from oh_mai_agent.permission import PermissionResolver, Role
from oh_mai_agent.plugin import MaibotAgentPlugin
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord, TaskStatus
from oh_mai_agent.domain.task_store import TaskStore

# ══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════════════════════

class FakeTaskManager:
    """TaskManager 的内存版 fake，用于命令测试。"""

    def __init__(self, store: TaskStore) -> None:
        self._store = store
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.modified: list[dict] = []
        self._sfmt = StatusFormatter()

    async def create_task(self, **kw: Any) -> tuple[bool, object]:
        task = TaskRecord(
            id="t1",
            title=kw.get("intent", "")[:10] or "untitled",
            intent=kw.get("intent", ""),
            level=kw.get("level") or TaskLevel.AGENT,
            owner=kw.get("owner", ""),
            stream_id=kw.get("stream_id", ""),
            platform=kw.get("platform", ""),
        )
        self.created.append(kw)
        await self._store.save(task)
        return (True, task)

    async def list_tasks(self, caller_role: Role, owner: str,
                         status: TaskStatus | None = None,
                         stream_id: str | None = None,
                         limit: int = 20) -> list[dict]:
        tasks = await self._store.list(owner=owner or None, status=status,
                                        stream_id=stream_id, limit=limit)
        return [
            {
                "id": t.id, "title": t.title, "level": t.level.value,
                "status": t.status.value,
                "format_status": self._sfmt.format(*t.status_info()), "owner": t.owner,
            }
            for t in tasks
        ]

    async def get_task(self, task_id: str, caller_role: Role,
                       owner: str) -> tuple[bool, object]:
        t = await self._store.get(task_id)
        if t:
            return (True, t)
        return (False, "not found")

    async def cancel_task(self, task_id: str, caller_role: Role,
                          owner: str) -> tuple[bool, str]:
        self.cancelled.append(task_id)
        return (True, "已取消")

    async def modify_task(self, task_id: str, caller_role: Role,
                          owner: str, inject_instruction: str) -> tuple[bool, str]:
        self.modified.append({"task_id": task_id, "instruction": inject_instruction})
        return (True, "已注入")

    async def task_history(self, task_id: str, caller_role: Role,
                           owner: str, limit: int = 20) -> tuple[bool, list[dict]]:
        return (True, [{"type": "status_change", "timestamp": "2025-01-01"}])


@pytest_asyncio.fixture
async def fake_store(real_store: TaskStore) -> TaskStore:
    await real_store.init()
    return real_store


@pytest.fixture
def plugin(fake_store: TaskStore) -> MaibotAgentPlugin:
    from oh_mai_agent.config import MaibotAgentConfig
    from oh_mai_agent.executor.sender import ReplySender

    p = MaibotAgentPlugin()
    p._task_manager = FakeTaskManager(fake_store)
    p._sfmt = StatusFormatter()
    p._resolver = PermissionResolver(PermissionConfig(
        admins=["qq:1"], users=["qq:2"],
    ))
    mock_ctx = MockCtx()
    p._set_context(mock_ctx)
    p._mock_ctx = mock_ctx  # 暴露给 send.text 断言使用
    # FakeTaskManager 注入真实 ReplySender（直发出口写回 mock_ctx._sent_messages）
    p._task_manager.sender = ReplySender(
        ctx=mock_ctx, config_getter=lambda: MaibotAgentConfig(),
    )
    return p


@pytest.fixture
def base_kwargs() -> dict[str, Any]:
    """群聊命令的最小 MaiBot 真实结构 kwargs。"""
    return {
        "text": "/maitask create 帮我查天气",
        "stream_id": "qq:group:123",
        "platform": "qq",
        "user_id": "2",
        "group_id": "123",
        "matched_groups": {},
    }


# ══════════════════════════════════════════════════════════════════════════════
# 测试
# ══════════════════════════════════════════════════════════════════════════════


class TestCmdCreate:
    @pytest.mark.asyncio
    async def test_create_with_text_key(self, plugin: MaibotAgentPlugin,
                                        base_kwargs: dict[str, Any]) -> None:
        """/maitask create 应从 'text' kwarg 提取 intent（而非 plain_text）。"""
        kwargs = {**base_kwargs, "text": "/maitask create 帮我查天气 --level agent"}
        ok, resp, pri = await plugin.cmd_task_create(**kwargs)
        assert ok, (ok, resp)
        tm: FakeTaskManager = plugin._task_manager  # type: ignore[assignment]
        assert tm.created and tm.created[0]["intent"] == "帮我查天气 --level agent"

    @pytest.mark.asyncio
    async def test_create_no_plain_text_key(self, plugin: MaibotAgentPlugin,
                                            base_kwargs: dict[str, Any]) -> None:
        """当 plain_text 键缺失时，仍应从 text 提取。"""
        kwargs = dict(base_kwargs, text="/maitask create 复杂任务描述 here")
        ok, resp, pri = await plugin.cmd_task_create(**kwargs)
        assert ok, (ok, resp)

    @pytest.mark.asyncio
    async def test_create_empty_intent(self, plugin: MaibotAgentPlugin,
                                       base_kwargs: dict[str, Any]) -> None:
        """'/maitask create' 后为空 intent 应返回错误。"""
        kwargs = {**base_kwargs, "text": "/maitask create   "}
        ok, resp, pri = await plugin.cmd_task_create(**kwargs)
        assert not ok
        assert "用法" in resp

    @pytest.mark.asyncio
    async def test_create_permission_denied_guest(self, plugin: MaibotAgentPlugin,
                                                  base_kwargs: dict[str, Any]) -> None:
        kwargs = dict(base_kwargs, user_id="99")
        ok, resp, pri = await plugin.cmd_task_create(**kwargs)
        assert not ok
        assert "权限不足" in resp

    @pytest.mark.asyncio
    async def test_create_sends_reply(self, plugin: MaibotAgentPlugin,
                                      base_kwargs: dict[str, Any]) -> None:
        """命令 handler 必须通过 ctx.send.text 发送响应到聊天流。"""
        kwargs = {**base_kwargs, "text": "/maitask create 测试任务"}
        ok, resp, pri = await plugin.cmd_task_create(**kwargs)
        assert ok, (ok, resp)
        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent, "命令响应必须发送"
        assert sent[-1]["text"] == resp, "发送内容应等于响应文本"
        assert sent[-1]["stream_id"] == base_kwargs["stream_id"]

    @pytest.mark.asyncio
    async def test_create_sends_permission_denied(self, plugin: MaibotAgentPlugin,
                                                  base_kwargs: dict[str, Any]) -> None:
        """权限不足时也应发送拒绝消息。"""
        kwargs = dict(base_kwargs, user_id="99", text="/maitask create x")
        ok, resp, pri = await plugin.cmd_task_create(**kwargs)
        assert not ok
        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent, "权限不足也应发送"
        assert sent[-1]["text"] == resp


class TestCmdList:
    @pytest.mark.asyncio
    async def test_list_with_text_key(self, plugin: MaibotAgentPlugin,
                                      fake_store: TaskStore,
                                      base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask list"}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert ok and "测试" in resp, (ok, resp)

    @pytest.mark.asyncio
    async def test_list_no_plain_text_key(self, plugin: MaibotAgentPlugin,
                                          base_kwargs: dict[str, Any]) -> None:
        kwargs = dict(base_kwargs, text="/maitask list pending")
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert ok, (ok, resp)

    @pytest.mark.asyncio
    async def test_list_sends_reply(self, plugin: MaibotAgentPlugin,
                                    base_kwargs: dict[str, Any]) -> None:
        kwargs = {**base_kwargs, "text": "/maitask list"}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert ok
        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent, "命令响应必须发送"
        assert sent[-1]["text"] == resp

    # ── -all 语义矩阵测试 ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_list_admin_all(self, plugin: MaibotAgentPlugin,
                                  fake_store: TaskStore,
                                  base_kwargs: dict[str, Any]) -> None:
        """admin /maitask list -all → 跨 owner 全部任务。"""
        # 创建三个不同 owner 的任务
        await fake_store.save(make_task(task_id="ta", title="Admin任务",
                                        owner="qq:1", stream_id="qq:group:123"))
        await fake_store.save(make_task(task_id="tu", title="User任务",
                                        owner="qq:2", stream_id="qq:group:123"))
        await fake_store.save(make_task(task_id="tp", title="Planner任务",
                                        owner="planner:qq:10001", stream_id="qq:group:123"))
        # admin 身份：user_id="1"，私聊形式 stream_id 避免 admin_in_group_chats 降级
        kwargs = {"text": "/maitask list -all", "stream_id": "qq:1",
                  "platform": "qq", "user_id": "1", "group_id": "",
                  "matched_groups": {}}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert ok, (ok, resp)
        assert "Admin任务" in resp, "应包含 admin 任务"
        assert "User任务" in resp, "应包含 user 任务"
        assert "Planner任务" in resp, "应包含 planner 任务"

    @pytest.mark.asyncio
    async def test_list_admin_all_status(self, plugin: MaibotAgentPlugin,
                                         fake_store: TaskStore,
                                         base_kwargs: dict[str, Any]) -> None:
        """admin /maitask list -all pending → 跨 owner + 状态过滤。"""
        await fake_store.save(make_task(task_id="ta", title="已完成任务",
                                        owner="qq:1", stream_id="qq:g:1",
                                        status=TaskStatus.COMPLETED))
        await fake_store.save(make_task(task_id="tb", title="等待任务",
                                        owner="qq:2", stream_id="qq:g:1",
                                        status=TaskStatus.PENDING))
        await fake_store.save(make_task(task_id="tc", title="Planner等待",
                                        owner="planner:qq:10001", stream_id="qq:g:1",
                                        status=TaskStatus.PENDING))
        kwargs = {"text": "/maitask list -all pending", "stream_id": "qq:1",
                  "platform": "qq", "user_id": "1", "group_id": "",
                  "matched_groups": {}}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert ok, (ok, resp)
        assert "等待任务" in resp, "应包含 user 等待任务"
        assert "Planner等待" in resp, "应包含 planner 等待任务"
        assert "已完成任务" not in resp, "不应包含已完成任务"

    @pytest.mark.asyncio
    async def test_list_admin_no_all(self, plugin: MaibotAgentPlugin,
                                     fake_store: TaskStore,
                                     base_kwargs: dict[str, Any]) -> None:
        """admin /maitask list（无 -all）→ 只看自己。"""
        await fake_store.save(make_task(task_id="ta", title="Admin任务",
                                        owner="qq:1", stream_id="qq:g:1"))
        await fake_store.save(make_task(task_id="tu", title="User任务",
                                        owner="qq:2", stream_id="qq:g:1"))
        await fake_store.save(make_task(task_id="tp", title="Planner任务",
                                        owner="planner:qq:10001", stream_id="qq:g:1"))
        kwargs = {"text": "/maitask list", "stream_id": "qq:1",
                  "platform": "qq", "user_id": "1", "group_id": "",
                  "matched_groups": {}}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert ok, (ok, resp)
        assert "Admin任务" in resp, "应包含 admin 自己的任务"
        assert "User任务" not in resp, "不应包含 user 任务"
        assert "Planner任务" not in resp, "不应包含 planner 任务"

    @pytest.mark.asyncio
    async def test_list_user_all_ignored(self, plugin: MaibotAgentPlugin,
                                         fake_store: TaskStore,
                                         base_kwargs: dict[str, Any]) -> None:
        """user /maitask list -all → -all 被静默忽略，只看自己。"""
        await fake_store.save(make_task(task_id="tu", title="User任务",
                                        owner="qq:2", stream_id="qq:g:1"))
        await fake_store.save(make_task(task_id="tp", title="Planner任务",
                                        owner="planner:qq:10001", stream_id="qq:g:1"))
        # user 身份：base_kwargs 默认 user_id="2"
        kwargs = {**base_kwargs, "text": "/maitask list -all",
                  "stream_id": "qq:g:1", "group_id": "1"}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert ok, (ok, resp)
        assert "User任务" in resp, "应包含自己的任务"
        assert "Planner任务" not in resp, "不应包含 planner 任务"

    @pytest.mark.asyncio
    async def test_list_guest_all(self, plugin: MaibotAgentPlugin,
                                  fake_store: TaskStore,
                                  base_kwargs: dict[str, Any]) -> None:
        """guest /maitask list -all → -all 忽略，只按 owner 过滤。"""
        await fake_store.save(make_task(task_id="tg", title="Guest任务",
                                        owner="qq:99", stream_id="qq:g:1"))
        await fake_store.save(make_task(task_id="tp", title="Planner任务",
                                        owner="planner:qq:10001", stream_id="qq:g:1"))
        # guest 身份：user_id="99"，owner 解析为 qq:99
        kwargs = {"text": "/maitask list -all", "stream_id": "qq:g:1",
                  "platform": "qq", "user_id": "99", "group_id": "1",
                  "matched_groups": {}}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        # guest 只能看到自己的任务；-all 被静默忽略
        assert ok, (ok, resp)
        assert "Guest任务" in resp, "guest 应看到自己的任务"
        assert "Planner任务" not in resp, "guest 不应看到 planner 任务"

    @pytest.mark.asyncio
    async def test_list_invalid_status(self, plugin: MaibotAgentPlugin,
                                       base_kwargs: dict[str, Any]) -> None:
        """/maitask list nonsense → 无效状态错误。"""
        kwargs = {**base_kwargs, "text": "/maitask list nonsense"}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert not ok, (ok, resp)
        assert "无效状态" in resp

    @pytest.mark.asyncio
    async def test_list_all_after_status(self, plugin: MaibotAgentPlugin,
                                         fake_store: TaskStore,
                                         base_kwargs: dict[str, Any]) -> None:
        """/maitask list pending -all → -all 在 status 后也生效。"""
        await fake_store.save(make_task(task_id="ta", title="Admin待办",
                                        owner="qq:1", stream_id="qq:g:1",
                                        status=TaskStatus.PENDING))
        await fake_store.save(make_task(task_id="tb", title="User待办",
                                        owner="qq:2", stream_id="qq:g:1",
                                        status=TaskStatus.PENDING))
        await fake_store.save(make_task(task_id="tc", title="已完成",
                                        owner="qq:1", stream_id="qq:g:1",
                                        status=TaskStatus.COMPLETED))
        # admin + pending -all 应看到所有 pending 任务
        kwargs = {"text": "/maitask list pending -all", "stream_id": "qq:1",
                  "platform": "qq", "user_id": "1", "group_id": "",
                  "matched_groups": {}}
        ok, resp, pri = await plugin.cmd_task_list(**kwargs)
        assert ok, (ok, resp)
        assert "Admin待办" in resp, "应包含 admin 待办"
        assert "User待办" in resp, "应包含 user 待办"
        assert "已完成" not in resp, "不应包含已完成任务"


class TestCmdStatus:
    @pytest.mark.asyncio
    async def test_status_from_text(self, plugin: MaibotAgentPlugin,
                                    fake_store: TaskStore,
                                    base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask status t1"}
        ok, resp, pri = await plugin.cmd_task_status(**kwargs)
        assert ok and "t1" in resp, (ok, resp)

    @pytest.mark.asyncio
    async def test_status_from_matched_groups(self, plugin: MaibotAgentPlugin,
                                              fake_store: TaskStore,
                                              base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t2", title="测试2",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = dict(base_kwargs, text="/maitask status t2",
                      matched_groups={1: "t2"})
        ok, resp, pri = await plugin.cmd_task_status(**kwargs)
        assert ok and "t2" in resp, (ok, resp)

    @pytest.mark.asyncio
    async def test_status_missing_id(self, plugin: MaibotAgentPlugin,
                                     base_kwargs: dict[str, Any]) -> None:
        kwargs = {**base_kwargs, "text": "/maitask status"}
        ok, resp, pri = await plugin.cmd_task_status(**kwargs)
        assert not ok
        assert "用法" in resp

    @pytest.mark.asyncio
    async def test_status_sends_reply(self, plugin: MaibotAgentPlugin,
                                      fake_store: TaskStore,
                                      base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask status t1"}
        ok, resp, pri = await plugin.cmd_task_status(**kwargs)
        assert ok
        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent, "命令响应必须发送"
        assert sent[-1]["text"] == resp


class TestCmdCancel:
    @pytest.mark.asyncio
    async def test_cancel_from_text(self, plugin: MaibotAgentPlugin,
                                    fake_store: TaskStore,
                                    base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask cancel t1"}
        ok, resp, pri = await plugin.cmd_task_cancel(**kwargs)
        assert ok, (ok, resp)
        tm: FakeTaskManager = plugin._task_manager  # type: ignore[assignment]
        assert "t1" in tm.cancelled

    @pytest.mark.asyncio
    async def test_cancel_from_matched_groups(self, plugin: MaibotAgentPlugin,
                                              fake_store: TaskStore,
                                              base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t3", title="测试3",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = dict(base_kwargs, text="/maitask cancel t3",
                      matched_groups={1: "t3"})
        ok, resp, pri = await plugin.cmd_task_cancel(**kwargs)
        assert ok, (ok, resp)

    @pytest.mark.asyncio
    async def test_cancel_missing_id(self, plugin: MaibotAgentPlugin,
                                     base_kwargs: dict[str, Any]) -> None:
        kwargs = {**base_kwargs, "text": "/maitask cancel"}
        ok, resp, pri = await plugin.cmd_task_cancel(**kwargs)
        assert not ok
        assert "用法" in resp

    @pytest.mark.asyncio
    async def test_cancel_sends_reply(self, plugin: MaibotAgentPlugin,
                                      fake_store: TaskStore,
                                      base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask cancel t1"}
        ok, resp, pri = await plugin.cmd_task_cancel(**kwargs)
        assert ok
        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent, "命令响应必须发送"
        assert sent[-1]["text"] == resp


class TestCmdHistory:
    @pytest.mark.asyncio
    async def test_history_from_text(self, plugin: MaibotAgentPlugin,
                                     fake_store: TaskStore,
                                     base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask history t1"}
        ok, resp, pri = await plugin.cmd_task_history(**kwargs)
        assert ok, (ok, resp)

    @pytest.mark.asyncio
    async def test_history_from_matched_groups(self, plugin: MaibotAgentPlugin,
                                               fake_store: TaskStore,
                                               base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t4", title="测试4",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = dict(base_kwargs, text="/maitask history t4",
                      matched_groups={1: "t4"})
        ok, resp, pri = await plugin.cmd_task_history(**kwargs)
        assert ok, (ok, resp)

    @pytest.mark.asyncio
    async def test_history_missing_id(self, plugin: MaibotAgentPlugin,
                                      base_kwargs: dict[str, Any]) -> None:
        kwargs = {**base_kwargs, "text": "/maitask history"}
        ok, resp, pri = await plugin.cmd_task_history(**kwargs)
        assert not ok
        assert "用法" in resp

    @pytest.mark.asyncio
    async def test_history_sends_reply(self, plugin: MaibotAgentPlugin,
                                       fake_store: TaskStore,
                                       base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask history t1"}
        ok, resp, pri = await plugin.cmd_task_history(**kwargs)
        assert ok
        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent, "命令响应必须发送"
        assert sent[-1]["text"] == resp


class TestCmdAsk:
    @pytest.mark.asyncio
    async def test_ask_from_text(self, plugin: MaibotAgentPlugin,
                                 fake_store: TaskStore,
                                 base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask ask t1 换个思路"}
        ok, resp, pri = await plugin.cmd_task_ask(**kwargs)
        assert ok, (ok, resp)
        tm: FakeTaskManager = plugin._task_manager  # type: ignore[assignment]
        assert tm.modified and tm.modified[0]["task_id"] == "t1"
        assert tm.modified[0]["instruction"] == "换个思路"

    @pytest.mark.asyncio
    async def test_ask_from_matched_groups(self, plugin: MaibotAgentPlugin,
                                           fake_store: TaskStore,
                                           base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t5", title="测试5",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = dict(base_kwargs, text="/maitask ask t5 执行这个",
                      matched_groups={1: "t5", 2: "执行这个"})
        ok, resp, pri = await plugin.cmd_task_ask(**kwargs)
        assert ok, (ok, resp)
        tm: FakeTaskManager = plugin._task_manager  # type: ignore[assignment]
        assert tm.modified and tm.modified[0]["task_id"] == "t5"

    @pytest.mark.asyncio
    async def test_ask_missing_args(self, plugin: MaibotAgentPlugin,
                                    base_kwargs: dict[str, Any]) -> None:
        kwargs = {**base_kwargs, "text": "/maitask ask"}
        ok, resp, pri = await plugin.cmd_task_ask(**kwargs)
        assert not ok
        assert "用法" in resp

    @pytest.mark.asyncio
    async def test_ask_missing_instruction(self, plugin: MaibotAgentPlugin,
                                           base_kwargs: dict[str, Any]) -> None:
        kwargs = {**base_kwargs, "text": "/maitask ask t1"}
        ok, resp, pri = await plugin.cmd_task_ask(**kwargs)
        assert not ok
        assert "用法" in resp

    @pytest.mark.asyncio
    async def test_ask_sends_reply(self, plugin: MaibotAgentPlugin,
                                   fake_store: TaskStore,
                                   base_kwargs: dict[str, Any]) -> None:
        await fake_store.save(make_task(task_id="t1", title="测试",
                                        owner="qq:2", stream_id="qq:group:123"))
        kwargs = {**base_kwargs, "text": "/maitask ask t1 换个思路"}
        ok, resp, pri = await plugin.cmd_task_ask(**kwargs)
        assert ok
        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent, "命令响应必须发送"
        assert sent[-1]["text"] == resp


class TestCmdTextHelper:
    """直接测试 commands.py 的 cmd_text / cmd_arg 辅助函数。"""

    def test_cmd_text_prefers_text_key(self) -> None:
        result = cmd_text(text="/maitask list", plain_text="/ignored")
        assert result == "/maitask list"

    def test_cmd_text_falls_back_to_plain_text(self) -> None:
        result = cmd_text(plain_text="/maitask list")
        assert result == "/maitask list"

    def test_cmd_text_empty(self) -> None:
        result = cmd_text()
        assert result == ""

    def test_cmd_arg_extracts_group(self) -> None:
        result = cmd_arg({"matched_groups": {1: "t123"}}, 1)
        assert result == "t123"

    def test_cmd_arg_missing_group(self) -> None:
        result = cmd_arg({"matched_groups": {}}, 1, default="fallback")
        assert result == "fallback"

    def test_cmd_arg_no_matched_groups(self) -> None:
        result = cmd_arg({}, 1)
        assert result == ""


class TestCmdFallback:
    """兜底命令：拦截所有未匹配的 /maitask 输入，显示帮助并发送，避免落入 planner。"""

    @pytest.mark.asyncio
    async def test_fallback_unknown_subcommand(self, plugin: MaibotAgentPlugin,
                                               base_kwargs: dict[str, Any]) -> None:
        """/maitask xxx（未知子命令）应显示帮助并发送。"""
        kwargs = {**base_kwargs, "text": "/maitask xxx"}
        ok, resp, pri = await plugin.cmd_zz_task_fallback(**kwargs)
        assert ok, (ok, resp)
        assert "用法" in resp
        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent, "兜底命令必须发送帮助"
        assert sent[-1]["text"] == resp
        assert sent[-1]["stream_id"] == base_kwargs["stream_id"]

    @pytest.mark.asyncio
    async def test_fallback_bare_task(self, plugin: MaibotAgentPlugin,
                                      base_kwargs: dict[str, Any]) -> None:
        """/maitask（裸命令）应显示帮助。"""
        kwargs = {**base_kwargs, "text": "/maitask"}
        ok, resp, pri = await plugin.cmd_zz_task_fallback(**kwargs)
        assert ok, (ok, resp)
        assert "用法" in resp

    @pytest.mark.asyncio
    async def test_fallback_help(self, plugin: MaibotAgentPlugin,
                                 base_kwargs: dict[str, Any]) -> None:
        """/maitask help 应显示帮助。"""
        kwargs = {**base_kwargs, "text": "/maitask help"}
        ok, resp, pri = await plugin.cmd_zz_task_fallback(**kwargs)
        assert ok, (ok, resp)
        assert "用法" in resp


class TestToolTaskCreateReplyStreamId:
    """@Tool task_create 处理器应提取并透传 reply_stream_id。"""

    @pytest.mark.asyncio
    async def test_tool_task_create_passes_reply_stream_id(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """传入 reply_stream_id kwarg 时，_tool_task_create 将其转发给 create_task。"""
        result = await plugin._tool_task_create(
            intent="跨流回复任务",
            stream_id="qq:g:1",
            reply_stream_id="qq:g:2",
        )
        assert result["success"] is True
        tm: FakeTaskManager = plugin._task_manager  # type: ignore[assignment]
        assert tm.created, "create_task should have been called"
        assert tm.created[0]["reply_stream_id"] == "qq:g:2"

    @pytest.mark.asyncio
    async def test_tool_task_create_reply_stream_id_absent(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """未传入 reply_stream_id kwarg 时，create_task 收到 None。"""
        result = await plugin._tool_task_create(
            intent="默认回复流任务",
            stream_id="qq:g:1",
        )
        assert result["success"] is True
        tm: FakeTaskManager = plugin._task_manager  # type: ignore[assignment]
        assert tm.created and tm.created[0]["reply_stream_id"] is None


class TestCmdReplyUnifiedSend:
    @pytest.mark.asyncio
    async def test_cmd_reply_sends_raw_without_polish(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """命令响应经直发出口（ReplySender.send_raw）发送，不做润色。"""
        from oh_mai_agent import commands as commands_module

        await commands_module.cmd_reply(plugin, "qq:group:123", "任务已创建 ID=t1")

        sent = plugin._mock_ctx._sent_messages  # type: ignore[attr-defined]
        assert sent and sent[0]["text"] == "任务已创建 ID=t1"
        assert sent[0]["stream_id"] == "qq:group:123"

    @pytest.mark.asyncio
    async def test_cmd_reply_send_failure_swallowed(
        self, plugin: MaibotAgentPlugin,
    ) -> None:
        """命令响应发送失败时只打 warning，不向上抛。"""
        from unittest.mock import AsyncMock, patch

        from oh_mai_agent import commands as commands_module

        with patch.object(
            plugin._task_manager.sender, "send_raw",
            AsyncMock(side_effect=RuntimeError("send failed")),
        ):
            await commands_module.cmd_reply(plugin, "qq:group:123", "响应")  # 不应抛异常


# ══════════════════════════════════════════════════════════════════════════════
# 命令失败路径（底层 TaskManager 返回失败时的回复与返回码）
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdFailurePaths:
    @pytest.mark.asyncio
    async def test_create_failure_replies_error(
        self, plugin: MaibotAgentPlugin, base_kwargs: dict[str, Any],
    ) -> None:
        plugin._task_manager.create_task = AsyncMock(return_value=(False, "boom"))  # type: ignore[method-assign]
        ok, reply, code = await cmd_create(plugin, **base_kwargs)
        assert ok is False
        assert "创建失败: boom" in reply
        assert code == 2

    @pytest.mark.asyncio
    async def test_status_failure_replies_error(
        self, plugin: MaibotAgentPlugin, base_kwargs: dict[str, Any],
    ) -> None:
        base_kwargs = {**base_kwargs, "text": "/maitask status task-1"}
        plugin._task_manager.get_task = AsyncMock(return_value=(False, "not found"))  # type: ignore[method-assign]
        ok, reply, _ = await cmd_status(plugin, **base_kwargs)
        assert ok is False
        assert "查询失败: not found" in reply

    @pytest.mark.asyncio
    async def test_cancel_failure_replies_error(
        self, plugin: MaibotAgentPlugin, base_kwargs: dict[str, Any],
    ) -> None:
        base_kwargs = {**base_kwargs, "text": "/maitask cancel task-1"}
        plugin._task_manager.cancel_task = AsyncMock(return_value=(False, "boom"))  # type: ignore[method-assign]
        ok, reply, _ = await cmd_cancel(plugin, **base_kwargs)
        assert ok is False
        assert "取消失败: boom" in reply

    @pytest.mark.asyncio
    async def test_history_failure_replies_error(
        self, plugin: MaibotAgentPlugin, base_kwargs: dict[str, Any],
    ) -> None:
        base_kwargs = {**base_kwargs, "text": "/maitask history task-1"}
        plugin._task_manager.task_history = AsyncMock(return_value=(False, "boom"))  # type: ignore[method-assign]
        ok, reply, _ = await cmd_history(plugin, **base_kwargs)
        assert ok is False
        assert "查询历史失败: boom" in reply

    @pytest.mark.asyncio
    async def test_history_empty_replies_no_records(
        self, plugin: MaibotAgentPlugin, base_kwargs: dict[str, Any],
    ) -> None:
        base_kwargs = {**base_kwargs, "text": "/maitask history task-1"}
        plugin._task_manager.task_history = AsyncMock(return_value=(True, []))  # type: ignore[method-assign]
        ok, reply, _ = await cmd_history(plugin, **base_kwargs)
        assert ok is True
        assert "该任务暂无历史记录。" in reply

    @pytest.mark.asyncio
    async def test_ask_failure_replies_error(
        self, plugin: MaibotAgentPlugin, base_kwargs: dict[str, Any],
    ) -> None:
        base_kwargs = {**base_kwargs, "text": "/maitask ask task-1 继续"}
        plugin._task_manager.modify_task = AsyncMock(return_value=(False, "boom"))  # type: ignore[method-assign]
        ok, reply, _ = await cmd_ask(plugin, **base_kwargs)
        assert ok is False
        assert "注入失败: boom" in reply

    @pytest.mark.asyncio
    async def test_create_infers_platform_from_stream_id(
        self, plugin: MaibotAgentPlugin, base_kwargs: dict[str, Any],
    ) -> None:
        """platform 缺省时从 stream_id 推断（覆盖 resolve_caller 推断分支）。"""
        kwargs = {**base_kwargs, "platform": ""}
        ok, _, _ = await cmd_create(plugin, **kwargs)
        assert ok is True
        assert plugin._task_manager.created[-1]["platform"] == "qq"
