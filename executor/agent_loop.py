"""Agent 循环核心 — agent 任务执行引擎。

实现多轮 LLM 工具调用循环（``ctx.llm.generate_with_tools``）、
指令注入消费、waiting_input 挂起/恢复、以及结果持久化。

工具呈现：**直接全量**（与子 Agent 循环一致）——对当前角色可见的
essential 与 discoverable 工具每轮全部直接暴露给 LLM，不做
list_tools / get_tool_schema 的按需发现仪式，避免「发现结果与
可调用集不一致」导致的 tool-not-found 空转。

工具在呈现和执行两个环节均按调用者角色过滤。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Awaitable
from datetime import datetime
from typing import Any, TYPE_CHECKING

from ..bus.messages import CommandKind, EventKind, TaskCommand, TaskEvent
from ..permission import Role
from ..prompt.manager import PromptManager
from ..domain.task_record import TaskRecord, TaskStatus
from ..tools.registry import (
    ToolRegistry,
    ToolDefinition,
    build_llm_tool_schemas,
)
from ..tools.synthetic.discovery import (
    handle_list_tools,
    handle_get_tool_schema,
)

logger = logging.getLogger(__name__)

# ── 常量 ───────────────────────────────────────────────────────────────────

MAX_HISTORY_KEEP = 50
"""每个任务在数据库中保留的最大历史条目数。"""


class AgentLoop:
    """agent 任务执行引擎：多轮 LLM 工具调用循环。

    每个任务持有独立的 ``AgentLoop`` 实例，命令通过 ``command_bus`` 路由。

    用法::

         loop = AgentLoop(
             ctx=plugin_ctx,
             registry=tool_registry,
             store=task_store,
             command_bus=command_bus,
             on_ask=scheduler.on_ask_user,
             role_provider=lambda: Role.ADMIN,
         )
        await loop.run(task)
    """

    def __init__(
        self,
        *,
        ctx: Any,
        registry: ToolRegistry,
        store: Any,  # TaskStore – 避免循环导入
        on_ask: Callable[[str, str], Awaitable[None]] | None = None,
        max_rounds: int = 30,
        role_provider: Callable[[], Role] | None = None,
    send_final: Callable[[TaskRecord, str], Awaitable[None]] | None = None,
        prompt_manager: PromptManager | None = None,
        prompt_service: Any | None = None,
        command_bus: Any,
    ) -> None:
        """初始化 AgentLoop 实例。

        Args:
            ctx: SDK PluginContext（用于 LLM 调用等）。
            registry: 工具注册中心（含两级呈现）。
            store: 任务持久化存储（TaskStore）。
            on_ask: 可选 — ask_user 提问回调（向用户发送消息）。
            max_rounds: LLM 最大对话轮数（默认 30）。
            role_provider: 可选 — 角色解析回调，默认返回 GUEST。
            send_final: 可选 — 最终结果发送回调。
            prompt_manager: 可选 — PromptManager 实例（用于模板化提示词）。
            prompt_service: 可选 — PromptService 实例（builder 模式构建提示词）。
            command_bus: TaskCommandBus 实例（用于跨组件命令通信）。
        """
        self._ctx = ctx
        self._registry = registry
        self._store = store
        self._on_ask = on_ask
        self._max_rounds = max_rounds
        self._role_provider = role_provider
        self._send_final = send_final
        self._prompt_manager = prompt_manager
        self._prompt_service = prompt_service
        self._command_bus = command_bus

        # run() 期间赋值 — 供类方法使用
        self._task: TaskRecord | None = None

        # task_id → asyncio.Event 映射，用于 ask_user 挂起/恢复。
        # Event 必须放在这里而非 task.metadata 中，因为 metadata
        # 通过 json.dumps 序列化，Event 不可 JSON 序列化。
        self._resume_events: dict[str, asyncio.Event] = {}

        self._cancelled = False
        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()

        # 记录已通过 get_tool_schema 加载 schema 的 discoverable 工具，
        # 以便在后续轮次中将其包含在 tools 参数中。
        self._loaded_discoverable: set[str] = set()

    @property
    def is_cancelled(self) -> bool:
        """只读访问器：主循环是否已被取消（供子 Agent 循环传导取消状态）。"""
        return self._cancelled

    # ── 辅助函数 ───────────────────────────────────────────────────────

    def _get_role(self) -> Role:
        """解析当前调用者角色（默认 GUEST）。"""
        if self._role_provider is not None:
            return self._role_provider()
        return Role.GUEST

    def _build_tool_schemas(self, role: Role) -> list[dict[str, Any]]:
        """为单轮 LLM 调用构建完整的工具 schema 列表。

        Agent 循环采用**直接全量呈现**（与子 Agent 循环一致，见
        ``executor/subagent.py``）：对 *role* 可见的 essential 与 discoverable
        工具每轮全部直接暴露，不依赖 list_tools / get_tool_schema 的发现仪式。
        这样 LLM 看到的工具名即注册表真实名称（如 ``mcp_fetch_fetch``），
        不会出现「发现结果与可调用集不一致」导致的 tool-not-found 空转。

        ``list_tools`` / ``get_tool_schema`` 不再出现在 schema 中（避免噪音），
        但 handler 仍保留：历史会话恢复或 LLM 残余调用可得到友好响应。
        """
        schemas: list[dict[str, Any]] = []

        # Essential 工具
        essential = self._registry.list_essential(role)
        schemas.extend(build_llm_tool_schemas(essential))

        # 全部对角色可见的 discoverable 工具（直接暴露，不做按需发现）
        discoverable = self._registry.list_discoverable(role)
        schemas.extend(build_llm_tool_schemas(discoverable))

        # 已加载的 discoverable 工具（历史 get_tool_schema 会话兜底，避免重复）
        for name in sorted(self._loaded_discoverable):
            td = self._registry.get(name)
            if td is not None:
                schemas.append(td.to_llm_definition())

        return schemas

    # ── 内部工具处理器 ─────────────────────────────────────────────────

    async def _handle_list_tools(self, role: Role) -> dict[str, Any]:
        """返回对 *role* 可见的所有 discoverable 工具的名称和描述。

        兜底兼容：工具已全量直接暴露，正常情况下 LLM 无需调用本工具；
        历史会话恢复或 LLM 残余调用时返回真实工具清单，避免误导。
        """
        return await handle_list_tools(self._registry, role)

    async def _handle_get_tool_schema(
        self, role: Role, name: str
    ) -> dict[str, Any]:
        """返回 discoverable 工具的完整 LLM schema。

        兜底兼容：同 ``_handle_list_tools``，正常路径不再需要；
        该工具仍会被加入 ``_loaded_discoverable``，在后续轮次的
        tools 参数中包含（防御重复）。
        """
        return await handle_get_tool_schema(
            self._registry, self._loaded_discoverable, role, name
        )

    async def _handle_ask_user(
        self, task: TaskRecord, args: dict[str, Any]
    ) -> dict[str, Any]:
        """执行 ask_user 工具：挂起任务 → 等待用户回复 → 恢复。

        状态转换：RUNNING → WAITING_INPUT → RUNNING。
        挂起信号就绪后通过 on_ask 回调向用户发送问题；
        若未配置 on_ask 回调，直接恢复任务并返回错误，避免无限挂起。
        """
        question = args.get("question", "")
        if not question:
            return {"success": False, "error": "缺少必需参数: question"}

        resume_event: asyncio.Event = asyncio.Event()
        self._resume_events[task.id] = resume_event
        task.transition(TaskStatus.WAITING_INPUT)
        await self._store.save(task)

        # 回调在挂起信号就绪后调用（发消息给用户）
        # 唤醒不依赖事件广播：用户回复经 chat.receive.after_process Hook →
        # TaskControl.handle_user_reply → bus.send(RESUME_REPLY)，由主订阅
        # _on_bus_command 的 RESUME_REPLY 分支写入 _user_reply 并 set resume_event。
        if self._on_ask is not None:
            await self._on_ask(task.stream_id, question)
        else:
            # 无回调：无法提问，恢复任务并返回错误（避免无限挂起）
            logger.warning("任务 %s：ask_user 无 on_ask 回调，跳过提问", task.id)
            resume_event.set()

        logger.info("任务 %s 已挂起 waiting_input，等待用户输入（问题：%s）", task.id, question[:80])
        if self._cancelled:
            return {"success": False, "error": "cancelled"}
        await resume_event.wait()
        logger.info("任务 %s 已从 waiting_input 恢复", task.id)

        if self._cancelled:
            return {"success": False, "error": "cancelled"}

        task.transition(TaskStatus.RUNNING)
        await self._store.save(task)

        reply = task.take_user_reply()
        return {"success": True, "reply": reply}

    # ── 指令注入消费 ───────────────────────────────────────────────────

    def _build_injection_message(self, instruction: str) -> dict[str, Any]:
        """构建注入指令的 system 消息（运行与恢复回放共用）。"""
        return {
            "role": "system",
            "content": self._prompt_service.build("injection", instruction=instruction),
        }

    async def _consume_injections(
        self,
        task: TaskRecord,
        messages: list[dict[str, Any]],
    ) -> None:
        """消费所有待注入指令，将其作为 system 消息插入到 LLM 消息列表。

        经 ``task.take_injections()`` 弹出待注入指令（一次性清空）。
        """
        injections = task.take_injections()

        if not injections:
            return
        while injections:
            instruction = injections.pop(0)
            messages.append(self._build_injection_message(instruction))
            await self._store.append_history(task.id, {
                "type": "injection",
                "instruction": instruction,
                "timestamp": datetime.now().isoformat(),
            })

    async def _persist_round(
        self,
        task: TaskRecord,
        round_num: int,
        result: dict[str, Any],
        messages: list[dict[str, Any]],
        msgs_before: int,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """持久化单轮 LLM 调用条目：追加历史 + 更新水位 + 守卫保存任务快照。

        - 第 1 轮存完整消息列表（回放种子）；后续轮只存增量（``new_messages``）；
        - ``_last_history_id`` 记录持久化水位（审计 / 未来增量续传锚点）；
        - 守卫保存：仅当持久化记录未达终态时写快照，避免覆盖并发终态。

        Args:
            task: 当前任务。
            round_num: 本轮号。
            result: 本轮 LLM 结果 dict（取 ``response`` / ``tool_calls``）。
            messages: 完整消息列表（用于首轮快照与增量差分）。
            msgs_before: 本轮消息追加前的消息数（增量差分起点）。
            tool_calls: 本轮工具调用列表（收尾轮为 ``[]``）。
        """
        _entry: dict[str, Any] = {
            "round": round_num,
            "llm_result": {
                "response": result.get("response", ""),
                "tool_calls": tool_calls,
            },
            "timestamp": datetime.now().isoformat(),
        }
        if round_num == 1:
            _entry["messages"] = messages.copy()
        else:
            _entry["new_messages"] = messages[msgs_before:]
        _history_id = await self._store.append_history(task.id, _entry)
        task.set_last_history_id(_history_id)
        persisted = await self._store.get(task.id)
        if persisted is None or not persisted.is_terminal():
            await self._store.save(
                task,
                expected_status=(
                    persisted.status if persisted is not None else None
                ),
            )

    # ── 总线命令处理器 ────────────────────────────────────────────────

    async def _on_bus_command(self, cmd: Any) -> None:
        """命令总线监听器：处理 TaskCommand 的注入指令和恢复请求。"""
        if not isinstance(cmd, TaskCommand):
            return
        task = self._task
        if task is None:
            return

        if cmd.kind == CommandKind.INJECT_INSTRUCTION:
            instruction = cmd.payload.get("instruction", "")
            if instruction:
                task.push_injection(instruction)
                logger.info(
                    "总线注入指令到任务 %s：%s", task.id, instruction[:80],
                )
        elif cmd.kind == CommandKind.RESUME_REPLY:
            reply = cmd.payload.get("reply", "")
            task.set_user_reply(reply)
            resume_event = self._resume_events.get(task.id)
            if resume_event is not None:
                resume_event.set()
            logger.info("总线恢复指令：任务 %s 从 waiting_input 恢复", task.id)
        elif cmd.kind == CommandKind.CANCEL:
            self._cancelled = True
            resume_event = self._resume_events.get(task.id)
            if resume_event is not None:
                resume_event.set()
            if self._paused:
                self._pause_event.set()
        elif cmd.kind == CommandKind.PAUSE:
            self._paused = True
            self._pause_event.clear()
            # 暂停标记必须落在循环自有对象上：调度器 pause() 写在独立
            # 副本上，会被本循环轮次整记录保存覆盖；写回自有对象后
            # 后续轮次保存会保留该标记（resume/超时跳过依赖它）。
            task.set_coop_paused(True)
        elif cmd.kind == CommandKind.RESUME:
            self._paused = False
            self._pause_event.set()
            task.set_coop_paused(False)
            task.started_at = datetime.now()
            await self._store.save(task)

    async def _notify_completed(
        self, task: TaskRecord, *, success: bool, error: str = "",
    ) -> None:
        """通过命令总线发布任务完成事件。"""
        kind = EventKind.COMPLETED if success else EventKind.FAILED
        payload: dict[str, Any] = {}
        if error:
            payload["error"] = error
        await self._command_bus.publish(
            TaskEvent(task_id=task.id, kind=kind, payload=payload),
        )

    async def _finalize_cancelled(self, task: TaskRecord) -> None:
        guard_status: TaskStatus | None = None
        try:
            persisted = await self._store.get(task.id)
        except Exception as exc:
            logger.warning("任务 %s 取消收尾重新加载失败，使用内存状态：%s", task.id, exc)
            persisted = task
        else:
            guard_status = persisted.status if persisted is not None else None

        if persisted is not None and persisted.is_terminal():
            return

        task.force(TaskStatus.CANCELLED, actor="agent_loop", reason="cancelled_by_bus")
        try:
            saved = await self._store.save(task, expected_status=guard_status)
        except Exception as exc:
            logger.warning("任务 %s 取消收尾保存失败，继续发布事件：%s", task.id, exc)
            saved = True
        if not saved:
            # 守卫保存被原子拒绝：持久化记录已被并发终态（如超时 FAILED）
            # 覆盖。保持记录终态，不再广播 CANCELLED，避免记录/事件不一致。
            logger.warning(
                "任务 %s 取消收尾被并发终态拦截（持久化已非 %s），跳过 CANCELLED 事件",
                task.id, guard_status,
            )
            return
        try:
            await self._command_bus.publish(
                TaskEvent(task_id=task.id, kind=EventKind.CANCELLED),
            )
        except Exception as exc:
            logger.warning("任务 %s 取消事件发布失败：%s", task.id, exc)

    # ── 主循环 ─────────────────────────────────────────────────────────

    async def run(self, task: TaskRecord) -> None:
        """为 *task* 执行 agent 循环。

        1. 转入 RUNNING 状态，持久化。
        2. 构建 Agent 上下文（系统提示词 + 历史消息）。
        3. LLM 循环（最多 *max_rounds* 轮）：
           - 消费注入的指令。
           - 按角色过滤构建工具 schema。
           - 调用 ``ctx.llm.generate_with_tools``。
           - 若无 tool_calls → 循环结束（最终回复）。
           - 执行工具调用，将结果追加到消息列表，继续。
         4. 转入 COMPLETED（异常则 FAILED），持久化，并通过命令总线发布事件。
        """
        self._task = task
        self._command_bus.subscribe(task.id, self._on_bus_command)

        try:
            # ── 1. 进入 RUNNING ───────────────────────────────────
            logger.info("任务 %s 开始执行 Agent 循环（最大轮数：%d）", task.id, self._max_rounds)
            # 调度器（_try_start）可能已先将任务置为 RUNNING；
            # 已是 RUNNING 则跳过转换，避免 "running → running" 非法转换。
            if task.status != TaskStatus.RUNNING:
                task.transition(TaskStatus.RUNNING)
            task.started_at = datetime.now()
            await self._store.save(task)

            role = self._get_role()

            # ── 2. 构建 Agent 上下文 ──────────────────────────────
            system_prompt = self._prompt_service.build(
                "agent_system", task=task,
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt}
            ]

            # 加载持久化的历史消息（恢复之前执行的消息上下文）。
            # 从头回放全部已持久化条目，幂等重建对话上下文：
            # 第 1 轮条目是完整消息种子（替换），后续轮是增量
            # （new_messages 追加），injection 条目重建为 system 消息。
            # task.set_last_history_id() 记录持久化水位（审计 / 未来增量续传锚点）。
            for _entry in await self._store.get_history_after(task.id, 0):
                if _entry.get("type") == "injection":
                    instruction = _entry.get("instruction", "")
                    if instruction:
                        messages.append(self._build_injection_message(instruction))
                elif "messages" in _entry:
                    messages = _entry["messages"].copy()
                elif "new_messages" in _entry:
                    messages.extend(_entry["new_messages"])

            # ── 3. LLM 循环 ───────────────────────────────────────
            for round_num in range(1, self._max_rounds + 1):
                if self._cancelled:
                    break
                if self._paused:
                    await self._pause_event.wait()
                    if self._cancelled:
                        break
                # 消费本轮前注入的指令。
                await self._consume_injections(task, messages)

                # 构建本轮工具 schema（按角色过滤）。
                tool_schemas = self._build_tool_schemas(role)

                logger.debug(
                    "任务 %s 第 %d 轮调用 LLM（消息数：%d，工具 schema 数：%d）",
                    task.id, round_num, len(messages), len(tool_schemas),
                )

                # 调用 LLM（含工具）。
                result: dict[str, Any] = await self._ctx.llm.generate_with_tools(
                    prompt=messages,
                    tools=tool_schemas,
                    model="planner",
                    timeout_ms=240000,
                )
                if self._cancelled:
                    break

                # 记录追加本轮消息前的消息数，作为增量差分起点（本轮注入
                # 消息已计入，assistant/tool 结果消息尚未追加）。
                _msgs_before: int = len(messages)

                # ── 检查工具调用 ──────────────────────
                tool_calls: list[dict[str, Any]] = result.get("tool_calls", [])
                if not tool_calls:
                    # Agent 产出了最终回复 — 循环结束。
                    messages.append({
                        "role": "assistant",
                        "content": result.get("response", ""),
                    })
                    # ── 持久化本轮条目（收尾轮无工具调用）──
                    await self._persist_round(
                        task, round_num, result, messages, _msgs_before, [],
                    )
                    logger.info(
                        "任务 %s 已完成，共 %d 轮（无工具调用）",
                        task.id, round_num,
                    )
                    break

                # ── 记录 assistant 消息（含工具调用）────
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": result.get("response", "") or None,
                    "tool_calls": tool_calls,
                }
                # 清理 None 字段，保持输出整洁。
                assistant_msg = {k: v for k, v in assistant_msg.items() if v is not None}
                messages.append(assistant_msg)

                # ── 执行每个工具调用 ─────────────────────
                for call in tool_calls:
                    func: dict[str, Any] = call.get("function", {})
                    name: str = func.get("name", "")
                    args_str: str | dict[str, Any] = func.get("arguments", "{}")

                    # 解析工具参数（防御式编程）。
                    if isinstance(args_str, dict):
                        args: dict[str, Any] = args_str
                    else:
                        try:
                            args = json.loads(args_str)
                        except (json.JSONDecodeError, TypeError):
                            logger.warning(
                                "任务 %s：解析工具 %s 的参数失败：%s",
                                task.id, name, args_str[:80],
                            )
                            args = {}

                    # ── 工具分发 ────────────────────────────
                    if name == "list_tools":
                        tool_result = await self._handle_list_tools(role)
                    elif name == "get_tool_schema":
                        tool_result = await self._handle_get_tool_schema(
                            role, args.get("name", "")
                        )
                    elif name == "ask_user":
                        tool_result = await self._handle_ask_user(task, args)
                    else:
                        # LLM 可能在 arguments 中夹带保留参数名（name/role），
                        # 直接解包会导致 execute(name, role, **args) 关键字
                        # 重复绑定抛 TypeError，这里剥离保留键。
                        call_args = {
                            k: v for k, v in args.items()
                            if k not in ("name", "role")
                        }
                        tool_result = await self._registry.execute(
                            name, role, **call_args
                        )

                    logger.debug(
                        "任务 %s 工具 %s 执行结果：%s",
                        task.id, name,
                        json.dumps(tool_result, ensure_ascii=False)[:80],
                    )

                    # ── 追加工具结果消息 ──────────
                    tc_id: str = call.get("id", "")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    })

                # ── 持久化本轮条目 ────────────────
                await self._persist_round(
                    task, round_num, result, messages, _msgs_before, tool_calls,
                )

            else:
                # 超出最大轮数限制
                logger.info(
                    "任务 %s 达到最大轮数 %d，强制结束",
                    task.id, self._max_rounds,
                )
                messages.append({
                    "role": "assistant",
                    "content": "任务已达到最大轮数限制，已强制结束。请检查任务是否过于复杂。",
                })

            # ── 4. 收尾 ─────────────────────────────────────────────
            if self._cancelled:
                await self._finalize_cancelled(task)
                return

            # 发送最终结果（Agent 的响应）
            if self._send_final is not None:
                final_response = ""
                if len(messages) > 1:
                    last_msg = messages[-1]
                    if last_msg.get("role") == "assistant":
                        final_response = last_msg.get("content", "") or ""
                if not final_response:
                    final_response = "任务完成"
                await self._send_final(task, final_response)

            # send_final 耗时窗口（润色 LLM ≤120s + 重试）内可能收到
            # CANCEL 或调度器超时 FAILED：重新检查取消标记与持久化
            # 终态，避免在终态记录上覆盖 COMPLETED（含 CRON 复活）。
            if self._cancelled:
                await self._finalize_cancelled(task)
                return
            persisted = await self._store.get(task.id)
            if persisted is not None and persisted.is_terminal():
                logger.info(
                    "任务 %s 完成前发现持久化已为终态 %s，跳过完成",
                    task.id, persisted.status.value,
                )
                return
            task.transition(TaskStatus.COMPLETED)
            saved = await self._store.save(
                task,
                expected_status=persisted.status if persisted is not None else None,
            )
            if not saved:
                logger.warning(
                    "任务 %s 完成保存被并发终态拦截，跳过 COMPLETED 事件", task.id,
                )
                return
            await self._notify_completed(task, success=True)

        except Exception as exc:
            if self._cancelled:
                await self._finalize_cancelled(task)
                return
            logger.exception("任务 %s 执行异常失败", task.id)
            # 记录错误信息（send_final 发送失败消息时读取）。
            task.set_error(str(exc))
            if self._send_final is not None:
                await self._send_final(task, f"任务失败：{exc}")
            try:
                task.transition(TaskStatus.FAILED)
            except Exception:
                # 若已处于终态，transition 可能抛出异常。
                task.force(TaskStatus.FAILED, actor="agent_loop", reason=str(exc))
            await self._store.save(task)
            await self._notify_completed(task, success=False, error=str(exc))

        finally:
            logger.info("任务 %s Agent 循环退出", task.id)
            self._resume_events.pop(task.id, None)
            self._task = None
            self._command_bus.unsubscribe(task.id)
