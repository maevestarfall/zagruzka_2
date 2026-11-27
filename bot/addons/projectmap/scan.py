# Project-map scanner: build a function call-graph from source via AST.
# Co-authored with CoCo
"""Scan a project's Python source and produce a function-level graph.

Nodes  = functions (``module.func``).
Edges  = "one function calls another", preserving the ORDER of calls so
the web view can show «сначала → потом → к чему обращается».

Pure-stdlib (``ast``), offline, deterministic. Works on ANY project path,
not just lilush — the owner can point it at a cloned repo under /data.
"""

from __future__ import annotations

import ast
import os
import time
from typing import Any

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache", "data"}
_MAX_FILES = 2000


def _iter_py_files(root: str):
    count = 0
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dp, f)
                count += 1
                if count >= _MAX_FILES:
                    return


def _module_name(root: str, path: str) -> str:
    rel = os.path.relpath(path, root)
    rel = rel[:-3] if rel.endswith(".py") else rel
    parts = [p for p in rel.split(os.sep) if p and p != "__init__"]
    return ".".join(parts) if parts else os.path.basename(path)


def _called_names(node: ast.AST) -> list[str]:
    """Return the ordered list of simple call names inside a function body."""
    names: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            fn = n.func
            if isinstance(fn, ast.Name):
                names.append(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.append(fn.attr)
    return names


def scan_project(root: str) -> dict[str, Any]:
    """Build ``{nodes, edges, built_at, root}`` for the project at ``root``."""
    nodes: list[dict] = []
    # name -> node id (last one wins; good enough for a visual map)
    func_index: dict[str, str] = {}
    raw: list[tuple[str, str, list[str]]] = []  # (node_id, func_name, calls)

    for path in _iter_py_files(root):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read(), path)
        except Exception:  # noqa: BLE001 — skip unparseable files
            continue
        mod = _module_name(root, path)
        for item in ast.walk(tree):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node_id = f"{mod}.{item.name}"
                calls = _called_names(item)
                # de-dup preserving order
                seen: set[str] = set()
                ordered = [c for c in calls if not (c in seen or seen.add(c))]
                nodes.append({
                    "id": node_id,
                    "module": mod,
                    "name": item.name,
                    "kind": "async" if isinstance(item, ast.AsyncFunctionDef) else "func",
                    "calls_order": ordered[:40],
                })
                func_index[item.name] = node_id
                raw.append((node_id, item.name, ordered))

    # Resolve call names to known function nodes → edges (with order index).
    edges: list[dict] = []
    edge_seen: set[tuple[str, str]] = set()
    for node_id, _fname, calls in raw:
        order = 0
        for callee in calls:
            target = func_index.get(callee)
            if target and target != node_id:
                key = (node_id, target)
                if key not in edge_seen:
                    edges.append({"source": node_id, "target": target, "order": order})
                    edge_seen.add(key)
                    order += 1

    return {
        "root": root,
        "built_at": int(time.time()),
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


def to_text(graph: dict, custom_edges: list[dict] | None = None, *, limit: int = 120) -> str:
    """Compact Markdown rendering of the graph for the AI system prompt."""
    if not isinstance(graph, dict) or not graph.get("nodes"):
        # Fall back to just custom edges if we have no scanned graph yet.
        if custom_edges:
            lines = ["Пользовательские связки:"]
            for e in custom_edges[:limit]:
                note = f" — {e.get('note')}" if e.get("note") else ""
                lines.append(f"- {e.get('from')} → {e.get('to')}{note}")
            return "\n".join(lines)
        return ""

    lines: list[str] = []
    lines.append(
        f"Функций: {graph.get('node_count', 0)}, связей: {graph.get('edge_count', 0)}."
    )
    # Outgoing map per node (ordered).
    out: dict[str, list[str]] = {}
    for e in graph.get("edges", []):
        out.setdefault(e["source"], []).append(e["target"])
    shown = 0
    for n in graph.get("nodes", []):
        outs = out.get(n["id"], [])
        if not outs:
            continue
        seq = " → ".join(t.split(".")[-1] for t in outs[:8])
        lines.append(f"- {n['id']}: сначала {seq}")
        shown += 1
        if shown >= limit:
            lines.append("… (обрезано)")
            break
    if custom_edges:
        lines.append("\nПользовательские связки (курируются владельцем):")
        for e in custom_edges[:limit]:
            note = f" — {e.get('note')}" if e.get("note") else ""
            lines.append(f"- {e.get('from')} → {e.get('to')}{note}")
    return "\n".join(lines)
