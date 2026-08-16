# 命令使用指南（Command）

本页是 `/maitask` 命令组的**用户向**使用手册。命令由插件注册到 MaiBot 宿主
（`plugin.py` 的 `@Command`，实现见 `commands.py`），所有子命令都有 `/mt` 短别名。

## 命令一览

| 命令 | 作用 | 权限 |
|---|---|---|
| `/maitask help` | 显示帮助（未匹配子命令时自动兜底显示） | 所有人 |
| `/maitask create <意图>` | 创建任务并开始执行 | user 及以上 |
| `/maitask list [-all] [状态]` | 列出任务 | 所有登录用户（看自己的） |
| `/maitask status <任务ID>` | 查看任务详情 | 任务所有者 |
| `/maitask cancel <任务ID>` | 取消任务 | 任务所有者 |
| `/maitask history <任务ID>` | 查看任务执行历史 | 任务所有者 |
| `/maitask ask <任务ID> <指令>` | 向任务注入指令 / 回答提问 | 任务所有者 |

任务 ID 支持**前缀匹配**：创建回复里给出的是前 8 位短 ID，`status` / `cancel` / `history` /
`ask` 直接用短 ID 即可，不必抄完整 UUID。

## 各命令详解

### `/maitask help`

显示命令帮助。任何以 `/maitask` 开头但未匹配上述子命令的输入（如 `/maitask foo`）
都会被兜底命令拦截并显示帮助，不会落入 MaiBot 的 planner 流程。

```
/maitask help
/mt help        # 短别名
```

### `/maitask create <意图>`

用自然语言描述你想让 Agent 做的事，创建任务并立即开始执行（可延迟 / 定时，见下文）。

```
/maitask create 帮我整理这个月的收支记录，输出一份摘要
/mt create 每天下午 6 点提醒我喝水        # 定时任务用自然语言描述即可
```

创建成功后回复示例：

```
任务已创建！
ID: a1b2c3d4
标题: 整理本月收支记录
级别: agent
状态: 排队中
```

- 不写 `<意图>` 会提示 `用法: /maitask create <意图描述>`。
- 任务标题由插件调用 LLM 根据意图自动生成。
- 定时任务（cron / 延迟）目前主要面向主 Planner 的 `subagent_schedule` 工具；
  聊天里用自然语言描述时间点即可，插件会理解。

### `/maitask list [-all] [状态]`

列出任务（最多 20 条）。默认只看**你自己**的任务；管理员加 `-all` 可看全部任务
（含 planner 任务），非管理员传 `-all` 会被静默忽略。

```
/maitask list                       # 我的全部任务
/maitask list running               # 只看运行中
/maitask list -all                  # 管理员：全部任务
/maitask list waiting_input         # 等待你回复的任务（重点！）
```

可选状态：`pending`（排队中）/ `running`（运行中）/ `waiting_input`（等待用户输入）/
`scheduled`（定时待触发）/ `paused`（已暂停）/ `completed`（已完成）/ `failed`（失败）/
`cancelled`（已取消）。传无效状态会提示可选列表。

输出示例：

```
共 2 个任务:
  [a1b2c3d4] agent/running 整理本月收支记录 — 运行中（已执行 2 轮）
  [e5f6a7b8] instant/completed 发送欢迎消息 — 已完成
```

### `/maitask status <任务ID>`

查看单个任务的完整详情：ID、标题、意图、级别、状态、所有者、创建时间。

```
/maitask status a1b2c3d4
```

输出示例：

```
任务详情:
  ID: a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  标题: 整理本月收支记录
  意图: 帮我整理这个月的收支记录，输出一份摘要
  级别: agent
  状态: 运行中
  所有者: qq:12345678
  创建时间: 2025-08-16 10:30
```

### `/maitask cancel <任务ID>`

取消任务。对任意非终态任务生效（排队中、运行中、挂起等待输入、定时未触发都可取消）。

```
/maitask cancel a1b2c3d4
```

### `/maitask history <任务ID>`

查看任务的执行历史（状态流转与关键事件），展示最近 10 条。用于排查「任务为什么失败 /
卡在哪一步」。

```
/maitask history a1b2c3d4
```

### `/maitask ask <任务ID> <指令>` — 最重要的人机交互入口

向任务注入指令，两种典型场景：

1. **回答挂起中的提问**。任务执行中调用 `ask_user` 向你提问后会进入 `waiting_input`
   状态（`/maitask list waiting_input` 可随时查看有哪些任务在等你），此时用本命令回答：

   ```
   /maitask ask a1b2c3d4 用 QQ 邮箱，抄送 leader@example.com
   ```

2. **调整运行中任务的方向**。不打断任务，注入一条新指令让 Agent 继续：

   ```
   /maitask ask a1b2c3d4 预算部分改用图表展示
   ```

回复会进入 Agent 的下一个推理轮次，任务随后自动继续执行。

## 典型使用流程

```
你: /maitask create 对比这三款云服务器的价格，出一份对比表
插件: 任务已创建！ ID: a1b2c3d4 ...
你: /maitask list waiting_input
插件: 共 1 个任务: [a1b2c3d4] agent/waiting_input ...
你: /maitask ask a1b2c3d4 按一年期计费价格对比
插件: 指令已注入任务 a1b2c3d4...
(片刻后任务完成，结果自动发到本聊天流)
你: /maitask history a1b2c3d4     # 想看看它都做了什么
```

## 与 Planner 工具的关系

同一套任务能力也以工具形式暴露给 MaiBot 主 Planner：`subagent_create` / `subagent_list` /
`subagent_status` / `subagent_modify` / `subagent_delete` / `subagent_history` /
`subagent_schedule`。聊天中直接吩咐 planner「帮我挂个后台任务」也会走这条路径；
`/maitask` 命令是用户侧的操作入口，两者操作的是同一批任务。

实现细节见 [命令系统](../features/13-commands.md) 与 [任务模型](../features/01-task-model.md)。
