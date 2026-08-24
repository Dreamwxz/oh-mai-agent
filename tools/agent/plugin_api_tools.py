"""跨插件 API 工具 — 将其他插件暴露的 API 动态转换为 Agent 的 Discoverable 级工具。

扫描 ``ctx.api.list()`` 并将**显式允许名单**内的 API 包装为 ``call_{api_name}``
工具（API 名中的 ``.`` 替换为 ``_``），参数通过松散的 ``args`` 对象传入。
task_manager 在工具注册时调用 ``refresh_plugin_api_tools()`` 以保持注册表同步。

权限模型（相对历史版本收紧，见 docs/features/04-permission.md）：
  - **显式允许名单**：仅包装 ``allowed_apis`` 名单内的 API；缺省名单为空 ——
    默认**不暴露任何** call_* 工具（不再自动全量包装）。
  - **最低角色 ADMIN**：包装出的工具固定 ``min_role=Role.ADMIN``，guest / user
    级 Agent 不可见、不可调用。跨插件 API 底层按受信任插件以 ADMIN 执行，
    任何低于 ADMIN 的包装都会让低权限 Agent 以管理员身份触达远端端点。
  - **自身端点排除**：本插件 6 个任务端点（create / list / get / cancel /
    inject / history）永远不包装为 call_* 工具 —— Agent 操作任务一律走原生
    owner 隔离工具（list_my_tasks / create_subtask / inject_task / cancel_task）。
  - **身份键剥离**：handler 透传前剥离保留身份参数（``_caller_role`` 等），
    防止经工具通道伪造调用方身份（见 api_expose.py 的身份契约）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

from ...permission import Role
from ..registry import ToolDefinition

#: 本插件在 MaiBot 中的插件 ID（与 _manifest.json 的 "id" 字段一致）。
PLUGIN_ID = "github.dreamwxz.oh-mai-agent"

#: call_* 工具的最低角色 —— 固定为 ADMIN，不可低于。
MIN_API_TOOL_ROLE = Role.ADMIN

#: 本插件自身端点短名 —— 永远不包装为 call_* 工具（原生工具已按 owner 隔离）。
OWN_API_NAMES: frozenset[str] = frozenset({
    "create", "list", "get", "cancel", "inject", "history",
})

#: 保留身份参数键（api_expose.py 的调用方身份契约，禁止作为普通 API 参数透传）。
_CALLER_IDENTITY_KEYS: tuple[str, ...] = (
    "_caller_role", "_caller_owner", "_caller_stream_id",
)


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


def _is_own_api(api: dict[str, Any]) -> bool:
    """判断 API 条目是否属于本插件自身端点。

    Host 序列化的条目带 ``plugin_id`` 字段（精确判定）；mock / 旧形态条目
    无该字段时按端点短名精确匹配，或按完整名（``plugin_id.api_name``）前缀
    匹配。不做模糊后缀匹配——避免把 ``weather.get`` / ``news.list`` 之类
    恰好以 get/list 结尾的外部 API 误判为本插件端点。
    """
    plugin_id = str(api.get("plugin_id", "") or "")
    if plugin_id:
        return plugin_id == PLUGIN_ID
    name = str(api.get("api_name") or api.get("name") or "")
    return name in OWN_API_NAMES or name.startswith(f"{PLUGIN_ID}.")


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
        # 剥离保留身份参数：身份契约键只允许由受信任通道注入
        # （api_expose.py 的 handler 校验），LLM/工具参数不得伪造。
        for key in _CALLER_IDENTITY_KEYS:
            args.pop(key, None)
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
    *,
    allowed_apis: list[str] | None = None,
) -> list[ToolDefinition]:
    """重新扫描插件 API 并返回最新的 ToolDefinition 列表（异步版）。

    仅包装**显式允许名单**（``allowed_apis``）内的 API；缺省（None / 空列表）
    不暴露任何 call_* 工具。包装出的工具固定 ``min_role=Role.ADMIN``，且
    本插件自身 6 个任务端点（create / list / get / cancel / inject / history）
    永远排除——Agent 操作任务请用原生 owner 隔离工具。

    Args:
        ctx_api: ``ctx.api`` 对象，提供 ``.list()`` 和 ``.call()`` 方法。
        allowed_apis: 允许包装为 call_* 工具的插件 API 名列表
            （如 ``["weather.get"]``）；缺省为空名单，不暴露任何工具。

    Returns:
        ToolDefinition 列表，每个通过允许名单的插件 API 对应一条。
    """
    allow: set[str] = set(allowed_apis or [])
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
        # Host 序列化条目用 "name"（短名）/ "full_name"，mock 用 "api_name"；
        # 两种形态都兼容。
        api_name: str = str(api.get("api_name") or api.get("name") or "").strip()
        if not api_name:
            logger.warning("跳过缺少 api_name/name 的跨插件 API 条目")
            continue

        # 显式允许名单：名单之外的 API 一律不包装（默认名单为空 → 不暴露）。
        if api_name not in allow:
            logger.debug("插件 API %s 不在允许名单内，跳过包装", api_name)
            continue
        # 自身端点排除：本插件的任务端点永远不包装为 call_* 工具。
        if _is_own_api(api):
            logger.info(
                "插件 API %s 为本插件自身端点，跳过包装（请用原生任务工具）",
                api_name,
            )
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
                min_role=MIN_API_TOOL_ROLE,
            )
        )

    logger.info(
        "已生成 %d 个跨插件 API 工具（允许名单 %d 项，最低角色 %s）",
        len(tools), len(allow), MIN_API_TOOL_ROLE.value,
    )
    return tools
