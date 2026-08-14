# AI 提问（ask_user）

`ask_user` 是 Agent 循环中唯一需要人类介入的工具：Agent 执行长时任务时，如果缺少必要信息，可以暂停当前任务向任务所有者提问，用户直接在聊天中回复，任务随即自动恢复继续执行。

---

## 设计目标

Agent 自主执行并非总是「一条路走到黑」。长时任务常常在运行中才暴露出信息缺口：需要用户做选择、确认某个前提、或者补充一段无法从工具中获取的上下文。如果遇到这种情况只能结束任务重新创建，已经消耗的 LLM 轮次和工具调用结果全部作废，Agent 循环的上下文也会丢失。

`ask_user` 要解决的问题就是：**让 Agent 在执行中途暂停，向用户提问，收到回复后从暂停点继续**。它把「人机交互」变成 Agent 循环的一个普通分支，而不是任务的终结。任务从 RUNNING 进入 WAITING_INPUT 挂起，用户回复后转回 RUNNING，Agent 把回复作为 LLM 上下文继续推理，不满意还可以再次提问，形成多轮问答。

设计上有两条硬约束。一是**状态必须可持久化**：WAITING_INPUT 是任务模型的正式状态，落盘后插件重启也能原样恢复，挂起中的任务不会因为进程重启而丢失；二是**交互必须异步**：Agent 的提问通过消息发送，用户的回复通过消息 Hook 回来，中间 Agent 不占任何 LLM 调用，只是安静地等待，不阻塞调度器的其他任务执行。

---

## 设计方案

### 工具形态：essential 级，始终可见

`ask_user` 定义为 `visibility="essential"` 的 ToolDefinition（`tools/agent/ask_tool.py:108-115`），在 Agent 循环中始终出现在 LLM 的 tools 参数里，无需经 `list_tools` 发现。这是唯一一个 essential 级工具：提问能力是 Agent 循环的兜底交互通道，不能依赖按需发现，否则 Agent 恰恰在最需要提问的时候看不到它。工具参数为 `question`（必填）与 `stream_id`、`context`（可选），最低调用角色为 USER。

值得注意，`ask_tool.py` 只是定义工具外壳，其 handler 仅负责把问题文本交给注入的 `ask_callback` 回调（`tools/agent/ask_tool.py:41-81`），真正的挂起与恢复状态转换完全由 AgentLoop 内部完成。

### 完整流程：一次提问的往返

```
Agent 调用 ask_user
   │
   ▼
_handle_ask_user（executor/agent_loop.py 的 _handle_ask_user）
   建 resume_event → transition(WAITING_INPUT) + save
   → on_ask 发问 → await resume_event.wait()  ← 挂起
   │
   ▼
用户回复 → chat.receive.after_process Hook（plugin.py:532-537）
   │
   ▼
TaskManager.handle_user_reply（task_manager.py:374）
   → TaskControl.handle_user_reply（task_control.py:86-124）
     查 WAITING_INPUT + owner 精确匹配 → 经 set_user_reply() 写入 → bus.send(RESUME_REPLY)
   │
   ▼
AgentLoop._on_bus_command（agent_loop.py:273-279）
   经 set_user_reply() 写入 + resume_event.set()
   │
   ▼
_handle_ask_user 恢复：transition(RUNNING) → 返回 reply 给 Agent
```

### 挂起：先登记再等待，保证任何时刻都能被唤醒

挂起逻辑集中在 `AgentLoop._handle_ask_user()`（`executor/agent_loop.py` 的 `_handle_ask_user`），顺序是刻意的：

1. **先建恢复信号**：创建 `asyncio.Event` 并登记到 `_resume_events[task.id]`。这个 Event 是进程内对象，只存实例属性，不落库。
2. **再改状态**：`task.transition(WAITING_INPUT)` 并 save 落盘，保证重启后任务恢复为 WAITING_INPUT 而非 RUNNING。
3. **发问**：调用 `on_ask(stream_id, question)` 回调把问题发送到聊天流。
4. **阻塞等待**：`await resume_event.wait()` 无限期挂起，直到收到恢复信号。
5. **恢复**：收到信号后 `transition(RUNNING)` 并 save，经 `take_user_reply()` 弹出回复作为工具返回值交给 Agent。

唤醒不依赖事件广播：用户回复经 Hook → `TaskControl.handle_user_reply` → `bus.send(RESUME_REPLY)`，由主订阅 `_on_bus_command` 的 RESUME_REPLY 分支经 `set_user_reply()` 写入并 set 恢复事件（v0.1.0 的 `WAITING_INPUT` 事件与 ask_user 临时订阅 `_on_resume_reply` 已移除）。

「先登记 Event、再改状态、后发问」的顺序保证挂起流程任意时刻都具备恢复能力：即使问题发送失败，Event 已经存在，用户回复或取消命令依然能唤醒任务。

**无 on_ask 回调的兜底**：如果 AgentLoop 初始化时未注入 `on_ask` 回调，直接 `resume_event.set()` 恢复任务并返回错误，避免任务因无人提问而永久挂死。

### 回复链路：一次回复，精确唤醒一个任务

用户回复经三段链路回到挂起的 AgentLoop：

1. **Hook 入口**：插件注册 `chat.receive.after_process` Hook（`plugin.py:532-537`，name=`agent_user_reply`，OBSERVE 模式），从 message 中提取 `session_id` → stream_id、`user_info.user_id` → user_id、`processed_plain_text` → 回复文本（`plugin.py:550-556`），任一字段为空直接跳过。
2. **门面转发**：`TaskManager.handle_user_reply()`（`core/task_manager.py:374`）是门面方法，转发给 TaskControl。
3. **匹配与唤醒**：`TaskControl.handle_user_reply()`（`core/usecases/task_control.py:86-124`）按 `stream_id` 前缀解析出 platform，拼成 `full_owner = platform:user_id` 与任务 owner 做精确比较（`task_control.py:94-95`），在查询结果中找第一个 owner 匹配且仍为 WAITING_INPUT 的任务（二次 `store.get` 确认，防状态已变），把回复经 `set_user_reply()` 写入并落盘，再 `command_bus.send(RESUME_REPLY)`（`task_control.py:110-116`），随后 `return`，**单次只唤醒第一个匹配任务**。

AgentLoop 侧，`_on_bus_command()` 收到 RESUME_REPLY 后把回复经 `set_user_reply()` 写入并 `resume_event.set()`（`executor/agent_loop.py:273-279`），与挂起流程第 6 步的 `wait()` 衔接，任务恢复。

匹配按 `stream_id + owner` 精确比较而非消息内容，是刻意的安全设计：只有任务所有者（或同流同平台用户）的回复才能唤醒任务，避免群聊中无关消息误触发。恢复动作还受 `task_control.py` 的二次状态校验保护，用户回复恰逢任务被取消时不会错误复活终态任务。

### 迁移说明

v0.1.0 回退子进程架构时，回复匹配与唤醒逻辑随执行控制一并下沉到 `core/usecases/task_control.py`，`TaskManager` 保留公共签名作门面，外部调用点（Hook、命令）无需感知这一变化。

---

## 使用与配置

### 用户如何回复

用户无需任何命令，直接在任务所在的聊天流里回复即可。回复进入 `chat.receive.after_process` Hook 后自动匹配唤醒任务。同流内多个等待任务并存时，一条回复唤醒最先匹配到的一个；任务不区分回复文本内容，也不要求以 `/task` 前缀开头。

用户侧看到的是普通聊天消息：Agent 的提问（含可选的 `[上下文]` 补充说明）作为一条消息发到聊天流，回复后任务继续，Agent 可能再次提问或直接给出最终结果。提问文本在日志中截断到前 80 字符，避免敏感内容刷屏。

### 挂起中的任务如何结束

WAITING_INPUT 不是终态，任务可以经两条路径离开挂起：收到匹配回复后恢复 RUNNING 继续执行；或被 `/task cancel` 取消。取消经命令总线发送 CANCEL，AgentLoop 置 `_cancelled` 并 set 恢复事件（`executor/agent_loop.py` 的 `_on_bus_command` CANCEL 分支），`_handle_ask_user` 的 `wait()` 随即返回并上报 cancelled。

### 插件重启后的恢复

WAITING_INPUT 状态落盘，插件重启时 `recover_active_tasks()` 对这类任务执行 `RecoveryAction.WAITING`（`lifecycle.py:271-275`），保持状态不变。旧的 `asyncio.Event` 随进程消失，但任务仍在存储中，用户后续回复经 Hook 重新走完整条唤醒链路，任务照常恢复。

### 超时行为

WAITING_INPUT **永不超时**。`config.py` 的 `default_timeout_min`（`config.py:124`）虽声明了默认 10 分钟，但当前实现未消费该值：`resume_event.wait()` 无超时参数，调度器的 `_check_loop`（`core/scheduler.py:464`）只检查 RUNNING 任务的 `max_runtime_min`，不检查 WAITING_INPUT 停留时长。任务进入挂起后一直等待，直到收到回复或被手动取消；期间回复随时到达都能唤醒。

### 相关文档

- [01-任务模型](./01-task-model.md)：WAITING_INPUT 状态在状态机中的位置与合法转换
- [05-工具系统](./05-tools.md)：essential / discoverable 两级工具呈现
- [11-命令总线](./11-command-bus.md)：RESUME_REPLY 命令的唤醒链路
- [12-提示词系统](./12-prompt.md)：Agent 系统提示词如何引导提问行为

### 已知限制

1. **WAITING_INPUT 无超时**：`default_timeout_min` 已声明未执行，任务可无限期挂起，需要用户主动回复或手动取消（见「超时行为」）。
2. **单次仅唤醒一个任务**：同一 owner 在同流有多个 WAITING_INPUT 任务时，一条回复只唤醒第一个匹配项，无法按回复内容路由到更合适的任务。
