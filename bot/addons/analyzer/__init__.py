"""Analyzer PRO addon — встроенный пульт Analyzer Bot Family (ana).

Полностью повторяет UX standalone-фронта Analyzer-Boss FastAPI: список
текущих задач, история, новая задача (FSM 5 шагов), мультисерверность
(можно подключить несколько инстансов Analyzer-Boss и переключаться
между ними), TrustScore-health-check (`🔍 Проверить все`).

Состояние хранится в ``state.json`` под ключом ``addons.analyzer``
(список серверов + активный + словарь tasks + defaults).

Аддон доступен только владельцу бота (тот же UID, что и Lilush
owner-DM lock в ana). В группах он не работает — только в DM.

Точка входа в главное меню — кнопка ``main:analyzer`` («🔬 Анализатор
PRO»), сразу под ``main:ai_check``/``main:debates``.
"""

from .handlers import build_analyzer_router, show_screen  # noqa: F401
from .handlers import submit_query, poll_result  # noqa: F401

__all__ = ["build_analyzer_router", "show_screen", "submit_query", "poll_result"]
