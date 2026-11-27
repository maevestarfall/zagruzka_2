"""«🕵️ Антидетект» — toggle the stealth fingerprint and run a live-check.

State (addon namespace ``stealth``):
    • ``enabled`` (bool, default True) — when OFF, ``browser_clicker`` skips
      the Dolphin-Anty-grade fingerprint enrichment entirely.

The router (``stl:*`` callbacks) gives the owner three actions, exactly
as requested:
    ▶️ Запустить live-check  — run :func:`bot.addons.stealth_fp_check.live_check`
                               in a real Chromium and show the report;
    🟢/🔴 Антидетект ВКЛ/ВЫКЛ — flip the toggle;
    ↩️ Назад                 — return to ⚙️ Настройки.
"""

from __future__ import annotations

ADDON = "stealth"


def _st():
    from .. import state as addon_state
    return addon_state


def is_enabled() -> bool:
    """Stealth fingerprint is ON by default (preserves prior behaviour)."""
    v = _st().get(ADDON, "enabled", None)
    return True if v is None else bool(v)


def set_enabled(on: bool) -> None:
    _st().set_(ADDON, "enabled", bool(on))


__all__ = ["ADDON", "is_enabled", "set_enabled", "build_stealth_router"]


def build_stealth_router():
    from .handlers import build_stealth_router as _b

    return _b()
