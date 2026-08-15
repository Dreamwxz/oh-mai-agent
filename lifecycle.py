"""MaiBot Agent 插件生命周期函数 — lifecycle.py。

将原 plugin.py 中 MaibotAgentPlugin 的 5 个生命周期/辅助方法
抽取为模块级函数，以 `plugin` 参数替代 `self`，便于测试和复用。

提供：
- load_plugin — 原 on_load 体（初始化全部组件并恢复活跃任务）
- apply_config_update — 原 on_config_update 体（配置热更新传播）
- recover_active_tasks — 插件重启后恢复活跃任务（原 on_load 内联逻辑）
- reload_mcp_if_changed — MCP 配置热更新（原 on_config_update 内联逻辑）
- llm_title — 原 _llm_title（LLM 生成任务标题）
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF

from .api_expose import build_api_handlers
from .bus import TaskCommandBus
from .core.scheduler import TaskScheduler
from .core.task_manager import TaskManager
from .domain.task_record import TaskLevel, TaskRecord
from .domain.task_store import TaskStore
from .tools.mcp.provider import MCPManager, unregister_stale_mcp_tools
from .permission import PermissionResolver
from .planner_hooks import PlannerBoard
from .prompt.builders import ALL_BUILDERS
from .prompt.manager import PromptManager
from .prompt.service import PromptService
from .tools.registry import ToolRegistry

if TYPE_CHECKING:
    from .plugin import MaibotAgentPlugin


# ═══════════════════════════════════════════════════════════════════════════
# 1. load_plugin — 原 on_load 体
# ═══════════════════════════════════════════════════════════════════════════

async def load_plugin(plugin: "MaibotAgentPlugin") -> None:
    """插件加载：初始化所有组件并恢复活跃任务。"""
    logger = plugin.ctx.logger
    logger.info("oh-mai-agent 插件加载中...")

    data_dir = plugin.ctx.paths.data_dir

    # 1. 初始化 TaskStore (sqlite)
    plugin._store = TaskStore(data_dir / "tasks.db")
    await plugin._store.init()
    logger.info("TaskStore 初始化完成: %s/tasks.db", data_dir)

    # 2. 初始化 ToolRegistry、PermissionResolver
    plugin._registry = ToolRegistry()
    plugin._resolver = PermissionResolver(plugin.config.permission)
    logger.info(
        "权限解析器就绪 (admins=%d, admin_groups=%d)",
        len(plugin.config.permission.admins),
        len(plugin.config.permission.admin_groups),
    )

    # 2.5. 初始化命令总线（注入 / 唤醒 / 事件通道）
    plugin._command_bus = TaskCommandBus()
    logger.info("TaskCommandBus 初始化完成")

    # 3. 初始化 TaskScheduler（executor 后绑定打破 TaskManager 构造环：
    #    TaskManager 需要 scheduler，scheduler 需要 TaskManager 的执行回调，
    #    先构造 scheduler，TaskManager 就绪后 set_executor 注入）
    plugin._scheduler = TaskScheduler(
        config=plugin.config.task,
        store=plugin._store,
        command_bus=plugin._command_bus,
    )

    # 4. PromptManager — 供 llm_title / llm_classify 等使用
    plugin._pm = PromptManager(Path(__file__).parent / "prompt" / "templates")
    logger.info("PromptManager 初始化完成")

    # 4.1. PromptService — builder 模式统一入口
    plugin._pm_service = PromptService(manager=plugin._pm, builders=ALL_BUILDERS)

    # 4.2. ReplySender — 统一发送出口（直发 / 完整润色 + 上下文注释）
    # config_getter 每次调用读取，配置热更新立即生效
    from .executor.instant import ReplySender

    plugin._sender = ReplySender(
        ctx=plugin.ctx,
        config_getter=lambda: plugin.config,
        prompt_service=plugin._pm_service,
    )

    # 5. 初始化 TaskManager
    plugin._task_manager = TaskManager(
        ctx=plugin.ctx,
        store=plugin._store,
        scheduler=plugin._scheduler,
        registry=plugin._registry,
        resolver=plugin._resolver,
        config=plugin.config,
        llm_title=plugin._llm_title,
        data_dir=data_dir,
        prompt_manager=plugin._pm,
        prompt_service=plugin._pm_service,
        command_bus=plugin._command_bus,
        sender=plugin._sender,
    )
    # 注入调度器执行回调（TaskManager 就绪后，start() 之前）
    plugin._scheduler.set_executor(plugin._task_manager.execute_task)
    await plugin._task_manager.setup()
    logger.info(
        "TaskManager 初始化完成 (%d 个工具已注册)",
        len(plugin._registry.all_names()),
    )

    # 6. 启动调度器（执行回调已绑定，派发的任务可安全执行）
    await plugin._scheduler.start()

    # 7. 恢复活跃任务
    await recover_active_tasks(plugin, logger)

    # 8. 初始化 MCP（如启用）
    plugin._mcp = MCPManager(plugin.config.mcp)
    await plugin._mcp.start()
    mcp_tools = plugin._mcp.build_tool_definitions()
    for td in mcp_tools:
        plugin._registry.register(td)
    logger.info("MCP 初始化完成: %d 个服务器, %d 个工具", len(plugin._mcp._connections), len(mcp_tools))
    plugin._mcp_config = plugin.config.mcp

    # 9. Planner 看板初始化
    plugin._planner_board = PlannerBoard(
        store=plugin._store,
        config=plugin.config.planner_board,
        logger=plugin.ctx.logger,
        prompt_service=plugin._pm_service,
    )
    logger.info("PlannerBoard 初始化完成")

    # 10. 注册跨插件动态 API
    handlers = build_api_handlers(plugin._task_manager)
    for h in handlers:
        plugin.register_dynamic_api(
            name=h["name"],
            handler=h["handler"],
            description=h["description"],
            version=h["version"],
            public=h["public"],
        )
    await plugin.sync_dynamic_apis()
    logger.info("跨插件动态 API 注册完成: %d 个端点", len(handlers))

    logger.info("oh-mai-agent 插件加载成功")


# ═══════════════════════════════════════════════════════════════════════════
# 2. apply_config_update — 原 on_config_update 体
# ═══════════════════════════════════════════════════════════════════════════

async def apply_config_update(
    plugin: "MaibotAgentPlugin",
    scope: str,
    config_data: dict[str, object],
    version: str,
) -> None:
    """配置热更新回调：将新配置应用到运行时组件。

    SDK 在调用本回调前已通过 set_plugin_config 刷新 plugin.config，
    本方法负责将新配置传播到各运行时组件（权限、调度器、任务管理器、MCP）。
    """
    if scope != CONFIG_RELOAD_SCOPE_SELF:
        return
    logger = plugin.ctx.logger
    logger.info("插件配置已更新: version=%s", version)
    try:
        # 热更新过程中任一步失败仅记日志，不向 SDK 抛出，避免影响插件整体运行
        # 1. 权限解析器重建（权限变更立即生效）
        plugin._resolver = PermissionResolver(plugin.config.permission)
        if (
            hasattr(plugin, "_task_manager")
            and plugin._task_manager is not None
        ):
            plugin._task_manager.update_resolver(plugin._resolver)
        logger.debug("权限解析器已重建")

        # 2. 调度器配置更新（并发上限/超时）
        plugin._scheduler.update_config(plugin.config.task)
        logger.debug("调度器配置已更新 (并发上限/超时)")

        # 3. TaskManager 配置引用更新
        if (
            hasattr(plugin, "_task_manager")
            and plugin._task_manager is not None
        ):
            plugin._task_manager.update_config(plugin.config)
            logger.debug("任务管理器配置引用已更新")

        # 4. MCP 热更新
        logger.debug("开始检查 MCP 配置变更")
        await reload_mcp_if_changed(plugin)

        # 5. PlannerBoard 配置更新（重建看板，清空 hash 去重状态）
        if hasattr(plugin, "_planner_board") and plugin._planner_board is not None:
            plugin._planner_board = PlannerBoard(
                store=plugin._store,
                config=plugin.config.planner_board,
                logger=plugin.ctx.logger,
                prompt_service=plugin._pm_service,
            )
            logger.info("PlannerBoard 配置已热更新")

        logger.info(
            "插件配置热更新应用完成 (version=%s): 权限、调度器、任务管理器、MCP、PlannerBoard 已按需更新",
            version,
        )
    except Exception as exc:
        logger.error("配置热更新应用失败: %s", exc, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════
# 3. llm_title — 原 _llm_title 体
# ═══════════════════════════════════════════════════════════════════════════

async def llm_title(plugin: "MaibotAgentPlugin", intent: str) -> str:
    """调用 LLM 生成一句话任务标题；失败时降级为 intent[:40]。"""
    try:
        prompt = plugin._pm_service.build("title", intent=intent)
        result = await plugin.ctx.llm.generate(
            prompt=prompt, model="utils", timeout_ms=60000,
        )
        title = str(result.get("response", "")).strip()
        if title:
            return title
    except Exception as exc:
        logger = plugin.ctx.logger
        logger.debug("LLM 标题生成失败，降级为意图前 40 字符: %s", exc)
    return intent[:40]


# ═══════════════════════════════════════════════════════════════════════════
# 4. recover_active_tasks — 原 on_load 内联恢复逻辑体
# ═══════════════════════════════════════════════════════════════════════════

async def recover_active_tasks(plugin: "MaibotAgentPlugin", logger: Any) -> None:
    """恢复插件重启前未完成的活跃任务。

    - SCHEDULED：重新入队等待定时触发。
    - RUNNING：降级为 PENDING 重新排队（Agent 上下文丢失，续跑重新开始）。
    - WAITING_INPUT：保持状态，chat.receive.after_process Hook 收到用户回复时恢复。
    """
    from .domain.recovery import RecoveryAction, TaskRecovery

    try:
        active = await plugin._store.list_active()
    except Exception:
        logger.warning("恢复活跃任务时获取任务列表失败", exc_info=True)
        return

    recovered_count = 0
    for task in active:
        action = TaskRecovery.recover(task)
        if action == RecoveryAction.ENQUEUE:
            await plugin._scheduler.enqueue(task)
            recovered_count += 1
            logger.debug("任务 %s 已重新入队 (SCHEDULED)", task.id)
        elif action == RecoveryAction.PENDING:
            await plugin._store.save(task)
            await plugin._scheduler.enqueue(task)
            recovered_count += 1
            logger.info("任务 %s 已恢复: RUNNING → PENDING", task.id)
        elif action == RecoveryAction.WAITING:
            # 保持 WAITING_INPUT；chat.receive.after_process Hook 收到回复时通过
            # handle_user_reply 通过命令总线发送 RESUME_REPLY 恢复
            recovered_count += 1
            logger.debug("任务 %s 保持 WAITING_INPUT，等待用户回复恢复", task.id)
        # PAUSED：不自动处理（需手动恢复）

    if recovered_count > 0:
        logger.info("已从上次会话恢复 %d 个活跃任务", recovered_count)
    else:
        logger.debug("无活跃任务需要恢复")


# ═══════════════════════════════════════════════════════════════════════════
# 5. reload_mcp_if_changed — 原 on_config_update 内联 MCP 重载逻辑体
# ═══════════════════════════════════════════════════════════════════════════

async def reload_mcp_if_changed(plugin: "MaibotAgentPlugin") -> None:
    """MCP 配置变更时重启 MCP 管理器。

    比较当前 plugin.config.mcp 与记录的上次 _mcp_config；若相同则跳过。
    若变更则停止旧 MCP、用新配置重建、重新注册工具到 registry。
    registry.register 对同名工具直接覆盖，无需显式 unregister。
    """
    new_cfg = plugin.config.mcp
    old_cfg = getattr(plugin, "_mcp_config", None)
    if old_cfg is not None and new_cfg == old_cfg:
        return
    plugin._mcp_config = new_cfg

    logger = plugin.ctx.logger
    if hasattr(plugin, "_mcp") and plugin._mcp is not None:
        await plugin._mcp.stop()
        logger.info("MCP 管理器已停止，重新初始化...")
    plugin._mcp = MCPManager(new_cfg)
    await plugin._mcp.start()
    mcp_tools = plugin._mcp.build_tool_definitions()
    for td in mcp_tools:
        plugin._registry.register(td)
    unregister_stale_mcp_tools(plugin._registry, {td.name for td in mcp_tools})
    logger.info(
        "MCP 配置热更新: %d server(s), %d tool(s)",
        len(plugin._mcp._connections),
        len(mcp_tools),
    )
