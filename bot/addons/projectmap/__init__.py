# «🗺 Карта проекта» (graphify) — interactive project function-map addon.
# Co-authored with CoCo
"""Project map addon.

Main-menu button «🗺 Карта проекта» downloads graphify into /data and
serves a token-gated interactive HTML graph of the project's functions
and their «связки» (call order). «📜 Инструкции → 🗺 graphify» toggles
whether the AI uses the map (text in prompt) and whether it auto-updates
(with Hermes re-analysing into the exportable base).
"""

from __future__ import annotations

from .handlers import build_projectmap_router
from .server import register_http_endpoints
from .store import (
    disable_link,
    generate_link,
    get_link,
    get_token,
    is_enabled,
)

__all__ = [
    "build_projectmap_router",
    "disable_link",
    "generate_link",
    "get_link",
    "get_token",
    "is_enabled",
    "register_http_endpoints",
]
