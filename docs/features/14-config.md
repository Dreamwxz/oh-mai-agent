# 配置体系

## 设计目标

插件有大量可调参数：并发上限、权限名单、MCP 服务器、看板条数、润色开关。这些参数散落在各功能模块里，如果每个模块各自读配置，用户要改一处行为就得翻多个文件。配置体系要解决的就是这个问题：把全部可调项收拢到一个地方，统一声明、统一校验、统一暴露。

设计目标有三条：

- **开箱即用**。所有配置项都有默认值，Runner 启动时自动生成 `config.toml`，用户不编辑任何文件也能跑起来。
- **强类型校验**。配置错误在加载时暴露，而不是等运行时才炸。字段类型由 Pydantic 保证，插件内部拿到的是校验后的强类型对象。
- **WebUI 可编辑**。MaiBot WebUI 根据配置模型自动生成表单，用户改配置不用手写 TOML。

## 设计方案

### Pydantic 数据模型

配置模型全部定义在 `config.py`，共 12 个 Pydantic 类，分三层：

- **10 个配置节类**，对应 `config.toml` 的 10 个节：`PluginSection`（config.py:23）、`PermissionConfig`（:47）、`TaskConfig`（:100）、`PlannerBoardConfig`（:144）、`PolishConfig`（:188）、`SplitterConfig`（:205）、`MCPConfig`（:310）、`ApiExposeConfig`（:354）、`SearchConfig`（:371）、`SubAgentConfig`（:388）。
- **1 个嵌套模型** `MCPServerConfig`（config.py:242），描述单个 MCP 服务器，作为 `MCPConfig.servers` 列表的元素。
- **1 个根模型** `MaibotAgentConfig`（config.py:434），聚合上述 10 节，是插件对外声明的完整配置。

每个类继承 `maibot_sdk.PluginConfigBase`。字段用 `Field(default=..., description=...)` 声明默认值和中文描述，`json_schema_extra` 提供 WebUI 表单的中文 `label` / `hint`（无 label 时 WebUI 回退显示英文字段名），`__ui_label__` 提供 WebUI 表单的分组名。`Literal[...]` 类型（如 `transport`、`max_level`）会让 WebUI 渲染成下拉框。

### config.toml 生成与校验

`plugin.py:42` 将 `MaibotAgentConfig` 声明为插件的 `config_model`。Runner 启动时读取该模型，做三件事：

1. 若 `config.toml` 不存在，用模型默认值补齐生成；
2. 校验已有文件的字段类型和结构；
3. 把字段的 `description` 和 `__ui_label__` 暴露给 WebUI 生成表单。

插件内部通过 `self.config` 访问校验后的 `MaibotAgentConfig` 实例，字段类型由 Pydantic 保证。

### 热更新

配置变更无需重启插件。SDK 在调用 `on_config_update`（plugin.py:68）前已刷新 `plugin.config`，插件侧由 `apply_config_update`（lifecycle.py:160-217）把新值传播到运行时组件：

1. 重建 `PermissionResolver`，权限变更立即生效；
2. `scheduler.update_config` 更新并发上限与超时参数；
3. `task_manager.update_config` 刷新配置引用；
4. `reload_mcp_if_changed` 按需重启 MCP 客户端；
5. 重建 `PlannerBoard`，清空 hash 去重状态。

任一步失败只记日志，不向 SDK 抛出，避免影响插件整体运行。

## 使用与配置

### 10 节配置项详解

以下按 10 个配置节列出全部字段。配置键与 `config.py` 字段一一对应。

#### `[plugin]` — 插件

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | `bool` | `true` | 是否启用插件 |
| `config_version` | `str` | `"0.1.0"` | 配置文件版本号 |

#### `[permission]` — 权限

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `admins` | `list[str]` | `[]` | 管理员列表（按人），格式 `platform:user_id` |
| `admin_groups` | `list[str]` | `[]` | 管理群列表（按群），格式 `platform:group:group_id` |
| `users` | `list[str]` | `[]` | 用户列表（按人），格式 `platform:user_id` |
| `user_groups` | `list[str]` | `[]` | 用户群列表（按群），格式 `platform:group:group_id` |
| `admin_in_group_chats` | `bool` | `false` | 按人配置的 admin 在群聊中是否生效（私聊无条件生效） |

权限判定顺序：admin（人/群）> user（人/群）> guest（未匹配）。详见 [权限模型](./04-permission.md)。

#### `[task]` — 任务

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_concurrent_tasks` | `int` | `4` | 并发任务上限，超出部分按优先级排队 |
| `max_runtime_min` | `int` | `0` | agent 任务总时长兜底（分钟），`0` = 不限 |
| `default_timeout_min` | `int` | `10` | `ask_user` 无回复挂起等待时间（分钟），已声明但当前未执行 |
| `persist_history` | `bool` | `true` | 是否持久化完整任务历史（当前实现始终持久化，未读取该开关） |

`scheduled` 状态任务创建不受并发限制，但触发运行时仍计入并发计数。详见 [调度器](./02-scheduler.md)。

#### `[planner_board]` — Planner 看板

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | `bool` | `true` | 是否向 Planner 注入任务摘要 |
| `max_active` | `int` | `5` | 活跃任务（running / waiting_input / paused）条数上限 |
| `max_scheduled` | `int` | `3` | 即将触发的定时任务条数上限 |
| `max_recent` | `int` | `3` | 最近完成任务条数上限 |

每类任务条数超过上限时按优先级截断。详见 [Planner 看板](./09-planner-board.md)。

#### `[polish]` — 润色

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `use_jargon` | `bool` | `true` | 润色时机械匹配黑话（复刻 MaiBot `jargon_context_matcher`） |

其他润色参数（消息条数、黑话条数上限等）直接跟随 MaiBot 全局配置，不提供插件级覆盖。

#### `[splitter]` — 回复分割

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enable` | `bool` | `true` | 是否把长回复拆成多条消息发送（复刻 MaiBot `response_splitter` 思路的确定性版本） |
| `max_length` | `int` | `1000` | 单条消息目标最大长度（字符），无标点的超长句会被硬切（`ge=50`） |
| `max_messages` | `int` | `5` | 一次回复最多拆成几条消息，超过时尾部合并进最后一条（`ge=1`） |

分割在 `send_final_reply` 润色之后进行，按行/句末标点确定性切分，保留原文不丢内容；四条回复路径（instant 任务、agent 完成回复、send_message 工具、失败通知）统一生效。`send_message` 工具的 `split` 参数可按单次调用覆盖（`false` 时整条发送）。详见 [回复润色](./06-polish.md)。

#### `[mcp]` — MCP

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | `bool` | `true` | 是否启用 MCP 工具 |
| `fetch_enabled` | `bool` | `true` | 是否启用内置 fetch MCP 服务器 |
| `exa_enabled` | `bool` | `true` | 是否启用内置 exa.ai MCP 服务器 |
| `servers` | `list[MCPServerConfig]` | `[]`（自定义追加；内置 exa/fetch 预设见 08-mcp.md） | MCP 服务器列表 |

插件内置 exa（远程 web 搜索，http）与 fetch（本地网页抓取，stdio）两个预设 MCP 服务器，
由 `fetch_enabled` / `exa_enabled` 开关控制，开箱即用；`servers` 默认空列表，仅用于自定义
追加，与预设同名的条目会替代预设连接。详见 [MCP 工具集成](./08-mcp.md)。`servers` 列表中的
每个元素为 `MCPServerConfig`（config.py 的 `MCPServerConfig`），字段如下：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | `str` | `""` | MCP 服务器名称（标识用） |
| `transport` | `Literal["stdio", "http", "sse"]` | `"stdio"` | 传输协议 |
| `command` | `str` | `""` | 启动命令（`transport="stdio"` 时使用） |
| `args` | `list[str]` | `[]` | 命令行参数列表 |
| `env` | `dict[str, str]` | `{}` | 环境变量键值对 |
| `url` | `str` | `""` | 服务器 URL（`transport="http"` / `"sse"` 时使用） |
| `headers` | `dict[str, str]` | `{}` | HTTP 请求头 |

MCP 工具进入 Agent 工具集的 Discoverable 层，按权限过滤。不支持运行时动态增删服务器。详见 [MCP 工具集成](./08-mcp.md)。

#### `[api_expose]` — API 暴露

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_level` | `Literal["guest", "user", "admin"]` | `"user"` | 本插件 API 最大暴露等级，已声明但当前未执行 |

#### `[search]` — 搜索

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_results` | `int` | `20` | `search_users` 返回条数上限 |

#### `[subagent]` — 子 Agent

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | `bool` | `true` | 是否启用子 Agent 工具（ask_subagent / ask_subagents），`false` 时两个工具都不注册 |
| `max_rounds` | `int` | `10` | 子 Agent 最大执行轮数（`ge=1`） |
| `max_result_chars` | `int` | `8000` | 子 Agent 答案最大字符数，超长截断 |
| `max_parallel_subagents` | `int` | `3` | ask_subagents 单次批量派发的子 Agent 数量上限（`ge=1`） |

子 Agent 由主 Agent 通过工具调用触发，结果回传主 Agent 上下文继续判断；配置经 config_getter 热更新，无需重注册。详见 [子 Agent](./15-subagent.md)。

### 已知限制

- **`default_timeout_min` 已配置但未执行**（config.py:124-131）。`TaskConfig.default_timeout_min` 声明了 `ask_user` 无回复挂起等待时间的意图，但调度器未读取该值：`waiting_input` 状态的任务不因超时而自动取消或推进，而是永久挂起直到用户回复或手动取消。
- **`api_expose.max_level` 声明未执行**（config.py:354-361）。`ApiExposeConfig.max_level` 声明了暴露等级约束，但 `api_expose.py` 的 `build_api_handlers()` 未读取该值做调用方过滤，6 个端点全部 `public=True`。用户将 `max_level` 设为 `"admin"` 不会改变实际行为。