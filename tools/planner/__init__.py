"""Planner 工具通道子包 — 暴露给主 Planner 的 @Tool handler 逻辑体工厂。

每个模块提供一个工厂函数，将 plugin.py 中 @Tool 装饰器下的 handler 逻辑
原样提取为可单独测试的 async callable，plugin.py 改为懒构建委托。
"""
