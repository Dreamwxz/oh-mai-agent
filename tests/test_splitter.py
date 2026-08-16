"""executor/splitter.py 的测试 —— 确定性回复分割器。

覆盖：短文本不分割、两级策略（有换行按行为主 / 无换行按句号）、
引号保护、守卫规则、超长硬切、max_messages 尾部合入、
原文保真（join(segments) == 归一化原文）。
"""

from __future__ import annotations

import re

from oh_mai_agent.executor.splitter import (
    _can_split_soft,
    _chunk_oversized,
    _mark_quote_regions,
    _split_into_units,
    split_message,
)


def _normalized(text: str) -> str:
    """与 splitter 内部一致的归一化：连续换行（含中间空白）→ 单个换行。"""
    return re.sub(r"\n\s*\n+", "\n", text)


def _is_whole_line_concatenation(segment: str, lines: list[str]) -> bool:
    """判断 *segment* 是否由若干完整行（无行内截断）拼接而成。"""
    content = segment[:-1] if segment.endswith("\n") else segment
    parts = content.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not parts:
        return False
    start = next((i for i, line in enumerate(lines) if line == parts[0]), None)
    if start is None:
        return False
    return lines[start : start + len(parts)] == parts


class TestShortText:
    def test_empty_returns_empty(self) -> None:
        assert split_message("") == []
        assert split_message("   ") == []

    def test_short_text_single_segment(self) -> None:
        text = "你好，世界！"
        assert split_message(text) == [text]

    def test_text_at_max_length_single_segment(self) -> None:
        text = "好" * 1000
        assert split_message(text, max_length=1000) == [text]

    def test_whitespace_stripped_around_text(self) -> None:
        # 首尾空白被 strip，返回内部文本
        assert split_message("  你好  ") == ["你好"]


class TestLineSplitting:
    def test_long_text_with_newlines_splits_by_line(self) -> None:
        lines = [f"第{i}行" + "好" * 400 for i in range(3)]
        text = "\n".join(lines)
        segments = split_message(text, max_length=500, max_messages=5)
        assert len(segments) == 3
        # 换行符保留在左侧片段，join 后与归一化原文一致
        assert "".join(segments) == _normalized(text)

    def test_consecutive_newlines_collapsed(self) -> None:
        text = "第一段内容。\n\n\n第二段内容。"
        segments = split_message(text, max_length=10, max_messages=5)
        assert "".join(segments) == _normalized(text)
        assert len(segments) >= 2

    def test_single_line_long_text_splits_at_sentence_end(self) -> None:
        text = "。".join([f"句子{i}内容很丰富" for i in range(50)])
        segments = split_message(text, max_length=200, max_messages=5)
        assert len(segments) >= 2
        assert all(len(s) <= 200 for s in segments)
        assert "".join(segments) == _normalized(text)

    def test_join_equals_normalized_original(self) -> None:
        text = (
            "今天天气不错，适合出门散步。\n"
            "我看了天气预报：明天会下雨！大家记得带伞？\n"
            "下面是明天的安排；上午开会，下午写代码。"
        )
        segments = split_message(text, max_length=40, max_messages=5)
        assert "".join(segments) == _normalized(text)


class TestTwoTierStrategy:
    """两级策略：有换行按行分割为主，无换行按句号分割。"""

    def test_short_lines_packed_but_never_broken(self) -> None:
        # 短行合并打包：段边界只在行边界，任何段都是完整行的拼接
        lines = [f"行{i}内容" for i in range(30)]
        text = "\n".join(lines)
        segments = split_message(text, max_length=50, max_messages=5)
        assert "".join(segments) == _normalized(text)
        assert len(segments) > 1
        for segment in segments:
            assert _is_whole_line_concatenation(segment, lines)

    def test_oversized_line_split_by_sentence_within_line(self) -> None:
        # 行超长 → 行内按句号切；短行保持完整，段边界只在行边界/行内句号
        line = "第一句。第二句。第三句。" * 60  # 720 字
        text = line + "\n" + "结尾短行。"
        segments = split_message(text, max_length=100, max_messages=5)
        assert "".join(segments) == _normalized(text)
        assert all(len(s) <= 100 for s in segments[:-1])
        assert segments[-1].endswith("结尾短行。")

    def test_segments_do_not_mix_partial_lines(self) -> None:
        # 回归：旧实现会把一行从句子中间拆断并跨行混装
        # （seg 以第一行末尾句子开头、再混入第二行），两级策略后不再发生
        line = "这是第一段描述内容，包含多个句子。第二个句子在这里。第三个句子也在这里。" * 24  # 864 字
        text = line + "\n" + "短行结尾。"
        segments = split_message(text, max_length=500, max_messages=5)
        assert "".join(segments) == _normalized(text)
        assert all(len(s) <= 500 for s in segments)
        # 短行始终完整保留在最后一段
        assert segments[-1].endswith("短行结尾。")

    def test_no_newline_falls_back_to_sentence_split(self) -> None:
        # 无换行（单段长文）→ 整段按句号切
        text = "。".join([f"句子{i}内容" for i in range(50)])
        segments = split_message(text, max_length=100, max_messages=5)
        assert "".join(segments) == _normalized(text)
        assert len(segments) >= 2
        assert all(len(s) <= 100 for s in segments[:-1])

    def test_trailing_newline_merged_into_previous_unit(self) -> None:
        # 回归：行尾 \n 归入前一片段，join 保真不丢换行
        units = _split_into_units("句子一。句子二。\n")
        assert units == ["句子一。", "句子二。\n"]
        assert "".join(units) == "句子一。句子二。\n"


class TestGuardRules:
    def test_quote_content_not_split(self) -> None:
        text = '他说："你好，世界"，然后离开了。'
        units = _split_into_units(text)
        # 引号内的逗号不切分：任何单元都不以 "你好，世界" 内部为边界
        assert "".join(units) == _normalized(text)
        for unit in units:
            assert "你好，世界" in unit or "你好" not in unit
    def test_colon_guard_no_split_after_colon(self) -> None:
        text = "说明： 这是一个很长的说明内容" + "很" * 100
        assert _can_split_soft(" ", text, text.index("：") + 1) is False

    def test_space_between_alnum_not_split(self) -> None:
        assert _can_split_soft(" ", "hello world", 5) is False
        assert _can_split_soft(" ", "QQ 群聊", 2) is False

    def test_space_after_period_can_split(self) -> None:
        # "." 不是分隔符，但空格两侧非字母数字时可切
        assert _can_split_soft(" ", "结束。 新的开始", 3) is True

    def test_dash_guard_no_split(self) -> None:
        assert _can_split_soft(" ", "— 重要内容", 1) is False
        assert _can_split_soft(" ", "- 列表项", 1) is False

    def test_soft_separator_without_guard_splits(self) -> None:
        units = _split_into_units("第一段，第二段，第三段")
        assert units == ["第一段，", "第二段，", "第三段"]


class TestOversizedChunking:
    def test_hard_chunk_long_unit_without_punctuation(self) -> None:
        unit = "a" * 2500
        chunks = _chunk_oversized(unit, 1000)
        assert chunks == ["a" * 1000, "a" * 1000, "a" * 500]
        assert "".join(chunks) == unit

    def test_split_message_hard_chunks_oversized_code_block(self) -> None:
        text = "下面是代码：" + "x" * 2200
        segments = split_message(text, max_length=1000, max_messages=5)
        assert "".join(segments) == _normalized(text)
        assert all(len(s) <= 1000 for s in segments)

    def test_oversized_unit_within_quotes_not_chunked_by_quotes(self) -> None:
        # 引号内部不切分，但超长时仍会硬切兜底
        text = "他说" + "很" * 1500 + "然后走了"
        segments = split_message(text, max_length=1000, max_messages=5)
        assert "".join(segments) == _normalized(text)


class TestMaxMessages:
    def test_overflow_merged_into_last_segment(self) -> None:
        # 50 个句子约 340 字符、max_length=100、max_messages=2
        # → 段 1 满 100，其余全部尾部合入最后一条（最后一条超长）
        text = "。".join([f"句子{i}内容" for i in range(50)])
        segments = split_message(text, max_length=100, max_messages=2)
        assert len(segments) == 2
        assert "".join(segments) == _normalized(text)
        assert len(segments[-1]) > 100

    def test_max_messages_one_means_no_split(self) -> None:
        text = "\n".join([f"第{i}行内容" for i in range(20)])
        segments = split_message(text, max_length=10, max_messages=1)
        assert segments == [text]

    def test_few_units_stay_below_cap(self) -> None:
        text = "\n".join([f"第{i}行内容" + "长" * 100 for i in range(2)])
        segments = split_message(text, max_length=150, max_messages=5)
        assert len(segments) == 2
        assert "".join(segments) == _normalized(text)


class TestQuoteMarking:
    def test_quote_regions_marked(self) -> None:
        text = '他说"你好，世界"然后走了'
        inside = _mark_quote_regions(text)
        assert inside[0] is False
        # 引号内的字符标记为 True
        quote_start = text.index("你")
        quote_end = text.index("世") + 1
        assert all(inside[quote_start:quote_end])
        # 引号外的"他说"区域
        assert not any(inside[0:2])
