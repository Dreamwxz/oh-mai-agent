"""oh_mai_agent.prompt 的测试——subagent_system 模板 + SubAgentSystemBuilder。"""

from __future__ import annotations

import pytest
from conftest import make_task  # noqa: F401  (与 test_prompt_builders 一致的夹具风格)

from oh_mai_agent.prompt.base import PromptContext
from oh_mai_agent.prompt.builders import ALL_BUILDERS
from oh_mai_agent.prompt.builders.subagent_system import SubAgentSystemBuilder
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
# SubAgentSystemBuilder — 子 Agent 系统提示词
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubAgentSystemBuilder:
    def test_build_without_pm_raises(self) -> None:
        builder = SubAgentSystemBuilder()
        with pytest.raises(RuntimeError, match="PromptManager 未注入"):
            builder.build(PromptContext(data={"intent": "查天气"}))

    def test_via_prompt_service_renders_intent_and_tool_list(self, svc: PromptService) -> None:
        prompt = svc.build(
            "subagent_system",
            intent="查天气",
            tool_list="- search_web: 网页搜索",
        )
        assert "查天气" in prompt
        assert "search_web" in prompt
        assert "<intent>" in prompt and "</intent>" in prompt
        assert "<tools>" in prompt and "</tools>" in prompt
        # 保留底本核心节结构 + 新增检索工具说明节
        assert "ROLE" in prompt
        assert "COOP" in prompt
        assert "COMPLETION" in prompt
        assert "检索工具说明" in prompt
        # yield 协议已被替换为本架构语义
        assert "yield" not in prompt
        assert "{{" not in prompt

    def test_xml_escape_intent(self, svc: PromptService) -> None:
        prompt = svc.build(
            "subagent_system",
            intent="查天气<明天>",
            tool_list="- search_web: 网页搜索",
        )
        assert "&lt;明天&gt;" in prompt
        assert "<明天>" not in prompt

    def test_xml_escape_tool_list(self, svc: PromptService) -> None:
        prompt = svc.build(
            "subagent_system",
            intent="查天气",
            tool_list="- search_web: 网页搜索<&",
        )
        assert "&lt;" in prompt and "&amp;" in prompt

    def test_missing_variables_raises(self, pm: PromptManager) -> None:
        # PromptManager 校验声明变量：缺任一声明变量（intent/tool_list）即抛 ValueError
        with pytest.raises(ValueError, match="requires variables"):
            pm.render("subagent_system")

    def test_defaults_to_empty_when_missing_via_builder(self, svc: PromptService) -> None:
        # builder 侧缺省空串兜底：声明变量始终有值，渲染不抛错
        prompt = svc.build("subagent_system")
        assert "<intent>" in prompt
        assert "<tools>" in prompt
        assert "{{" not in prompt

    def test_bot_name_override(self, svc: PromptService) -> None:
        """bot_name 传入时替换检索工具说明中的硬编码昵称。"""
        prompt = svc.build(
            "subagent_system",
            intent="查天气",
            tool_list="- search_web: 网页搜索",
            bot_name="小美",
        )
        assert "检索小美的长期记忆" in prompt
        assert "麦麦" not in prompt

    def test_registered_in_all_builders(self) -> None:
        assert any(b.name == "subagent_system" for b in ALL_BUILDERS)
