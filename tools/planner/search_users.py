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
        支持 keyword 或多个 keywords（OR 语义）；名字含虚词时自动分词容错。
        同时检索人物画像与记忆线索（支持昵称/别名匹配），辅助定位无活跃会话的用户。
        """
        try:
            keyword = str(kwargs.get("keyword", "")).strip()
            keywords = kwargs.get("keywords") or []
            chat_type = str(kwargs.get("chat_type", "")).strip()
            platform = str(kwargs.get("platform", "all_platforms")).strip()

            # 合并去重：keyword + keywords（OR 语义）
            kws = list(dict.fromkeys(
                str(k).strip() for k in ([keyword] + list(keywords)) if str(k or "").strip()
            ))

            logger.debug(
                "search_users 调用: keyword=%s keywords=%s chat_type=%s platform=%s",
                keyword[:80] or "-", [str(k)[:40] for k in kws], chat_type or "-", platform,
            )

            streams = await ctx.chat.get_all_streams(platform=platform)
            filtered = _filter_streams(
                streams,
                keyword=keyword,
                keywords=list(keywords),
                chat_type=chat_type,
                max_results=config.search.max_results,
            )

            persons: list[dict] = []
            knowledge: list[dict] = []
            seen_persons: set[str] = set()
            seen_knowledge: set[str] = set()

            for kw in kws:
                # ── 人物画像查找（精确名称匹配） ──────────────
                try:
                    pid_result = await ctx.person.get_id_by_name(kw)
                    # SDK 返回 person_id 字符串；同时兼容 dict 形式。
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
                except Exception:
                    logger.debug("search_users 人物画像查找失败，跳过: keyword=%s", kw[:80], exc_info=True)  # 人物查找失败不应中断流搜索

                # ── 记忆线索查找（混合检索） ────────────
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
                            # "你不太了解..." 为 knowledge.search 无结果的占位文案，跳过
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
                except Exception:
                    logger.debug("search_users 记忆线索查找失败，跳过: keyword=%s", kw[:80], exc_info=True)  # 知识查找失败不应中断流搜索

            logger.info(
                "search_users 搜索完成: keyword=%s keywords=%s chat_type=%s platform=%s count=%d",
                keyword[:80] or "-", [str(k)[:40] for k in kws], chat_type or "-", platform, len(filtered),
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
