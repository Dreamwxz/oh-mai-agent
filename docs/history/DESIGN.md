# oh-mai-agent 插件设计文档

> 状态：设计讨论中（v0.2）
> 目标：让 Bot 离线多线程处理事务，具备完整 Agent 能力。
> 优势：通过 MaiBot 载体获取更多信息（记忆、聊天流、人物画像），做纯代码 Agent 做不到的事。

---

## 1. 概述

单一插件包 `oh-mai-agent`，运行于 MaiBot 插件 Runner（独立进程，与 Host 通过 RPC 通信）。

- **离线**：任务运行在插件进程内，不阻塞 Host 主流程。
- **多线程**：每个任务 = 一个 asyncio Task（IO 密集型 LLM/网络/文件操作可高并发）。
- **Agent 能力**：插件自带完整 Agent 循环，不依赖 Maisaka 代为执行。

## 2. 核心架构（已确认：方案 B）

**插件自带 Agent 循环**，每个任务独立运行：

```
┌─────────────────────────────────────────────────────┐
│  oh-mai-agent 插件（独立 Runner 进程）                 │
│                                                     │
│  TaskManager ── 并发额度调度 ── sqlite 落盘           │
│      │                                              │
│      ├── Task #1 (asyncio.Task)  Agent循环           │
│      │      └── LLM 上下文 + 工具循环                  │
│      │          （ctx.llm.generate_with_tools）      │
│      ├── Task #2 ...                                 │
│      └── 消息队列（注入指令用）                        │
│                                                     │
│  工具层：信息工具 / 文件工具 / 任务工具 / 跨插件API /    │
│          MCP 工具 / ask_user                        │
│  权限层：guest / user / admin 判定                    │
│  Planner 集成：Hook 注入摘要 + Tool + Command         │
└─────────────────────────────────────────────────────┘
```

关键决策：

- **信息获取工具全部自己实现**（不依赖 Maisaka 输出）。基于 SDK 的 `self.ctx` 能力实现等价途径：`ctx.message`、`ctx.knowledge`、`ctx.person`、`ctx.chat`、`ctx.render`、`ctx.tool` 等（参考 `MaiBot/docs/plugin-info-tools.md`）。
- **Maisaka 的角色**：仅作为"信息源"（记忆/画像/聊天流）和"出口"（回复润色，见 §9）。
- 任务 Agent 的 LLM 调用走 `ctx.llm.generate_with_tools()`，工具循环由插件实现。

## 3. 任务模型

### 3.1 状态机

```
             ┌──────────┐
             │ scheduled │ ← 定时任务（未到触发时间）
             └──────────┘
                  │ 到点且有空闲额度
                  ▼
             ┌──────────┐     ┌──────────┐
  创建 ───▶ │ pending   │──▶ │ running  │──▶ waiting_input
             └──────────┘     └──────────┘     │  ▲
                 ▲             │   │  ▲        │  │ 用户回复
                 │ 额度满       │   │  │paused   │  │
                 └─────────────┘   ▼  └────────┘  ▼
                            ┌──────────┐     ┌──────────┐
                            │ paused   │     │ completed │
                            └──────────┘     │ failed    │
                                             │ cancelled │
                                             └──────────┘
```

- `pending`：排队等待并发额度
- `running`：Agent 循环执行中
- `waiting_input`：Agent 正在向用户提问，等待回复
- `paused`：手动暂停（可恢复）
- `scheduled`：定时任务等待触发，**状态上挂相对时间**（"2 小时后开始"）
- `completed` / `failed` / `cancelled`：终态

### 3.2 任务分级（执行引擎选择）

**触发方式（立即/定时/cron）与执行级别正交**。每个任务 = 触发方式 × 执行级别。级别由**创建时 LLM 自动判定**（默认）+ 用户 `--level` 显式覆盖 + **运行时可升级**（planner 对话中可用 `task_create` 创建 agent 任务，形成闭环）。（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）

| 级别 | 名称 | 适用场景 | 执行引擎 | 典型例子 |
|---|---|---|---|---|
| **L1** | instant 直接执行 | 单步动作，无需推理或仅需轻量表达 | 定时器 + LLM 轻量润色直发 | "8 点提醒我喝水"、"明天 9 点给张三发'早安'" |
| **L3** | agent 离线长时 | 多步、需工具、需持久化、可能持续很久 | 完整 Agent 循环（asyncio + LLM + 工具） | 监控网页价格、批量抓取分析、复杂调研 |

分级要点：

- **判定标准**：任务产出是"表达"还是"动作"；是否需工具/多步/长时（→agent）。（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）分级 prompt 明确"**宁可升级，不要降级**"倾向，防误判。
- **LLM 分级兜底**：创建时 LLM 分级调用失败（超时/服务不可用/返回不可解析）时，**默认降级为 L1**（直接执行，最少依赖），记录警告日志；用户可通过 `--level` 显式指定级别绕过自动分级。L1 运行时若发现复杂度超预期，可升级重试（以 L3 重启，保留 intent）。
- **L1 简化状态机**：`scheduled → running(瞬时) → completed`，无 `waiting_input`、无 Agent 历史；但**定时 L1 任务也必须落 sqlite**（否则重启后提醒丢失）。
- ~~**L2 无完成确认**：proactive 触发后 Maisaka **自行决定是否回复**，任务触发即标 `completed`（"已交给麦麦"），无法确认实际回复；可接受（无法没收 Maisaka 的 no_reply/wait 能力）~~（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）。
- ~~**L2 需限流**：proactive 占用聊天流主循环，与用户消息排队；同流短时多次触发会自动合并（任务文本已入 `_chat_history` 不丢失）。建议每流同时最多 1 个 pending proactive~~（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）。
- **L3 完整状态机**（§3.1）+ 工具 + 持久化 + 注入指令。
- **分级不改变权限模型**：guest 依然不能创建任何级别任务。

### 3.3 相对时间显示

状态展示统一用**相对时间**，由插件格式化：

- `running`：已运行 3 分钟 / 2 小时 / 1 天
- `scheduled`：5 分钟后开始 / 明天 10:00 开始 / 3 天后开始
- `waiting_input`：已等待 1 分钟

### 3.4 任务标题

- 创建任务时**异步调一次 LLM 生成一句话标题**（如"整理上周聊天记录并生成摘要"）。
- 生成失败则用 intent 截断兜底。
- Planner / 用户列表一眼看懂。

### 3.5 任务持久化

- 状态落盘 **sqlite**（`data_dir/tasks.db`）。
- 插件重载后恢复任务（`running` 恢复为 `pending` 并重建 Agent 上下文摘要，实现"续跑"）。
- **L1 特殊恢复**：L1 无 `pending` 状态，重启时 `scheduled` 任务按剩余触发时间重新调度；`running`（瞬时态，几乎不会残留）直接标记 `completed`。
- **on_unload 清理**：插件禁用/热重载时，在 `on_unload()` 中：取消所有 running 的 asyncio Task（状态落盘为 `paused`，重启后恢复为 `pending` 续跑）、持久化 scheduled 任务、`waiting_input` 任务保持 `waiting_input`（重启后继续等待，用户回复仍可唤醒）。
- 任务完整历史（LLM 对话、工具调用记录、指令注入记录）随任务持久化。

## 4. 调度与并发

- `max_concurrent_tasks`：**由用户在 config 决定**（默认建议 3~5）。
- 即时任务：创建时检查额度，额度满 → `pending` 排队。
- **定时任务：创建不受并发限制**（可创建任意多），但**触发运行时仍计入并发计数**；到点时若额度满 → 进入 `pending` 等待。
- 定时调度：`asyncio` 延迟调度 + `croniter`（支持 cron 表达式：每天/每周固定时间）。到点触发。
- **任务总时长兜底**：`max_runtime_min`（config，`0` = 不限）。running 超过上限时强制终止（或挂起并通知 owner，实施时定具体动作）。
- 任务注入指令：每个 running 任务维护 `asyncio.Queue`，`task_modify` / `/task ask` 推入队列，Agent 循环每轮消费。

## 5. Planner 集成

### 5.1 暴露给 Planner 的 Tool

| Tool | 功能 | 权限 |
|---|---|---|
| `task_create` | 创建任务（含定时/单次，intent、优先级、目标聊天流） | user+ |
| `task_list` | 查询任务（按状态/时间过滤，含 AI 标题） | guest+（只读） |
| `task_query` | 查看单个任务详情 | guest+（只读） |
| `task_modify` | 修改任务；**注入新指令到运行中任务的队列** | owner 或 admin |
| `task_delete` | 删除/取消任务 | owner 或 admin |
| `task_history` | 查看过往任务（归档） | guest+（只读） |
| `task_schedule` | 设置定时任务（cron/延迟） | user+ |

### 5.2 暴露给用户的 Command

`/task create ...`、`/task list`、`/task status <id>`、`/task cancel <id>`、`/task history`、`/task ask <id> <指令>`

**Command 同样遵循权限等级**：guest 只能使用只读命令（list/status/history/query），管理命令（create/modify/cancel/schedule/ask）需 user+（其中 owner 或 admin 才能改/删他人任务）。

### 5.3 Planner Hook 注入（Planner 看板）

用 `@HookHandler("maisaka.planner.before_request")`（BLOCKING，允许改参）注入任务摘要：

> **可行性调研结论（已根据 MaiBot 代码确认，2026-08-02）**：
> - hook 每轮 action loop 都触发（一次用户消息最多 10 轮，`MAX_INTERNAL_ROUNDS=10`），子代理请求也会触发；kwargs 含 `messages`/`tool_definitions`/`session_id` 等，**无 request_kind/round_index 标志**。
> - `modified_kwargs` 的 `messages` 字段可注入任意 role 消息，生效于 **messages 层**；注意多插件 last-write-wins，需返回完整 `messages` 键。
> - **hook 注入是一次性的**：修改的是本轮请求快照，不写回 `_chat_history`，下一轮历史里没有上一轮注入内容 → 去重只能靠插件侧 `session_id → last_hash` 状态映射，无法靠扫描 messages 判断。
> - 备选：`ctx.maisaka.context.append` 会把消息持久 append 进 `_chat_history`，后续轮次自动纳入上下文（可扫描 marker 去重），但只增不减、固定 user 角色、重启后丢失（`_chat_history` 从消息库恢复）。
> - **采用混合方案**：BLOCKING hook 中检查 `messages` 里是否已含当前摘要 marker；已含则原样返回，未含则注入。插件侧维护 `session_id → last_hash` 防重复。**注册为 `HookOrder.EARLY`** 尽早执行，降低被其他插件覆盖的概率；若其他插件也改 `messages`，本插件注入可能被覆盖——视为可接受限制。

- **注入内容**：
  - 活跃任务：`running` / `waiting_input` / `paused`（标题 + 相对时间 + 状态）
  - 即将触发的定时任务（标题 + 相对时间）
  - **最近完成的任务**（含时间）——让 Planner 看到相关已完成任务时会主动去查看（`task_query`）
- **条数限制**：每类限制条数，**数量由用户在 config 调整**（如 `max_active=5, max_scheduled=3, max_recent=3`）。
- **注入频率**（避免每轮都注入）：
  - 摘要内容**哈希去重**：插件侧维护 `session_id → last_hash`，内容无变化则不重复注入；
  - "仅在有新用户消息的轮次注入"通过插件侧启发式实现（追踪 `selected_history_count`/`built_message_count` 突变，或检测 `messages` 尾部新用户消息文本）；
  - 用户可通过 config 关闭该功能。
- 标题由 LLM 生成（§3.4），供 Planner 快速识别。

### 5.4 Planner 工具安全子集（防提示词注入）

**关键安全原则**：暴露给主 Planner 的工具只包含**安全子集**。Planner 在对话中可被用户用聊天内容诱导（提示词注入），因此：

- ✅ 暴露：任务管理工具（task_create/list/query/modify/delete/history/schedule）、只读信息工具（search_memory、fetch_history、query_person 等）
- ❌ 不暴露：**文件写工具**、宿主机操作、修改插件配置等危险操作——这些只在 **L3 Agent 任务内**可用（且受 §8 权限沙箱约束）
- 即：工具分两套视图——**Planner 视图**（安全子集，注册到 `@Tool` 暴露给 Maisaka）与 **L3 Agent 视图**（完整工具集，含文件/MCP/跨插件 API，只在插件内部 Agent 循环可见）
- 即使 Planner 被注入恶意指令，最多只能创建任务/查信息，无法写宿主机文件

## 6. 工具系统

### 6.1 两级呈现（借鉴 oh-my-pi xdev）

- **Essential 层**（常驻工具 schema，控制数量）：`search_memory`、`fetch_history`、`ask_user`、`list_tools`（发现入口）、任务管理工具。
- **Discoverable 层**（按需发现）：`list_tools` 列出全部，`get_tool_schema` 取单个工具定义后调用。跨插件 API 工具、MCP 工具、文件工具、低频信息工具全部放此层。

### 6.2 工具来源（四类）

1. **内置信息工具**（自己实现，覆盖 plugin-info-tools.md 的等价途径）：
   - `search_memory` → `ctx.call_capability("knowledge.search", ...)` 全参数
   - `fetch_history` → `ctx.message.get_recent()`
   - `query_person` → `ctx.person.*` + `knowledge.search`(aggregate)
   - `list_streams` → `ctx.chat.get_all_streams()`
   - `get_frequency` → `ctx.frequency.get_current_talk_value()`
   - `render_html2png` → `ctx.render.html2png()`
   - `send_message` → `ctx.send.text()`
   - `list_plugin_tools` → `ctx.tool.get_definitions()`
2. **跨插件 API 工具**：扫描 `ctx.api.list()` → 动态转换为工具（Discoverable 层）
3. **MCP 工具**（§7）
4. **自有工具**：文件读写（权限沙箱）、任务管理、ask_user

### 6.3 权限过滤

- 按调用者角色过滤工具集合：guest 看不到文件工具；user 只看到沙箱内文件工具；admin 全量。
- **Command 权限**：guest 不能触发管理命令（§5.2）。
- 工具执行入口统一做角色判定（包装装饰器）。

## 7. MCP 支持（已确认：方案 A）

- **插件自己实现精简 MCP 客户端**（stdlib 传输 stdio/http/sse），不依赖 MaiBot 官方 `mcp_module`（其实现仍在变更，风险大）。
- 实现可参考 MaiBot `src/mcp_module/` 的协议细节（License 兼容），但**独立实现、独立演进**。
- MCP 服务器静态配置于插件 `config.toml`（`[[mcp.servers]]`：command/args/env 或 url/headers）。
- **不支持运行时动态增删**（后续有需求再做）。
- MCP 工具进入 Discoverable 层，按权限过滤。

## 8. 权限模型（已确认，简化版）

### 8.1 角色与配置

**术语定义（约定）**：

- **管理员（admin）**：指**某个人**具有管理权限——按人配置（`admins` 列表）。
- **管理群（admin_group）**：指**整个群**的所有成员都具有管理权限——按群配置（`admin_groups` 列表），认群不认人。类似地，`user_groups` 是"用户群"，群内所有成员均为 user。
- 角色是**在某聊天流语境下**判定的：同一人在不同流（私聊/群聊）可能得到不同角色。

三种角色：`guest`（默认） / `user` / `admin`。

```toml
[permission]
# 管理员（按人）：platform:user_id —— 这个人具有管理权限
admins = ["qq:10001", "qq:10002"]
# 管理群（按群）：platform:group:group_id —— 群内所有成员均具有管理权限
admin_groups = ["qq:group:123456"]
# 用户（按人）
users = ["qq:20001"]
# 用户群（按群）：群内所有成员均为 user
user_groups = ["qq:group:654321"]

# 开关：按人配置的 admin 是否在"群聊等其他非私聊聊天流"中生效。
# 私聊流（admin 与 bot 一对一）中 admin 权限无条件生效，不受此开关影响。
admin_in_group_chats = false
```

- **判定顺序**：admin（人/群）> user（人/群）> guest（未匹配）。
- **私聊保底规则**：按人配置的 admin 在与 bot 的**私聊流**中**无条件拥有 admin 权限**（不依赖任何开关）。
- **群聊开关规则**：`admin_in_group_chats` 决定按人配置的 admin 在**群聊等其他聊天流**中是否仍为 admin：
  - `false`（默认）：在群聊中自动**降级为 user**（安全默认）；
  - `true`：在群聊中也保持 admin 权限。
- **按群配置不受开关影响**：`admin_groups` 指定的群内所有成员在该群始终为 admin（认群不认人）；`user_groups` 同理。
- 群级配置：认群不认人（群内所有人提升为该角色）。

### 8.2 角色能力

| 能力 | guest | user | admin |
|---|---|---|---|
| 查看任务列表/详情/历史 | ✅ 只读 | ✅ | ✅ |
| 创建/调度任务 | ❌ | ✅ | ✅ |
| 修改/删除/注入指令 | ❌ | 仅自己创建的任务（owner） | 全部 |
| 文件工具（读） | ❌ | data_dir/files/ 内 | 宿主机任意路径 |
| 文件工具（写） | ❌ | data_dir/files/ 内 | 宿主机任意路径 |
| 修改插件配置/设置 | ❌ | ❌ | ✅ |
| 管理命令 | ❌ | ✅ | ✅ |

- **guest 只允许只读任务**（看板 + 查询历史），不能创建任务。
- **user 可写路径 = `data_dir/files/`**（Agent 专属工作区；`data_dir` 已由 Runtime 分配且防逃逸）。
- **admin 文件工具完全开放宿主路径，不做白名单**。
- 文件工具统一做 `resolve()` 路径校验，防 `../` 逃逸；user 级强制沙箱到 `data_dir/files/`。

### 8.3 所有者机制

- 每个任务有 **owner**（创建者的 `platform:user_id`）。
- **提问发到任务所在聊天流**（任务创建时的流）：管理员在群聊中创建的任务，提问返回该管理群（不是私聊）。
- **管理员在群聊中按 user 权限对待**（受 `admin_in_group_chats` 开关约束）：群聊中的提问、任务管理均按降级后的角色执行。
- **私聊兜底**：若目标流为私聊但私聊流不存在/发送失败 → 退回任务所在群提问；群也不可达则标记"提问投递失败"并保持挂起。
- owner 可管理自己的任务；admin（按其生效角色）可管理全部任务。

## 9. 回复润色（v0.3 修订：按任务级别区分）

> **设计转变说明（2026-08-02）**：之前定为"任务最终回复统一走 proactive 交给 Maisaka replyer 润色"（路径 A）。引入任务分级后，该路径与 L1 直发矛盾，故修订为**按级别区分**。核心诉求"融入对话 + 黑话润色"通过**插件侧拉取聊天记录 + 黑话表 + LLM 风格润色**实现，不再依赖 Maisaka 转发。（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）

### 9.1 按级别的回复路径

| 级别 | 回复路径 |
|---|---|
| **L1** | LLM 轻量润色直发：拉取目标流最近消息（条数跟随 MaiBot 配置：群聊 40/私聊 60）+ 黑话表（`ctx.db` 查 `Jargon`/`HighFrequencyTerm` 按 chat + global 过滤）→ 注入润色 prompt → `ctx.send.text` 直发 |
| **L3** | 同 L1 润色直发（最终结果）；Agent 执行期间的提问/中间消息直发 |

### 9.2 润色实现（L1/L3）

> **设计原则（2026-08-02）**：润色相关的所有参数**直接跟随 MaiBot 现有配置与实现**，插件不提供覆盖项、不做过度设计——可预见情况下这些必然要与 MaiBot 保持一致。

- **上下文融入**：拉取目标流最近消息作为对话风格参考，条数直接读 MaiBot 配置——`max_context_size`（群聊默认 40）/ `max_private_context_size`（私聊默认 60）（`bot_config.toml:19-20`），通过 `ctx.config.get()` 读取。
- **黑话润色（复刻 MaiBot 机械匹配方案，`jargon_context_matcher.py`）**：
  1. **加载候选**：`ctx.db.query("Jargon", filters={"is_jargon": True})` 按 `order_by=["-count"]` 降序；**`meaning != ""` 等非等值过滤在插件侧客户端完成**（`ctx.db` filters 仅支持等值查询，`data.py:104` 直接透传给 `database_service.db_get`）；
  2. **作用域过滤（插件侧重新实现，不能 import `src.*`）**：`is_global=True` 直接纳入；否则解析 `session_id_dict` JSON（`{"session_id": count, ...}`）与当前流 ID 求交集。jargon_groups 共享群逻辑参照 `JargonConfigUtils.resolve_jargon_group_scope`（utils_config.py:227）重新实现：读取 `ctx.config.get("jargon.jargon_groups")`（全局配置可读，见上），把同一 group 的会话 ID 合并进作用域集合。**v0.2 可先只支持 is_global + 精确 session_id_dict 匹配，jargon_groups 扩展后补**；
  3. **加载高频词**：`ctx.db.query("HighFrequencyTerm", filters={"chat_id": stream_id})`；
  4. **机械匹配**：从拉取的消息文本（排除 bot 自身发言）中做子串匹配（归一化小写后 `term in text`）；
  5. **打分排序**：`score = count + (1000 + high_freq_count*2 + rank_bonus if 命中高频词) - first_index*0.01`，取前 `MAX_JARGON_REFERENCE_MATCHES=10` 条（与 MaiBot 常量一致）；
  6. **注入格式**：`1. {content}：{meaning}`（命中高频词追加"，同时命中高频词"），包成 `[黑话参考]` 消息段注入润色 prompt。
- **风格 prompt**：固定"麦麦风格"system prompt（参考 MaiBot replyer 的表达方式选择），一次 LLM 调用完成润色。
- **不修改主程序**：黑话注入完全由插件侧实现（SDK 无 jargon capability，`ctx.db` 是唯一可读途径——`data.py:104` 动态模型访问无白名单）。

### 9.3 边界

- ~~L2 触发即视为完成（无完成确认，见 §3.2）~~（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）。
- `ask_user` 的提问消息直接发送（无需润色），并**注入当前聊天流 planner 上下文**（`ctx.maisaka.context.append`），让 Maisaka 知晓该问题。
- ~~中间状态（如"任务已开始""进度 50%"）可选择性直接发送~~ → **已移入未来规划**（v0.2 不实现进度播报；开始/完成/失败/提问消息属必要交互，随功能本身发送）。

### 9.4 回复路径统一重构（2026-08-04）

> **设计转变说明（2026-08-04）**：重构前，回复路径存在两条独立链路（instant 任务的 `InstantExecutor` 直发 + agent 任务完成的 `task_manager._send_final_reply` 同步发送），且 `PolishService` 与 `executor/base.py` 之间的依赖方向不合理。重构后统一为单一路径——**所有回复（instant 任务、agent 完成回复）均作为 instant 任务走调度器分发**，消除死代码，纯净化协议层。

#### 背景：从"冗余观察"到"架构决策"

1. **表面观察**：根目录 `polish.py` 与 `executor/instant.py` 似乎存在职责重叠，一度提议"用 instant 替代 polish"或"把 polish 合并进 instant"。

2. **深入分析后的真相**：
   - `PolishService` 本身**不冗余**——它被 instant 回复路径（`executor/instant.py`）与 agent 完成回复路径（原 `task_manager._send_final_reply`）**两条路径共用**，是核心润色服务，不是重复代码。
   - **真正的冗余**：`task_manager._send_final_reply` 与 `executor/base.py::send_final_reply` 是同一逻辑的两份实现；`_fail_task` / `_complete_and_notify` / `_complete_task` 是 P3 重构遗留的死代码（生产环境零调用者）。
   - **架构味**：`executor/base.py` 作为协议层，却 `from ..polish import PolishService` 依赖具体服务——依赖方向不合理。进一步分析发现：若把 `PolishService` 移入 `instant.py`，`base.py` 会反向依赖 `instant.py`，形成循环依赖，暴露 base 层职责不清。

#### 核心决策：统一回复路径

**所有回复（instant 任务、agent 任务完成回复）统一走"instant 任务"一条路径**：

- `PolishService` 从根目录 `polish.py` 移入 `executor/instant.py`（原文件删除）
- `send_final_reply`（含重试）与 `fail_task` 从 `executor/base.py` 移入 `executor/instant.py`
- `executor/base.py` 纯净化：只保留 `TaskExecutor` 协议 + `ExecutionContext` / `ExecutionResult` 数据类 + `complete_and_notify`
- agent 完成回复不再直接同步润色+发送，而是通过 `task_manager._dispatch_reply_instant` **派遣一个 instant 任务**交给调度器处理

#### 决策理由

1. **统一回复路径**：instant 任务本质就是"润色 + 发送一条消息"。把 agent 完成回复也建模为 instant 任务，整个系统只有一条回复链路（润色 → 发送 → 重试 → 完成/失败），心智模型简单，行为一致。

2. **恢复保证 = 消息可达**：`_dispatch_reply_instant` 创建 PENDING 回复任务后交给 `scheduler.enqueue` 异步调度，而非同步执行。若插件在回复发送前重启，恢复机制会把 PENDING 回复任务重新调度执行——保证消息最终可达。**这是刻意接受"可能重复发送"换取"不丢消息"**（消息可达性优先于去重）。这是与"任务完成时直接同步发送"最大的行为差异，是设计意图而非缺陷。

3. **重试内嵌在发送逻辑中**（`send_final_reply` 任务内退避重试）：最多 3 次、指数退避 1s→2s（`asyncio.sleep(2 ** attempt)`），**只重试发送环节**（`ctx.send.text`），**不重试润色**（LLM 调用失败由 PolishService 自身回退原始文本兜底）。重试期间任务保持在 RUNNING 状态（可查询、可观察）。全部重试耗尽后抛异常 → 任务 FAILED。
   - 为什么不做调度器通用重试：agent 任务重跑会重复执行工具副作用（写文件/调 API）——调度器不知道任务语义，通用重试是灾难。所以重试只作用于回复（instant）任务。（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）

4. **失败递归防护**：回复任务全败后 `fail_task(send_message=True)` 的失败消息**直接同步发送**（经 `send_final_reply`），不派遣新的 instant 任务——否则"失败消息发送失败"会无限递归。

5. **跨聊天扩展预留**：`send_final_reply` 签名从 `(text, task, ...)` 改为 `(text, stream_id, ...)`，把"目标聊天流"与"任务对象"解耦。未来跨聊天转发只需传不同的 `stream_id`，接口不变。本次只做签名解耦，不实现实际跨聊天功能。

#### 明确不做的（Scope OUT）

- 不实现实际跨聊天转发（只预留签名）
- 不给调度器加通用重试（agent 副作用风险）
- 不修改 `PolishService` 内部算法（黑话匹配/评分/上下文加载）
- 不修改 `prompt/builders/polish.py`（独立的 prompt 模板系统）
- 不引入新的任务类型或异步消息队列

#### 代码位置索引

- `executor/instant.py`：`PolishService`、`send_final_reply`（重试）、`fail_task`、`InstantExecutor`
- `executor/base.py`：`TaskExecutor` 协议、`ExecutionContext` / `ExecutionResult`、`complete_and_notify`
- `core/task_manager.py`：`_dispatch_reply_instant`

## 10. AI 提问机制

1. Agent 调用 `ask_user` 工具 → 任务进入 `waiting_input` → 向**任务所在聊天流**发送问题（含任务标识）。
2. 插件 `@EventHandler(ON_MESSAGE)` 监听入站消息 → 匹配"等待该流回复的任务" → **注入聊天记录**（`ctx.message.get_recent()`），让 Agent 判断当前回复是否足够/有效 → 用户回复注入任务队列 → 任务恢复 `running`。
3. **无回复超时（默认 10 分钟，config 可调）→ 任务挂起（`waiting_input` 保持），不取消**；用户后续回复仍可唤醒。
4. **回复匹配机制**：EventHandler 按 `stream_id + owner(user_id)` 匹配等待任务；命中后把用户回复 + 最近聊天记录注入任务队列。Agent 循环收到后**自动恢复 running**，由 Agent 用 LLM 判断回复是否足够/有效（不够则继续追问，ask_user 可再次调用）——判断逻辑放在 Agent 循环的"处理注入消息"步骤。
5. 群聊场景：只有 **owner 的回复**才计入回答（避免他人误答）；管理员在群聊中按 user 权限对待。
6. 私聊兜底：目标流为私聊但私聊流不存在/发送失败 → 退回任务所在群提问；群也不可达 → 标记"提问投递失败"，任务保持挂起。
7. 提问同时注入当前聊天流 planner 上下文，Maisaka 侧可见。

## 11. 与其他插件互动

### 11.1 本插件暴露的 API（`@API(public=True)`）

任务管理 API：`create` / `list` / `get` / `cancel` / `inject` / `history`。

- **默认暴露等级：user 及以下**可被其他插件调用；`config` 可调最大暴露等级（admin 级 API 仅配置指定插件可调）。
- 其他插件调用同样经过权限判定（按调用方插件 ID 的配置等级）。

### 11.2 调用其他插件 API

- `ctx.api.list()` → 动态转换为 Agent 工具（Discoverable 层，§6.2），权限过滤。

## 12. 插件形态（可维护性优先）

```
oh-mai-agent/
├── plugin.py                # 入口，create_plugin()
├── config.toml              # 插件配置
├── _manifest.json           # 插件清单
└── maibot_agent/
    ├── __init__.py
    ├── config.py            # config_model（Pydantic）
    ├── permission.py        # 角色判定
    ├── task_model.py        # 任务数据模型 + 状态机
    ├── task_store.py        # sqlite 持久化
    ├── scheduler.py         # 并发额度 + 定时调度
    ├── task_manager.py      # 任务生命周期管理
    ├── agent_loop.py        # Agent 循环（LLM + 工具循环）
    ├── prompts/
    │   ├── agent_system.md  # 任务 Agent 系统提示词
    │   └── title.md         # 标题生成提示词
    ├── tools/
    │   ├── __init__.py
    │   ├── registry.py      # 工具注册 + 两级呈现 + 权限过滤
    │   ├── info_tools.py    # 信息获取工具
    │   ├── file_tools.py    # 文件工具（权限沙箱）
    │   ├── task_tools.py    # 任务管理工具
    │   ├── plugin_api_tools.py  # 跨插件 API 工具
    │   └── ask_tool.py      # 提问工具
    ├── mcp_client/          # 精简 MCP 客户端
    │   ├── __init__.py
    │   ├── connection.py
    │   └── provider.py
    ├── planner_hooks.py     # HookHandler 摘要注入
    └── commands.py          # /task 命令
```

- 使用 `config_model` 声明配置 → WebUI 自动生成配置面板。
- 插件 ID：`oh-mai-agent`。

## 13. config.toml 草案

```toml
[permission]
admins = ["qq:10001"]
admin_groups = ["qq:group:123456"]
users = ["qq:20001"]
user_groups = []
admin_in_group_chats = false   # 开关：按人 admin 在群聊是否生效；私聊中无条件生效

[task]
max_concurrent_tasks = 4      # 并发上限，用户可调
max_runtime_min = 0           # L3 任务总时长兜底（分钟），0 = 不限；超时强制终止并通知 owner
default_timeout_min = 10      # ask_user 无回复挂起等待时间
persist_history = true        # 是否持久化完整任务历史
max_l2_pending_per_stream = 1 # 每聊天流最多 1 个 pending proactive（L2 限流）（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）

[planner_board]
enabled = true                # 是否向 Planner 注入任务摘要
max_active = 5                # 活跃任务条数上限
max_scheduled = 3             # 定时任务条数上限
max_recent = 3                # 最近完成任务条数上限

[polish]
use_jargon = true             # 润色时机械匹配黑话（复刻 MaiBot jargon_context_matcher）
                              # 其余参数（消息条数/黑话条数/黑话开关）直接跟随 MaiBot 配置，不提供覆盖

[mcp]
enabled = true
[[mcp.servers]]
name = "filesystem"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
# transport = "http" / "sse" 时用 url + headers

[api_expose]
max_level = "user"            # 本插件 API 最大暴露等级（guest/user/admin）
```

## 14. 待确认/后续事项

1. ✅ **Planner 摘要注入**：可行性已根据 MaiBot 代码确认（§5.3 已写入调研结论 + 混合方案）。
2. ⏭️ 中间进度消息：**移入未来规划**（v0.2 不实现）。
3. ✅ 提问去向（8.3/10）：管理员群聊任务提问返回管理群；管理员群中按 user 权限；私聊不存在兜底回群；任务默认目标流 = 当前对话流（已确认）。
4. ✅ 术语固化（8.1）：管理员=按人，管理群=按群（群内所有人）；已写入文档。
5. ✅ 任务分级（§3.2）：instant / agent 两级，创建时 LLM 自动分级 + 用户覆盖 + 运行升级（已确认）。（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）
6. ✅ 回复润色修订（§9）：instant/agent 带聊天记录 + 黑话表 LLM 润色直发（已确认）。（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）
7. ✅ 任务超时兜底：`max_runtime_min`（0=不限）已加入 config。
8. ✅ Planner 工具安全子集（§5.4）：危险工具不对 Planner 暴露，防提示词注入（已确认）。
9. ✅ proactive 限制：**无跨流全局限制**，按聊天流独立；同流短时多次自动合并（已确认代码事实）。（已移除，2026-08：PLANNER 任务等级删除，仅保留 instant/agent 两级）
10. ✅ 黑话可访问性：SDK 无 jargon capability，但 `ctx.db` 可查 `Jargon`/`HighFrequencyTerm` 表（已确认代码事实）。
11. ✅ **Momus 评审（2026-08-02）**：APPROVE（有条件通过）。已修正：config.toml `[planner_board]` 重复定义（删重）、LLM 分级失败兜底（§3.2）、L1 重启恢复 + on_unload 清理（§3.5）、jargon group scope 插件侧重实现说明（§9.2）、ask_user 回复匹配机制（§10）、HookOrder.EARLY（§5.3）、MCP 键名统一（§7/§13）。**Momus 指控的"ctx.config.get() 无法读全局配置"经代码验证为误判**——`_cap_config_get`（core.py:703-722）实际读取 `global_config`，`ctx.config.get("chat.max_context_size")` 可行（MaiBot 自身即用 `global_config.chat.max_context_size`）。
12. MCP 客户端实现细节：传输层选型（参考 MaiBot mcp_module 但精简），后续实现时定。
13. sqlite 表结构、prompt 具体内容、L1/L3 润色 prompt：实现阶段细化。

## 15. 参考资料

- `MaiBot/docs/plugin-info-tools.md` — 信息获取途径
- `MaiBot/docs/plugin_persistence.md` — 持久化路径
- `maibot-plugin-sdk/docs/guide.md` — SDK 全量文档
- `oh-my-pi/docs/xdev-tool-dispatch.md` — 两级工具呈现
- `oh-my-pi/docs/mcp-config.md` — MCP 配置参考
- `oh-my-pi/packages/coding-agent/src/prompts/` — Agent prompt 借鉴
- `MaiBot/src/maisaka/runtime.py` — proactive 任务实现
- `MaiBot/src/maisaka/builtin_tool/reply.py` — reply 润色机制
- `MaiBot/src/mcp_module/` — MCP 客户端参考实现
