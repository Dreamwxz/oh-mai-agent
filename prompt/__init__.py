"""提示词构建子系统 — builder 模式统一入口。

对外暴露：``PromptService``（build()）、
``PromptContext`` / ``PromptBuilder`` 基础类型。
"""

from .base import PromptBuilder, PromptContext
from .manager import PromptManager, PromptTemplate
from .service import PromptService

__all__ = [
    "PromptBuilder",
    "PromptContext",
    "PromptManager",
    "PromptService",
    "PromptTemplate",
]
