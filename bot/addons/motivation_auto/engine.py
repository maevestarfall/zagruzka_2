"""Auto-motivation injection engine for main bot chat.

Logic:
  - If memory (HISTORY_LIMIT) ≤ 100: inject every (memory + 1) messages
  - If memory > 100: inject every N messages (configurable, default 40)
  - Only inject on meaningful messages (skip greetings, time questions,
    photo/video uploads)
  - Messages always count toward the counter, but motivation is skipped
    for trivial ones

The injection is a single phrase from the shared motivation document
(same doc as debates use, stored in addons.debates.motivation).
"""

from __future__ import annotations

import re

from ...storage import storage

# Keys in storage settings dict
_KEY_MODE = "motivation_auto_mode"          # "auto" | "manual"
_KEY_INTERVAL = "motivation_auto_interval"  # int, 0 = auto
_KEY_COUNTER = "motivation_auto_counter"    # int, msgs since last injection

# Trivial message patterns (Russian)
_TRIVIAL_PATTERNS = [
    re.compile(r"^\s*(привет|здравствуй|хай|хей|hey|hi|hello|здорово"
               r"|добр(ый|ое|ая)\s+(утро|день|вечер)|доброе|добрый"
               r"|салам|йо|yo|ку|кек|лол|ок|ладно|спасибо|пасиб"
               r"|спс|благодарю|пока|до свидания|bye|бай)\s*[!.?]*\s*$",
               re.IGNORECASE),
    re.compile(r"^\s*(скок(а|о)?\s+врем(я|ени)|который\s+час"
               r"|what\s+time|what'?s\s+the\s+time|время"
               r"|сколько\s+(сейчас|врем(я|ени))"
               r"|который\s+час)\s*[!.?]*\s*$",
               re.IGNORECASE),
    re.compile(r"^\s*(да|нет|угу|ага|не|нее|ну|хм+|мм+|ааа+|эм+"
               r"|ок|ok|окей|okay|го|ясно|понял|понятно)\s*[!.?]*\s*$",
               re.IGNORECASE),
]


# ─── Settings ─────────────────────────────────────────────────────────────────


def get_mode() -> str:
    """'auto' or 'manual'. Default 'auto'."""
    s = storage._settings()  # noqa: SLF001
    return s.get(_KEY_MODE, "auto")


def set_mode(mode: str) -> None:
    s = storage._settings()  # noqa: SLF001
    s[_KEY_MODE] = mode
    storage._save_data()  # noqa: SLF001


def get_interval() -> int:
    """Custom interval. 0 = auto (based on memory)."""
    s = storage._settings()  # noqa: SLF001
    raw = s.get(_KEY_INTERVAL, 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def set_interval(n: int) -> None:
    s = storage._settings()  # noqa: SLF001
    s[_KEY_INTERVAL] = n
    storage._save_data()  # noqa: SLF001


def get_counter() -> int:
    s = storage._settings()  # noqa: SLF001
    raw = s.get(_KEY_COUNTER, 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _set_counter(n: int) -> None:
    s = storage._settings()  # noqa: SLF001
    s[_KEY_COUNTER] = n
    storage._save_data()  # noqa: SLF001


def _effective_interval() -> int:
    """Compute the effective injection interval."""
    custom = get_interval()
    if custom > 0:
        return custom
    mem = storage.get_history_limit()
    if mem > 100:
        return 40
    return mem + 1


# ─── Message classification ──────────────────────────────────────────────────


def is_trivial_message(text: str | None, has_media: bool = False) -> bool:
    """Return True if the message should NOT trigger motivation.

    Trivial = greetings, one-word acks, time questions, photo/video.
    The message still counts toward the counter.
    """
    if has_media:
        return True
    if not text:
        return True
    stripped = text.strip()
    if len(stripped) < 3:
        return True
    for pat in _TRIVIAL_PATTERNS:
        if pat.match(stripped):
            return True
    return False


# ─── Core injection ──────────────────────────────────────────────────────────


def check_and_inject(user_text: str | None,
                     has_media: bool = False) -> str | None:
    """Call before building LLM messages. Increments counter.

    Returns a motivation phrase string to inject into system prompt,
    or None if no injection is needed this turn.
    """
    if get_mode() != "auto":
        return None

    # Always increment counter
    counter = get_counter() + 1
    _set_counter(counter)

    interval = _effective_interval()
    if counter < interval:
        return None

    # Counter reached threshold — but only inject for meaningful msgs
    if is_trivial_message(user_text, has_media):
        return None

    # Time to inject!
    _set_counter(0)  # reset counter

    # Get a phrase from the shared motivation doc
    try:
        from ..debates import motivation as _mot
    except ImportError:
        return None

    doc = _mot.get_doc()
    if not doc:
        return None

    result = _mot.select_phrase({
        "persona_name": "ассистент",
        "msg_count": counter,
    })
    if not result:
        return None

    # Record
    _mot.record_sent("bot_auto", result)

    return result["text"]


def get_status_text() -> str:
    """Human-readable status for the settings screen."""
    mode = get_mode()
    counter = get_counter()
    interval = _effective_interval()
    custom = get_interval()
    mem = storage.get_history_limit()

    mode_label = "🟢 Авто" if mode == "auto" else "⚪ Вручную"
    if custom > 0:
        interval_label = f"каждые {custom} сообщений (вручную)"
    elif mem > 100:
        interval_label = "каждые 40 сообщений (память > 100)"
    else:
        interval_label = f"каждые {mem + 1} сообщений (память + 1)"

    try:
        from ..debates import motivation as _mot
        doc = _mot.get_doc()
        doc_label = f"📄 {doc['name'][:50]}" if doc else "❌ Не загружен"
    except ImportError:
        doc_label = "❌ Модуль не найден"

    return (
        f"<b>🎯 Мотивация авто</b>\n\n"
        f"Режим: {mode_label}\n"
        f"Интервал: {interval_label}\n"
        f"Счётчик: {counter} / {interval}\n"
        f"Память бота: {mem} пар\n\n"
        f"Документ: {doc_label}\n\n"
        f"<i>Мотивация добавляется к контексту бота как "
        f"короткая фраза, повышающая качество ответов. "
        f"Пропускается для простых сообщений (привет, фото, видео).</i>"
    )
