"""oh-mai-agent 插件配置模型。

定义插件 config.toml 的 Pydantic 模型，供 MaiBot 插件 SDK 使用。
声明的 config_model 会被 Runner / Host 用于补齐默认值、生成 WebUI Schema，
插件内部通过 self.config 访问校验后的强类型配置对象。

配置结构由下方 Pydantic 模型定义，Runner 自动生成并校验 config.toml。
每个字段通过 ``json_schema_extra`` 提供 WebUI 表单的中文 label / hint，
与参考插件 snowluma_adapter 的 settings.py 保持一致（无 label 时 WebUI
回退显示英文字段名）。
"""

from __future__ import annotations

import logging
from typing import Literal

from maibot_sdk import Field, PluginConfigBase

logger = logging.getLogger(__name__)


class PluginSection(PluginConfigBase):
    """插件基本信息配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件", "hint": "是否启用插件", "order": 0},
    )
    config_version: str = Field(
        default="0.1.0",
        description="配置文件版本号",
        json_schema_extra={
            "label": "配置版本",
            "hint": "配置文件版本号",
            "disabled": True,
            "hidden": True,
            "order": 1,
        },
    )


class PermissionConfig(PluginConfigBase):
    """权限配置。"""

    __ui_label__ = "权限"
    __ui_order__ = 1

    admins: list[str] = Field(
        default_factory=list,
        description="管理员列表（按人），格式 platform:user_id",
        json_schema_extra={
            "label": "管理员",
            "hint": "管理员列表（按人），格式 platform:user_id",
            "order": 0,
        },
    )
    admin_groups: list[str] = Field(
        default_factory=list,
        description="管理群列表（按群），格式 platform:group:group_id，群内所有成员均为 admin",
        json_schema_extra={
            "label": "管理群",
            "hint": "管理群列表（按群），格式 platform:group:group_id，群内所有成员均为 admin",
            "order": 1,
        },
    )
    users: list[str] = Field(
        default_factory=list,
        description="用户列表（按人），格式 platform:user_id",
        json_schema_extra={
            "label": "用户",
            "hint": "用户列表（按人），格式 platform:user_id",
            "order": 2,
        },
    )
    user_groups: list[str] = Field(
        default_factory=list,
        description="用户群列表（按群），格式 platform:group:group_id，群内所有成员均为 user",
        json_schema_extra={
            "label": "用户群",
            "hint": "用户群列表（按群），格式 platform:group:group_id，群内所有成员均为 user",
            "order": 3,
        },
    )
    admin_in_group_chats: bool = Field(
        default=False,
        description="按人配置的 admin 在群聊等其他聊天流中是否生效（私聊无条件生效）",
        json_schema_extra={
            "label": "群聊中管理员生效",
            "hint": "按人配置的 admin 在群聊等其他聊天流中是否生效（私聊无条件生效）",
            "order": 4,
        },
    )


class TaskConfig(PluginConfigBase):
    """任务调度与执行配置。"""

    __ui_label__ = "任务"
    __ui_order__ = 2

    max_concurrent_tasks: int = Field(
        default=4,
        description="并发任务上限；0 = 不启动任何任务",
        json_schema_extra={
            "label": "并发任务上限",
            "hint": "并发任务上限；0 = 不启动任何任务",
            "order": 0,
        },
    )
    max_runtime_min: int = Field(
        default=0,
        description="运行中任务总时长兜底（分钟），0 = 不限；超时强制标记为 FAILED 并释放并发额度",
        json_schema_extra={
            "label": "运行时长兜底",
            "hint": "运行中任务总时长兜底（分钟），0 = 不限；超时强制标记为 FAILED 并释放并发额度",
            "order": 1,
        },
    )
    default_timeout_min: int = Field(
        default=10,
        description="ask_user 无回复挂起等待时间（分钟）——已声明，当前实现未读取该配置，任务保持挂起直至收到回复或被取消",
        json_schema_extra={
            "label": "提问等待超时",
            "hint": "ask_user 无回复挂起等待时间（分钟）——已声明，当前实现未读取该配置，任务保持挂起直至收到回复或被取消",
            "order": 2,
        },
    )
    persist_history: bool = Field(
        default=True,
        description="是否持久化完整任务历史（当前实现未读取该配置项，任务历史始终持久化）",
        json_schema_extra={
            "label": "持久化历史",
            "hint": "是否持久化完整任务历史（当前实现未读取该配置项，任务历史始终持久化）",
            "order": 3,
        },
    )


class PlannerBoardConfig(PluginConfigBase):
    """Planner 看板配置。"""

    __ui_label__ = "Planner看板"
    __ui_order__ = 3

    enabled: bool = Field(
        default=True,
        description="是否向 Planner 注入任务摘要",
        json_schema_extra={
            "label": "启用看板",
            "hint": "是否向 Planner 注入任务摘要",
            "order": 0,
        },
    )
    max_active: int = Field(
        default=5,
        description="活跃任务（running/waiting_input/paused）条数上限",
        json_schema_extra={
            "label": "活跃任务上限",
            "hint": "活跃任务（running/waiting_input/paused）条数上限",
            "order": 1,
        },
    )
    max_scheduled: int = Field(
        default=3,
        description="即将触发的定时任务条数上限",
        json_schema_extra={
            "label": "定时任务上限",
            "hint": "即将触发的定时任务条数上限",
            "order": 2,
        },
    )
    max_recent: int = Field(
        default=3,
        description="最近完成任务条数上限",
        json_schema_extra={
            "label": "最近任务上限",
            "hint": "最近完成任务条数上限",
            "order": 3,
        },
    )


class PolishConfig(PluginConfigBase):
    """回复润色配置。"""

    __ui_label__ = "润色"
    __ui_order__ = 4

    use_jargon: bool = Field(
        default=True,
        description="润色时机械匹配黑话（复刻 MaiBot jargon_context_matcher）",
        json_schema_extra={
            "label": "匹配黑话",
            "hint": "润色时机械匹配黑话（复刻 MaiBot jargon_context_matcher）",
            "order": 0,
        },
    )


class SplitterConfig(PluginConfigBase):
    """回复分割配置。"""

    __ui_label__ = "回复分割"
    __ui_order__ = 5

    enable: bool = Field(
        default=True,
        description="是否把长回复拆成多条消息发送（复刻 MaiBot response_splitter 思路的确定性版本）",
        json_schema_extra={
            "label": "启用回复分割",
            "hint": "是否把长回复拆成多条消息发送（复刻 MaiBot response_splitter 思路的确定性版本）",
            "order": 0,
        },
    )
    max_length: int = Field(
        default=1000,
        ge=50,
        description="单条消息目标最大长度（字符）；无标点的超长句会被硬切",
        json_schema_extra={
            "label": "单条最大长度",
            "hint": "单条消息目标最大长度（字符）；无标点的超长句会被硬切",
            "order": 1,
        },
    )
    max_messages: int = Field(
        default=5,
        ge=1,
        description="一次回复最多拆成几条消息；超过时尾部合并进最后一条",
        json_schema_extra={
            "label": "最多分割条数",
            "hint": "一次回复最多拆成几条消息；超过时尾部合并进最后一条",
            "order": 2,
        },
    )


class MCPServerConfig(PluginConfigBase):
    """单个 MCP 服务器配置。"""

    name: str = Field(
        default="",
        description="MCP 服务器名称（标识用）",
        json_schema_extra={
            "label": "名称",
            "hint": "MCP 服务器名称（标识用）",
            "order": 0,
        },
    )
    transport: Literal["stdio", "http", "sse"] = Field(
        default="stdio",
        description="传输协议：stdio / http / sse",
        json_schema_extra={
            "label": "传输协议",
            "hint": "传输协议：stdio / http / sse",
            "order": 1,
        },
    )
    command: str = Field(
        default="",
        description="启动命令（transport=stdio 时使用）",
        json_schema_extra={
            "label": "启动命令",
            "hint": "启动命令（transport=stdio 时使用）",
            "order": 2,
        },
    )
    args: list[str] = Field(
        default_factory=list,
        description="命令行参数列表",
        json_schema_extra={
            "label": "命令行参数",
            "hint": "命令行参数列表",
            "order": 3,
        },
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="环境变量",
        json_schema_extra={
            "label": "环境变量",
            "hint": "环境变量",
            "order": 4,
        },
    )
    url: str = Field(
        default="",
        description="服务器 URL（transport=http/sse 时使用）",
        json_schema_extra={
            "label": "服务器 URL",
            "hint": "服务器 URL（transport=http/sse 时使用）",
            "order": 5,
        },
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="HTTP 请求头",
        json_schema_extra={
            "label": "HTTP 请求头",
            "hint": "HTTP 请求头",
            "order": 6,
        },
    )


class MCPConfig(PluginConfigBase):
    """MCP 协议配置。"""

    __ui_label__ = "MCP"
    __ui_order__ = 6

    enabled: bool = Field(
        default=True,
        description="是否启用 MCP 工具",
        json_schema_extra={
            "label": "启用 MCP",
            "hint": "是否启用 MCP 工具",
            "order": 0,
        },
    )
    fetch_enabled: bool = Field(
        default=True,
        description="是否启用内置 fetch MCP 服务器（mcp-server-fetch，抓取网页并转 Markdown）",
        json_schema_extra={
            "label": "内置 fetch",
            "hint": "是否启用内置 fetch MCP 服务器（mcp-server-fetch，抓取网页并转 Markdown）",
            "order": 1,
        },
    )
    exa_enabled: bool = Field(
        default=True,
        description="是否启用内置 exa.ai MCP 服务器（远程 web 搜索）",
        json_schema_extra={
            "label": "内置 exa.ai",
            "hint": "是否启用内置 exa.ai MCP 服务器（远程 web 搜索）",
            "order": 2,
        },
    )
    servers: list[MCPServerConfig] = Field(
        default_factory=list,
        description="自定义 MCP 服务器列表（在内置 fetch / exa 之外追加）",
        json_schema_extra={
            "label": "自定义服务器",
            "hint": "自定义 MCP 服务器列表（在内置 fetch / exa 之外追加）",
            "order": 3,
        },
    )


class ApiExposeConfig(PluginConfigBase):
    """API 暴露等级配置。"""

    __ui_label__ = "API暴露"
    __ui_order__ = 7

    max_level: Literal["guest", "user", "admin"] = Field(
        default="user",
        description="本插件 API 最大暴露等级（当前实现未读取该配置，6 个端点均 public=True，见 api_expose.py）",
        json_schema_extra={
            "label": "最大暴露等级",
            "hint": "本插件 API 最大暴露等级（当前实现未读取该配置，6 个端点均 public=True，见 api_expose.py）",
            "order": 0,
        },
    )


class SearchConfig(PluginConfigBase):
    """搜索配置。"""

    __ui_label__ = "搜索"
    __ui_order__ = 8

    max_results: int = Field(
        default=20,
        description="search_users 返回条数上限",
        json_schema_extra={
            "label": "搜索结果上限",
            "hint": "search_users 返回条数上限",
            "order": 0,
        },
    )


class SubAgentConfig(PluginConfigBase):
    """子 Agent 配置。"""

    __ui_label__ = "子Agent"
    __ui_order__ = 9

    enabled: bool = Field(
        default=True,
        description="是否启用子 Agent 工具（ask_subagent / ask_subagents）",
        json_schema_extra={
            "label": "启用子Agent",
            "hint": "是否启用子 Agent 工具（ask_subagent / ask_subagents）",
            "order": 0,
        },
    )
    max_rounds: int = Field(
        default=10,
        ge=1,
        description="子 Agent 最大执行轮数",
        json_schema_extra={
            "label": "子Agent最大轮数",
            "hint": "子 Agent 最大执行轮数",
            "order": 1,
        },
    )
    max_result_chars: int = Field(
        default=8000,
        description="子 Agent 答案最大字符数，超长截断",
        json_schema_extra={
            "label": "答案长度上限",
            "hint": "子 Agent 答案最大字符数，超长截断",
            "order": 2,
        },
    )
    max_parallel_subagents: int = Field(
        default=3,
        ge=1,
        description="ask_subagents 单次批量派发的子 Agent 数量上限",
        json_schema_extra={
            "label": "并行子Agent上限",
            "hint": "ask_subagents 单次批量派发的子 Agent 数量上限",
            "order": 3,
        },
    )


class ShellConfig(PluginConfigBase):
    """宿主机命令执行工具配置。"""

    __ui_label__ = "Shell命令"
    __ui_order__ = 10

    enabled: bool = Field(
        default=True,
        description="是否启用 run_command 命令执行工具（仅 admin 可调用，Discoverable 层按需发现）",
        json_schema_extra={
            "label": "启用命令执行",
            "hint": "是否启用 run_command 命令执行工具（仅 admin 可调用，Discoverable 层按需发现）",
            "order": 0,
        },
    )
    timeout_seconds: int = Field(
        default=60,
        ge=1,
        description="命令默认超时（秒）；超时后强制终止整个进程树",
        json_schema_extra={
            "label": "默认超时（秒）",
            "hint": "命令默认超时（秒）；超时后强制终止整个进程树",
            "order": 1,
        },
    )
    max_output_chars: int = Field(
        default=8000,
        ge=100,
        description="stdout/stderr 单侧最大返回字符数，超长截断",
        json_schema_extra={
            "label": "输出长度上限",
            "hint": "stdout/stderr 单侧最大返回字符数，超长截断",
            "order": 2,
        },
    )


class MaibotAgentConfig(PluginConfigBase):
    """oh-mai-agent 插件完整配置。"""

    __ui_label__ = "oh-mai-agent"

    plugin: PluginSection = Field(
        default_factory=PluginSection,
        description="插件基本信息",
    )
    permission: PermissionConfig = Field(
        default_factory=PermissionConfig,
        description="权限配置",
    )
    task: TaskConfig = Field(
        default_factory=TaskConfig,
        description="任务配置",
    )
    planner_board: PlannerBoardConfig = Field(
        default_factory=PlannerBoardConfig,
        description="Planner 看板配置",
    )
    polish: PolishConfig = Field(
        default_factory=PolishConfig,
        description="回复润色配置",
    )
    splitter: SplitterConfig = Field(
        default_factory=SplitterConfig,
        description="回复分割配置",
    )
    mcp: MCPConfig = Field(
        default_factory=MCPConfig,
        description="MCP 协议配置",
    )
    api_expose: ApiExposeConfig = Field(
        default_factory=ApiExposeConfig,
        description="API 暴露等级配置",
    )
    search: SearchConfig = Field(
        default_factory=SearchConfig,
        description="搜索配置",
    )
    subagent: SubAgentConfig = Field(
        default_factory=SubAgentConfig,
        description="子 Agent 配置",
    )
    shell: ShellConfig = Field(
        default_factory=ShellConfig,
        description="命令执行工具配置",
    )
