"""Мост «чат с мозгом» для не-TG платформ (фикс 2.4, ТЗ-1 R3).

Свободный текст привязанного пользователя из Discord/VK должен уходить
в ТОТ ЖЕ пайплайн диалога, что и обычное сообщение в Telegram. В TG эта
точка входа — ``bot.handlers.handle_text`` → :func:`bot.agent.run_agent`
(единственная «мозговая» функция: роутер LLM, инструменты, RAM-guard).

:func:`ask_brain` — ЧИСТАЯ обёртка над этим пайплайном, без дублирования
логики: те же предохранители, что и в TG-хендлере (бот выключен /
brain=devin), затем прямой вызов ``run_agent`` с тем же ``cwd``.

TG-специфика (typing-индикатор, кнопка «🛑 Отмена», мини-статус,
голосовой ответчик) остаётся в aiogram-хендлере — на Discord/VK её нет
по построению, это косметика поверх того же мозга, а не часть пайплайна.

Правила:

* Ошибки мозга НЕ глотаются — исключение поднимается вызывающему
  адаптеру, который отвечает вежливым текстом и логирует БЕЗ текста
  пользователя (правило приватности из FIX_PART_2).
* Все импорты ленивые: модуль импортируется на голом Python (§13.3),
  тяжёлые зависимости (openai и т.п.) подтягиваются только при вызове.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("gateway.brain")

#: Ответы-предохранители — те же ситуации, что в TG ``handle_text``.
DISABLED_TEXT = "Бот выключен. Владелец может включить его в Telegram: /enable"
DEVIN_TEXT = (
    "Принято. Brain=devin — жди ответ от Devin.\n"
    "Сообщение записано в inbox.log."
)


async def ask_brain(user_id: int, text: str) -> str:
    """Ответ мозга на свободный текст — единый для всех платформ.

    ``user_id`` — уже смапленный TG user_id (адаптер обязан сначала
    вызвать ``identity.resolve_user_id``). Возвращает текст ответа;
    любые ошибки пайплайна пробрасываются наружу.
    """
    from ..storage import storage

    if not storage.is_enabled():
        return DISABLED_TEXT
    if storage.get_brain() == "devin":
        return DEVIN_TEXT

    cwd = None
    try:
        cwd = storage.get_cwd(int(user_id))
    except Exception:  # noqa: BLE001 — cwd — удобство, не требование
        logger.debug("get_cwd failed, using default", exc_info=True)

    from ..agent import run_agent

    answer = await run_agent(int(user_id), text or "", cwd)
    return answer or "…"
