"""MaiBot Agent 任务管理器 — 任务生命周期编排层。

连接权限判定、调度器、Agent 循环、工具注册，提供任务创建、
查询、修改（注入指令）、删除、历史等完整操作。

TaskManager 核心 —— commands.py / plugin.py / api_expose.py 均依赖本模块。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..executor.context import current_task, make_role_provider
from ..executor.instant import ReplySender
from ..config import MaibotAgentConfig
from ..executor import ExecutionContext, ExecutorFactory, make_exec_ctx
from ..domain.status_formatter import StatusFormatter
from ..permission import PermissionResolver, Role
from ..prompt.manager import PromptManager
from .scheduler import TaskScheduler
from .usecases.task_crud import TaskCrud
from .usecases.task_control import TaskControl
from ..domain.task_record import TaskLevel, TaskRecord, TaskStatus, TriggerType
from ..domain.task_store import TaskStore
from ..tools.agent.ask_tool import build_ask_tool
from ..tools.agent.file_tools import build_file_tools
from ..tools.agent.info_tools import build_info_tools
from ..tools.agent.plugin_api_tools import refresh_plugin_api_tools
from ..tools.agent.shell_tools import build_shell_tools
from ..tools.send_message import build_send_tool
from ..tools.agent.subagent_tool import build_subagent_tool, build_subagents_tool
from ..tools.registry import ToolDefinition, ToolRegistry

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# TaskManager — 任务管理器
# ═══════════════════════════════════════════════════════════════════════


class TaskManager:
    """任务生命周期编排层。

    连接权限判定、调度器、Agent 循环、工具注册，提供任务创建、
    查询、修改（注入指令）、删除、历史等完整操作，是任务管理核心模块，
    commands.py / plugin.py / api_expose.py 均依赖本模块。

    用法::

        tm = TaskManager(
            ctx=plugin_ctx, store=store, scheduler=sched,
            registry=reg, resolver=resolver, config=cfg,
        )
        await tm.setup()
        ok, task = await tm.create_task(
            intent="整理聊天记录", owner="qq:1", platform="qq",
            stream_id="qq:g:1", caller_role=Role.ADMIN,
        )
    """

    def __init__(
        self,
        *,
        ctx: Any,
        store: TaskStore,
        scheduler: TaskScheduler,
        registry: ToolRegistry,
        resolver: PermissionResolver,
        config: MaibotAgentConfig,
        llm_title: Callable[[str], Awaitable[str]] | None = None,
        data_dir: str | Path = "data",
        prompt_manager: PromptManager | None = None,
        prompt_service: Any | None = None,
        command_bus: Any,
        sender: Any = None,
    ) -> None:
        """初始化任务管理器。

        Args:
            ctx: SDK PluginContext（用于 LLM 调用、发消息等）。
            store: 任务持久化存储。
            scheduler: 调度器（enqueue/cancel/pause/resume）。
            registry: 工具注册中心。
            resolver: 权限判定。
            config: 完整插件配置。
            llm_title: 标题生成回调。
            data_dir: 插件数据目录。
            prompt_manager: PromptManager 实例。
            prompt_service: PromptService 实例。
            command_bus: TaskCommandBus 实例。
            sender: ReplySender 实例（统一发送出口）。
        """
        self._ctx = ctx
        self._store = store
        self._scheduler = scheduler
        self._registry = registry
        self._resolver = resolver
        self._config = config
        self._sfmt = StatusFormatter()
        self._llm_title = llm_title
        self._data_dir = Path(data_dir)
        self._prompt_manager = prompt_manager
        self._prompt_service = prompt_service
        self._command_bus = command_bus
        if sender is None:
            # 缺省自举：用任务管理器持有的 ctx/config 构造标准发送器
            # （config_getter 每次读取 self._config，热更新后自动生效）
            sender = ReplySender(
                ctx=ctx,
                config_getter=lambda: self._config,
                prompt_service=prompt_service,
            )
        self._sender = sender
        self._crud = TaskCrud(
            store=store,
            scheduler=scheduler,
            resolver=resolver,
            sfmt=self._sfmt,
            llm_title=llm_title,
            config=config,
            inject_instruction=self.handle_injection,
        )

        # 执行器工厂 — instant/agent 执行器遵循统一协议
        self._executor_factory = ExecutorFactory(
            registry=registry,
            on_ask=self._ask_callback,
            send_final=self._dispatch_reply_instant,
            prompt_manager=prompt_manager,
            command_bus=command_bus,
            resolver=self._resolver,
        )
        self._control = TaskControl(
            store=store,
            scheduler=scheduler,
            command_bus=command_bus,
            executor_factory=self._executor_factory,
            config=config,
            prompt_manager=prompt_manager,
            prompt_service=prompt_service,
            ctx=ctx,
            sender=sender,
        )

    @property
    def sender(self) -> Any:
        """统一发送出口（ReplySender），供命令层 / Planner 工具使用。"""
        return self._sender

    def _make_exec_ctx(self) -> ExecutionContext:
        """用当前 TaskManager 状态构造 ExecutionContext。"""
        return make_exec_ctx(
            ctx=self._ctx,
            store=self._store,
            scheduler=self._scheduler,
            config=self._config,
            prompt_manager=self._prompt_manager,
            prompt_service=self._prompt_service,
            sender=self._sender,
        )

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def setup(self) -> None:
        """初始化任务管理器。

        1. 注册任务管理工具（list_my_tasks / create_subtask / inject_task）。
        2. 注册信息工具、文件工具、ask_user 工具、跨插件 API 工具。
        3. 初始化 AgentLoop 工厂并绑定到调度器。
        """
        # ── 1. 任务管理工具 ──────────────────────────────────────────
        for tool in self._build_task_mgmt_tools():
            self._registry.register(tool)

        # ── 2. 信息工具 ──────────────────────────────────────────────
        for tool in build_info_tools(self._ctx, search_max_results=self._config.search.max_results):
            self._registry.register(tool)

        # ── 3. 文件工具 ──────────────────────────────────────────────
        user_workspace = self._data_dir / "files"
        # role_provider 在 AgentLoop 构造时按任务绑定；此处注册阶段仅定义工具
        file_tools = build_file_tools(
            self._ctx,
            user_workspace=user_workspace,
            admin_open=True,
            role_provider=lambda: self._current_task_role(),
        )
        for tool in file_tools:
            self._registry.register(tool)

        # ── 4. ask_user 工具 ─────────────────────────────────────────
        # on_ask 回调与 AgentLoop 共用 self._ask_callback
        # 此处注册的 ask_user 工具 handler 由 ask_tool 内部创建，
        # 实际挂起由 AgentLoop._handle_ask_user 处理。
        # 但 ask_tool 的 handler 调用 ask_callback(stream_id, question)，
        # 这与 AgentLoop 的 on_ask 一致 —— 两者都指向 _ask_callback。
        # 说明: 两个 ask_user 路径（AgentLoop 内置 + 注册到 registry）
        # 都通过同一个 _ask_callback 发消息；挂起由 AgentLoop 内部处理。
        ask_tools = build_ask_tool(
            self._ctx,
            ask_callback=self._ask_callback,
            min_role=Role.USER,
        )
        for tool in ask_tools:
            self._registry.register(tool)

        # ── 5. send_message 工具（升级版） ──────────────────────────
        # send_polished 回调绑定 ReplySender.send_polished（润色 + 分割 + 重试），
        # relay_from（转达委托人）由 send_message 工具参数透传
        async def _send_polished(
            text: str, stream_id: str, *, relay_from: str | None = None,
        ) -> None:
            await self._sender.send_polished(text, stream_id, relay_from=relay_from)

        send_msg_tool = build_send_tool(
            self._ctx,
            send_polished=_send_polished,
            min_role=Role.USER,
            prompt_service=self._prompt_service,
        )
        self._registry.register(send_msg_tool)

        # ── 6. 跨插件 API 工具 ──────────────────────────────────────
        try:
            ctx_api = getattr(self._ctx, "api", None)
            if ctx_api is not None:
                api_tools = await refresh_plugin_api_tools(ctx_api)
                for tool in api_tools:
                    self._registry.register(tool)
        except Exception:
            logger.warning("扫描插件 API 工具失败", exc_info=True)

        # ── 7. 子 Agent 工具 ─────────────────────────────────────────
        # config_getter 为 lambda 读取当前 self._config.subagent 引用，
        # update_config 热更新后新值立即生效（无需重新注册）。
        if self._config.subagent.enabled:
            for tool in (
                build_subagent_tool(
                    self._ctx,
                    self._registry,
                    self._prompt_service,
                    config_getter=lambda: self._config.subagent,
                    role_provider=lambda: self._current_task_role(),
                ),
                build_subagents_tool(
                    self._ctx,
                    self._registry,
                    self._prompt_service,
                    config_getter=lambda: self._config.subagent,
                    role_provider=lambda: self._current_task_role(),
                ),
            ):
                self._registry.register(tool)

        # ── 8. 命令执行工具 ─────────────────────────────────────────
        # config_getter 每次调用读取，timeout / 输出上限热更新立即生效；
        # 工具仅 admin 可见（min_role + role_provider 双重门控）
        if self._config.shell.enabled:
            for tool in build_shell_tools(
                self._ctx,
                config_getter=lambda: self._config.shell,
                role_provider=lambda: self._current_task_role(),
            ):
                self._registry.register(tool)

        logger.info(
            "TaskManager 初始化完成，已注册 %d 个工具",
            len(self._registry.all_names()),
        )

    # ── 配置热更新 ──────────────────────────────────────────────────

    def update_config(self, config: MaibotAgentConfig) -> None:
        """热更新 TaskManager 配置引用。

         更新内部 _config 引用，后续 instant 润色、agent 循环创建
        等均使用新配置。
        """
        self._config = config
        self._crud.update_config(config)
        self._control.update_config(config)
        logger.info(
            "TaskManager 配置已热更新 (task=%r, polish=%r, mcp=%r)",
            config.task,
            config.polish,
            config.mcp,
        )

    def update_resolver(self, resolver: PermissionResolver) -> None:
        """Replace the permission resolver used by task orchestration."""
        self._resolver = resolver
        self._crud._resolver = resolver
        agent_executor = self._executor_factory.get(TaskLevel.AGENT)
        agent_executor._resolver = resolver

    # ── 创建 ──────────────────────────────────────────────────────────

    async def create_task(
        self,
        *,
        intent: str,
        owner: str,
        platform: str,
        stream_id: str,
        level: TaskLevel | None = None,
        trigger: TriggerType = TriggerType.NOW,
        delay_seconds: int | None = None,
        cron_expr: str | None = None,
        priority: int = 0,
        reply_stream_id: str | None = None,
        caller_role: Role,
    ) -> tuple[bool, TaskRecord | str]:
        return await self._crud.create_task(
            intent=intent, owner=owner, platform=platform, stream_id=stream_id,
            level=level, trigger=trigger, delay_seconds=delay_seconds,
            cron_expr=cron_expr, priority=priority, reply_stream_id=reply_stream_id,
            caller_role=caller_role,
        )

    # ── 查询 ──────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        *,
        caller_role: Role,
        owner: str,
        status: TaskStatus | None = None,
        stream_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        return await self._crud.list_tasks(
            caller_role=caller_role, owner=owner, status=status,
            stream_id=stream_id, limit=limit,
        )

    async def _resolve_task_by_id(self, task_id: str) -> tuple[bool, TaskRecord | str]:
        return await self._crud._resolve_task_by_id(task_id)

    async def get_task(
        self,
        task_id: str,
        *,
        caller_role: Role,
        owner: str,
    ) -> tuple[bool, TaskRecord | str]:
        return await self._crud.get_task(task_id, caller_role=caller_role, owner=owner)

    # ── 修改 ──────────────────────────────────────────────────────────

    async def modify_task(
        self,
        task_id: str,
        *,
        caller_role: Role,
        owner: str,
        new_intent: str | None = None,
        inject_instruction: str | None = None,
        priority: int | None = None,
    ) -> tuple[bool, str]:
        return await self._crud.modify_task(
            task_id, caller_role=caller_role, owner=owner,
            new_intent=new_intent, inject_instruction=inject_instruction,
            priority=priority,
        )

    # ── 控制 ──────────────────────────────────────────────────────────

    async def cancel_task(
        self,
        task_id: str,
        *,
        caller_role: Role,
        owner: str,
    ) -> tuple[bool, str]:
        return await self._crud.cancel_task(task_id, caller_role=caller_role, owner=owner)

    async def pause_task(
        self,
        task_id: str,
        *,
        caller_role: Role,
        owner: str,
    ) -> tuple[bool, str]:
        return await self._crud.pause_task(task_id, caller_role=caller_role, owner=owner)

    async def resume_task(
        self,
        task_id: str,
        *,
        caller_role: Role,
        owner: str,
    ) -> tuple[bool, str]:
        return await self._crud.resume_task(task_id, caller_role=caller_role, owner=owner)

    # ── instant / agent 执行 ─────────────────────────────────────────

    async def _dispatch_reply_instant(self, task: TaskRecord, text: str) -> None:
        return await self._control._dispatch_reply_instant(task, text)

    async def execute_instant(self, task: TaskRecord) -> None:
        return await self._control.execute_instant(task)

    # ── 历史 ──────────────────────────────────────────────────────────

    async def task_history(
        self,
        task_id: str,
        *,
        caller_role: Role,
        owner: str,
        limit: int = 50,
    ) -> tuple[bool, list | str]:
        return await self._crud.task_history(
            task_id, caller_role=caller_role, owner=owner, limit=limit,
        )

    # ── 用户回复匹配 ─────────────────────────────────────────────────

    async def handle_user_reply(
        self,
        *,
        stream_id: str,
        user_id: str,
        reply: str,
    ) -> None:
        return await self._control.handle_user_reply(
            stream_id=stream_id,
            user_id=user_id,
            reply=reply,
        )

    # ── 指令注入转发 ─────────────────────────────────────────────────

    async def handle_injection(self, task_id: str, instruction: str) -> bool:
        return await self._control.handle_injection(task_id, instruction)

    # ═══════════════════════════════════════════════════════════════════
    # 内部：Agent 循环工厂
    # ═══════════════════════════════════════════════════════════════════

    def _build_agent_loop(
        self,
        task: TaskRecord,
    ) -> Callable[[TaskRecord], Awaitable[None]]:
        """构造 AgentLoop 执行器回调（向后兼容包装）。

        通过 executor 工厂委托给 ``AgentExecutor``。
    返回一个 ``(TaskRecord) -> Awaitable[None]`` 回调，匹配调度器
        的 executor 契约。
        """
        exec_ctx = self._make_exec_ctx()

        async def _executor(t: TaskRecord) -> None:
            await self._executor_factory.get(TaskLevel.AGENT).execute(exec_ctx, t)

        return _executor

    def _make_role_provider(self, task: TaskRecord) -> Callable[[], Role]:
        return make_role_provider(self._resolver, task)

    def _current_task_role(self) -> Role:
        """从当前上下文变量中的任务解析角色。

        供工具 handler 中的 role_provider 使用。
        若当前没有任务上下文，返回 GUEST（安全默认）。
        """
        task = current_task.get()
        if task is None:
            return Role.GUEST
        return self._make_role_provider(task)()

    # ═══════════════════════════════════════════════════════════════════
    # 内部：ask 回调（发消息 + 日志）
    # ═══════════════════════════════════════════════════════════════════

    async def _ask_callback(self, stream_id: str, question: str) -> None:
        """向用户提问的跨层回调。

        由 AgentLoop 的 on_ask 参数和 ask_tool 的 ask_callback 共用。
        经统一发送出口（``ReplySender.send_polished``）润色后发送到目标聊天流 ——
        提问也走"更像人"的润色链路，与任务回复共享同一套可靠性保障
        （指数退避重试 + 静默掉包检测 + 长文本分割）。
        真实的挂起 / 等待 / 恢复状态转换由 AgentLoop._handle_ask_user 内部处理。

        Args:
            stream_id: 目标聊天流 ID。
            question: 待提问的问题文本。
        """
        # 带任务标识前缀的消息
        task = current_task.get()
        if task is not None:
            prefix = f"[任务 {task.title[:30]}] "
        else:
            prefix = ""

        full_msg = f"{prefix}{question}"

        try:
            await self._sender.send_polished(full_msg, stream_id)
            logger.info("已向聊天流 %s 发送 ask_user 提问：%s", stream_id, question[:80])
        except Exception:
            logger.exception("向聊天流 %s 发送 ask_user 提问失败", stream_id)

    # ═══════════════════════════════════════════════════════════════════
    # 内部：任务管理工具构建
    # ═══════════════════════════════════════════════════════════════════

    def _build_task_mgmt_tools(self) -> list[ToolDefinition]:
        """内部任务管理工具（list_my_tasks / create_subtask / inject_task）。"""
        from ..tools.agent.task_mgmt import build_task_mgmt_tools

        return build_task_mgmt_tools(
            self._store,
            self._sfmt,
            create_task=self.create_task,
            handle_injection=self.handle_injection,
            get_current_task=lambda: current_task.get(),
            get_current_task_role=self._current_task_role,
        )
