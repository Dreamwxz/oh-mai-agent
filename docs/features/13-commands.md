# 命令系统

`/maitask` 命令组是 oh-mai-agent 面向用户的直接操作入口。任务模型、调度器、执行器都在 Runner 进程内自主运转，但用户需要一个不依赖 Planner 的、确定性的方式来创建和干预任务，这就是 `/maitask` 命令存在的理由。

## 设计目标

任务系统的大部分能力（创建、调度、执行、持久化）都藏在后台，用户能感知到的只有聊天流。要让用户真正"用得上"任务，必须有一条从聊天输入直达任务管理的路径，而且这条路径要满足三个要求：

- **确定性**：命令的匹配、权限、返回文本都是写死的逻辑，不经过 LLM 推理，结果可预期。
- **不干扰主链路**：`/maitask` 输入被拦截在消息链之外（返回值第三项 `intercept_message_level=2`），不会落入 Maisaka Planner 被当作对话意图处理。
- **权限可控**：任务涉及创建、取消、注入指令等敏感操作，必须按 guest / user / admin 三级角色分级放行。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配回退到进程内 contextvars + usecase 分层。命令层始终是进程内薄壳，不涉及跨进程通信。

## 设计方案

### 7 个 @Command 薄壳

命令注册在 plugin.py 的 `@Command` 装饰器区（`maitask_create` ~ `maitask_help_fallback`），共 7 个 `@Command` 装饰器：6 个用户命令（create / list / status / cancel / history / ask）加 1 个兜底帮助命令 `maitask_help_fallback`。每个装饰器只声明命令名、描述、正则 pattern 和别名（如 `/mt create`），方法体只有一行委托：

```python
async def cmd_task_create(self, **kwargs: Any) -> tuple[bool, str, int]:
    return await self._cmd_create(**kwargs)
```

薄壳本身不含任何业务逻辑。真正的实现在 `commands.py` 的模块级函数里（`cmd_create` / `cmd_list` / `cmd_status` / `cmd_cancel` / `cmd_history` / `cmd_ask` / `cmd_fallback`），plugin.py 的 `_cmd_*` 包装只是转发。这样做的原因是把命令处理从插件类中剥离出来，保持 plugin.py 只做注册与接线。

命令与 @Tool 是任务管理的两条平行入口：@Tool 面向主 Planner（Planner 经 LLM 决策调用，调用者恒为 ADMIN），`/maitask` 命令面向聊天流里的真实用户（调用者角色由权限配置决定）。两者最终都汇聚到同一个 `TaskManager` 门面，因此对任务的操作语义完全一致，只是入口和权限来源不同。

### 统一执行链

每个 handler 遵循同一条链路：

1. `resolve_caller()`（commands.py 模块级函数）从 kwargs 提取 stream_id / user_id / platform，推断群聊，调用 `PermissionResolver.resolve_role()` 得到调用者角色。
2. 按命令做权限检查（如 `cmd_create` 要求 `Role.USER` 以上）。
3. 参数提取：`cmd_text()` 兼容 `text` / `plain_text` 两种键名，`cmd_arg()` 从 `matched_groups` 取正则捕获组，缺失时回退到文本 `re.sub` 解析。
4. 委托给 `plugin._task_manager.*` 门面方法。
5. `cmd_reply()` 把回复文本发回聊天流，失败只记 warning，不影响返回状态。

所有 handler 的返回值都是 `(bool, str, int)` 三元组：成功与否、回复文本、拦截级别 2。

以 `/maitask create 帮我查天气` 为例，完整链路是：SDK 按注册顺序匹配 `^/maitask\s+create\b` → 调用 `cmd_task_create` 薄壳 → `_cmd_create` 转发 `cmd_create` → `resolve_caller` 解析角色 → `require(role, Role.USER)` 权限检查 → `plugin._task_manager.create_task` → TaskCrud 落库并 `scheduler.enqueue()` → `cmd_reply` 把任务摘要发回聊天流。整条链路不经过 LLM，任何一步失败都返回确定性的中文错误文案。

### 门面委托与底层归属

commands.py 不直接接触 usecase 层，一律经 `TaskManager` 门面（core/task_manager.py 的 `TaskManager` 类）。门面内部再转发给两个 usecase：

- **TaskCrud**（core/usecases/task_crud.py）：持久化 CRUD，提供 `create_task` / `list_tasks` / `get_task` / `modify_task` / `cancel_task` / `task_history`。
- **TaskControl**（core/usecases/task_control.py）：执行控制。`/maitask ask` 的注入指令最终经 `TaskControl.handle_injection` 向命令总线发送 `INJECT_INSTRUCTION`，由 AgentLoop 消费。

### 权限映射

| 命令 | 权限 | 说明 |
|---|---|---|
| create | user+ | 显式 `require(role, Role.USER)`，guest 拒绝 |
| list / status / history | guest+ | 命令层无门控，TaskCrud 内部按 caller_role / owner 做数据级过滤 |
| cancel / ask | owner / admin | 只能操作自己的任务，admin 可操作任意任务 |

### 兜底帮助

`maitask_help_fallback`（plugin.py 的 `@Command` 装饰器区）的 pattern 是 `^/maitask\b`，比前 6 条命令的 `\b` 后缀模式更宽。因为注册顺序靠后，只有前面的精确匹配全部失败时才会命中，输出固定帮助文本（commands.py 的 `cmd_fallback`）并拦截，避免未匹配的 `/maitask` 输入落入 Planner。

这个兜底命令解决的是一个实际问题：用户输入 `/maitask 随便什么` 时，如果没有任何精确 pattern 命中，消息会按普通对话流入 Planner，被当作闲聊意图处理。兜底命令用最宽的 pattern 兜住所有 `/maitask` 前缀输入，保证命令组对用户输入要么执行、要么给帮助，绝不漏进主链路。

## 使用与配置

### 命令表

| 命令 | 用法 | 权限 | 说明 |
|---|---|---|---|
| `/maitask create <意图>` | 创建任务 | user+ | 返回任务 ID（前 8 位）、标题、级别、状态 |
| `/maitask list [-all] [状态]` | 列出任务 | guest+（`-all` 仅 admin） | 可按状态过滤，最多 20 条 |
| `/maitask status <ID>` | 查看任务详情 | guest+ | 支持完整 ID 或前缀匹配 |
| `/maitask cancel <ID>` | 取消任务 | owner / admin | 支持完整 ID 或前缀匹配 |
| `/maitask history <ID>` | 查看执行历史 | guest+ | 展示最近 10 条 |
| `/maitask ask <ID> <指令>` | 注入指令 | owner / admin | 向运行中任务注入指令 |
| `/maitask help`（兜底） | 显示帮助 | 无限制 | 任何未匹配的 `/maitask` 输入都显示帮助 |

`-all` 标志仅对 admin 生效：列出全部任务（含 Planner 创建的定时任务），可写作 `/maitask list -all [状态]` 或 `/maitask list [状态] -all`；非 admin 输入 `-all` 会被静默忽略，仍只列出自己的任务。

所有命令都支持 `/mt` 别名（如 `/mt create`）。命令的权限判定依赖 `[permission]` 配置节，详见 [权限模型](./04-permission.md)。

> 迁移说明：命令组已由 `/task` 更名为 `/maitask`，别名 `/t` 同步改为 `/mt`。旧 `/task` 命令不再支持（breaking change），输入旧命令格式的消息将作为普通对话流入 Planner，不会被命令组拦截。

命令的返回文本经统一发送入口（`send_final_reply`，commands.py 的 `cmd_reply`）以 `polish=False` 直发原文，**不经过 LLM 润色**——命令是排障/操作场景，任务 ID、状态等关键信息不能被改写。但命令响应与任务回复共享同一发送实现，因此同样获得指数退避重试、静默掉包检测与上下文记录保障。这意味着命令响应是即时、原始、确定性的，与任务回复的"加工后"风格形成对照——区别只在"是否润色"，发送可靠性是一致的。

### 已知限制

- **命令解析强绑定 SDK**：参数提取依赖 SDK 的 `text` / `plain_text` 键名和 `matched_groups` 捕获组，SDK 升级变更传参格式时需同步维护 `cmd_text()` / `cmd_arg()` 的兼容逻辑。
- **命令帮助为静态文本**：兜底帮助文本写死在 `cmd_fallback` 中，新增命令后需手动同步。
- **参数校验无统一框架**：每个 handler 自行实现参数提取与校验，错误文案措辞不完全统一。
- **无法动态禁用命令**：命令在类定义阶段由装饰器一次性注册，运行时无法按配置开关禁用某个子命令。

### 相关文档

- [任务模型](./01-task-model.md)：`TaskStatus` 枚举被 `/maitask list` 的状态过滤引用
- [权限模型](./04-permission.md)：命令权限判定的底层实现
- [跨插件 API](./10-cross-plugin-api.md)：任务管理的另一条入口（API 端点）
- [提示词系统](./12-prompt.md)：注入指令经 prompt builder 格式化后进入 Agent 循环