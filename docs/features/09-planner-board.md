# Planner 看板

## 设计目标

主 Planner 负责调度后台 Agent 任务，但它默认对当前聊天流里有哪些任务在跑、处于什么状态一无所知。它可能重复创建相同任务，或误判一个等待用户输入的任务已经完成。Planner 看板（PlannerBoard）解决这个问题：在 Planner 每次发起 LLM 请求前，把当前流的活跃、定时与最近完成任务摘要，以 `<task_board>` XML 块注入到消息里，让 Planner 感知后台正在发生什么，从而做出更合理的调度决策。

举一个场景：用户在群里发起「帮我爬数据」，Planner 创建任务后进入后台执行。若没有看板，下一轮 Planner 请求看不到这个任务，可能又创建一个一模一样的；有了看板，Planner 一眼读到 `[RUNNING] 爬取用户数据`，自然会接着已有任务继续，而不是重复创建。

摘要分三类：

| 类别 | 状态 | 作用 |
|---|---|---|
| 活跃任务 | RUNNING / WAITING_INPUT / PAUSED | 让 Planner 知道有哪些任务正在跑，避免重复创建 |
| 定时任务 | SCHEDULED | 告知即将触发但尚未执行的任务 |
| 最近完成 | COMPLETED / FAILED / CANCELLED | 让 Planner 看到刚结束的任务，便于衔接后续 |

> 架构说明：插件组件组装已从 plugin.py 收敛到 lifecycle.py 这一唯一组装点（`load_plugin`），PlannerBoard 的创建随之落在 lifecycle.py:130-138。

## 设计方案

### PlannerBoard 职责

PlannerBoard（planner_hooks.py）是看板的唯一实现，职责有两个：构建摘要、注入去重。它持有 `TaskStore`（查询任务）、`PlannerBoardConfig`（条数上限与开关）与 `PromptService`（模板渲染），在 `load_plugin` 第 9 步实例化（lifecycle.py:130-138）。

### 注入时机

通过 `maisaka.planner.before_request` Hook 挂在每轮 Planner 请求前，注册为 BLOCKING + EARLY（plugin.py:573-581），保证摘要先于其他 Hook 注入，Planner 读到的是最新任务状态。Hook 处理函数是 `hook_before_request()`（planner_hooks.py:153），任何异常都兜底返回 continue，绝不阻断 Planner 主流程。

### marker+hash 混合去重

Planner 的请求是流式的，同一轮对话会发起多次 LLM 请求，摘要内容若不变就不该重复注入。单靠一种手段都不稳：只看 marker 无法知道摘要内容是否已变化；单看内存 hash 又会在 messages 被其他 Hook 改动后误判。因此 PlannerBoard 用两层防御叠加：

- **marker 检查**：正则 `<task_board session="...">` 扫描已有 messages，判断本 session 是否已注入过看板。提取逻辑在 `_extract_marker_session()`（planner_hooks.py:235-253）。
- **hash 去重**：对摘要做 sha256，存进 `session_id → last_hash` 内存映射（planner_hooks.py:59），内容未变且已注入则跳过本轮。

只有当 marker 匹配的 session 与当前一致、且摘要 hash 与上次相同，才跳过注入；任一条件不满足（内容变化、首次注入、session 切换）都会重新注入。两层互为兜底，兼顾了内容变化检测与跨请求的幂等。

### 摘要构建与渲染

`build_summary()`（planner_hooks.py:68）是数据准备层，职责三块：查数据、筛状态、委托渲染。先通过 `list_active()` 取当前流所有非终端任务，按 running → waiting_input → paused → scheduled 分组，分别截断到 `max_active` / `max_scheduled`；再通过 `_fetch_recent_terminal()`（planner_hooks.py:128）对三种终态分别查询，合并后按 `updated_at` 倒序截取 `max_recent` 条。全空则返回空串，让上层跳过注入。

非空时调用 `prompt_service.build("planner_board", ...)`（planner_hooks.py:120-126）。这个 `prompt_service` 是 `load_plugin` 时构造的 PromptService 实例（lifecycle.py:92-93），持有所有 8 个 builder，按 name 分发到 PlannerBoardBuilder。Builder（prompt/builders/planner_board.py）把 TaskRecord 预格式化为模板可迭代的 dict 列表，再经 Jinja2 模板渲染成 `<task_board>` XML 块。

数据筛选与展示格式在此解耦：数量限制、排序在 `build_summary()` 完成；中文文案、XML 结构在模板 `prompt/templates/planner_board.md` 里。修改任一侧都不影响另一侧。最终输出的摘要类似：

```xml
<task_board session="qq:g:123456">
当前后台任务看板：
活跃任务：
- [RUNNING] 爬取用户数据（运行中 65 秒）
- [WAITING_INPUT] 确认输出格式（等待中 3 分钟）
定时任务：
- [SCHEDULED] 每日报表（15 分钟后触发）
最近完成：
- [COMPLETED] 分析日志（2 分钟前）
</task_board>
```

### 注入流程

`hook_before_request()`（planner_hooks.py:153）按固定顺序串行处理：先检查 `enabled`，关闭则直接返回；再提取 `session_id` 与 `messages`，会话为空也跳过；随后 `build_summary()` 构造摘要，全空则跳过；接着做 marker 与 hash 双重检查，未变则跳过；最后把摘要作为一条 system 消息追加到 messages 尾部，返回带 `modified_kwargs` 的完整消息列表。因为注册为 BLOCKING + EARLY，注入后的 messages 会传递给后续 Hook 和 Planner，保证摘要入场。

## 使用与配置

### 配置节 [planner_board]

`PlannerBoardConfig`（config.py:144-182）对应 `config.toml` 的 `[planner_board]` 节：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否向 Planner 注入任务摘要；关闭后 Hook 直接返回 continue |
| `max_active` | `5` | 活跃任务条数上限 |
| `max_scheduled` | `3` | 定时任务条数上限 |
| `max_recent` | `3` | 最近完成任务条数上限 |

条数上限只控制注入条数，不影响 TaskStore 实际查询。配置热更新时 `apply_config_update`（lifecycle.py:160-217）会重建 PlannerBoard（lifecycle.py:203-209），并传入 `prompt_service`，同时清空 hash 去重状态，保证下次请求重新注入。这意味着改看板配置无需重启插件，WebUI 保存后立即生效。

对一般用户而言，看板默认开启、无需任何配置即可工作；只有当消息里不断出现冗余的任务摘要、影响回复质量时，才需要把 `enabled` 调成 `false` 关闭注入。

若想调整看板文案或结构，直接编辑模板 `prompt/templates/planner_board.md` 即可，无需改动 Python 代码；但需保持模板变量与 PlannerBoardBuilder 传入的字段一致，否则渲染会因变量缺失而报错。

### 已知限制

- `_last_hash` 是内存映射（planner_hooks.py:59），插件重启后丢失，首个请求必然重新注入。
- marker 检查只扫描字符串类型的 content（planner_hooks.py:249-251），多模态列表内容会被跳过，可能误判无 marker 而重复注入。
- Hook 对 Planner 所有请求一视同仁，不区分工具调用、消息回复或错误重试，无法按请求类型裁剪摘要。

### 相关文档

- [提示词系统](./12-prompt.md)：planner_board builder 与模板渲染体系
- [命令系统](./13-commands.md)：任务管理命令与状态查看
- [配置体系](./14-config.md)：配置节总体说明与热更新机制