"""回复消息分割器 —— 复刻 MaiBot response_splitter 思路的确定性版本。

MaiBot 的 ``split_into_sentences_w_remove_punctuation``（``src/chat/utils/utils.py``）
按 ``，, 。; 空格 换行`` 切分回复，再按文本长度**概率性合并**相邻段落（为聊天
"打字感"服务），随后 ``merge_sentences_to_max_count`` 把段数压缩到 ``max_split_num``。
本模块借用其切分规则，但做了三处调整以适配任务回复场景：

1. **确定性**：去掉概率性合并。任务回复是结果交付而非闲聊，输出必须可预测、
   可测试；合并只发生在"段数超过 ``max_messages``"时（尾部并入最后一条）。
2. **保留原文**：切分点标点随左侧片段保留，``"".join(segments)`` 与归一化后的
   原文完全一致（MaiBot 会丢弃切分点标点）。任何内容都不会丢失。
3. **不丢内容兜底**：超长文本不会像 MaiBot 那样返回默认回复"呃呃"，而是
   按 ``max_length`` 硬切无标点的超长句（如长代码块），保证消息全部送达。

切分规则（两级策略，句末标点部分与 MaiBot 一致）：

- **有换行 → 按行分割为主**：行是打包的基本单元，段边界只在行边界
  （多行可合并打包到 ``max_length``，但不会把行拆散、不会跨行混装）；
  仅当单行超过 ``max_length`` 时，才在行内按句末标点切分；
- **无换行（单段长文）→ 按句号分割**：整段按句末标点切分打包；
- 句末标点（。！？!?；;）处切分；
- 逗号/空格为软分隔：冒号（中英文）旁边不切、破折号旁边不切、
  空格两侧均为字母/数字时不切（"hello world"、"3.14"不断开）；
- 成对引号（中英文单双引号、书名号式引号）内部一律不切；
- 连续空行先归一化为单个换行；
- 无标点的超长片段（如长代码块）按 ``max_length`` 硬切兜底（不丢内容）。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────

_QUOTE_CHARS = frozenset('"\'“”‘’「」『』')
"""成对引号字符集（与 MaiBot split_into_sentences 一致）。"""

_SENTENCE_SEPARATORS = frozenset("。！？!?；;")
"""句末标点：命中即切分（引号内部除外）。"""

_SOFT_SEPARATORS = frozenset("，, ")
"""软分隔：逗号/空格，满足守卫条件才切分。"""

_NEWLINE_RUN = re.compile(r"\n\s*\n+")
"""连续换行（含中间空白）归一化为单个换行。"""


# ── 内部：切分辅助 ───────────────────────────────────────────────────────────


def _mark_quote_regions(text: str) -> list[bool]:
    """标记文本中位于成对引号内部的字符位置（复刻 MaiBot 的引号状态机）。

    返回与 *text* 等长的布尔列表，True 表示该字符在引号内部。
    英文单双引号开闭字符相同，用同一字符切换状态；中文引号成对区分。
    """
    inside_quote = [False] * len(text)
    in_quote = False
    current_quote_char = ""
    for idx, ch in enumerate(text):
        if ch in _QUOTE_CHARS:
            if not in_quote:
                in_quote = True
                current_quote_char = ch
                inside_quote[idx] = False
            else:
                if (
                    ch == current_quote_char
                    or ch in {'"', "'"} and current_quote_char in {'"', "'"}
                ):
                    in_quote = False
                    current_quote_char = ""
                inside_quote[idx] = False
        else:
            inside_quote[idx] = in_quote
    return inside_quote


def _can_split_soft(char: str, text: str, index: int) -> bool:
    """判定软分隔符（逗号/空格）是否可以作为切分点。

    守卫规则（与 MaiBot 一致）：
    1. 分隔符左右紧邻中英文冒号时不切（"例如： 苹果"中的空格不切）；
    2. 空格左右紧邻破折号（- / —）时不切（"—— 重要"不切）；
    3. 空格左右两侧均为字母/数字时不切（"hello world"、"QQ 群"不切）。
    """
    prev_char = text[index - 1] if index > 0 else ""
    next_char = text[index + 1] if index < len(text) - 1 else ""
    if prev_char in {":", "："} or next_char in {":", "："}:
        return False
    if char == " ":
        if prev_char in {"-", "—"} or next_char in {"-", "—"}:
            return False
        if prev_char.isalnum() and next_char.isalnum():
            return False
    return True


def _split_into_units(text: str) -> list[str]:
    """把文本切成句级候选片段（句末标点/软分隔处切分），切分点标点保留在左侧。

    这是两级策略中的**句级切分**：用于无换行的整段长文，以及按行分割时
    超长单行的行内切分。换行不在此处切分（由 ``_split_lines`` 负责），
    若文本带行尾 ``\n`` 会随片段保留，不丢内容。

    规则：
    - 句末标点（。！？!?；;）切分；
    - 逗号/空格按 ``_can_split_soft`` 守卫条件切分；
    - 成对引号内部一律不切分。

    返回片段满足：``"".join(units) == text``。
    """
    inside_quote = _mark_quote_regions(text)

    units: list[str] = []
    current = ""
    for index, char in enumerate(text):
        if char in _SENTENCE_SEPARATORS:
            # 引号内部的句末标点也不切分（与 MaiBot 一致）
            if inside_quote[index]:
                current += char
                continue
            units.append(current + char)
            current = ""
            continue
        if char in _SOFT_SEPARATORS:
            if not inside_quote[index] and _can_split_soft(char, text, index):
                units.append(current + char)
                current = ""
            else:
                current += char
            continue
        current += char

    if current:
        if current.strip():
            units.append(current)
        elif units:
            # 尾随空白（如行尾 "\n"）归入前一片段，避免 join 时丢内容
            units[-1] += current
        else:
            units.append(current)
    return units


def _split_lines(text: str) -> list[str]:
    """按换行把文本切成行，换行符保留在左侧行尾。

    与"切分点随左侧保留"约定一致：每行（除最后一行）以 ``\n`` 结尾，
    满足 ``"".join(lines) == text``。文本经归一化后无连续换行，不存在空行。
    """
    parts = text.split("\n")
    lines: list[str] = []
    for index, part in enumerate(parts):
        if index < len(parts) - 1:
            lines.append(part + "\n")
        elif part:
            lines.append(part)
    return lines


def _split_by_lines(text: str, max_length: int) -> list[str]:
    """两级切分第一步：按行切分，超长行再在行内按句号切。

    返回文档顺序的候选片段：
    - 不超过 *max_length* 的行整体保留为单元（贪心打包时不会被拆行）；
    - 超过 *max_length* 的行经 ``_split_into_units`` 切成句单元，
      段边界将落在行内句号处（无标点则落到硬切兜底）。

    满足 ``"".join(units) == text``。
    """
    units: list[str] = []
    for line in _split_lines(text):
        if len(line) <= max_length:
            units.append(line)
        else:
            units.extend(_split_into_units(line))
    return units


def _chunk_oversized(unit: str, max_length: int) -> list[str]:
    """硬切无标点/换行的超长片段（如长代码块），按 *max_length* 均匀截断。"""
    return [unit[i : i + max_length] for i in range(0, len(unit), max_length)]


# ── 公开接口 ─────────────────────────────────────────────────────────────────


def split_message(
    text: str,
    *,
    max_length: int = 1000,
    max_messages: int = 5,
) -> list[str]:
    """把长回复文本切成多条待发送消息。

    流程（两级分割策略）：
    1. 空白文本返回空列表；长度不超过 *max_length* 的短文本原样返回单条
       （保持未开启分割时的发送行为不变）；
    2. **含换行 → 按行分割为主**：行是打包的基本单元，段边界只在行边界，
       仅当单行超过 *max_length* 时才在行内按句号切分（``_split_by_lines``）；
    3. **无换行（单段长文）→ 按句号分割**：整段按句末标点切分
       （``_split_into_units``）；
    4. 超过 *max_length* 的无标点长片段按 *max_length* 硬切（兜底，不丢内容）；
    5. 贪心打包：优先让每段不超过 *max_length*；片段数达到 *max_messages*
       上限后，剩余片段并入最后一条（复刻 MaiBot ``merge_sentences_to_max_count``
       的"尾部合入"思路），保证最多发出 *max_messages* 条。

    Args:
        text: 待分割的回复文本。
        max_length: 单条消息目标最大长度（字符）；超长无标点片段会被硬切。
        max_messages: 最多分割条数；``1`` 表示不分割。

    Returns:
        非空分段列表；``"".join(segments)`` 与归一化后的 *text* 一致。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    normalized = _NEWLINE_RUN.sub("\n", text)

    # 两级切分：含换行按行为主（超长行再在行内按句号切）；无换行整段按句号切
    if "\n" in normalized:
        units = _split_by_lines(normalized, max_length)
    else:
        units = _split_into_units(normalized)
    if not units:
        return [text]

    # 兜底：硬切超过 max_length 的无标点长片段
    if max_length > 0:
        chunks: list[str] = []
        for unit in units:
            if len(unit) > max_length:
                chunks.extend(_chunk_oversized(unit, max_length))
            else:
                chunks.append(unit)
        units = chunks

    # 贪心打包：每段尽量不超过 max_length，最多 max_messages 条
    segments: list[str] = []
    current = ""
    for unit in units:
        if (
            current
            and len(current) + len(unit) > max_length
            and len(segments) < max_messages - 1
        ):
            segments.append(current)
            current = unit
        else:
            current += unit
    if current:
        segments.append(current)

    segments = [segment for segment in segments if segment.strip()]
    return segments or [text]
