# FIX_PART_3 — Экраны волны 2 + запись отклонения по slash-командам

Ты — один из трёх ИИ, работающих параллельно над остатком
`TZ1_FIX_LIST.md`. Прочитай сначала `TZ1_ORIGINAL.md` (§7, этап 4, §5),
`TZ1_FIX_LIST.md` (пункты 1.5, 2.1, 2.2) и
`docs/handoff/FIX_STATUS_REPORT.md` (общие правила, раздел 4).

**Твои файлы (другие НЕ трогать):**
- НОВЫЙ модуль `bot/gateway/screens_wave2b.py` (существующий
  `screens_wave2.py` НЕ менять — там рабочие экраны волны 3 с
  тестами; максимум — импортировать из него `_plain`, `_is_admin`,
  `_denied`, `_rows_from_tg` или скопировать эти хелперы)
- регистрация модуля: найди, где импортируется/регистрируется
  `screens_wave2` (grep `screens_wave2` по `bot/`), добавь рядом
  `screens_wave2b` тем же способом
- новые тесты: `tests/test_gateway_wave2b.py`

---

## Задача A — фикс 1.5: недостающие экраны волны 2 (MUST, ТЗ §7)

Реализованы экраны волны 3 (tokens, debates, promptbox, github,
projmap, misc) — их НЕ удалять. Надо ДОБАВИТЬ экраны волны 2 по тому
же паттерну (`screens_wave2.py` — образец: builder читает реальные
данные из storage/handlers, screen открывается через
`register_action`, mutating-действия — отдельные `register_action`,
гейтинг внутри builder через `access.can_admin` / `_denied`):

1. **Роли** (`roles:*`) — источник данных/меню: `bot/wizard.py`
   (builder ролей около строки 564, callbacks `roles:menu`).
   Показ списка ролей + текущей; выбор роли — mutating-действие.
2. **Память** — источник: `bot/addons/memory/handlers.py`
   (экран `main:memory`, builders в модуле). Просмотр статуса памяти;
   очистка — mutating, только владелец.
3. **Доступ** (`access:menu`) — ТОЛЬКО владелец. Источник:
   `bot/wizard.py` около строки 5779 (builder access-меню).
   Режим доступа / список допущенных.
4. **Терминал** (`term:*`) — ТОЛЬКО владелец. Источник: `bot/wizard.py`
   (callbacks `term:*`, builder около строки 1025/3600 — проверь).
   Статус терминала; вкл/выкл — mutating.
5. **Мозги/ключи** (`brains:*`) — ТОЛЬКО владелец. Источник:
   `bot/wizard.py` (builder мозгов, callbacks `brains:*`).
   Список мозгов + активный; переключение — mutating. Ключи API
   НИКОГДА не показывать целиком — маска как `_mask()` в адаптерах.
6. **Slack** (`slack:*`) — статус/подключение. Источник:
   `bot/addons/slack/handlers.py` (builders на строках ~89–111,
   callbacks `slack:*`).

Требования к каждому экрану:
- builder `build_<name>_screen(user_id) -> MenuScreen` читает
  РЕАЛЬНЫЕ данные (storage/addon-state), не заглушки;
- все callback-действия ≤ 64 байта;
- owner-only экраны: не-админу возвращать `_denied(...)`;
- деструктивные действия (очистка памяти и т.п.) — подтверждение
  вторым экраном/кнопкой.

## Задача B — фиксы 2.1/2.2: отклонение по slash-командам (SHOULD)

Кодом ничего не менять. Подготовить текст аргументированного
отклонения для `AI_CHANGELOG.md` (впишет интегратор):

- Текстовые команды `!lilush ...` оставлены вместо slash-команд:
  `tree.sync()` имеет жёсткий rate limit, глобальная синхронизация
  команд занимает до часа, а для self-hosted бота на 512 МБ RAM
  текстовые команды проще и не требуют дополнительного состояния.
- Следствие: `defer()` (п. 2.2) неактуален — long-running действий
  через interactions нет.
- MESSAGE CONTENT INTENT при текстовых командах обязателен — подсказка
  владельцу уже есть в статусе ошибки discord-адаптера (сделано в
  фиксе 1.2/2.3, см. FIX_STATUS_REPORT.md).

## Тесты (обязательно, без сети)

`tests/test_gateway_wave2b.py`, по образцу
`tests/test_gateway_wave2.py`:
- каждый builder возвращает MenuScreen с корректным screen_id/parent;
- все action ≤ 64 байта (переиспользуй хелпер из test_gateway_wave2);
- owner-only экраны: гость → denied-экран, владелец → полный;
- mutating-действия зарегистрированы и меняют состояние (мок storage);
- ключи/токены на экранах замаскированы.

## Сдача

- `python -m pytest tests/test_gateway_*.py` — все зелёные
  (baseline 157 passed).
- Запись для `AI_CHANGELOG.md` приложить текстом в ответе,
  НЕ вписывая в файл (см. FIX_STATUS_REPORT.md §4).
