# «🎓 Обучалка» — стартовый сосуд «Кодинг: веб-сайт» (каркас + задания).
"""Seeds the very first vessel so a teacher has work from the first minute.

The vessel is the «каркас» the user described: a macro map of what a small
website codebase should look like (which files, how many functions in each,
what must stay small, where the load goes) plus ~16 leaves, each with a
pre-written task («маленькое задание») for the teacher/student to fill in.

Runs once — if any vessel already exists, it does nothing.
"""

from __future__ import annotations

from . import store

_MACRO_MAP = """\
КАРКАС: маленький веб-сайт (страница + форма + API).
Файлы и их размер:
- index.html — 1 файл, разметка страницы, без логики. Маленький.
- styles.css — 1 файл, только внешний вид. Маленький.
- app.js — 3–5 маленьких функций UI (рендер, обработчики). Каждая ≤ 20 строк.
- api.js — 2–3 функции запросов к серверу (fetch-обёртки). Маленькие.
- server.py — сервер: 1 функция на маршрут (~4–6 маршрутов) + 1 общий
  обработчик ошибок. Сюда идёт нагрузка (валидация, БД).
- db.py — 3–4 функции работы с хранилищем. Средние, но по одной задаче.
Принцип: интерфейсные файлы маленькие, нагрузка — в server.py/db.py.
Каждый листик ниже = один кусочек этого каркаса."""

# (title, task) — position follows list order.
_LEAVES: list[tuple[str, str]] = [
    ("Структура index.html",
     "Напиши идеальный index.html для каркаса: шапка, main с формой (имя, email), "
     "список результатов, подключение styles.css и app.js. Без инлайн-стилей и скриптов."),
    ("Базовый styles.css",
     "Напиши styles.css: система из 3–4 цветов, мобильная вёрстка через flexbox, "
     "классы для формы и списка. Без фреймворков."),
    ("app.js: функция render",
     "Напиши функцию render(items) — принимает массив объектов и отрисовывает список "
     "в DOM. ≤ 20 строк, без глобальных побочных эффектов кроме целевого узла."),
    ("app.js: обработчик формы",
     "Напиши обработчик submit формы: собрать значения, вызвать api.createItem, "
     "по ответу вызвать render. Обработай ошибку показом сообщения."),
    ("app.js: валидация на клиенте",
     "Напиши функцию validateForm(values) — проверка имени (непустое) и email "
     "(простое регулярное выражение). Возвращает {ok, errors}."),
    ("api.js: обёртка fetchJson",
     "Напиши fetchJson(url, options) — обёртка над fetch: JSON-заголовки, "
     "проверка status, разбор ошибок сервера в понятный throw."),
    ("api.js: createItem и listItems",
     "Напиши createItem(data) и listItems() поверх fetchJson. По одной задаче "
     "на функцию, без дублирования путей — базовый URL в константе."),
    ("server.py: каркас приложения",
     "Напиши каркас сервера (aiohttp или Flask): создание приложения, регистрация "
     "маршрутов списком, запуск. Одна функция = один маршрут."),
    ("server.py: маршрут GET /items",
     "Напиши обработчик GET /items: читает из db.list_items, отдаёт JSON. "
     "Никакой логики кроме вызова db и сериализации."),
    ("server.py: маршрут POST /items",
     "Напиши обработчик POST /items: разбор JSON, серверная валидация (обязательная, "
     "даже если клиент валидировал), вызов db.create_item, ответ 201."),
    ("server.py: общий обработчик ошибок",
     "Напиши middleware/обработчик ошибок: любые исключения → JSON {error}, "
     "код 400/404/500 по типу. Логирование через logging, не print."),
    ("db.py: инициализация SQLite",
     "Напиши init_db(): создание таблицы items (id, name, email, created_at) "
     "в SQLite. Идемпотентно (IF NOT EXISTS)."),
    ("db.py: create_item и list_items",
     "Напиши create_item(name, email) и list_items(): параметризованные запросы "
     "(никакой конкатенации SQL), возврат словарей."),
    ("Связка: поток данных формы",
     "Опиши по шагам поток: submit → validateForm → api.createItem → POST /items → "
     "серверная валидация → db.create_item → ответ → render. Укажи, где что может "
     "упасть и кто это ловит."),
    ("Принцип: маленькие функции",
     "Сформулируй правило: когда функцию пора делить, почему UI-функции маленькие, "
     "а нагрузка живёт на сервере. Приведи 1 плохой и 1 хороший пример."),
    ("Принцип: безопасность минимум",
     "Сформулируй минимум безопасности для этого каркаса: параметризованный SQL, "
     "валидация на сервере, экранирование при рендере в DOM. По одному примеру."),
]


def ensure_seed() -> bool:
    """Create the starter vessel once. Returns True if it seeded now."""
    store.init_db()
    if store.list_vessels():
        return False
    vessel_id = store.create_vessel(
        "Кодинг: веб-сайт (каркас)", macro_map=_MACRO_MAP, priority=100,
    )
    for pos, (title, task) in enumerate(_LEAVES):
        store.create_leaf(vessel_id, title, task=task, position=pos)
    store.journal_add("system", "seed", "создан стартовый сосуд «Кодинг: веб-сайт»")
    return True
