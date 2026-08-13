# oh-mai-agent 插件实施计划

> 基于 `docs/DESIGN.md` v0.3+（已通过 Momus 评审，APPROVE）
> 目标：实现 MaiBot 离线多线程 Agent 插件

---

## 1. 模块拆分与依赖图

```
M1 基础层（无依赖）
   ├── config.py         config_model（Pydantic，含 §13 全部配置）
   ├── permission.py     角色判定（admin/user/guest，人+群，私聊保底+群聊开关）
   └── task_model.py     任务数据模型 + 状态机（§3.1/§3.2，L1/L2/L3）

M2 持久化与调度（依赖 M1）
   ├── task_store.py     sqlite 持久化（tasks 表 + 历史表）
   └── scheduler.py      并发额度 + 定时调度（asyncio + croniter）

M3 Agent 核心（依赖 M1/M2）
   ├── agent_loop.py     L3 Agent 循环（LLM + 工具循环 + 注入队列 + 超时兜底）
   └── tools/            工具系统（registry + info + file + ask_user）

M4 任务管理 + 插件入口（依赖 M1-M3）
   ├── task_manager.py   任务生命周期管理（create/list/query/modify/delete/inject/schedule）
   ├── commands.py       /task 用户命令（权限分级）
   ├── plugin.py         create_plugin 入口 + @Tool 暴露（Planner 安全子集）
   └── api_expose.py     跨插件 API 暴露（默认 user 级）

M5 Planner 集成（依赖 M4）
   └── planner_hooks.py  HookHandler 摘要注入（混合方案 + 哈希去重）

M6 MCP 客户端（独立，可并行于 M4/M5）
   └── mcp_client/       精简 MCP 客户端（stdio/http/sse）

M7 润色系统（依赖 M1/M3）
   └── polish.py         L1/L3 润色（拉消息 + 黑话机械匹配 + 风格 prompt）
                         + prompts/（agent_system.md, title.md, polish.md）

M8 验证与文档
   └── tests/ + README.md + config.toml 完整示例
```

### 依赖关系摘要

| 模块 | 依赖 | 可并行性 |
|---|---|---|
| config / permission / task_model | 无 | 三者可并行 |
| task_store / scheduler | M1 | 可并行 |
| agent_loop / tools | M1/M2 | tools 内部可并行 |
| task_manager | M1-M3 | 串行 |
| planner_hooks | M4 | 串行 |
| mcp_client | M1 | 可与 M2-M5 全程并行 |
| polish | M1/M3 | 可与 M5 并行 |

---

## 2. 实施里程碑

### M1：基础层（config + permission + task_model）
- `config.py`：完整 config_model（§13：permission/task/planner_board/polish/mcp/api_expose）
- `permission.py`：
  - `resolve_role(platform, user_id, stream_id) -> Role`（admin/user/guest）
  - 私聊保底：`is_private_stream` + admin 无条件生效
  - 群聊开关：`admin_in_group_chats`
  - 按群配置：admin_groups/user_groups 认群不认人
- `task_model.py`：
  - `Task` dataclass：id/title/intent/level(L1/L2/L3)/status/owner/stream_id/priority/created_at/...
  - 状态机转换方法（validate_transition）
  - 相对时间格式化（running 已跑 X 分钟 / scheduled X 后开始）
- **验证**：`pytest tests/test_permission.py`（角色判定全矩阵）

### M2：持久化 + 调度
- `task_store.py`：sqlite 封装（`data_dir/tasks.db`）
  - `tasks` 表：全字段
  - `task_history` 表：LLM 对话/工具调用/指令注入记录
  - `save/get/query/list/update/delete` 方法
  - 启动恢复：scheduled 重新调度、running→pending（L3）/completed（L1）
- `scheduler.py`：
  - 并发额度（`max_concurrent_tasks`）：pending 排队
  - 定时调度：asyncio 延迟 + croniter（cron 表达式）
  - L2 限流（`max_l2_pending_per_stream`）
  - `max_runtime_min` 超时兜底
- **验证**：`pytest tests/test_task_store.py`（CRUD + 恢复）

### M3：Agent 核心 + 工具
- `agent_loop.py`：
  - `run_task(task)` asyncio Task
  - LLM 循环：`ctx.llm.generate_with_tools()` → 解析 tool_calls → 执行 → 循环
  - 注入队列（`asyncio.Queue`）：task_modify/ask_user 回复消费
  - `waiting_input` 挂起/恢复
  - 超时兜底（max_runtime_min）
- `tools/registry.py`：
  - 工具注册 + Essential/Discoverable 两级呈现（借鉴 xdev）
  - 权限过滤装饰器（按调用者角色）
  - Planner 视图（安全子集） vs Agent 视图（完整工具）
- `tools/info_tools.py`：search_memory / fetch_history / query_person / list_streams / get_frequency / render_html2png / send_message / list_plugin_tools
- `tools/file_tools.py`：read/write，路径 resolve 防逃逸，user 沙箱 `data_dir/files/`，admin 全开
- `tools/ask_tool.py`：ask_user（进入 waiting_input + 发消息 + 注入 planner 上下文）
- `tools/plugin_api_tools.py`：ctx.api.list() 动态转工具（Discoverable）
- **验证**：`pytest tests/test_agent_loop.py`（mock LLM 的循环/注入/挂起）

### M4：任务管理 + 插件入口
- `task_manager.py`：任务生命周期编排（create 分级、list/query、modify/inject、delete、schedule）
- `commands.py`：/task 系列命令（guest 只读，user+ 管理，owner/admin 权限）
- `plugin.py`：
  - `@Tool` 暴露安全子集给 Planner（task_create/list/query/modify/delete/history/schedule）
  - 生命周期：on_load（恢复任务）/ on_unload（清理）/ on_config_update
- `api_expose.py`：@API 暴露 create/list/get/cancel/inject/history（默认 user 级）
- **验证**：`pytest tests/test_task_manager.py`

### M5：Planner 集成
- `planner_hooks.py`：
  - `@HookHandler("maisaka.planner.before_request", mode=BLOCKING, order=EARLY)`
  - 摘要注入：活跃任务 + 即将触发定时任务 + 最近完成
  - 混合方案：检查 marker → 已含则返回原样，未含则注入
  - 哈希去重：`session_id → last_hash` 状态映射
  - 条数限制：max_active/max_scheduled/max_recent
- **验证**：单元测试（mock messages kwargs 模拟注入）

### M6：MCP 客户端（可与 M4/M5 并行）
- `mcp_client/connection.py`：stdio/http/sse 传输 + 生命周期
- `mcp_client/provider.py`：工具列表发现 + 调用转发 → Discoverable 层
- 参考 MaiBot `src/mcp_module/`（精简，独立实现）
- 静态配置（`[[mcp.servers]]`），不支持动态
- **验证**：`pytest tests/test_mcp_client.py`（mock stdio 子进程）

### M7：润色系统
- `polish.py`：
  - 拉取最近消息（条数跟随 MaiBot：chat.max_context_size / max_private_context_size）
  - 黑话机械匹配（复刻 jargon_context_matcher：Jargon + HighFrequencyTerm + 打分 + 前10条）
  - 风格 prompt 润色（ctx.llm.generate 一次调用）
  - L1/L3 最终结果直发
- `prompts/`：agent_system.md / title.md / polish.md
- **验证**：`pytest tests/test_polish.py`（mock ctx.db 黑话 + mock llm）

### M8：验证与交付
- 全量测试 + lint（ruff/pyright）
- README.md（安装、配置说明）
- config.toml 完整示例
- 部署到 MaiBot plugins/ 目录冒烟测试

---

## 3. 关键实现决策（来自设计冻结）

| 决策 | 内容 | 来源 |
|---|---|---|
| Agent 循环 | 插件自带（方案 B），不用 Maisaka 执行 | §2 |
| 任务分级 | L1/L2/L3，LLM 自动 + 用户覆盖 + 升级 | §3.2 |
| 回复润色 | L1/L3 直发（跟随 MaiBot 配置），L2 走 proactive | §9 |
| 黑话 | 复刻 jargon_context_matcher（ctx.db 客户端过滤） | §9.2 |
| Planner 工具 | 只暴露安全子集（防提示词注入） | §5.4 |
| 权限 | 人+群，私聊保底，群聊开关 | §8 |
| 摘要注入 | 混合方案 + EARLY + 哈希去重 | §5.3 |
| MCP | 插件自建客户端，静态配置 | §7 |
| 持久化 | sqlite + on_unload 清理 + 恢复 | §3.5 |

---

## 4. 风险与对策

| 风险 | 对策 |
|---|---|
| ctx.db 查 Jargon 的 filters 行为与预期不符 | 实现时先写最小验证脚本确认（filters 透传 database_service） |
| proactive 在非 focus 模式下的触发行为 | 参照 runtime.py enqueue_proactive_task，聚焦 L2 场景测试 |
| Hook 注入被其他插件覆盖 | EARLY order + 接受限制（文档已记录） |
| ctx.config.get 读全局配置的 key 路径漂移 | 读取时 try/except + 默认值兜底（40/60） |
| sqlite 并发写（多任务同时落盘） | asyncio 单事件循环内串行写 + WAL 模式 |

---

## 5. 完成标准

- [ ] 全部 8 个里程碑实现完成
- [ ] 所有单元测试通过（pytest）
- [ ] ruff / pyright 零错误
- [ ] 插件可在 MaiBot plugins/ 目录加载（冒烟测试）
- [ ] /task 命令与 Planner Tool 基本流程可用
- [ ] README + config.toml 示例齐全
