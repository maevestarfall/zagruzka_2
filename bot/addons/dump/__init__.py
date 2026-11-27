"""«🗑 Скинуть весь мусор» — a one-button garbage bin.

The user dumps anything (links, videos, files, photos, voice, text), the
bot accumulates it, then on «📤 Отправить» / «✖️ Закрыть» it *reads* every
item by combining the bot's existing capabilities (markitdown, Groq
Whisper/Vision, Firecrawl/Apify, OCR) and answers the user's question with
all of it as context.

Telegram handlers live on ``wizard_router`` (registered first) so the
collecting-state capture wins over the catch-all and Groq overrides.
This package only holds the reusable, testable logic: ``store`` + ``readers``.
"""

from __future__ import annotations

from . import readers, store  # noqa: F401

__all__ = ["store", "readers"]
