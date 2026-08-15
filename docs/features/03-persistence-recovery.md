# 持久化与恢复

sqlite 持久化存储与插件重启后的活跃任务恢复。所有任务记录、执行历史、状态变更审计日志统一经 `TaskStore` 落盘到 `data_dir/tasks.db`；插件重启时由 `recover_active_tasks`（lifecycle.py:244-281）结合 `TaskRecovery` 决策器（domain/recovery.py）为每条活跃任务定恢复动作。

## 设计目标（为什么需要持久化？）

持久化要解决两个问题：任务历史必须落盘，进程重启后任务不能丢。

离线任务不是一次性的内存对象。agent 级任务最长跑 30 轮，期间经历 LLM 推理、工具调用、ask_user 挂起、指令注入，跨多个执行阶段。这些上下文如果不落盘，进程一旦重启，任务的状态和对话全部归零。更实际的是，插件会因更新、崩溃、手动卸载而重启，重启后必须能区分哪些任务还活着（未到终态），并接续处理：定时任务重新排队、运行中任务降级重排、等待输入的任务保持挂起等回复。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁移到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配，回退到进程内 contextvars + usecase 分层方案。子进程隔离意味着跨进程传对象、序列化全部运行时状态，代价远超收益；回退后持久化层只负责"可序列化的任务记录"，运行时对象留在进程内。

核心设计约束随之而来：**只有纯数据可以落盘，运行时对象不能**。TaskRecord 因此成为唯一可落库形态。

## 设计方案

### TaskRecord：唯一可落库形态

`TaskRecord`（domain/task_record.py:140-354）是收纳全部任务字段的 dataclass，每个字段均可 JSON 序列化：标识、分级、归属、触发信息、时间戳、优先级、metadata 扩展字段，以及内嵌的状态审计日志。`to_dict()`（task_record.py:294-319）/ `from_dict()`（task_record.py:320-354）在持久化字典与运行时对象之间互转；`from_dict` 经 `_restore()`（task_record.py:239-250）直接覆盖状态，不触发校验、不产生新审计条目。

运行时对象（asyncio.Event、注入队列、AgentLoop 引用）不落在 TaskRecord 上，而是存于各模块的实例属性。典型例子是 AgentLoop 的 `_resume_events`（task_id → Event 映射，executor/agent_loop.py:108）：Event 不可 JSON 序列化，若写进 metadata 会在落盘时报错，因此放在循环实例的属性里。早期版本曾把这类运行时状态集中在一个独立对象中管理，v0.1.0 迁移时已删除；现行约定是"对象属于哪个模块，就存在哪个模块的实例属性，一律不写进 metadata"。

状态字段也不是裸属性，直接赋值不会经过任何保护。变更必须走两条方法：

- `transition(new_status, actor)`（task_record.py:209-234）：受状态机约束（`_ALLOWED_TRANSITIONS`，task_record.py:64-76），非法转换抛 `TaskStatusError`。这是常规路径。
- `force(new_status, actor, reason)`（task_record.py:236-258）：跳过校验的兜底逃逸口，用于终态回退、重启恢复、异常兜底，同样追加审计记录。

两者都会自动向 `_status_log` 追加一条 `StatusChange`（task_record.py:126-153），记录时间戳、新状态、触发方（actor）和原因，随 `to_dict()` 序列化进 `tasks.data` 列。`/task history` 命令展示的完整状态流转，就是这条审计日志。

### TaskStore：sqlite 存储层

`TaskStore`（domain/task_store.py:29）管理两张表：`tasks` 每任务一行，`data` 列存完整 JSON，另冗余 6 个筛选索引列（status / owner / stream_id / level / trigger_type / created_at）；`task_history` 存有序历史条目，自增主键同时充当恢复游标。`init()`（task_store.py:47-110）建两张表加 8 个索引，并启用 WAL 日志模式。

存储层围绕并发安全做了三个决策：

- **WAL 模式**（task_store.py:54）提升读写并发。
- **每方法独立连接**：每次调用在独立 `sqlite3.connect` / `closing` 上下文中完成，经 `asyncio.to_thread` 到后台线程执行（task_store.py:151-190），不阻塞事件循环，也避免连接跨线程复用。
- **乐观锁 CAS 守卫**：`save(task, *, expected_status=None)`（task_store.py:118-193）。expected_status 非 None 时执行 `UPDATE ... WHERE id=? AND status=?`，rowcount=0 返回 False。这关闭了「读取→写入」之间的 TOCTOU 窗口：调度器超时把任务置为 FAILED 之后，Agent 循环的旧快照保存会被原子拒绝，不会覆盖并发写下的终态。未开启守卫时为全量 upsert。

守卫模式被多个写点使用：调度器超时落盘（core/scheduler.py:543-554，expected_status=RUNNING）、cron 任务重排（core/scheduler.py:371，expected_status=COMPLETED）、AgentLoop 每轮保存（agent_loop.py:458-465）。

查询侧接口：`get` / `get_by_prefix`（task_store.py:215-242，按 ID 前缀匹配）、`get_by_title`（task_store.py:244-276，按标题精确匹配，供 resolve_task 的标题兜底）、`list`（task_store.py:278-332，多条件组合筛选）、`list_active`（task_store.py:334-352，返回全部非终态任务，是恢复流程的入口）、`delete`、`count`。

### 状态变更的落盘时机

每次状态变更都立即落盘，保证极端情况下最多丢失"变更后未写入"的一瞬。关键写点：

- 任务创建：`TaskCrud.create_task` 完成级别落定（未指定默认 agent）与标题生成后落库，再入队。
- 状态转换：调度器每次 `transition()` / `force()` 后立即 `store.save(task)`。
- Agent 循环：进入 RUNNING（agent_loop.py:373）、每轮结束（agent_loop.py:458-465）、ask_user 挂起（agent_loop.py:186）与恢复（agent_loop.py:220）。
- 卸载兜底：`scheduler.stop()`（core/scheduler.py:100-147）把所有 RUNNING 任务 `force(PAUSED)` 落盘（scheduler.py:138-140）；`on_unload` 随后停 MCP、`store.close()`（plugin.py:50-66）。正常卸载不会留下 RUNNING 孤儿任务。

### 恢复流程 recover_active_tasks

加载序列（lifecycle.py:43-153）中，恢复发生在调度器启动与 MCP 初始化**之后**：`load_plugin` 第 8 步调用 `recover_active_tasks(plugin, logger)`（lifecycle.py 的 `load_plugin`）。MCP 先注册保证恢复出的 agent 任务首轮即可看到 MCP 工具；随后恢复的任务能立即进入调度队列。

`recover_active_tasks`（lifecycle.py 的 `recover_active_tasks`）先 `store.list_active()` 取全部非终态任务，逐条交给无状态的 `TaskRecovery.recover()`（domain/recovery.py）决策，再按动作执行：

| 原状态 | 恢复动作 | 调用方行为 |
|---|---|---|
| SCHEDULED | ENQUEUE | 直接 `scheduler.enqueue(task)`，等待定时器到点触发（重新入队幂等，不再产生非法状态转换错误日志） |
| PENDING | ENQUEUE | 崩溃/停机瞬间已落库但尚未派发的任务——调度器 pending 队列是纯内存的，重启必须回读 DB 中的 PENDING 行，否则成为永久孤儿（enqueue 已返回 bool，入队失败不再静默） |
| RUNNING | PENDING | 决策器已 `force(PENDING, actor="recovery", reason="recovered_from_running")` 并经 `mark_recovered_from_running()` 打标记（recovery.py 的 `TaskRecovery.recover`，键 `META_RECOVERED_FROM_RUNNING`）；调用方落盘后重新入队（lifecycle.py 的 `recover_active_tasks`） |
| WAITING_INPUT | WAITING | 保持状态；旧进程的 resume Event 已随进程消失，等 `chat.receive.after_process` Hook 收到用户回复后，经命令总线 RESUME_REPLY 唤醒 |
| PAUSED（paused_by_stop） | PENDING | 优雅停机（`scheduler.stop`）时被 `force(PAUSED)` 的任务带 `META_PAUSED_BY_STOP` 标记：与崩溃遗留的 RUNNING 对称，自动降级 PENDING 重新入队，标记随即清除（用户主动暂停无此标记，不受影响） |
| PAUSED（用户主动） | PAUSED | 不做任何操作，须手动恢复 |

RUNNING 降级走 `force` 而非 `transition`，因为 RUNNING→PENDING 不在状态机允许表里，重启恢复属于必须绕过校验的兜底场景。`was_recovered_from_running()` 标记用来区分"恢复重排"与"正常排队"。

### 历史回放

`task_history` 表是任务上下文的持久化形态。AgentLoop 每轮把消息写入历史：第 1 轮存完整 `messages` 列表作回放种子，后续轮只存 `new_messages` 增量（agent_loop.py:452-455）；`append_history` 返回自增 id，经 `set_last_history_id()` 作为持久化水位记入（键 `META_LAST_HISTORY_ID`，agent_loop.py:456-457）。

重启恢复后的新 AgentLoop 在构建上下文时从头幂等回放：`get_history_after(task.id, 0)`（agent_loop.py:390-398）按 id 升序取出全部条目，injection 条目重建为 system 消息，`messages` 条目整条替换，`new_messages` 条目增量追加。以 0 为起点意味着每次恢复都全量重建，天然幂等；`last_history_id()` 仅作审计与未来增量续传的锚点。`MAX_HISTORY_KEEP = 50`（agent_loop.py:41）声明了每任务历史条目的预期上限，但当前没有裁剪逻辑消费该常量。

## 使用与配置

### persist_history

`[task]` 节 `persist_history`（config.py:133-140），默认 `True`，意图是控制是否把 Agent 执行历史写入 `task_history` 表。注意：**当前实现未读取该配置项**，任务历史始终持久化（见已知限制）。

### 数据库文件位置

数据库路径在 `load_plugin` 第 1 步确定：`TaskStore(data_dir / "tasks.db")`（lifecycle.py:51-52）。`data_dir` 由 MaiBot Plugin SDK 注入，指向插件的专属数据目录，不属于 Pydantic 配置模型，因此不在 config.toml / WebUI 中可见。`init()` 首次运行自动建表并启用 WAL。

### 对用户可见的行为

- `/task history <ID>` 展示任务的状态流转审计与执行历史条目。
- 重启后：SCHEDULED / PENDING 任务重新排队，定时任务到点照常触发；RUNNING 任务降级重排后重新执行，Agent 上下文经历史回放重建；优雅停机时被暂停的任务自动恢复；WAITING_INPUT 任务保持挂起，收到用户回复后继续。

### 已知限制

1. **无 schema 版本管理**。`init()` 用 `CREATE TABLE IF NOT EXISTS`（task_store.py:56-67），不检测表结构变更、无版本标记、无自动迁移逻辑。未来 `TaskRecord` 字段增减或序列化格式变更时，旧库记录可能反序列化失败或产生默认值丢失。
2. **persist_history 已声明未执行**。config.py:133-140 的字段描述明示"当前实现未读取该配置项，任务历史始终持久化"，修改配置不会改变落盘行为。
3. **WAITING_INPUT 恢复后无超时**。`resume_event.wait()`（agent_loop.py:213）没有超时参数；`default_timeout_min`（config.py:124-131）在配置模型中声明但未接入等待逻辑。恢复后的挂起任务若用户始终不回复，将无限期停留 WAITING_INPUT。
4. **大任务数据膨胀**。`tasks.data` 列存完整 JSON，内嵌的 `_status_log` 随状态变更无限增长，每次 `save()` 都重写整个 data 列，cron 反复调度加频繁状态变更的长时任务尤其明显。
5. **MAX_HISTORY_KEEP 未消费**。agent_loop.py:41 声明 50 条上限，但代码没有裁剪历史条目的逻辑，历史表持续累积。

### 相关文档

- [任务模型](./01-task-model.md) — TaskRecord 数据模型与 8 态状态机
- [调度器](./02-scheduler.md) — 入队落盘、超时守卫保存、卸载 PAUSED
- [命令总线](./11-command-bus.md) — RESUME_REPLY / CANCEL 等命令与终态事件
- [生命周期总览](../LIFECYCLE.md) — 插件 on_load / on_unload 全流程
- [配置体系](./14-config.md) — persist_history 等配置项说明
