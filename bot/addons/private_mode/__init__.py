"""Private-mode control panel UI addon (Settings → ⛔ Доступ → 🔒 Приват режим).

Lets the owner pick per-feature use/see permissions for guests, configure
payment (card / Telegram Stars / crypto, each on/off) with a $-per-slot price
and pay-on-join switch, and edit the guest-facing welcome / limit-reached
texts. Logic lives in :mod:`bot.private_mode`.
"""

from __future__ import annotations

__all__ = ["build_private_mode_router", "build_payment_gate_middleware"]


def build_private_mode_router():
    from .handlers import build_private_mode_router as _build
    return _build()


def build_payment_gate_middleware():
    from .handlers import build_payment_gate_middleware as _build
    return _build()
