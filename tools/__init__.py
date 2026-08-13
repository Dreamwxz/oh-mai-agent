"""oh-mai-agent 工具系统 —— 注册中心、两级呈现与权限过滤。

两级模型（受 oh-my-pi xdev 启发）：
  - Essential：始终携带的工具 schema（数量受控，节省 token）。
  - Discoverable：按需发现，Agent 通过 list_tools / get_tool_schema 获取。

工具在**呈现**（列出 schema）与**执行**两个阶段均按调用者角色
（guest / user / admin）过滤。

子包：
  - agent/      Agent 循环内工具（info_tools / file_tools / ask_tool / send_tool / plugin_api_tools / task_mgmt）
  - planner/    暴露给主 Planner 的 @Tool handler 工厂（search_users / task_tools / send_message）
  - synthetic/  Agent 循环合成发现工具（list_tools / get_tool_schema）
  - mcp/        MCP 工具提供方（connection 协议客户端 + provider 管理器）
"""
