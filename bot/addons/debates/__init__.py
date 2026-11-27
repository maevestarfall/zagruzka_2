"""Debates addon — встроенный пульт сервера ИИ-дебатов ``debate-ii``.

Полностью повторяет UX standalone Telegram-бота из
``services/debate-ii/telegram_bot.py``: список дверей, создание/удаление,
запуск/завершение сессии, лента событий двери, плюс мультисерверность —
можно подключить сразу несколько инстансов debate-ii.

Состояние хранится в ``state.json`` под ключом ``addons.debates``
(список серверов с собственным ``token``).
"""

from .handlers import build_debates_router, show_screen  # noqa: F401
from .viktor_v3 import build_viktor_v3_router  # noqa: F401
from . import tz_manager  # noqa: F401

__all__ = ["build_debates_router", "build_viktor_v3_router", "show_screen",
           "tz_manager"]
