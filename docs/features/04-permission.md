# 权限模型与 API 暴露层

oh-mai-agent 用 guest / user / admin 三级角色划分任务操作与工具访问权限。权限按聊天流上下文解析，同一人在私聊和群聊中可能拿到不同角色。

## 设计目标（为什么需要三级权限？）

任务系统面向多用户，核心矛盾是**多用户隔离**：普通用户只能创建和管理自己的任务，不能越权查看他人的任务；管理员需要全量控制。文件工具直接触碰宿主机文件系统，访问范围必须按角色收敛，否则任意用户都能读写任意路径。

三级角色的定位：

| 角色 | 能力 |
|---|---|
| guest | 只读：查看任务列表 / 详情 / 历史 |
| user | 创建与调度任务、管理自己的任务、`data_dir/files/` 沙箱内文件读写 |
| admin | 全量：所有任务、宿主机任意路径文件、插件配置 |

角色既可以按人授予（`platform:user_id`），也可以按群授予（`platform:group:group_id`）。按群配置解决群聊场景的粗粒度授权：群内所有人直接获得对应角色，无需逐个登记。

权限解析必须**按聊天流进行**，而不是按人全局固定。原因在于同一个人的身份在不同场景下含义不同：私聊里他是管理员本人，群聊里他可能只是群成员。因此解析的输入是 platform / user_id / stream_id / is_group 四元组，输出是该人在该流中的角色。未命中任何规则时默认 GUEST，这是 fail-closed 设计：宁可少给权限，也不默认放开。

跨插件 API 暴露层是权限模型的第三个应用面。除聊天流内的用户外，其他 MaiBot 插件也可能需要创建、查询或取消任务，这些操作经动态 API 端点完成。对它们同样需要等级门控，只是门控的对象从"人"变成了"调用方插件"。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁移到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配回退到进程内方案。权限逻辑随此次重构下沉：owner 检查从 TaskManager 内联代码移入 TaskCrud usecase（core/usecases/task_crud.py），任务执行上下文统一收口到 `executor/context.py` 的 `current_task` ContextVar，工具层角色判定经由该上下文获取。

## 设计方案

### 角色模型与判定链

`Role` 枚举（permission.py:31-36）按权限升序定义三级：GUEST < USER < ADMIN。大小比较统一走 `PermissionResolver.require(role, minimum)` 静态方法（permission.py:83-93），内部以整数序 0/1/2 比较，未知角色一律判为不通过。所有权限检查点都复用这一个比较函数，避免各处自行实现大小判断导致口径漂移。

`PermissionResolver`（permission.py:39-173）回答核心问题：**这个人在这个聊天流里是什么角色？** 输入 platform / user_id / stream_id / is_group，按固定优先级逐级匹配，命中即返回，不合并多级结果：

1. **按人 admin**（permission.py:143-154）：私聊无条件 ADMIN，这是私聊保证，管理员在私聊里必须能管理一切；群聊受 `admin_in_group_chats` 开关约束，开关关闭时降级为 USER。开关存在的理由是群聊里管理员身份可能被冒充，默认不信任。
2. **管理群**（permission.py:157-159）：群内所有人 ADMIN，不受开关影响。按群配置是显式声明，信任度高于按人配置在群聊中的推断。
3. **按人 user**、**用户群** 依次匹配。
4. 全部未命中则默认 GUEST。

### 三层执行入口

权限检查分布在三个入口，各自面向不同的调用方，覆盖从"人发命令"到"Agent 自主调用工具"的完整链路。

**用户交互层。** /task 命令与 planner 工具经 TaskManager 门面进入 TaskCrud（core/usecases/task_crud.py）。创建任务对 guest 直接拦截（task_crud.py:67）；查看 / 修改 / 取消 / 注入等操作要求 owner 匹配或 ADMIN，owner 精确比较在 `resolve_task` 解析成功后于各方法内完成（task_crud.py:139-166）。解析支持完整 ID、唯一前缀与唯一标题，但权限校验始终在解析之后执行，标题兜底不会绕过 owner 隔离。这一层是同步的、有明确调用者的，角色由命令上下文直接解析。planner 工具是例外：主 Planner 被视为受信任的宿主组件，`_planner_caller_role` 恒返回 ADMIN（tools/planner/task_tools.py:25-27），不参与聊天流角色解析。

**Agent 执行层。** 离线任务由 LLM 自主执行，工具调用需要知道"当前任务属于谁、以什么角色执行"。答案来自角色回调：`make_role_provider`（executor/context.py:16-31）从 task.owner / task.platform / task.stream_id 构造 `() -> Role` 回调，AgentExecutor 经 `_make_role_provider` 注入 AgentLoop（executor/agent.py:110-119），每轮执行前解析一次，resolver 缺失时回退 GUEST。角色绑定在任务上而非调用者上，因为 Agent 循环里没有"当前用户"，只有"当前任务"。

**工具层。** 每个 ToolDefinition 带 `min_role` 字段，schema 呈现时按角色过滤（tools/registry.py:135），`registry.execute(name, role)` 执行时二次校验（tools/registry.py:198），绕过呈现直接调用也会被拦下。文件工具在此基础上再加 `FileAccessPolicy` 沙箱（tools/agent/file_tools.py:50）：user 角色限制在 `data_dir/files/` 内，admin 可访问宿主机任意路径。文件的角色回调来自 `TaskManager._current_task_role`，读的是 `current_task` ContextVar（executor/context.py:11-13），与 Agent 执行层共用同一份上下文。

### 跨插件 API 的信任模型

6 个跨插件端点（create / list / get / cancel / inject / history）由 `build_api_handlers`（api_expose.py 的 `build_api_handlers`）构建，在插件加载第 10 步注册到 SDK 动态 API。

信任模型：**跨插件 API 面向受信任插件**。MaiBot 的插件均为部署者手动安装的代码，插件间互信是架构前提，因此所有 handler 统一以 `_CALLER_ROLE = Role.ADMIN`（api_expose.py 的 `_CALLER_ROLE`）调用 TaskManager——TaskCrud / TaskControl 的 owner / guest 检查被有意旁路，`public=True` 使端点对全部插件可见可调。历史版本曾声明 `[api_expose].max_level` 配置用于限制暴露等级，但跨插件调用不携带调用方角色（SDK 的 `api.call` 只传 `api_name/version/args`，Host 侧 `public` 也只是「是否对其他插件开放」的二元开关），等级概念无处安放，该字段已废弃移除，见「[api_expose] 配置节」。

Agent 侧还有一组对应的调用工具：`refresh_plugin_api_tools`（tools/agent/plugin_api_tools.py）动态扫描 `ctx.api.list()`，把每个可见 API 包装成 `call_{api}` 工具，min_role 为 USER。这组工具受工具层 min_role 过滤约束，但底层端点内部以 ADMIN 执行——这是信任模型沿工具链继承的已知边界，详见「已知限制」。

## 使用与配置

### [permission] 配置节

| 字段 | 格式 | 默认 | 说明 |
|---|---|---|---|
| admins | platform:user_id | [] | 私聊无条件 ADMIN；群聊受开关控制 |
| admin_groups | platform:group:group_id | [] | 群内所有人 ADMIN，不受开关影响 |
| users | platform:user_id | [] | 按人授予 user |
| user_groups | platform:group:group_id | [] | 群内所有人 USER |
| admin_in_group_chats | bool | false | 按人 admin 在群聊是否保持 ADMIN，false 时降级 USER |

配置热更新走 `apply_config_update`（lifecycle.py:160-217）：第一步重建 PermissionResolver 并透传到 TaskManager（lifecycle.py:178），权限变更立即生效，无需重启。

**如何授予角色。** 在 `[permission]` 节按 `platform:user_id` 格式登记个人（如 `qq:10001`），或按 `platform:group:group_id` 格式登记群（如 `qq:group:123456`）。平台前缀与 MaiBot 的平台标识一致。只读用户无需登记，默认 GUEST 即可查看任务列表与历史。

### 角色能力速查

| 操作 | GUEST | USER | ADMIN |
|---|---|---|---|
| 查看任务列表 / 详情 / 历史 | ✅ | ✅ | ✅ |
| 创建任务 | ❌ | ✅ | ✅ |
| 修改 / 取消 / 注入指令 | ❌ | 仅自己的任务 | 全部任务 |
| 文件读写 | ❌ | `data_dir/files/` 内 | 宿主机任意路径 |

### [api_expose] 配置节

该配置节已废弃：历史 `max_level` 字段（"guest" / "user" / "admin"，默认 "user"）声明了 API 最大暴露等级，但跨插件调用不携带调用方角色、Host 侧 `public` 也只是二元可见性开关，等级过滤无法实现，字段已从 `config.py` 移除。存量 `config.toml` 中的 `[api_expose]` 节会被静默忽略（`PluginConfigBase` 配置模型 `extra="ignore"`），无需迁移。

### 已知限制

1. **call_* 工具继承 ADMIN 执行（已知边界）**：Agent 侧 `call_{api}` 工具（min_role=USER）可触达本插件 6 个端点并以 `_CALLER_ROLE=ADMIN` 执行，owner 隔离被绕过——user 级 Agent 经 `call_list`（空 owner）可见全部任务，经 `call_cancel` / `call_pause` / `call_inject` 可取消/暂停/注入任意任务。原生 Agent 工具（tools/agent/task_mgmt.py）是 owner 隔离的，两条路径行为不一致。收紧方向：在 call_* 工具集排除本插件敏感端点（方案见 [跨插件 API](./10-cross-plugin-api.md)）。
2. **resolver / config 参数已删除**：`build_api_handlers` 签名收敛为单参数 `build_api_handlers(task_manager)`，未使用的 resolver 与 config 参数已移除。
3. **任务权限边界**：跨插件 API 无角色门控，任务权限依赖用户交互层与 Agent 工具层的检查；这两层本身是完整的。

### 相关文档

- [工具系统](./05-tools.md) — ToolDefinition.min_role 过滤与文件沙箱
- [跨插件 API](./10-cross-plugin-api.md) — 6 个端点的调用方式
- [配置体系](./14-config.md) — permission / api_expose 配置节完整说明