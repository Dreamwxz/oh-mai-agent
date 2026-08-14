"""oh-mai-agent pytest 套件的共享 fixture。

提供：
  - 导入路径设置（将 oh-mai-agent 目录插入 sys.path）
  - 经 ``real_store`` fixture 提供 TaskStore（真实 SQLite）
  - MockCtx（供 task_manager / agent_loop 测试使用的 mock PluginContext）
  - MockLLM（供 agent_loop 测试使用的 mock LLM）
  - ToolRegistry 构造辅助
- 创建 TaskRecord 的 ``make_task`` 工厂
"""

from __future__ import annotations

import sys
import types
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

# ── 导入路径：将插件根目录注入为包 'oh_mai_agent' ───────────────────────────
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT))

# 将插件根目录挂载为包 'oh_mai_agent'，使模块文件内的相对导入
# （from .config / from .tools.registry）能够正常解析。
_pkg = types.ModuleType("oh_mai_agent")
_pkg.__path__ = [str(_PLUGIN_ROOT)]
sys.modules["oh_mai_agent"] = _pkg

from oh_mai_agent.config import (
    ApiExposeConfig,
    MaibotAgentConfig,
    MCPConfig,
    PermissionConfig,
    PlannerBoardConfig,
    PolishConfig,
    SearchConfig,
    TaskConfig,
)
from oh_mai_agent.permission import PermissionResolver, Role
from oh_mai_agent.domain.status_formatter import StatusFormatter
from oh_mai_agent.domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType
from oh_mai_agent.domain.task_store import TaskStore
from oh_mai_agent.prompt.manager import PromptManager
from oh_mai_agent.prompt.service import PromptService
from oh_mai_agent.prompt.builders import ALL_BUILDERS
from oh_mai_agent.tools.registry import ToolDefinition, ToolRegistry
from oh_mai_agent.bus.command_bus import TaskCommandBus


# ═══════════════════════════════════════════════════════════════════════════════
# MockLLM —— 供 agent_loop 测试使用的假 LLM
# ═══════════════════════════════════════════════════════════════════════════════

class MockLLM:
    """用于测试 agent 循环的 mock LLM。

    支持带可配置响应的 ``generate_with_tools`` 与 ``generate``。
    """

    def __init__(self) -> None:
        self._tool_responses: list[dict] = []
        self._generate_responses: list[dict] = []
        self.call_history: list[dict] = []

    def set_tool_response(self, response: str, tool_calls: list[dict] | None = None) -> None:
        """为下一次 generate_with_tools 调用排队一条响应。"""
        self._tool_responses.append({
            "success": True,
            "response": response,
            "tool_calls": tool_calls or [],
        })

    def set_generate_response(self, response: str) -> None:
        """为下一次 generate 调用排队一条响应。"""
        self._generate_responses.append({"success": True, "response": response})

    async def generate_with_tools(self, prompt: list, tools: list, model: str = "", **kwargs: Any) -> dict:
        self.call_history.append({"type": "generate_with_tools", "prompt": prompt, "tools": tools, "model": model, **kwargs})
        if self._tool_responses:
            return self._tool_responses.pop(0)
        return {"success": True, "response": "done", "tool_calls": []}

    async def generate(self, prompt: Any, model: str = "", **kwargs: Any) -> dict:
        self.call_history.append({"type": "generate", "prompt": prompt, "model": model, **kwargs})
        if self._generate_responses:
            return self._generate_responses.pop(0)
        return {"success": True, "response": "ok"}


# ═══════════════════════════════════════════════════════════════════════════════
# MockLogger —— 供测试使用的假 logger
# ═══════════════════════════════════════════════════════════════════════════════

class MockLogger:
    """假 logger：方法均为空实现，吞掉日志调用，避免测试时输出日志。"""

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# MockCtx —— 模拟 MaiBot PluginContext
# ═══════════════════════════════════════════════════════════════════════════════

class MockCtx:
    """供需要 ctx.llm / ctx.config / ctx.send / ctx.message 的测试使用的 mock PluginContext。"""

    def __init__(self) -> None:
        self.llm: MockLLM = MockLLM()
        self.logger: MockLogger = MockLogger()
        self._sent_messages: list[dict] = []
        self._config_values: dict[str, Any] = {}
        self._db_data: dict[str, list[dict]] = {}
        self._message_data: dict[str, list[dict]] = {}
        self._chat_streams: list[dict] = []
        self._chat: "MockCtx._Chat | None" = None
        self._person: "MockCtx._Person | None" = None
        self._person_data: dict[str, Any | None] = {}  # person_name → person_id（str | dict | None）
        self._capability_responses: dict[str, Any] = {}  # "capability:query_key" → 结果
        self.api: Any = None  # 供 plugin_api_tools 测试在外部设置
        self.maisaka = self._Maisaka()

    # 发送（send）mock
    class _SendText:
        def __init__(self, ctx: "MockCtx") -> None:
            self._ctx = ctx

        async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
            self._ctx._sent_messages.append({"text": text, "stream_id": stream_id})
            return True

    @property
    def send(self) -> "_SendText":
        return self._SendText(self)

    # 配置（config）mock
    class _Config:
        def __init__(self, ctx: "MockCtx") -> None:
            self._ctx = ctx

        async def get(self, key: str, default: Any = None) -> Any:
            return self._ctx._config_values.get(key, default)

    @property
    def config(self) -> "_Config":
        return self._Config(self)

    # 数据库（db）mock
    class _DB:
        def __init__(self, ctx: "MockCtx") -> None:
            self._ctx = ctx

        async def query(self, table: str, filters: dict | None = None) -> list[dict]:
            records = self._ctx._db_data.get(table, [])
            if filters:
                return [r for r in records if all(r.get(k) == v for k, v in filters.items())]
            return list(records)

    @property
    def db(self) -> "_DB":
        return self._DB(self)

    # 消息（message）mock
    class _Message:
        def __init__(self, ctx: "MockCtx") -> None:
            self._ctx = ctx

        async def get_recent(self, chat_id: str, limit: int = 50) -> list[dict]:
            msgs = self._ctx._message_data.get(chat_id, [])
            return msgs[-limit:]

    @property
    def message(self) -> "_Message":
        return self._Message(self)

    # 聊天（chat）mock
    class _Chat:
        def __init__(self, ctx: "MockCtx") -> None:
            self._ctx = ctx
            self._open_session_calls: list[dict] = []
            self._stream_lookup_calls: list[dict] = []

        async def get_all_streams(self, platform: str = "qq") -> list[dict]:
            return self._ctx._chat_streams

        async def open_session(
            self, platform: str, chat_type: str,
            *, user_id: str = "", group_id: str = "", **kwargs: Any,
        ) -> dict:
            self._open_session_calls.append({
                "platform": platform, "chat_type": chat_type,
                "user_id": user_id, "group_id": group_id,
                **kwargs,
            })
            stream_id = f"{platform}:{'g' if chat_type == 'group' else ''}:{group_id or user_id}"
            return {
                "success": True, "stream_id": stream_id,
                "session_id": stream_id, "created": True,
                "chat_type": chat_type,
            }

        async def get_stream_by_user_id(self, user_id: str, platform: str = "qq", **kwargs: Any) -> dict | None:
            self._stream_lookup_calls.append({"method": "get_stream_by_user_id", "user_id": user_id, "platform": platform, **kwargs})
            return None

        async def get_stream_by_group_id(self, group_id: str, platform: str = "qq", **kwargs: Any) -> dict | None:
            self._stream_lookup_calls.append({"method": "get_stream_by_group_id", "group_id": group_id, "platform": platform, **kwargs})
            return None

    @property
    def chat(self) -> "_Chat":
        if self._chat is None:
            self._chat = self._Chat(self)
        return self._chat

    # ── 人员（person）mock ──────────────────────────────────────────────

    class _Person:
        def __init__(self, ctx: "MockCtx") -> None:
            self._ctx = ctx

        async def get_id_by_name(self, person_name: str) -> str | dict | None:
            """在 _person_data 字典中按 person_name 查找。

            返回：
                str 类型的 person_id、dict {"person_id": ...}，未找到时返回 None。
            """
            return self._ctx._person_data.get(person_name)

    @property
    def person(self) -> "_Person":
        if self._person is None:
            self._person = self._Person(self)
        return self._person

    # ── call_capability mock ────────────────────────────────────────────

    async def call_capability(self, capability: str, **kwargs: Any) -> Any:
        """模拟 MaiBot SDK 的 call_capability。

        对 "knowledge.search"：按 query 键在 _capability_responses 中查找结果。
        其余 capability 一律返回 {"success": True}。
        """
        if capability == "knowledge.search":
            query = str(kwargs.get("query", ""))
            result = self._capability_responses.get(query)
            if result is not None:
                if callable(result):
                    return result()
                return result
            # 默认：返回桩内容
            return {"success": True, "content": f"stub knowledge for '{query}'"}
        return {"success": True}

    # ── Maisaka 上下文 mock ─────────────────────────────────────────────

    class _Maisaka:
        class _Context:
            def __init__(self, parent: "_Maisaka") -> None:
                self._parent = parent

            async def append(
                self, stream_id: str, segments: list,
                *, visible_text: str = "", source_kind: str = "",
                message_id: str = "", **kwargs,
            ) -> dict:
                record: dict = {
                    "stream_id": stream_id,
                    "segments": segments,
                    "visible_text": visible_text,
                    "source_kind": source_kind,
                    "message_id": message_id,
                    **kwargs,
                }
                self._parent.appends.append(record)
                idx = len(self._parent.appends) - 1
                return {
                    "success": True,
                    "index": idx,
                    "stream_id": stream_id,
                    "visible_text": visible_text,
                    "source_kind": source_kind,
                }

        def __init__(self) -> None:
            self.appends: list[dict] = []
            self.context = self._Context(self)

    def add_message(self, chat_id: str, content: str, is_bot: bool = False) -> None:
        self._message_data.setdefault(chat_id, []).append({
            "content": content, "is_bot": is_bot,
        })

    def add_db_record(self, table: str, record: dict) -> None:
        self._db_data.setdefault(table, []).append(record)


# ═══════════════════════════════════════════════════════════════════════════════
# 简单辅助工厂
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def default_config() -> MaibotAgentConfig:
    return MaibotAgentConfig()


@pytest.fixture
def default_resolver(default_config: MaibotAgentConfig) -> PermissionResolver:
    return PermissionResolver(default_config.permission)


@pytest.fixture
def real_store(tmp_path: Any) -> TaskStore:
    """基于临时文件的真实 SQLite TaskStore，用于集成测试。

    返回未初始化的 store；测试需自行调用 ``await store.init()``。
    db 文件会在 tmp_path 拆除时自动清理。
    （项目约定：不 mock 持久化，统一使用真实 sqlite。）
    """
    return TaskStore(str(tmp_path / "test.db"))


@pytest.fixture
def mock_ctx() -> MockCtx:
    return MockCtx()


@pytest.fixture
def command_bus() -> TaskCommandBus:
    return TaskCommandBus()


@pytest.fixture
def pm() -> PromptManager:
    """从项目模板目录加载的 PromptManager。"""
    return PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates")


@pytest.fixture
def prompt_service(pm: PromptManager) -> PromptService:
    """注入了 pm 与 ALL_BUILDERS 的 PromptService。"""
    return PromptService(manager=pm, builders=ALL_BUILDERS)


@pytest.fixture
def tool_registry() -> ToolRegistry:
    return ToolRegistry()


def make_task(
    task_id: str = "task-001",
    title: str = "测试任务",
    intent: str = "测试意图",
    level: TaskLevel = TaskLevel.AGENT,
    owner: str = "qq:10001",
    stream_id: str = "qq:10001",
    platform: str = "qq",
    status: TaskStatus = TaskStatus.PENDING,
    trigger_type: TriggerType = TriggerType.NOW,
    **kwargs: Any,
) -> TaskRecord:
    """创建测试用 TaskRecord 实例的便捷工厂。"""
    return TaskRecord(
        id=task_id,
        title=title,
        intent=intent,
        level=level,
        owner=owner,
        stream_id=stream_id,
        platform=platform,
        status=status,
        trigger_type=trigger_type,
        **kwargs,
    )


class FakeTaskManager:
    """TaskManager 的内存 fake：记录调用、可注入失败。

    覆盖 planner / api_expose / plugin 层 handler 所需的 TaskManager 接口：
    ``create_task / list_tasks / get_task / modify_task / cancel_task / task_history``。

    - 每次调用按方法名记入 ``self.calls``（kwargs 列表），便于断言透传参数。
    - 将方法名加入 ``self.fail`` 集合后，对应方法返回固定失败结果。
    - 传入真实 ``store`` 时，list / get 基于真实 sqlite 数据（不 mock 持久化）；
      未传入时 list 返回空列表、get 返回不存在。
    """

    def __init__(self, store: TaskStore | None = None) -> None:
        self._store = store
        self.calls: dict[str, list[dict[str, Any]]] = {}
        self.fail: set[str] = set()
        self._sfmt = StatusFormatter()

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.setdefault(name, []).append(kwargs)

    async def create_task(self, **kwargs: Any) -> tuple[bool, TaskRecord | str]:
        self._record("create_task", **kwargs)
        if "create_task" in self.fail:
            return False, "create failed"
        return True, make_task(
            task_id="t-created",
            intent=str(kwargs.get("intent", "")),
            level=kwargs.get("level") or TaskLevel.AGENT,
            owner=str(kwargs.get("owner", "")),
            stream_id=str(kwargs.get("stream_id", "")),
            platform=str(kwargs.get("platform", "")),
        )

    async def list_tasks(self, **kwargs: Any) -> list[dict]:
        self._record("list_tasks", **kwargs)
        if "list_tasks" in self.fail or self._store is None:
            return []
        tasks = await self._store.list(
            owner=kwargs.get("owner") or None,
            status=kwargs.get("status"),
            stream_id=kwargs.get("stream_id"),
            limit=kwargs.get("limit", 50),
        )
        return [
            {
                "id": t.id,
                "title": t.title,
                "level": t.level.value,
                "status": t.status.value,
                "format_status": self._sfmt.format(*t.status_info()),
                "owner": t.owner,
            }
            for t in tasks
        ]

    async def get_task(self, task_id: str, **kwargs: Any) -> tuple[bool, TaskRecord | str]:
        self._record("get_task", task_id=task_id, **kwargs)
        if "get_task" in self.fail:
            return False, "not found"
        if self._store is not None:
            t = await self._store.get(task_id)
            return (True, t) if t else (False, "not found")
        return True, make_task(task_id=task_id)

    async def modify_task(self, task_id: str, **kwargs: Any) -> tuple[bool, str]:
        self._record("modify_task", task_id=task_id, **kwargs)
        if "modify_task" in self.fail:
            return False, "modify failed"
        return True, "已注入"

    async def cancel_task(self, task_id: str, **kwargs: Any) -> tuple[bool, str]:
        self._record("cancel_task", task_id=task_id, **kwargs)
        if "cancel_task" in self.fail:
            return False, "cancel failed"
        return True, "已取消"

    async def task_history(self, task_id: str, **kwargs: Any) -> tuple[bool, list | str]:
        self._record("task_history", task_id=task_id, **kwargs)
        if "task_history" in self.fail:
            return False, "history failed"
        return True, [{"type": "status_change", "timestamp": "2025-01-01T00:00:00+00:00"}]
