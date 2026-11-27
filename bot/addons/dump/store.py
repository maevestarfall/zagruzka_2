"""Persistent "garbage bin" store for the «🗑 Скинуть весь мусор» feature.

The user opens the bin, then throws in anything — links, videos, files,
photos, voice, plain text — and the bot just *accumulates* it. Each item
is a small dict describing how to fetch/read it later (Telegram ``file_id``
for media, raw text/URL for text). Nothing heavy is done at capture time;
the actual reading happens on «📤 Отправить» / «✖️ Закрыть».

State lives under the shared addon-state JSON so it survives restarts:
``_settings.addons.dump.bin_<user_id> = [item, ...]``.
"""

from __future__ import annotations

import time
from typing import Any

ADDON = "dump"

# Item kinds we know how to read later (see ``readers.read_item``).
KIND_TEXT = "text"
KIND_URL = "url"
KIND_DOCUMENT = "document"
KIND_PHOTO = "photo"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_VOICE = "voice"
KIND_ANIMATION = "animation"

KIND_EMOJI = {
    KIND_TEXT: "📝",
    KIND_URL: "🔗",
    KIND_DOCUMENT: "📄",
    KIND_PHOTO: "🖼",
    KIND_VIDEO: "🎬",
    KIND_AUDIO: "🎵",
    KIND_VOICE: "🎙",
    KIND_ANIMATION: "🎞",
}


def _key(user_id: int) -> str:
    return f"bin_{int(user_id)}"


def get_items(user_id: int) -> list[dict[str, Any]]:
    from .. import state as addon_state

    items = addon_state.get(ADDON, _key(user_id), []) or []
    return list(items) if isinstance(items, list) else []


def add_item(user_id: int, item: dict[str, Any]) -> int:
    """Append ``item`` to the user's bin, return the new total count."""
    from .. import state as addon_state

    item = dict(item)
    item.setdefault("ts", int(time.time()))
    items = get_items(user_id)
    items.append(item)
    addon_state.set_(ADDON, _key(user_id), items)
    return len(items)


def count(user_id: int) -> int:
    return len(get_items(user_id))


def clear(user_id: int) -> int:
    """Empty the bin, return how many items were removed."""
    from .. import state as addon_state

    n = count(user_id)
    addon_state.set_(ADDON, _key(user_id), [])
    return n


def last_question(user_id: int) -> str:
    """The user's most recent *textual* item — treated as «the question»."""
    for item in reversed(get_items(user_id)):
        if item.get("kind") in (KIND_TEXT, KIND_URL):
            txt = (item.get("text") or "").strip()
            if txt:
                return txt
        # A media caption also counts as an instruction.
        cap = (item.get("text") or "").strip()
        if cap:
            return cap
    return ""


def summary_lines(user_id: int) -> str:
    """Short human-readable inventory of what's currently in the bin."""
    items = get_items(user_id)
    if not items:
        return "<i>Пока пусто. Кидай ссылки, видео, файлы, фото, голосовые, текст…</i>"
    lines: list[str] = []
    for i, item in enumerate(items, 1):
        kind = item.get("kind", KIND_TEXT)
        emoji = KIND_EMOJI.get(kind, "•")
        label = item.get("file_name") or (item.get("text") or "")[:60] or kind
        lines.append(f"{i}. {emoji} {label}")
    return "\n".join(lines)
