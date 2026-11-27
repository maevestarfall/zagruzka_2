"""AI CHECK addon — встроенная панель управления ai-tool-inspector.

Полный UX отдельного Telegram-бота из ``services/ai-tool-inspector/``
(bot.mjs) перенесён внутрь Lilush:

* 📊 Статистика — сводка по всем подключённым серверам + TrustScore.
* 🌐 Сервисы — список серверов, удаление в один тап, добавление через
  ``➕ Добавить сервер``.
* 🤖 Модели — список ИИ-моделей первого онлайн-сервера, доли verified/
  failed/unchecked, TrustScore, кнопки «🔗 Ссылка» и «✅ Проверить все».
* 🔗 Ссылки для ИИ — выбор модели → персональный probe/verify URL +
  пример тела запроса.
* 🔑 Токены — все активные токены первого онлайн-сервера.
* ❓ Помощь — короткая справка прямо в боте.

Состояние пишется в ``state.json`` под ключом ``addons.ai_check``
(список серверов + автоматическая миграция старого ``server_url``).
"""

from .handlers import build_ai_check_router, show_screen  # noqa: F401

__all__ = ["build_ai_check_router", "show_screen"]
