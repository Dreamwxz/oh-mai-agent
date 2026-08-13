# 子进程架构扩展点（预留设计）

> 本文档记录曾经规划的子进程架构边界。当前实现仍是 Runner 进程内的异步任务，
> 子进程 worker、stdio 传输和进程生命周期管理均未实现。本文仅作为历史设计记录，
> 不代表当前可用功能。

## 当前实现

- `bus/transport.py` 仅提供 `Transport` 抽象和 `LoopbackTransport` 进程内实现。
- `TaskCommandBus` 通过 JSON 可序列化的 `TaskCommand` / `TaskEvent` 路由命令和事件。
- `AgentLoop`、队列、事件等运行时对象只存在于 Runner 进程内，不落盘、不跨进程传输。
- 当前不存在 `bus/stdio_transport.py`、worker 入口或 supervisor。

## 预留边界

若未来实现跨进程执行，传输层应继续实现 `Transport` 抽象，命令总线和 AgentLoop 的
业务逻辑应保持独立。跨进程协议可以采用带明确帧边界的 JSON 消息，但具体传输实现、
worker 生命周期和故障恢复策略尚未确定。

父进程与 worker 之间需要至少支持以下消息方向：

- 父进程向 worker 发送 `TaskCommand`，包括注入、回复、取消、暂停和超时指令。
- worker 向父进程发送 `TaskEvent`，包括启动、等待输入、恢复、完成和失败事件。

## 边界约束

1. 跨进程消息只能包含 JSON 可序列化数据，不得携带队列、事件或事件循环引用。
2. 持久化状态继续由 `TaskRecord` 和 `TaskStore` 负责，运行时对象不得写入任务元数据。
3. 子进程设计落地前，不应在生产代码或文档中引用不存在的传输类、worker 文件或启动入口。

## 状态

本设计为 **reserved design, not implemented**。当前生产路径是：

```text
TaskScheduler -> TaskManager -> ExecutorFactory -> AgentLoop
                                      |
                                      v
                              TaskCommandBus
                                      |
                              LoopbackTransport
```
