"""«🔌 Подключение к каналу источников» — connect the bot to Telegram
channels / chats it should *harvest* from.

The owner adds the bot to a channel (as admin) or a group, registers that
chat as a *source*, then dumps anything into it: image-API keys, bank
cards, code, photos/videos, or plain-text instructions. The bot captures
every post, the AI sorts each one into one of four buckets —

    • ✅ проверить        (needs a human look)
    • 🖼 видео и фото      (media to keep / route to generation)
    • 💻 код              (code snippets)
    • 💳 карта (деньги)    (cards / API keys / money → encrypted Vault)

— and, when a post is an *instruction*, the bot executes it.

Two routers are exposed:

* :func:`build_source_capture_router` — registered **first** in the
  dispatcher (before the Groq catch-all) so posts in source chats are
  captured instead of being answered as normal chat.
* :func:`build_source_channels_router` — the inline-keyboard UI
  (``srcch:*`` callbacks), registered among the other addons.

State lives under the shared addon-state JSON (``addons.source_channels``)
so everything survives restarts with no extra files.
"""

from __future__ import annotations

from . import store  # noqa: F401

__all__ = [
    "store",
    "build_source_channels_router",
    "build_source_capture_router",
]


def build_source_channels_router():
    from .handlers import build_source_channels_router as _b

    return _b()


def build_source_capture_router():
    from .handlers import build_source_capture_router as _b

    return _b()
