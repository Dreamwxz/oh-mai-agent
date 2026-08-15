"""send_message 工具（Agent 循环与 Planner 共用实现）。

合并自 ``tools/agent/send_tool.py`` 与 ``tools/planner/send_message.py``——
两者原本是近 300 行几乎逐行相同的重复实现（参数校验、建流、润色发送、
上下文记录）。合并后发送逻辑只有一份，两个入口各留一个薄工厂：

- ``build_send_tool``：Agent 循环 Discoverable 工具（TaskManager 注册），
  润色+发送委托注入的 ``send_polished`` 回调（即 ``ReplySender.send_polished``，
  关注点分离，工具层不依赖发送器的参数列表）。
- ``build_send_message_handler``：Planner @Tool handler（plugin.py 懒构建），
  内部直接绑定 ``ReplySender.send_polished``。

两个入口共享 ``_send_message_core``，行为完全一致（日志、校验、建流、
返回结构）。目标三选一：``stream_id``（直接发送到指定聊天流，如其他
用户的流，跳过建流）或 ``group_id`` / ``user_id``（经 open_session 建流）。

工具固定走完整发送出口（润色 + 分割 + 重试，见 ``ReplySender``），不再暴露
``polish`` / ``split`` 开关。转达（relay）由自动判定完成：Agent 循环版经注入的
``resolve_relay`` 回调判定"目标用户 ≠ 任务发起人"即点名委托人（见
``executor/instant.py`` 的 ``_resolve_relay``）；Planner 版无任务上下文，
不做转达判定（一律本人发言）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..domain.stream_ref import is_group_stream
from ..permission import Role
from .registry import ToolDefinition

logger = logging.getLogger(__name__)

# ── send_message 工具的单一 schema 来源 ────────────────────────────────────
# Agent 循环版（build_send_tool 的 ToolDefinition）与 Planner 版（plugin.py 的
# @Tool）共享同一份描述与参数规范，避免两份手写 schema 漂移。

SEND_MESSAGE_DESCRIPTION = (
    "向好友/群发送消息（自动创建聊天流、默认润色与长文本分割）。"
    "参数: text（消息文本,必填）+ 目标三选一: stream_id（目标聊天流 ID）"
    "或 group_id 或 user_id（不能同时提供多个）"
    "+ platform（可选,默认 qq）。"
    "若不知道目标的 user_id/group_id，先调用 search_users 按昵称搜索获取。"
    "发送给他人（非任务发起人）时系统自动按转达处理并点名委托人。"
)

SEND_MESSAGE_PARAMS: list[dict[str, Any]] = [
    {"name": "text", "type": "string", "description": "要发送的消息文本", "required": True},
    {"name": "stream_id", "type": "string",
     "description": "目标聊天流 ID（与 group_id/user_id 三选一，提供时直接发送到该流）"},
    {"name": "group_id", "type": "string", "description": "目标群 ID（与 user_id 二选一）"},
    {"name": "user_id", "type": "string", "description": "目标用户 ID（与 group_id 二选一）"},
    {"name": "platform", "type": "string", "description": "平台标识（可选，默认 qq）",
     "default": "qq"},
]


def params_to_json_schema(params: list[dict[str, Any]]) -> dict:
    """将参数规范转换为 LLM function-calling 的 JSON Schema（ToolDefinition 用）。"""
    properties: dict[str, Any] = {}
    for p in params:
        prop: dict[str, Any] = {"type": p["type"], "description": p["description"]}
        if "default" in p:
            prop["default"] = p["default"]
        properties[p["name"]] = prop
    return {
        "type": "object",
        "properties": properties,
        "required": [p["name"] for p in params if p.get("required")],
    }


async def _send_to_stream(
    ctx: object,
    *,
    text: str,
    stream_id: str,
    is_group: bool,
    send_polished: Callable[..., Awaitable[None]],
    prompt_service: Any | None,
    resolve_relay: Callable[[str], Awaitable[str | None]] | None = None,
    created: bool = False,
) -> dict[str, Any]:
    """向已就绪的聊天流发送：委托润色+发送，随后写上下文记录。"""
    # ── 自动转达判定：目标用户 ≠ 任务发起人 → 点名委托人 ─────────────
    relay_from: str | None = None
    if resolve_relay is not None:
        try:
            relay_from = await resolve_relay(stream_id)
        except Exception:
            logger.debug("send_message 自动转达判定失败，按本人发言处理: stream=%s", stream_id, exc_info=True)
    # ── 委托润色 + 发送（完整出口：润色 + 分割 + 重试）──────────────────
    try:
        logger.debug(
            "send_message 委托 send_polished 发送: stream=%s is_group=%s relay_from=%s",
            stream_id, is_group, relay_from,
        )
        await send_polished(text, stream_id, relay_from=relay_from)
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
                # 主程序 [bot].nickname：缺省空串（builder 兜底"麦麦"）
                bot_name=str(await ctx.config.get("bot.nickname", "") or ""),
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
    resolve_relay: Callable[[str], Awaitable[str | None]] | None = None,
    chat_id: str = "",
) -> dict[str, Any]:
    """send_message 共享核心：宿主上下文剥离 → 参数校验 → 建流/直发 → 润色+发送 → 上下文记录。

    *stream_id* 提供时直接发送到该聊天流（如其他用户的流，跳过建流）；
    否则 ``group_id`` / ``user_id`` 二选一，经 open_session 建流后发送。

    Args:
        ctx: MaiBot PluginContext。
        text: 消息文本。
        stream_id: 目标聊天流 ID（与 group_id/user_id 三选一，优先级最高）。
        group_id: 目标群 ID（与 user_id 二选一）。
        user_id: 目标用户 ID（与 group_id 二选一）。
        platform: 平台标识。
        send_polished: ``async def(text, stream_id, *, relay_from=None)``
            完整发送回调（即 ``ReplySender.send_polished``：润色 + 分割 + 重试；
            Agent 版由 TaskManager 注入，Planner 版内部绑定）。
        prompt_service: PromptService 实例（可选）。提供后 XML 上下文注释
            通过 builder 生成，否则跳过 XML 记录。
        resolve_relay: ``async def(stream_id) -> relay_from | None`` 自动转达
            判定回调（Agent 循环版由上层注入，基于 current_task 的发起人与
            目标用户比对）；Planner 版不注入（无任务上下文，一律本人发言）。
        chat_id: 宿主注入的当前会话流 ID（MaiBot Host 专用字段，工具 schema
            无此参数，LLM 永不传）。Host 调用工具时会把当前会话上下文注入
            kwargs（stream_id/chat_id/group_id/user_id/platform，且仅当 LLM
            未提供该键时注入），见 MaiBot ``component_query._build_tool_context_payload``。
            宿主注入的 stream_id 恒等于 chat_id，据此剥离宿主上下文，避免
            「目标流」与「当前会话流」同名冲突（LLM 传 group_id 时宿主补
            stream_id、传 stream_id 时宿主补 group_id/user_id 的误报）。

    Returns:
        ``{"success": bool, ...}`` 结构的结果字典。
    """
    # ── 宿主上下文剥离 ──────────────────────────────────────────────
    # chat_id 是宿主注入指纹：schema 无此参数，LLM 永不传；宿主注入的
    # stream_id 恒等于 chat_id。剥离后 stream_id/group_id/user_id 只保留
    # LLM 显式目标，与「当前会话上下文」解耦。
    if chat_id:
        if stream_id == chat_id:
            logger.debug("send_message 剥离宿主注入 stream_id=%s", stream_id)
            stream_id = ""
        # 反查当前会话流（chat_id）的 group_id/user_id，剥离宿主注入值
        host_group = host_user = ""
        try:
            streams = await ctx.chat.get_all_streams(platform=platform)
            for s in streams:
                sid = (
                    str(s.get("stream_id") or s.get("session_id") or "")
                    if isinstance(s, dict)
                    else str(getattr(s, "stream_id", None) or getattr(s, "session_id", None) or "")
                )
                if sid == chat_id:
                    host_group = (
                        str(s.get("group_id") or "")
                        if isinstance(s, dict)
                        else str(getattr(s, "group_id", None) or "")
                    )
                    host_user = (
                        str(s.get("user_id") or "")
                        if isinstance(s, dict)
                        else str(getattr(s, "user_id", None) or "")
                    )
                    break
        except Exception:
            logger.debug("send_message 反查宿主上下文失败（回退不剥离）", exc_info=True)
        if host_group and group_id == host_group:
            logger.debug("send_message 剥离宿主注入 group_id=%s", group_id)
            group_id = ""
        if host_user and user_id == host_user:
            logger.debug("send_message 剥离宿主注入 user_id=%s", user_id)
            user_id = ""

    # ── 参数校验 ──────────────────────────────────────────────────────
    if not text:
        logger.warning("send_message 参数校验失败: 缺少 text（消息文本）")
        return {"success": False, "error": "必须提供 text（消息文本）"}
    if stream_id:
        if group_id or user_id:
            logger.warning("send_message 参数校验失败: stream_id 与 group_id/user_id 只能提供其一")
            return {"success": False, "error": "stream_id 与 group_id/user_id 只能提供其一"}
        logger.info(
            "send_message 入口(直发流): stream_id=%s text=%r",
            stream_id, text[:80],
        )
        # 群聊判定沿用发送器的推导规则（domain.stream_ref.is_group_stream）
        return await _send_to_stream(
            ctx,
            text=text,
            stream_id=stream_id,
            is_group=is_group_stream(stream_id),
            send_polished=send_polished,
            prompt_service=prompt_service,
            resolve_relay=resolve_relay,
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
        "send_message 入口: platform=%s group_id=%r user_id=%r text=%r",
        platform, group_id, user_id, text[:80],
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
        resolve_relay=resolve_relay,
        created=created,
    )


# ── Agent 循环版：discoverable ToolDefinition ───────────────────────────────


def build_send_tool(
    ctx: object,
    *,
    send_polished: Callable[..., Awaitable[None]],
    min_role: Role = Role.USER,
    prompt_service: Any | None = None,
    resolve_relay: Callable[[str], Awaitable[str | None]] | None = None,
) -> ToolDefinition:
    """构建 Agent 循环的 ``send_message`` 工具（discoverable，USER 可访问）。

    Args:
        ctx: 插件上下文（用于 ctx.chat.open_session）。
        send_polished: ``async def(text, stream_id, *, relay_from=None)``
            完整发送回调（``ReplySender.send_polished``），由上层（TaskManager）注入。
        min_role: 调用此工具所需的最低角色（默认 USER）。
        prompt_service: PromptService 实例（可选）。提供后，XML 上下文注释
            通过 builder 生成。
        resolve_relay: ``async def(stream_id) -> relay_from | None`` 自动转达
            判定回调（可选）。基于当前任务发起人与目标用户比对，目标为他人
            私聊时返回委托人；不注入则一律本人发言（Planner 版场景）。

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
            resolve_relay=resolve_relay,
            chat_id=str(kwargs.get("chat_id", "")).strip(),
        )

    return ToolDefinition(
        name="send_message",
        description=SEND_MESSAGE_DESCRIPTION,
        parameters=params_to_json_schema(SEND_MESSAGE_PARAMS),
        handler=_handler,
        visibility="discoverable",
        min_role=min_role,
    )


# ── Planner 版：@Tool handler 工厂 ─────────────────────────────────────────


def build_send_message_handler(ctx: Any, sender: Any) -> Callable[..., Awaitable[dict]]:
    """返回 Planner @Tool ``send_message`` 的 handler 逻辑体。

    Args:
        ctx: MaiBot PluginContext。
        sender: ``ReplySender`` 实例（绑定 ``send_polished`` 完整发送出口）。

    Returns:
        ``async def handler(**kwargs) -> dict``，行为与 Agent 循环版一致。
    """

    async def _send_polished(
        text: str, stream_id: str, *, relay_from: str | None = None,
    ) -> None:
        await sender.send_polished(text, stream_id, relay_from=relay_from)

    async def handler(**kwargs: Any) -> dict[str, Any]:
        """Planner 调用：向好友/群发送消息（自动创建流 + 润色 + 重试）。

        Planner 场景无任务上下文，不注入 resolve_relay —— 一律按本人发言
        处理（转达纪律仅 Agent 循环版经自动判定生效）。
        """
        return await _send_message_core(
            ctx,
            text=str(kwargs.get("text", "")),
            stream_id=str(kwargs.get("stream_id", "")).strip(),
            group_id=str(kwargs.get("group_id", "")).strip(),
            user_id=str(kwargs.get("user_id", "")).strip(),
            platform=str(kwargs.get("platform", "qq")).strip(),
            send_polished=_send_polished,
            prompt_service=sender.prompt_service,
            chat_id=str(kwargs.get("chat_id", "")).strip(),
        )

    return handler
