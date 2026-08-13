"""工具系统共享辅助函数。

本模块存放被 tools/ 下多个模块（如 ``info_tools.py``）及插件入口
（``plugin.py`` 的 ``@Tool``）共同使用的工具函数，避免重复定义。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _filter_streams(
    streams: list[dict],
    keyword: str = "",
    chat_type: str = "",
    max_results: int = 20,
) -> list[dict]:
    """对聊天流列表应用 keyword/chat_type 过滤和数量上限。

    Args:
        streams: 原始聊天流列表。
        keyword: 过滤关键词（对 user_nickname/user_cardname/group_name/group_id/user_id 做子串匹配，大小写不敏感）。
        chat_type: 聊天类型过滤（"group"/"private"，空字符串表示不过滤）。
        max_results: 返回条数上限。

    Returns:
        过滤后裁剪至 max_results 的列表。
    """
    if keyword:
        # 关键词过滤：大小写不敏感子串匹配，命中任一字段即保留。
        # `str(x or "")` 将 None 归一为空串，避免 str(None)="None" 被关键词误命中。
        kw = keyword.lower()
        streams = [
            s for s in streams
            if kw in str(s.get("user_nickname", "") or "").lower()
            or kw in str(s.get("user_cardname", "") or "").lower()
            or kw in str(s.get("group_name", "") or "").lower()
            or kw in str(s.get("group_id", "") or "").lower()
            or kw in str(s.get("user_id", "") or "").lower()
        ]
    if chat_type:
        # 聊天类型精确匹配（如 "group"/"private"），空串表示不过滤。
        streams = [s for s in streams if s.get("chat_type") == chat_type]
    return streams[:max_results]
