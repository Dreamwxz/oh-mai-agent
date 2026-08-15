"""MaiBot Agent 命令处理模块 — commands.py。

承载 /maitask 命令组的 11 个模块级函数，从 plugin.py 逐字搬迁。
所有 `self.` 引用替换为 `plugin` 参数或模块级调用。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from .domain.status_formatter import StatusFormatter
from .domain.stream_ref import Owner, is_group_stream, platform_of
from .domain.task_record import TaskRecord, TaskStatus
from .permission import PermissionResolver, Role

if TYPE_CHECKING:
    from .plugin import MaibotAgentPlugin

logger = logging.getLogger(__name__)


# ── 命令参数提取辅助 ────────────────────────────────────────────────

def cmd_text(**kwargs: Any) -> str:
    """提取完整命令消息文本（兼容 text / plain_text 两种键名）。

    MaiBot 命令执行器传 text（processed_plain_text），但部分旧代码用 plain_text。
    优先取 text，回退 plain_text。
    """
    return str(kwargs.get("text") or kwargs.get("plain_text") or "")


def cmd_arg(kwargs: dict[str, Any], index: int, default: str = "") -> str:
    """从 matched_groups 提取第 index 个正则组；缺失则返回 default。

    matched_groups 可能是 {0: 全文, 1: 第一组...} 或 {group_name: ...}。
    当正则组不存在时返回 default，调用方自行回退到文本解析。
    """
    groups = kwargs.get("matched_groups") or {}
    val = groups.get(index) if isinstance(groups, dict) else None
    if val is not None:
        return str(val)
    return default


# ── 命令角色解析辅助 ─────────────────────────────────────────────────

def resolve_caller(plugin: "MaibotAgentPlugin", **kwargs: Any) -> tuple[Role, str, str, str, bool]:
    """从命令 kwargs 提取并解析调用者信息。

    Returns:
        (role, owner, stream_id, platform, is_group)
    """
    stream_id = str(kwargs.get("stream_id", ""))
    user_id = str(kwargs.get("user_id", ""))
    platform = str(kwargs.get("platform", ""))

    # 从 stream_id 推断 platform
    if not platform:
        platform = platform_of(stream_id)

    # 群聊判定：stream_id 含 ":group:" 段（格式 platform:group:group_id）
    is_group = is_group_stream(stream_id)

    role = plugin.resolver.resolve_role(
        platform=platform,
        user_id=user_id,
        stream_id=stream_id,
        is_group=is_group,
    )
    owner = Owner.join(platform, user_id) if user_id else f"unknown:{stream_id}"
    return role, owner, stream_id, platform, is_group


# ── 命令内部实现 ──────────────────────────────────────────────────────

async def cmd_reply(plugin: "MaibotAgentPlugin", stream_id: str, response: str) -> None:
    """发送命令响应到目标聊天流（直发出口，不润色）。

    命令是排障/操作场景，需要确定性输出（任务 ID、状态等关键信息不能被
    改写），经 ``ReplySender.send_raw`` 直发原文 —— 仍获得指数退避重试
    与静默掉包检测的可靠性保障，但不写入 MaiBot 上下文。
    """
    try:
        await plugin.task_manager.sender.send_raw(response, stream_id)
    except Exception:
        plugin.ctx.logger.warning(
            "命令响应发送失败 stream=%s: %s", stream_id, response[:50], exc_info=True
        )


async def cmd_create(plugin: "MaibotAgentPlugin", **kwargs: Any) -> tuple[bool, str, int]:
    """处理 /maitask create <意图描述>."""
    role, owner, stream_id, platform, _ = resolve_caller(plugin, **kwargs)
    logger.debug("处理 /maitask create 命令 owner=%s stream=%s", owner, stream_id)
    if not PermissionResolver.require(role, Role.USER):
        logger.info("拒绝 /maitask create：%s 角色权限不足 owner=%s", role.value, owner)
        reply = "权限不足：需要用户及以上权限才能创建任务"
        await cmd_reply(plugin, stream_id, reply)
        return False, reply, 2

    text = cmd_text(**kwargs)
    # 提取 "/maitask create " 之后的内容
    intent = re.sub(r"^/maitask\s+create\s*", "", text, count=1).strip()
    if not intent:
        logger.warning("/maitask create 缺少意图描述 owner=%s", owner)
        reply = "用法: /maitask create <意图描述>"
        await cmd_reply(plugin, stream_id, reply)
        return False, reply, 2

    ok, result = await plugin.task_manager.create_task(
        intent=intent,
        owner=owner,
        platform=platform,
        stream_id=stream_id,
        caller_role=role,
    )
    if ok and isinstance(result, TaskRecord):
        sfmt = StatusFormatter()
        reply = (
            f"任务已创建！\n"
            f"ID: {result.id[:8]}\n"
            f"标题: {result.title}\n"
            f"级别: {result.level.value}\n"
            f"状态: {sfmt.format(*result.status_info())}"
        )
        await cmd_reply(plugin, stream_id, reply)
        return True, reply, 2
    logger.warning("创建任务失败 owner=%s intent=%s: %s", owner, intent[:60], result)
    reply = f"创建失败: {str(result)}"
    await cmd_reply(plugin, stream_id, reply)
    return False, reply, 2


async def cmd_list(plugin: "MaibotAgentPlugin", **kwargs: Any) -> tuple[bool, str, int]:
    """处理 /maitask list [-all] [状态]."""
    role, owner, stream_id, _, _ = resolve_caller(plugin, **kwargs)
    logger.debug("处理 /maitask list 命令 owner=%s stream=%s", owner, stream_id)

    text = cmd_text(**kwargs)
    # 提取状态参数
    status_str = re.sub(r"^/maitask\s+list\s*", "", text, count=1).strip()

    # 解析 -all 标志（非 TaskStatus，可出现在 status 前或后）
    all_flag = False
    tokens = status_str.split()
    if "-all" in tokens:
        all_flag = True
        tokens = [t for t in tokens if t != "-all"]
    status_str = " ".join(tokens).strip()

    status: TaskStatus | None = None
    if status_str:
        try:
            status = TaskStatus(status_str)
        except ValueError:
            logger.warning("/maitask list 收到无效状态 %s", status_str)
            reply = f"无效状态: {status_str}，可选: pending/running/waiting_input/completed/failed/cancelled/scheduled/paused"
            await cmd_reply(plugin, stream_id, reply)
            return False, reply, 2

    # ADMIN + -all → 查看全部任务（含 planner 任务）；非 ADMIN 静默忽略 -all
    if all_flag and role == Role.ADMIN:
        owner = ""

    tasks = await plugin.task_manager.list_tasks(
        caller_role=role,
        owner=owner,
        status=status,
        limit=20,
    )
    if not tasks:
        reply = "当前没有任务。"
        await cmd_reply(plugin, stream_id, reply)
        return True, reply, 2

    lines: list[str] = [f"共 {len(tasks)} 个任务:"]
    for t in tasks:
        lines.append(
            f"  [{t['id'][:8]}] {t['level']}/{t['status']} "
            f"{t['title']} — {t['format_status']}"
        )
    reply = "\n".join(lines)
    await cmd_reply(plugin, stream_id, reply)
    return True, reply, 2


async def cmd_status(plugin: "MaibotAgentPlugin", **kwargs: Any) -> tuple[bool, str, int]:
    """处理 /maitask status <id>."""
    role, owner, stream_id, _, _ = resolve_caller(plugin, **kwargs)
    logger.debug("处理 /maitask status 命令 owner=%s stream=%s", owner, stream_id)

    task_id = cmd_arg(kwargs, 1)
    if not task_id:
        # 无匹配正则组时，回退到整段文本提取任务 ID
        text = cmd_text(**kwargs)
        task_id = re.sub(r"^/maitask\s+status\s*", "", text, count=1).strip()
    if not task_id:
        logger.warning("/maitask status 缺少任务 ID owner=%s", owner)
        reply = "用法: /maitask status <任务ID>"
        await cmd_reply(plugin, stream_id, reply)
        return False, reply, 2

    # 尝试完整 ID 或前缀匹配
    ok, result = await plugin.task_manager.get_task(
        task_id=task_id,
        caller_role=role,
        owner=owner,
    )
    if ok and isinstance(result, TaskRecord):
        sfmt = StatusFormatter()
        reply = (
            "任务详情:\n"
            f"  ID: {result.id}\n"
            f"  标题: {result.title}\n"
            f"  意图: {result.intent[:100]}\n"
            f"  级别: {result.level.value}\n"
            f"  状态: {sfmt.format(*result.status_info())}\n"
            f"  所有者: {result.owner}\n"
            f"  创建时间: {result.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
        await cmd_reply(plugin, stream_id, reply)
        return True, reply, 2
    logger.warning("查询任务 %s 失败: %s", task_id, result)
    reply = f"查询失败: {str(result)}"
    await cmd_reply(plugin, stream_id, reply)
    return False, reply, 2


async def cmd_cancel(plugin: "MaibotAgentPlugin", **kwargs: Any) -> tuple[bool, str, int]:
    """处理 /maitask cancel <id>."""
    role, owner, stream_id, _, _ = resolve_caller(plugin, **kwargs)
    logger.debug("处理 /maitask cancel 命令 owner=%s stream=%s", owner, stream_id)

    task_id = cmd_arg(kwargs, 1)
    if not task_id:
        # 无匹配正则组时，回退到整段文本提取任务 ID
        text = cmd_text(**kwargs)
        task_id = re.sub(r"^/maitask\s+cancel\s*", "", text, count=1).strip()
    if not task_id:
        logger.warning("/maitask cancel 缺少任务 ID owner=%s", owner)
        reply = "用法: /maitask cancel <任务ID>"
        await cmd_reply(plugin, stream_id, reply)
        return False, reply, 2

    ok, msg = await plugin.task_manager.cancel_task(
        task_id=task_id,
        caller_role=role,
        owner=owner,
    )
    if ok:
        reply = f"任务 {task_id[:8]} 已取消"
        await cmd_reply(plugin, stream_id, reply)
        return True, reply, 2
    logger.warning("取消任务 %s 失败: %s", task_id, msg)
    reply = f"取消失败: {msg}"
    await cmd_reply(plugin, stream_id, reply)
    return False, reply, 2


async def cmd_history(plugin: "MaibotAgentPlugin", **kwargs: Any) -> tuple[bool, str, int]:
    """处理 /maitask history [<id>]."""
    role, owner, stream_id, _, _ = resolve_caller(plugin, **kwargs)
    logger.debug("处理 /maitask history 命令 owner=%s stream=%s", owner, stream_id)

    task_id = cmd_arg(kwargs, 1)
    if not task_id:
        # 无匹配正则组时，回退到整段文本提取任务 ID
        text = cmd_text(**kwargs)
        task_id = re.sub(r"^/maitask\s+history\s*", "", text, count=1).strip()

    if not task_id:
        logger.warning("/maitask history 缺少任务 ID owner=%s", owner)
        reply = "用法: /maitask history <任务ID>"
        await cmd_reply(plugin, stream_id, reply)
        return False, reply, 2

    ok, result = await plugin.task_manager.task_history(
        task_id=task_id,
        caller_role=role,
        owner=owner,
        limit=20,
    )
    if ok:
        if not result:
            reply = "该任务暂无历史记录。"
            await cmd_reply(plugin, stream_id, reply)
            return True, reply, 2
        lines: list[str] = [
            f"任务 {task_id[:8]} 执行历史（最近 {len(result)} 条）:"
        ]
        # 仅展示最近 10 条，避免回复过长
        for i, entry in enumerate(result[-10:], 1):
            entry_type = entry.get("type", "unknown")
            entry_ts = entry.get("timestamp", "")
            entry_summary = str(entry)[:80]
            lines.append(f"  {i}. [{entry_type}] {entry_summary}")
        reply = "\n".join(lines)
        await cmd_reply(plugin, stream_id, reply)
        return True, reply, 2
    logger.warning("查询任务 %s 历史失败: %s", task_id, result)
    reply = f"查询历史失败: {str(result)}"
    await cmd_reply(plugin, stream_id, reply)
    return False, reply, 2


async def cmd_ask(plugin: "MaibotAgentPlugin", **kwargs: Any) -> tuple[bool, str, int]:
    """处理 /maitask ask <id> <指令>."""
    role, owner, stream_id, _, _ = resolve_caller(plugin, **kwargs)
    logger.debug("处理 /maitask ask 命令 owner=%s stream=%s", owner, stream_id)

    task_id = cmd_arg(kwargs, 1)
    instruction = cmd_arg(kwargs, 2)
    if not task_id or not instruction:
        # 回退到文本解析（兼容无正则组场景）
        text = cmd_text(**kwargs)
        rest = re.sub(r"^/maitask\s+ask\s*", "", text, count=1).strip()
        parts = rest.split(None, 1)
        if len(parts) < 2:
            logger.warning("/maitask ask 参数不完整 owner=%s", owner)
            reply = "用法: /maitask ask <任务ID> <指令文本>"
            await cmd_reply(plugin, stream_id, reply)
            return False, reply, 2
        task_id, instruction = parts[0], parts[1]

    ok, msg = await plugin.task_manager.modify_task(
        task_id=task_id,
        caller_role=role,
        owner=owner,
        inject_instruction=instruction,
    )
    if ok:
        reply = f"指令已注入任务 {task_id[:8]}..."
        await cmd_reply(plugin, stream_id, reply)
        return True, reply, 2
    logger.warning("向任务 %s 注入指令失败: %s", task_id, msg)
    reply = f"注入失败: {msg}"
    await cmd_reply(plugin, stream_id, reply)
    return False, reply, 2


# ── 兜底命令 ──────────────────────────────────────────────────────

async def cmd_fallback(plugin: "MaibotAgentPlugin", **kwargs: Any) -> tuple[bool, str, int]:
    """兜底：任何 /maitask 开头的输入都显示帮助并拦截，避免落入 Maisaka planner。"""
    _, _, stream_id, _, _ = resolve_caller(plugin, **kwargs)
    logger.debug("处理 /maitask 兜底命令 stream=%s", stream_id)
    reply = (
        "maitask 命令用法：\n"
        "  /maitask create <意图>       创建任务\n"
        "  /maitask list [状态]         列出任务\n"
        "  /maitask status <ID>         查看任务详情\n"
        "  /maitask cancel <ID>         取消任务\n"
        "  /maitask history <ID>        查看执行历史\n"
        "  /maitask ask <ID> <指令>     向任务注入指令\n"
        "输入 /maitask help 查看此帮助"
    )
    await cmd_reply(plugin, stream_id, reply)
    return True, reply, 2
