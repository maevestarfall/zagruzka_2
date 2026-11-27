# 🔧 Регенерация — самодиагностика и саморемонт бота

Бот смотрит на свой код **изнутри**, находит больное место и **Кодер** (addon
`coder`) пишет фикс. На выходе — три формата, плюс свободный ввод «скажи
словами, что не так» и фоновый анализ.

Кнопка: главное меню → **🔧 Регенерация** (`main:regen`). Только владелец
(`can_use`).

## Что делает скан (`bot/addons/regen/_scan_worker.py`)
Запускается отдельным процессом (`python -m bot.addons.regen._scan_worker`),
печатает один JSON-отчёт. Проверки:
1. **Синтаксис** — компиляция всех `bot/**/*.py`.
2. **Импорты** — импорт core-модулей + всех `bot.addons.*` (тяжёлые worker/ML
   модули пропускаются: их import-time баги и так роняют бот при старте).
3. **Роутеры** — `build_addon_routers()` собирается без ошибок.
4. **Lint** — `ruff --select F821,F811` (F821 = неопределённое имя =
   главный footgun «недомёрженного» кода; F811 = переопределение).
5. **Мёртвые кнопки** — статически: `callback_data=` без обработчика
   (`F.data` / `startswith` / `==`).

> На проде (Render) `ruff` может отсутствовать → глубокий lint пропускается,
> остаются синтаксис+импорты+роутеры+кнопки. UI показывает пометку.

Отчёт: `{root, ruff_available, counts{error,warn}, issues[]}`. Каждый issue:
`{id, kind, severity, where, symbol, message, snippet}`.

## Кодер пишет фикс (`bot/addons/coder/regen_brain.py`)
`generate_fix(problem, code_context, diagnosis, research)` → `FixResult`
(`problem, root_cause, instructions, files{path: полный код}, diff,
confidence, notes`). Это **настоящая** генерация через LLM-слот (`_llm`),
а не театральный `run_fsm_loop`. «Код пишет именно кодер».

## «Как решают люди» (`research.py`)
`research_problem()` через веб-поиск (Tavily) — биас на текст ошибки. Если
недоступно — фикс делается и без ресёрча.

## Три формата выдачи (`outputs.py`)
1. **📦 ZIP** — копия репозитория с применённым фиксом + `REGEN_FIX_NOTES.md`;
   каждый изменённый `.py` проходит `py_compile` (ошибки помечаются в notes).
2. **🧩 JSON** — `regen_fix_*.json` с полным кодом файлов + инструкцией +
   описанием проблемы (отдать другому ИИ).
3. **📄 Описание** — plain-text «где и что болит».

## «Скажи словами, что не так»
`regen:tell` → состояние `RegenStates.awaiting_problem`. Текст пользователя =
`problem`; контекст подтягивается грепом по упомянутым файлам/идентификаторам
(`_regen_guess_context`); далее ресёрч + кодер + 3 формата.

## Фоновый анализ (`background.py`, `store.py`)
Тоггл `addons.regen.bg_enabled` (по умолчанию OFF — цикл дёшево спит). Когда
ON — скан каждые `interval_min` (по умолч. 6ч), DM владельцу о **новых**
проблемах. Режимы: **симуляция** (только статический скан) и **реал** (плюс
self-ping `PUBLIC_URL/ping` — реальный «выход в мир»). Зарегистрирован в
`bot/main.py` рядом с mailbox-поллером.

## Где что лежит
- UI-хендлеры — в `bot/wizard.py` на `wizard_router` (`cb_regen`,
  `regen_capture_problem`, `_regen_*`). Без отдельного роутера.
- `bot/addons/regen/` — `scanner.py`, `_scan_worker.py`, `store.py`,
  `research.py`, `outputs.py`, `background.py`, `__init__.py`.
- Кодоген — `bot/addons/coder/regen_brain.py`.
- Тесты — `tests/test_regen_addon.py`.
