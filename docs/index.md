# oh-mai-agent

> MaiBot 离线多线程 Agent 插件：任务分级管理、定时调度、MCP 工具集成与跨插件协作。
> 离线也能安心把事办妥～

oh-mai-agent（插件 ID `oh-mai-agent`）把 MaiBot 主流程中不适合实时承载的复杂任务剥离出来，
放进独立的 Runner 进程离线执行：任务创建、调度、执行与持久化全部在后台完成，不阻塞 Host 主流程。

## 功能特性

| 能力 | 说明 |
|---|---|
| **任务分级管理** | `instant`（即时动作，单次执行）与 `agent`（长时自主循环，最多 30 轮 LLM 交互）两级任务，共享同一数据模型与 8 态状态机 |
| **定时调度** | cron 表达式与延迟触发（`delay_seconds` / `cron_expr`），到点自动入队 |
| **MCP 工具集成** | stdio / http / sse 三种连接方式，内置 fetch / exa 预设，Agent 循环可直接调用外部能力 |
| **跨插件协作** | 6 个动态 API 端点，其他插件可创建 / 查询 / 取消任务、注入指令、查看历史 |
| **权限模型** | guest / user / admin 三级角色，文件工具二次经沙箱校验 |
| **人机交互** | `ask_user` 挂起等待用户输入、向运行中任务注入新指令、Planner 待办看板 |
| **Shell 命令执行** | 内置命令执行工具，离线任务可调用外部命令 |
| **持久化与恢复** | sqlite 任务持久化，崩溃后自动恢复未完成任务 |
| **回复润色与发送** | 黑话匹配润色、长文自动拆分发送，回复可跨流送达 |

## 快速开始

### 安装

1. 环境要求：Python ≥ 3.10，MaiBot ≥ 1.0.0，插件 SDK ≥ 2.6.0。
2. 将本仓库克隆或复制到 MaiBot 的 `plugins/` 目录下（保持目录名 `oh-mai-agent`）。
3. 重启 MaiBot。插件加载后自动安装依赖（croniter / jinja2 / mcp / mcp-server-fetch）。
4. 验证：在聊天中发送 `/maitask help`（或 `/mt help`）应返回命令帮助。

### 配置速查

配置集中在 `config.py`（13 个配置节，运行时经 MaiBot 配置系统读取，强类型校验）：

| 配置节 | 用途 |
|---|---|
| PluginSection | 插件基本信息 |
| PermissionConfig | 管理员 / 用户白名单（按人或按群） |
| TaskConfig | 并发上限、运行时长兜底、提问等待超时、历史持久化 |
| PlannerBoardConfig | Planner 待办看板开关与条数上限 |
| PolishConfig | 回复润色（黑话匹配） |
| SplitterConfig | 长回复分割（单条长度 / 最多条数） |
| SendConfig | 发送重试次数与退避 |
| MCPServerConfig / MCPConfig | MCP 服务器清单与连接配置 |
| SearchConfig | 用户搜索 |
| SubAgentConfig | 子 Agent 执行参数 |
| ShellConfig | Shell 命令执行参数 |

各字段含义与默认值详见 [配置体系](features/14-config.md)。

### 命令用法

`/maitask` 命令组（别名 `/mt`），面向使用者的完整手册见
[命令使用指南（Command）](guide/commands.md)：

| 命令 | 作用 |
|---|---|
| `/maitask create <意图>` | 创建任务（用自然语言描述要做什么） |
| `/maitask list [-all] [状态]` | 列出任务（支持按状态过滤，`waiting_input` 查看在等你的任务） |
| `/maitask status <任务ID>` | 查看任务详情（支持前 8 位短 ID） |
| `/maitask cancel <任务ID>` | 取消任务 |
| `/maitask history <任务ID>` | 查看任务执行历史 |
| `/maitask ask <任务ID> <指令>` | 回答挂起中的提问 / 向任务注入新指令 |
| `/maitask help` | 帮助（兜底拦截所有未匹配的 `/maitask` 输入） |

### MCP 工具（开箱即用）

插件内置 **exa 搜索**与 **fetch 网页抓取**两个 MCP 服务器预设，默认启用：
装上插件后直接吩咐 Agent「搜索……」「抓取这个网页……」即可使用，无需配置。
自定义服务器（stdio / http / sse）与常见配置场景见 [MCP 使用指南](guide/mcp.md)。

### 给主 Planner 的工具

插件向 MaiBot 主 Planner 暴露 11 个安全工具（@Tool），主 Agent 循环中还有按角色过滤的
Discoverable 工具层（MCP 工具、文件读写、跨插件 API 等），详见 [工具系统](features/05-tools.md)：

`search_users` · `subagent_create` · `subagent_list` · `subagent_status` ·
`subagent_modify` · `subagent_delete` · `subagent_history` · `subagent_schedule` ·
`send_message` · `list_mcp_tools` · `call_mcp_tool`

## 文档地图

| 文档 | 内容 |
|---|---|
| [命令使用指南](guide/commands.md) | `/maitask` 命令组的用户手册：语法、参数、示例、典型流程 |
| [MCP 使用指南](guide/mcp.md) | MCP 工具的用户手册：内置预设、自定义服务器、常见配置 |
| [生命周期总览](LIFECYCLE.md) | 任务 / 插件 / Agent 循环 / 回复路径四大生命周期如何串联 |
| [功能文档](features/01-task-model.md) | 16 篇，按主题深入每个子系统（任务模型、调度器、持久化、权限、工具、润色、MCP、看板、跨插件 API、命令总线、提示词、命令、配置、子 Agent、Shell） |
| [提示词写作规范](prompt-style-guide.md) | 提示词系统的写作风格与纪律 |
| [历史归档](history/DESIGN.md) | 早期设计 / 实施 / 重构计划（历史快照，仅存档） |

## 开发者

- **开发速查**：[AGENTS.md](https://github.com/Dreamwxz/oh-mai-agent/blob/main/AGENTS.md)（架构速览、目录导航、开发约定、已知限制）
- **相关资源**：MaiBot 插件开发文档（`MaiBot_docs/zh/plugin/`）、[maibot-plugin-sdk](https://github.com/Dreamwxz/maibot-plugin-sdk)、MaiBot 本体源码
- **测试**：`uv run --with maibot-plugin-sdk pytest tests/ -q`（999+ 测试函数，0 失败）
- **站点构建**：文档站由 [mkdocs.yml](https://github.com/Dreamwxz/oh-mai-agent/blob/main/mkdocs.yml) 配置，
  GitHub Actions（`.github/workflows/docs.yml`）在推送到 `main` 时自动构建并部署到 GitHub Pages。

## License

GPL-3.0-or-later
