# 调度器

`core/scheduler.py` 的 `TaskScheduler` 是任务状态机的核心驱动引擎：并发调度、定时触发、超时兜底、取消/暂停/恢复，全部在这里收口。

---

## 设计目标

MaiBot 的离线任务不是「创建即执行」这么简单。任务创建后可能排队等待、可能延迟到点才跑、可能按 cron 周期循环，而同时运行的 agent 任务会长时间占用 LLM 与工具资源，必须限制并发。调度器要回答三个问题：

1. **并发怎么控**：多个任务同时就绪时，谁先跑？同时最多跑几个？
2. **定时怎么触发**：DELAY 和 CRON 任务到点后，由谁把它们唤醒？
3. **失控怎么兜底**：一个 agent 任务卡死或跑太久，谁来强制终止？

调度器把这三件事统一到一个每秒轮询的后台循环里，配合一个按 priority 降序的 pending 队列和一个 running 集合，形成「排队 → 启动 → 完成/超时」的闭环。它不关心任务内部怎么执行（那是 ExecutorFactory 和 AgentLoop 的事），只负责「什么时候该跑、能不能跑、跑挂了怎么办」。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁移到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配，回退到进程内 contextvars + usecase 分层方案。当前调度器与执行器全部在 Runner 进程内，调度器通过 executor 回调直接派发，不跨进程。

---

## 设计方案

### 位置：TaskManager 与执行器之间的调度中枢

调度器由 `lifecycle.py:81-86` 构造，依赖四个东西：`TaskConfig`（并发/超时参数）、`TaskStore`（持久化）、executor 回调（实际执行入口）、`TaskCommandBus`（协作控制通道）。

executor 回调**后绑定**（`core/scheduler.py` 的 `set_executor`）：调度器先于 TaskManager 构造（`executor` 参数可选），TaskManager 就绪后 `lifecycle.py` 调用 `scheduler.set_executor(task_manager.execute_task)` 注入——`TaskManager.execute_task` 按任务等级分发到 `execute_instant` 或 AgentLoop。`TaskScheduler.start()` 在 `lifecycle.py` 调用时执行回调已绑定，派发的任务可以安全执行。

### 入队：三条路径，一个队列

`enqueue()`（`scheduler.py:151`）按触发类型分三条路径：

| 触发类型 | 行为 | 状态 |
|---|---|---|
| **NOW** | 立即入 pending 队列（priority 降序），随即 `_try_dispatch()` 尝试启动 | PENDING |
| **DELAY** | `scheduled_at = now + delay_seconds`，落盘等待 | SCHEDULED |
| **CRON** | `croniter(expr, now).get_next(datetime)` 计算下次触发，落盘等待 | SCHEDULED |

CRON 表达式非法时（`croniter` 抛 `ValueError`），任务直接标记 FAILED 落盘，不入队列（`scheduler.py:191-195`）。DELAY 和 CRON 任务不立即执行，由后台检查循环到点后转入 PENDING。

enqueue 对已处于 SCHEDULED 的任务幂等：跳过重复 transition（避免非法状态转换异常），仅刷新 `scheduled_at` 后落盘——这保证了重启恢复流程（对已落盘的 SCHEDULED 任务重新入队）无副作用；pending 队列按 id 去重，重复入队被忽略。

`enqueue` 的调用方是 `TaskCrud.create_task()`（`task_crud.py:51-99`）：权限校验 → 级别落定（未显式指定时默认 agent，INSTANT 仅由定时任务与 Agent 内部显式创建）→ 标题生成 → 落库 → `scheduler.enqueue()`（`task_crud.py:94`）。`TaskManager.create_task` 只是门面转发，真正的创建逻辑在 usecase 层。

### 后台检查循环：每秒一次的心跳

`_check_loop()`（`scheduler.py:464`）是调度器的心脏，每秒轮询一次，做三件事：

1. **SCHEDULED → PENDING**：从 `store.list_active()` 取所有非终态任务，把 `scheduled_at <= now` 的 SCHEDULED 任务转入 PENDING 并入队。
2. **超时检测**：`max_runtime_min > 0` 时遍历 running 集合，`(now - started_at) / 60 > 阈值` 的任务强制 FAILED 并通知执行器协作停止。
3. **触发分发**：调用 `_try_dispatch()` 从 pending 队列取任务启动。

### 并发控制：配额预留 + priority 降序

`_try_dispatch()`（`scheduler.py:445`）从 pending 队列按 priority 降序取任务，逐个交给 `_try_start()`（`scheduler.py:394`）。`_try_start` 先检查 `len(_running) >= max_concurrent_tasks`，额度不足则任务留在队列。

这里有一个并发正确性细节：`_try_dispatch` 可能被检查循环、事件监听、enqueue、resume 并发调用，若等 `save` 完成后再登记 running，两个并发派发可同时通过额度检查，导致实际并发数超过上限。因此 `_try_start` 在**第一个 await 之前**同步 `_running.add(task.id)`（`scheduler.py:415`）预留额度；若随后的 `save` 失败，则 `force(PENDING)` 回滚内存状态并返回 False（`scheduler.py:424`），否则任务会因 running→running 非法转换永久卡死队首，阻塞其后全部 pending 任务。

### 超时兜底：守卫保存 + 协作停止

超时路径（`scheduler.py:543-560`）是「标记 + 通知」两步：

1. `transition(FAILED)` 后，用 `save(t, expected_status=RUNNING)` 守卫落盘。若 get 之后、save 之前循环已把任务持久化为终态（COMPLETED/CANCELLED），本次 FAILED 写入被原子拒绝，避免超时降级覆盖并发完成的终态记录。
2. 无论保存是否成功，都从 running 集合移除，并 `bus.send(CANCEL)` 通知执行器协作停止（AgentLoop 收到后自行收尾）。

`is_coop_paused()` 标记的任务跳过超时检查（`scheduler.py:526-527`）：暂停中的任务不计时，避免「暂停期间被超时误杀」。

### 取消 / 暂停 / 恢复：按状态分路径

`cancel()`（`scheduler.py:222`）按当前状态分三条路径：

- **SCHEDULED / PENDING / PAUSED**：直接 `transition(CANCELLED)` 落盘，并从 pending 队列移除。
- **INSTANT RUNNING**：`force(CANCELLED)` 落盘（instant 任务无协作循环，只能强制）。
- **AGENT RUNNING / WAITING_INPUT**：`bus.send(CANCEL)` 协作取消，由 AgentLoop 收到命令后自行终止。

已处于终态的任务不可取消，返回 False。

`pause()`（`scheduler.py:267`）只对 RUNNING 任务生效：经 `set_coop_paused(True)` 落盘（键 `META_COOP_PAUSED`），再 `bus.send(PAUSE)`。`resume()`（`scheduler.py:291`）分两种情况：RUNNING 且 `is_coop_paused()` 为真的，清标记并 `bus.send(RESUME)` 让循环继续；PAUSED 的，转入 PENDING 重新排队。

### Cron 循环重排：仅 COMPLETED 才循环

`on_task_completed()`（`scheduler.py:332`）从 running 集合移除任务释放并发额度，然后触发 `_try_dispatch()`。只有 **COMPLETED 的 CRON 任务**才走 `_reschedule_cron()`（`scheduler.py:360`）：`croniter` 计算下次触发时间，`force(SCHEDULED)` 重置终态，再用 `save(task, expected_status=COMPLETED)` 守卫落盘。守卫的意义在于：若事件与重排之间发生并发强制取消/删除，本次写入被原子拒绝，避免复活一个已被终态覆盖的 CRON 任务。FAILED / CANCELLED 的 CRON 任务不循环，防止死循环。

### 停止：RUNNING 降级 PAUSED

`stop()`（`scheduler.py:100`）取消检查循环和事件监听，然后把所有 RUNNING 任务 `force(PAUSED)` 落盘（经 `mark_paused_by_stop()` 打标记，键 `META_PAUSED_BY_STOP`），由恢复机制在下次启动时重新入队。

---

## 使用与配置

### 配置项

调度器配置定义在 `config.py:100` 的 `TaskConfig` 中：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `max_concurrent_tasks` | `4` | 并发任务上限；设为 0 时不允许任何任务启动 |
| `max_runtime_min` | `0` | agent 任务总运行时长兜底（分钟），`0` = 不限制；超时后强制 FAILED |
| `default_timeout_min` | `10` | ask_user 挂起等待时间（分钟）— **声明但未执行**，见已知限制 |
| `persist_history` | `True` | 是否持久化完整任务历史（由 TaskStore 负责，调度器不直接使用） |

配置支持热更新：`update_config()`（`scheduler.py:69`）可在线修改并发上限和超时参数。若当前 running 数超过新上限，不强制停止已有任务；新任务按新上限排队。超时检测在下一轮询周期生效。

### 与 TaskCrud 的交互

调度器不直接暴露给外部调用方。任务创建经 `TaskCrud.create_task()` 落库后调用 `enqueue()`；取消/暂停/恢复经 `TaskCrud.cancel/pause/resume_task()`（`task_crud.py:215-225`）转发到调度器对应方法。外部入口（命令、API、Planner 工具）一律经 TaskManager 门面，不直接接触调度器。

### 已知限制

1. **`default_timeout_min` 未由调度器执行**：该配置键声明在 `TaskConfig` 中，但调度器不使用。实际的 ask_user 挂起等待逻辑在 AgentLoop 中通过 `resume_event.wait()` 无超时阻塞实现，超时后任务保持挂起，后续回复仍可唤醒。
2. **Cron 任务断点丢失**：若插件在 `_check_loop` 刚将 SCHEDULED → PENDING → RUNNING 的过程中崩溃，任务可能丢失（RUNNING 降级为 PENDING 恢复时 Agent 上下文丢失）。没有原子化的调度点推进机制。
3. **priority 排序无持久化保证**：pending 队列维护在内存中，插件重启后丢失。恢复机制将 RUNNING 降级为 PENDING，但 PENDING 任务不会在恢复中保留原有的队列排序。
4. **`_do_on_task_completed` 非幂等**：直接回调与事件监听共用同一完成处理，极端情况下可能重复触发 cron 重排。

### 相关文档

- [任务模型](./01-task-model.md)：状态机与触发类型定义
- [持久化与恢复](./03-persistence-recovery.md)：`expected_status` 守卫保存与重启恢复
- [命令总线](./11-command-bus.md)：CANCEL / PAUSE / RESUME 命令的协作通道