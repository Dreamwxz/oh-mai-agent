# 跨插件 API 与动态工具生成

## 设计目标

MaiBot 的插件生态里，插件之间需要协作。oh-mai-agent 掌握着完整的任务管理能力：创建、查询、取消、注入指令、查看历史。这些能力对其他插件是有价值的。比如一个翻译插件想让 Agent 离线处理长文本，或者一个定时插件想按 cron 创建任务，它们不该各自重复实现一套任务系统，而应该直接调用 oh-mai-agent 的现成能力。

于是跨插件 API 体系要解决两个方向的问题：

- **向外暴露**：让其他 MaiBot 插件能调用 oh-mai-agent 的任务管理 API（`api_expose.py` 的 `build_api_handlers()`）。
- **向内扫描**：让 oh-mai-agent 的 Agent 循环能调用其他插件暴露的 API（`tools/agent/plugin_api_tools.py` 动态生成 `call_{api}` 工具）。

两条链路在功能上对称：暴露层回答"让别的插件能调用我"，动态工具层回答"让 Agent 能调用别的插件"。它们都建立在 MaiBot Plugin SDK 的 `ctx.api` 机制上，一个注册、一个扫描。两条链路在实现上相互独立：暴露层由插件加载时的组装步骤驱动，扫描层由 TaskManager 的工具注册步骤驱动，各自维护自己的生命周期。

## 设计方案

### 向外暴露：build_api_handlers 构建 6 个端点

`build_api_handlers(task_manager)`（`api_expose.py`）是暴露层的核心。它接收 TaskManager 门面，返回 6 个端点描述符的列表，每个描述符包含 name / description / version / public / handler 五个字段；handler 由统一的 `_wrap_handler` 工厂构建（提取参数 → 调用 → 结果映射 → 异常兜底），端点只声明差异。

每个 handler 是 `async def _xxx(**kwargs)` 闭包，遵循同一套处理模式：从 kwargs 提取参数 → 类型安全转换（`_to_int`、`_parse_status`）→ 调用 TaskManager 门面方法 → 返回统一的 `{"success": bool, ...}` 结构，失败时附带 `"error"` 或 `"message"` 字段。以 create 为例（`api_expose.py:127-198`）：解析 intent / owner / platform / stream_id 等参数，然后调用 `task_manager.create_task(...)`，成功返回 `{"success": True, "task_id": ..., "title": ..., "level": ...}`。create 端点不接受 `level` 参数——INSTANT 仅由定时任务与 Agent 模型显式创建，跨插件 API 创建的任务固定为 agent 级。

参数处理是防御性的：所有关键参数经 `str()` 强制转字符串，数值参数经 `_to_int(val, default)` 安全转换，缺失时落到默认值（如 `limit` 默认 50）；每个 handler 外层都有 `try/except Exception` 兜底，任意异常转为 `{"success": False, "error": str(exc)}`，不让异常穿透到 SDK 调用方。

一个关键设计决策是 `_caller_role = Role.ADMIN`（api_expose.py 的 `_CALLER_ROLE`）。所有 handler 都以 ADMIN 身份调用 TaskManager，因为跨插件调用被视为受信任的内部通信，不应被面向用户的 owner 权限检查阻拦——MaiBot 的插件均为部署者手动安装的代码，插件间互信是架构前提。6 个端点描述符的 `"public"` 字段全部为 `True`，对所有插件无门槛可见（Host 侧 `_is_api_visible_to_plugin` 仅凭 `entry.plugin_id == caller_plugin_id or entry.public` 判定）。这个信任模型沿 Agent 侧 `call_{api}` 工具链继承后产生一个已知边界，详见「已知限制」。

### 组装位置：lifecycle.py 第 10 步

端点的注册发生在插件加载的组装阶段。`load_plugin()` 的第 10 步（`lifecycle.py:141-150`）调用 `build_api_handlers()`，遍历返回的描述符逐个 `register_dynamic_api()`，最后 `sync_dynamic_apis()` 把 6 个端点同步到插件宿主。此后其他插件即可通过 `ctx.api.call("create", ...)` 调用。

### TaskManager 门面：底层操作的唯一入口

6 个 handler 调用的 `create_task` / `list_tasks` / `get_task` / `modify_task` / `cancel_task` / `task_history` 都是 TaskManager 门面方法（`core/task_manager.py:255-370`）。门面内部转发给 TaskCrud / TaskControl 两个 usecase，api_expose 与命令系统、Planner 工具共享同一套门面，不直接接触 usecase 层。这样跨插件 API 与 `/task` 命令、Planner 工具走的是同一条业务路径，行为一致。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁到独立子进程，后回退到进程内 contextvars + usecase 分层。TaskManager 从编排器降级为门面 + 组装器，跨插件 API 的调用点不变，底层逻辑下沉到 usecases。

### 向内扫描：call_{api} 动态工具

Agent 循环内要调用其他插件的 API，不能预先把所有工具写死，因为插件生态是动态的。设计选择是运行时扫描：`TaskManager.setup()` 的第 6 步（`core/task_manager.py:213-222`）调用 `refresh_plugin_api_tools(ctx_api)`（`tools/agent/plugin_api_tools.py:67-135`），扫描 `ctx.api.list()` 返回的可见 API 列表，为每个 API 生成一个 `ToolDefinition`：

- 工具名 `call_{api_name}`（API 名中的 `.` 替换为 `_`）
- 参数统一为松散的 `args` 对象，键值对应 API 参数
- `visibility="discoverable"`，归入 Discoverable 层，Agent 经 `list_tools` 按需发现
- `min_role=Role.USER`

生成的工具注册进 ToolRegistry（`core/task_manager.py:218-219`）。工具 handler 是 `_build_handler` 闭包（`plugin_api_tools.py:65-89`），内部执行 `ctx.api.call(api_name, **args)` 透传目标 API，dict 结果直接返回，其他类型包装为 `{"success": True, "result": ...}`，异常转为 `{"success": False, "error": ...}` 不向上抛出，让 Agent 循环把失败当作普通工具输出处理。

扫描逻辑做了多层容错：`ctx.api.list()` 返回 list 或 `{"apis": [...]}` 都能归一化（`_normalize_api_list`，`plugin_api_tools.py`），缺 `api_name` 的条目跳过，扫描异常回退为空列表不阻断工具注册。生产环境使用异步版 `refresh_plugin_api_tools()`，由 `TaskManager.setup()` 在 async 上下文中调用。

## 使用与配置

### 端点清单

6 个端点由 `build_api_handlers()` 构建，经 `lifecycle.py:141-150` 注册为 SDK 动态 API，其他插件通过 `ctx.api.call(api_name, **kwargs)` 调用：

| 端点 | 描述 | 关键参数 | 成功返回 |
|---|---|---|---|
| create | 创建新任务 | intent / owner / platform / stream_id，可选 level / delay_seconds / cron_expr / priority / reply_stream_id | task_id / title / level |
| list | 列出任务摘要 | owner，可选 status / limit（默认 50） | tasks / count |
| get | 查看任务详情 | task_id / owner | task（TaskRecord.to_dict()） |
| cancel | 取消任务 | task_id / owner | message |
| inject | 向运行中任务注入指令 | task_id / instruction / owner | message |
| history | 查看任务执行历史 | task_id / owner，可选 limit（默认 50） | history |

所有端点返回统一的 `{"success": bool, ...}` 结构，失败时附带 `"error"` 或 `"message"` 字段。inject 端点与命令系统的 `/task ask` 共享 TaskManager 的 `modify_task` 路径（见 [13-commands.md](./13-commands.md)）。

以创建任务为例，其他插件的一次完整调用如下：

```
ctx.api.call("create", intent="整理聊天记录", owner="qq:1",
             platform="qq", stream_id="qq:g:1", level="agent")
# → 返回 {"success": True, "task_id": "...", "title": "...", "level": "agent"}
```

### Agent 侧调用其他插件

与向外暴露对称，Agent 侧经动态工具向内调用。`TaskManager.setup()` 注册完成后，Agent 在循环中先 `list_tools` 发现 `call_{api_name}` 工具，再通过 `get_tool_schema` 获取参数定义，最后把目标 API 的参数放进 `args` 对象调用。`_build_handler` 闭包（`plugin_api_tools.py:65-89`）把调用转发为 `ctx.api.call(api_name, **args)`，结果按规则透传或包装后返回给 Agent 循环。

两类 API 存在形式的对照：

| 来源 | 调用者 | 工具/端点名 | 可见性 | 角色门槛 |
|---|---|---|---|---|
| 本插件 `build_api_handlers()` | 其他 MaiBot 插件 | `create` / `list` / `get` / `cancel` / `inject` / `history` | public=True（SDK 动态 API） | 无（ADMIN 执行） |
| 其他插件 `ctx.api.list()` | 本插件 Agent 循环 | `call_{api_name}` | discoverable（工具注册表） | USER |

### 配置

跨插件 API 不再有配置节。历史 `[api_expose]` 配置节（含 `max_level` 字段，默认 `"user"`）声明了「API 最大暴露等级」，但跨插件调用不携带调用方角色、Host 侧 `public` 也只是二元可见性开关，等级过滤无法实现，该配置节已从 `config.py` 整体废弃。存量 `config.toml` 中的 `[api_expose]` 节会被静默忽略（`PluginConfigBase` 配置模型 `extra="ignore"`），无需迁移。

### 已知限制

以下限制经代码验证，详见 [04-permission.md](./04-permission.md)：

- **`max_level` 已废弃移除**：历史配置字段声明但未强制执行（`build_api_handlers` 也不接收 config 参数），6 个端点始终 `public=True`。因 SDK 模型无法表达「调用方等级」，字段已删除，不再有「配置形同虚设」的问题。
- **handler 硬编码 ADMIN 执行（有意设计）**：所有 handler 以 `Role.ADMIN` 调用 TaskManager，owner 权限检查被旁路。这是跨插件调用「受信任内部通信」的设计决策，门控责任在部署侧（只安装可信插件）。
- **call_* 工具继承 ADMIN 执行（已知边界）**：`refresh_plugin_api_tools` 把本插件 6 个端点也包装成 `call_{api}` 工具（min_role=USER），user 级 Agent 经 `call_list`（空 owner）可见全部任务、经 `call_cancel` / `call_pause` / `call_inject` 可控制任意任务——owner 隔离被端点内部的 ADMIN 执行绕过。原生 Agent 工具（tools/agent/task_mgmt.py）是 owner 隔离的，两条路径行为不一致。收紧方向：在 `refresh_plugin_api_tools` 的包装过程中排除本插件自身的敏感端点（create / cancel / pause / inject），使 Agent 只能经原生工具按 owner 隔离操作任务。

整体影响：用户经聊天命令、Agent 经原生工具这两条路径都有完整的角色解析与 owner 校验；跨插件调用（受信任插件）与 call_* 工具（user 级 Agent）两条路径以 ADMIN 执行，前者是设计使然，后者是当前最值得注意的边界。

### 与工具系统的关系

`call_{api}` 工具以 `visibility="discoverable"` 注入工具注册表，是 Discoverable 层工具来源之一（见 [05-tools.md](./05-tools.md)）。Agent 循环内通过 `list_tools` 发现、`get_tool_schema` 获取参数定义后即可调用，调用时把目标 API 的参数放进 `args` 对象传入。