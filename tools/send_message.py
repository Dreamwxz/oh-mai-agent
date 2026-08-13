"""send_message 工具（Agent 循环与 Planner 共用实现）。

合并自 ``tools/agent/send_tool.py`` 与 ``tools/planner/send_message.py``——
两者原本是近 300 行几乎逐行相同的重复实现（参数校验、建流、润色发送、
上下文记录）。合并后发送逻辑只有一份，两个入口各留一个薄工厂：

- ``build_send_tool``：Agent 循环 Discoverable 工具（TaskManager 注册），
  润色+发送委托注入的 ``send_polished`` 回调（关注点分离，工具层不依赖
  ``send_final_reply`` 的参数列表）。
- ``build_send_message_handler``：Planner @Tool handler（plugin.py 懒构建），
  内部直接绑定 ``send_final_reply``。

两个入口共享 ``_send_message_core``，行为完全一致（日志、校验、建流、
返回结构）。目标三选一：``stream_id``（直接发送到指定聊天流，如其他
用户的流，跳过建流）或 ``group_id`` / ``user_id``（经 open_session 建流）。
可选参数 ``polish`` / ``split`` 控制发送行为：

- ``polish``（默认 true）：false 时跳过 LLM 润色直发原文，
  适合发送代码、命令或结构化文本等不希望被改写的内容；
- ``split``（默认 true）：false 时不分割长文本整条发送，
  适合希望完整呈现（如代码块）的场景。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..permission import Role
from .registry import ToolDefinition

logger = logging.getLogger(__name__)


def _opt_bool(kwargs: dict[str, Any], key: str, default: bool) -> bool:
    """读取工具调用参数中的布尔值，兼容 LLM 传字符串 "true"/"false"。"""
    value = kwargs.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


async def _send_to_stream(
    ctx: object,
    *,
    text: str,
    stream_id: str,
    is_group: bool,
    send_polished: Callable[..., Awaitable[None]],
    prompt_service: Any | None,
    polish: bool,
    split: bool,
    created: bool = False,
) -> dict[str, Any]:
    """向已就绪的聊天流发送：委托润色+发送，随后写上下文记录。"""
    # ── 委托润色 + 发送（polish/split 可选项透传）──────────────────────
    try:
        logger.debug(
            "send_message 委托 send_polished 发送: stream=%s is_group=%s polish=%s split=%s",
            stream_id, is_group, polish, split,
        )
        await send_polished(text, stream_id, is_group, polish=polish, split=split)
    except Exception as exc:
        # send_polished 内部重试已耗尽，此处是最终失败
        logger.error("send_message 发送失败（重试已耗尽）: %s", exc, exc_info=True)
        return {"success": False, "error": f"发送失败: {exc}"}

    logger.info("send_message 发送成功: stream=%s created=%s", stream_id, created)

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
    # 2. XML 系统说明（独立一条，prompt_service 可用时）
    if prompt_service is not None:
        try:
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


async def _send_message_core(
    ctx: object,
    *,
    text: str,
    stream_id: str = "",
    group_id: str = "",
    user_id: str = "",
    platform: str,
    send_polished: Callable[..., Awaitable[None]],
    prompt_service: Any | None,
    polish: bool = True,
    split: bool = True,
) -> dict[str, Any]:
    """send_message 共享核心：参数校验 → 建流/直发 → 润色+发送 → 上下文记录。

    *stream_id* 提供时直接发送到该聊天流（如其他用户的流，跳过建流）；
    否则 ``group_id`` / ``user_id`` 二选一，经 open_session 建流后发送。

    Args:
        ctx: MaiBot PluginContext。
        text: 消息文本。
        stream_id: 目标聊天流 ID（与 group_id/user_id 三选一，优先级最高）。
        group_id: 目标群 ID（与 user_id 二选一）。
        user_id: 目标用户 ID（与 group_id 二选一）。
        platform: 平台标识。
        send_polished: ``async def(text, stream_id, is_group, *, polish, split)``
            润色 + 发送回调（Agent 版由 TaskManager 注入，Planner 版内部绑定
            send_final_reply）。
        prompt_service: PromptService 实例（可选）。提供后 XML 上下文注释
            通过 builder 生成，否则跳过 XML 记录。
        polish: 是否 LLM 润色（透传给 send_polished）。
        split: 是否分割长文本（透传给 send_polished）。

    Returns:
        ``{"success": bool, ...}`` 结构的结果字典。
    """
    # ── 参数校验 ──────────────────────────────────────────────────────
    if not text:
        logger.warning("send_message 参数校验失败: 缺少 text（消息文本）")
        return {"success": False, "error": "必须提供 text（消息文本）"}
    if stream_id:
        if group_id or user_id:
            logger.warning("send_message 参数校验失败: stream_id 与 group_id/user_id 只能提供其一")
            return {"success": False, "error": "stream_id 与 group_id/user_id 只能提供其一"}
        logger.info(
            "send_message 入口(直发流): stream_id=%s text=%r polish=%s split=%s",
            stream_id, text[:80], polish, split,
        )
        # 群聊判定沿用 send_final_reply 的推导规则（流 ID 含 ":group:" 视为群聊）
        return await _send_to_stream(
            ctx,
            text=text,
            stream_id=stream_id,
            is_group=":group:" in stream_id,
            send_polished=send_polished,
            prompt_service=prompt_service,
            polish=polish,
            split=split,
        )
    if not group_id and not user_id:
        logger.warning("send_message 参数校验失败: 未提供 stream_id/group_id/user_id")
        return {"success": False, "error": "必须提供 stream_id、group_id 或 user_id"}
    if group_id and user_id:
        logger.warning("send_message 参数校验失败: group_id 与 user_id 只能提供一个")
        return {"success": False, "error": "group_id 与 user_id 只能提供一个"}

    chat_type = "group" if group_id else "private"
    is_group = chat_type == "group"
    target_id = group_id or user_id
    logger.info(
        "send_message 入口: platform=%s group_id=%r user_id=%r text=%r polish=%s split=%s",
        platform, group_id, user_id, text[:80], polish, split,
    )

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

    # ── 创建/确保聊天流（幂等：已存在则复用，不存在则自动创建）────────
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

    if not stream_id:
        logger.warning("send_message 创建聊天流失败: 无法获取 stream_id")
        return {"success": False, "error": "创建聊天流失败: 无法获取 stream_id"}

    return await _send_to_stream(
        ctx,
        text=text,
        stream_id=stream_id,
        is_group=is_group,
        send_polished=send_polished,
        prompt_service=prompt_service,
        polish=polish,
        split=split,
        created=created,
    )


# ── Agent 循环版：discoverable ToolDefinition ───────────────────────────────


def build_send_tool(
    ctx: object,
    *,
    send_polished: Callable[..., Awaitable[None]],
    min_role: Role = Role.USER,
    prompt_service: Any | None = None,
) -> ToolDefinition:
    """构建 Agent 循环的 ``send_message`` 工具（discoverable，USER 可访问）。

    Args:
        ctx: 插件上下文（用于 ctx.chat.open_session）。
        send_polished: ``async def(text, stream_id, is_group, *, polish, split)``
            润色 + 发送回调，由上层（TaskManager）注入 send_final_reply。
        min_role: 调用此工具所需的最低角色（默认 USER）。
        prompt_service: PromptService 实例（可选）。提供后，XML 上下文注释
            通过 builder 生成。

    Returns:
        单个 ``send_message`` ToolDefinition。
    """

    async def _handler(**kwargs: Any) -> dict:
        return await _send_message_core(
            ctx,
            text=str(kwargs.get("text", "")),
            stream_id=str(kwargs.get("stream_id", "")).strip(),
            group_id=str(kwargs.get("group_id", "")).strip(),
            user_id=str(kwargs.get("user_id", "")).strip(),
            platform=str(kwargs.get("platform", "qq")).strip(),
            send_polished=send_polished,
            prompt_service=prompt_service,
            polish=_opt_bool(kwargs, "polish", True),
            split=_opt_bool(kwargs, "split", True),
        )

    description = (
        "向好友/群发送消息（自动创建聊天流、默认润色与长文本分割）。"
        "参数: text（消息文本,必填）+ 目标三选一: stream_id（目标聊天流 ID）"
        "或 group_id 或 user_id（不能同时提供多个）"
        "+ platform（可选,默认 qq）+ polish（可选,默认 true,false 时不润色直发原文）"
        "+ split（可选,默认 true,false 时不分割长文本整条发送）。"
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
            "stream_id": {
                "type": "string",
                "description": "目标聊天流 ID（与 group_id/user_id 三选一，提供时直接发送到该流）",
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
            "polish": {
                "type": "boolean",
                "description": "是否 LLM 润色（可选，默认 true；发代码/命令等不希望改写时设 false）",
                "default": True,
            },
            "split": {
                "type": "boolean",
                "description": "是否分割长文本为多条消息（可选，默认 true；希望整条完整呈现时设 false）",
                "default": True,
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


# ── Planner 版：@Tool handler 工厂 ─────────────────────────────────────────


def build_send_message_handler(ctx: Any, config: Any, pm: Any, pm_service: Any) -> Callable[..., Awaitable[dict]]:
    """返回 Planner @Tool ``send_message`` 的 handler 逻辑体。

    Args:
        ctx: MaiBot PluginContext。
        config: MaibotAgentConfig。
        pm: PromptManager（透传给 send_final_reply）。
        pm_service: PromptService（用于 build context_note）。

    Returns:
        ``async def handler(**kwargs) -> dict``，行为与 Agent 循环版一致。
    """

    from ..executor.instant import send_final_reply

    async def _send_polished(
        text: str, stream_id: str, is_group: bool,
        *, polish: bool = True, split: bool = True,
    ) -> None:
        await send_final_reply(
            text, stream_id,
            ctx, config, pm,
            pm_service,
            max_retries=3, is_group=is_group,
            polish=polish, split=split,
        )

    async def handler(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：向好友/群发送消息（自动创建流 + 润色 + 重试）。"""
        return await _send_message_core(
            ctx,
            text=str(kwargs.get("text", "")),
            stream_id=str(kwargs.get("stream_id", "")).strip(),
            group_id=str(kwargs.get("group_id", "")).strip(),
            user_id=str(kwargs.get("user_id", "")).strip(),
            platform=str(kwargs.get("platform", "qq")).strip(),
            send_polished=_send_polished,
            prompt_service=pm_service,
            polish=_opt_bool(kwargs, "polish", True),
            split=_opt_bool(kwargs, "split", True),
        )

    return handler
