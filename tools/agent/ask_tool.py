"""ask_user 工具 — Agent 暂停当前任务，向用户提问并等待回复后继续执行。

本工具是 essential 级工具，在 Agent 循环中始终可见，无需经 list_tools 发现。
调用后任务由 AgentLoop 内部切换至 ``waiting_input`` 状态并挂起（RUNNING →
WAITING_INPUT → RUNNING）；消息发送由上层（task_manager）注入的
*ask_callback* 回调完成。用户回复经 ``chat.receive.after_process`` Hook 唤醒。
无回复超时配置虽已声明但当前实现未读取，任务会一直保持挂起直至收到回复或被取消。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ...permission import Role
from ..registry import ToolDefinition

logger = logging.getLogger(__name__)


def build_ask_tool(
    ctx: object,
    *,
    ask_callback: Callable[[str, str], Awaitable[None]],
    min_role: Role = Role.USER,
) -> list[ToolDefinition]:
    """构建 ``ask_user`` 提问工具。

    Args:
        ctx: 插件上下文（可用但未直接使用 — 实际工作由注入的回调承担）。
        ask_callback: ``async def(stream_id, question)`` — 由上层注入。
            仅负责将问题发送到目标聊天流；挂起/等待/恢复的状态转换
            由 AgentLoop 的 ask_user 处理逻辑内部完成。
        min_role: 调用此工具所需的最低角色（默认 USER）。

    Returns:
        包含单个 ``ask_user`` ToolDefinition 的列表。
    """

    async def _handler(**kwargs: Any) -> dict:
        question: str = kwargs.get("question", "")
        stream_id: str = kwargs.get("stream_id", "")
        context: str = kwargs.get("context", "")

        # 调用入口（问题文本截断到前 80 字符，避免敏感内容刷屏）
        logger.debug(
            "ask_user 工具调用：stream_id=%r，问题 %.80r",
            stream_id,
            question,
        )

        # 参数校验（仅记录日志，不改变执行流程）
        if not question or not stream_id:
            logger.warning(
                "ask_user 参数校验失败：question=%.80r, stream_id=%r",
                question,
                stream_id,
            )

        # 将问题与可选上下文合并，让用户看到完整信息。
        full_question = question
        if context:
            full_question = f"{question}\n\n[上下文]\n{context}"

        try:
            await ask_callback(stream_id, full_question)
            # 提问发起成功（问题文本截断到前 80 字符）
            logger.info(
                "ask_user 提问发起成功：stream_id=%r，问题 %.80r",
                stream_id,
                full_question,
            )
            return {"success": True, "message": "已提问，等待用户回复"}
        except Exception as exc:
            logger.exception(
                "ask_user 提问发送失败：stream_id=%r，错误 %.80r",
                stream_id,
                exc,
            )
            return {"success": False, "error": str(exc)}

    description = (
        "向用户提问并等待回复，任务将进入等待状态。问题要清晰具体。"
        "使用本工具后 Agent 暂停执行，直到收到用户的回复后自动恢复。"
    )

    parameters: dict = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要提问的问题，需清晰具体，便于用户理解并回复",
            },
            "stream_id": {
                "type": "string",
                "description": "目标聊天流 ID（通常是任务所在的聊天流）",
            },
            "context": {
                "type": "string",
                "description": "补充上下文说明（可选），帮助用户更好理解问题背景",
            },
        },
        "required": ["question", "stream_id"],
    }

    # essential 级：Agent 循环中始终可见，无需经 list_tools 发现即可调用。
    ask_user = ToolDefinition(
        name="ask_user",
        description=description,
        parameters=parameters,
        handler=_handler,
        visibility="essential",
        min_role=min_role,
    )

    logger.debug(
        "ask_user 工具定义构建完成：name=%s, visibility=%s, min_role=%s",
        ask_user.name,
        ask_user.visibility,
        min_role.value,
    )

    return [ask_user]
