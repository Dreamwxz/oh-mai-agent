# MCP 使用指南

本页是 **MCP（Model Context Protocol）工具**的用户向使用手册：怎么开箱即用、怎么配置
自定义服务器、怎么让 Agent 调用外部工具。实现细节见 [MCP 工具集成](../features/08-mcp.md)，
完整配置项见 [配置体系](../features/14-config.md)。

## MCP 是什么，为什么需要它

MCP 是开放的工具接入协议，生态里已有大量现成服务器实现：网页抓取、搜索、文件系统、
数据库、GitHub 等。插件自带精简 MCP 客户端，Agent 任务可以直接调用这些外部工具——
**不用为每个能力单独开发，配置即用**。

插件内置两个 MCP 服务器预设，**默认全部启用，开箱即用**：

| 预设 | 能力 | 传输 | 说明 |
|---|---|---|---|
| **exa** | 远程 web 搜索 | http | `https://mcp.exa.ai/mcp`，匿名可用（有限流） |
| **fetch** | 本地网页抓取 | stdio | `python -m mcp_server_fetch` 启动，依赖由 Runner 自动安装，默认遵守 robots.txt |

也就是说：装上插件后，直接吩咐 Agent「帮我搜索一下……」「抓取这个网页的内容……」
就能用，无需任何 MCP 配置。

## 在聊天里怎么用

Agent 看到的是真实工具名，格式为 `mcp_<服务器名>_<工具名>`，例如：

- `mcp_fetch_fetch` —— 抓取网页内容
- `mcp_exa_web_search_exa` —— web 搜索

用法就是**自然语言吩咐**：

```
/maitask create 抓取 https://example.com 的首页，总结主要功能
/maitask create 用 web 搜索查一下 "MCP 最佳实践"，整理要点
```

主 Planner 侧另有 `list_mcp_tools` / `call_mcp_tool` 两个工具用于发现与调用
（`list_mcp_tools` 查看当前有哪些 MCP 工具可用，`call_mcp_tool` 直接调用）。

## 配置自定义 MCP 服务器

编辑 MaiBot 的 `config.toml` 中插件的 `[mcp]` 节（或用 WebUI 表单编辑）。改动后
**配置热更新即可生效**，无需重启插件（MCP 配置变更会自动重连）。

```toml
[mcp]
enabled = true                 # 总开关；false 时所有 MCP 工具不可见
fetch_enabled = true           # 内置 fetch 网页抓取预设
fetch_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
exa_enabled = true             # 内置 exa 搜索预设

# 自定义服务器：stdio（本地命令）
[[mcp.servers]]
name = "filesystem"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/mcp-workspace"]
env = { LANG = "en_US.UTF-8" }

# 自定义服务器：http（远程）
[[mcp.servers]]
name = "remote-api"
transport = "http"
url = "https://mcp.example.com/mcp"
headers = { Authorization = "Bearer sk-xxx" }
```

### 字段速查

| 字段 | 说明 |
|---|---|
| `name` | 服务器名称（工具名前缀 `mcp_<name>_` 用） |
| `transport` | `"stdio"`（本地子进程）\| `"http"` \| `"sse"`（远程） |
| `command` / `args` / `env` | stdio 传输：启动命令、参数、环境变量 |
| `url` / `headers` | http/sse 传输：服务器 URL、请求头 |

### 常见配置场景

- **给 exa 提额度**：exa 匿名有限流，在 `servers` 里加同名条目覆盖预设并带密钥：

  ```toml
  [[mcp.servers]]
  name = "exa"
  transport = "http"
  url = "https://mcp.exa.ai/mcp?tools=web_search_exa"
  headers = { "x-api-key" = "你的 exa API key" }
  ```

  同名（或同 URL）条目会**替代预设连接**，无需关闭 `exa_enabled`。

- **关掉某个预设**：`fetch_enabled = false` 或 `exa_enabled = false`，完全自定义时
  可全部关闭、只用 `servers` 自建。

- **本地文件系统工具**：上方 filesystem 示例即可（需要本机能执行 `npx`）。

- **反爬站点的 UA 伪装**：内置 fetch 默认携带浏览器 UA（`fetch_user_agent`），
  `mcp-server-fetch` 自带的 bot UA 会被 B 站等站点直接命中验证码。如站点有变化
  可自行更换 UA；留空则退回服务器默认 UA。

## 注意事项

- **静态配置**：服务器增删只能改配置后热重载/重启，不支持运行时动态管理。
- **连接失败不影响整体**：某个服务器连不上只跳过它自己并记日志，其余服务器照常工作；
  工具调用超时上限 30 秒（硬编码）。
- **权限**：MCP 工具 `min_role` 为 user，所有登录用户的任务都能用；角色过滤见
  [权限模型](../features/04-permission.md)。
- **支持范围**：只支持 `tools/list` 与 `tools/call`（MCP 的 resources / prompts /
  sampling 不在支持范围）。
- **stdio 需本机可执行**：如 `npx`、`python -m` 等命令要在 MaiBot 运行环境可用。
- **故障排查**：日志中搜索 `mcp` 相关 warning（连接失败、模块缺失会给出修复建议）；
  确认工具是否注册成功可用 `list_mcp_tools`。
