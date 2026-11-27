# Builds a written project map (modules, docstrings, functions, buttons) for the coder.
# Co-authored with CoCo
"""Project map for «🔧 Регенерация».

The coder must know «кто за что отвечает» across the whole bot before it
fixes anything — otherwise it invents non-existent APIs. This module walks
the ``bot/`` package and, using the AST (no imports, so it never executes
project code), extracts a compact written map:

* per-module: first line of the docstring (responsibility),
* top-level function / async-function / class names,
* inline-button callbacks + labels it registers (so the coder knows the UI).

The result is a plain-text digest meant to be dropped into the coder's
context. It is size-capped so it never blows the token budget.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["build_project_map", "repo_root"]

_MAX_CHARS = 12000
_SKIP_DIRS = {"__pycache__", ".git", ".venv", ".ruff_cache", "node_modules", "session"}


def repo_root() -> Path:
    """Repo root containing the ``bot/`` package.

    ``bot/addons/regen/project_map.py`` → parents[3] == repo root.
    """
    return Path(__file__).resolve().parents[3]


def _first_doc_line(node: ast.Module) -> str:
    doc = ast.get_docstring(node) or ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return ""


def _callback_strings(tree: ast.AST) -> list[str]:
    """Collect callback_data string literals (rough UI fingerprint)."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "callback_data":
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.append(v.value)
    return out


def _module_entry(path: Path, root: Path) -> str | None:
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)
    except Exception:
        return None
    rel = path.relative_to(root).as_posix()
    doc = _first_doc_line(tree)
    funcs: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(node.name)
        elif isinstance(node, ast.ClassDef):
            funcs.append(f"class {node.name}")
    cbs = sorted(set(_callback_strings(tree)))
    parts = [f"### {rel}"]
    if doc:
        parts.append(f"  {doc}")
    if funcs:
        parts.append("  fn: " + ", ".join(funcs[:25]))
    if cbs:
        parts.append("  cb: " + ", ".join(cbs[:20]))
    return "\n".join(parts)


def build_project_map(root: Path | None = None, *, max_chars: int = _MAX_CHARS) -> str:
    """Return a compact written map of every ``bot/`` module.

    Output is capped at ``max_chars`` (entries appended until the budget is
    spent), prioritising the ``bot/`` package root and addons.
    """
    root = root or repo_root()
    bot_dir = root / "bot"
    if not bot_dir.is_dir():
        return ""
    files: list[Path] = []
    for p in sorted(bot_dir.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        files.append(p)
    # Order: shallow modules first (core), then deeper (addons/workers).
    files.sort(key=lambda p: (len(p.relative_to(root).parts), p.as_posix()))

    header = (
        "# КАРТА ПРОЕКТА lilush (модуль → ответственность → функции → кнопки)\n"
        "# Используй её, чтобы НЕ выдумывать API: бери существующие функции.\n"
    )
    chunks = [header]
    size = len(header)
    truncated = False
    for path in files:
        entry = _module_entry(path, root)
        if not entry:
            continue
        if size + len(entry) + 2 > max_chars:
            truncated = True
            break
        chunks.append(entry)
        size += len(entry) + 2
    if truncated:
        chunks.append("# … (карта обрезана по лимиту контекста)")
    return "\n\n".join(chunks)
