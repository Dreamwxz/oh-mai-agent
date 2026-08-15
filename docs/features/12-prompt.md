# 提示词系统

## 设计目标

提示词系统要解决的核心问题是：**LLM 提示词散落在代码里，改一处要翻遍整个插件**。Agent 循环的 system prompt、标题生成、回复润色、Planner 看板、指令注入、动机小提示，这些场景都要向 LLM 喂不同的中文提示词。如果每个场景都在代码里用 f-string 或字符串常量手拼，提示词和逻辑就缠在一起：改文案要动代码，加场景要复制粘贴，提示词内容也无法被审查和测试。

所以这个设计把「提示词内容」和「构建逻辑」彻底分开：**提示词文本全部收进 `prompt/templates/` 的模板文件，代码只负责按场景选模板、填变量**。这是一条硬规则——代码里禁止内联提示词（f-string、字符串常量、`lines.append` 手拼都不行），所有提示词必须经模板渲染。模板固定中文，不做 i18n。

模板化带来的三个直接收益：**单一事实来源**（改文案只动 `.md` 文件，不动代码）、**可审查**（提示词集中一处，可逐条 review 语气与内容）、**可测试**（渲染输入输出可断言，变量校验失败即抛错）。代价是引入一层间接——builder 必须正确声明变量、调用方必须传全变量，否则渲染期直接失败，把「提示词写错」从静默的坏输出变成显式异常。

> 架构变更：早期 Builder 曾内置硬编码 fallback 常量，在 `PromptManager` 缺失时回退。重构后这些常量全部移除，Builder 强制依赖 `PromptManager` 注入，不再有任何内置回退。

## 设计方案

提示词系统分两层：**`PromptManager` 管模板，`PromptService` 管分发**。二者在插件组装时创建（`lifecycle.py:89-93`），`PromptManager` 指向 `prompt/templates/` 目录，`PromptService` 接收 `ALL_BUILDERS` 注册表。

**PromptManager（`prompt/manager.py`）** 是模板的注册与渲染中心。它由 `index.json` 驱动——`index.json` 声明每个模板的 `path` 和 `variables`，`PromptManager` 据此构建模板元数据（`manager.py:50-82`）。渲染用 Jinja2，`StrictUndefined` 保证模板里引用了未声明变量时抛错而非静默变空串，`autoescape=False` 防止提示词内容被 HTML 转义（`manager.py:131-136`）。`render()` 在渲染前校验变量：传入变量必须覆盖模板声明的全部变量（缺则 `ValueError`），也不允许传未声明的额外变量（多则 `ValueError`），模板名不存在则 `KeyError`（`manager.py:115-126`）。模板内容懒加载并缓存。

**PromptService（`prompt/service.py`）** 是统一构建入口。`build(name, task=..., **kwargs)` 按 name 查注册表，构造 `PromptContext` 后调用对应 builder（`service.py:46-76`）。构造时若 builder 未注入 `_pm`，会自动回填 manager，让所有 builder 共享同一渲染入口（`service.py:37-38`）。调用方不直接接触 builder 实例，只认 name 字符串——这层间接让「换实现」和「加场景」都不动调用点。

**基础设施（`prompt/base.py`）** 定义两个通用类型：`PromptContext` 是不可变数据类（`frozen=True, slots=True`），携带 `task` 引用与任意 `data` 键值参数字典（`base.py:21-32`）；`PromptBuilder` 是抽象基类，声明唯一 `name` 属性与 `build(ctx)` 方法，可选持有 `_pm` 引用（`base.py:35-65`）。builder 未注入 `_pm` 时 `build()` 抛 `RuntimeError`，无内置 fallback。

**7 个 Builder（`prompt/builders/__init__.py` 的 `ALL_BUILDERS`）** 各管一个场景，对应 7 个模板：

| Builder | name | 场景 | 模板 |
|---|---|---|---|
| `AgentSystemBuilder` | `agent_system` | Agent 循环的 system prompt，注入任务标题、意图与机器人昵称 | `agent_system.md` |
| `TitleBuilder` | `title` | LLM 生成 15 字内标题 | `title.md` |
| `PolishBuilder` | `polish` | 回复润色的 system prompt，注入聊天上下文、黑话表、原始结果、主程序人格/表达风格与昵称 | `polish.md` |
| `PlannerBoardBuilder` | `planner_board` | Planner 看板 XML 块，注入活跃/定时/最近任务 | `planner_board.md` |
| `InjectionMessageBuilder` | `injection` | 指令注入的 system 消息格式化 | `injection.md` |
| `ContextNoteBuilder` | `context_note` | 跨流/长时任务的动机小提示 | `context_note.md` |
| `SubAgentSystemBuilder` | `subagent_system` | 子 Agent 的 system prompt，注入意图、工具列表与机器人昵称 | `subagent_system.md` |

每个 builder 的 `build(ctx)` 都从 `ctx.task` / `ctx.data` 提取参数，调用 `self._pm.render(name, ...)` 渲染对应模板。**XML 转义在 builder 侧完成**：injection / context_note 在把变量传入模板前先经 `xml.sax.saxutils.escape` 转义（如 `context_note.py:60-65`），防止注入内容拆出 XML 块；模板侧 `autoescape=False`，不会二次转义。二者分工明确。

**no-inline-prompt 规则的验证方式**：`grep -rn "{{" prompt/builders/ --include="*.py"` 应无占位符残留（占位符只能出现在模板文件）；builders 内的 f-string 仅用于错误文案、日志、变量格式化，不构成内联提示词。

**一次典型构建的数据流**：调用方 `prompt_service.build("agent_system", task=task)` → `PromptService` 查注册表拿到 `AgentSystemBuilder` → 构造 `PromptContext(task, data)` → `builder.build(ctx)` 从 `ctx.task` 提取 title/intent → `self._pm.render("agent_system", title=..., intent=...)` → `PromptManager` 校验变量、懒加载模板、Jinja2 渲染 → 返回提示词字符串。整条链路无一处内联提示词，调用方只认 name，builder 只认模板名。

**注入点**集中在 Agent 循环（`executor/agent_loop.py`）：

- **system prompt 构建**：AgentLoop 启动时 `prompt_service.build("agent_system", task=task)` 生成 system prompt（`agent_loop.py:378-383`）。
- **指令注入消费**：每轮 LLM 调用前 `_consume_injections()` 经 `take_injections()` 弹出待注入指令，逐条经 `_build_injection_message()` 格式化为 system 消息插入，并落历史（`agent_loop.py:234-256`）。
- **注入消息格式化**：`_build_injection_message()` 调用 `prompt_service.build("injection", instruction=...)`（`agent_loop.py:227-233`）。历史回放时 injection 条目同样重建为 system 消息，保证恢复后上下文一致（`agent_loop.py:390-394`）。

其余 builder 的调用点：`title` 在任务创建时（`lifecycle.py` 的 `llm_title` 回调），`polish` 在 `PolishService.polish()`（`executor/sender.py`），`planner_board` 在 `PlannerBoard.build_summary()`（`planner_hooks.py:120`），`context_note` 在 `ReplySender.append_motivation_note()`（跨流回复补写动机注释，`executor/sender.py`）。

**为什么 builder 不入插件主流程的 pydantic 配置**：提示词是内容不是参数，模板本身就是配置。把模板目录视为「提示词的配置节」，`index.json` 是其 schema——这比给每个提示词加配置项更简单，也让提示词与代码解耦得更彻底。

**与插件生命周期的关系**：提示词系统是纯被动的服务，不持有自己的生命周期——它由 `lifecycle.py` 组装时创建（`lifecycle.py:89-93`），随后被注入到 `TaskManager`、`AgentLoop`、`PolishService`、`PlannerBoard` 等消费方，随插件启停。它不参与任务状态机，也不感知调度，只回答一个问题：给定场景和参数，返回渲染好的提示词字符串。

## 使用与配置

提示词系统没有显式配置节——所有「配置」以模板文件形式存在。开发者与提示词交互的方式是**改模板、加模板、加 builder**。

**模板清单（`prompt/templates/index.json`）** 声明 7 个模板，每个含 `path` 与 `variables`：

| 模板 | 变量 |
|---|---|
| `agent_system.md` | `title`, `intent`, `bot_name` |
| `title.md` | `intent` |
| `polish.md` | `context`, `jargon`, `result`, `requester`, `personality`, `reply_style`, `bot_name` |
| `planner_board.md` | `session_id`, `active`, `scheduled`, `recent` |
| `injection.md` | `instruction`, `note_id` |
| `context_note.md` | `kind`, `content`, `note_id`, `bot_name` |
| `subagent_system.md` | `intent`, `tool_list`, `bot_name` |

**新增一个提示词场景**的步骤：在 `prompt/templates/` 下创建 `.md` 模板（用 `{{var}}` 占位符）→ 在 `index.json` 注册 `path` 与 `variables` → 在 `prompt/builders/` 新建 builder 子类（声明 `name`、实现 `build(ctx)` 调 `self._pm.render()`）→ 在 `ALL_BUILDERS` 列表注册实例（`prompt/builders/__init__.py`）→ 调用方经 `prompt_service.build("xxx", ...)` 使用。模板变量声明必须与 `index.json` 一致，XML 转义在 builder 侧完成。

**prompt_service 的消费方**：`AgentLoop`（agent_system / injection）、`TaskCrud` 的标题回调（title）、`PolishService`（polish）、`PlannerBoard`（planner_board）、`ReplySender.append_motivation_note`（context_note，跨流回复的动机注释）。它们都经 `lifecycle.py` 组装时注入的同一个 `prompt_service` 实例，保证全插件提示词走同一渲染入口。

**工具描述纪律**。工具描述（`ToolDefinition.description`）面向 LLM，与 JSON Schema（`parameters`）面向函数调用引擎分工不同。描述应告诉模型**何时用、为什么用、失败会怎样**（surface），禁止描述内部实现细节（machinery），如「调用 XX API」「查询 XX 表」。

| ✅ 描述（surface） | ❌ 描述（machinery） |
|---|---|
| 「搜索用户：按昵称/名字/ID 搜索已知用户与群，返回其 user_id（QQ号）、昵称、群信息，用于确定 send_message 的发送目标。」 | 「调用 MaibaChat API 的 /v1/user/search 端点」 |
| 「获取最近聊天历史：拉取指定聊天流的最近消息记录。」 | 「从 chat_records 表查询最近 N 条消息」 |

参数名、类型、必填、默认值、枚举、数值边界由 schema 承载，禁止在 description 中重复罗列；而使用时机、目的、失败模式、枚举中文释义、行为边界这些 schema 之外的信息必须留在 description 里。新增或修改工具时按此 checklist 审查：description 不含「参数:」前缀段落、无与 schema 重复的参数罗列、schema 之外的独有信息（WHEN/WHY/失败模式/枚举释义）已在 description 中、数值边界若未在 schema 中声明则写入 description。风格规范详见 [提示词写作规范](../prompt-style-guide.md)。

**已知限制**：

- **固定中文，无 i18n**：模板固定为中文，`PromptManager` 不感知多语言，已放弃多语言扩展。
- **模板不支持热重载**：模板内容懒加载后缓存，修改 `.md` 文件需重启插件生效。

**相关文档**：[Planner 看板](./09-planner-board.md)、[AI 提问](./07-ask-user.md)、[命令系统](./13-commands.md)。
