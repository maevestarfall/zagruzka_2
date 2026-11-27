# План карта addon: web flowchart editor (boxes + arrows) with TG export/import.
# Co-authored with CoCo
"""«🗺 План карта» — цепочка действий в виде квадратов со стрелками.

Принцип ссылок — как у Fable5 («Перенастроить мозг → Fable5»): кнопка
«Создать ссылку» минтит случайный URL-safe токен, включает
``addons.plan_map.enabled = True`` и печатает полный URL. «Выключить
ссылку» гасит флаг — ссылка мгновенно умирает без ротации токена.

На сайте — редактор цепочки действий: квадратики с текстом внутри,
подпись (название) над каждым квадратиком, и стрелки между ними для
очерёдности. Всё сохраняется в файл ``plan_map.json`` в хранилище
Master Room (``/data/storage``), поэтому план переживает перезапуск и
виден в экране «💾 Хранилище».

Экспорт/импорт — через телеграм-бот: экспорт шлёт ``plan_map.json``
документом, импорт принимает такой же файл и кладёт его обратно.
"""

from __future__ import annotations

from . import server  # noqa: F401 — re-exports for callers

generate_link = server.generate_link
disable_link = server.disable_link
get_link = server.get_link
get_token = server.get_token
is_enabled = server.is_enabled
register_http_endpoints = server.register_http_endpoints
plan_file = server.plan_file
load_plan = server.load_plan
save_plan = server.save_plan

__all__ = [
    "generate_link",
    "disable_link",
    "get_link",
    "get_token",
    "is_enabled",
    "register_http_endpoints",
    "plan_file",
    "load_plan",
    "save_plan",
]
