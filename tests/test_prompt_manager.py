"""prompt_manager.py 的测试——PromptManager + PromptTemplate + PromptSnapshot。"""

from __future__ import annotations

import json
import textwrap

import jinja2
import pytest

from oh_mai_agent.prompt.manager import PromptManager, PromptSnapshot, PromptTemplate


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def mgr() -> PromptManager:
    """返回一个绑定到真实 prompts/ 目录的 PromptManager。"""
    from pathlib import Path

    return PromptManager(Path(__file__).resolve().parent.parent / "prompt" / "templates")


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestRender:
    """正常路径渲染测试。"""

    def test_render_agent_system(self, mgr: PromptManager) -> None:
        result = mgr.render("agent_system", title="测试任务", intent="写一个测试")
        assert "测试任务" in result
        assert "写一个测试" in result
        assert "你是 MaiBot 的离线任务 Agent" in result
        assert "{{" not in result

    def test_render_polish(self, mgr: PromptManager) -> None:
        result = mgr.render(
            "polish",
            context="[群聊记录: 用户A说了你好]",
            jargon="黑话1, 黑话2",
            result="这是原始回复内容",
            kind="reply",
            requester="",
        )
        assert "用户A说了你好" in result
        assert "黑话1, 黑话2" in result
        assert "这是原始回复内容" in result
        assert "{{" not in result

    def test_render_title(self, mgr: PromptManager) -> None:
        result = mgr.render("title", intent="帮我查一下明天上海的天气")
        assert "帮我查一下明天上海的天气" in result
        assert "15 字以内" in result
        assert "{{" not in result

    def test_render_classify_level(self, mgr: PromptManager) -> None:
        result = mgr.render("classify_level", intent="帮我定时每天早上8点发送天气")
        assert "帮我定时每天早上8点发送天气" in result
        assert "instant" in result
        assert "{{" not in result


class TestValidation:
    """输入校验——缺失变量与多余变量。"""

    def test_missing_variable_raises(self, mgr: PromptManager) -> None:
        with pytest.raises(ValueError, match="missing"):
            mgr.render("agent_system", title="测试任务")

    def test_extra_undeclared_variable_raises(self, mgr: PromptManager) -> None:
        with pytest.raises(ValueError, match="does not declare"):
            mgr.render("agent_system", title="t", intent="i", extra_var="x")

    def test_unknown_template_raises(self, mgr: PromptManager) -> None:
        with pytest.raises(KeyError, match="nonexistent"):
            mgr.render("nonexistent", title="t", intent="i")

    def test_missing_all_variables_raises(self, mgr: PromptManager) -> None:
        with pytest.raises(ValueError, match="missing"):
            mgr.render("polish")


class TestSnapshot:
    """PromptSnapshot 接口（P4 预留）。"""

    def test_snapshot_contains_all_templates(self, mgr: PromptManager) -> None:
        snap = mgr.snapshot()
        assert isinstance(snap, PromptSnapshot)
        assert set(snap.templates.keys()) == {"agent_system", "classify_level", "title", "polish", "planner_board", "injection", "context_note", "subagent_system"}
        for name, content in snap.templates.items():
            assert isinstance(content, str)
            assert len(content) > 0


class TestPromptTemplate:
    """PromptTemplate 数据类。"""

    def test_frozen(self) -> None:
        pt = PromptTemplate(name="test", variables=frozenset({"a", "b"}))
        with pytest.raises(Exception):  # FrozenInstanceError 或类似异常
            pt.name = "other"  # type: ignore[misc]


class TestAllPlacholdersReplaced:
    """确保每次渲染都会替换所有 {{var}} 占位符。"""

    def test_all_templates_render_fully(self, mgr: PromptManager) -> None:
        test_data = {
            "agent_system": {"title": "T", "intent": "I"},
            "classify_level": {"intent": "I"},
            "title": {"intent": "I"},
            "polish": {"context": "C", "jargon": "J", "result": "R", "kind": "reply", "requester": ""},
        }
        for name, data in test_data.items():
            result = mgr.render(name, **data)
            assert "{{" not in result, f"{name} has unreplaced placeholder"
            assert len(result) > 0, f"{name} is empty"


# ---------------------------------------------------------------------------
# Jinja2 渲染行为测试
# ---------------------------------------------------------------------------


@pytest.fixture
def jinja2_mgr(tmp_path):
    """创建一个 PromptManager，加载临时目录中的自定义模板。"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    index = {"templates": {}}

    def add_template(name: str, content: str, variables: list[str]) -> None:
        (templates_dir / f"{name}.md").write_text(textwrap.dedent(content), encoding="utf-8")
        index["templates"][name] = {"path": f"{name}.md", "variables": variables}

    add_template(
        "ghost_template",
        """
        Hello {{ghost}}
        """,
        [],
    )
    add_template(
        "cond_template",
        """
        {% if extra %}ON{% else %}OFF{% endif %}
        """,
        ["extra"],
    )
    (templates_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return PromptManager(templates_dir)


class TestJinja2Render:
    """验证 Jinja2 渲染行为：StrictUndefined 拦截与条件语法支持。"""

    def test_render_undeclared_var_raises(self, jinja2_mgr: PromptManager) -> None:
        """模板含 index.json 未声明的 {{ghost}}，StrictUndefined 应抛出 UndefinedError。"""
        with pytest.raises(jinja2.UndefinedError, match="ghost"):
            jinja2_mgr.render("ghost_template")

    def test_render_jinja2_conditional(self, jinja2_mgr: PromptManager) -> None:
        """模板中 {% if %} 条件语法正常工作。"""
        assert jinja2_mgr.render("cond_template", extra="yes").strip() == "ON"
        assert jinja2_mgr.render("cond_template", extra="").strip() == "OFF"
