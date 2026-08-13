"""send_message 工具（升级版）— Agent 按 group_id/user_id 创建聊天流 + 润色 + 发送。

与旧版 info_tools 中 send_message 的区别：
- 旧版：直接接收 stream_id，简单透传 ctx.send.text（无润色、无流自动创建）
- 新版：接收 group_id 或 user_id → 调用 ctx.chat.open_session 创建/确保流存在 →
        通过注入的 send_polished 回调委托润色与重试发送

工厂模式（参照 ask_tool.py）：build_send_tool(ctx, *, send_polished, min_role) 返回 ToolDefinition。
send_polished 回调由上层（TaskManager / plugin.py）注入，实现关注点分离：
工具层不直接依赖 send_final_reply 的参数列表（ctx/config/prompt_manager）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...permission import Role
from ..registry import ToolDefinition

logger = logging.getLogger(__name__)


def build_send_tool(
    ctx: object,
    *,
    send_polished: Callable[[str, str, bool], Awaitable[None]],
    min_role: Role = Role.USER,
    prompt_service: Any | None = None,
) -> ToolDefinition:
    """构建 ``send_message`` 升级版工具。

    Args:
        ctx: 插件上下文（用于 ctx.chat.open_session）。
        send_polished: ``async def(text: str, stream_id: str, is_group: bool) -> None``
            润色 + 发送回调，由上层注入（TaskManager 注入 send_final_reply）。
        min_role: 调用此工具所需的最低角色（默认 USER）。
        prompt_service: PromptService 实例（可选）。提供后，XML 上下文注释通过 builder 生成。

    Returns:
        单个 ``send_message`` ToolDefinition。
    """

    async def _handler(**kwargs: Any) -> dict:
        text: str = str(kwargs.get("text", ""))
        group_id: str = str(kwargs.get("group_id", "")).strip()
        user_id: str = str(kwargs.get("user_id", "")).strip()
        platform: str = str(kwargs.get("platform", "qq")).strip()

        # 发送入口（消息正文截断到前 80 字符，避免敏感内容刷屏）
        logger.info(
            "send_message 入口: platform=%s group_id=%r user_id=%r text=%r",
            platform,
            group_id,
            user_id,
            text[:80],
        )

        # ── 参数校验 ──────────────────────────────────────────────────────
        if not text:
            logger.warning("send_message 参数校验失败: 缺少 text（消息文本）")
            return {"success": False, "error": "必须提供 text（消息文本）"}

        if not group_id and not user_id:
            logger.warning("send_message 参数校验失败: 未提供 group_id 或 user_id")
            return {"success": False, "error": "必须提供 group_id 或 user_id"}
        if group_id and user_id:
            logger.warning("send_message 参数校验失败: group_id 与 user_id 只能提供一个")
            return {"success": False, "error": "group_id 与 user_id 只能提供一个"}

        chat_type = "group" if group_id else "private"
        is_group = chat_type == "group"
        target_id = group_id or user_id

        # ── 推导 account_id / scope（从 get_all_streams 匹配真实会话流）─────
        account_id = ""
        scope_val = ""
        try:
            streams = await ctx.chat.get_all_streams(platform=platform)
            matches: list[dict | Any] = []
            for s in streams:
                sid_user = str(s.get("user_id") or "") if isinstance(s, dict) else str(getattr(s, "user_id", None) or "")
                sid_group = str(s.get("group_id") or "") if isinstance(s, dict) else str(getattr(s, "group_id", None) or "")
                if is_group and sid_group == group_id:
                    matches.append(s)
                elif not is_group and sid_user == user_id:
                    matches.append(s)
            if matches:
                # 优先选择带 account_id 的流（真实会话；插件创建的孤儿流没有 account_id）
                best = matches[0]
                for m in matches:
                    aid = str(m.get("account_id") or "") if isinstance(m, dict) else str(getattr(m, "account_id", None) or "")
                    if aid:
                        best = m
                        break
                account_id = str(best.get("account_id") or "") if isinstance(best, dict) else str(getattr(best, "account_id", None) or "")
                scope_val = str(best.get("scope") or "") if isinstance(best, dict) else str(getattr(best, "scope", None) or "")
        except Exception:
            logger.debug("send_message 获取流列表失败，回退为空 account_id/scope", exc_info=True)

        # ── 创建/确保聊天流 ──
        # ctx.chat.open_session 语义：目标流已存在则直接复用，不存在则自动创建（幂等确保）
        stream_id = ""
        created = False

        try:
            result = await ctx.chat.open_session(
                platform=platform,
                chat_type=chat_type,
                group_id=group_id if is_group else "",
                user_id=user_id if not is_group else "",
                account_id=account_id,
                scope=scope_val,
            )
        except Exception as exc:
            logger.warning("send_message 创建聊天流失败: %s", exc)
            return {"success": False, "error": f"创建聊天流失败: {exc}"}

        if isinstance(result, dict):
            if result.get("success") is False:
                logger.warning("send_message 创建聊天流失败: %s", result.get("error", ""))
                return {"success": False, "error": f"创建聊天流失败: {result.get('error', '')}"}
            stream_id = str(result.get("stream_id") or result.get("session_id") or "")
            created = bool(result.get("created", False))
        else:
            stream_id = str(getattr(result, "stream_id", "") or getattr(result, "session_id", ""))
            created = bool(getattr(result, "created", False))

        # created 标记该流是否为本次新建（False 表示复用了已存在的流）
        if not stream_id:
            logger.warning("send_message 创建聊天流失败: 无法获取 stream_id")
            return {"success": False, "error": "创建聊天流失败: 无法获取 stream_id"}

        # ── 委托润色 + 发送 ───────────────────────────────────────────────
        # 润色（PolishService）与指数退避重试（1s → 2s）均封装在注入的 send_polished 回调内，
        # 本工具只负责委托；回调抛出的异常在此统一捕获并转为失败结果
        try:
            logger.debug("send_message 委托 send_polished 发送（含润色与指数退避重试）: stream=%s is_group=%s", stream_id, is_group)
            await send_polished(text, stream_id, is_group)
        except Exception as exc:
            # send_polished 内部重试已耗尽，此处是最终失败
            logger.error("send_message 发送失败（重试已耗尽）: %s", exc, exc_info=True)
            return {"success": False, "error": f"发送失败: {exc}"}

        logger.info("send_message 发送成功: stream=%s created=%s 目标=%s", stream_id, created, target_id)

        # ── 记录上下文 ─────────────────────────────────────────────────────
        # 1. 纯文本记录（消息原话，无 XML）
        try:
            await ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": text}],
                visible_text=text,
                source_kind="plugin:oh-mai-agent:send_message",
            )
        except Exception:
            logger.warning("send_message 纯文本 context.append 失败 stream=%s", stream_id, exc_info=True)
        # 2. XML 系统说明（独立一条）
        if prompt_service is not None:
            try:
                # 以毫秒时间戳生成唯一 note_id，作为 message_id 随 XML 说明写入上下文
                note_id = f"oh-mai-agent:send:{int(time.time() * 1000)}"
                note_text = prompt_service.build(
                    "context_note",
                    kind="sent-message",
                    content=text,
                    id=note_id,
                )
                await ctx.maisaka.context.append(
                    stream_id=stream_id,
                    segments=[{"type": "text", "content": note_text}],
                    visible_text=note_text,
                    message_id=note_id,
                    source_kind="plugin:oh-mai-agent:send_message",
                )
            except Exception:
                logger.warning("send_message XML context.append 失败 stream=%s", stream_id, exc_info=True)

        return {"success": True, "stream_id": stream_id, "created": created}

    description = (
        "向好友/群发送消息（自动创建聊天流、必润色）。"
        "参数: text（消息文本,必填）+ group_id 或 user_id（目标 ID,必填其一且不能同时提供）"
        "+ platform（可选,默认 qq）。"
        "若不知道目标的 user_id/group_id，先调用 search_users 按昵称搜索获取。"
        "转达他人之言必须点明委托人，禁止转述废话。"
    )

    parameters: dict = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要发送的消息文本（必填）",
            },
            "group_id": {
                "type": "string",
                "description": "目标群 ID（与 user_id 二选一）",
            },
            "user_id": {
                "type": "string",
                "description": "目标用户 ID（与 group_id 二选一）",
            },
            "platform": {
                "type": "string",
                "description": "平台标识（可选，默认 qq）",
                "default": "qq",
            },
        },
        "required": ["text"],
    }

    return ToolDefinition(
        name="send_message",
        description=description,
        parameters=parameters,
        handler=_handler,
        visibility="discoverable",
        min_role=min_role,
    )
