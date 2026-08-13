"""收集并暴露全部提示词构建器实例。

``ALL_BUILDERS`` 为 ``list[PromptBuilder]``，供 ``PromptService`` 初始化时注册。
"""

from .agent_system import AgentSystemBuilder
from .classify_level import ClassifyLevelBuilder
from .context_note import ContextNoteBuilder
from .injection import InjectionMessageBuilder
from .planner_board import PlannerBoardBuilder
from .polish import PolishBuilder
from .subagent_system import SubAgentSystemBuilder
from .title import TitleBuilder

ALL_BUILDERS: list = [
    AgentSystemBuilder(),
    ClassifyLevelBuilder(),
    TitleBuilder(),
    PolishBuilder(),
    PlannerBoardBuilder(),
    InjectionMessageBuilder(),
    ContextNoteBuilder(),
    SubAgentSystemBuilder(),
]
