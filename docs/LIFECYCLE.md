# 生命周期总览

本文档从设计的逻辑讲述 oh-mai-agent 的四大核心生命周期：任务、插件、Agent 循环、回复路径。它们共同回答一个问题：**一条离线任务从创建到最终回复，中间经历了什么，每一步为什么这样设计**。所有代码引用均为当前 HEAD 的实际文件路径与行号，只标注关键触发点，不贴函数体。

> 架构变更：v0.1.0 曾尝试把 instant 任务迁移到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配，回退到进程内 contextvars + usecase 分层方案。当前实现全部在 Runner 进程内。

### 四大生命周期如何串联

四个生命周期不是孤立的，而是一条链：**插件生命周期**负责把整套运行时组装起来（②）；**任务生命周期**负责任务从入队到终态的状态流转（①）；agent 级任务进入 **Agent 循环生命周期**执行多轮推理与工具调用（③）；循环结束或 instant 任务执行完毕后，结果经 **回复路径生命周期**拆成独立回复任务送达用户（④）。

```
插件加载（② load_plugin）
   │
   ▼
任务创建 → 入队（① 状态机：SCHEDULED/PENDING → RUNNING）
   │
   ├─ instant 级 ──→ ④ 回复路径（润色 + 发送 + 完成）
   │
   └─ agent 级 ──→ ③ Agent 循环（30 轮 LLM + 工具）
                        │
                        ├─ ask_user → WAITING_INPUT → 用户回复唤醒
                        │
                        └─ 完成/失败 ──→ ④ 回复路径（拆回复任务送达）
```

---

## ① 任务生命周期

### 设计逻辑

任务生命周期要解决的核心问题是：**离线任务的状态必须可观测、可恢复、可被外部控制**。MaiBot 主流程是实时聊天，任务一旦创建就脱离调用方独立运行，调用方（用户、Planner、其他插件）只能通过状态感知它的进度。因此任务不能只有"在跑 / 跑完"两个状态，而要有完整的中间态：等待调度、排队、运行、挂起等待用户输入、暂停。

这个设计还隐含一个约束：**状态变更必须走受限状态机**。直接赋值会绕过合法性校验，导致任务从终态"复活"或跳过中间态。所以 `TaskRecord` 提供两个入口：`transition()` 走合法转换矩阵，非法转换抛 `TaskStatusError`；`force()` 是跳过校验的兜底逃逸口，只用于终态回退、恢复、异常兜底等场景，两者都追加 `_status_log` 留审计。

### 状态机

8 个状态定义在 `TaskStatus` 枚举（`domain/task_record.py:31-41`），合法转换矩阵 `_ALLOWED_TRANSITIONS`（`domain/task_record.py:64-76`），终态集合 `_TERMINAL_STATUSES`（`domain/task_record.py:79-81`，completed / failed / cancelled）。

```
                        ┌──────────┐
                        │SCHEDULED │  ← 延迟/cron 任务入队
                        └──┬───┬───┘
                 到期触发  │   │  取消
         ┌──────────────┘   │   └──────────────────────┐
         ▼                  │                          ▼
    ┌─────────┐        取消  │                     ┌───────────┐
    │ PENDING │◄────── 并发满│                     │ CANCELLED │
    └────┬─────┘             │                     └───────────┘
  额度空 │                   │                          ▲
        ▼                   │                    取消   │
    ┌─────────┐   超时/异常/完成                       │
    │ RUNNING │───────┬──────────┬──────────┐          │
    └──┬───┬──┘       │          │          │          │
       │   │          ▼          ▼          ▼          │
       │   │   ┌──────────┐ ┌───────┐ ┌──────────┐    │
       │   │   │ COMPLETED│ │FAILED │ │  PAUSED  │────┘
       │   │   └──────────┘ └───────┘ └────┬──────┘
       │   │                               │ 恢复
       │   │ ask_user                      ▼
       │   └────────→┌──────────────┐   返回 PENDING
       │             │WAITING_INPUT │
       │             └──────┬───────┘
       │        用户回复/取消  │
       │         ┌────────────┴───────┐
       │         ▼                    ▼
       │   返回 RUNNING            CANCELLED
       │
       │   cron 完成 → 重置回 SCHEDULED（循环执行）
       └──────────────────────────────────────────┘
```

状态转换入口：`transition()`（`domain/task_record.py:209-234`）校验合法性并追加审计日志；`force()`（`domain/task_record.py:236-258`）跳过校验强制落状态；`is_terminal()`（`domain/task_record.py:273-275`）判断是否已到终态。

### 状态转换速查

| 当前状态 | 允许目标 | 触发条件 | 关键代码 |
|---|---|---|---|
| SCHEDULED | PENDING | 到点触发（_check_loop 每秒检查） | `core/scheduler.py:464-567` |
| SCHEDULED | CANCELLED | 用户取消 | `core/scheduler.py:222-265` |
| PENDING | RUNNING | 并发额度有空余 | `core/scheduler.py:394-430` |
| PENDING | CANCELLED | 用户取消 | `core/scheduler.py:222-265` |
| RUNNING | WAITING_INPUT | Agent 调用 ask_user | `executor/agent_loop.py:170-223` |
| RUNNING | PAUSED | 手动暂停 / 插件停止 | `core/scheduler.py:267-289` |
| RUNNING | COMPLETED | Agent 循环正常结束 | `executor/agent_loop.py:564-603` |
| RUNNING | FAILED | 异常 / 超时 | `executor/agent_loop.py:605-620` / `core/scheduler.py:464-567` |
| WAITING_INPUT | RUNNING | 用户回复唤醒 | `executor/agent_loop.py:213` / `core/usecases/task_control.py:86-124` |
| WAITING_INPUT | CANCELLED | 用户取消 | `core/scheduler.py:222-265` |
| PAUSED | PENDING | 手动恢复 | `core/scheduler.py:291-328` |
| PAUSED | CANCELLED | 用户取消 | `core/scheduler.py:222-265` |
| COMPLETED/FAILED/CANCELLED | 无 | 终态，不可再转换 | `domain/task_record.py:79-81` |

### 入队分发

`TaskScheduler.enqueue()`（`core/scheduler.py:151-208`）按触发类型三分：

- **NOW**：直接置 PENDING 入队（按 priority 降序），触发 `_try_dispatch` 尝试立即执行
- **DELAY**：计算 `scheduled_at = now + delay_seconds`，置 SCHEDULED 落盘
- **CRON**：经 croniter 计算下次触发时间，置 SCHEDULED 落盘；表达式非法直接置 FAILED

### 后台调度循环

`_check_loop()`（`core/scheduler.py:464-567`）每秒轮询一次，做三件事：

1. 扫描 SCHEDULED 任务，到点转 PENDING 入队
2. 检测 RUNNING 任务是否超过 `max_runtime_min`（配置为 0 则不启用），超时则 `transition(FAILED)` 并用**守卫保存** `expected_status=RUNNING`（`core/scheduler.py:543-554`），再经命令总线发 `CANCEL` 协作停止循环（`core/scheduler.py:558-560`）。守卫保存的意义：若 get 之后、save 之前循环已把任务落成终态（COMPLETED/CANCELLED），本次 FAILED 写入会被拒绝，防止超时覆盖正常完成
3. 调用 `_try_dispatch()`（`core/scheduler.py:394-430`）做 pending → running 分发：额度检查 → `transition(RUNNING)` → 同步先登记 `_running` 再 await save（防并发重复执行）→ `asyncio.create_task` 异步执行；save 失败则 `force(PENDING)` 回滚，避免队首卡死

### 取消 / 暂停 / 恢复

- `cancel()`（`core/scheduler.py:222-265`）：SCHEDULED / PENDING / PAUSED 直接转 CANCELLED 落盘并出队；RUNNING / WAITING_INPUT 的 agent 任务经命令总线发 `CANCEL`，由 AgentLoop 协作停止
- `pause()`（`core/scheduler.py:267-289`）：RUNNING 任务经 `set_coop_paused(True)` 落盘（键 `META_COOP_PAUSED`）+ 发 `PAUSE` 命令，循环在自有 task 上响应
- `resume()`（`core/scheduler.py:291-328`）：RUNNING（须 `is_coop_paused()` 为真）清标记 + 发 `RESUME`；PAUSED 则重新排队回 PENDING

### Cron 循环重调度

`on_task_completed()`（`core/scheduler.py:332-358`）释放并发额度后，仅对 COMPLETED 的 CRON 任务调用 `_reschedule_cron()`（`core/scheduler.py:360-375`）：`force(SCHEDULED)` + 守卫保存 `expected_status=COMPLETED`。FAILED / CANCELLED 的 CRON 任务不循环，避免死循环。

### 相关文档

- [任务模型](./features/01-task-model.md)
- [调度器](./features/02-scheduler.md)
- [持久化与恢复](./features/03-persistence-recovery.md)
- [权限模型](./features/04-permission.md)

---

## ② 插件生命周期

### 设计逻辑

插件生命周期要解决的核心问题是：**组装顺序与依赖方向**。插件有十几个组件（存储、注册表、权限、总线、调度器、提示词、任务管理器、MCP、看板、API），它们之间存在严格的依赖顺序：调度器需要 executor 回调，executor 回调又需要 TaskManager，TaskManager 需要存储和总线。如果全部内联在 `plugin.py` 的 `on_load` 里，入口文件会膨胀且难以测试。

因此组装逻辑下沉到 `lifecycle.py` 的 `load_plugin()`（`lifecycle.py:43-153`），`plugin.py` 的钩子只做转发：`on_load` → `load_plugin`（`plugin.py:46-48`）、`on_unload` → 三步清理（`plugin.py:50-66`）、`on_config_update` → `apply_config_update`（`plugin.py:68-76`）。`create_plugin()` 工厂（`plugin.py:589-603`）只负责实例化。

另一个设计决策：**TaskManager 是门面 + 组装器，不是编排器**。它在构造内部实例化两个 usecase：`TaskCrud`（持久化 CRUD，`core/task_manager.py:103`）与 `TaskControl`（执行控制，`core/task_manager.py:124`），对外暴露统一方法。commands / api_expose / planner tools 一律经门面调用，不直接接触 usecase。

### load_plugin 组装流程

```
on_load → load_plugin()（lifecycle.py:43-153）
═══════════════════════════════════════════════════════════════
 1. TaskStore 初始化（sqlite, tasks.db）          lifecycle.py:51-52
 2. ToolRegistry + PermissionResolver             lifecycle.py:56-57
 2.5 TaskCommandBus（进程内命令路由）             lifecycle.py:65
 3. TaskScheduler（executor 闭包打破循环依赖）     lifecycle.py:81-86
 4. PromptManager + PromptService（7 builders）   lifecycle.py:89-93
 5. TaskManager 构造 + setup()                    lifecycle.py:96-110
    └─ 内部实例化 TaskCrud / TaskControl / ExecutorFactory
 6. scheduler.start()                             lifecycle.py:117
 7. MCPManager 启动 + 注册 MCP 工具                lifecycle.py:120-126
 8. recover_active_tasks() 恢复活跃任务            lifecycle.py:128（MCP 先注册，恢复的 agent 任务首轮即可看到 MCP 工具）
 9. PlannerBoard 看板初始化                        lifecycle.py:133-139
10. 注册 6 个跨插件动态 API                        lifecycle.py:141-150
```

关键点：第 3 步构造 `TaskScheduler` 时**不传 executor**（`executor` 改为可选，`core/scheduler.py`），TaskManager 就绪后经 `scheduler.set_executor(task_manager.execute_task)`（`lifecycle.py`）后绑定，打破「调度器 → 执行器 → 任务管理器 → 调度器」的构造环；第 5 步 `setup()`（core/task_manager.py 的 `setup`）注册任务管理工具、info、file（role_provider 取 current_task 角色）、ask_user、send_message、跨插件 API、子 Agent 与命令执行工具（`[shell] enabled` 关闭时不注册）。

### 配置热更新

`apply_config_update()`（`lifecycle.py:160-217`）按序传播新配置：重建 PermissionResolver → `tm.update_resolver`（`core/task_manager.py:246`）→ `scheduler.update_config`（`core/scheduler.py:69-80`）→ `tm.update_config`（`core/task_manager.py:230`）→ `reload_mcp_if_changed`（`lifecycle.py:288-314`，比较新旧 MCP 配置，变更则重建连接并重新注册工具）→ 重建 PlannerBoard。

### 恢复机制

`recover_active_tasks()`（`lifecycle.py` 的 `recover_active_tasks`）在加载第 8 步调用，恢复上次运行期间未完成的任务：

| 原状态 | 恢复动作 |
|---|---|
| SCHEDULED | 重新入队等待定时触发 |
| PENDING | 重新入队（崩溃/停机瞬间已落库但未派发的任务；调度器 pending 队列纯内存，重启必须回读 DB） |
| RUNNING | 降级为 PENDING 重新排队（Agent 上下文丢失），经 `mark_recovered_from_running()` 打标记（键 `META_RECOVERED_FROM_RUNNING`） |
| WAITING_INPUT | 保持状态，等待用户回复经 Hook 重新唤醒 |
| PAUSED（paused_by_stop） | 自动降级 PENDING 重新入队（优雅停机与崩溃恢复对称），标记清除 |
| PAUSED（用户主动）/ 终态 | 不动 |

### 相关文档

- [调度器](./features/02-scheduler.md)
- [持久化与恢复](./features/03-persistence-recovery.md)
- [MCP 集成](./features/08-mcp.md)
- [PlannerBoard 看板](./features/09-planner-board.md)
- [跨插件 API](./features/10-cross-plugin-api.md)
- [命令总线](./features/11-command-bus.md)
- [配置系统](./features/14-config.md)

---

## ③ Agent 循环生命周期

### 设计逻辑

Agent 循环要解决的核心问题是：**长时自主任务如何与 LLM 交互**。agent 级任务不是一次调用，而是「推理 → 工具调用 → 观察结果 → 再推理」的循环，最多 30 轮（`executor/agent_loop.py:70`）。这个循环必须处理三类中断：用户注入指令、ask_user 挂起等待回复、取消/暂停。它们都经命令总线异步到达，循环每轮消费。

执行上下文经 `executor/context.py` 的 `current_task` ContextVar 传递（`executor/context.py:11-13`），唯一 set 方是 `AgentExecutor.execute()`（`executor/agent.py:76`），`finally` 中 reset 防止泄漏到并发任务。角色回调 `make_role_provider`（`executor/context.py:16-31`）按任务 owner / stream_id 构造，供工具按角色过滤。

### 主循环

`AgentLoop.run()`（`executor/agent_loop.py:349-626`）四个阶段：

```
PENDING 入队 → 并发额度空余 → RUNNING
    │
    ▼
1. 进入 RUNNING（已 RUNNING 则跳过）        agent_loop.py:370-371
    │
    ▼
2. 构建上下文：system prompt + 历史回放      agent_loop.py:377-398
    │
    ▼
3. LLM 循环（1..30 轮）                     agent_loop.py:401
    │  每轮：
    │   ├─ 消费注入指令 _consume_injections   agent_loop.py:234-256
    │   ├─ 构建工具 schema（按角色）          agent_loop.py:127-150
    │   ├─ generate_with_tools               agent_loop.py:420-425
    │   │    timeout_ms=240000, model="planner"
    │   ├─ 无 tool_calls → 最终回复，落历史
    │   └─ 有 → 逐工具执行 + 结果追加 messages
    │
    ▼
4. 收尾（守卫完成路径）                     agent_loop.py:564-603
    ├─ _cancelled 复查 → _finalize_cancelled
    ├─ send_final 发送最终回复
    ├─ 重载持久化，终态则跳过 COMPLETED
    ├─ transition(COMPLETED) + 守卫保存 expected_status
    └─ 异常 → FAILED（transition 失败则 force 兜底）
```

### ask_user 挂起 / 恢复

`_handle_ask_user()`（`executor/agent_loop.py` 的 `_handle_ask_user`）实现 RUNNING → WAITING_INPUT → RUNNING：

1. 创建 `asyncio.Event` 并登记到 `_resume_events`（存实例属性而非 metadata，Event 不可 JSON 序列化）
2. `transition(WAITING_INPUT)` + save
3. 调 `on_ask` 回调向用户发问；无回调则直接 set 事件避免无限挂起
4. `await resume_event.wait()` 阻塞等待
5. 收到回复后转回 RUNNING，经 `take_user_reply()` 读取回复

唤醒不依赖事件广播：用户回复经 Hook → `TaskControl.handle_user_reply` → `bus.send(RESUME_REPLY)`，由主订阅 `_on_bus_command` 处理（v0.1.0 的 `WAITING_INPUT` 事件与 ask_user 临时订阅已移除）。

### 指令注入与总线命令

`_consume_injections()`（`executor/agent_loop.py:234-256`）每轮 LLM 调用前经 `take_injections()` 弹出指令，构建为 system 消息插入。`_on_bus_command()`（`executor/agent_loop.py:258-299`）处理四类命令：INJECT 经 `push_injection()` 写入注入队列；RESUME_REPLY 经 `set_user_reply()` 写入 + set 事件；CANCEL 置 `_cancelled` + set 事件；PAUSE 置 `_paused` + 在循环自有 task 上经 `set_coop_paused()` 写标记。

### 最终完成路径的守卫

`run()` 收尾段（`executor/agent_loop.py:564-603`）是并发安全的关键：`send_final` 可能耗时 120s+（润色 LLM + 重试），期间可能收到 CANCEL 或调度器超时 FAILED。因此发送后重新检查 `_cancelled`、重载持久化判断终态，最后 `transition(COMPLETED)` + 守卫保存 `expected_status=persisted.status`；save 被并发终态拒绝则跳过 COMPLETED 事件，保证事件与记录一致。

### 相关文档

- [任务模型](./features/01-task-model.md)
- [调度器](./features/02-scheduler.md)
- [工具层](./features/05-tools.md)
- [ask_user 提问](./features/07-ask-user.md)
- [命令总线](./features/11-command-bus.md)
- [Prompt 系统](./features/12-prompt.md)

---

## ④ 回复路径生命周期

### 设计逻辑

回复路径要解决的核心问题是：**任务结果如何可靠地送达用户**。agent 任务完成、instant 任务、失败消息都产生对外回复，如果各自直发，就无法统一重试、无法在插件重启后恢复。因此设计把回复拆成独立的 instant 任务：`TaskControl._dispatch_reply_instant()`（`core/usecases/task_control.py:49-72`）创建经 `mark_as_reply()` 标记的 instant 任务落库并入队（键 `META_IS_REPLY`），走与普通任务完全相同的调度与执行链路。这样回复可重试、可恢复、不会丢消息。

### 回复路径流程

```
Agent 循环完成 / Instant 任务
         │
         ▼
┌────────────────────────────────────┐
│ _dispatch_reply_instant()          │  task_control.py:49-72
│  创建 PENDING instant 任务          │
│  stream_id = task.reply_target     │
│  mark_as_reply()                   │
└───────────────┬────────────────────┘
                │
                ▼
┌────────────────────────────────────┐
│ scheduler.enqueue()                │  入队到调度器
└───────────────┬────────────────────┘
                │
                ▼
┌────────────────────────────────────┐
│ InstantExecutor.execute()          │  executor/instant.py
│  ┌──────────────────────────────┐  │
│  │ ReplySender.send_polished()  │  │  完整出口：信息获取→润色→直发
│  │  ├─ PolishService.polish()   │  │  仅执行一次，失败回退原文
│  │  ├─ split_message(分段)      │  │  两级：按行优先，无换行按句号，≤max_messages 条
│  │  └─ ctx.send.text(逐段发送)  │  │
│  │      └─ 失败 → 指数退避重试   │  │  1s → 2s（2^attempt）
│  │         检测 False/None 掉包  │  │
│  ├─ 跨流回复补写动机注释         │  │  append_motivation_note()
│  └──────────────┬───────────────┘  │
│                 ▼                  │
│  complete_and_notify()             │  base.py:114-118
│  task → COMPLETED + 释放额度        │
└────────────────────────────────────┘
         │ 异常
         ▼
┌────────────────────────────────────┐
│ fail_task()                        │  executor/sender.py
│  可选: ReplySender.send_raw(失败)   │  直发出口：错误文本不润色
│  task → FAILED（终态守卫 + force）  │
└────────────────────────────────────┘
```

### 润色与发送

`ReplySender`（`executor/sender.py`）是回复路径的核心，提供两条发送出口与独立上下文注释能力，如实记载四个行为：

1. **两条出口**：`send_raw`（直发：分割 + 重试，无润色，用于命令/失败通知等确定性文本）与 `send_polished`（完整：信息获取 → 润色 → 复用直发发送段，用于任务回复/提问/send_message）；发送出口**不做任何上下文写入**
2. **润色仅执行一次**：`PolishService.polish()` 拉取聊天记录和黑话表经 LLM 润色，失败时自动回退到原始文本
3. **长回复分割**：`split_message()`（`executor/splitter.py`）两级切分——含换行按行优先（行超长再行内按句号），无换行按句号——把回复切成多条（≤ `max_messages` 条、每段 ≤ `max_length`），保留原文不丢内容，统一跟随 `[splitter]` 配置
4. **逐段指数退避重试 + 静默掉包检测**：每段 `ctx.send.text()` 失败时重试（`config.send.max_retries` 次，间隔 1s → 2s），返回 `False/None` 视为失败，任一段耗尽即停止后续分段并抛异常

跨流回复（`reply_stream_id` 或 `is_reply_task()`）经 `append_motivation_note()` 补写动机 XML 注释——对用户不可见，写给 MaiBot/Planner 上下文，是插件工作内容进入认知层的关键通道。

### 完成与失败

- 成功路径：`complete_and_notify()`（`executor/base.py:114-118`）`transition(COMPLETED)`（拒绝时 force 兜底）→ save → `scheduler.on_task_completed()` 释放并发额度。仅 instant 路径使用，agent 在 AgentLoop.run() 内部自管终态
- 失败路径：`fail_task()`（`executor/sender.py`）可选先经 `ReplySender.send_raw` 直发失败消息（错误文本不润色），再 `transition(FAILED)`（失败回退 force），落盘后通知调度器释放额度

### 用户回复唤醒

`TaskControl.handle_user_reply()`（`core/usecases/task_control.py:86-124`）由 `chat.receive.after_process` Hook（`plugin.py:533`）触发：

1. 从 stream_id 提取 platform 前缀，拼接 `platform:user_id` 完整 owner
2. 查询该 stream 下 WAITING_INPUT 且 owner 匹配的任务（owner 精确比较）
3. 经 `set_user_reply()` 写入回复，然后经命令总线发送 `RESUME_REPLY` 唤醒等待中的 AgentLoop
4. 单次只唤醒第一个匹配任务

### 相关文档

- [回复润色](./features/06-polish.md)
- [ask_user 提问](./features/07-ask-user.md)
- [调度器](./features/02-scheduler.md)
- [命令总线](./features/11-command-bus.md)