"""Priority addon — «🎯 Приоритет» in ⚙️ Настройки.

Controls how the bot decides where to look for an answer:

* ``only_bot``           — use ONLY the bot's own built-in capabilities.
* ``bot_then_internet``  — try the bot's capabilities first, then the
  internet.
* ``instruction``        — same as bot-first, but the bot is told to
  follow the explicit capability tree (показывается «инструкцией»).

When enabled, :func:`priority_directive` injects a short instruction into
the chat system prompt so the live agent actually honours the setting.
"""

from __future__ import annotations

from .store import (
    MODE_LABELS,
    MODES,
    get_mode,
    is_enabled,
    priority_directive,
    set_enabled,
    set_mode,
)

__all__ = [
    "MODES",
    "MODE_LABELS",
    "get_mode",
    "is_enabled",
    "priority_directive",
    "set_enabled",
    "set_mode",
]
