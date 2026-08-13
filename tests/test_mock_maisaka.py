import pytest
from conftest import MockCtx


@pytest.mark.asyncio
async def test_mock_maisaka_context_append():
    """验证 Maisaka context.append 会记录追加、返回递增索引与流 ID。"""
    ctx = MockCtx()
    result = await ctx.maisaka.context.append(
        "qq:g:12345", [{"type": "text", "content": "hello"}],
        visible_text="hello", source_kind="agent",
    )
    assert result["success"] is True
    assert result["index"] == 0
    assert result["stream_id"] == "qq:g:12345"
    assert len(ctx.maisaka.appends) == 1
    assert ctx.maisaka.appends[0]["stream_id"] == "qq:g:12345"
    result2 = await ctx.maisaka.context.append(
        "qq:g:67890", [{"type": "text", "content": "world"}],
        visible_text="world", source_kind="user",
    )
    assert result2["index"] == 1
    assert len(ctx.maisaka.appends) == 2


@pytest.mark.asyncio
async def test_mock_maisaka_stores_segments():
    """验证 context.append 原样保存 segments 及额外扩展字段。"""
    ctx = MockCtx()
    segments = [{"type": "text", "content": "line 1"}, {"type": "text", "content": "line 2"}]
    await ctx.maisaka.context.append(
        "qq:p:99999", segments, visible_text="line 1\nline 2",
        source_kind="system", message_id="msg_001", extra_field="extra_value",
    )
    record = ctx.maisaka.appends[0]
    assert record["segments"] == segments
    assert record["message_id"] == "msg_001"
    assert record["extra_field"] == "extra_value"
