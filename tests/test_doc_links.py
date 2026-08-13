"""文档引用完整性检查：docs 中引用的 .py 文件必须存在、行号不越界。

防漂移机制：任何 `git mv` / 删除 .py 文件后若文档未同步更新，本测试会红。
只校验「含目录的路径」（必须含一个 `/`），裸文件名引用（如 connection.py:473）
无法可靠解析，不校验（已知债，不清理）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 扫描目标：当前文档（排除 docs/history/ 归档；prompt-style-guide.md 不含 .py 引用）
DOC_FILES = sorted(
    list((REPO_ROOT / "docs" / "features").glob("*.md"))
    + [REPO_ROOT / "docs" / "LIFECYCLE.md", REPO_ROOT / "AGENTS.md", REPO_ROOT / "README.md"]
)

# 含目录的代码路径引用：path.py / path.py:NNN / path.py:NNN-MMM
# 必须含至少一个 `/`；不匹配裸文件名。lookbehind 阻止 `plugin.py/lifecycle.py`
# 这类「路径列表」中从 `.py` 后缀中途开始匹配（如误匹配 py/lifecycle.py）
_PATH_REF = re.compile(r"(?<![a-zA-Z0-9_./])([a-zA-Z_][a-zA-Z0-9_]*/[a-zA-Z0-9_/]*\.py)(?::(\d+)(?:-(\d+))?)?")
# 裸文件名（仅统计报告用）
_BARE_REF = re.compile(r"(?<![a-zA-Z0-9_/])([a-zA-Z_][a-zA-Z0-9_]*\.py)")


def _iter_path_refs(text: str):
    for m in _PATH_REF.finditer(text):
        path = m.group(1)
        start = int(m.group(2)) if m.group(2) else None
        end = int(m.group(3)) if m.group(3) else None
        yield path, start, end


def _is_ignored(path: str) -> bool:
    return (
        path.startswith("docs/history/")
        or path.startswith(".venv/")
        or path.startswith("__pycache__/")
        or path.startswith("tests/")
        or path.startswith("node_modules/")
    )


@pytest.mark.parametrize("doc_file", DOC_FILES, ids=lambda p: p.name)
def test_doc_path_refs_resolve(doc_file: Path) -> None:
    """文档中每个含目录的 .py 路径引用必须指向存在的文件，行号必须在文件长度内。"""
    text = doc_file.read_text(encoding="utf-8")
    failures: list[str] = []
    for path, start, end in _iter_path_refs(text):
        if _is_ignored(path):
            continue
        target = REPO_ROOT / path
        if not target.is_file():
            failures.append(f"{doc_file.name}: 引用的文件不存在: {path}")
            continue
        line_count = len(target.read_text(encoding="utf-8").splitlines())
        for lineno in (start, end):
            if lineno is not None and not (1 <= lineno <= line_count):
                failures.append(
                    f"{doc_file.name}: 行号越界 {path}:{lineno}（文件共 {line_count} 行）"
                )
    assert not failures, "\n".join(failures)
