"""oh_mai_agent.prompt.builders 的测试——全部 7 个 builder + PromptService 集成。"""

from __future__ import annotations

import pytest
from conftest import make_task

from oh_mai_agent.prompt.base import PromptContext
from oh_mai_agent.prompt.builders import ALL_BUILDERS
from oh_mai_agent.prompt.builders.agent_system import AgentSystemBuilder
from oh_mai_agent.prompt.builders.context_note import ContextNoteBuilder
from oh_mai_agent.prompt.builders.injection import InjectionMessageBuilder
from oh_mai_agent.prompt.builders.planner_board import PlannerBoardBuilder
from oh_mai_agent.prompt.builders.polish import PolishBuilder
from oh_mai_agent.prompt.builders.title import TitleBuilder
from oh_mai_agent.prompt.manager import PromptManager
from oh_mai_agent.prompt.service import PromptService


# ═══════════════════════════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def pm() -> PromptManager:
    from pathlib import Path
    return PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates")


@pytest.fixture
def svc(pm: PromptManager) -> PromptService:
    return PromptService(manager=pm, builders=ALL_BUILDERS)


# ═══════════════════════════════════════════════════════════════════════════════
# AgentSystemBuilder — Agent 系统提示词
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentSystemBuilder:
    def test_build_without_pm_raises(self) -> None:
        task = make_task("t1", title="查询天气", intent="查询北京明天的天气")
        builder = AgentSystemBuilder()
        with pytest.raises(RuntimeError, match="PromptManager 未注入"):
            builder.build(PromptContext(task=task))

    def test_with_pm_renders_template(self, pm: PromptManager) -> None:
        task = make_task("t1", title="测试任务", intent="写一个测试")
        builder = AgentSystemBuilder(pm=pm)
        prompt = builder.build(PromptContext(task=task))
        assert "测试任务" in prompt
        assert "写一个测试" in prompt
        assert "你是 MaiBot 的离线任务 Agent" in prompt
        assert "{{" not in prompt
        assert "plugin_injected_instruction" in prompt
        assert "plugin_context_note" in prompt

    def test_via_prompt_service(self, svc: PromptService) -> None:
        task = make_task("t1", title="T", intent="I")
        prompt = svc.build("agent_system", task=task)
        assert "T" in prompt
        assert "I" in prompt

    def test_bot_name_defaults_to_maimei(self, pm: PromptManager) -> None:
        """bot_name 缺省时兜底"麦麦"（与 MaiBot nickname 默认值一致）。"""
        task = make_task("t1", title="T", intent="I")
        prompt = AgentSystemBuilder(pm=pm).build(PromptContext(task=task))
        assert "你是麦麦（MaiBot）的一部分" in prompt
        assert "符合麦麦的风格" in prompt

    def test_bot_name_override(self, pm: PromptManager) -> None:
        """bot_name 传入时替换模板中的硬编码昵称。"""
        task = make_task("t1", title="T", intent="I")
        prompt = AgentSystemBuilder(pm=pm).build(
            PromptContext(task=task, data={"bot_name": "小美"})
        )
        assert "你是小美（MaiBot）的一部分" in prompt
        assert "符合小美的风格" in prompt
        assert "麦麦" not in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# TitleBuilder — 任务标题
# ═══════════════════════════════════════════════════════════════════════════════

class TestTitleBuilder:
    def test_build_without_pm_raises(self) -> None:
        builder = TitleBuilder()
        with pytest.raises(RuntimeError, match="PromptManager 未注入"):
            builder.build(PromptContext(data={"intent": "查天气"}))

    def test_with_pm_renders_template(self, pm: PromptManager) -> None:
        builder = TitleBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={"intent": "查天气"}))
        assert "查天气" in prompt
        assert "15 字以内" in prompt
        assert "{{" not in prompt

    def test_via_prompt_service(self, svc: PromptService) -> None:
        prompt = svc.build("title", intent="测试意图")
        assert "测试意图" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# PolishBuilder — 回复润色
# ═══════════════════════════════════════════════════════════════════════════════

class TestPolishBuilder:
    def test_build_without_pm_raises(self) -> None:
        builder = PolishBuilder()
        with pytest.raises(RuntimeError, match="PromptManager 未注入"):
            builder.build(PromptContext(data={
                "jargon": [], "context": "", "result": "r",
            }))

    def test_with_jargon_and_context(self, pm: PromptManager) -> None:
        jargon = [{"content": "爷", "meaning": "厉害"}]
        builder = PolishBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "jargon": jargon, "context": "聊天上下文", "result": "结果",
        }))
        assert "聊天上下文" in prompt
        assert "结果" in prompt
        assert "爷" in prompt

    def test_no_jargon(self, pm: PromptManager) -> None:
        builder = PolishBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "jargon": [], "context": "ctx", "result": "result",
        }))
        assert "（无）" in prompt

    def test_empty_context(self, pm: PromptManager) -> None:
        builder = PolishBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "jargon": [], "context": "", "result": "r",
        }))
        assert "无最近聊天记录" in prompt

    def test_empty_result(self, pm: PromptManager) -> None:
        builder = PolishBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "jargon": [], "context": "ctx", "result": "",
        }))
        assert "{{result}}" not in prompt

    def test_with_pm_renders_template(self, pm: PromptManager) -> None:
        builder = PolishBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "jargon": [], "context": "上下文", "result": "结果",
        }))
        assert "上下文" in prompt
        assert "结果" in prompt
        assert "{{" not in prompt

    def test_via_prompt_service(self, svc: PromptService) -> None:
        prompt = svc.build("polish", jargon=[], context="C", result="R")
        assert "C" in prompt
        assert "R" in prompt

    def test_requester_triggers_relay_block(self, pm: PromptManager) -> None:
        """requester 非空（relay_from 传入）时，输出包含转达纪律并点名委托人。"""
        builder = PolishBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "jargon": [], "context": "ctx", "result": "r",
            "requester": "张三",
        }))
        assert "委托人" in prompt
        assert "张三" in prompt
        assert "由 张三 委托" in prompt
        assert "我帮你转达" in prompt

    def test_default_requester_empty_omits_relay_block(self, pm: PromptManager) -> None:
        """requester 缺省（本人发言）时，输出不含转达纪律块。"""
        builder = PolishBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "jargon": [], "context": "ctx", "result": "r",
        }))
        assert "委托人" not in prompt
        assert "转达纪律" not in prompt

    def test_bot_name_override(self, pm: PromptManager) -> None:
        """bot_name 传入时替换开场与默认人格行中的硬编码昵称。"""
        builder = PolishBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "jargon": [], "context": "ctx", "result": "r",
            "bot_name": "小美",
        }))
        assert "你是小美，现在请你读读" in prompt
        assert "保持小美的人格" in prompt
        assert "麦麦" not in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# PlannerBoardBuilder — Planner 看板
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlannerBoardBuilder:
    def test_empty_when_no_intro_and_no_waiting(self) -> None:
        builder = PlannerBoardBuilder()
        prompt = builder.build(PromptContext(data={
            "session_id": "s1", "show_intro": False, "waiting": [],
        }))
        assert prompt == ""

    def test_intro_section(self, pm: PromptManager) -> None:
        builder = PlannerBoardBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "session_id": "s1", "show_intro": True, "waiting": [],
        }))
        assert '<plugin_intro session="s1">' in prompt
        assert "</plugin_intro>" in prompt
        assert "后台子代理" in prompt
        assert "task_board" not in prompt

    def test_waiting_tasks_section(self, pm: PromptManager) -> None:
        from oh_mai_agent.domain.task_record import TaskStatus
        task = make_task("t1", title="等待回复任务", status=TaskStatus.WAITING_INPUT)
        builder = PlannerBoardBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "session_id": "s1",
            "show_intro": False,
            "waiting": [task],
        }))
        assert '<task_board session="s1">' in prompt
        assert "</task_board>" in prompt
        assert "waiting_input" in prompt
        assert "等待回复任务" in prompt
        assert "plugin_intro" not in prompt

    def test_intro_and_waiting_together(self, pm: PromptManager) -> None:
        from oh_mai_agent.domain.task_record import TaskStatus
        task = make_task("t1", title="等待回复任务", status=TaskStatus.WAITING_INPUT)
        builder = PlannerBoardBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "session_id": "s1",
            "show_intro": True,
            "waiting": [task],
        }))
        assert "plugin_intro" in prompt
        assert "task_board" in prompt

    def test_board_lines_include_short_id(self, pm: PromptManager) -> None:
        """看板每行应带 [id:xxxx] 短 ID，供 Planner 复制到 subagent_status 等工具。"""
        from oh_mai_agent.domain.task_record import TaskStatus
        task = make_task("abcdef12-3456-7890-abcd-ef1234567890", title="系统环境检查",
                         status=TaskStatus.WAITING_INPUT)
        builder = PlannerBoardBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "session_id": "s1",
            "show_intro": False,
            "waiting": [task],
        }))
        assert "[id:abcdef12]" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# InjectionMessageBuilder — 注入消息
# ═══════════════════════════════════════════════════════════════════════════════

class TestInjectionMessageBuilder:
    def test_includes_instruction(self, pm: PromptManager) -> None:
        builder = InjectionMessageBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={"instruction": "请处理优先任务"}))
        assert "请处理优先任务" in prompt
        assert "用户/管理者注入了新指令" in prompt
        assert "请优先处理" in prompt
        assert "<plugin_injected_instruction" in prompt
        assert "</plugin_injected_instruction>" in prompt
        assert "plugin_id=\"oh-mai-agent\"" in prompt

    def test_task_id_passthrough(self, pm: PromptManager) -> None:
        builder = InjectionMessageBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={"instruction": "x", "task_id": "abc"}))
        assert 'id="abc"' in prompt

    def test_generates_id_when_no_task_id(self, pm: PromptManager) -> None:
        builder = InjectionMessageBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={"instruction": "x"}))
        assert 'id="oh-mai-agent:inject:' in prompt

    def test_via_prompt_service(self, svc: PromptService) -> None:
        prompt = svc.build("injection", instruction="紧急指令")
        assert "紧急指令" in prompt
        assert "请优先处理" in prompt
        assert "<plugin_injected_instruction" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# ContextNoteBuilder — 上下文注释
# ═══════════════════════════════════════════════════════════════════════════════

class TestContextNoteBuilder:
    def test_sent_message_format(self, pm: PromptManager) -> None:
        builder = ContextNoteBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "kind": "sent-message",
            "content": "你好世界",
        }))
        assert "你好世界" in prompt
        assert "麦麦在此流发送了消息" in prompt
        assert "<plugin_context_note" in prompt
        assert "</plugin_context_note>" in prompt
        assert 'kind="sent-message"' in prompt
        assert "不是聊天对象发言" in prompt

    def test_task_reply_format(self, pm: PromptManager) -> None:
        builder = ContextNoteBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "kind": "task-reply",
            "content": "查询天气任务",
        }))
        assert "查询天气任务" in prompt
        assert "麦麦此前在此流发送了任务消息" in prompt
        assert 'kind="task-reply"' in prompt

    def test_task_waiting_format(self, pm: PromptManager) -> None:
        builder = ContextNoteBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "kind": "task-waiting",
            "title": "确认输出格式",
            "question": "请选择输出格式：JSON 还是表格？",
        }))
        assert 'kind="task-waiting"' in prompt
        assert "确认输出格式" in prompt
        assert "请选择输出格式：JSON 还是表格？" in prompt
        assert "正在等待用户回复" in prompt

    def test_task_waiting_escapes_title_and_question(self, pm: PromptManager) -> None:
        """task-waiting 的 title/question 与 content 一样做 XML 转义。"""
        builder = ContextNoteBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "kind": "task-waiting",
            "title": "</plugin_context_note> hack",
            "question": "</plugin_context_note> hack2",
        }))
        assert "&lt;/plugin_context_note&gt;" in prompt
        assert prompt.count("</plugin_context_note>") == 1

    def test_escape_xml_close_tag_in_content(self, pm: PromptManager) -> None:
        builder = ContextNoteBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "kind": "sent-message",
            "content": "</plugin_context_note> hack",
        }))
        assert "&lt;/plugin_context_note&gt;" in prompt
        assert prompt.count("</plugin_context_note>") == 1

    def test_auto_id_has_prefix(self, pm: PromptManager) -> None:
        builder = ContextNoteBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "kind": "sent-message",
            "content": "hello",
        }))
        assert 'id="oh-mai-agent:note:' in prompt

    def test_explicit_id_passthrough(self, pm: PromptManager) -> None:
        builder = ContextNoteBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "kind": "sent-message",
            "content": "hello",
            "id": "custom:42",
        }))
        assert 'id="custom:42"' in prompt
        assert '"oh-mai-agent:note:' not in prompt

    def test_missing_kind_raises(self, pm: PromptManager) -> None:
        builder = ContextNoteBuilder(pm=pm)
        with pytest.raises(ValueError, match="kind"):
            builder.build(PromptContext(data={"content": "x"}))

    def test_invalid_kind_raises(self, pm: PromptManager) -> None:
        builder = ContextNoteBuilder(pm=pm)
        with pytest.raises(ValueError, match="kind"):
            builder.build(PromptContext(data={
                "kind": "unknown",
                "content": "x",
            }))

    def test_via_prompt_service(self, svc: PromptService) -> None:
        prompt = svc.build("context_note", kind="sent-message", content="你好")
        assert "你好" in prompt
        assert "麦麦在此流发送了消息" in prompt
        assert "<plugin_context_note" in prompt

    def test_bot_name_override(self, pm: PromptManager) -> None:
        """bot_name 传入时替换两种注释格式中的硬编码昵称。"""
        builder = ContextNoteBuilder(pm=pm)
        sent = builder.build(PromptContext(data={
            "kind": "sent-message", "content": "你好", "bot_name": "小美",
        }))
        reply = builder.build(PromptContext(data={
            "kind": "task-reply", "content": "你好", "bot_name": "小美",
        }))
        assert "小美在此流发送了消息" in sent
        assert "小美此前在此流发送了任务消息" in reply
        assert "麦麦" not in sent
        assert "麦麦" not in reply

    def test_bot_name_escaped(self, pm: PromptManager) -> None:
        """bot_name 进入 XML 块文本，须与 content 同样转义。"""
        builder = ContextNoteBuilder(pm=pm)
        prompt = builder.build(PromptContext(data={
            "kind": "task-reply", "content": "x", "bot_name": "小<美>&",
        }))
        assert "小&lt;美&gt;&amp;" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# PromptService — 集成测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestPromptService:
    def test_build_unknown_builder_raises(self, svc: PromptService) -> None:
        with pytest.raises(KeyError, match="Unknown prompt builder"):
            svc.build("nonexistent")

    def test_all_builders_registered(self, svc: PromptService) -> None:
        names = set(svc.builders.keys())
        assert names == {"agent_system", "title", "polish", "planner_board", "injection", "context_note", "subagent_system"}

    def test_no_manager_builders_have_pm_injected(self, pm: PromptManager) -> None:
        from oh_mai_agent.prompt.builders import (
            AgentSystemBuilder, InjectionMessageBuilder,
            PlannerBoardBuilder, PolishBuilder, TitleBuilder,
        )
        fresh = [
            AgentSystemBuilder(),
            TitleBuilder(),
            PolishBuilder(),
            PlannerBoardBuilder(),
            InjectionMessageBuilder(),
        ]
        svc = PromptService(manager=pm, builders=fresh)
        for builder in svc.builders.values():
            assert builder._pm is pm

    def test_plain_service_build_without_manager_raises(self) -> None:
        from oh_mai_agent.prompt.builders import TitleBuilder
        svc = PromptService(manager=None, builders=[TitleBuilder()])
        with pytest.raises(RuntimeError, match="PromptManager 未注入"):
            svc.build("title", intent="测试")
