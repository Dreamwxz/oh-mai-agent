# 子 Agent

本文档讲述 oh-mai-agent 的子 Agent 能力：主 Agent 如何把「去搜 X、查 Y、读/写文件 Z」这类局部工作派给子 Agent 独立完成，再把结论收回自己的上下文继续判断。全程进程内运行，不新增任何子进程。

## 设计目标（为什么需要子 Agent？）

主 Agent 的自主循环（LLM 推理 + 工具调用，最多 30 轮）擅长长时规划，但一轮内多个工具调用是**顺序执行**的，且它只有一轮上下文——「并行多路检索、交叉验证」这种工作形态在主循环里没有落点。更关键的是，现有 `create_subtask` 是「发出去就收不回结果」的：子任务在独立循环里跑，结论不会回到主 Agent 的上下文里做判断。

子 Agent 要解决的就是这两个问题：

1. **结果回传**。子 Agent 是主 Agent 的一个**特殊工具调用**，结论直接作为工具结果交回主 Agent 上下文，由主 Agent 在下一轮继续判断——「派出去、收回来、看结果」是一个完整闭环。
2. **并行多路**。子 Agent 一轮内的多个工具调用经 `asyncio.gather` 并发执行（主循环是顺序的），这是并行多路检索/查证唯一能落地的位置；`ask_subagents` 更进一步，一次派发多个子 Agent 并行干活，全部返回后合并答案。

设计取舍：子 Agent 是**同步工具调用语义**——主 Agent 在该任务内等待，子 Agent 全部返回后主循环下一轮才继续（不做异步 fire-and-forget 派发，那是被明确排除的 B 方案）。子 Agent 自动继承当前任务的属主与角色，文件操作仍然受既有 FileAccessPolicy 沙箱约束，权限模型零改动。

> 架构变更：v0.1.0 曾尝试把任务迁到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配回退到进程内方案。本次子 Agent 同样是**进程内**方案——复用现有 LLM 调用、工具注册表和权限体系，零新依赖、零子进程。历史子进程方案的文档见 `docs/history/`（`SUBPROCESS_README.md` 等归档）。

## 设计方案

### 两种派发：ask_subagent 单派 / ask_subagents 批量并行

`tools/agent/subagent_tool.py` 提供两个 Discoverable 工具（`min_role=USER`，`config.py` 的 `SubAgentConfig.enabled=false` 时 `TaskManager.setup()` 不注册）：

- **`ask_subagent`（单派）**：参数 `intent`（必填）+ `tools`（可选，必须为默认允许集的子集，含非法名则**整体拒绝**，绝不静默过滤；空列表 = 默认集）。构建一个 SubAgentLoop 运行，返回 `{"success", "answer", "rounds", "max_rounds_reached", "error"}`（原样透传 SubAgentLoop 结果，答案按 `max_result_chars` 截断）。
- **`ask_subagents`（批量并行）**：参数 `intents`（必填、非空、长度 ≤ `config_getter().max_parallel_subagents`，超限/为空整体拒绝）+ `tools`（可选，语义同单个工具，**所有子 Agent 共享同一工具集**，不支持按 intent 单独指定）。`asyncio.gather` 并行运行多个独立 SubAgentLoop（每个独立 messages、独立 rounds），合并返回 `{"success", "answers": [{"intent", "answer", "rounds", "max_rounds_reached", "success", "error"}...], "total_rounds", "error"}`——单项失败不影响其余项继续跑，顶层 `success` 取所有项的与，失败项错误以 `"; "` 聚合。

批量上限是硬约束：`max_parallel_subagents`（默认 3）由配置控制，超限请求在进入任何 LLM 调用前就被拒绝。

### 进程内 SubAgentLoop

`executor/subagent.py` 的 `SubAgentLoop` 是一个轻量嵌套循环，只做一件事：把一段 `intent` 跑成一段答案。流程（`run(intent)`）：

1. system prompt = `prompt_service.build("subagent_system", intent=..., tool_list=...)`（模板见下文「提示词模板」）；
2. 循环最多 `config.subagent.max_rounds`（默认 10）轮：每轮 `ctx.llm.generate_with_tools(model="planner", timeout_ms=240000)`（沿用主循环同一 LLM 通道，不新增 task_name）；
3. 无工具调用 → 该轮回复即最终答案；有工具调用 → 一轮内多个调用 `asyncio.gather` 并发执行，结果按 `tool_calls` **原始顺序** zip 追加 tool 消息；
4. 返回 `{"success", "answer", "rounds", "max_rounds_reached", "error"}` 五个键。

与主 AgentLoop 的关键差异：**无可持久化**（不创建 TaskRecord、不写 task_history，结果只存在于主循环工具消息）、**无总线**（不经 TaskCommandBus）、**无 ask_user**（不能反问用户）、**无工具动态发现**（不做 list_tools/get_tool_schema——防经发现机制加载被排除工具），每轮固定用初始工具集 schema。

`max_rounds` 耗尽（含 `max_rounds=1` 且该轮全为 tool_calls 的边界）时：取最后一轮 assistant 内容（可能为空串）为答案、`max_rounds_reached=True`、`success` 仍为 True——由主 Agent 依据 `max_rounds_reached` 判断结果是否可信。

### 工具集规则与执行守卫（schema 层 + 执行层双保险）

子 Agent 能看到的工具不是全量注册表，而是**默认允许集**：registry 按当前角色可见的全部工具，排除精确名 `ask_user` / `send_message` / `list_my_tasks` / `create_subtask` / `inject_task` / `ask_subagent` / `ask_subagents` / `list_plugin_tools`（`_SUBAGENT_EXCLUDED`），排除 `call_` 前缀（跨插件 API 工具）；MCP 工具（`mcp_` 前缀）包含。工具集解析统一收敛在 `tools/agent/subagent_tool.py` 的 `_resolve_toolset`。

安全边界有两层：

- **schema 层**：传给子循环 LLM 的 schema 只含允许集；子 Agent 无 `list_tools` / `get_tool_schema` 发现工具，tools 参数也不能加载被排除工具（严格校验，非法名整体拒绝）。
- **执行层**（防 LLM 幻觉/注入）：每个工具调用执行前校验名字在允许集内，不在则直接返回 `{"success": False, "error": "tool not in allowed set: <name>"}`，**绝不落入 registry.execute**——即便模型幻觉输出 `send_message`，其 handler 也零调用、任务不产生任何消息发送。

### 取消传导

主任务取消要能打断正在跑的子 Agent：`executor/agent_loop.py` 的 `AgentLoop` 暴露只读属性 `is_cancelled`；`executor/context.py` 增加 `current_cancel_check` ContextVar（默认 None），由 `AgentExecutor.execute()` 在构造循环后 set、finally 中与 `current_task` 一起 reset。SubAgentLoop 每轮开始前与 gather 前检查 `should_cancel()`，命中则提前返回 `{"success": False, "error": "cancelled", ...}`。对主循环是纯增量改动（只读访问器），行为零变化。

### 角色与文件沙箱继承

子 Agent 不建自己的角色体系：handler 经 `role_provider`（`TaskManager` 的 `_current_task_role()`）取得当前任务的属主与角色，工具集按该角色过滤，`registry.execute` 的权限门控原样生效。文件操作二次经 FileAccessPolicy 沙箱校验——子 Agent 写文件与主 Agent 写文件走完全相同的路径，user 级隔离到 `data_dir/files/`，越界写被拒。

### 配置热更新

handler 每次调用都执行 `cfg = config_getter()` 读取**当前**配置（闭包内是 `lambda: self._config.subagent` 引用，绝不缓存配置对象快照）——`TaskManager.update_config()` 后新值（如 `max_parallel_subagents`）无需重注册立即生效。

### 提示词模板

子 Agent 系统提示经正规模板渲染：`prompt/templates/subagent_system.md` + `prompt/builders/subagent_system.py`（`SubAgentSystemBuilder`，变量 `intent` / `tool_list`，XML 转义在 builder 侧）。模板底本为 oh-my-pi 的 subagent-system-prompt.md 汉化适配：保留 ROLE / COOP / COMPLETION 节（yield 协议替换为「末轮无工具调用即答案」），删除 worktree / ircPeers / planReference / outputSchema 节，新增 MaiBot 检索工具说明节（`search_memory` 记忆检索、`fetch_history` 历史消息、`search_users` / `query_person` 用户与群检索、`get_frequency` 活跃度、`mcp_fetch_*` / `mcp_exa_*` 网络检索），鼓励一轮内并行发起多个检索、交叉验证。

## 使用与配置

### [subagent] 配置节

配置键位于 `config.py` 的 `SubAgentConfig`（`MaibotAgentConfig.subagent`，`__ui_label__="子Agent"`）：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | `bool` | `true` | 是否启用子 Agent 工具（ask_subagent / ask_subagents），`false` 时两个工具都不注册 |
| `max_rounds` | `int` | `10` | 子 Agent 最大执行轮数（`ge=1`） |
| `max_result_chars` | `int` | `8000` | 子 Agent 答案最大字符数，超长截断（追加「…（已截断）」） |
| `max_parallel_subagents` | `int` | `3` | ask_subagents 单次批量派发的子 Agent 数量上限（`ge=1`） |

### 使用方式

子 Agent 由主 Agent 通过工具调用自动触发，用户无需手动介入：主循环某一轮 LLM 返回 `ask_subagent(intent="...")` 或 `ask_subagents(intents=[...])` 工具调用 → handler 构建子循环运行 → 结论作为工具结果交回主 Agent 上下文 → 主 Agent 下一轮据此继续判断。

### 已知限制与边界

- **不做异步派发**。ask_subagent / ask_subagents 均为同步工具调用语义，主 Agent 必须等子 Agent 全部返回后才继续；无 fire-and-forget 模式。
- **子 Agent 不持久化**。结果只存在于主循环工具消息，重启后不可恢复；子 Agent 不能发消息、反问用户、再派生更深的子 Agent、或调用跨插件 API。
- **批量工具集共享**。批量派发不支持按 intent 单独指定工具集。
- **无工具动态发现**。子 Agent 看不到 list_tools / get_tool_schema，固定使用初始工具集。

### 关联文档

子 Agent 复用主 Agent 的执行与权限体系：[工具系统](05-tools.md)（两级呈现与注册）、[权限模型](04-permission.md)（角色过滤与文件沙箱）、[提示词系统](12-prompt.md)（subagent_system 模板）、[命令总线](11-command-bus.md)（取消命令经总线传导）。与历史 v0.1.0 子进程方案的关系见本文「设计目标」的架构变更块，子进程方案归档见 `docs/history/`。
