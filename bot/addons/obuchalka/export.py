# «🎓 Обучалка» — экспорт «цемента»: JSONL-датасет из листиков-оснований.
"""Builds the fine-tune dataset («цемент», который забирается на комп).

Only leaves with score == 10 (locked foundations) are exported — the vessel
guarantees each line is an already-verified «идеальная форма» of knowledge.

Line format (standard chat fine-tune shape for Qwen/LoRA):
``{"messages": [{"role":"system",...}, {"role":"user",...}, {"role":"assistant",...}]}``
"""

from __future__ import annotations

import json
from typing import Iterator

from . import store


def iter_dataset() -> Iterator[dict]:
    for vessel in store.list_vessels():
        macro = (vessel.get("macro_map") or "").strip()
        system = f"Каркас: {vessel['title']}.\n{macro}" if macro else f"Каркас: {vessel['title']}."
        for leaf in store.list_leaves(vessel["id"]):
            if leaf["score"] != 10 or not leaf["content"]:
                continue
            yield {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": leaf["task"] or leaf["title"]},
                    {"role": "assistant", "content": leaf["content"]},
                ]
            }


def build_jsonl() -> str:
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in iter_dataset())


def build_markdown() -> str:
    """Human-readable snapshot of the whole vessel tree (bonus surface)."""
    lines: list[str] = ["# Сосуд знаний — слепок"]
    for vessel in store.list_vessels():
        lines.append(f"\n## {vessel['title']}")
        if vessel.get("macro_map"):
            lines.append(f"\n```\n{vessel['macro_map']}\n```")
        for leaf in store.list_leaves(vessel["id"]):
            mark = "🔒 10/10" if leaf["locked"] else f"{leaf['score']}/10"
            lines.append(f"\n### {leaf['title']} — {mark}")
            if leaf["task"]:
                lines.append(f"Задание: {leaf['task']}")
            if leaf["content"]:
                lines.append(f"\n{leaf['content']}")
    return "\n".join(lines)
