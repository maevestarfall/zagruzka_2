"""«🎞 Анализ динамики видео» — reusable logic (no router).

Telegram handlers live on ``wizard_router`` (state ``VidDynStates``) so the
video-capture wins over Groq overrides and the catch-all. Entry buttons sit
in BOTH «Генерация → Видео» and «Исследование и поиск человека», pointing at
the same ``viddyn:open`` callback.
"""

from __future__ import annotations

from . import analyzer, store  # noqa: F401

__all__ = ["store", "analyzer"]
