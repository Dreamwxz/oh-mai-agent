"""信息获取工具集。

本模块提供 ``build_info_tools(ctx)`` 工厂函数，将 MaiBot SDK 的上下文能力包装为
Agent 工具系统的 Discoverable 级 ``ToolDefinition`` 实例。

这些工具赋予 Agent 以下能力：
- 长期记忆检索（通过 ``knowledge.search``）
- 聊天历史拉取（通过 ``message.get_recent``）
- 人物画像近似查询（通过 ``person.get_id_by_name`` + 聚合搜索）
- 聊天流列表（通过 ``chat.get_all_streams``）
- 发言频率查询（通过 ``frequency.get_current_talk_value``）
- 插件工具发现（通过 ``tool.get_definitions``）

所有工具的 visibility 均为 ``"discoverable"``、min_role 均为 ``Role.GUEST``。
所有 handler 均遵循签名 ``async def handler(**kwargs) -> dict``，
并在 try/except 内执行，始终返回 ``{"success": bool, ...}`` 结构。
"""

from __future__ import annotations

import logging
from typing import Any

from .._shared import _filter_streams
from ..registry import ToolDefinition

logger = logging.getLogger(__name__)


# ── 工厂函数 ────────────────────────────────────────────────────────────────


def build_info_tools(ctx, search_max_results: int = 20) -> list[ToolDefinition]:
    """基于 PluginContext 构建信息获取工具定义列表。

    Args:
        ctx: MaiBot 的 ``PluginContext``（来自 ``maibot_sdk``）。
        search_max_results: search_users 返回条数上限。

    Returns:
        六个 ``ToolDefinition`` 实例，全部为 discoverable 级别、GUEST 可访问。
    """

    logger.debug("开始构建信息获取工具集，search_max_results=%d", search_max_results)

    # ── 1. search_memory：长期记忆检索 ─────────────────────────────────

    async def _search_memory(
        query: str,
        chat_id: str,
        limit: int = 5,
        mode: str = "hybrid",
        person_name: str = "",
        time_start: str = "",
        time_end: str = "",
        **kwargs: Any,
    ) -> dict:
        try:
            logger.debug(
                "search_memory 调用：query=%s, chat_id=%s, limit=%s, mode=%s",
                str(query)[:80], chat_id, limit, mode,
            )
            extra: dict[str, Any] = {}
            if person_name:
                extra["person_name"] = person_name
            if time_start:
                extra["time_start"] = time_start
            if time_end:
                extra["time_end"] = time_end
            result = await ctx.call_capability(
                "knowledge.search",
                query=query,
                limit=limit,
                mode=mode,
                chat_id=chat_id,
                **extra,
            )
            if isinstance(result, dict) and "success" in result:
                return result
            return {"success": True, "content": str(result)}
        except Exception as exc:
            logger.exception("search_memory 调用异常：%s", str(exc)[:80])
            return {"success": False, "error": str(exc)}

    # ── 2. fetch_history：聊天历史拉取 ─────────────────────────────────

    async def _fetch_history(
        chat_id: str,
        limit: int = 20,
        **kwargs: Any,
    ) -> dict:
        try:
            logger.debug("fetch_history 调用：chat_id=%s, limit=%s", chat_id, limit)
            messages = await ctx.message.get_recent(chat_id=chat_id, limit=limit)
            count = len(messages) if messages else 0
            return {"success": True, "messages": messages, "count": count}
        except Exception as exc:
            logger.exception("fetch_history 调用异常：%s", str(exc)[:80])
            return {"success": False, "error": str(exc)}

    # ── 3. query_person：人物信息查询 ─────────────────────────────────

    async def _query_person(
        person_name: str,
        **kwargs: Any,
    ) -> dict:
        try:
            logger.debug("query_person 调用：person_name=%s", str(person_name)[:80])
            pid_result = await ctx.person.get_id_by_name(person_name)
            # SDK 返回 person_id 字符串；同时兼容 dict-mock 形式。
            # 注意：真实宿主对查无此名返回空串 ""（success=True），必须判空；
            # 否则空 pid + 空 query 会被 knowledge.search 以「缺少必要参数 query」拒绝。
            if isinstance(pid_result, str):
                pid = pid_result
            elif isinstance(pid_result, dict) and pid_result.get("person_id"):
                pid = pid_result["person_id"]
            else:
                pid = ""
            if not pid:
                logger.warning("query_person 无法解析人物：%s", str(person_name)[:80])
                return {
                    "success": False,
                    "error": f"无法解析人物: {person_name}",
                }

            result = await ctx.call_capability(
                "knowledge.search",
                query=person_name,
                person_id=pid,
                mode="aggregate",
                limit=5,
                chat_id="",
            )
            if isinstance(result, dict) and "success" in result:
                return result
            return {"success": True, "person_id": pid, "profile": str(result)}
        except Exception as exc:
            logger.exception("query_person 调用异常：%s", str(exc)[:80])
            return {"success": False, "error": str(exc)}

    # ── 4. search_users：用户搜索 ────────────────────────────────────

    async def _search_users(
        keyword: str = "",
        keywords: list[str] | None = None,
        chat_type: str = "",
        platform: str = "all_platforms",
        **kwargs: Any,
    ) -> dict:
        try:
            # 兼容两种传参：单个 keyword，或 keywords 数组（OR 语义，合并去重）
            kws = list(dict.fromkeys(
                str(k).strip() for k in ([keyword] + (keywords or [])) if str(k or "").strip()
            ))
            logger.debug(
                "search_users 调用：keyword=%s, keywords=%s, chat_type=%s, platform=%s",
                str(keyword)[:80], [str(k)[:40] for k in kws], chat_type, platform,
            )
            streams = await ctx.chat.get_all_streams(platform=platform)
            filtered = _filter_streams(
                streams,
                keyword=keyword,
                keywords=keywords or [],
                chat_type=chat_type,
                max_results=search_max_results,
            )

            persons: list[dict] = []
            knowledge: list[dict] = []
            seen_persons: set[str] = set()
            seen_knowledge: set[str] = set()

            for kw in kws:
                # ── 人物画像查找（精确姓名匹配）───────────────
                try:
                    pid_result = await ctx.person.get_id_by_name(kw)
                    # 真实宿主对查无此名返回空串 ""，须判空避免假命中
                    if isinstance(pid_result, str) and pid_result:
                        pid = pid_result
                    elif isinstance(pid_result, dict) and pid_result.get("person_id"):
                        pid = pid_result["person_id"]
                    else:
                        pid = ""
                    if pid and pid not in seen_persons:
                        seen_persons.add(pid)
                        persons.append({"person_id": pid, "matched_by": "exact_name"})
                except Exception as exc:
                    logger.warning("人物画像查找失败，跳过（不影响流搜索）：%s", str(exc)[:80])
                    pass  # 人物查找失败不应中断 streams 流程

                # ── 记忆线索查找（混合检索）─────────────
                try:
                    k_result = await ctx.call_capability(
                        "knowledge.search",
                        query=kw,
                        limit=5,
                        mode="hybrid",
                    )
                    if isinstance(k_result, dict):
                        if k_result.get("success") is False:
                            pass  # 显式失败——跳过
                        else:
                            content = k_result.get("content", "")
                            if content and content != "你不太了解...":
                                snippet = str(content)[:300]
                                if snippet not in seen_knowledge:
                                    seen_knowledge.add(snippet)
                                    knowledge.append({
                                        "query": kw,
                                        "content": snippet,
                                    })
                    elif k_result:
                        content = str(k_result)
                        snippet = content[:300]
                        if snippet not in seen_knowledge:
                            seen_knowledge.add(snippet)
                            knowledge.append({
                                "query": kw,
                                "content": snippet,
                            })
                except Exception as exc:
                    logger.warning("记忆线索查找失败，跳过（不影响流搜索）：%s", str(exc)[:80])
                    pass  # 记忆查找失败不应中断 streams 流程

            logger.info(
                "search_users 搜索完成：keyword=%s, keywords=%s, chat_type=%s, platform=%s, "
                "流 %d 个, 人物 %d 个, 记忆线索 %d 个",
                str(keyword)[:80], [str(k)[:40] for k in kws], chat_type, platform,
                len(filtered), len(persons), len(knowledge),
            )
            return {
                "success": True,
                "streams": filtered,
                "persons": persons,
                "knowledge": knowledge,
                "count": len(filtered),
            }
        except Exception as exc:
            logger.exception("search_users 调用异常：%s", str(exc)[:80])
            return {"success": False, "error": str(exc)}

    # ── 5. get_frequency：发言频率查询 ─────────────────────────────────

    async def _get_frequency(
        chat_id: str,
        **kwargs: Any,
    ) -> dict:
        try:
            logger.debug("get_frequency 调用：chat_id=%s", chat_id)
            value = await ctx.frequency.get_current_talk_value()
            return {"success": True, "chat_id": chat_id, "value": value}
        except Exception as exc:
            logger.exception("get_frequency 调用异常：%s", str(exc)[:80])
            return {"success": False, "error": str(exc)}

    # ── 6.（已移除）list_plugin_tools ───────────────────────────────
    #
    # 该工具曾经 ``ctx.tool.get_definitions()`` 列出 MaiBot 宿主侧全量工具
    # （含插件 planner 层 @Tool：list_mcp_tools / call_mcp_tool / subagent_* 等），
    # 而这些名字在 Agent 循环注册表里不可调用，导致 LLM 照单调用后反复
    # tool-not-found 空转。Agent 循环已改为全量直接暴露自身可调工具
    # （executor/agent_loop.py），动态发现入口不再需要，故移除。

    # ── 组装返回 ─────────────────────────────────────────────────────────

    _defs = [
        ToolDefinition(
            name="search_memory",
            description=(
                "长期记忆检索：按关键词、时间范围、人物等条件检索对话记忆。"
                "支持 5 种模式：search(事实查询)/time(时间段)/hybrid(混合，默认)/episode(经历)/aggregate(整体印象)。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "关键词或搜索问题",
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "当前聊天流ID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，默认5",
                        "default": 5,
                    },
                    "mode": {
                        "type": "string",
                        "description": "检索模式：search/time/hybrid/episode/aggregate",
                        "default": "hybrid",
                        "enum": ["search", "time", "hybrid", "episode", "aggregate"],
                    },
                    "person_name": {
                        "type": "string",
                        "description": "人物名（可选）",
                    },
                    "time_start": {
                        "type": "string",
                        "description": "开始时间（可选，ISO格式）",
                    },
                    "time_end": {
                        "type": "string",
                        "description": "结束时间（可选，ISO格式）",
                    },
                },
                "required": ["query", "chat_id"],
            },
            handler=_search_memory,
        ),
        ToolDefinition(
            name="fetch_history",
            description=(
                "获取最近聊天历史：拉取指定聊天流的最近消息记录。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "聊天流ID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "获取数量，默认20，最大50",
                        "default": 20,
                        "maximum": 50,
                    },
                },
                "required": ["chat_id"],
            },
            handler=_fetch_history,
        ),
        ToolDefinition(
            name="query_person",
            description=(
                "查询人物信息：根据人物名/昵称解析person_id，然后通过记忆聚合模式获取该人物的画像近似信息。"
                "注意：宿主按注册名精确匹配，名字必须完整（如'低调的空格'中的'的'字不可省略）；"
                "解析失败时请改用 search_users 按昵称或QQ号检索。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "person_name": {
                        "type": "string",
                        "description": "人物名或昵称",
                    },
                },
                "required": ["person_name"],
            },
            handler=_query_person,
        ),
        ToolDefinition(
            name="search_users",
            description=(
                "搜索用户：按昵称/名字/ID 搜索已知用户与群，返回其 user_id（QQ号）、昵称、群信息，"
                "用于确定 send_message 的发送目标。"
                "支持一次传多个关键词（keywords 数组，任一命中即可）：把候选名、别名、QQ号一次全给，命中率更高。"
                "名字含'的'等虚词时自动分词容错（'低调空格'也能命中'低调的空格'）。"
                "当活跃会话中搜不到时，会同时检索人物画像与记忆线索（支持昵称/别名/QQ号匹配），"
                "返回 persons(人物画像) 与 knowledge(记忆线索) 辅助定位 user_id。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词（匹配用户昵称、群名、ID等，可选）",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "多个搜索关键词（可选，OR 语义：任一命中即返回，如昵称/别名/QQ号）",
                    },
                    "chat_type": {
                        "type": "string",
                        "description": "聊天类型过滤：group 或 private（可选）",
                        "enum": ["group", "private"],
                    },
                    "platform": {
                        "type": "string",
                        "description": "平台名（可选，如 qq/discord/wechat）",
                    },
                },
                "required": [],
            },
            handler=_search_users,
        ),
        ToolDefinition(
            name="get_frequency",
            description=(
                "获取当前发言频率：查询指定聊天流的当前发言频率值，用于了解 Bot 在该流的活跃程度。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "聊天流ID",
                    },
                },
                "required": ["chat_id"],
            },
            handler=_get_frequency,
        ),
    ]

    logger.info("信息获取工具集构建完成：%d 个工具定义", len(_defs))
    return _defs
