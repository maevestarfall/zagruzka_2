"""VISION — web dashboard for controlling the bot through a browser.

The «👁 VISION» button in the main menu generates a token-gated URL
under ``<PUBLIC_URL>/vision/<token>/``. The dashboard is a self-contained
SPA that talks to the Fable5 REST API for state, logs, exec, and send.

Distinct from «🧪 Тест» which hosts external code-gen preview payloads.
VISION is the bot's own management panel.
"""

from __future__ import annotations

from .server import (
    generate_link,
    disable_link,
    get_link,
    get_token,
    get_status,
    get_expires_at,
    is_enabled,
    is_expired,
    preflight,
    register_http_endpoints,
)

__all__ = [
    "generate_link",
    "disable_link",
    "get_link",
    "get_token",
    "get_status",
    "get_expires_at",
    "is_enabled",
    "is_expired",
    "preflight",
    "register_http_endpoints",
]
