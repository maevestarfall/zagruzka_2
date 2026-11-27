# Regen optimizer: analyses all bot functions and produces an optimized action playbook.
# Co-authored with CoCo
"""«🛠 Оптимизация» for Регенерация.

Reads the whole-project map (every module/function/button) and asks the
coder brain to produce an OPTIMIZED instruction-playbook:

* лучшие связки функций под каждую задачу (генерация фото/видео, поиск
  человека/статистики/github, чтение .mhtml и т.д.);
* какой источник/парсинг под какую тему (reddit/twitter/linkedin/apify…);
* тайминги между шагами и сохранение контекста между функциями;
* пошаговый алгоритм действий.

Output is plain text/markdown — a руководство, которое можно отдать
агенту или сохранить. Не меняет код, только советует.
"""

from __future__ import annotations

import logging

from .._llm import NoBrainError, chat_once

logger = logging.getLogger(__name__)

__all__ = ["build_optimization", "NoBrainError"]

_SYSTEM = (
    "Ты — архитектор-оптимизатор Telegram-бота lilush (aiogram 3.x). Тебе "
    "дают КАРТУ ПРОЕКТА: все модули, их ответственность, функции и кнопки. "
    "Твоя задача — составить ОПТИМИЗИРОВАННУЮ ИНСТРУКЦИЮ (playbook): как "
    "лучше всего связывать существующие функции бота в цепочки под каждую "
    "типовую задачу. Используй ТОЛЬКО реально существующие функции/модули "
    "из карты — не выдумывай.\n\n"
    "Для каждой типовой задачи опиши:\n"
    "1) Связка функций — какие модули/функции и в каком ПОРЯДКЕ вызывать.\n"
    "2) Источник/парсинг под тему — где что искать (например: люди → "
    "linkedin/apify, обсуждения → reddit/twitter-акторы, факты → веб-поиск, "
    "документы/.mhtml → markitdown или unstructured, и т.п.).\n"
    "3) Тайминги — где нужны паузы/задержки между шагами и почему.\n"
    "4) Сохранение контекста — что передавать от шага к шагу, чтобы не "
    "терять состояние.\n\n"
    "Покрой как минимум: генерация фото, генерация видео, чтение мусора "
    "(.mhtml/файлы/ссылки), поиск информации, поиск человека (OSINT), "
    "статистика, работа с GitHub, кодер/регенерация. "
    "Пиши кратко и по делу, маркдауном, разделами по задачам. Без воды."
)


async def build_optimization(project_map: str, *, max_tokens: int = 3500) -> str:
    """Ask the brain to produce the optimized playbook. Raises NoBrainError."""
    user = (
        (project_map.strip() or "(карта проекта пуста)")
        + "\n\nСоставь оптимизированную инструкцию-плейбук по схеме из system. "
        "Только существующие функции, разделы по задачам."
    )
    text = await chat_once(_SYSTEM, user, max_tokens=max_tokens, temperature=0.3)
    return (text or "").strip() or "(модель вернула пустой ответ)"
