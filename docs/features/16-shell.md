# 命令执行（Shell）

本文档讲述 oh-mai-agent 的宿主机命令执行能力：Agent 循环如何在 Windows 与 Linux/macOS 上以同一套工具语义执行 shell 命令，以及为什么这个工具只对 admin 开放、如何防超时残留。

## 设计目标（为什么需要命令执行？）

Agent 级任务跑在离线循环里，之前触达宿主机只有两条路：`read` / `write` 文件读写（user 级被沙箱隔离到 `data_dir/files/`）与 MCP 工具（受服务器能力限制）。遇到「检查磁盘占用、看进程状态、跑一段脚本、调用本机 CLI 工具」这类需求，Agent 只能干瞪眼。

命令执行工具要解决的就是这个问题：**把宿主机的完整计算能力交给 Agent**——`tools/agent/shell_tools.py` 的 `run_command` 在宿主机上执行任意 shell 命令并把 stdout / stderr / 退出码收回来，让 Agent 像在终端里干活一样完成任务。

这个能力天然是双刃剑，所以设计上有三条硬约束：

1. **只给 admin**。命令执行 = 任意代码执行，guest / user 在 schema 呈现阶段就不可见、执行阶段被双重门控（详见「安全模型」）。
2. **跨平台零特判**。Windows 用 `cmd.exe`、POSIX 用 `/bin/sh`，shell 选择在运行时自动完成，Agent 按本地终端的语法写命令即可，不需要感知宿主平台。
3. **有界运行**。超时强杀整棵进程树、输出按字符数截断——命令执行不能无限占用 Runner 的线程池，也不能把几十 MB 的输出塞回 LLM 上下文。

## 设计方案

### 跨平台执行：交给平台默认 shell

`build_shell_tools` 工厂（`tools/agent/shell_tools.py`）产出唯一工具 `run_command`（discoverable / `min_role=Role.ADMIN`），handler 经 `asyncio.to_thread` 把同步执行放进线程池，不阻塞事件循环。

执行本身只做一件事：`subprocess.Popen(command, shell=True)`——**shell 的选择完全交给 Python 的平台默认**：Windows 上按 `COMSPEC` 定位 `cmd.exe`，POSIX 上使用 `/bin/sh`。这样管道、重定向、环境变量、`&&` / `||` 等语法在两端语义一致，Agent 无需按平台特判命令写法（Windows 上写 `dir` / `type`，Linux 上写 `ls` / `cat`，各自按本地习惯来即可）。

平台差异只在进程管理参数上：

- Windows：`creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`——新进程组便于枚举进程树，无控制台窗口避免后台服务场景弹窗。
- POSIX：`start_new_session=True`——shell 成为新会话/进程组组长，超时后可整组强杀。

stdin 一律接 `DEVNULL`（命令不能从终端读输入），stdout / stderr 走管道回收。

### 超时与进程树强杀

`communicate(timeout=N)` 超时抛 `TimeoutExpired` 时只杀了**直接子进程**（cmd / sh），其派生的孙进程会残留——`_kill_process_tree`（`tools/agent/shell_tools.py`）补杀整棵进程树：

- POSIX：`os.killpg(os.getpgid(pid), SIGKILL)`（依赖 `start_new_session` 的进程组组长身份）。
- Windows：`taskkill /F /T /PID <pid>` 递归终止整棵进程树。

超时结果以 `{"success": False, "timed_out": True, "error": "命令执行超时（N 秒），已强制终止进程树"}` 返回，同时带回已回收的 stdout / stderr 片段与退出码，方便 Agent 判断卡在哪一步。

### 输出解码与截断

输出解码先按 UTF-8 严格解码，失败回退到 `locale.getpreferredencoding(False)`（`errors="replace"` 兜底）——Windows 下 cmd 常以本地 codepage（如 GBK）输出中文，直接 UTF-8 解码会得到乱码。stdout / stderr 各自按 `config.shell.max_output_chars`（默认 8000）截断，截断时返回 `truncated: true` 与 note，防止输出撑爆 LLM 上下文。

### 安全模型（三道防线）

与文件工具同构：

1. **呈现层**：`min_role=Role.ADMIN`，guest / user 在 `registry.names(role)` / `list_discoverable(role)` 中直接不可见，`get_tool_schema` 发现阶段也被角色过滤拒绝。
2. **执行层**：`registry.execute` 执行前二次门控，权限不足返回 `permission denied`；handler 内部再经 `role_provider`（绑定 `current_task` ContextVar 的实时角色，`TaskManager._current_task_role()`）第三道校验——攻击者无法伪造角色。
3. **子 Agent 隔离**：`run_command` 被加入 `_SUBAGENT_EXCLUDED`（`tools/agent/subagent_tool.py`），子 Agent 的默认允许集与显式 tools 参数都拿不到它——宿主机命令执行是主 Agent（即 admin 本人）的专属能力，不允许通过子 Agent 间接触发。

### 配置热更新

handler 每次调用都执行 `cfg = config_getter()`（闭包内是 `lambda: self._config.shell` 引用，不缓存配置快照），`TaskManager.update_config()` 后 `timeout_seconds` / `max_output_chars` 修改立即生效，无需重注册。

## 使用与配置

### [shell] 配置节

配置键位于 `config.py` 的 `ShellConfig`（`MaibotAgentConfig.shell`，`__ui_label__="Shell命令"`）：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | `bool` | `true` | 是否注册 run_command 工具；`false` 时 `TaskManager.setup()` 不注册 |
| `timeout_seconds` | `int` | `60` | 命令默认超时（秒，`ge=1`）；超时后强制终止整个进程树 |
| `max_output_chars` | `int` | `8000` | stdout/stderr 单侧最大返回字符数（`ge=100`），超长截断 |

### 工具参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `command` | `string` | 是 | 命令字符串（支持管道、重定向、环境变量等 shell 语法） |
| `timeout_seconds` | `integer` | 否 | 单次调用超时覆盖（正整数），缺省取配置默认值 |
| `cwd` | `string` | 否 | 命令工作目录（绝对路径），缺省为插件进程当前目录；不存在或非目录则拒绝 |

### 返回结构

```json
{
  "success": true,
  "command": "...",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "timed_out": false,
  "truncated": false,
  "duration_seconds": 0.123
}
```

约定：`exit_code` 非 0 或 `stderr` 有内容**不代表工具失败**（`success` 仍为 true）——退出码是命令的执行结果，由 Agent 结合输出判断是否达成目标；只有超时、启动失败、参数非法、权限不足时 `success` 才为 false。

### 使用方式

由 Agent 循环自动触发：某轮 LLM 返回 `run_command(command="...")` 工具调用 → handler 线程池执行 → stdout / stderr / 退出码作为工具结果交回主 Agent 上下文 → 下一轮据此继续判断。主 Planner 的 11 个 @Tool 安全子集**不含** run_command——命令执行只存在于 Agent 循环的 Discoverable 层。

### 已知限制与边界

- **无白名单机制**。admin 可执行任意命令；若需限制命令范围，靠部署侧（系统权限、容器隔离）而非插件配置。
- **受提示词注入影响（本工具最主要的风险面）**。命令由 LLM 生成，Agent 的上下文若被不可信内容污染（抓取的网页、外部消息、注入的指令），可能被诱导执行非预期的命令。三道 admin 门控挡的是「非 admin 调用者」，挡不住「admin 授权的 Agent 被提示词注入诱导」——提示词注入发生在 LLM 内部，工具层无法区分。缩小 blast radius 只能靠部署侧：容器隔离、受限 PATH、只读挂载、最小化运行账号。
- **非交互**。stdin 接 DEVNULL，交互式命令（如 `ssh` 密码提示、`vi`）无法工作；需要交互的程序请用非交互参数（如 `ssh -o BatchMode=yes`）。
- **默认工作目录**。缺省 `cwd` 时命令在插件 Runner 进程的当前目录执行，与宿主 MaiBot 的启动目录一致。
- **命令执行是主 Agent 专属**。子 Agent 不可见（`_SUBAGENT_EXCLUDED`），也不暴露给主 Planner。

### 关联文档

- [工具系统](05-tools.md)：两级呈现、注册顺序与角色过滤。
- [权限模型](04-permission.md)：admin 角色判定与工具门控。
- [子 Agent](15-subagent.md)：`_SUBAGENT_EXCLUDED` 与子 Agent 工具集规则。
