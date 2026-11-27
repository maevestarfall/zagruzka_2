# «🧭 Гид» — step-by-step web setup wizard (sequence of buttons to click).
# Co-authored with CoCo
"""Token-gated HTML wizard opened from «🧩 Прочие → 🧭 Гид»."""

from __future__ import annotations

from .handlers import build_guide_router
from .server import register_http_endpoints
from .store import disable_link, generate_link, get_link, get_token, is_enabled

__all__ = [
    "build_guide_router",
    "register_http_endpoints",
    "disable_link",
    "generate_link",
    "get_link",
    "get_token",
    "is_enabled",
]
