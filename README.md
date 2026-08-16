# oh-mai-agent

为 MaiBot 提供离线任务管理，支持即时与长时 Agent 任务、定时调度、MCP 工具集成与跨插件协作。离线也能安心把事办妥～

> 📚 完整文档站点（GitHub Pages，随 `main` 分支自动构建）：<https://Dreamwxz.github.io/oh-mai-agent/>

## 功能特性

- **任务分级管理**：`instant`（即时动作）/ `agent`（长时自主循环，最多 30 轮 LLM 交互）两级任务，8 态状态机
- **定时调度**：cron 表达式与延迟触发
- **MCP 工具集成**：stdio / http / sse 三种连接，内置 fetch / exa 预设
- **跨插件协作**：6 个动态 API 端点，其他插件可创建 / 查询 / 取消任务、注入指令
- **权限模型**：guest / user / admin 三级角色，文件工具二次沙箱校验
- **人机交互**：`ask_user` 挂起等待用户输入、注入指令、Planner 待办看板
- **Shell 命令执行**：内置命令执行工具
- **持久化与恢复**：sqlite 任务持久化、崩溃自动恢复
- **回复润色与发送**：黑话匹配润色、长文自动拆分、跨流送达

## 快速开始

1. 环境要求：Python ≥ 3.10，MaiBot ≥ 1.0.0，插件 SDK ≥ 2.6.0。
2. 将本仓库克隆或复制到 MaiBot 的 `plugins/` 目录（保持目录名 `oh-mai-agent`）。
3. 重启 MaiBot，插件加载后自动安装依赖。
4. 聊天中发送 `/maitask help`（或 `/mt help`）验证安装。

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/index.md](docs/index.md) | 文档站首页：快速开始、配置速查、命令用法、功能索引 |
| [docs/guide/commands.md](docs/guide/commands.md) | 命令使用指南：`/maitask` 命令组语法、参数、示例 |
| [docs/guide/mcp.md](docs/guide/mcp.md) | MCP 使用指南：内置预设、自定义服务器、常见配置 |
| [docs/LIFECYCLE.md](docs/LIFECYCLE.md) | 任务 / 插件 / Agent 循环 / 回复路径四大生命周期总览 |
| [docs/features/](docs/features/) | 16 篇功能文档（任务模型、调度器、持久化、权限、工具、MCP、配置……） |
| [docs/prompt-style-guide.md](docs/prompt-style-guide.md) | 提示词写作规范 |
| [docs/history/](docs/history/) | 历史归档（设计 / 实施 / 重构计划，仅存档） |
| [AGENTS.md](AGENTS.md) | 开发者速查：架构、目录导航、开发约定、已知限制 |

文档站由 MkDocs Material 构建（`mkdocs.yml`），GitHub Actions（`.github/workflows/docs.yml`）自动部署到 GitHub Pages；本地预览：`uv run --with mkdocs-material mkdocs serve`。

## License

GPL-3.0-or-later
