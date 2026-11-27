"""«🛠 Master Room» — devin-style coder/test/deploy console.

Public surface (v0.3, P1):

* :func:`build_masterroom_router` — aiogram :class:`Router` with the
  full UI / FSM / callback set. Appended in
  :func:`bot.addons.build_addon_routers`.
* :func:`show_screen` — entry point used by ``bot/wizard.py`` when
  the «🛠 Master Room» button is tapped in the main menu.
* :func:`register_http_endpoints` — mounts ``/masterroom/<token>/...``
  routes on the bot's aiohttp app (both polling health_app and
  webhook app).

Back-compat shims still re-exported for old callsites:

* ``is_enabled`` · ``get_token`` · ``get_link`` · ``reserve_defaults``
"""

from __future__ import annotations

from . import server  # noqa: F401  — keeps reserve_defaults / is_enabled live
from .handlers import build_masterroom_router, show_screen
from .server import (
    get_link,
    get_token,
    is_enabled,
    register_http_endpoints,
    reserve_defaults,
)

__all__ = [
    "build_masterroom_router",
    "show_screen",
    "register_http_endpoints",
    "reserve_defaults",
    "is_enabled",
    "get_token",
    "get_link",
]
