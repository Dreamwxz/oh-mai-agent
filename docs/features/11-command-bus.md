# 命令总线

## 设计目标

Agent 任务执行需要跨组件协作：调度器要往运行中的 AgentLoop 注入指令、唤醒等待回复的任务、取消或暂停执行；AgentLoop 完成或失败时要通知调度器释放并发额度。如果这些交互都走直接方法调用，调度器、任务管理器与执行循环会互相持有对方引用，形成紧耦合，任何一方的内部改动都会波及整条链路。

命令总线（`bus/`）解决的就是这个解耦问题。它定义一套统一的消息协议（命令），让生产者只面向总线发送消息，消费者只面向总线订阅消息，双方互不知晓对方存在。命令按 `task_id` 精准投递到目标 AgentLoop。总线是纯内部通信管道，不负责任务状态管理（由 `TaskRecord` 状态机负责）、不负责持久化（DB 是唯一共享状态）、不向外部暴露接口。

> 架构变更：v0.1.0 曾尝试把任务迁移到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配回退到进程内方案。回退时一并移除了字节帧序列化、`Transport` 协议与 `decode_frame` 帧解码。随后「完成通知统一为直接调用」落地，事件通道（`TaskEvent` / `EventKind` / `publish` / `listen_events`）失去生产者与消费者，已删除——当前总线只保留命令路由表，消息以类型化对象直接传递。

---

## 设计方案

### 消息模型：命令

总线传输一类消息，定义在 `bus/messages.py`：

- **命令（TaskCommand）**：从调度器/任务管理器发往运行中任务的控制指令。`CommandKind`（`bus/messages.py` 的 `CommandKind`）定义 5 种：`INJECT_INSTRUCTION`（注入用户指令）、`RESUME_REPLY`（用户回复唤醒）、`CANCEL`、`PAUSE`、`RESUME`。

命令是纯 dataclass（`@dataclass(slots=True)`），字段为 `task_id` / `kind` / `payload`，**无序列化方法**——v0.1.0 的 `to_dict()` / `from_dict()` / `ts` 时间戳字段已随跨进程方案移除。消息中不携带任何运行时对象（队列、Event、AgentLoop 引用）。

> 历史：终态事件（COMPLETED / FAILED / CANCELLED）曾由 AgentLoop 广播给调度器释放并发额度；「完成通知统一为直接调用」后该机制退役——执行器直接调用 `scheduler.on_task_completed`，时序可预期。事件通道代码已删除，需要时从 git 史找回。

### 路由与分发：TaskCommandBus

`TaskCommandBus`（`bus/command_bus.py` 的 `TaskCommandBus`）是总线核心，维护 `task_id → list[handler]` 路由表，提供一条分发路径：

- **命令发送 `send(cmd)`**（`TaskCommandBus.send`）：按 `cmd.task_id` 查路由表，同步调用该任务的全部订阅处理器。单处理器异常 log-and-continue，不中断后续订阅者。这个容错是刻意的：RESUME 分支含 store 写入，超时路径的 `bus.send(CANCEL)` 无守卫，若此处重新抛出，检查循环会被永久终止，并发额度随之泄漏。

订阅接口：`subscribe(task_id, handler)` 注册命令处理器，`unsubscribe(task_id)` 在任务结束后移除其全部处理器。

### 订阅生命周期

命令订阅跟随 AgentLoop 的执行生命周期，注册与清理都在循环内部完成：

- **主订阅**：AgentLoop 在 `run()` 入口 `bus.subscribe(task.id, self._on_bus_command)`（`executor/agent_loop.py` 的 `AgentLoop.run`），在 `finally` 块 `bus.unsubscribe(task.id)` 移除该任务的全部处理器，保证任务结束后路由表不残留。ask_user 挂起期间的唤醒**不需要临时订阅**——用户回复经 `chat.receive.after_process` Hook → `TaskControl.handle_user_reply` 发送 `RESUME_REPLY`，由主订阅 `_on_bus_command` 的 RESUME_REPLY 分支经 `set_user_reply()` 写入并 set resume_event（旧的 `_on_resume_reply` 重复订阅已移除）。

### 组装与数据流

总线在 `lifecycle.py` 的 `load_plugin` 中组装（`lifecycle.py` 的 `load_plugin`）：直接构造 `TaskCommandBus()`（无传输参数）。总线实例随后注入 `TaskScheduler` 与 `TaskManager`，并经 ExecutorFactory 传递到每个 AgentLoop。

```
调度器 / TaskControl                 TaskCommandBus                  AgentLoop
     │  send(TaskCommand)                 │                              │
     ├───────────────────────────────────►│  路由表本地分发              │
     │                                    ├─────────────────────────────►│  _on_bus_command()
```

生产与消费关系：

| 角色 | 组件 | 消息 |
|---|---|---|
| 命令生产者 | `TaskScheduler.cancel/pause/resume`（`core/scheduler.py`） | CANCEL / PAUSE / RESUME |
| 命令生产者 | `TaskControl.handle_injection`（`core/usecases/task_control.py`） | INJECT_INSTRUCTION |
| 命令生产者 | `TaskControl.handle_user_reply`（`core/usecases/task_control.py`） | RESUME_REPLY |
| 命令消费者 | `AgentLoop._on_bus_command`（`executor/agent_loop.py`） | 全部 5 种命令 |

命令消费端 `AgentLoop._on_bus_command`（`executor/agent_loop.py`）按 kind 分支：`INJECT_INSTRUCTION` 经 `push_injection()` 追加到注入队列（键 `META_INJECT_QUEUE`），下一轮循环消费；`RESUME_REPLY` 经 `set_user_reply()` 写入并 set 对应的 resume_event；`CANCEL` 置 `_cancelled` 标记并唤醒等待；`PAUSE` 置 `_paused` 并经 `set_coop_paused()` 写标记；`RESUME` 清标记恢复。

协作取消是总线价值的典型体现：调度器超时检测或用户取消时 `bus.send(CANCEL)`，AgentLoop 收到后置取消标记，在下一轮循环或等待点协作退出，而不是被外部强杀。

---

## 使用与配置

命令总线是纯内部机制，无独立配置项，也不需要用户操作。开发者接触它的场景有两类：

- **注入指令**：`/task ask` 命令与 Planner 的 `subagent_modify` 工具最终都经 `TaskManager.handle_injection`（`core/task_manager.py`）→ `TaskControl.handle_injection` 发送 `INJECT_INSTRUCTION`。
- **唤醒任务**：用户在聊天流回复后，`chat.receive.after_process` Hook 触发 `TaskManager.handle_user_reply`（`core/task_manager.py`）→ `TaskControl.handle_user_reply` 匹配 WAITING_INPUT 任务并发送 `RESUME_REPLY`。

### 已知限制

1. **仅进程内**：总线是纯进程内路由，消息以类型化对象传递，无外部通道（跨进程方案已废弃，不再规划）。
2. **命令不可确认**：`send()` 恒返回 `True`，无确认/超时/重试。若目标 AgentLoop 尚未注册处理器（如 `_try_start` 置 RUNNING 后、`run()` 订阅前的窗口），本地分发静默失败，调用方无法感知——这也是已知的取消竞态窗口（见 [调度器](./02-scheduler.md)）。
3. **消费者注册非显式**：AgentLoop 在 `run()` 中订阅，无集中注册机制。

### 相关文档

- [任务模型](./01-task-model.md)：任务状态机与两级任务模型
- [调度器](./02-scheduler.md)：CANCEL / PAUSE / RESUME 命令的生产方
- [AI 提问](./07-ask-user.md)：RESUME_REPLY 唤醒链路与 WAITING_INPUT 状态
- [持久化与恢复](./03-persistence-recovery.md)：WAITING_INPUT 任务重启后保持，回复仍可唤醒
