"""MaiBot Agent 插件入口 — plugin.py。

MaiBot SDK 插件主入口，实现 MaibotAgentPlugin 类，提供：
- 生命周期管理（on_load / on_unload / on_config_update）
- 暴露给主 Planner 的安全子集 Tool（11 个工具：7 个 subagent_* 后台子代理管理 + search_users + send_message + 2 个 MCP 代理）
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
from .commands import cmd_ask, cmd_cancel, cmd_create, cmd_fallback, cmd_history, cmd_list, cmd_status
from .lifecycle import apply_config_update, llm_title as llm_title_fn, load_plugin
from .tools.send_message import SEND_MESSAGE_DESCRIPTION, SEND_MESSAGE_PARAMS

logger = logging.getLogger(__name__)


def _send_message_params() -> list[ToolParameterInfo]:
    """将 send_message 单一参数规范转换为 SDK @Tool 参数列表。

    与 ``tools/send_message.py`` 的 ``params_to_json_schema`` 共用同一份
    ``SEND_MESSAGE_PARAMS``，保证 Planner @Tool 与 Agent 循环工具的 schema
    永不漂移。
    """
    _type_map: dict[str, ToolParamType] = {
        "string": ToolParamType.STRING,
        "boolean": ToolParamType.BOOLEAN,
        "integer": ToolParamType.INTEGER,
        "array": ToolParamType.ARRAY,
    }
    return [
        ToolParameterInfo(
            name=p["name"],
            param_type=_type_map[p["type"]],
            description=p["description"],
            required=bool(p.get("required", False)),
        )
        for p in SEND_MESSAGE_PARAMS
    ]


class MaibotAgentPlugin(MaiBotPlugin):
    """MaiBot Agent 插件主类。

    插件启动流程（on_load）：
    1. 初始化 TaskStore（sqlite）→ ToolRegistry / PermissionResolver / TaskScheduler
    2. 初始化 TaskManager → setup() 注册所有 Agent 工具
    3. 启动 scheduler → 恢复 active 任务 → 初始化 MCP

    Planner 安全子集：@Tool 仅暴露后台子代理管理（subagent_*）+ 用户搜索/
    消息发送/MCP 代理工具，不暴露文件/宿主等危险操作，Planner 即使被提示词
    注入也无法写宿主机文件。
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
    # 组件公共访问器 — 供命令层 / 外部模块读取运行时组件
    # （运行期消费方必须经公共接口访问；lifecycle.py 组装根仍直接操作私有字段）
    # ═══════════════════════════════════════════════════════════════════════

    @property
    def resolver(self) -> Any:
        """权限解析器（PermissionResolver）。on_load 完成后可用；未初始化返回 None。"""
        return getattr(self, "_resolver", None)

    @property
    def task_manager(self) -> Any:
        """任务管理器（TaskManager）。on_load 完成后可用；未初始化返回 None。"""
        return getattr(self, "_task_manager", None)

    # ═══════════════════════════════════════════════════════════════════════
    # 内部：LLM 标题生成
    # ═══════════════════════════════════════════════════════════════════════

    async def _llm_title(self, intent: str) -> str:
        """调用 LLM 生成一句话任务标题；失败时降级为 intent[:40]."""
        return await llm_title_fn(self, intent)

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
                self._planner_tool_cache[name] = build_send_message_handler(self.ctx, self._task_manager.sender)
            elif name == "list_mcp_tools":
                from .tools.planner.mcp_tools import build_list_mcp_tools_handler
                self._planner_tool_cache[name] = build_list_mcp_tools_handler(lambda: getattr(self, "_mcp", None))
            elif name == "call_mcp_tool":
                from .tools.planner.mcp_tools import build_call_mcp_tool_handler
                self._planner_tool_cache[name] = build_call_mcp_tool_handler(lambda: getattr(self, "_mcp", None))
            else:
                from .tools.planner.task_tools import build_task_tools
                self._planner_tool_cache[name] = build_task_tools(self._task_manager)[name]
        return self._planner_tool_cache[name]

    # ═══════════════════════════════════════════════════════════════════════
    # @Tool：暴露给主 Planner 的安全子集（11 个工具）
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
        "subagent_create",
        description="创建后台子代理任务并立即开始执行（可延迟）。任务会交由独立 Agent 在后台自主执行，执行结果自动汇报到任务所在聊天流；执行中可等待用户输入（subagent_status 可见 waiting_input），也可用 subagent_modify 注入新指令调整方向。用户提出需要多步骤或耗时处理的需求（如「帮我爬取数据」「整理这份文档」）时使用。一次性任务用本工具；定时/周期任务用 subagent_schedule；查任务状态用 subagent_list / subagent_status。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="intent",
                param_type=ToolParamType.STRING,
                description="任务意图描述（要后台子代理做什么）",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="任务所属聊天流 ID（必须是当前会话流，任务结果将汇报到此流）",
                required=True,
            ),
            ToolParameterInfo(
                name="level",
                param_type=ToolParamType.STRING,
                description="执行级别：agent=由独立 Agent 自主推理执行（默认，适合多步骤复杂任务）；instant=立即执行单个动作（仅适合纯消息类任务，如定时提醒、自动回复）",
                enum_values=["instant", "agent"],
                required=False,
            ),
            ToolParameterInfo(
                name="delay_seconds",
                param_type=ToolParamType.INTEGER,
                description="延迟秒数，到点后开始执行",
                required=False,
            ),
            ToolParameterInfo(
                name="cron_expr",
                param_type=ToolParamType.STRING,
                description="cron 表达式（不推荐，常规定时/周期任务请用 subagent_schedule）",
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
    async def _tool_subagent_create(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("subagent_create")(**kwargs)

    @Tool(
        "subagent_list",
        description="列出当前聊天流的子代理任务，可按状态过滤。用户询问任务进展（「我的任务怎么样了」「有哪些任务在跑」）时使用；看单个任务详情用 subagent_status，看执行时间线用 subagent_history，要取消用 subagent_delete。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="任务所属聊天流 ID（必须是当前会话流）",
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
    async def _tool_subagent_list(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("subagent_list")(**kwargs)

    @Tool(
        "subagent_status",
        description="查看单个子代理任务的当前详情快照（状态、进度、错误等）。task_id 支持完整 ID、唯一前缀或唯一标题。用户问某个具体任务的情况时使用；要查看完整执行历史用 subagent_history。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID（支持完整 ID、唯一前缀或唯一标题）",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="任务所属聊天流 ID（必须是当前会话流）",
                required=True,
            ),
        ],
    )
    async def _tool_subagent_status(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("subagent_status")(**kwargs)

    @Tool(
        "subagent_modify",
        description="向运行中或等待输入的子代理任务注入新指令，实时调整其执行方向。用户想改变正在进行的任务（「换个格式」「先做 X 再继续」）时使用；任务结束后无法注入，需重新创建。注意：注入指令仅管理员可用。",
        visibility="deferred",
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID（支持完整 ID、唯一前缀或唯一标题）",
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
                description="任务所属聊天流 ID（必须是当前会话流）",
                required=True,
            ),
        ],
    )
    async def _tool_subagent_modify(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("subagent_modify")(**kwargs)

    @Tool(
        "subagent_delete",
        description="取消/删除子代理任务，不可恢复，请谨慎操作。用户不想让任务继续时使用；定时任务取消后不再触发。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID（支持完整 ID、唯一前缀或唯一标题）",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="任务所属聊天流 ID（必须是当前会话流）",
                required=True,
            ),
        ],
    )
    async def _tool_subagent_delete(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("subagent_delete")(**kwargs)

    @Tool(
        "subagent_history",
        description="查看子代理任务的完整执行历史时间线（创建、运行、提问、完成/失败等事件）。排查任务为什么失败、回答「任务经历了什么」时使用；看当前状态用 subagent_status。",
        visibility="deferred",
        parameters=[
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID（支持完整 ID、唯一前缀或唯一标题）",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="任务所属聊天流 ID（必须是当前会话流）",
                required=True,
            ),
        ],
    )
    async def _tool_subagent_history(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("subagent_history")(**kwargs)

    @Tool(
        "subagent_schedule",
        description="创建定时/周期执行的子代理任务（cron 表达式），到点由后台自动执行，结果汇报到任务所在聊天流。用户要求「每天/每周定时」「定时提醒我」时使用；一次性任务用 subagent_create。注意：cron 频率不要过高（「* * * * *」表示每分钟执行，慎用）。",
        visibility="visible",
        parameters=[
            ToolParameterInfo(
                name="intent",
                param_type=ToolParamType.STRING,
                description="任务意图描述（到点后后台子代理要做什么）",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="任务所属聊天流 ID（必须是当前会话流，任务结果将汇报到此流）",
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
                description="执行级别：agent=由独立 Agent 自主推理执行（默认，适合多步骤复杂任务）；instant=立即执行单个动作（仅适合纯消息类任务，如定时提醒、自动回复）",
                enum_values=["instant", "agent"],
                required=False,
            ),
        ],
    )
    async def _tool_subagent_schedule(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("subagent_schedule")(**kwargs)

    @Tool(
        "send_message",
        description=SEND_MESSAGE_DESCRIPTION,
        visibility="deferred",
        parameters=_send_message_params(),
    )
    async def _tool_send_message(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("send_message")(**kwargs)

    @Tool(
        "list_mcp_tools",
        description="列出所有已连接的 MCP 服务器及其可用工具。需要外部能力（网页抓取、搜索等）时先用本工具了解有哪些可用工具，再调用 call_mcp_tool。",
        visibility="deferred",
    )
    async def _tool_list_mcp_tools(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("list_mcp_tools")(**kwargs)

    @Tool(
        "call_mcp_tool",
        description="调用 MCP 服务器的工具（如抓取网页、查询外部数据）。先用 list_mcp_tools 查看可用的服务器和工具列表；arguments 是 JSON 字符串或对象，参数结构由具体工具决定。",
        visibility="deferred",
        parameters=[
            ToolParameterInfo(
                name="server",
                param_type=ToolParamType.STRING,
                description="MCP 服务器名称",
                required=True,
            ),
            ToolParameterInfo(
                name="tool",
                param_type=ToolParamType.STRING,
                description="工具名称",
                required=True,
            ),
            ToolParameterInfo(
                name="arguments",
                param_type=ToolParamType.STRING,
                description='工具参数的 JSON 字符串（如 {"url": "https://..."}），不传则使用空参数',
                required=False,
            ),
        ],
    )
    async def _tool_call_mcp_tool(self, **kwargs: Any) -> dict[str, Any]:
        return await self._get_planner_tool("call_mcp_tool")(**kwargs)

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
        return await cmd_create(self, **kwargs)

    @Command(
        "maitask_list",
        description="列出任务",
        pattern=r"^/maitask\s+list\b",
        aliases=["/mt list"],
    )
    async def cmd_task_list(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await cmd_list(self, **kwargs)

    @Command(
        "maitask_status",
        description="查看任务状态",
        pattern=r"^/maitask\s+status\b",
        aliases=["/mt status"],
    )
    async def cmd_task_status(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await cmd_status(self, **kwargs)

    @Command(
        "maitask_cancel",
        description="取消任务",
        pattern=r"^/maitask\s+cancel\b",
        aliases=["/mt cancel"],
    )
    async def cmd_task_cancel(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await cmd_cancel(self, **kwargs)

    @Command(
        "maitask_history",
        description="查看任务历史",
        pattern=r"^/maitask\s+history\b",
        aliases=["/mt history"],
    )
    async def cmd_task_history(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await cmd_history(self, **kwargs)

    @Command(
        "maitask_ask",
        description="向任务注入指令",
        pattern=r"^/maitask\s+ask\b",
        aliases=["/mt ask"],
    )
    async def cmd_task_ask(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await cmd_ask(self, **kwargs)

    @Command(
        "maitask_help_fallback",
        description="maitask 命令帮助（兜底：拦截所有未匹配的 /maitask 输入）",
        pattern=r"^/maitask\b",
    )
    async def cmd_zz_task_fallback(self, **kwargs: Any) -> tuple[bool, str, int]:
        """兜底：任何 /maitask 开头的输入都显示帮助并拦截，避免落入 Maisaka planner。"""
        return await cmd_fallback(self, **kwargs)

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
        - platform → platform（MaiBot 序列化消息自带；部分宿主 session_id
          是不带平台前缀的裸 UUID，须显式传 platform 才能拼出 owner）
        - message_info.user_info.user_id → user_id
        - processed_plain_text → plain_text

        注意：MaiBot 1.1.3 中 ON_MESSAGE 事件已不触发，
        改用 chat.receive.after_process Hook（bot.py:724 真实触发）。
        """
        try:
            message = kwargs.get("message") or {}
            stream_id = str(message.get("session_id", "")) if isinstance(message, dict) else ""
            platform = str(message.get("platform", "")) if isinstance(message, dict) else ""
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
                platform=platform or None,
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
