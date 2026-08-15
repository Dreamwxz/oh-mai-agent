# AGENTS.md：oh-mai-agent 开发者速查

> 本文是编码 agent / 新贡献者的**最少必知**速查。用户向文档见 [README.md](README.md)。

## 一句话定位

oh-mai-agent 是 MaiBot 的离线多线程 Agent 插件（Python ≥ 3.10，插件 ID `oh-mai-agent`），
在独立 Runner 进程中为 MaiBot 提供任务创建、调度、执行与持久化能力，不阻塞 Host 主流程。

## 架构速览

核心围绕五个概念组织，每个都先讲清「解决什么问题」，再给关键代码位置。

**任务分级与两级工具模型**。离线任务要区分「一次即时动作」和「长时自主循环」，故 TaskLevel
枚举（`domain/task_record.py:26-29`）定义 `instant` / `agent` 两级。工具呈现同理分两层：
Essential 层仅 `ask_user`（始终可见）；Discoverable 层按角色过滤后由 Agent 循环**直接全量
暴露**（每轮全部进 `tools` 参数，LLM 看到真实工具名如 `mcp_fetch_fetch`；`list_tools` /
`get_tool_schema` 合成发现工具降级为兜底，`tools/synthetic/discovery.py`）。主 Planner 只见
11 个 @Tool 安全子集（`tools/planner/` handler 工厂 + `plugin.py` 注册）。

**命令总线与调度链**。任务执行需要跨组件协作（注入指令、唤醒、取消、暂停），故有
TaskCommandBus（`bus/command_bus.py`，进程内命令路由表，无传输/序列化层）
路由命令 INJECT_INSTRUCTION / RESUME_REPLY / CANCEL / PAUSE / RESUME（事件
通道 COMPLETED / FAILED / CANCELLED 已随「完成通知统一为直接调用」删除，
见下）。
调度链：TaskManager（门面）→ TaskCrud / TaskControl（`core/usecases/`）→ ExecutorFactory
→ InstantExecutor（进程内同步）/ AgentExecutor（AgentLoop 最大 30 轮，经
`ctx.llm.generate_with_tools(model=planner, timeout_ms=240000)` 调用 LLM）。
任务进入终态的完成通知**统一走直接调用** `scheduler.on_task_completed`：InstantExecutor
经 `complete_and_notify` / `fail_task`，AgentExecutor 把 `on_task_done` 回调绑定到
`ctx.scheduler.on_task_completed` 注入 AgentLoop——同步释放并发额度 + CRON 重排，
不经过事件总线（时序可预期）。主 Agent 还可经 `ask_subagent` / `ask_subagents` 工具派发进程内子 Agent
（AgentLoop 合成分支 `_run_subagent` / `_run_subagents` 实例化 `executor/subagent.py` 的
SubAgentLoop，并行工具轮 + 答案回传主循环继续判断；取消经注入 `lambda: loop.is_cancelled`
传导，工具层不 import executor）。
执行期上下文经 `executor/context.py` 的 `current_task` ContextVar 传递（唯一 set 方
AgentExecutor.execute，finally reset 防并发泄漏），`make_role_provider` 按任务 owner/stream_id
构造角色回调。回复链路统一走 `executor/sender.py` 的 `ReplySender` 两条出口：
`send_raw`（直发：分割 + 重试，无润色，命令/失败通知等确定性文本）与
`send_polished`（完整：信息获取 → PolishService 润色 → 复用直发段，任务回复/提问/
send_message）；发送出口纯发送（不写 context），跨流动机注释经独立能力
`append_motivation_note` 显式写入（对用户不可见，写给 MaiBot/Planner 上下文）。
重试次数读 `[send] max_retries`（默认 3，指数退避 1s → 2s），分割跟随 `[splitter]`。

**权限与隔离**。guest / user / admin 三级角色，PermissionResolver（`permission.py`）按配置判定。
每个 ToolDefinition 有 `min_role` 做角色过滤；文件工具二次经 FileAccessPolicy 沙箱校验，
user 级隔离到 `data_dir/files/`。api_expose 暴露 6 个跨插件端点
（create/list/get/cancel/inject/history）。

**提示词体系**。所有 prompt 经 PromptManager.render() / PromptService.build() 生成，模板在
`prompt/templates/`（固定中文，无 i18n），builder 注册在 `prompt/builders/`（7 个：
agent_system / title / polish / planner_board / injection / context_note / subagent_system）。

**持久化**。TaskRecord（`domain/task_record.py`）是唯一可落库形态，JSON 序列化到 sqlite。
运行时对象（queue、Event、AgentLoop 引用）绝不写入 metadata 落库。状态变更必须走
`task.transition(new_status)`（受限状态机）或 `task.force(new_status)`（强制兜底），直接赋值
抛 TaskStatusError；TaskStore.save 带 `expected_status` CAS 守卫防并发覆盖终态。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁到独立子进程（WorkerManager + StdioTransport），
> 后因复杂度与收益不匹配回退到进程内 contextvars + usecase 分层（`executor/context.py` +
> `core/usecases/`）。当前实现全部在 Runner 进程内。

## 目录导航

| 目录 | 用途 |
|---|---|
| [README.md](README.md) | 用户入口：安装 / 配置速查 / 命令用法 / 功能索引 |
| [docs/](docs/) | LIFECYCLE 生命周期总览 + features/（15 份功能文档）+ history/（4 份归档） |
| [tests/](tests/) | 测试：58 文件 999 个测试函数，pytest 验证 0 失败 + 文档引用检查（test_doc_links.py） |

**目录结构**（散文式，不用多级缩进树）：

- 根目录单文件模块：`plugin.py`（入口，注册 11 个 @Tool / 7 @Command）、`config.py`（10 节配置）、
  `permission.py`、`api_expose.py`、`planner_hooks.py`、`commands.py`、`lifecycle.py`
  （唯一组装点，load_plugin 编排全部依赖）、`_manifest.json`
- `bus/`：命令总线，消息类型（`bus/messages.py` 纯 dataclass）+ TaskCommandBus 命令路由
  （按 task_id 精准投递；事件通道已随「完成通知统一为直接调用」删除）
- `core/`：编排层（TaskScheduler / TaskManager 门面 + core/usecases/ 下沉 TaskControl 与
  TaskCrud——控制三件套 cancel/pause/resume 在 TaskControl，CRUD 在 TaskCrud）
- `domain/`：领域模型与持久化，TaskRecord 状态机 + TaskStore + Recovery + StatusFormatter
  + 流/owner 身份值对象（stream_ref.py，`:group:` / `planner:` 语义唯一出处）
- `executor/`：执行层——AgentLoop 执行引擎（agent_loop.py）+ current_task 执行上下文（context.py）+ 发送基础设施（sender.py，ReplySender / PolishService / fail_task，全插件共用）+ ExecutorFactory 按级分发 InstantExecutor（进程内）/ AgentExecutor + 工具装配（tool_registrar.py，TaskManager.setup() 委托其注册全部 Agent 工具）
- `tools/mcp/`：MCP 工具提供方，MCPConnection（stdio/http/sse）+ MCPManager + presets（内置 fetch/exa 预设）
- `prompt/`：提示词系统，manager / service / base + builders/（7）+ templates/（7 中文模板）
- `tools/`：工具系统三通道（agent 循环工具（含子 Agent 工具 `subagent_tool.py`）/ planner @Tool 工厂 / synthetic 发现工具）+ 发送工具共用实现 `tools/send_message.py` + MCP 工具提供方（tools/mcp/）
- `tests/`：测试（58 文件，999+ 测试函数，0 失败）+ 文档引用检查（test_doc_links.py）
- `docs/` + `data/`：功能文档（15 + LIFECYCLE + 4 归档）与运行时数据（gitignored）

> 依赖方向：root（plugin.py/lifecycle.py 组装根）→ core（编排）→ executor（执行）→ {tools, prompt, bus} → domain。
> `config.py` / `permission.py` 是**共享叶子层**：虽物理位于根目录，实际被全部层级 import
> （28 处），自身只依赖 SDK / domain —— 视为与 domain 并列的共享内核，不是组装根的一部分。

## 相关资源（同级目录）

| 位置 | 用途 |
|---|---|
| `../MaiBot_docs/zh/plugin/` | **插件开发文档（最有价值）**：index 架构总览 + lifecycle / hooks / commands / tools / config / manifest / api-reference 等 15 篇 |
| `../MaiBot/` | MaiBot 本体源码：Host/Runner IPC、`plugins/` 真实插件示例、`docs/` |
| `../maibot-plugin-sdk/` | 插件 SDK 源码与文档（`docs/guide.md` + `docs/migration-guide.md`） |

## 提示词硬性规则

**禁止内联提示词**。LLM 提示词（系统提示、注入指令、上下文注释、看板、标题、润色）
必须经 `prompt/templates/` 模板渲染（`PromptManager.render()` /
`PromptService.build()`），**代码里禁止内联提示词**文本，包括 f-string 拼接、字符
串常量、`lines.append` 手拼。

**豁免**。用户可见聊天回复文本（`commands.py` 的回复内容）不属于提示词，不适用此规则。

**验证方式**。可 grep `prompt/builders/` 下无 f-string 内联提示词、无 `{{` 占位符
残留（提示：`grep -rn "{{" prompt/builders/ --include="*.py"`）。

**模板纪律**。模板只含纯文本 + Jinja2 语法，变量声明必须与 `prompt/templates/index.json`
一致；XML 转义在 builder 侧完成（`xml.sax.saxutils.escape`），模板 `autoescape=False`。

## 文档引用纪律

**禁止新写裸行号引用**。文档引用代码一律用 `path.py` + 符号名（类/方法名），
不用 `path.py:NNN` 裸行号。存量行号引用（约 300+ 处）不一次性清理，
随重构顺手替换；`tests/test_doc_links.py` 只校验「文件存在 + 行号不越界」，
不校验语义，属最小兜底。

**移动/重命名 = 全仓清理 + 同 commit**。`git mv` 或重命名任何 .py 文件时，
必须 `grep -rn "旧路径|旧名"` 全仓（含 docs/、AGENTS.md、README.md）清理
所有引用，且文档更新与文件移动在同一 commit 内完成，不留中间态。

**归档豁免**。`docs/history/` 是历史快照，不更新、不检查
（`tests/test_doc_links.py` 豁免该目录）。

**历史叙事标注**。features/ 文档中的「架构变更」块若引用已移动或已删除
的路径，改写为现状路径或标注「（历史）」，避免误导读者。

## 开发约定

**测试**。项目用 uv 管理依赖（`uv.lock` + `.venv`）。测试依赖 pytest + pytest-asyncio
+ maibot-plugin-sdk（PyPI，≥ 2.6.0），`--with` 是 uv 的临时附加依赖语法：

```bash
uv run --with maibot-plugin-sdk pytest tests/ -q
```

共 999 个测试函数，pytest 验证 0 失败。conftest 将项目根挂载为 `oh_mai_agent` 包。
约定：mock LLM 和 transport，**不 mock 持久化**（使用 real_store 即真 sqlite）。

**Lint / 类型检查**。项目未配置形式化 lint 或类型检查工具（`pyproject.toml` 中无
ruff / mypy / pylint 配置节），需依赖代码审查。

**提交**。使用 conventional commits，格式 `type(scope): description`。参考仓库历史：

```
docs: rewrite AGENT.md as dev quick-reference
chore: drop docs/ path references from code comments
feat(api): accept reply_stream_id in task create entry points
refactor(domain): remove planner task level and legacy level map
```

## 已知限制

以下为代码中已识别的限制，均在功能文档中详述，任务范围内不修复：

| 位置 | 问题 | 详见 |
|---|---|---|
| `api_expose.py` | `max_level` 声明但未执行，6 个端点全部 public=true | [04-permission](docs/features/04-permission.md) |
| `domain/task_record.py` | 无 schema 版本管理，数据模型升级无法自动迁移 | [01-task-model](docs/features/01-task-model.md) |
| `plugin.py` | 对外 @Tool 名 `task_delete` 与 @Command 名 `task_cancel` 不一致 | [01-task-model](docs/features/01-task-model.md) |
| `config.py [task]` | `default_timeout_min` 已配置但实际未执行 | [14-config](docs/features/14-config.md) |
| `config.py [task]` | `persist_history` 已配置但实际未执行（任务历史始终持久化） | [03-persistence-recovery](docs/features/03-persistence-recovery.md) |
| `tools/mcp/connection.py` | stdio 发送侧固定 newline 帧（适配 MCP SDK <2.0；读取侧双格式兼容）；若宿主切换 mcp SDK 2.0 需适配发送侧，manifest 已 pin `mcp>=1.1.3,<2.0.0` | [08-mcp](docs/features/08-mcp.md) |
| 代码注释 12 处 | 裸 § 节号引用残留（plugin.py / domain / tools 等文件） | 已知遗留，不修复 |
