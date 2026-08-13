# oh-mai-agent 重构方案

> 状态：草案 v0.1（待审查）
> 目标：解决 prompt 抽象不足、i18n 缺失、可维护性、异步管理问题；
> 并为后续「离线任务派遣子进程」架构预留抽象边界。

---

## 1. 现状诊断

### 1.1 已实证的问题

| # | 问题 | 证据 | 严重度 |
|---|---|---|---|
| 1 | **运行时对象混入持久化结构**：`task.metadata["_resume_event"] = asyncio.Event()`，而 `TaskStore.save()` 直接 `json.dumps(task.to_dict())` → `TypeError`，ask_user 在真实 sqlite 存储下必崩（任务标记 FAILED） | `agent_loop.py:229-232` + `task_store.py:120`；已实证 `Object of type Event is not JSON serializable` | 致命 |
| 2 | **历史恢复 O(n²) 重复膨胀**：每轮存全量 messages 快照，恢复时 extend 所有轮次 → 续跑上下文重复消息按轮次平方增长 | `agent_loop.py:346-357` + `326-328` | 高 |
| 3 | **状态机被绕过 5 处**：`try: task.transition(x) except: task.status = x` 直接赋值，`@dataclass` 公有字段无强制 | `task_manager.py` / `scheduler.py` / `agent_loop.py` | 中 |
| 4 | **prompt 构建 4 种方式并存**：硬编码 f-string（agent system）、硬编码常量 + `.format()`（classify_level）、文件模板 + `.replace()`（polish）、程序化拼接（planner board） | `agent_loop.py:73-95` / `task_manager.py:46-63` / `polish.py` / `planner_hooks.py` | 中 |
| 5 | **prompts/ 目录名不副实**：设计文档承诺 `agent_system.md`、`title.md`，实际目录只有 `polish.md`；标题 LLM 生成是空壳（`_llm_title` 回调从未被注入） | `prompts/` 目录 + `plugin.py:92-100` | 中 |
| 6 | **死代码/重复**：`commands.py`（691 行）从未被 import，与 plugin.py `_cmd_*` 双轨；`_inject_event` 只读不写（僵尸唤醒机制） | grep 全项目无 `import commands`；`agent_loop.py:486` | 中 |
| 7 | **零 i18n**：prompt、用户可见文案、状态描述全部硬编码中文 | 全项目 | 中 |
| 8 | **测试 mock 掉了持久化层**：`FakeTaskStore.save()` 不序列化，恰好掩盖了问题 1 | `tests/test_agent_loop.py:200-239` | 高 |

### 1.2 根本原因

**运行时对象和持久化对象混在一起，通信靠进程内对象引用**（`AgentLoop._loops` 静态注册表、metadata 塞 Event/queue）。后续「离线任务派遣子进程」模式将彻底废弃对象引用——跨进程没有引用，只有**消息**和**共享存储**。

---

## 2. 设计原则（面向子进程架构）

> 子进程模式三个硬约束，现在就要为它们设计：

1. **DB 是唯一共享状态**——子进程不持有任何主进程对象引用。
2. **命令用消息、不用引用**——注入/回复/取消都是可 JSON 序列化的指令。
3. **AgentLoop 可移植**——只依赖 (store, llm_adapter, send_adapter, command_bus)，不依赖插件实例。

第一性原理：**一切可序列化，一切通过消息，DB 是唯一共享状态。**

---

## 3. 目标架构

```
现在（进程内）                                未来（子进程派遣）
┌─ 插件进程 ─────────────────┐              ┌─ 插件进程 (supervisor) ────────┐
│ TaskManager ─ Scheduler    │              │ TaskManager ─ Scheduler         │
│      │                     │              │      │        │                │
│      └─ AgentLoop (asyncio)│              │      │        └─ TaskCommandBus │
│           │ ▲              │              │      │            │ (JSON 行)   │
│  _loops 静态注册表 (引用)    │              │  TaskStore(sqlite)◄──┴──┐        │
│  metadata 塞 Event/queue   │              │      ▲    唯一共享状态源 │        │
└────────────────────────────┘              └──────┼──────────────────────────┘
                                                  │ IPC (stdio/socket)
                                          ┌───────┴────────┐
                                          │ AgentWorker #1  │  ← AgentLoop 原样运行
                                          │ AgentWorker #2  │  ← 只换 ctx/transport
                                          └────────────────┘
```

---

## 4. 重构模块

### 模块 1：Prompt 管理抽象（PromptManager + i18n）

**问题**：见 1.1 #4/#5/#7。

**目录结构**：

```
prompts/
├── en/
│   ├── agent_system.md
│   ├── classify_level.md
│   ├── title.md
│   └── polish.md
├── zh/                          # 默认
│   ├── agent_system.md          # ← 从 agent_loop.py 硬编码迁出
│   ├── classify_level.md        # ← 从 task_manager.py 常量迁出
│   ├── title.md                 # ← 补齐（现在是空壳）
│   └── polish.md                # ← 已有，移入
└── index.json                   # 模板元数据：变量声明、默认语言
```

**核心接口**：

```python
class PromptTemplate:
    name: str
    lang: str
    variables: frozenset[str]    # 声明的 {{var}}，渲染时校验

class PromptManager:
    """唯一 prompt 入口。禁止任何模块级硬编码 prompt 字符串。"""
    def __init__(self, templates_dir: Path, lang: str = "zh") -> None: ...
    def render(self, name: str, *, lang: str | None = None, **data: Any) -> str
    def set_lang(self, lang: str) -> None          # 运行期切换
    def snapshot(self) -> "PromptSnapshot"          # 子进程模式：打包模板给 worker

    # 渲染规则：
    # 1. 校验所有声明变量已提供（开发期 raise，运行期降级）
    # 2. 未声明变量 → raise（防 typo 静默吞掉）
    # 3. 模板内容内存缓存，index.json 变更触发重载
```

**迁移点**：
- `build_agent_system_prompt(task)` → `pm.render("agent_system", title=…, intent=…)`
- `classify_level_prompt.format(intent=…)` → `pm.render("classify_level", intent=…)`
- polish.py 走 `pm.render("polish", context=…, jargon=…)`
- **title.md 补齐并接通**：TaskManager 构造时注入 `llm_title` 回调（当前 plugin.py 未传）
- 变量分两类：**数据变量**（intent/title）与**组装变量**（context/jargon，由 renderer 预格式化）

**i18n**：`lang` 一次确定，`PromptManager` 与 `I18n` 共用来源（config 或 per-stream）。

---

### 模块 2：任务模型拆分 TaskRecord / TaskRuntime

**问题**：见 1.1 #1/#2。

```python
# ── 持久态：唯一可落库形态，所有字段 JSON 可序列化 ──
@dataclass
class TaskRecord:
    id: str
    title: str
    intent: str
    level: TaskLevel
    _status: TaskStatus                    # 私有，见模块 3
    owner: str
    stream_id: str
    platform: str
    trigger: TriggerInfo                   # 聚合：type/delay/cron/scheduled_at
    timestamps: Timestamps
    priority: int
    history: list[HistoryEntry]            # 增量，见下
    schema_version: int = 2                # DB 迁移用

# ── 内存态：永不落库 ──
@dataclass
class TaskRuntime:
    task_id: str
    resume_event: asyncio.Event
    inject_queue: deque[str]
    inject_event: asyncio.Event
    loop_ref: object | None                # 弱引用，防循环

class TaskRuntimeRegistry:
    """取代 AgentLoop._loops 静态注册表。内部持 task_id → TaskRuntime。"""
    def get(self, task_id: str) -> TaskRuntime | None: ...
    def attach(self, record: TaskRecord) -> TaskRuntime: ...
    def detach(self, task_id: str) -> None: ...
```

**三条硬规则**：
1. `json.dumps(TaskRecord.to_dict())` 永远不炸——不存在非 JSON 字段。
2. 任何跨层通信（注入/回复/唤醒）不碰 record 字段，走模块 4 的 `TaskCommandBus` + `TaskRuntime`。
3. `TaskRuntime` 随任务结束销毁，随进程退出消失，不需要恢复。

**history 修复**：
- 每轮照常存快照，但恢复时**只取最后一轮**（`task_store.get_history_snapshot(task_id)` → 只取 `max(round)` 完整快照）。
- 中间轮次存**增量**（`new_messages`），供审计展示；恢复不用。
- 效果：上下文重建 O(n²) → O(1)。

---

### 模块 3：状态机收口

**问题**：见 1.1 #3。

```python
@dataclass
class TaskRecord:
    _status: TaskStatus = TaskStatus.PENDING
    _status_log: list[StatusChange] = field(default_factory=list)

    @property
    def status(self) -> TaskStatus:
        return self._status

    def transition(self, new: TaskStatus, *, actor: str = "system") -> None:
        if new not in _ALLOWED_TRANSITIONS[self._status]:
            raise TaskStatusError(...)
        self._apply(new, actor)

    def force(self, new: TaskStatus, *, actor: str, reason: str) -> None:
        """唯一的恢复/兜底逃逸口。所有非标准转换必须走这里并留痕。"""
        self._apply(new, actor, reason)
```

- 反序列化走内部 `_restore(status, log)`，不触发校验。
- 恢复逻辑收口到单一 `TaskRecovery.recover(record)` 模块（并入 plugin.py `_recover_active_tasks`、scheduler.stop、agent_loop finally 的恢复逻辑）。
- `_status_log` 提供审计能力（actor + reason + 时间戳）。

---

### 模块 4：任务命令总线（核心抽象，面向子进程）

**问题**：注入/唤醒靠类方法 + 静态注册表 + metadata 字段，语义散在 4 个文件；子进程模式全废。

```python
class CommandKind(str, Enum):
    INJECT_INSTRUCTION = "inject"
    RESUME_REPLY = "resume"          # 用户回复
    CANCEL = "cancel"
    PAUSE = "pause"
    TIMEOUT = "timeout"

@dataclass
class TaskCommand:                   # 外部 → 运行中任务
    task_id: str
    kind: CommandKind
    payload: dict                    # {"instruction": ...} / {"reply": ...}
    ts: datetime

@dataclass
class TaskEvent:                     # 任务 → 外部（状态变更广播）
    task_id: str
    kind: EventKind                  # STARTED/WAITING_INPUT/RESUMED/COMPLETED/FAILED
    payload: dict
    ts: datetime

class Transport(Protocol):
    async def send(self, frame: bytes) -> None: ...
    async def receive(self) -> bytes | None: ...
    async def close(self) -> None: ...

class LoopbackTransport(Transport):
    """进程内实现：asyncio.Queue。现在就能用。"""

class TaskCommandBus:
    def __init__(self, transport: Transport) -> None: ...
    async def send(self, cmd: TaskCommand) -> bool: ...
    async def publish(self, event: TaskEvent) -> None: ...
    def subscribe(self, task_id: str, handler: Callable[[TaskCommand], Awaitable[None]]) -> None: ...
```

**未来子进程模式**：`StdioTransport` / `UnixSocketTransport`（JSON 行协议），`TaskCommandBus` 一行不改。命令流：

```
/task ask → TaskManager.modify_task → bus.send(TaskCommand(INJECT_INSTRUCTION))
  进程内:  → subscribe handler → runtime.inject_queue.append + inject_event.set()
  子进程:  → transport 序列化 → worker stdin → worker 侧同一逻辑
```

用户回复同理（`on_message → handle_user_reply → bus.send(RESUME_REPLY)`）。上层权限判定、任务匹配、存储全部复用，只有 transport 不同。scheduler 释放额度、planner board 刷新改听 `TaskEvent` 广播。

---

### 模块 5：L1/L2/L3 收敛为执行引擎策略

**问题**：一个状态机承载三种本质不同的执行语义，scheduler/task_manager 到处 `if level == L2` 分支。

```python
class TaskExecutor(Protocol):
    async def execute(self, ctx: ExecutionContext, record: TaskRecord) -> ExecutionResult

class L1InstantExecutor: ...    # 直发，无状态机：scheduled → done
class L2PlannerExecutor: ...    # proactive 触发即完成，无确认
class L3AgentExecutor: ...      # 唯一用完整状态机的
class SubprocessAgentExecutor: ...   # 未来：起子进程跑 L3，接口不变

class ExecutorFactory:
    def get(self, level: TaskLevel) -> TaskExecutor: ...
```

- 状态机**只为 L3 服务**；L1/L2 生命周期语义自包含在各自 executor。
- scheduler 不再需要 L2 分支（L2 限流变为 executor 内部信号量）。
- `ExecutionContext` 注入 store / command_bus / prompt_manager / llm_adapter / send_adapter——executor 不依赖插件实例。
- 分级判定保留，结果只决定选哪个 executor。

---

### 模块 6：i18n 落点

| 优先级 | 内容 | 方案 |
|---|---|---|
| P0 | LLM prompt 模板 | PromptManager 语言目录（模块 1） |
| P1 | 用户可见文案：命令回复、ask_user 前缀 | `I18n.t(key)` 翻译表（`locale/zh.py`、`locale/en.py`） |
| P1 | 状态描述 `format_status()`（"已运行 3 分钟"） | 从 TaskRecord 剥离，改 `LocaleStatusFormatter(lang, now)` |
| P2 | 日志文案 | 不做（日志英文，开发向） |

```python
class I18n:
    def __init__(self, lang: str = "zh") -> None: ...
    def t(self, key: str, **kwargs: Any) -> str: ...
    # 与 PromptManager 共用同一 lang 来源
```

关键点：`TaskRecord.format_status()` 删除，返回结构化 `(status, relevant_timestamp)`；本地化文本是展示层职责。

---

## 5. 实施路线图

| 阶段 | 内容 | 验证标准 |
|---|---|---|
| **P0 止血** | ① 修 O(n²) 历史恢复（只取最后一轮快照）② 修 ask_user 序列化炸弹（`_resume_event` 挪出 metadata）③ 删死代码（commands.py 与 plugin.py `_cmd_*` 收敛到一处；删 `_inject_event`）④ 引入真 sqlite 集成测试（conftest 加 RealStoreFixture；mock 边界：mock LLM/transport，不 mock 持久化） | 真 sqlite 下 ask_user 全链路通过；pytest 全绿 |
| **P1 PromptManager + i18n** | 建模板目录 + PromptManager；迁移 4 条 prompt 路径；补 title.md 并接通标题生成；状态格式化本地化 | 改 prompt 只改文件不改代码；中英切换验证 |
| **P2 模型拆分** | TaskRecord/TaskRuntime 拆分 + schema_version 迁移；状态机私有化 + force 收口；恢复逻辑并入 TaskRecovery；history 增量存储 | `json.dumps(record)` 全字段可序列化测试；状态转换审计日志 |
| **P3 命令总线** | TaskCommand/TaskEvent 协议 + LoopbackTransport + TaskCommandBus；替换 `_loops` 静态表和 metadata 通信 | 注入/回复/暂停全走总线；全量回归 |
| **P4 子进程派遣**（未来） | StdioTransport + SubprocessAgentExecutor + AgentWorker（复用 AgentLoop，只换 ctx）；supervisor 监控（worker 崩溃 → 任务 FAILED + 通知 owner） | worker 崩溃不丢任务；命令注入跨进程生效 |

**迁移顺序原则**：先抽象后拆解；每阶段独立合入，不做大爆炸式重构。

---

## 6. 风险与边界

1. **DB 迁移**：`tasks.data` 是 JSON 列，加 `schema_version`；`from_dict` 按版本兼容读取（先读旧格式，写时升版）。
2. **测试 mock 边界**：mock 掉 LLM、transport（真外部依赖），**绝不 mock 持久化和序列化**（真实约束；ask_user bug 即 mock 掉的教训）。`FakeTaskStore` 在 P0 退役。
3. **MaiBot SDK 边界**：`ctx.*` 封装为 `LLMAdapter` / `SendAdapter` / `MessageAdapter` 薄层——子进程 worker 只替换这三个 adapter。
4. **每阶段结束跑全量测试**，P0-P3 独立合入。

---

## 7. 审查重点（供评审者）

1. 模块 4（命令总线）是否足以支撑子进程模式，接口是否完整、不过度。
2. 模块 2 的 TaskRecord/TaskRuntime 拆分是否消除序列化炸弹且不过度设计。
3. P0-P4 阶段划分是否可独立验证、依赖顺序是否合理。
4. i18n 范围（P0-P2）是否务实，有无遗漏的高频用户可见文案。
5. 迁移风险（DB 兼容、测试 mock 边界）是否有遗漏。
