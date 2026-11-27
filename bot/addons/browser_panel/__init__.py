"""«🌐 Браузерная» — main-menu hub with two browser engines.

Tapping «🌐 Браузерная» opens a chooser with two panels, each with the same
🤖 Авто / 📝 Промт / ↩️ Назад actions:

    🌐 Браузерная        — the project's CLASSIC engine
                           (bot.workers.browser_clicker.run_browser_macro):
                           NL → JSON steps → stealth browser. Honours the
                           🕵️ Антидетект toggle.
    🆕 Браузерная новая  — the real ``browser-use`` package from GitHub
                           (bot.workers.browser_use_agent.run_browser_use_task):
                           an autonomous agent that decides clicks itself.

State (addon namespace ``browser_panel``):
    • ``auto_old`` (bool) — auto-execute via the classic engine.
    • ``auto_new`` (bool) — auto-execute via browser-use.
Both default to False so normal chat is untouched. If both are ON, the new
(browser-use) engine wins.
"""

from __future__ import annotations

ADDON = "browser_panel"


def _st():
    from .. import state as addon_state
    return addon_state


def is_auto_old() -> bool:
    return bool(_st().get(ADDON, "auto_old", False))


def set_auto_old(on: bool) -> None:
    _st().set_(ADDON, "auto_old", bool(on))


def is_auto_new() -> bool:
    return bool(_st().get(ADDON, "auto_new", False))


def set_auto_new(on: bool) -> None:
    _st().set_(ADDON, "auto_new", bool(on))


def build_browser_panel_router():
    from .handlers import build_browser_panel_router as _b

    return _b()


__all__ = [
    "ADDON",
    "is_auto_old", "set_auto_old",
    "is_auto_new", "set_auto_new",
    "build_browser_panel_router",
]
