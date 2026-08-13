"""Planner tool: send_message handler 工厂。

从 ``plugin.py:866-977`` 的 ``_tool_send_message`` 原样提取。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ...executor.instant import send_final_reply

logger = logging.getLogger(__name__)


def build_send_message_handler(ctx: Any, config: Any, pm: Any, pm_service: Any) -> Callable[..., Any]:
    """返回 ``_tool_send_message`` 的 handler 逻辑体。

    Args:
        ctx: MaiBot PluginContext。
        config: MaibotAgentConfig。
        pm: PromptManager（透传给 send_final_reply）。
        pm_service: PromptService（用于 build context_note）。
    """

    async def handler(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：向好友/群发送消息（自动创建流 + 润色 + 重试）。"""
        try:
            text = str(kwargs.get("text", ""))
            group_id = str(kwargs.get("group_id", "")).strip()
            user_id = str(kwargs.get("user_id", "")).strip()
            platform = str(kwargs.get("platform", "qq")).strip()

            logger.info(
                "send_message 调用: platform=%s group_id=%s user_id=%s",
                platform, group_id or "-", user_id or "-",
            )

            if not text:
                logger.warning("send_message 参数校验失败: 缺少 text 消息文本")
                return {"success": False, "error": "必须提供 text（消息文本）"}
            if not group_id and not user_id:
                logger.warning("send_message 参数校验失败: 缺少 group_id 或 user_id")
                return {"success": False, "error": "必须提供 group_id 或 user_id"}
            if group_id and user_id:
                logger.warning("send_message 参数校验失败: group_id 与 user_id 不能同时提供")
                return {"success": False, "error": "group_id 与 user_id 只能提供一个"}

            # 目标解析：有 group_id 视为群聊，否则按 user_id 视为私聊
            chat_type = "group" if group_id else "private"
            is_group = chat_type == "group"

            # 推导 account_id / scope（从 get_all_streams 匹配真实会话流）
            account_id = ""
            scope_val = ""
            try:
                streams = await ctx.chat.get_all_streams(platform=platform)
                matches: list[dict] = []
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
                logger.debug("send_message 流匹配失败，回退空 account_id/scope", exc_info=True)

            # 创建/确保聊天流（open_session 命中已有会话则复用，新建时返回 created=True）
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
                logger.exception("send_message 创建聊天流失败: %s", exc)
                return {"success": False, "error": f"创建聊天流失败: {exc}"}

            # 兼容 dict / 对象两种返回形态
            if isinstance(result, dict):
                if result.get("success") is False:
                    logger.error("send_message 创建聊天流失败: %s", result.get("error", ""))
                    return {"success": False, "error": f"创建聊天流失败: {result.get('error', '')}"}
                stream_id = str(result.get("stream_id") or result.get("session_id") or "")
                created = bool(result.get("created", False))
            else:
                stream_id = str(getattr(result, "stream_id", "") or getattr(result, "session_id", ""))
                created = bool(getattr(result, "created", False))

            if not stream_id:
                logger.error("send_message 创建聊天流失败: 无法获取 stream_id")
                return {"success": False, "error": "创建聊天流失败: 无法获取 stream_id"}

            logger.info(
                "send_message 流就绪: platform=%s chat_type=%s stream_id=%s created=%s account_id=%s",
                platform, chat_type, stream_id, created, account_id,
            )

            # 润色 + 发送（指数退避重试，max_retries=3）
            await send_final_reply(
                text, stream_id,
                ctx, config, pm,
                pm_service,
                max_retries=3, is_group=is_group,
            )

            logger.info(
                "send_message 发送成功: platform=%s group_id=%s user_id=%s stream_id=%s text=%s",
                platform, group_id or "-", user_id or "-", stream_id, text[:80],
            )

            # 记录上下文 — 两条独立追加（失败仅告警，不影响发送结果）
            try:
                # note_id：毫秒时间戳生成唯一 id，作 XML 注记的 message_id
                note_id = f"oh-mai-agent:send:{int(time.time() * 1000)}"
                # 1. 纯文本记录（消息原话，无 XML）
                await ctx.maisaka.context.append(
                    stream_id=stream_id,
                    segments=[{"type": "text", "content": text}],
                    visible_text=text,
                    source_kind="plugin:oh-mai-agent:send_message",
                )
                # 2. XML 系统说明（独立一条，通过 builder 生成）
                note_text = pm_service.build(
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
                logger.warning("send_message 上下文记录失败（不影响发送结果）: stream=%s", stream_id, exc_info=True)

            return {"success": True, "stream_id": stream_id, "created": created}
        except Exception as exc:
            logger.exception("send_message 执行异常: %s", exc)
            return {"success": False, "error": str(exc)}

    return handler
