"""工具系统共享辅助函数。

本模块存放被 tools/ 下多个模块（如 ``info_tools.py``）及插件入口
（``plugin.py`` 的 ``@Tool``）共同使用的工具函数，避免重复定义。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 中文姓名/群名中的常见虚词与停顿字。分词容错时从两端剔除，
# 使「低调空格」也能命中「低调的空格」这类带虚词的候选。
_PARTICLE_CHARS = set(
    "的得了和与及或而在是有也并就都被把从对到为于之啊呀呢吗吧嘛哦噢嗯哈嘿"
)


def _normalize_name(value: str) -> str:
    """剔除空白与常见虚词后的小写归一化，用于分词容错匹配。"""
    return "".join(
        ch for ch in str(value or "").lower()
        if ch not in _PARTICLE_CHARS and not ch.isspace()
    )


def _stream_text(stream: dict) -> str:
    """把流的候选匹配字段拼成单个字符串（昵称/群名片/群名/群ID/用户ID）。

    字段间用 ``|`` 分隔，防止单个关键词跨字段拼接误命中。
    """
    parts = [
        stream.get("user_nickname", ""),
        stream.get("user_cardname", ""),
        stream.get("group_name", ""),
        stream.get("group_id", ""),
        stream.get("user_id", ""),
    ]
    return "|".join(str(p or "") for p in parts)


def _keyword_hits_stream(stream: dict, keyword: str) -> bool:
    """单个关键词是否命中流。

    先做原始子串匹配（大小写不敏感）；失败时再做分词容错：
    关键词与候选都剔除虚词后重新子串匹配（如「低调空格」命中「低调的空格」）。
    """
    text = str(_stream_text(stream)).lower()
    kw = str(keyword or "").lower().strip()
    if not kw:
        return False
    if kw in text:
        return True
    norm_kw = _normalize_name(kw)
    return bool(norm_kw) and norm_kw in _normalize_name(text)


def _filter_streams(
    streams: list[dict],
    keyword: str = "",
    keywords: list[str] | None = None,
    chat_type: str = "",
    max_results: int = 20,
) -> list[dict]:
    """对聊天流列表应用 keyword/keywords/chat_type 过滤和数量上限。

    Args:
        streams: 原始聊天流列表。
        keyword: 过滤关键词（对 user_nickname/user_cardname/group_name/group_id/user_id
            做子串匹配，大小写不敏感；失败时自动剔除虚词做分词容错）。
        keywords: 附加关键词列表（可选）。与 keyword 合并后取 OR 语义：
            任一关键词命中即保留该流。
        chat_type: 聊天类型过滤（"group"/"private"，空字符串表示不过滤）。
        max_results: 返回条数上限。

    Returns:
        过滤后裁剪至 max_results 的列表。
    """
    merged = [kw for kw in ([keyword] if keyword else []) + (keywords or []) if str(kw or "").strip()]
    if merged:
        streams = [s for s in streams if any(_keyword_hits_stream(s, kw) for kw in merged)]
    if chat_type:
        # 聊天类型精确匹配（如 "group"/"private"），空串表示不过滤。
        streams = [s for s in streams if s.get("chat_type") == chat_type]
    return streams[:max_results]
