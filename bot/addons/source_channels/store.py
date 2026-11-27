"""Persistent store for the «🔌 Каналы-источники» addon.

All state lives under ``addons.source_channels`` via the shared
``bot.addons.state`` helper, so it is saved to ``state.json`` and
survives restarts. Nothing here touches Telegram — pure data.
"""

from __future__ import annotations

import time
from typing import Any

ADDON = "source_channels"

# ── category constants ─────────────────────────────────────────────────
CAT_CHECK = "check"   # ✅ проверить
CAT_MEDIA = "media"   # 🖼 видео и фото
CAT_CODE = "code"     # 💻 код
CAT_MONEY = "money"   # 💳 карта (деньги) / api-ключи → сейф

CATEGORY_LABEL = {
    CAT_CHECK: "✅ Проверить",
    CAT_MEDIA: "🖼 Видео и фото",
    CAT_CODE: "💻 Код",
    CAT_MONEY: "💳 Карта (деньги)",
}
CATEGORY_EMOJI = {
    CAT_CHECK: "✅",
    CAT_MEDIA: "🖼",
    CAT_CODE: "💻",
    CAT_MONEY: "💳",
}
ALL_CATEGORIES = (CAT_CHECK, CAT_MEDIA, CAT_CODE, CAT_MONEY)

# Default daily check schedule (owner's local time, see settings tz).
_DEFAULT_TIMES = ["09:00", "21:00"]
DEFAULT_TZ = "Europe/Moscow"


def _st():
    from .. import state as addon_state

    return addon_state


# ── sources (channels / chats) ─────────────────────────────────────────

def get_sources() -> list[dict[str, Any]]:
    items = _st().get(ADDON, "sources", []) or []
    return list(items) if isinstance(items, list) else []


def get_source_ids() -> set[int]:
    out: set[int] = set()
    for s in get_sources():
        try:
            out.add(int(s["chat_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def find_source(chat_id: int) -> dict[str, Any] | None:
    for s in get_sources():
        try:
            if int(s["chat_id"]) == int(chat_id):
                return s
        except (KeyError, TypeError, ValueError):
            continue
    return None


def add_source(chat_id: int, title: str, ctype: str = "channel") -> dict[str, Any]:
    """Register a Telegram source. Idempotent on ``chat_id``."""
    sources = get_sources()
    for s in sources:
        if s.get("chat_id") is not None and int(s.get("chat_id", 0)) == int(chat_id):
            s["title"] = title or s.get("title") or str(chat_id)
            s["type"] = ctype or s.get("type", "channel")
            _st().set_(ADDON, "sources", sources)
            return s
    entry = {
        "id": f"src_{int(time.time() * 1000)}",
        "chat_id": int(chat_id),
        "url": "",
        "title": title or str(chat_id),
        "type": ctype,
        "added_ts": int(time.time()),
    }
    sources.append(entry)
    _st().set_(ADDON, "sources", sources)
    return entry


def add_site_source(url: str, title: str = "") -> dict[str, Any]:
    """Register a website source (fetched on each check). Idempotent on URL."""
    url = (url or "").strip()
    sources = get_sources()
    for s in sources:
        if s.get("type") == "site" and s.get("url") == url:
            if title:
                s["title"] = title
            _st().set_(ADDON, "sources", sources)
            return s
    entry = {
        "id": f"src_{int(time.time() * 1000)}",
        "chat_id": None,
        "url": url,
        "title": title or url,
        "type": "site",
        "added_ts": int(time.time()),
    }
    sources.append(entry)
    _st().set_(ADDON, "sources", sources)
    return entry


def get_site_sources() -> list[dict[str, Any]]:
    return [s for s in get_sources() if s.get("type") == "site"]


def remove_source(source_id: str) -> bool:
    sources = get_sources()
    new = [s for s in sources if s.get("id") != source_id]
    if len(new) == len(sources):
        return False
    _st().set_(ADDON, "sources", new)
    return True


# ── auto on/off ────────────────────────────────────────────────────────

def is_auto_enabled() -> bool:
    return bool(_st().get(ADDON, "auto_enabled", False))


def set_auto(enabled: bool) -> None:
    _st().set_(ADDON, "auto_enabled", bool(enabled))


# ── daily schedule (list of "HH:MM") ───────────────────────────────────

def get_times() -> list[str]:
    raw = _st().get(ADDON, "times", None)
    if raw is None:
        return list(_DEFAULT_TIMES)
    if not isinstance(raw, list):
        return list(_DEFAULT_TIMES)
    return [t for t in raw if isinstance(t, str)]


def _norm_time(value: str) -> str | None:
    """Normalise '9:5' / '9.05' / '0900' → '09:05'. Return None if invalid."""
    s = (value or "").strip().replace(".", ":").replace("-", ":")
    if ":" not in s and s.isdigit() and len(s) in (3, 4):
        s = s.zfill(4)
        s = f"{s[:2]}:{s[2:]}"
    if ":" not in s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def add_time(value: str) -> str | None:
    """Add a daily check time. Returns the normalised value or None."""
    norm = _norm_time(value)
    if norm is None:
        return None
    times = get_times()
    if norm not in times:
        times.append(norm)
        times.sort()
        _st().set_(ADDON, "times", times)
    return norm


def remove_time(value: str) -> bool:
    norm = _norm_time(value) or value
    times = get_times()
    if norm not in times:
        return False
    times = [t for t in times if t != norm]
    _st().set_(ADDON, "times", times)
    return True


def get_tz() -> str:
    return _st().get(ADDON, "tz", DEFAULT_TZ) or DEFAULT_TZ


def set_tz(tz: str) -> None:
    _st().set_(ADDON, "tz", tz)


# ── owner chat (where reports are sent) ────────────────────────────────

def get_owner_chat_id() -> int | None:
    val = _st().get(ADDON, "owner_chat_id", None)
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def set_owner_chat_id(chat_id: int) -> None:
    _st().set_(ADDON, "owner_chat_id", int(chat_id))


# ── captured buffer ────────────────────────────────────────────────────
#
# Each captured post is stored raw until the next (auto or manual) check
# processes it. Processed items keep their assigned category for history.

def get_buffer() -> list[dict[str, Any]]:
    items = _st().get(ADDON, "buffer", []) or []
    return list(items) if isinstance(items, list) else []


def add_to_buffer(item: dict[str, Any]) -> int:
    item = dict(item)
    item.setdefault("ts", int(time.time()))
    item.setdefault("processed", False)
    item.setdefault("id", f"item_{int(time.time() * 1000)}_{len(get_buffer())}")
    buf = get_buffer()
    buf.append(item)
    # Keep the buffer bounded so state.json never explodes.
    if len(buf) > 500:
        buf = buf[-500:]
    _st().set_(ADDON, "buffer", buf)
    return len(buf)


def pending_items() -> list[dict[str, Any]]:
    return [i for i in get_buffer() if not i.get("processed")]


def pending_count() -> int:
    return len(pending_items())


def save_buffer(buf: list[dict[str, Any]]) -> None:
    _st().set_(ADDON, "buffer", buf)


def clear_processed() -> int:
    buf = get_buffer()
    keep = [i for i in buf if not i.get("processed")]
    removed = len(buf) - len(keep)
    _st().set_(ADDON, "buffer", keep)
    return removed


def category_counts() -> dict[str, int]:
    counts = {c: 0 for c in ALL_CATEGORIES}
    for i in get_buffer():
        cat = i.get("category")
        if cat in counts:
            counts[cat] += 1
    return counts


def items_in_category(cat: str, limit: int = 20) -> list[dict[str, Any]]:
    out = [i for i in get_buffer() if i.get("category") == cat]
    return out[-limit:]


# ── run de-dup marker (avoid double fire within one scheduled minute) ──

def get_last_run_marker() -> str:
    return _st().get(ADDON, "last_run_marker", "") or ""


def set_last_run_marker(marker: str) -> None:
    _st().set_(ADDON, "last_run_marker", marker)
