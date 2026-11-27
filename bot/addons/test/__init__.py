"""Test live-preview surface — web slot for an external code-gen bot.

The «🧪 Тест» button in the lilush main menu mints a token-gated URL
under ``<PUBLIC_URL>/test/<token>/``. Lilush itself does **not**
generate any code — Chloe runs a separate coder bot that builds sites
/ apps and (eventually) POSTs their preview content here. For now
the surface is a static placeholder that proves the route is wired
on both polling and webhook deployments and on both Render and
Oracle Cloud Free Tier hosts.

The token + enabled flag live in ``state.json`` under
``addons.test``; flipping ``enabled`` to ``False`` kills access
without rotating the token (so a later re-enable can reuse the same
URL). :func:`generate_link` always rotates and re-enables.

All wizard UI lives in :mod:`bot.wizard` next to the rest of the
main-menu glue so this addon stays a backend-only package.
"""

from __future__ import annotations

from . import server  # noqa: F401 — re-exports for callers

generate_link = server.generate_link
disable_link = server.disable_link
get_link = server.get_link
get_token = server.get_token
is_enabled = server.is_enabled
register_http_endpoints = server.register_http_endpoints

# Target picker (Render / Oracle / Custom).
get_active_target = server.get_active_target
get_active_target_id = server.get_active_target_id
set_active_target = server.set_active_target
get_target_url = server.get_target_url
get_target_label = server.get_target_label
set_target_url = server.set_target_url
add_custom_target = server.add_custom_target
remove_custom_target = server.remove_custom_target
get_custom_targets = server.get_custom_targets

__all__ = [
    "generate_link",
    "disable_link",
    "get_link",
    "get_token",
    "is_enabled",
    "register_http_endpoints",
    "get_active_target",
    "get_active_target_id",
    "set_active_target",
    "get_target_url",
    "get_target_label",
    "set_target_url",
    "add_custom_target",
    "remove_custom_target",
    "get_custom_targets",
]
