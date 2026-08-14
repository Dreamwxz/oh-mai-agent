"""MaiBot Agent 插件入口 — plugin.py。

MaiBot SDK 插件主入口，实现 MaibotAgentPlugin 类，提供：
- 生命周期管理（on_load / on_unload / on_config_update）
- 暴露给主 Planner 的安全子集 Tool（9 个工具：7 个任务管理 + search_users + send_message）
- /maitask 命令组（7 个 Command，含兜底帮助命令）
- 用户回复监听（HookHandler chat.receive.after_process）
- Planner 摘要注入 Hook（HookHandler，委托 PlannerBoard）

任务模型与插件架构详见 domain/task_record.py 和本文件内注释。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import MaibotAgentConfig
from .commands import cmd_arg, cmd_ask, cmd_cancel, cmd_create, cmd_fallback, cmd_history, cmd_list, cmd_status, cmd_text
from .lifecycle import apply_config_update, llm_title as llm_title_fn, load_plugin, recover_active_tasks, reload_mcp_if_changed

logger = logging.getLogger(__name__)


class MaibotAgentPlugin(MaiBotPlugin):
    """MaiBot Agent 插件主类。

    插件启动流程（on_load）：
    1. 初始化 TaskStore（sqlite）→ ToolRegistry / PermissionResolver / TaskScheduler
    2. 初始化 TaskManager → setup() 注册所有 Agent 工具
    3. 启动 scheduler → 恢复 active 任务 → 初始化 MCP

    Planner 安全子集：@Tool 仅暴露任务管理 + 用户搜索/消息发送工具，
    不暴露文件/宿主等危险操作，Planner 即使被提示词注入也无法写宿主机文件。
    """

    # ── SDK 类属性 ──────────────────────────────────────────────────────
    config_model = MaibotAgentConfig

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """插件加载：初始化所有组件并恢复活跃任务。"""
        await load_plugin(self)

    async def on_unload(self) -> None:
        """插件卸载：停止调度器、关闭 MCP、落盘 running 任务、关闭存储。"""
        logger = self.ctx.logger
        logger.info("oh-mai-agent 插件卸载中...")

        # 1. 停止调度器（内部将 running 任务标记为 paused 落盘）
        await self._scheduler.stop()

        # 2. 停止 MCP
        if hasattr(self, "_mcp") and self._mcp is not None:
            await self._mcp.stop()

        # 3. 关闭存储
        if hasattr(self, "_store") and self._store is not None:
            await self._store.close()

        logger.info("oh-mai-agent 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        """配置热更新回调：将新配置应用到运行时组件。

        SDK 在调用本回调前已通过 set_plugin_config 刷新 self.config，
        本方法负责将新配置传播到各运行时组件（权限、调度器、任务管理器、MCP）。
        """
        await apply_config_update(self, scope, config_data, version)

    # ═══════════════════════════════════════════════════════════════════════
    # 内部：LLM 标题生成
    # ═══════════════════════════════════════════════════════════════════════

    async def _llm_title(self, intent: str) -> str:
        """调用 LLM 生成一句话任务标题；失败时降级为 intent[:40]."""
        return await llm_title_fn(self, intent)

    # ═══════════════════════════════════════════════════════════════════════
    # 内部：任务恢复
    # ═══════════════════════════════════════════════════════════════════════

    async def _recover_active_tasks(self, logger: Any) -> None:
        """恢复插件重启前未完成的活跃任务。

        - SCHEDULED：重新入队等待定时触发。
        - RUNNING：降级为 PENDING 重新排队（Agent 上下文丢失，续跑重新开始）。
        - WAITING_INPUT：保持状态，chat.receive.after_process Hook 收到用户回复时恢复。
        """
        await recover_active_tasks(self, logger)

    async def _reload_mcp_if_changed(self) -> None:
        """MCP 配置变更时重启 MCP 管理器。

        比较当前 self.config.mcp 与记录的上次 _mcp_config；若相同则跳过。
        若变更则停止旧 MCP、用新配置重建、重新注册工具到 registry。
        registry.register 对同名工具直接覆盖，无需显式 unregister。
        """
        await reload_mcp_if_changed(self)

    # ═══════════════════════════════════════════════════════════════════════
    # 内部：Planner Tool 懒构建
    # ═══════════════════════════════════════════════════════════════════════

    def _get_planner_tool(self, name: str) -> Callable[..., Awaitable[dict]]:
        """懒构建 planner 工具 handler 并缓存。测试直接调用 _tool_* 不经过 on_load，因此必须懒构建。"""
        if not hasattr(self, "_planner_tool_cache"):
            self._planner_tool_cache: dict[str, Callable[..., Awaitable[dict]]] = {}
        if name not in self._planner_tool_cache:
            if name == "search_users":
                from .tools.planner.search_users import build_search_users_handler
                self._planner_tool_cache[name] = build_search_users_handler(self.ctx, self.config)
            elif name == "send_message":
                from .tools.send_message import build_send_message_handler
                self._planner_tool_cache[name] = build_send_message_handler(self.ctx, self.config, self._pm, self._pm_service)
            else:
                from .tools.planner.task_tools import build_task_tools
                self._planner_tool_cache[name] = build_task_tools(self._task_manager)[name]
        return self._planner_tool_cache[name]

    # ═══════════════════════════════════════════════════════════════════════
    # @Tool：暴露给主 Planner 的安全子集（9 个工具）
    # ═══════════════════════════════════════════════════════════════════════

    @Tool(
        "search_users",
        description="按昵称搜索用户，返回 user_id 用于 send_message 定位目标。",
        visibility="deferred",
        parameters=[
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="搜索关键词（昵称、名字、QQ号等，可选）",
                required=False,
            ),
            ToolParameterInfo(
                name="chat_type",
                param_type=ToolParamType.STRING,
                description="聊天类型过滤：group 或 private（可选）",
                required=False,
            ),
            ToolParameterInfo(
                name="platform",
                param_type=ToolParamType.STRING,
                description="平台名（可选，如 qq/discord/wechat）",
                required=False,
            ),
        ],
    )
    async def _tool_search_users(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("search_users")(**kwargs)

    @Tool(
        "task_create",
        description="创建一次性/延迟任务并立即调度执行。要定时/周期任务用 task_schedule；要查详情用 task_status。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="intent",
                param_type=ToolParamType.STRING,
                description="任务意图描述",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="目标聊天流 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="level",
                param_type=ToolParamType.STRING,
                description="执行级别 instant/agent，不填默认 agent；仅纯消息提醒类任务（定时发消息）用 instant",
                enum_values=["instant", "agent"],
                required=False,
            ),
            ToolParameterInfo(
                name="delay_seconds",
                param_type=ToolParamType.INTEGER,
                description="延迟秒数",
                required=False,
            ),
            ToolParameterInfo(
                name="cron_expr",
                param_type=ToolParamType.STRING,
                description="cron 表达式（如非必要勿用，周期任务请用 task_schedule）",
                required=False,
            ),
            ToolParameterInfo(
                name="priority",
                param_type=ToolParamType.INTEGER,
                description="优先级，越高越优先",
                required=False,
            ),
            ToolParameterInfo(
                name="reply_stream_id",
                param_type=ToolParamType.STRING,
                description="回复目标聊天流 ID（不填则回复到任务所在的 stream_id）",
                required=False,
            ),
        ],
    )
    async def _tool_task_create(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("task_create")(**kwargs)

    @Tool(
        "task_list",
        description="列出任务（可按 stream/status 过滤）。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="聊天流 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="status",
                param_type=ToolParamType.STRING,
                description="按状态过滤（可选）",
                enum_values=["pending", "running", "waiting_input", "paused", "scheduled", "completed", "failed", "cancelled"],
                required=False,
            ),
        ],
    )
    async def _tool_task_list(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("task_list")(**kwargs)

    @Tool(
        "task_status",
        description="查看单个任务当前详情（快照）。要看执行历史用 task_history。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="聊天流 ID",
                required=True,
            ),
        ],
    )
    async def _tool_task_status(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("task_status")(**kwargs)

    @Tool(
        "task_modify",
        description="修改任务或注入指令。注意：注入指令仅管理员可用。",
        visibility="deferred",
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="inject_instruction",
                param_type=ToolParamType.STRING,
                description="要注入的指令文本",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="聊天流 ID",
                required=True,
            ),
        ],
    )
    async def _tool_task_modify(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("task_modify")(**kwargs)

    @Tool(
        "task_delete",
        description="取消/删除任务。不可恢复，请谨慎操作。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="聊天流 ID",
                required=True,
            ),
        ],
    )
    async def _tool_task_delete(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("task_delete")(**kwargs)

    @Tool(
        "task_history",
        description="查看任务执行历史时间线。看当前状态用 task_status。",
        visibility="deferred",
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="聊天流 ID",
                required=True,
            ),
        ],
    )
    async def _tool_task_history(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("task_history")(**kwargs)

    @Tool(
        "task_schedule",
        description="创建定时任务（cron）。一次性任务请用 task_create。",
        visibility="deferred",
        parameters=[
            ToolParameterInfo(
                name="intent",
                param_type=ToolParamType.STRING,
                description="任务意图描述",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="聊天流 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="cron_expr",
                param_type=ToolParamType.STRING,
                description="cron `* * * * *` 每分钟永久重跑，慎用高频率表达式",
                required=True,
            ),
            ToolParameterInfo(
                name="level",
                param_type=ToolParamType.STRING,
                description="执行级别 instant/agent，不填默认 agent；仅纯消息提醒类任务（定时发消息）用 instant",
                enum_values=["instant", "agent"],
                required=False,
            ),
        ],
    )
    async def _tool_task_schedule(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("task_schedule")(**kwargs)

    @Tool(
        "send_message",
        description=(
            "向好友/群发送消息（自动创建聊天流、默认润色与长文本分割）。"
            "转达他人之言必须点明委托人。"
        ),
        visibility="deferred",
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="要发送的消息文本",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="目标聊天流 ID（与 group_id/user_id 三选一，提供时直接发送到该流，如其他用户的流）",
                required=False,
            ),
            ToolParameterInfo(
                name="group_id",
                param_type=ToolParamType.STRING,
                description="目标群 ID（与 user_id 二选一）",
                required=False,
            ),
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="目标用户 ID（与 group_id 二选一）",
                required=False,
            ),
            ToolParameterInfo(
                name="platform",
                param_type=ToolParamType.STRING,
                description="平台标识（可选，默认 qq）",
                required=False,
            ),
            ToolParameterInfo(
                name="polish",
                param_type=ToolParamType.BOOLEAN,
                description="是否 LLM 润色（可选，默认 true；发代码/命令等不希望改写时设 false）",
                required=False,
            ),
            ToolParameterInfo(
                name="split",
                param_type=ToolParamType.BOOLEAN,
                description="是否分割长文本为多条消息（可选，默认 true；希望整条完整呈现时设 false）",
                required=False,
            ),
        ],
    )
    async def _tool_send_message(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("send_message")(**kwargs)

    # ═══════════════════════════════════════════════════════════════════════
    # @Command：/maitask 命令组 — 薄壳委托 commands.py
    # ═══════════════════════════════════════════════════════════════════════

    @Command(
        "maitask_create",
        description="创建任务",
        pattern=r"^/maitask\s+create\b",
        aliases=["/mt create"],
    )
    async def cmd_task_create(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._cmd_create(**kwargs)

    @Command(
        "maitask_list",
        description="列出任务",
        pattern=r"^/maitask\s+list\b",
        aliases=["/mt list"],
    )
    async def cmd_task_list(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._cmd_list(**kwargs)

    @Command(
        "maitask_status",
        description="查看任务状态",
        pattern=r"^/maitask\s+status\b",
        aliases=["/mt status"],
    )
    async def cmd_task_status(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._cmd_status(**kwargs)

    @Command(
        "maitask_cancel",
        description="取消任务",
        pattern=r"^/maitask\s+cancel\b",
        aliases=["/mt cancel"],
    )
    async def cmd_task_cancel(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._cmd_cancel(**kwargs)

    @Command(
        "maitask_history",
        description="查看任务历史",
        pattern=r"^/maitask\s+history\b",
        aliases=["/mt history"],
    )
    async def cmd_task_history(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._cmd_history(**kwargs)

    @Command(
        "maitask_ask",
        description="向任务注入指令",
        pattern=r"^/maitask\s+ask\b",
        aliases=["/mt ask"],
    )
    async def cmd_task_ask(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._cmd_ask(**kwargs)

    @Command(
        "maitask_help_fallback",
        description="maitask 命令帮助（兜底：拦截所有未匹配的 /maitask 输入）",
        pattern=r"^/maitask\b",
    )
    async def cmd_zz_task_fallback(self, **kwargs: Any) -> tuple[bool, str, int]:
        """兜底：任何 /maitask 开头的输入都显示帮助并拦截，避免落入 Maisaka planner。"""
        return await cmd_fallback(self, **kwargs)

    # ── Command 参数提取辅助 ──────────────────────────────────────────

    @staticmethod
    def _cmd_text(**kwargs: Any) -> str:
        """提取完整命令消息文本（兼容 text / plain_text 两种键名）。

        MaiBot 命令执行器传 text（processed_plain_text），但部分旧代码用 plain_text。
        优先取 text，回退 plain_text。
        """
        return cmd_text(**kwargs)

    @staticmethod
    def _cmd_arg(kwargs: dict[str, Any], index: int, default: str = "") -> str:
        """从 matched_groups 提取第 index 个正则组；缺失则返回 default。

        matched_groups 可能是 {0: 全文, 1: 第一组...} 或 {group_name: ...}。
        当正则组不存在时返回 default，调用方自行回退到文本解析。
        """
        return cmd_arg(kwargs, index, default)

    # ── Command 内部实现 ────────────────────────────────────────────────

    async def _cmd_create(self, **kwargs: Any) -> tuple[bool, str, int]:
        """处理 /maitask create <意图描述>."""
        return await cmd_create(self, **kwargs)

    async def _cmd_list(self, **kwargs: Any) -> tuple[bool, str, int]:
        """处理 /maitask list [状态]."""
        return await cmd_list(self, **kwargs)

    async def _cmd_status(self, **kwargs: Any) -> tuple[bool, str, int]:
        """处理 /maitask status <id>."""
        return await cmd_status(self, **kwargs)

    async def _cmd_cancel(self, **kwargs: Any) -> tuple[bool, str, int]:
        """处理 /maitask cancel <id>."""
        return await cmd_cancel(self, **kwargs)

    async def _cmd_history(self, **kwargs: Any) -> tuple[bool, str, int]:
        """处理 /maitask history [<id>]."""
        return await cmd_history(self, **kwargs)

    async def _cmd_ask(self, **kwargs: Any) -> tuple[bool, str, int]:
        """处理 /maitask ask <id> <指令>."""
        return await cmd_ask(self, **kwargs)

    # ═══════════════════════════════════════════════════════════════════════
    # @HookHandler：监听用户回复，唤醒 waiting_input 任务
    # ═══════════════════════════════════════════════════════════════════════

    @HookHandler(
        "chat.receive.after_process",
        name="agent_user_reply",
        mode=HookMode.OBSERVE,
    )
    async def on_message(self, **kwargs: Any) -> dict[str, Any]:
        """监听入站消息，匹配 WAITING_INPUT 任务并注入用户回复。

        从 message dict 提取字段：
        - session_id → stream_id
        - message_info.user_info.user_id → user_id
        - processed_plain_text → plain_text

        注意：MaiBot 1.1.3 中 ON_MESSAGE 事件已不触发，
        改用 chat.receive.after_process Hook（bot.py:724 真实触发）。
        """
        try:
            message = kwargs.get("message") or {}
            stream_id = str(message.get("session_id", "")) if isinstance(message, dict) else ""
            user_id = ""
            plain_text = ""
            if isinstance(message, dict):
                user_info = (message.get("message_info") or {}).get("user_info") or {}
                user_id = str(user_info.get("user_id", ""))
                plain_text = str(message.get("processed_plain_text", ""))
            if not stream_id or not user_id or not plain_text:
                return {"action": "continue"}

            await self._task_manager.handle_user_reply(
                stream_id=stream_id,
                user_id=user_id,
                reply=plain_text,
            )
        except Exception:
            self.ctx.logger.debug("用户回复匹配失败", exc_info=True)
        return {"action": "continue"}

    # ═══════════════════════════════════════════════════════════════════════
    # @HookHandler：Planner 摘要注入
    # ═══════════════════════════════════════════════════════════════════════

    @HookHandler(
        "maisaka.planner.before_request",
        name="agent_planner_board",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def on_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        """Planner 摘要注入 Hook — 委托给 PlannerBoard。"""
        return await self._planner_board.hook_before_request(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# 模块级工厂
# ═══════════════════════════════════════════════════════════════════════════


def create_plugin() -> MaibotAgentPlugin:
    """SDK 要求的模块级工厂函数，返回 MaibotAgentPlugin 实例。"""
    logger.info("创建 oh-mai-agent 插件实例...")
    instance = MaibotAgentPlugin()
    components = instance.get_components()
    tool_count = sum(1 for c in components if c["type"] == "TOOL")
    command_count = sum(1 for c in components if c["type"] == "COMMAND")
    hook_count = sum(1 for c in components if c["type"] == "HOOK_HANDLER")
    logger.info(
        "oh-mai-agent 注册组件汇总：Tool=%d、Command=%d、HookHandler=%d",
        tool_count,
        command_count,
        hook_count,
    )
    return instance
