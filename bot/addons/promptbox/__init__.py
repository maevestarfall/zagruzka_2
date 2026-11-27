"""«🗄 Хранилище промтов» — prompt storage addon.

Session 1: 20 dynamic prompt slots. Each slot holds a short description +
a full "prompt for execution". Free slots show «пусто»; filled slots show
their description. Manual save / edit / delete. Export/import as a single
JSON file so prompts can be shared between bots.

Later sessions add: Функции (function picker), Выбрать исполнителя (brain),
Исполнить (send composed prompt to the brain), Умное планирование (ordered
pipeline), «Сначала анализ» (preview → Исполнить/Дополнить/Отмена).
"""

from __future__ import annotations

from .handlers import build_promptbox_router

__all__ = ["build_promptbox_router"]
