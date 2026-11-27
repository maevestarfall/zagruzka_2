"""Core version-diff logic — pure stdlib (zipfile + ast), no aiogram, testable.

For each project archive we build a map ``{relative_module_path -> {object_qualname -> normalized_source}}``
and diff every version against a chosen reference archive. ``normalized_source``
is ``ast.unparse`` of the node, so formatting/comment-only changes do NOT count
as "изменилась" — only real code changes do.
"""

from __future__ import annotations

import ast
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path


# ---- extraction ----------------------------------------------------------


def _strip_root(name: str) -> str:
    """Drop the archive's top-level folder so modules align across versions.

    ``lilush-bot-v17.18/bot/x.py`` and ``lilush-bot-clean/bot/x.py`` both
    normalise to ``bot/x.py``.
    """
    parts = name.replace("\\", "/").split("/")
    if len(parts) > 1:
        return "/".join(parts[1:])
    return name


def _collect_objects(tree: ast.AST) -> dict[str, tuple[str, str]]:
    """Return ``{qualname: (kind, normalized_source)}`` for funcs/methods/classes."""
    out: dict[str, tuple[str, str]] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                kind = "метод" if prefix else "функция"
                try:
                    src = ast.unparse(child)
                except Exception:  # noqa: BLE001
                    src = ""
                out[qual] = (kind, src)
                # nested functions/classes
                walk(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                qual = f"{prefix}{child.name}"
                try:
                    # class signature without bodies of methods would be noisy;
                    # use the full unparse so renamed/added methods mark it changed too.
                    src = ast.unparse(child)
                except Exception:  # noqa: BLE001
                    src = ""
                out[qual] = ("класс", src)
                walk(child, f"{qual}.")

    walk(tree, "")
    return out


def extract_zip_objects(zip_path: str | Path) -> dict[str, dict[str, tuple[str, str]]]:
    """Map ``{relative_py_path -> {qualname -> (kind, normalized_source)}}``."""
    result: dict[str, dict[str, tuple[str, str]]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.endswith(".py"):
                continue
            rel = _strip_root(info.filename)
            try:
                raw = zf.read(info.filename)
                tree = ast.parse(raw.decode("utf-8", errors="replace"))
            except (SyntaxError, ValueError):
                continue
            objs = _collect_objects(tree)
            if objs:
                result[rel] = objs
    return result


# ---- diff -----------------------------------------------------------------


STATUS_ADDED = "добавлена"
STATUS_REMOVED = "удалена"
STATUS_CHANGED = "изменилась"
STATUS_SAME = "без изменений"


@dataclass
class DiffRow:
    version: str       # zip filename (the compared version)
    module: str        # relative .py path
    obj: str           # qualified object name
    kind: str          # функция / метод / класс
    status: str        # one of STATUS_*


def diff_against_reference(
    version_label: str,
    version_objs: dict[str, dict[str, tuple[str, str]]],
    reference_objs: dict[str, dict[str, tuple[str, str]]],
    *,
    include_unchanged: bool = False,
) -> list[DiffRow]:
    """Compare one version's objects to the reference; return per-object rows."""
    rows: list[DiffRow] = []
    modules = set(version_objs) | set(reference_objs)
    for module in sorted(modules):
        v = version_objs.get(module, {})
        r = reference_objs.get(module, {})
        names = set(v) | set(r)
        for name in sorted(names):
            in_v, in_r = name in v, name in r
            kind = (v.get(name) or r.get(name))[0]
            if in_v and not in_r:
                status = STATUS_ADDED          # exists in this version, absent in reference
            elif in_r and not in_v:
                status = STATUS_REMOVED        # in reference but missing here
            elif v[name][1] != r[name][1]:
                status = STATUS_CHANGED
            else:
                status = STATUS_SAME
            if status == STATUS_SAME and not include_unchanged:
                continue
            rows.append(DiffRow(version_label, module, name, kind, status))
    return rows


# ---- table rendering ------------------------------------------------------


def render_csv(rows: list[DiffRow], reference_label: str) -> str:
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Версия (zip)", "Эталон", "Модуль", "Объект", "Тип", "Статус"])
    for r in rows:
        w.writerow([r.version, reference_label, r.module, r.obj, r.kind, r.status])
    return buf.getvalue()


def summarize(rows: list[DiffRow]) -> dict[str, dict[str, int]]:
    """Per-version counts of each status."""
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r.version, {}).setdefault(r.status, 0)
        out[r.version][r.status] += 1
    return out


# ---- version ordering -----------------------------------------------------


def version_key(filename: str) -> tuple:
    """Sort key from a name like ``lilush-bot-v17.18-...zip`` → (17, 18)."""
    import re
    m = re.search(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", filename)
    if not m:
        return (0, 0, 0, filename)
    nums = tuple(int(x) if x else 0 for x in m.groups())
    return nums + (filename,)
