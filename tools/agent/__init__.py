"""Agent 循环工具通道子包 —— 供 AgentLoop 在任务执行中调用的工具集合。

包含 Agent 循环内可用的工具工厂：信息获取（info_tools）、文件读写
（file_tools）、向用户提问（ask_tool）与跨插件 API 动态工具
（plugin_api_tools）。发送消息工具（send_message）为 Agent 循环与
Planner 共用实现，位于 ``tools/send_message.py``。
"""
