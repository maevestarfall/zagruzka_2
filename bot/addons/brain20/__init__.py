"""20-slot brain pool UI addon.

Adds Settings → 🧠 Авто мозг → «🧩 20 API-слотов»: a pool of up to 20
OpenAI-compatible endpoints with per-slot API key / URL / 3 models / token
budget, a «во все 20 слотов» bulk-fill screen, a top-of-menu active-model
selector, and sequential auto-failover (wired in :mod:`bot.agent`).
"""

from __future__ import annotations

__all__ = ["build_brain20_router"]


def build_brain20_router():
    from .handlers import build_brain20_router as _build
    return _build()
