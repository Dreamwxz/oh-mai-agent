"""oh_mai_agent.config.SubAgentConfig 的测试 — 默认值、ge=1 校验与 UI 元数据。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oh_mai_agent.config import MaibotAgentConfig, SubAgentConfig


class TestSubAgentConfig:
    def test_defaults(self) -> None:
        cfg = SubAgentConfig()
        assert cfg.enabled is True
        assert cfg.max_rounds == 10
        assert cfg.max_result_chars == 8000
        assert cfg.max_parallel_subagents == 3

    def test_disabled(self) -> None:
        cfg = SubAgentConfig(enabled=False)
        assert cfg.enabled is False

    def test_custom_values(self) -> None:
        cfg = SubAgentConfig(
            max_rounds=20,
            max_result_chars=16000,
            max_parallel_subagents=5,
        )
        assert cfg.max_rounds == 20
        assert cfg.max_result_chars == 16000
        assert cfg.max_parallel_subagents == 5

    @pytest.mark.parametrize("field", ["max_rounds", "max_parallel_subagents"])
    @pytest.mark.parametrize("value", [0, -1, -10])
    def test_ge1_validation(self, field: str, value: int) -> None:
        with pytest.raises(ValidationError):
            SubAgentConfig(**{field: value})

    def test_max_result_chars_no_ge_constraint(self) -> None:
        # max_result_chars 无 ge=1 约束，允许任意非负（含 0）配置
        cfg = SubAgentConfig(max_result_chars=0)
        assert cfg.max_result_chars == 0

    def test_ui_order(self) -> None:
        assert SubAgentConfig.__ui_order__ == 9

    def test_ui_label(self) -> None:
        assert SubAgentConfig.__ui_label__ == "子Agent"

    def test_fields_have_json_schema_extra(self) -> None:
        # maibot_sdk.Field 会把 json_schema_extra 拍平进 schema 的字段属性
        schema = SubAgentConfig.model_json_schema()["properties"]
        for name in ("enabled", "max_rounds", "max_result_chars", "max_parallel_subagents"):
            assert "label" in schema[name], name
            assert "hint" in schema[name], name
            assert "order" in schema[name], name


class TestMaibotAgentConfigSubagent:
    def test_defaults_via_parent(self) -> None:
        cfg = MaibotAgentConfig()
        assert isinstance(cfg.subagent, SubAgentConfig)
        assert cfg.subagent.enabled is True
        assert cfg.subagent.max_rounds == 10
        assert cfg.subagent.max_result_chars == 8000
        assert cfg.subagent.max_parallel_subagents == 3

    def test_custom_nested_config(self) -> None:
        cfg = MaibotAgentConfig(subagent=SubAgentConfig(enabled=False, max_rounds=5))
        assert cfg.subagent.enabled is False
        assert cfg.subagent.max_rounds == 5
        # 其余字段保持默认
        assert cfg.subagent.max_result_chars == 8000
        assert cfg.subagent.max_parallel_subagents == 3
