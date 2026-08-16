# Planner 看板

## 设计目标

主 Planner 负责调度后台子代理任务，但它默认对当前聊天流里有哪些任务需要它介入一无所知。Planner 看板（PlannerBoard）解决这个问题：在 Planner 每次发起 LLM 请求前，把当前流「需要 Planner 主动介入」的待办以 `<task_board>` XML 块注入到消息里。

**设计原则：hook 推事件，工具拉状态。** 看板只推送需要 Planner 动作的事件（waiting_input 待用户回复）；运行中 / 暂停 / 定时 / 已完成等状态快照一律不注入——用户询问任务状态时，Planner 用 `subagent_list` / `subagent_status` 等工具按需查询。这样绝大多数请求没有任何待办，注入为零开销，Planner 的上下文也免于被状态快照刷屏。

除待办看板外，PlannerBoard 还承担**插件能力简介**注入：每个会话首次请求时注入一次 `<plugin_intro>` 块，向 Planner 说明本插件是「后台子代理管理」系统（创建的任务由独立 Agent 在后台自主执行、结果自动汇报、可等待输入、可注入指令、可定时），帮助 Planner 建立正确的心智模型，在合适的时机想起 `subagent_*` 工具。

举一个场景：后台任务调用 `ask_user` 向用户提问后挂起（waiting_input）。没有看板时，Planner 不知道有任务在等回复，可能误以为任务已完成或对用户突然的回复感到困惑；有了看板，Planner 一眼读到待办，会主动引导用户「任务在等你回复」。同时任务挂起瞬间还会写入一条对用户不可见的上下文注释（见下文「等待留痕」），双通道保证 Planner 跨轮对话也能感知。

## 设计方案

### PlannerBoard 职责

PlannerBoard（planner_hooks.py）是看板的唯一实现，职责有三个：构建插件简介、构建待办摘要、注入去重。它持有 `TaskStore`（查询任务）、`PlannerBoardConfig`（开关与条数上限）与 `PromptService`（模板渲染），在 `load_plugin` 中实例化（lifecycle.py:133-138）。

### 注入时机

通过 `maisaka.planner.before_request` Hook 挂在每轮 Planner 请求前，注册为 BLOCKING + EARLY（plugin.py:550-558），保证注入先于其他 Hook，Planner 读到的是最新任务状态。Hook 处理函数是 `hook_before_request()`（planner_hooks.py），任何异常都兜底返回 continue，绝不阻断 Planner 主流程。

### 注入内容

两类内容，各自独立去重：

1. **插件简介（`<plugin_intro>`）**：静态文案，每会话首次请求注入一次（marker 检查——messages 中已有本 session 的 `<plugin_intro>` 即跳过）。即使当前没有任何待办，首个请求也会注入简介，保证每个会话的 Planner 至少获得一次心智模型。
2. **待办看板（`<task_board>`）**：当前流所有 WAITING_INPUT 任务，按 `updated_at` 升序（等待最久的在前）截取 `max_waiting` 条。marker + hash 双层去重：marker 判断本 session 是否已注入过看板，hash 判断内容是否变化；两者都命中才跳过。用户回复后任务恢复 RUNNING，下次构建待办为空，看板自动消失。

最终输出的注入内容类似：

```xml
<plugin_intro session="qq:g:123">
你是调度者：负责与用户对话、判断需求、管理后台子代理任务。后台子代理（subagent_* 任务）拥有比你更完整的能力集——文件读写、命令执行、记忆检索、并行子代理、MCP 全量工具——并可在后台自主多轮执行，不阻塞你的对话。
当用户需求超出你的能力边界（需要文件/命令处理、长时自主执行、完整工具集）时，用 subagent_create 委托执行；轻量的外部信息获取（如网页抓取）你可用 call_mcp_tool 直接完成，不必委托。
委托后可经 subagent_status 查进度、subagent_modify 注入指令、subagent_delete 取消、subagent_schedule 定时执行；任务等待用户输入（waiting_input）时，请引导用户直接回复即可。
</plugin_intro>

<task_board session="qq:g:123">
当前需要你处理的子代理任务：
待用户回复（任务在等待用户输入，请引导用户直接回复即可）：
- [waiting_input] 确认输出格式（已等待 3 分钟）[id:9d4e3b2c]
</task_board>
```

简介的叙事是 **Planner 视角的委托心智**：明确 Planner 是调度者、后台子代理能力更完整（文件/命令/记忆/并行子代理/MCP 全量），超出能力边界就委托——而不是罗列"插件有什么"。轻量外部信息（MCP 代理）Planner 自己做，不必委托。

每行末尾的 `[id:xxxxxxxx]` 是任务 ID 前 8 位短标识。它解决了 Planner 的一个常见误用：若看板只显示标题，Planner 想查任务详情时会把标题当作 `task_id` 传入 `subagent_status`，导致「任务不存在」。带短 ID 后 Planner 可以直接复制 ID；即使仍传标题，`TaskCrud.resolve_task`（core/usecases/task_crud.py）也会按唯一标题兜底解析。

### 流隔离：任务工具绑定当前会话

`subagent_*` 任务工具只允许操作**当前会话流**的任务，防止群 A 的对话跨流访问/操纵群 B 的 planner 任务。校验利用宿主注入指纹 `chat_id`（MaiBot Host 专用字段，schema 无此参数、LLM 无法伪造；宿主注入的 `stream_id` 恒等于 `chat_id`，见 `tools/send_message.py` 同款指纹）：handler 在 `tools/planner/task_tools.py` 经 `_current_stream_error` 比对 LLM 传入的 `stream_id` 与宿主注入的 `chat_id`，不一致即拒绝（"任务工具只能操作当前会话的任务"）。`chat_id` 缺失（无宿主注入环境，如测试直接调用）时放行，不误伤正常调用。`subagent_create` 的 `reply_stream_id` 参数保留跨流——它是结果投递目标，不是任务归属。

### 等待留痕（上下文注释）

任务挂起等待输入时（`AgentLoop._handle_ask_user`，executor/agent_loop.py），经 `on_waiting_note` 回调调用 `ReplySender.append_task_waiting_note`（executor/sender.py），写入一条对用户不可见的 `<plugin_context_note kind="task-waiting">` 上下文注释（经 `maisaka.context.append`），内容含**任务标题与 ask_user 的问题文本**。

为什么需要这条留痕：ask_user 的问题虽然发给了用户，但从未进入 Planner 的上下文。用户可能隔很久才回复，hook 注入的看板消息可能已被上下文窗口裁剪；而这条注释写入会话流，让 Planner 跨轮对话也能理解「用户这条回复是在回答哪个任务的提问」，从而正确衔接。恢复/取消时不追加新注释（旧注释自然留痕，噪声最小）。

### 摘要构建与渲染

`build_intro()` / `build_board()`（planner_hooks.py）是数据准备层：`build_intro` 直接渲染模板简介段；`build_board` 通过 `store.list(status=WAITING_INPUT, stream_id=...)` 查询等待任务，按等待时长排序截断，再委托渲染。全空则返回空串，让上层跳过注入。

非空时调用 `prompt_service.build("planner_board", ...)`。Builder（prompt/builders/planner_board.py）把 TaskRecord 预格式化为模板可迭代的 dict 列表，再经 Jinja2 模板渲染成 `<plugin_intro>` / `<task_board>` XML 块。数据筛选与展示格式在此解耦：数量限制、排序在 `build_board()` 完成；中文文案、XML 结构在模板 `prompt/templates/planner_board.md` 里。

### 注入流程

`hook_before_request()`（planner_hooks.py）按固定顺序串行处理：先检查 `enabled`，关闭则直接返回；再提取 `session_id` 与 `messages`，会话为空也跳过；随后依次处理简介（marker 去重）与待办（marker + hash 去重），任一内容非空则追加 system 消息到 messages 尾部，返回带 `modified_kwargs` 的完整消息列表。因为注册为 BLOCKING + EARLY，注入后的 messages 会传递给后续 Hook 和 Planner，保证内容入场。

## 使用与配置

### 配置节 [planner_board]

`PlannerBoardConfig`（config.py:153-172）对应 `config.toml` 的 `[planner_board]` 节：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否向 Planner 注入简介与待办看板；关闭后 Hook 直接返回 continue |
| `max_waiting` | `5` | 待用户回复任务条数上限（等待最久的优先展示） |

条数上限只控制注入条数，不影响 TaskStore 实际查询。配置热更新时 `apply_config_update`（lifecycle.py）会重建 PlannerBoard，并传入 `prompt_service`，同时清空 hash 去重状态，保证下次请求重新注入看板。这意味着改看板配置无需重启插件，WebUI 保存后立即生效。

对一般用户而言，看板默认开启、无需任何配置即可工作；只有当消息里不断出现冗余的任务摘要、影响回复质量时，才需要把 `enabled` 调成 `false` 关闭注入。

若想调整看板文案或结构，直接编辑模板 `prompt/templates/planner_board.md` 即可，无需改动 Python 代码；但需保持模板变量与 PlannerBoardBuilder 传入的字段一致，否则渲染会因变量缺失而报错。

### 已知限制

- `_last_board_hash` 是内存映射（planner_hooks.py），插件重启后丢失，首个请求必然重新注入看板。
- marker 检查只扫描字符串类型的 content，多模态列表内容会被跳过，可能误判无 marker 而重复注入。
- Hook 对 Planner 所有请求一视同仁，不区分工具调用、消息回复或错误重试，无法按请求类型裁剪注入。
- 上下文注释同样受上下文窗口裁剪影响；它提供的是「对话流内留痕」而非「永久记忆」。

### 相关文档

- [提示词系统](./12-prompt.md)：planner_board builder 与模板渲染体系
- [工具系统](./05-tools.md)：subagent_* 工具与 deferred 发现机制
- [命令系统](./13-commands.md)：任务管理命令与状态查看
- [配置体系](./14-config.md)：配置节总体说明与热更新机制
