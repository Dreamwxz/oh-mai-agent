"""Planner tool: search_users handler 工厂。

从 ``plugin.py:374-439`` 的 ``_tool_search_users`` 原样提取。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .._shared import _filter_streams

logger = logging.getLogger(__name__)


def build_search_users_handler(ctx: Any, config: Any) -> Callable[..., Any]:
    """返回 ``_tool_search_users`` 的 handler 逻辑体。

    Args:
        ctx: MaiBot PluginContext。
        config: MaibotAgentConfig。
    """

    async def handler(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：按昵称搜索用户，返回 user_id（QQ号）等信息。
        同时检索人物画像与记忆线索（支持昵称/别名匹配），辅助定位无活跃会话的用户。
        """
        try:
            keyword = str(kwargs.get("keyword", "")).strip()
            chat_type = str(kwargs.get("chat_type", "")).strip()
            platform = str(kwargs.get("platform", "all_platforms")).strip()

            logger.debug(
                "search_users 调用: keyword=%s chat_type=%s platform=%s",
                keyword[:80] or "-", chat_type or "-", platform,
            )

            streams = await ctx.chat.get_all_streams(platform=platform)
            filtered = _filter_streams(
                streams,
                keyword=keyword,
                chat_type=chat_type,
                max_results=config.search.max_results,
            )

            persons: list[dict] = []
            knowledge: list[dict] = []

            if keyword:
                # ── 人物画像查找（精确名称匹配） ──────────────
                try:
                    pid_result = await ctx.person.get_id_by_name(keyword)
                    # SDK 返回 person_id 字符串；同时兼容 dict 形式。
                    # 真实宿主对查无此名返回空串 ""，须判空避免假命中
                    if isinstance(pid_result, str) and pid_result:
                        persons.append({"person_id": pid_result, "matched_by": "exact_name"})
                    elif isinstance(pid_result, dict) and pid_result.get("person_id"):
                        persons.append(
                            {"person_id": pid_result["person_id"], "matched_by": "exact_name"}
                        )
                except Exception:
                    logger.debug("search_users 人物画像查找失败，跳过: keyword=%s", keyword[:80], exc_info=True)  # 人物查找失败不应中断流搜索

                # ── 记忆线索查找（混合检索） ────────────
                try:
                    k_result = await ctx.call_capability(
                        "knowledge.search",
                        query=keyword,
                        limit=5,
                        mode="hybrid",
                    )
                    if isinstance(k_result, dict):
                        if k_result.get("success") is False:
                            pass  # 显式失败——跳过
                        else:
                            content = k_result.get("content", "")
                            # "你不太了解..." 为 knowledge.search 无结果的占位文案，跳过
                            if content and content != "你不太了解...":
                                knowledge.append({
                                    "query": keyword,
                                    "content": str(content)[:300],
                                })
                    elif k_result:
                        content = str(k_result)
                        knowledge.append({
                            "query": keyword,
                            "content": content[:300],
                        })
                except Exception:
                    logger.debug("search_users 记忆线索查找失败，跳过: keyword=%s", keyword[:80], exc_info=True)  # 知识查找失败不应中断流搜索

            logger.info(
                "search_users 搜索完成: keyword=%s chat_type=%s platform=%s count=%d",
                keyword[:80] or "-", chat_type or "-", platform, len(filtered),
            )

            return {
                "success": True,
                "streams": filtered,
                "persons": persons,
                "knowledge": knowledge,
                "count": len(filtered),
            }
        except Exception as exc:
            logger.exception("search_users 搜索异常: %s", exc)
            return {"success": False, "error": str(exc)}

    return handler
