# 工具系统

本文档讲述工具系统的设计逻辑：Agent 循环如何呈现与调用工具、工具如何注册与按角色过滤，以及 agent 循环、planner、synthetic 三个通道如何分工。

## 设计目标

Agent 级任务的核心是 LLM 推理加工具调用。每轮 LLM 调用都要把工具 schema 放进 `tools` 参数，schema 越多，token 成本越高，LLM 的注意力也越分散。工具数量受角色过滤约束：guest 只见查询类，admin 见全部，规模始终可控。

同时，工具是 Agent 触达外部世界的唯一通道，权限边界必须清晰。不同角色（guest / user / admin）能看到的工具不同，主 Planner 与 Agent 循环能调用的工具也不同。

所以工具系统要解决两个问题：

1. **可用性优先**：Agent 循环**直接全量暴露**当前角色可见的工具（Essential + Discoverable 全部进 `tools` 参数），让 LLM 看到真实可调用的工具名（如 `mcp_fetch_fetch`），避免「发现结果与可调用集不一致」导致的 tool-not-found 空转。
2. **安全隔离**：工具按角色过滤，危险工具只对高权限角色可见。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配，回退到进程内 contextvars + usecase 分层。工具系统随之全部留在 Runner 进程内；早期依赖 list_tools / get_tool_schema 按需发现的呈现方式已改为直接全量暴露（合成发现工具降级为兜底，见下文）。

## 设计方案

### 两级呈现：Essential 与 Discoverable 均直接暴露

每个工具是一个 `ToolDefinition`（tools/registry.py:25-70），携带名称、描述、参数 JSON Schema、异步 handler，以及两个控制字段：`visibility`（essential / discoverable）和 `min_role`（最低调用角色）。

- **Essential 层**：schema 始终携带在每轮 LLM 调用的 `tools` 参数中。当前代码中唯一的 Essential 工具是 `ask_user`（tools/agent/ask_tool.py:108-115，`visibility="essential"`）。提问是 Agent 与用户交互的核心能力，必须随时可用。
- **Discoverable 层**：Agent 循环每轮把当前角色可见的全部 discoverable 工具**直接**放进 `tools` 参数（`_build_tool_schemas`，executor/agent_loop.py:127-150），不再要求 LLM 先经发现工具枚举再按名加载——LLM 看到的工具名即注册表真实名称。信息检索、文件读写、消息发送、任务管理、跨插件 API、MCP 工具全部在这一层，按 `min_role` 过滤后暴露。

### 合成发现工具（兜底，不再进入 schema）

`list_tools` / `get_tool_schema`（tools/synthetic/discovery.py）曾是 Discoverable 层的必经入口，现已被直接全量暴露取代，**不再出现在 Agent 循环每轮的 schema 中**（避免噪音与误导）：

- `list_tools`：列出当前角色可见的所有 discoverable 工具名与描述（`handle_list_tools`，56-71）。
- `get_tool_schema`：按名称返回完整 JSON Schema（`handle_get_tool_schema`，74-110）。非 discoverable 工具（88-97）或角色不足（99-104）都会被拒绝。

两者仍由 AgentLoop 在工具分发时特判调用（executor/agent_loop.py:502-513）作为**兜底兼容**：历史会话恢复或 LLM 残余调用时返回真实工具清单，不产生误导性空转。

### 三通道分工

工具按调用方分成三个通道。

**Agent 循环通道（tools/agent/）**。Agent 在离线循环中自主调用的全部工具，注册进 ToolRegistry，经 `TaskManager.setup()`（core/task_manager.py 的 `setup`）按顺序注册：任务管理 → 信息 → 文件 → ask_user → send_message → 跨插件 API → 子 Agent → 命令执行。

- 任务管理（tools/agent/task_mgmt.py:25-46）：`list_my_tasks` / `create_subtask` / `inject_task` 三个 discoverable 工具，全部从 `current_task` ContextVar 读取当前任务上下文取 owner。`inject_task` 要求 owner 匹配或 ADMIN（151）。
- 信息获取（tools/agent/info_tools.py:33）：5 个 discoverable 工具（search_memory / fetch_history / query_person / search_users / get_frequency），GUEST 可访问。原 `list_plugin_tools` 已移除：它经 `ctx.tool.get_definitions()` 列出 MaiBot 宿主侧全量工具（含插件 planner 层 `list_mcp_tools` / `call_mcp_tool` 等），这些名字在 Agent 循环注册表不可调用，曾导致 LLM 照单调用后反复 tool-not-found 空转。
- 文件读写（tools/agent/file_tools.py:50）：`read` / `write`，user 级隔离到 `data_dir/files/` 沙箱，admin 可开 `admin_open` 绕过。
- 提问（tools/agent/ask_tool.py:22）：`ask_user`，唯一 Essential 工具。
- 消息发送（tools/send_message.py：`build_send_tool`）：`send_message`，目标三选一 —— `stream_id` 直发指定聊天流（如其他用户的流，跳过建流）或 `group_id`/`user_id` 建流，默认润色 + 长文本分割，`polish`/`split` 可选项按场景关闭。**宿主上下文剥离**：MaiBot Host 调用工具时会向 kwargs 注入当前会话上下文（`stream_id`/`chat_id`/`group_id`/`user_id`/`platform`，且仅当 LLM 未提供该键时注入，见 MaiBot `component_query.py` 的 `_build_tool_context_payload`）。`chat_id` 是宿主专用字段（schema 无此参数），且宿主注入的 `stream_id` 恒等于 `chat_id` —— `_send_message_core` 以此为指纹剥离宿主注入的会话上下文，避免「目标流」与「当前会话流」同名冲突（LLM 传 `group_id` 时宿主补 `stream_id`、传 `stream_id` 时宿主补 `group_id` 的误报「只能提供其一」）。
- 跨插件 API（tools/agent/plugin_api_tools.py）：扫描 `ctx.api.list()` 动态生成 `call_{api_name}` 工具。
- 命令执行（tools/agent/shell_tools.py 的 `build_shell_tools`）：`run_command`，跨平台（Windows 自动用 cmd.exe，Linux/macOS 用 /bin/sh），仅 admin 可调用，超时强杀进程树 + 输出截断。详见 [命令执行](./16-shell.md)。

**Planner 通道（tools/planner/）**。主 Planner 通过 11 个 `@Tool` 装饰器（plugin.py:138/166/219/242/264/292/314/336/371/426/434）看到的安全子集。handler 全部懒构建（`_get_planner_tool`，plugin.py:112-132）：`search_users` 走独立工厂，`send_message` 与 Agent 循环版共用 `tools/send_message.py` 的实现，7 个 `task_*` 经 `build_task_tools(self._task_manager)`（tools/planner/task_tools.py:30-311）从 TaskManager 门面取，`list_mcp_tools` / `call_mcp_tool` 两个 MCP 代理工具经 `tools/planner/mcp_tools.py` 的工厂函数构建。Planner 调用者角色恒为 ADMIN（`_planner_caller_role`，task_tools.py:25-27），owner 标识为 `planner:{stream_id}`（20-22）。

**Synthetic 通道（tools/synthetic/）**。`list_tools` / `get_tool_schema` 两个发现工具，现为兜底兼容（不再进入 Agent 循环 schema），见上文。

### 权限过滤

工具在呈现和执行两个阶段都按角色过滤：

- 呈现阶段：`ToolRegistry.names(role)` / `list_essential(role)` / `list_discoverable(role)`（tools/registry.py:129-173）用 `PermissionResolver.require(role, min_role)` 过滤可见工具；Agent 循环直接暴露过滤结果。
- 发现阶段（兜底）：`get_tool_schema` 仍会再次校验角色（discovery.py:99-104）。
- 执行阶段：`registry.execute(name, role, **kwargs)`（tools/registry.py:177-205）执行前二次门控，权限不足返回 `permission denied`。
- 文件工具还有第三道防线：`FileAccessPolicy` 沙箱（tools/agent/file_tools.py:50），role_provider 来自 `current_task` ContextVar，攻击者无法伪造角色。

**安全设计**：暴露给 Planner 的 11 个 @Tool 是任务管理安全子集（含 MCP 代理工具）。文件写操作、宿主机命令执行等危险工具只在 Agent 循环内可用且仅对 admin 可见（`run_command` 还从子 Agent 允许集中排除），Planner 即使被提示词注入也无法写宿主机文件或执行命令。

## 使用与配置

### 11 个 @Tool 清单

| # | 工具名 | 装饰器位置 | 功能 |
|---|---|---|---|
| 1 | `search_users` | plugin.py:138 | 按昵称/名字/ID 搜索用户，返回 user_id、昵称、群信息 |
| 2 | `task_create` | plugin.py:166 | 创建任务（支持延迟/cron） |
| 3 | `task_list` | plugin.py:219 | 列出当前流任务（可按状态过滤） |
| 4 | `task_status` | plugin.py:242 | 查询单个任务详情 |
| 5 | `task_modify` | plugin.py:264 | 向运行中任务注入指令 |
| 6 | `task_delete` | plugin.py:292 | 取消/删除任务 |
| 7 | `task_history` | plugin.py:314 | 查看任务执行历史 |
| 8 | `task_schedule` | plugin.py:336 | 创建定时任务（cron 表达式） |
| 9 | `send_message` | plugin.py:371 | 向好友/群/指定聊天流发送消息（目标三选一，默认润色 + 长文本分割） |
| 10 | `list_mcp_tools` | plugin.py:426 | 列出所有已连接的 MCP 服务器及其可用工具 |
| 11 | `call_mcp_tool` | plugin.py:434 | 调用 MCP 服务器的工具 |

通过 `list_mcp_tools` + `call_mcp_tool` 两个代理工具，Planner 可以**发现和调用**所有已配置的 MCP 工具，无需为每个 MCP 工具单独注册 `@Tool`。

### 工具权限

- guest：仅可调用 `min_role=GUEST` 的工具（信息查询类）。
- user：增加文件读写（沙箱内）、消息发送、任务管理等。
- admin：最高权限，可访问所有工具；`admin_open=True` 时可访问宿主机文件系统，且是 `run_command` 命令执行工具的唯一可见角色。

### 配置影响

- `[mcp] enabled`：关闭后 MCP 工具不注册。MCP 工具在 `load_plugin` 第 8 步初始化（lifecycle.py:122-129），配置热更新走 `reload_mcp_if_changed`（lifecycle.py:288-314）。详见 [MCP 集成](./08-mcp.md)。
- `[search] max_results`：`search_users` 返回条数上限（tools/agent/info_tools.py:33 与 tools/planner/search_users.py:43）。
- `[task]` 文件沙箱：user 级文件工具隔离到 `data_dir/files/`（core/task_manager.py:164）。
- `[shell] enabled`：关闭后 `run_command` 不注册（core/task_manager.py 的 `setup`）；`timeout_seconds` / `max_output_chars` 经 config_getter 热更新。详见 [命令执行](./16-shell.md)。

### 已知限制

- `ask_user` 无超时机制：`resume_event.wait()`（agent_loop.py:213）无限等待，配置中的 `default_timeout_min` 尚未生效。详见 [AI 提问](./07-ask-user.md)。
- `render_html2png` 在历史 IMPLEMENTATION_PLAN 中提及但未实现。
- 合成工具不注册进 ToolRegistry，由 AgentLoop 特判分发，外部无法直接复用。

### 相关文档

- [权限模型](./04-permission.md)：`PermissionResolver.require()` 门控所有工具的呈现与执行。
- [AI 提问](./07-ask-user.md)：`ask_user`，Essential 层唯一工具。
- [MCP 集成](./08-mcp.md)：MCP 工具动态注册到 ToolRegistry。
- [命令执行](./16-shell.md)：`run_command` 跨平台命令执行工具的权限与运行期防护。
- [提示词系统](./12-prompt.md)：System prompt 告知 LLM 可用工具及直接呈现规则。