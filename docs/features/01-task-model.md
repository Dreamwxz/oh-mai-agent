# 任务模型

本文档讲述 oh-mai-agent 的任务数据模型与状态机：任务以什么形态存在，状态如何流转，又由谁保证流转合法。持久化数据统一由 `domain/task_record.py` 管理。

## 设计目标（为什么需要两级任务模型？）

MaiBot 的 Host 主流程负责实时聊天，不适合承载需要长时间推理、多轮工具调用、甚至跨流回复的复杂任务。oh-mai-agent 要把这类任务从实时链路中剥离出来，放进独立的 Runner 进程离线执行。但"离线任务"内部差异很大：有的是一次即时动作（发一条消息、查一次信息），有的是需要自主决策的长时循环（LLM 推理 + 工具调用 + 回复润色）。把两者混在一个执行模型里，要么让简单任务背上循环的复杂度，要么让复杂任务没有足够的执行空间。

所以任务分两级。`instant`（即时动作，单次执行后完成）和 `agent`（离线长时循环，最多 30 轮 LLM 交互）。分级之后，执行器也按级分发：instant 走进程内同步执行器，agent 走独立的 AgentLoop。两级任务共享同一套数据模型和状态机，区别只在执行路径。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁移到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配，回退到进程内 contextvars + usecase 分层方案。当前实现全部在 Runner 进程内，执行上下文经 `executor/context.py` 的 `current_task` ContextVar 传递，业务逻辑下沉到 `core/usecases/`（TaskControl / TaskCrud）。

## 设计方案

### 两级分类：TaskLevel 枚举

`TaskLevel` 枚举（`domain/task_record.py:26-29`）定义两级任务：`instant` 和 `agent`。级别在任务创建时落定——未显式指定时默认 agent，instant 仅由定时任务与 Agent 内部显式创建（见下文创建流程），执行时由 `ExecutorFactory` 按级分发到不同执行器。

### 数据模型：TaskRecord

`TaskRecord`（`domain/task_record.py:161-375`）是任务的唯一领域模型，一个全字段 JSON 可序列化的 dataclass。它收纳任务的全部可持久化字段：标识（id / title / intent）、分级与状态（level / status）、归属（owner / stream_id / platform / reply_stream_id）、触发方式（trigger_type / delay_seconds / cron_expr / scheduled_at）、时间戳与优先级、运行时约束（max_runtime_min）、扩展（metadata）以及审计日志（_status_log）。

两个设计要点：

1. **不含任何运行时对象**。队列、asyncio.Event、AgentLoop 引用等运行时状态绝不写入 TaskRecord，也不落库。运行时对象由执行器持有（如 AgentLoop 的 `_resume_events` 存实例属性），TaskRecord 只负责可持久化的那一面。旧代码中的 `Task` 别名已删除，全仓库统一使用 `TaskRecord`。
2. **回复目标有回退**。`reply_target` 属性（`domain/task_record.py:308-310`）返回 `reply_stream_id`，未设置时回退到 `stream_id`。这支持跨流回复：agent 任务可以把回复发到与创建流不同的目标流。

序列化由 `to_dict()`（`domain/task_record.py:315-340`）与 `from_dict()`（`domain/task_record.py:341-375`）双向完成，`_status_log` 审计日志随序列化一起落盘、一起回填。

### 8 态状态机

`TaskStatus` 枚举（`domain/task_record.py:31-41`）定义 8 个状态，分活跃态与终态两组：

- 活跃态：`scheduled`（已排期，等待 delay/cron 到期）、`pending`（排队中，等待并发额度）、`running`（执行中）、`waiting_input`（等待用户回复，ask_user 触发）、`paused`（已暂停）
- 终态：`completed`（已完成）、`failed`（失败）、`cancelled`（已取消）

合法转换由 `_ALLOWED_TRANSITIONS` 转换矩阵（`domain/task_record.py:64-76`）约束，终态集合定义在 `_TERMINAL_STATUSES`（`domain/task_record.py:79-81`）。核心流转路径：

```
scheduled ──到期──▶ pending ──额度空──▶ running ──ask_user──▶ waiting_input
                ▲                        │  │  │                 │
                └────cron 重调度─────────┘  │  └──用户回复──▶ running
                                            │
                              completed / failed / cancelled（终态）
```

cron 任务完成后经 `force()` 重置回 `scheduled` 等待下次触发（`core/scheduler.py:360` `_reschedule_cron`），形成循环；其余任务进入终态后不再流转。

### transition / force：状态变更的两条通道

状态变更必须走 `task.transition(new_status)`（`domain/task_record.py:209-234`）或 `task.force(new_status)`（`domain/task_record.py:236-258`），直接赋值会抛 `TaskStatusError`。两条通道都会追加 `StatusChange` 审计条目并刷新 `updated_at`，区别在是否校验：

- `transition()` 受状态机约束，非法转换抛 `TaskStatusError`，由调用方捕获处理。这是正常业务路径。
- `force()` 跳过校验，是唯一的兜底逃逸口。终态回退、重启恢复、异常兜底、cron 重调度等场景必须走它，因为那些转换在状态机上不合法（例如终态回退到活跃态），但业务上需要。

反序列化走 `_restore()`（`domain/task_record.py:260-271`），直接覆盖 status 与 _status_log，不触发校验、不产生审计，仅供 `from_dict()` 调用。`is_terminal()`（`domain/task_record.py:273-275`）按 `_TERMINAL_STATUSES` 判定是否终态。

配套的状态查询方法：`runtime_seconds()`（`domain/task_record.py:281-289`）返回 RUNNING 任务的已运行秒数；`status_info()`（`domain/task_record.py:291-306`）返回结构化 `(状态, 关联时间戳)`，RUNNING 关联 started_at、WAITING_INPUT 关联 updated_at、SCHEDULED 关联 scheduled_at，供状态格式化与看板使用。

### TaskStore CAS 守卫：防并发覆盖

状态机约束的是单任务内的合法流转，但持久化层还要防并发覆盖。`TaskStore.save()`（`domain/task_record.py` 的配套 `domain/task_store.py:118-193`）带 `expected_status` 乐观锁守卫：传入期望状态时执行 `UPDATE ... WHERE id=? AND status=expected_status`，rowcount 为 0 说明记录已被并发修改（如超时 FAILED 或取消），返回 False 拒绝写入。这关闭了"读取到写入"之间的 TOCTOU 窗口，防止旧快照覆盖并发终态。未传 `expected_status` 时是全量 upsert。

### 创建任务的数据流

任务创建入口统一收敛到 `TaskManager` 门面（`core/task_manager.py:103` 实例化 TaskCrud、`:124` 实例化 TaskControl），实际逻辑在 `TaskCrud.create_task()`（`core/usecases/task_crud.py:51-99`）：

1. guest 拦截：`PermissionResolver.require(caller_role, USER)`，guest 无权创建任务
2. 级别落定：未显式指定 `level` 时默认 agent（INSTANT 仅由定时任务与 Agent 内部显式创建，用于消息投递）
3. LLM 标题：生成任务标题，失败降级为 intent 前 40 字符
4. 落库：构造 TaskRecord 写入 TaskStore
5. 入队：`scheduler.enqueue()` 按触发方式调度（NOW 直接排队，DELAY/CRON 进入 scheduled）

### 重启恢复

插件重启后，`TaskRecovery.recover()`（`domain/recovery.py:42-70`）按持久化状态决定恢复动作：SCHEDULED 重新入队等待定时器触发；RUNNING 经 `force()` 降级为 PENDING 重新排队并写入 `metadata["_recovered_from_running"]`；WAITING_INPUT 保持状态，旧 asyncio.Event 已随进程消失，等待用户通过 Hook 重新唤醒；PAUSED 与终态不做任何操作。

## 使用与配置

### 创建任务的入口

任务创建有三个入口，全部经 TaskManager 门面：

- **`/task create <意图>` 命令**：面向聊天用户，user 及以上角色可用
- **Planner `task_create` 工具**：主 Planner 在对话中按需创建任务，支持定时/延迟参数
- **跨插件 API**：其他 MaiBot 插件经 `api_expose` 层创建任务

任务模型相关的配置键位于 `config.py:100-140` 的 `TaskConfig` 节：`max_concurrent_tasks`（并发上限，默认 4）、`max_runtime_min`（agent 任务总时长兜底，0 = 不限，超时由调度器 `_check_loop()` 强制转 FAILED，`core/scheduler.py:464`）、`default_timeout_min`（ask_user 挂起等待时间，见已知限制）、`persist_history`（是否持久化完整任务历史）。

### 已知限制

1. **schema 版本迁移缺失**。`TaskRecord` 无版本号字段，数据模型升级时无法自动迁移已落盘数据。如果未来新增或重命名字段，旧记录可能出现字段缺失。

2. **default_timeout_min 未被消费**。配置键 `default_timeout_min`（`config.py:124`）已声明默认 10 分钟，但 AgentLoop 的 `resume_event.wait()`（`executor/agent_loop.py:213`）无超时参数，调度器 `_check_loop()` 也不检查 WAITING_INPUT 状态的停留时长。ask_user 挂起会无限等待，不会因超时自动取消。

3. **task_delete / task_cancel 命名不一致**。对外暴露给 Planner 的工具名为 `task_delete`（`plugin.py:298`），命令名为 `task_cancel`（`plugin.py:447`），两者指向同一取消能力但名称不统一，可能在文档和 LLM 调用中造成混淆。

### 关联文档

任务模型是生命周期体系的基础层，与以下功能文档紧密相关：[调度器](02-scheduler.md)（入队与超时检查）、[持久化与恢复](03-persistence-recovery.md)（sqlite 落盘与重启恢复）、[权限模型](04-permission.md)（guest 拦截与角色判定）、[命令总线](11-command-bus.md)（状态变更经总线路由到执行器）。