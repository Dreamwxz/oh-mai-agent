# 命令总线

## 设计目标

Agent 任务执行需要跨组件协作：调度器要往运行中的 AgentLoop 注入指令、唤醒等待回复的任务、取消或暂停执行；AgentLoop 完成或失败时要通知调度器释放并发额度。如果这些交互都走直接方法调用，调度器、任务管理器与执行循环会互相持有对方引用，形成紧耦合，任何一方的内部改动都会波及整条链路。

命令总线（`bus/`）解决的就是这个解耦问题。它定义一套统一的消息协议（命令与事件），让生产者只面向总线发送消息，消费者只面向总线订阅消息，双方互不知晓对方存在。命令按 `task_id` 精准投递到目标 AgentLoop，事件广播给所有监听者。总线是纯内部通信管道，不负责任务状态管理（由 `TaskRecord` 状态机负责）、不负责持久化（DB 是唯一共享状态）、不向外部暴露接口。

> 架构变更：v0.1.0 曾尝试把任务迁移到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配回退到进程内方案。当前实现全部在 Runner 进程内，`LoopbackTransport` 是唯一传输实现，跨进程传输不再规划。

---

## 设计方案

### 消息模型：命令与事件

总线传输两类消息，定义在 `bus/messages.py`：

- **命令（TaskCommand）**：从调度器/任务管理器发往运行中任务的控制指令。`CommandKind`（bus/messages.py:24-39）定义 5 种：`INJECT_INSTRUCTION`（注入用户指令）、`RESUME_REPLY`（用户回复唤醒）、`CANCEL`、`PAUSE`、`RESUME`。
- **事件（TaskEvent）**：从 AgentLoop 发往调度器的生命周期广播。`EventKind`（bus/messages.py:47-60）定义 4 种：`WAITING_INPUT`、`COMPLETED`、`FAILED`、`CANCELLED`。

两类消息都是纯 JSON dataclass（`@dataclass(slots=True)`），字段为 `task_id` / `kind` / `payload` / `ts`，提供 `to_dict()` / `from_dict()` 序列化。消息中不携带任何运行时对象（队列、Event、AgentLoop 引用），这是总线可序列化的前提。帧级解码由 `decode_frame`（bus/messages.py:193-208）按 `"type"` 区分符分派到命令或事件类。

### 传输抽象：Transport 协议 + LoopbackTransport

`Transport`（bus/transport.py:22-45）是异步传输抽象协议，只约束三个方法：`send(frame)` 推送字节帧、`receive()` 拉取下一帧、`close()` 关闭通道。帧边界约定为一次 send 对应一次 receive，实现层负责边界。

当前唯一实现是 `LoopbackTransport`（bus/transport.py:53-92），基于 `asyncio.Queue` 的进程内回环：send 推入队列，receive 从队列拉取，close 推入 `None` 哨兵解除阻塞的接收方。总线与传输解耦的意义在于：上层 `TaskCommandBus` 只依赖 `Transport` 协议，不关心底层是进程内队列还是别的通道。

### 路由与分发：TaskCommandBus

`TaskCommandBus`（bus/command_bus.py:37-192）是总线核心，维护 `task_id → list[handler]` 路由表，提供两条分发路径：

- **命令发送 `send(cmd)`**（bus/command_bus.py:66-97）：两步投递。先把命令序列化为 JSON 帧推入 Transport，再按 `cmd.task_id` 查路由表，同步调用该任务的全部订阅处理器。单处理器异常 log-and-continue，不中断后续订阅者。这个容错是刻意的：RESUME 分支含 store 写入，超时路径的 `bus.send(CANCEL)` 无守卫，若此处重新抛出，检查循环与事件监听会被永久终止，并发额度随之泄漏。
- **事件发布 `publish(event)`**（bus/command_bus.py:101-114）：fire-and-forget，只序列化推入 Transport，不走路由表。事件监听者在 Transport 接收侧订阅。

订阅接口：`subscribe(task_id, handler)` 注册命令处理器，`unsubscribe(task_id)` 移除某任务的全部处理器，`has_subscribers(task_id)` 查询。事件侧由 `listen_events(handler)`（bus/command_bus.py:144-192）提供阻塞循环：持续从 Transport 读帧，解码后把 `TaskEvent` 分发给 handler，非事件帧静默忽略；单事件处理异常同样 log-and-continue，保证监听循环不因单个坏事件退出（否则 COMPLETED/FAILED 事件不再释放并发额度，后续任务全部排队）。

### 订阅生命周期

命令订阅跟随 AgentLoop 的执行生命周期，注册与清理都在循环内部完成：

- **主订阅**：AgentLoop 在 `run()` 入口 `bus.subscribe(task.id, self._on_bus_command)`，在 `finally` 块 `bus.unsubscribe(task.id)` 移除该任务的全部处理器，保证任务结束后路由表不残留。
- **ask_user 临时订阅**：挂起期间额外注册 `_on_resume_reply` 处理器（executor/agent_loop.py:200），收到 `RESUME_REPLY` 即 set resume_event；恢复后随主订阅一并清理。

事件监听则常驻：调度器在 `start()` 中 `asyncio.create_task(listen_events(self._on_task_event))`（core/scheduler.py:95-97），`stop()` 时取消该任务。

### 组装与数据流

总线在 `lifecycle.py` 的 `load_plugin` 中组装（lifecycle.py:65-66）：先建 `LoopbackTransport`，再以它为参数构造 `TaskCommandBus`。总线实例随后注入 `TaskScheduler`（lifecycle.py:81-86）与 `TaskManager`（lifecycle.py:96-108），并经 ExecutorFactory 传递到每个 AgentLoop。

```
调度器 / TaskControl                 TaskCommandBus                  AgentLoop
     │  send(TaskCommand)                 │                              │
     ├───────────────────────────────────►│  1. JSON 帧 → Transport      │
     │                                    ├─────────────────────────────►│  _on_bus_command()
     │                                    │  2. 路由表本地分发           │
     │                                    ├─────────────────────────────►│
     │                                    │                              │
     │  publish(TaskEvent)                │                              │
     │◄───────────────────────────────────┤◄─────────────────────────────┤  _notify_completed()
     │  listen_events 分发                │                              │  _handle_ask_user()
```

生产与消费关系：

| 角色 | 组件 | 消息 |
|---|---|---|
| 命令生产者 | `TaskScheduler.cancel/pause/resume`（core/scheduler.py:222-328） | CANCEL / PAUSE / RESUME |
| 命令生产者 | `TaskControl.handle_injection`（core/usecases/task_control.py:126-134） | INJECT_INSTRUCTION |
| 命令生产者 | `TaskControl.handle_user_reply`（core/usecases/task_control.py:86-124） | RESUME_REPLY |
| 命令消费者 | `AgentLoop._on_bus_command`（executor/agent_loop.py:258-299） | 全部 5 种命令 |
| 事件生产者 | `AgentLoop`（ask_user 挂起、完成、失败、取消） | WAITING_INPUT / COMPLETED / FAILED / CANCELLED |
| 事件消费者 | `TaskScheduler._on_task_event`（core/scheduler.py:377-392） | COMPLETED / FAILED / CANCELLED |

命令消费端 `AgentLoop._on_bus_command`（executor/agent_loop.py:258-299）按 kind 分支：`INJECT_INSTRUCTION` 把指令追加到 `metadata["_inject_queue"]`，下一轮循环消费；`RESUME_REPLY` 写入 `_user_reply` 并 set 对应的 resume_event；`CANCEL` 置 `_cancelled` 标记并唤醒等待；`PAUSE` 置 `_paused` 并写 `_coop_paused` 标记；`RESUME` 清标记恢复。

协作取消是总线价值的典型体现：调度器超时检测（core/scheduler.py:558-560）或用户取消（core/scheduler.py:259-261）时 `bus.send(CANCEL)`，AgentLoop 收到后置取消标记，在下一轮循环或等待点协作退出，而不是被外部强杀。

---

## 使用与配置

命令总线是纯内部机制，无独立配置项，也不需要用户操作。开发者接触它的场景有两类：

- **注入指令**：`/task ask` 命令与 Planner 的 `task_modify` 工具最终都经 `TaskManager.handle_injection`（core/task_manager.py:389）→ `TaskControl.handle_injection` 发送 `INJECT_INSTRUCTION`。
- **唤醒任务**：用户在聊天流回复后，`chat.receive.after_process` Hook 触发 `TaskManager.handle_user_reply`（core/task_manager.py:374）→ `TaskControl.handle_user_reply` 匹配 WAITING_INPUT 任务并发送 `RESUME_REPLY`。

### 已知限制

1. **仅进程内传输**：`LoopbackTransport` 是唯一实现，命令与事件只在 Runner 进程内传递，无外部传输实现。
2. **命令不可确认**：`send()` 恒返回 `True`，无确认/超时/重试。若目标 AgentLoop 尚未注册处理器，命令经 Transport 推送但本地分发静默失败，调用方无法感知。
3. **事件无持久化**：事件瞬时推送，无缓冲。若监听循环未启动或异常退出，发布方无法感知事件丢失。
4. **消费者注册非显式**：AgentLoop 在 `run()` 中订阅、ask_user 期间临时订阅、调度器在 `start()` 中启动事件监听，无集中注册机制。

### 相关文档

- [任务模型](./01-task-model.md)：任务状态机与两级任务模型
- [调度器](./02-scheduler.md)：CANCEL / PAUSE / RESUME 命令的生产方
- [AI 提问](./07-ask-user.md)：WAITING_INPUT 事件与 RESUME_REPLY 唤醒链路
- [持久化与恢复](./03-persistence-recovery.md)：WAITING_INPUT 任务重启后保持，回复仍可唤醒