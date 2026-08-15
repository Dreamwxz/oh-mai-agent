"""Agent 工具装配 — 将全部工具工厂注册到 ToolRegistry。

从 ``core/task_manager.py`` 的 ``setup()`` 迁出：工具装配是"执行资源的组织
工作"，属于 executor 层（executor → tools 为文档声明的合法依赖方向）。
core 编排层不再直接 import 具体工具工厂 —— 新工具/工厂签名变化不再穿透
到 core。

``ToolWiring`` 聚合装配所需的全部运行时句柄，由 ``TaskManager.setup()``
（core 层）在调用 ``register_agent_tools`` 时提供。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.task_record import TaskRecord
from ..permission import Role
from ..tools.agent.ask_tool import build_ask_tool
from ..tools.agent.file_tools import build_file_tools
from ..tools.agent.info_tools import build_info_tools
from ..tools.agent.plugin_api_tools import refresh_plugin_api_tools
from ..tools.agent.shell_tools import build_shell_tools
from ..tools.agent.subagent_tool import build_subagent_tool, build_subagents_tool
from ..tools.agent.task_mgmt import build_task_mgmt_tools
from ..tools.send_message import build_send_tool

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolWiring:
    """工具装配所依赖的运行时句柄集合（由 TaskManager.setup() 提供）。

    字段均为宽松类型（Any）以避免装配层与具体实现强绑定；行为契约以
    关键字参数名 + 文档为准。
    """

    ctx: Any
    """SDK PluginContext。"""
    registry: Any
    """ToolRegistry — 装配目标。"""
    config_getter: Callable[[], Any]
    """MaibotAgentConfig 读取器 — 每次调用读取最新配置引用（热更新立即生效）。"""
    data_dir: Path
    """插件数据目录（文件工具 workspace = data_dir/files）。"""
    prompt_service: Any
    """PromptService（send/subagent 工具提示词构建）。"""
    store: Any
    """TaskStore（任务管理工具查询）。"""
    sfmt: Any
    """StatusFormatter（任务管理工具状态格式化）。"""
    role_provider: Callable[[], Role]
    """当前任务角色回调（文件/子 Agent/命令工具的 min_role 运行时门控）。"""
    ask_callback: Callable[[str, str], Awaitable[None]]
    """ask_user 提问回调（与 AgentLoop.on_ask 共用同一实现）。"""
    create_task: Callable[..., Awaitable[Any]]
    """任务创建入口（create_subtask 工具用）。"""
    handle_injection: Callable[[str, str], Awaitable[bool]]
    """指令注入入口（inject_task 工具用）。"""
    get_current_task: Callable[[], TaskRecord | None]
    """当前上下文任务（list_my_tasks / create_subtask 用）。"""
    sender: Any
    """ReplySender — send_message 工具的润色+发送出口。"""


async def register_agent_tools(w: ToolWiring) -> None:
    """将全部 Agent 循环工具注册到 ``w.registry``。

    注册顺序（与历史 TaskManager.setup() 一致，保证列表顺序稳定）：
    任务管理 → 信息 → 文件 → ask_user → send_message → 跨插件 API
    （尽力而为）→ 子 Agent（``[subagent] enabled`` 可开关）→ 命令执行
    （``[shell] enabled`` 可开关）。
    """

    # ── 1. 任务管理工具（list_my_tasks / create_subtask / inject_task）──
    for tool in build_task_mgmt_tools(
        w.store,
        w.sfmt,
        create_task=w.create_task,
        handle_injection=w.handle_injection,
        get_current_task=w.get_current_task,
        get_current_task_role=w.role_provider,
    ):
        w.registry.register(tool)

    # ── 2. 信息工具 ─────────────────────────────────────────────────
    for tool in build_info_tools(
        w.ctx, search_max_results=w.config_getter().search.max_results,
    ):
        w.registry.register(tool)

    # ── 3. 文件工具（user 级沙箱隔离到 data_dir/files）────────────
    user_workspace = w.data_dir / "files"
    # role_provider 在 AgentLoop 构造时按任务绑定；此处注册阶段仅定义工具
    file_tools = build_file_tools(
        w.ctx,
        user_workspace=user_workspace,
        admin_open=True,
        role_provider=w.role_provider,
    )
    for tool in file_tools:
        w.registry.register(tool)

    # ── 4. ask_user 工具 ────────────────────────────────────────────
    # on_ask 回调与 AgentLoop 共用 w.ask_callback；真实挂起由
    # AgentLoop._handle_ask_user 处理（两个 ask_user 路径共用同一回调）。
    for tool in build_ask_tool(
        w.ctx,
        ask_callback=w.ask_callback,
        min_role=Role.USER,
    ):
        w.registry.register(tool)

    # ── 5. send_message 工具 ────────────────────────────────────────
    # send_polished 绑定 ReplySender.send_polished（润色 + 分割 + 重试），
    # relay_from（转达委托人）由 send_message 工具参数透传。
    async def _send_polished(
        text: str, stream_id: str, *, relay_from: str | None = None,
    ) -> None:
        await w.sender.send_polished(text, stream_id, relay_from=relay_from)

    send_msg_tool = build_send_tool(
        w.ctx,
        send_polished=_send_polished,
        min_role=Role.USER,
        prompt_service=w.prompt_service,
    )
    w.registry.register(send_msg_tool)

    # ── 6. 跨插件 API 工具（尽力而为）──────────────────────────────
    try:
        ctx_api = getattr(w.ctx, "api", None)
        if ctx_api is not None:
            api_tools = await refresh_plugin_api_tools(ctx_api)
            for tool in api_tools:
                w.registry.register(tool)
    except Exception:
        logger.warning("扫描插件 API 工具失败", exc_info=True)

    # ── 7. 子 Agent 工具（可配置开关）───────────────────────────────
    # schema 为静态定义（零参数 builder）；执行在 AgentLoop 合成分支
    # （executor/agent_loop.py 的 _run_subagent/_run_subagents），子 Agent
    # 配置（max_rounds 等）由 AgentExecutor 注入 config 读取器，无需装配期绑定。
    if w.config_getter().subagent.enabled:
        for tool in (
            build_subagent_tool(),
            build_subagents_tool(),
        ):
            w.registry.register(tool)

    # ── 8. 命令执行工具（可配置开关，admin 双重门控）───────────────
    if w.config_getter().shell.enabled:
        for tool in build_shell_tools(
            w.ctx,
            config_getter=lambda: w.config_getter().shell,
            role_provider=w.role_provider,
        ):
            w.registry.register(tool)