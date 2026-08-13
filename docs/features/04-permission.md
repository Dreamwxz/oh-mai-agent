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

**用户交互层。** /task 命令与 planner 工具经 TaskManager 门面进入 TaskCrud（core/usecases/task_crud.py）。创建任务对 guest 直接拦截（task_crud.py:67）；查看 / 修改 / 取消 / 注入等操作要求 owner 匹配或 ADMIN，owner 精确比较在 `_resolve_task_by_id` 与各方法内完成（task_crud.py:126-137）。这一层是同步的、有明确调用者的，角色由命令上下文直接解析。planner 工具是例外：主 Planner 被视为受信任的宿主组件，`_planner_caller_role` 恒返回 ADMIN（tools/planner/task_tools.py:25-27），不参与聊天流角色解析。

**Agent 执行层。** 离线任务由 LLM 自主执行，工具调用需要知道"当前任务属于谁、以什么角色执行"。答案来自角色回调：`make_role_provider`（executor/context.py:16-31）从 task.owner / task.platform / task.stream_id 构造 `() -> Role` 回调，AgentExecutor 经 `_make_role_provider` 注入 AgentLoop（executor/agent.py:110-119），每轮执行前解析一次，resolver 缺失时回退 GUEST。角色绑定在任务上而非调用者上，因为 Agent 循环里没有"当前用户"，只有"当前任务"。

**工具层。** 每个 ToolDefinition 带 `min_role` 字段，schema 呈现时按角色过滤（tools/registry.py:135），`registry.execute(name, role)` 执行时二次校验（tools/registry.py:198），绕过呈现直接调用也会被拦下。文件工具在此基础上再加 `FileAccessPolicy` 沙箱（tools/agent/file_tools.py:50）：user 角色限制在 `data_dir/files/` 内，admin 可访问宿主机任意路径。文件的角色回调来自 `TaskManager._current_task_role`，读的是 `current_task` ContextVar（executor/context.py:11-13），与 Agent 执行层共用同一份上下文。

### 跨插件 API 的权限现状

6 个跨插件端点（create / list / get / cancel / inject / history）由 `build_api_handlers`（api_expose.py:87-416）构建，在插件加载第 10 步注册到 SDK 动态 API（lifecycle.py:140-151）。设计意图是调用方先做角色门控再调用，但**当前门控未执行**：

- 所有 handler 内部统一以 `_caller_role = Role.ADMIN`（api_expose.py:124）调用 TaskManager，注释认为"跨插件 API 调用可信"，TaskCrud 的 owner / guest 检查因此被旁路；
- 6 个端点的 `public` 字段全部硬编码 True（api_expose.py:378/385/392/399/406/413）；
- `check_api_call_permission`（api_expose.py:29-65）实现了角色与等级的比较逻辑，但全仓库无任何调用点。

实际效果：任何能访问这些端点的插件都会被当作 ADMIN 对待，权限责任完全压在外部门控上，而外部门控函数尚未接入调用链。详见"已知限制"。

Agent 侧还有一组对应的调用工具：`build_plugin_api_tools` 动态扫描 `ctx.api.list()`，把每个可见 API 包装成 `call_{api}` 工具（tools/agent/plugin_api_tools.py:148），min_role 为 USER。这组工具受工具层 min_role 过滤约束，但底层端点本身仍是 public 的。

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

`max_level`（"guest" / "user" / "admin"，默认 "user"）声明 API 最大暴露等级。**当前未生效**：`build_api_handlers` 不读取该配置，6 个端点始终以 public=True 注册，无论 max_level 设为何值。

### 已知限制

1. **api_expose 权限层未执行**：`max_level` 声明但未强制执行，6 个端点全部 public=true。
2. **`check_api_call_permission` 是死代码**：定义于 api_expose.py:29-65，全仓库无调用点。
3. **resolver 参数未使用**：`build_api_handlers` 接收 resolver（api_expose.py:89）但内部从不调用，仅保留签名。
4. **后果边界**：跨插件 API 无角色门控，任务权限只依赖用户交互层与 Agent 工具层的检查；这两层本身是完整的。

### 相关文档

- [工具系统](./05-tools.md) — ToolDefinition.min_role 过滤与文件沙箱
- [跨插件 API](./10-cross-plugin-api.md) — 6 个端点的调用方式
- [配置体系](./14-config.md) — permission / api_expose 配置节完整说明