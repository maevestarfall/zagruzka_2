# 🎯 Viktor style дебат (v3 STRICT) — встроенный пульт

В аддоне `bot/addons/debates/` появился новый подэкран — **«Viktor style дебат»**.
Кнопка живёт прямо в меню *🎤 Дебаты → 🚪 Двери* (рядом с обычными
debate-ii дверьми), но управляет отдельным backend'ом — **debate-server-v3**
с правилами `PRAVILO_DEBAT_v3` (banned phrases, evidence_quote, NRN cap,
phase gates A/B/C с TF-IDF cosine, owner directives).

## Путь UX
```
Главное меню → 🎤 Дебаты → 🎯 Viktor style дебат
   ├── ➕ Добавить v3-сервер   (URL → owner-token → имя)
   ├── 📡 Серверы              (multi-server, health, удалить)
   ├── 🆕 Новая v3-дверь        (Part A/B/C, title, 2 debater_id)
   └── 🔍 Двери                 → 🚪 view
         ├── 🔔 События        (?since=last_msg_id, streaming)
         ├── 📊 Acceptance     (phase gates report)
         ├── 💰 Budget         (per debater + hard caps)
         ├── 📦 Artifacts      (artifacts.zip скачивается прямо в чат)
         ├── ⏸ Pause / ▶ Resume / 🛑 Terminate / ✉ Inject note
         └── 🔄 Обновить
```

## Архитектура
- **UI** — `bot/addons/debates/viktor_v3.py` (~620 строк), namespace
  callback'ов `vsd:*` чтобы не конфликтовать с `debates:*`.
- **State** — `addons.debates.viktor_v3.{servers, doors, since}` в `state.json`.
- **Backend** — `debate-server-v3` (отдельный FastAPI сервис на Render).
  Бот к нему ходит через HTTP с `X-Owner-Token` для owner-операций
  и без токена для read-only (`/healthz`, `/door/:id/state`,
  `/door/:id/events`).

## Wiring
- `bot/addons/debates/handlers.py::_kb_home` — добавлен один ряд с кнопкой
  «🎯 Viktor style дебат» → `callback_data="vsd:open"`.
- `bot/addons/debates/__init__.py` — экспортирует `build_viktor_v3_router`.
- `bot/addons/__init__.py` — добавлен try/except блок, монтирующий
  router после обычного debates.

## Деплой backend'а
См. `debate-server-v3.zip` (присылал отдельно). Render Blueprint
deploy: один клик → Live → token в Environment → готово.

## Что НЕ изменилось
- Обычные `bot/addons/debates/` двери (старый debate-ii) работают как
  раньше — независимые серверы, независимые токены, независимые двери.
- Можно подключить одновременно и старый сервер debate-ii, и v3.
