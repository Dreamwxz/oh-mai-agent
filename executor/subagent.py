"""子 Agent 执行循环 — 主 Agent 分派局部工作的轻量嵌套循环。

``SubAgentLoop`` 在进程内运行（不新增子进程、不持久化、不走命令总线、
无 ask_user）：每轮经 ``ctx.llm.generate_with_tools(model="planner")``
调用 LLM；一轮内的多个工具调用经 ``asyncio.gather`` 并发执行，结果
按调用顺序追加；无工具调用的一轮即产出最终答案。

安全边界（防 LLM 幻觉 / 提示词注入）：
  - 每轮固定使用初始工具集 schema，无 list_tools / get_tool_schema
    动态发现；
  - 每个工具调用执行前校验其名称在允许工具集内，命中被排除工具名
    直接返回错误 dict，绝不落入 registry.execute。

取消传导：``should_cancel`` 由调用方注入——AgentLoop 合成分支
（``agent_loop.py`` 的 ``_make_subagent_loop``）注入 ``lambda: self.is_cancelled``，
主循环取消即子循环在每轮开始前与并行 gather 前退出（不经 ContextVar 间接层）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..permission import Role
from ..tools.registry import ToolDefinition, build_llm_tool_schemas

logger = logging.getLogger(__name__)

_TRUNCATE_SUFFIX = "…（已截断）"


class SubAgentLoop:
    """子 Agent 循环：固定工具集的多轮 LLM 工具调用，结果原样回传调用方。

    用法::

        loop = SubAgentLoop(
            ctx=plugin_ctx,
            tools=tool_definitions,
            role=Role.USER,
            prompt_service=prompt_service,
            max_rounds=cfg.max_rounds,
            max_result_chars=cfg.max_result_chars,
            should_cancel=lambda: main_loop.is_cancelled,
            execute_tool=_exec,  # async def _exec(name, role, args) -> dict
        )
        result = await loop.run(intent)
    """

    def __init__(
        self,
        ctx: Any,
        tools: list[ToolDefinition],
        role: Role,
        prompt_service: Any,
        max_rounds: int,
        max_result_chars: int,
        should_cancel: Callable[[], bool] | None = None,
        llm_timeout_ms: int = 240000,
        execute_tool: Callable[[str, Role, dict], Awaitable[dict]] | None = None,
    ) -> None:
        """初始化子 Agent 循环。

        Args:
            ctx: SDK PluginContext（用于 LLM 调用）。
            tools: 固定工具集（每轮 schema 均由此构建，无动态发现）。
            role: 子 Agent 工具调用者角色（继承自当前任务）。
            prompt_service: PromptService 实例（渲染 subagent_system 提示词）。
            max_rounds: LLM 最大轮数。
            max_result_chars: 最终答案最大字符数，超长截断。
            should_cancel: 可选 — 取消判定回调，命中则提前返回
                ``{"success": False, "error": "cancelled", ...}``。
            llm_timeout_ms: 单轮 LLM 调用超时（毫秒）。
            execute_tool: 可选 — 工具执行适配器
                ``async def execute_tool(name, role, args) -> dict``，
                由调用方注入（registry.execute 是 **kwargs 签名，须解包）。
                缺省 None 时工具调用返回 "execute_tool not configured" 错误。
        """
        self._ctx = ctx
        self._tools = tools
        self._role = role
        self._prompt_service = prompt_service
        self._max_rounds = max_rounds
        self._max_result_chars = max_result_chars
        self._should_cancel = should_cancel
        self._llm_timeout_ms = llm_timeout_ms
        self._execute_tool = execute_tool

    # ── 主循环 ─────────────────────────────────────────────────────────

    async def run(self, intent: str) -> dict[str, Any]:
        """为 *intent* 执行子 Agent 循环。

        返回 dict 固定携带 5 个键：
        ``{"success", "answer", "rounds", "max_rounds_reached", "error"}``。

        - 无 tool_calls 的轮 → 该轮 response 即最终答案。
        - max_rounds 耗尽（含 max_rounds=1 且该轮全为 tool_calls）→
          取最后一轮 assistant content（可能为空串）、
          ``max_rounds_reached=True``、success 仍为 True。
        - ``should_cancel`` 命中 → 提前返回 cancelled。
        - LLM 调用抛异常 → success False、rounds 0。
        """
        system_prompt = self._prompt_service.build(
            "subagent_system",
            intent=intent,
            tool_list="\n".join(
                f"- {tool.name}: {tool.description}" for tool in self._tools
            ),
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        allowed_names: set[str] = {tool.name for tool in self._tools}
        tool_schemas: list[dict[str, Any]] = build_llm_tool_schemas(self._tools)

        for round_num in range(1, self._max_rounds + 1):
            # 轮首取消检查
            if self._should_cancel is not None and self._should_cancel():
                return self._cancelled_result(round_num - 1)

            try:
                result: dict[str, Any] = await self._ctx.llm.generate_with_tools(
                    prompt=messages,
                    tools=tool_schemas,
                    model="planner",
                    timeout_ms=self._llm_timeout_ms,
                )
            except Exception as exc:
                logger.warning("子 Agent 第 %d 轮 LLM 调用异常：%s", round_num, exc)
                return {
                    "success": False,
                    "answer": "",
                    "rounds": 0,
                    "max_rounds_reached": False,
                    "error": str(exc),
                }

            tool_calls: list[dict[str, Any]] = result.get("tool_calls") or []
            if not tool_calls:
                # 无工具调用 → 该轮响应即最终答案。
                return {
                    "success": True,
                    "answer": self._truncate(str(result.get("response", ""))),
                    "rounds": round_num,
                    "max_rounds_reached": False,
                    "error": None,
                }

            # 记录 assistant 消息（含工具调用）。
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result.get("response", "") or None,
                "tool_calls": tool_calls,
            }
            assistant_msg = {k: v for k, v in assistant_msg.items() if v is not None}
            messages.append(assistant_msg)

            # gather 前取消检查
            if self._should_cancel is not None and self._should_cancel():
                return self._cancelled_result(round_num - 1)

            # 一轮内多个工具调用并行执行；每个 _exec_call 自行全捕获，
            # gather 永不抛异常（不使用 return_exceptions）。
            results: list[dict[str, Any]] = await asyncio.gather(
                *(self._exec_call(call, allowed_names) for call in tool_calls)
            )

            # 按 tool_calls 原始顺序追加工具结果消息（gather 保序）。
            for call, tool_result in zip(tool_calls, results):
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(tool_result, ensure_ascii=False),
                })

        # max_rounds 耗尽：取最后一轮 assistant content（可能为空串）。
        last_assistant = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                last_assistant = msg.get("content", "") or ""
                break
        return {
            "success": True,
            "answer": self._truncate(last_assistant),
            "rounds": self._max_rounds,
            "max_rounds_reached": True,
            "error": None,
        }

    # ── 内部工具处理器 ─────────────────────────────────────────────────

    async def _exec_call(
        self, call: dict[str, Any], allowed_names: set[str]
    ) -> dict[str, Any]:
        """执行单个工具调用（含执行守卫与全捕获）。

        守卫：调用名不在 *allowed_names* 内 → 返回错误 dict、
        绝不调用 execute_tool（防幻觉/注入执行被排除工具）。
        任何异常都被捕获为错误 dict，保证 gather 永不抛异常。
        """
        func: dict[str, Any] = call.get("function", {})
        name: str = func.get("name", "")
        if name not in allowed_names:
            logger.warning("子 Agent 执行守卫拒绝越权工具调用：%s", name)
            return {"success": False, "error": f"tool not in allowed set: {name}"}

        args_str: str | dict[str, Any] = func.get("arguments", "{}")
        if isinstance(args_str, dict):
            args: dict[str, Any] = args_str
        else:
            try:
                args = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "子 Agent 解析工具 %s 的参数失败：%s", name, str(args_str)[:80],
                )
                args = {}

        if self._execute_tool is None:
            return {"success": False, "error": "execute_tool not configured"}

        try:
            return await self._execute_tool(name, self._role, args)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ── 辅助 ───────────────────────────────────────────────────────────

    def _cancelled_result(self, rounds_run: int) -> dict[str, Any]:
        """取消命中时的提前返回（携带全部 5 个键）。"""
        return {
            "success": False,
            "answer": "",
            "rounds": rounds_run,
            "max_rounds_reached": False,
            "error": "cancelled",
        }

    def _truncate(self, answer: str) -> str:
        """按 ``max_result_chars`` 截断答案，超长追加截断标记。"""
        if len(answer) > self._max_result_chars:
            return answer[: self._max_result_chars] + _TRUNCATE_SUFFIX
        return answer
