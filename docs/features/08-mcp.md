# MCP 工具集成

## 设计目标（为什么自带 MCP 客户端？接入外部工具生态）

Agent 循环的工具体系（`tools/`，见 [工具系统](./05-tools.md)）只内置了文件读写、搜索、
消息发送这几类能力。真实场景里任务需要的工具远不止这些：读文件系统、查 GitHub、操作数据库、
调外部 API。每一个都自己实现一遍不现实，所以插件选择接入 **MCP（Model Context Protocol）**
生态，这个开放协议已经有大量现成的服务器实现，配置即用。

自带精简 MCP 客户端（`tools/mcp/`）而不是依赖第三方 SDK，是为了把依赖面控制在标准库内：
`MCPConnection` 仅用 asyncio / json / subprocess / ssl 实现协议核心，零第三方运行时依赖
（`tools/mcp/connection.py:1-24` 模块文档明确范围）。

> 迁移澄清：本文的 **stdio / 子进程** 是 **MCP 客户端协议** 术语，指 `MCPConnection`
> 通过 `asyncio.create_subprocess_exec` 启动 MCP 服务器子进程并与其 stdin/stdout 通信
> （`connection.py:473`）。这与 v0.1.0 已删除的插件 Worker 子进程架构（WorkerManager +
> StdioTransport）毫无关系，后者是插件自身的任务执行进程化方案，已回退为进程内
> contextvars + usecase 分层。MCP 的 stdio 传输完整保留，请勿混淆。

## 设计方案

### MCPConnection：单服务器连接，三协议统一抽象

`MCPConnection`（`tools/mcp/connection.py:285`）管理单个 MCP 服务器的完整生命周期：
连接、握手、工具发现、工具调用、关闭。`connect()`（`connection.py:337`）按配置的
`transport` 分发到三种传输实现：

- **stdio**：`_connect_stdio()`（`connection.py:473`）启动配置的 command + args 子进程，
  经 stdin/stdout 通信，使用 LSP 风格帧格式（`Content-Length: N\r\n\r\n{json}`，
  `connection.py:580-582`）。这是本地 MCP 服务器最常用的传输方式。
- **http / sse**：`_HttpTransport`（`connection.py:89`）每次请求新建 TCP/TLS 连接，发送手工
  构造的 HTTP/1.1 POST 请求。http 与 sse 共用同一套代码路径，仅凭 `Content-Type` 响应头区分
  JSON 与 SSE 格式（`connection.py:182-185`）。SSE 只提取同步响应的最后一条 `data:` 行，
  不支持服务端主动推送。通知按 MCP 规范发后即忘，服务器返回 202 空响应体时不会解析
  （容忍空体 2xx）。

协议常量统一在 `connection.py:42-45`：`_PROTOCOL_VERSION = "2024-11-05"`、客户端标识
`oh-mai-agent`、读取缓冲 `_READ_CHUNK = 8192`。所有 JSON-RPC 请求经 `_send_request`
（`connection.py:516`）串行化发送（`asyncio.Lock` 防 stdio 帧交错），超时默认 30 秒。

### MCPManager：多服务器编排与工具路由

`MCPManager`（`tools/mcp/provider.py:28`）负责连接编排。`start()`（`provider.py:54`）遍历
`resolve_effective_servers`（`tools/mcp/presets.py`）的解析结果，对每个服务器依次 `connect()`
→ `initialize()` 握手（声明 `capabilities: {"tools": {}}`）→ `list_tools()` 发现工具。容错
设计：单个服务器连接或初始化失败只关闭该连接并跳过，不影响其余服务器（`provider.py:72-80`）。

发现到的工具以扁平的 `{server, name, description, inputSchema}` dict 聚合（`provider.py:94-103`），
调用时经 `call_tool(server, name, arguments)`（`provider.py:141`）路由到对应服务器连接，返回
统一的 `{"success": ...}` 结构。

### 工具注册进 ToolRegistry，走 Agent 循环 discoverable 层

`build_tool_definitions()`（`provider.py:167`）把每条 MCP 工具转为 `ToolDefinition`：工具名加
`mcp_{server}_{tool}` 前缀避免与内置工具冲突，`visibility="discoverable"`、`min_role=Role.USER`
（`provider.py:180-204`）。也就是说 MCP 工具**不直接携带 schema 常驻**，Agent 经合成工具
`list_tools` / `get_tool_schema`（`tools/synthetic/discovery.py`）按需发现后调用，与所有
discoverable 层工具行为一致（详见 [工具系统](./05-tools.md) 的两级呈现模型）。

插件组装在 `lifecycle.py`：第 8 步（`lifecycle.py:122-129`）创建 `MCPManager`、启动、构建
`ToolDefinition` 并逐条 `registry.register(td)` 注册进 `ToolRegistry`。配置热更新时
`reload_mcp_if_changed`（`lifecycle.py:288-314`）比较新旧 `[mcp]` 配置，变更则停旧建新并
重新注册；注册新工具后调用 `unregister_stale_mcp_tools` 注销不再存在的 `mcp_*` 工具
（对应 `ToolRegistry.unregister` 能力）。

`MCPManager.start()` 有 15 秒整体超时上限（硬编码常量 `_STARTUP_TIMEOUT_S`）；单个服务器
另有 5 秒启动预算（`_PER_SERVER_STARTUP_TIMEOUT_S`，connect + initialize + list_tools
受其约束），坏服务器只浪费该预算即被跳过，不拖垮其余服务器。整体超时只关闭未完成握手的
连接，已初始化的服务器（如 exa）保留可用；stdio 服务器以 `-m <module>` 形态启动时，
模块缺失会被预检快速跳过（warning 日志含模块名与修复建议），避免 spawn 失败拖垮启动。

### 静态配置模型

MCP 是**纯静态配置**：生效服务器列表由内置预设与自定义条目共同决定，运行时不能动态增删
（`tools/mcp/provider.py:7` 明确声明）。`MCPConfig`（`config.py` 的 `MCPConfig`）含全局开关
`enabled`、`fetch_enabled` / `exa_enabled` 两个预设开关与自定义 `servers` 列表；每项
`MCPServerConfig`（`config.py` 的 `MCPServerConfig`）定义传输参数：`transport`
（`"stdio" | "http" | "sse"`）、stdio 用的 `command` / `args` / `env`、http/sse 用的
`url` / `headers`。

## 使用与配置

### 配置节 [mcp]

```toml
[mcp]
enabled = true
fetch_enabled = true
exa_enabled = true

[[mcp.servers]]
name = "filesystem"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/mcp-workspace"]

[[mcp.servers]]
name = "remote-api"
transport = "http"
url = "https://mcp.example.com/mcp"
headers = { Authorization = "Bearer sk-xxx" }
```

字段说明：`enabled = false` 时 `MCPManager.start()` 直接跳过，所有 MCP 工具不可见；`name`
为空或连接失败的服务器被跳过并记日志（`provider.py:63-80`）。`fetch_enabled` / `exa_enabled`
控制两个内置预设服务器是否启用，`servers` 默认空列表，仅在自定义追加时填写。完整配置项与
WebUI 表单见 [配置体系](./14-config.md)。注意 stdio 需要本机能执行对应命令（如 npx），
http/sse 需要网络可达目标 URL。

#### 内置预设

插件内置两个 MCP 服务器预设，开箱即用：

- **exa**：远程 web 搜索，http 传输，`https://mcp.exa.ai/mcp?tools=web_search_exa`。匿名可用
  （有限流），可通过 `x-api-key` 或 `Authorization: Bearer` 请求头提升额度。
- **fetch**：本地网页抓取，stdio 传输，经 `python -m mcp_server_fetch` 启动，Runner 自动安装
  依赖。默认遵守 robots.txt，env 自带 `PYTHONIOENCODING=utf-8`。

生效服务器列表由 `resolve_effective_servers`（`tools/mcp/presets.py`）按开关与去重规则组装：
exa 启用且用户列表无同名或同 URL 条目时加入，fetch 启用且用户列表无同名条目时加入，用户
自定义 `servers` 始终追加在后。存量配置若含旧 `websearch` 条目，因其 URL 与 exa 预设相同会
被自动去重，也可自行删除该条目。想完全自定义启动方式（如换 exa 的连接参数或改用其他搜索
服务），可关闭对应预设开关并在 `servers` 中自建条目；同名条目会替代预设连接。

### 使用路径

注册成功后，Agent 在循环中经 `list_tools` 看到 `mcp_filesystem_read_file` 这类条目，
`get_tool_schema` 取到完整参数 Schema，调用时 `ToolRegistry.execute` → `MCPManager.call_tool`
→ `MCPConnection.call_tool` → JSON-RPC `tools/call` 到 MCP 服务器（`connection.py:420`）。
外部工具由此获得与内置工具一致的发现、权限（USER 起）与调用体验。

### 已知限制

- **仅静态配置**：服务器增删只能改 `config.toml` 后热重载或重启，不支持运行时动态管理
  （`provider.py:7`）。
- **仅工具子集**：只支持 `tools/list` 与 `tools/call`，不支持 resources、prompts、sampling、
  roots（`connection.py:6-8`）。Agent 场景中这些能力已被插件自身的 [提示词系统](./12-prompt.md)
  覆盖。
- **tools/list 不分页**：`list_tools()`（`connection.py:403`）只取单次响应的全部结果，不处理
  `nextCursor` 分页；超大工具集的服务器只发现第一页。
- **http/sse 无连接复用**：每次 POST 新建 TCP/TLS 连接（`connection.py:89-104`），无连接池、
  无 keep-alive；SSE 不支持服务端推送。对低频工具调用足够，高频场景有额外延迟。
- **stdio 无自动重启**：MCP 服务器子进程异常退出后不会自动拉起，后续调用报
  `RuntimeError`。关闭时 `close()`（`connection.py:448`）先 `terminate()`，3 秒未退出则
  `kill()`。
- **协议参数硬编码**：协议版本、读取缓冲、30 秒超时等以常量写死（`connection.py:42-45`），
  未暴露到配置。整体启动上限 15s（`_STARTUP_TIMEOUT_S`）、每服务器启动预算 5s
  （`_PER_SERVER_STARTUP_TIMEOUT_S`）与单请求 30s 均为硬编码常量，
  未暴露到配置；stdio 预检仅对当前解释器的 `-m` 模块精确（自定义其他解释器的服务器不
  保证精确）。
- **预设可被同名条目覆盖**：用户 `servers` 列表中与预设同名（`exa` / `fetch`）的条目会替代
  预设连接，需注意配置一致性。
- **stdio 帧格式曾与 MCP Python SDK <2.0 不匹配（已修复）**：插件自研 stdio 客户端曾按
  LSP 风格 `Content-Length` 帧收发，而 MCP Python SDK 1.x 的 stdio 服务器默认
  newline-delimited JSON（每行一条 JSON-RPC 消息），双方无法完成 initialize 握手，
  内置 fetch 预设因此每次启动超时（5s）被跳过。现已修复：发送侧改用 newline 帧
  （`MCPConnection._send_raw`），读取侧兼容两种格式
  （`MCPConnection._read_stdio_response`）。`_manifest.json` 的 `dependencies` 显式声明
  `mcp>=1.1.3,<2.0.0` 固定宿主兼容区间（MaiBot 本体对 `mcp` 无版本约束，此声明不冲突、
  不阻止加载），防止宿主环境将 `mcp` 升级到 2.x。**MaiBot 本体 MCP 模块
  （`src.mcp_module`，官方 SDK 客户端）自始不受此问题影响，工作正常**。残余限制：
  发送侧固定 newline 帧，若宿主环境切换到 mcp SDK 2.0（Content-Length 帧服务器），
  发送侧需同步适配。
