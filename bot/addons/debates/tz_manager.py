"""ТЗ storage manager — лоу & супер.

Stores TZ files as JSON entries in addon state:
    addons.debates.tz_storage = [
        {
            "id": "tz_17xxxxx",
            "type": "low" | "super",
            "name": "Lilush v5 coder",
            "summary": "Полная спека кодер-бота …",
            "content": "…full cleaned text…",
            "createdAt": "2026-06-11T12:00:00Z",
        },
        …
    ]
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .. import state as addon_state


def _all() -> list[dict[str, Any]]:
    raw = addon_state.get("debates", "tz_storage", []) or []
    return [e for e in raw if isinstance(e, dict) and e.get("id")]


def _save(entries: list[dict[str, Any]]) -> None:
    addon_state.set_("debates", "tz_storage", entries)


def add(tz_type: str, name: str, summary: str, content: str) -> dict[str, Any]:
    """Add a new TZ entry. Returns the entry."""
    tz_type = tz_type if tz_type in ("low", "super") else "low"
    entry = {
        "id": f"tz_{int(time.time() * 1000)}",
        "type": tz_type,
        "name": name[:120].strip(),
        "summary": summary[:300].strip(),
        "content": content,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    entries = _all()
    entries.append(entry)
    _save(entries)
    return entry


def delete(tz_id: str) -> bool:
    entries = _all()
    new = [e for e in entries if e["id"] != tz_id]
    if len(new) == len(entries):
        return False
    _save(new)
    return True


def get(tz_id: str) -> dict[str, Any] | None:
    for e in _all():
        if e["id"] == tz_id:
            return e
    return None


def list_by_type(tz_type: str) -> list[dict[str, Any]]:
    return [e for e in _all() if e.get("type") == tz_type]


def update_content(tz_id: str, new_content: str) -> bool:
    """Update the content of a TZ entry."""
    entries = _all()
    for e in entries:
        if e["id"] == tz_id:
            e["content"] = new_content
            _save(entries)
            return True
    return False


def update_summary(tz_id: str, new_summary: str) -> bool:
    """Update the summary of a TZ entry."""
    entries = _all()
    for e in entries:
        if e["id"] == tz_id:
            e["summary"] = new_summary[:300].strip()
            _save(entries)
            return True
    return False


def list_all() -> list[dict[str, Any]]:
    return _all()
