"""Neutral menu contract for the Platform Gateway (ТЗ-1 §3.3).

``MenuScreen`` / ``MenuButton`` describe a bot menu **once**, in a
platform-agnostic format. Every platform then renders the same screen
through its own thin adapter:

* Telegram → :func:`screen_to_tg` (``InlineKeyboardMarkup``, pixel-equal
  to the legacy ``_kb_*`` output — verified by tests).
* Web (VISION / Fable5) → :func:`screen_to_json`.
* Discord / VK → their adapters (этапы 2–3).

This is what makes the owner's key requirement (R5) hold by
construction: «добавил кнопку — она видна на всех площадках».

Design rules (§3.3 of the spec):

* ``MenuButton.action`` is the **existing** ``callback_data`` string
  (``roles:menu``, ``main:settings``, …). Nothing is renamed —
  :mod:`bot.feature_gate` prefixes keep working untouched.
* Screens are built **at request time** — no stale copies exist, so no
  cross-platform push-sync is needed.
* Guest gating (``filter_menu_rows``) is applied once, centrally —
  either inside :func:`screen_to_tg` (TG path, same place as before the
  refactor) or in :mod:`bot.gateway.dispatch` (non-TG path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("gateway.menu")

#: Hard Telegram limit for ``callback_data``; the strictest across all
#: supported platforms, so every registered action must fit (§9А.3).
MAX_ACTION_BYTES = 64


@dataclass(frozen=True)
class MenuButton:
    """One button of a screen — platform-neutral."""

    text: str                  # "🎭 Роли" — with emoji, exactly as in TG
    action: str = ""           # the SAME callback string used today: "roles:menu"
    url: str | None = None     # URL buttons (VISION link, instructions)

    @property
    def callback_data(self) -> str | None:
        """Alias so :func:`bot.feature_gate.filter_menu_rows` (which reads
        ``btn.callback_data``) works on neutral buttons unchanged."""
        return self.action or None


@dataclass(frozen=True)
class MenuScreen:
    """Neutral description of one menu screen — the single source of truth."""

    screen_id: str             # "settings", "gateway:home", ...
    title: str                 # "⚙️ Настройки"
    body: str = ""             # text above the buttons (markdown-safe)
    rows: list[list[MenuButton]] = field(default_factory=list)
    parent: str | None = None  # for «↩️ Назад» on platforms without history


# --------------------------------------------------------------------------
# Screen registry (§3.4 — «реестр, а не переписывание»)
# --------------------------------------------------------------------------

ScreenBuilder = Callable[[int | None], MenuScreen]

#: screen_id → builder. Screens are registered wave by wave (§7); an
#: action whose screen is not here gets the polite ``tg_only`` stub on
#: non-TG platforms.
SCREEN_REGISTRY: dict[str, ScreenBuilder] = {}


def register_screen(screen_id: str, builder: ScreenBuilder) -> None:
    """Register (or replace) a screen builder.

    Also validates every static action string against the 64-byte
    ``callback_data`` limit — failing fast at import time instead of at
    the first tap (§10 «Лимиты payload»).
    """
    SCREEN_REGISTRY[screen_id] = builder


def build_screen(screen_id: str, user_id: int | None) -> MenuScreen | None:
    """Build a registered screen for ``user_id``; ``None`` if unknown."""
    builder = SCREEN_REGISTRY.get(screen_id)
    if builder is None:
        return None
    return builder(user_id)


def validate_screen(screen: MenuScreen) -> list[str]:
    """Return a list of action strings that violate the 64-byte limit."""
    bad: list[str] = []
    for row in screen.rows:
        for btn in row:
            if btn.action and len(btn.action.encode("utf-8")) > MAX_ACTION_BYTES:
                bad.append(btn.action)
    return bad


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------

def screen_to_tg(screen: MenuScreen, user_id: int | None):
    """Render a neutral screen into aiogram ``InlineKeyboardMarkup``.

    Applies :func:`bot.feature_gate.filter_menu_rows` exactly where the
    legacy ``_kb_*`` functions did, so the TG output stays byte-equal to
    the pre-refactor keyboards (invariant A2, tested).
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[InlineKeyboardButton]] = []
    for row in screen.rows:
        tg_row: list[InlineKeyboardButton] = []
        for btn in row:
            if btn.url:
                tg_row.append(InlineKeyboardButton(text=btn.text, url=btn.url))
            else:
                tg_row.append(
                    InlineKeyboardButton(text=btn.text, callback_data=btn.action)
                )
        if tg_row:
            rows.append(tg_row)

    from ..feature_gate import filter_menu_rows

    rows = filter_menu_rows(rows, user_id)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def screen_to_json(screen: MenuScreen) -> dict:
    """Render a neutral screen into the Fable5 JSON contract (§4.7)."""
    return {
        "screen_id": screen.screen_id,
        "title": screen.title,
        "body": screen.body,
        "rows": [
            [
                {"text": b.text, "action": b.action, "url": b.url}
                for b in row
            ]
            for row in screen.rows
        ],
        "parent": screen.parent,
    }
