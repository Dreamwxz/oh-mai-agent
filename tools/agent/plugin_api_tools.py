"""跨插件 API 工具 — 将其他插件暴露的 API 动态转换为 Agent 的 Discoverable 级工具。

扫描 ``ctx.api.list()`` 并将每个 API 包装为 ``call_{api_name}`` 工具
（API 名中的 ``.`` 替换为 ``_``），参数通过松散的 ``args`` 对象传入。
task_manager 在工具注册时调用 ``refresh_plugin_api_tools()`` 以保持注册表同步。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

from ...permission import Role
from ..registry import ToolDefinition


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


def _normalize_api_list(raw: Any) -> list[dict]:
    """将 ``ctx.api.list()`` 的返回值统一转换为 dict 列表。

    Host 可能直接返回列表，也可能返回包含 ``"apis"`` 键的 dict。
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and "apis" in raw:
        apis: Any = raw["apis"]
        if isinstance(apis, list):
            return apis
    return []


def _build_handler(api_name: str, ctx_api: object) -> Callable[..., Any]:
    """为指定 *api_name* 创建一个闭包 handler。"""

    async def _handler(**kwargs: Any) -> dict:
        args: Any = kwargs.get("args", {})
        if not isinstance(args, dict):
            # 容错：LLM 可能传错 args 类型。
            args = {}
        logger.debug("调用插件 API %s，参数：%s", api_name, str(args)[:80])
        try:
            # 动态调用远端插件端点：args 的键值按参数名展开透传。
            result: Any = await ctx_api.call(api_name, **args)  # type: ignore[attr-defined]
            logger.info("插件 API %s 调用成功", api_name)
            if isinstance(result, dict):
                # ctx.api.call() 已返回目标 API 的完整结果；
                # 直接透传，避免重复包装。
                return result
            return {"success": True, "result": result}
        except Exception as exc:
            # 异常不向上抛出，转为结构化失败结果，
            # 使 Agent 循环将其作为普通工具输出处理。
            logger.error("调用插件 API %s 失败：%s", api_name, str(exc)[:80])
            return {"success": False, "error": str(exc)}

    return _handler


# ── 公开 API ─────────────────────────────────────────────────────────────────


async def refresh_plugin_api_tools(
    ctx_api: object,
) -> list[ToolDefinition]:
    """重新扫描插件 API 并返回最新的 ToolDefinition 列表（异步版）。

    由 task_manager 在工具注册时调用，以保持工具注册表与插件当前
    暴露的 API 同步。

    Args:
        ctx_api: ``ctx.api`` 对象，提供 ``.list()`` 和 ``.call()`` 方法。

    Returns:
        ToolDefinition 列表，每个当前可见的插件 API 对应一条。
    """
    try:
        raw: Any = await ctx_api.list()  # type: ignore[attr-defined]
    except Exception:
        # 扫描失败不阻断工具注册，回退为空列表。
        logger.warning("跨插件 API 扫描失败，工具注册回退为空列表", exc_info=True)
        return []

    apis: list[dict] = _normalize_api_list(raw)
    tools: list[ToolDefinition] = []

    for api in apis:
        if not isinstance(api, dict):
            logger.warning("跳过无效的跨插件 API 条目：%s", str(api)[:80])
            continue
        api_name: str = api.get("api_name", "")
        if not api_name:
            logger.warning("跳过缺少 api_name 的跨插件 API 条目")
            continue

        desc: str = api.get("description", "")
        version: str = api.get("version", "")

        tool_name: str = f"call_{api_name.replace('.', '_')}"

        description: str = (
            f"调用插件 API：{api_name}。{desc}。"
            "参数通过 args 对象传入，args 中的键值对应 API 参数。"
        )
        if version:
            description += f" API 版本：{version}。"

        parameters: dict = {
            "type": "object",
            "properties": {
                "args": {
                    "type": "object",
                    "description": f"调用 {api_name} 所需的参数对象",
                },
            },
            "required": ["args"],
        }

        tools.append(
            ToolDefinition(
                name=tool_name,
                description=description,
                parameters=parameters,
                handler=_build_handler(api_name, ctx_api),
                visibility="discoverable",
                min_role=Role.USER,
            )
        )

    logger.info("已生成 %d 个跨插件 API 工具", len(tools))
    return tools
