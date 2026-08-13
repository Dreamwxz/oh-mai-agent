"""提示词模板管理器。

从目录中加载提示词模板（带 index.json 清单），
通过 Jinja2 渲染模板，使用 StrictUndefined 防止未声明变量静默变空串。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jinja2

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 领域类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """单个命名模板的元数据。"""
    name: str
    variables: frozenset[str]


@dataclass(frozen=True, slots=True)
class PromptSnapshot:
    """所有模板的时间点快照。"""
    templates: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 提示词管理器
# ---------------------------------------------------------------------------


class PromptManager:
    """管理命名提示词模板，支持懒加载内容与缓存。

    Args:
        templates_dir: 模板文件目录路径。
    """

    def __init__(self, templates_dir: str | Path) -> None:
        # resolve() 归一为绝对路径，作为 index.json 相对 path 的基准目录
        self._templates_dir = Path(templates_dir).resolve()

        # 加载索引清单：index.json 即模板注册表，声明每个模板的 path 与 variables
        index_path = self._templates_dir / "index.json"
        if not index_path.is_file():
            raise FileNotFoundError(f"index.json not found in {self._templates_dir}")
        with open(index_path, encoding="utf-8") as fh:
            self._index: dict[str, Any] = json.load(fh)

        # 构建模板元数据：variables 转为 frozenset，保证不可变且可哈希
        self._templates: dict[str, PromptTemplate] = {}
        for name, entry in self._index.get("templates", {}).items():
            vars_set = frozenset(entry["variables"])
            self._templates[name] = PromptTemplate(
                name=name,
                variables=vars_set,
            )

        # 内容缓存：{name: text_content}，命中后不再重复读盘
        self._content_cache: dict[str, str] = {}

        # Jinja2 编译模板缓存：{name: jinja2.Template}
        # 在 _load_content 中与内容缓存一起懒加载编译，
        # 避免每次 render 重复编译。
        self._template_cache: dict[str, jinja2.Template] = {}

        logger.info(
            "提示词管理器初始化完成：模板目录=%s，共注册 %d 个模板",
            self._templates_dir,
            len(self._templates),
        )

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def render(self, name: str, **data: str) -> str:
        """渲染命名模板，代入给定变量值。

        Args:
            name: 模板名称（必须在 index.json 中存在）。
            **data: 用于替换 ``{{var}}`` 占位符的变量值。

        Returns:
            渲染后的提示词字符串。

        Raises:
            ValueError: 若 **data 中缺少声明的变量，
                        或提供了未声明的额外变量。
            KeyError: 若模板名未知。
        """
        tmpl = self._templates.get(name)
        if tmpl is None:
            logger.warning("渲染未知模板：name=%s", name)
            raise KeyError(f"Unknown template: {name}")

        logger.debug(
            "渲染提示词模板：name=%s，参数键=%s",
            name,
            sorted(data.keys()),
        )

        # 根据声明变量验证输入
        provided = set(data.keys())
        if not tmpl.variables.issubset(provided):
            missing = tmpl.variables - provided
            raise ValueError(
                f"Template '{name}' requires variables {sorted(tmpl.variables)}; "
                f"missing: {sorted(missing)}"
            )
        if not provided.issubset(tmpl.variables):
            extra = provided - tmpl.variables
            raise ValueError(
                f"Template '{name}' does not declare variables: {sorted(extra)}"
            )

        # 触发懒加载：确保内容缓存和编译模板缓存都已就绪
        self._load_content(name)

        # Jinja2 渲染（StrictUndefined + autoescape=False）
        # StrictUndefined：模板中引用了 index.json 未声明变量时抛 UndefinedError，
        # 而非静默输出空串；autoescape=False 防止对提示词内容进行 HTML 转义。
        tmpl_obj = self._template_cache[name]
        result = tmpl_obj.render(**data)
        return result

    def snapshot(self) -> PromptSnapshot:
        """返回所有模板内容的快照。

        强制加载全部模板（而非仅已渲染过的），
        按 (template_name → content) 打包，供调用方保存或传递。
        """
        all_templates: dict[str, str] = {}
        for name in self._templates:
            all_templates[name] = self._load_content(name)
        return PromptSnapshot(templates=all_templates)

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _load_content(self, name: str) -> str:
        """懒加载模板内容并缓存，同时编译 Jinja2 模板对象。

        Args:
            name: 模板名称。

        Returns:
            模板文件原始文本内容。

        Raises:
            ValueError: 若 index.json 中未配置该模板路径。
            FileNotFoundError: 若模板文件不存在。
        """
        if name not in self._content_cache:
            rel_path = self._index["templates"][name]["path"]
            # 防御性检查：index.json 条目未声明 path 或 path 为空
            if not rel_path:
                logger.error("模板未配置路径：name=%s", name)
                raise ValueError(f"No path configured for template '{name}'")
            full_path = self._templates_dir / rel_path
            if not full_path.is_file():
                logger.error("模板文件不存在：name=%s，path=%s", name, full_path)
                raise FileNotFoundError(f"Template file not found: {full_path}")
            raw_text = full_path.read_text(encoding="utf-8")
            self._content_cache[name] = raw_text
            # 编译 Jinja2 模板并缓存：StrictUndefined 防止未声明变量静默变空串，
            # autoescape=False 防止对提示词内容进行 HTML 转义。
            self._template_cache[name] = jinja2.Template(
                raw_text,
                undefined=jinja2.StrictUndefined,
                autoescape=False,
            )
            logger.debug(
                "模板内容加载并编译：name=%s，path=%s，长度=%d 字符",
                name,
                full_path,
                len(raw_text),
            )
        return self._content_cache[name]
