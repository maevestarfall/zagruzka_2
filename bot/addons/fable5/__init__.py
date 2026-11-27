"""Fable5 live-access addon — remote-test surface for Viktor Ai.

Lilush is owned by Chloe; Viktor (codename ``Fable5``) iterates on it
remotely. To verify a build on the *actual* live bot — not just unit
tests — Chloe opens ``⚙️ Настройки → 🧠 Перенастроить мозг → 🌀 Fable5``
and taps «Сгенерировать ссылку». That mints a random URL-safe token,
flips ``addons.fable5.enabled = True``, and prints the full URL she can
paste to Viktor. Tapping «Выключить ссылку» flips the flag back off so
Viktor's token stops working immediately (the token itself is kept so a
later re-enable can reuse it; «Сгенерировать ссылку» always rotates).

All UI lives in :mod:`bot.wizard` next to the rest of the brain menu so
the addon stays a backend-only package. The HTTP routes are registered
on both polling and webhook aiohttp apps from :mod:`bot.main`.
"""

from __future__ import annotations

from . import server  # noqa: F401 — re-exports for callers

generate_link = server.generate_link
disable_link = server.disable_link
get_link = server.get_link
get_token = server.get_token
is_enabled = server.is_enabled
register_http_endpoints = server.register_http_endpoints

__all__ = [
    "generate_link",
    "disable_link",
    "get_link",
    "get_token",
    "is_enabled",
    "register_http_endpoints",
]
