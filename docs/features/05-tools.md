# 工具系统

本文档讲述工具系统的设计逻辑：为什么 Agent 循环需要两级工具呈现，工具如何注册与发现，以及 agent 循环、planner、synthetic 三个通道如何分工。

## 设计目标

Agent 级任务的核心是 LLM 推理加工具调用。每轮 LLM 调用都要把工具 schema 放进 `tools` 参数，schema 越多，token 成本越高，LLM 的注意力也越分散。如果把所有工具都常驻上下文，几十个 schema 会挤占本就有限的上下文窗口。

同时，工具是 Agent 触达外部世界的唯一通道，权限边界必须清晰。不同角色（guest / user / admin）能看到的工具不同，主 Planner 与 Agent 循环能调用的工具也不同。

所以工具系统要解决两个问题：

1. **上下文控制**：常驻工具数量必须受控，其余工具按需发现。
2. **安全隔离**：工具按角色过滤，危险工具只对高权限角色、只在 Agent 循环内可见。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配，回退到进程内 contextvars + usecase 分层。工具系统随之全部留在 Runner 进程内；合成发现工具的逻辑体也从 `executor/agent_loop.py` 迁出，独立成 `tools/synthetic/discovery.py` 通道。

## 设计方案

### 两级呈现：Essential 常驻，Discoverable 按需

每个工具是一个 `ToolDefinition`（tools/registry.py:25-70），携带名称、描述、参数 JSON Schema、异步 handler，以及两个控制字段：`visibility`（essential / discoverable）和 `min_role`（最低调用角色）。

- **Essential 层**：schema 始终携带在每轮 LLM 调用的 `tools` 参数中。当前代码中唯一的 Essential 工具是 `ask_user`（tools/agent/ask_tool.py:108-115，`visibility="essential"`）。提问是 Agent 与用户交互的核心能力，必须随时可用，不能等发现。
- **Discoverable 层**：schema 不直接携带，Agent 通过合成工具按需发现。信息检索、文件读写、消息发送、任务管理、跨插件 API、MCP 工具全部在这一层。

### 合成发现工具

Discoverable 层的入口是两个始终呈现的合成工具（tools/synthetic/discovery.py）：

- `list_tools`：列出当前角色可见的所有 discoverable 工具名与描述（`handle_list_tools`，56-71）。
- `get_tool_schema`：按名称返回完整 JSON Schema，并把该工具加入已加载集合（`handle_get_tool_schema`，74-110）。非 discoverable 工具（88-97）或角色不足（99-104）都会被拒绝。

合成工具不注册进 ToolRegistry，而是由 AgentLoop 在工具分发时特判调用（executor/agent_loop.py:502-513）：`list_tools` / `get_tool_schema` / `ask_user` 走内置 handler，其余走 `registry.execute(name, role, **args)`。每轮构建 schema 时（`_build_tool_schemas`，agent_loop.py:127-150），Essential 工具、两个合成工具、已加载的 discoverable 工具三部分拼装成 `tools` 参数。

### 三通道分工

工具按调用方分成三个通道。

**Agent 循环通道（tools/agent/）**。Agent 在离线循环中自主调用的全部工具，注册进 ToolRegistry，经 `TaskManager.setup()`（core/task_manager.py:148-226）按顺序注册：任务管理 → 信息 → 文件 → ask_user → send_message → 跨插件 API。

- 任务管理（tools/agent/task_mgmt.py:25-46）：`list_my_tasks` / `create_subtask` / `inject_task` 三个 discoverable 工具，全部从 `current_task` ContextVar 读取当前任务上下文取 owner。`inject_task` 要求 owner 匹配或 ADMIN（151）。
- 信息获取（tools/agent/info_tools.py:33）：6 个 discoverable 工具（search_memory / fetch_history / query_person / search_users / get_frequency / list_plugin_tools），GUEST 可访问。
- 文件读写（tools/agent/file_tools.py:50）：`read_file` / `write_file`，user 级隔离到 `data_dir/files/` 沙箱，admin 可开 `admin_open` 绕过。
- 提问（tools/agent/ask_tool.py:22）：`ask_user`，唯一 Essential 工具。
- 消息发送（tools/agent/send_tool.py:26）：`send_message` 升级版，按 group_id/user_id 建流 + 润色 + 重试。
- 跨插件 API（tools/agent/plugin_api_tools.py）：扫描 `ctx.api.list()` 动态生成 `call_{api_name}` 工具。

**Planner 通道（tools/planner/）**。主 Planner 通过 9 个 `@Tool` 装饰器（plugin.py:132/165/222/246/267/297/318/339/376）看到的安全子集。handler 全部懒构建（`_get_planner_tool`，plugin.py:112-126）：`search_users` 与 `send_message` 走独立工厂，7 个 `task_*` 经 `build_task_tools(self._task_manager)`（tools/planner/task_tools.py:30-311）从 TaskManager 门面取。Planner 调用者角色恒为 ADMIN（`_planner_caller_role`，task_tools.py:25-27），owner 标识为 `planner:{stream_id}`（20-22）。

**Synthetic 通道（tools/synthetic/）**。`list_tools` / `get_tool_schema` 两个发现工具，见上文。

### 权限过滤

工具在呈现和执行两个阶段都按角色过滤：

- 呈现阶段：`ToolRegistry.names(role)` / `list_essential(role)` / `list_discoverable(role)`（tools/registry.py:129-173）用 `PermissionResolver.require(role, min_role)` 过滤可见工具。
- 发现阶段：`get_tool_schema` 再次校验角色（discovery.py:99-104）。
- 执行阶段：`registry.execute(name, role, **kwargs)`（tools/registry.py:177-205）执行前二次门控，权限不足返回 `permission denied`。
- 文件工具还有第三道防线：`FileAccessPolicy` 沙箱（tools/agent/file_tools.py:50），role_provider 来自 `current_task` ContextVar，攻击者无法伪造角色。

**安全设计**：暴露给 Planner 的 9 个 @Tool 是任务管理安全子集。文件写操作、宿主机操作等危险工具只在 Agent 循环内可用，Planner 即使被提示词注入也无法写宿主机文件。

## 使用与配置

### 9 个 @Tool 清单

| # | 工具名 | 装饰器位置 | 功能 |
|---|---|---|---|
| 1 | `search_users` | plugin.py:132 | 按昵称/名字/ID 搜索用户，返回 user_id、昵称、群信息 |
| 2 | `task_create` | plugin.py:165 | 创建任务（支持延迟/cron） |
| 3 | `task_list` | plugin.py:222 | 列出当前流任务（可按状态过滤） |
| 4 | `task_status` | plugin.py:246 | 查询单个任务详情 |
| 5 | `task_modify` | plugin.py:267 | 向运行中任务注入指令 |
| 6 | `task_delete` | plugin.py:297 | 取消/删除任务 |
| 7 | `task_history` | plugin.py:318 | 查看任务执行历史 |
| 8 | `task_schedule` | plugin.py:339 | 创建定时任务（cron 表达式） |
| 9 | `send_message` | plugin.py:376 | 向好友/群发送消息（自动建流 + 润色 + 重试） |

### 工具权限

- guest：仅可调用 `min_role=GUEST` 的工具（信息查询类）。
- user：增加文件读写（沙箱内）、消息发送、任务管理等。
- admin：最高权限，可访问所有工具；`admin_open=True` 时可访问宿主机文件系统。

### 配置影响

- `[mcp] enabled`：关闭后 MCP 工具不注册。MCP 工具在 `load_plugin` 第 8 步初始化（lifecycle.py:122-129），配置热更新走 `reload_mcp_if_changed`（lifecycle.py:288-314）。详见 [MCP 集成](./08-mcp.md)。
- `[search] max_results`：`search_users` 返回条数上限（tools/agent/info_tools.py:33 与 tools/planner/search_users.py:43）。
- `[task]` 文件沙箱：user 级文件工具隔离到 `data_dir/files/`（core/task_manager.py:164）。

### 已知限制

- `ask_user` 无超时机制：`resume_event.wait()`（agent_loop.py:213）无限等待，配置中的 `default_timeout_min` 尚未生效。详见 [AI 提问](./07-ask-user.md)。
- `render_html2png` 在历史 IMPLEMENTATION_PLAN 中提及但未实现。
- 合成工具不注册进 ToolRegistry，由 AgentLoop 特判分发，外部无法直接复用。

### 相关文档

- [权限模型](./04-permission.md)：`PermissionResolver.require()` 门控所有工具的呈现与执行。
- [AI 提问](./07-ask-user.md)：`ask_user`，Essential 层唯一工具。
- [MCP 集成](./08-mcp.md)：MCP 工具动态注册到 ToolRegistry。
- [提示词系统](./12-prompt.md)：System prompt 告知 LLM 可用工具及两级呈现规则。