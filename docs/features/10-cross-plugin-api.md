# 跨插件 API 与动态工具生成

## 设计目标

MaiBot 的插件生态里，插件之间需要协作。oh-mai-agent 掌握着完整的任务管理能力：创建、查询、取消、注入指令、查看历史。这些能力对其他插件是有价值的。比如一个翻译插件想让 Agent 离线处理长文本，或者一个定时插件想按 cron 创建任务，它们不该各自重复实现一套任务系统，而应该直接调用 oh-mai-agent 的现成能力。

于是跨插件 API 体系要解决两个方向的问题：

- **向外暴露**：让其他 MaiBot 插件能调用 oh-mai-agent 的任务管理 API（`api_expose.py` 的 `build_api_handlers()`）。
- **向内扫描**：让 oh-mai-agent 的 Agent 循环能调用其他插件暴露的 API（`tools/agent/plugin_api_tools.py` 动态生成 `call_{api}` 工具）。

两条链路在功能上对称：暴露层回答"让别的插件能调用我"，动态工具层回答"让 Agent 能调用别的插件"。它们都建立在 MaiBot Plugin SDK 的 `ctx.api` 机制上，一个注册、一个扫描。两条链路在实现上相互独立：暴露层由插件加载时的组装步骤驱动，扫描层由 TaskManager 的工具注册步骤驱动，各自维护自己的生命周期。

## 设计方案

### 向外暴露：build_api_handlers 构建 6 个端点

`build_api_handlers(task_manager, resolver, config)`（`api_expose.py:87-416`）是暴露层的核心。它接收 TaskManager 门面、权限解析器和完整配置，返回 6 个端点描述符的列表，每个描述符包含 name / description / version / public / handler 五个字段。

每个 handler 是 `async def _xxx(**kwargs)` 闭包，遵循同一套处理模式：从 kwargs 提取参数 → 类型安全转换（`_to_int`、`_parse_level`、`_parse_status`）→ 调用 TaskManager 门面方法 → 返回统一的 `{"success": bool, ...}` 结构，失败时附带 `"error"` 或 `"message"` 字段。以 create 为例（`api_expose.py:127-198`）：解析 intent / owner / platform / stream_id 等参数，校验 level 合法性，然后调用 `task_manager.create_task(...)`，成功返回 `{"success": True, "task_id": ..., "title": ..., "level": ...}`。

参数处理是防御性的：所有关键参数经 `str()` 强制转字符串，数值参数经 `_to_int(val, default)` 安全转换，缺失时落到默认值（如 `limit` 默认 50）；`level` 为空时返回 `None`，由 TaskManager 自动判定任务级别；每个 handler 外层都有 `try/except Exception` 兜底，任意异常转为 `{"success": False, "error": str(exc)}`，不让异常穿透到 SDK 调用方。

一个关键设计决策是 `_caller_role = Role.ADMIN`（`api_expose.py:124`）。所有 handler 都以 ADMIN 身份调用 TaskManager，因为跨插件调用被视为受信任的内部通信，不应被面向用户的 owner 权限检查阻拦。这个决策的代价是权限门控责任完全推给了外部调用方，而外部门控函数并未接入，详见已知限制。6 个端点描述符的 `"public"` 字段全部硬编码为 `True`（`api_expose.py:378/385/392/399/406/413`），意味着所有端点对 SDK 注册层无门槛可见。

### 组装位置：lifecycle.py 第 10 步

端点的注册发生在插件加载的组装阶段。`load_plugin()` 的第 10 步（`lifecycle.py:141-150`）调用 `build_api_handlers()`，遍历返回的描述符逐个 `register_dynamic_api()`，最后 `sync_dynamic_apis()` 把 6 个端点同步到插件宿主。此后其他插件即可通过 `ctx.api.call("create", ...)` 调用。

### TaskManager 门面：底层操作的唯一入口

6 个 handler 调用的 `create_task` / `list_tasks` / `get_task` / `modify_task` / `cancel_task` / `task_history` 都是 TaskManager 门面方法（`core/task_manager.py:255-370`）。门面内部转发给 TaskCrud / TaskControl 两个 usecase，api_expose 与命令系统、Planner 工具共享同一套门面，不直接接触 usecase 层。这样跨插件 API 与 `/task` 命令、Planner 工具走的是同一条业务路径，行为一致。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁到独立子进程，后回退到进程内 contextvars + usecase 分层。TaskManager 从编排器降级为门面 + 组装器，跨插件 API 的调用点不变，底层逻辑下沉到 usecases。

### 向内扫描：call_{api} 动态工具

Agent 循环内要调用其他插件的 API，不能预先把所有工具写死，因为插件生态是动态的。设计选择是运行时扫描：`TaskManager.setup()` 的第 6 步（`core/task_manager.py:213-222`）调用 `refresh_plugin_api_tools(ctx_api)`（`tools/agent/plugin_api_tools.py:192-265`），扫描 `ctx.api.list()` 返回的可见 API 列表，为每个 API 生成一个 `ToolDefinition`：

- 工具名 `call_{api_name}`（API 名中的 `.` 替换为 `_`）
- 参数统一为松散的 `args` 对象，键值对应 API 参数
- `visibility="discoverable"`，归入 Discoverable 层，Agent 经 `list_tools` 按需发现
- `min_role=Role.USER`

生成的工具注册进 ToolRegistry（`core/task_manager.py:218-219`）。工具 handler 是 `_build_handler` 闭包（`plugin_api_tools.py:65-89`），内部执行 `ctx.api.call(api_name, **args)` 透传目标 API，dict 结果直接返回，其他类型包装为 `{"success": True, "result": ...}`，异常转为 `{"success": False, "error": ...}` 不向上抛出，让 Agent 循环把失败当作普通工具输出处理。

扫描逻辑做了多层容错：`ctx.api.list()` 返回 list 或 `{"apis": [...]}` 都能归一化（`_normalize_api_list`，`plugin_api_tools.py:25-36`），缺 `api_name` 的条目跳过，扫描异常回退为空列表不阻断工具注册。另有同步版 `build_plugin_api_tools()`（`plugin_api_tools.py:159-189`）供非 async 环境使用，内部经 `_run_coroutine_sync()` 执行扫描，在已有事件循环中安全失败（避免跨线程破坏 SDK 的主循环 IPC）。生产环境实际使用的是异步版 `refresh_plugin_api_tools()`，由 `TaskManager.setup()` 在 async 上下文中调用。

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

`[api_expose]` 配置节（`config.py:323-330`）声明了 `max_level` 字段，默认 `"user"`，设计意图是低于此等级的调用方角色不可访问 API。**当前实现未读取该配置**，详见已知限制。

### 已知限制

跨插件 API 体系的权限门控是未完成部分，以下限制经代码验证，详见 [04-permission.md](./04-permission.md)：

- **`max_level` 声明但未强制执行**：`build_api_handlers()` 接收 `config` 参数但从不读取 `config.api_expose.max_level`，6 个端点无论配置何值都以 `public=True` 注册。
- **`check_api_call_permission` 是死代码**：`check_api_call_permission(role, max_level)`（`api_expose.py:29-65`）实现了完整的角色 vs 等级比较逻辑，但全仓库无任何调用点。
- **handler 硬编码 ADMIN 绕过权限检查**：所有 handler 以 `Role.ADMIN` 调用 TaskManager，owner 权限检查被旁路。这是有意设计（跨插件调用视为受信任内部通信），但门控责任完全落在未接入的外部门控函数上。

整体影响：用户经聊天命令、Agent 经工具调用这两条路径都有完整的角色解析与校验，唯独其他插件经 `ctx.api.call()` 的路径无角色门控，端点 `public=True` 无门槛暴露，handler 以 ADMIN 执行。这是跨插件 API 体系当前最突出的安全缺口。

### 与工具系统的关系

`call_{api}` 工具以 `visibility="discoverable"` 注入工具注册表，是 Discoverable 层工具来源之一（见 [05-tools.md](./05-tools.md)）。Agent 循环内通过 `list_tools` 发现、`get_tool_schema` 获取参数定义后即可调用，调用时把目标 API 的参数放进 `args` 对象传入。