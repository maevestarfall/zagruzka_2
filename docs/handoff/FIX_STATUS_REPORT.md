# FIX_STATUS_REPORT — сводный отчёт по TZ1_FIX_LIST (2026-07-08)

Документы:
- Оригинал ТЗ: `TZ1_ORIGINAL.md` (в корне проекта).
- Общий список доработок: `TZ1_FIX_LIST.md` (в корне проекта).
- Разбиение остатка на 3 части: `docs/handoff/FIX_PART_1.md`,
  `FIX_PART_2.md`, `FIX_PART_3.md` (этот каталог).

---

## 0. Аудит «fixing»-архива от предыдущего ИИ

`diff -rq` между `new-project-function (5).zip` (оригинал) и
`fixing-bugs.zip` («исправленный») — **ноль отличий, байт-в-байт**.
Предыдущий ИИ не исправил ничего. Подозрение пользователя подтверждено.
Базой для работы взят оригинальный проект.

---

## 1. УЖЕ СДЕЛАНО (в этом проекте, тесты зелёные)

### ✅ Фикс 1.2 — валидация Discord-токена до сохранения (MUST)

Файл: `bot/gateway/adapters/discord_gw.py`.

- Новая корутина `validate_token(token) -> (ok, bot_tag, error)`:
  `GET https://discord.com/api/v10/users/@me`,
  заголовок `Authorization: Bot <token>`, aiohttp, таймаут 10с
  (константы `DISCORD_API_ME`, `VALIDATE_TIMEOUT`). Токен не логируется.
- `_act_settoken`: валидация ДО записи в state. 401 → ошибка
  `bad_token` с текстом ТЗ (`INVALID_TOKEN_MSG`), токен НЕ сохраняется.
  200 → сохранение токена + `bot_tag` (ключ state `discord_bot_tag`,
  геттер `get_bot_tag()`).
- Экран `gateway:discord:live` показывает «Бот: <tag>».
- `clear_token()` чистит и bot_tag.
- Текст ошибки `PrivilegedIntentsRequired` приведён к формулировке ТЗ.

### ✅ Фикс 2.3 — RAM-флаги discord-клиента (SHOULD)

Файл: `bot/gateway/adapters/discord_gw.py`, `_runner()`.

- `discord.Intents.none()` + только `guilds`, `guild_messages`,
  `dm_messages`, `message_content`.
- `chunk_guilds_at_startup=False`,
  `member_cache_flags=discord.MemberCacheFlags.none()`,
  с fallback для старых версий discord.py (`try/except TypeError`).

### ✅ Тесты

`tests/test_gateway_discord.py`:
- обновлён `test_settoken_owner_saves_token_and_returns_live_screen`
  (мок `validate_token`, без реальной сети, проверка `get_bot_tag`);
- добавлены `test_settoken_invalid_token_not_saved`,
  `test_validate_token_empty_returns_error`.

**Прогон:** `python -m pytest tests/test_gateway_*.py` →
**157 passed** (было 155 + 2 новых). Регрессий нет.

Запись в `AI_CHANGELOG.md`: № 31.

---

## 2. ОСТАЛОСЬ (разбито на 3 независимые части)

| Часть | Пункты TZ1_FIX_LIST | Файлы (только их трогать) |
|---|---|---|
| `FIX_PART_1.md` | 1.1 (FSM-ввод токена в TG), 1.3 (уведомления владельцу) | `bot/gateway/wizard_ui.py`, `bot/gateway/identity.py`, новые тесты |
| `FIX_PART_2.md` | 1.4 (VK-пагинация), 2.4 (чат с мозгом из Discord/VK) | `bot/gateway/adapters/vk_gw.py`, `bot/gateway/adapters/discord_gw.py` (только блок on_message для 2.4), новые тесты |
| `FIX_PART_3.md` | 1.5 (экраны волны 2), 2.1/2.2 (запись отклонения по slash-командам) | новый `bot/gateway/screens_wave2b.py`, `AI_CHANGELOG.md`, новые тесты |

Части спроектированы так, чтобы НЕ пересекаться по файлам
(единственное мягкое пересечение: часть 2 добавляет код в
`discord_gw.py`, где уже сделаны фиксы 1.2/2.3 — менять только
`on_message`-обработчик, ничего из сделанного не переписывать).

Пункты БЛОКА 3 (persistent-диск `/data`, замер RAM) — не код,
выполняет владелец на хостинге; в части не входят.

---

## 3. Инструкция для ИИ-интегратора (после выполнения 3 частей)

1. Слить части в порядке 1 → 2 → 3 (конфликтов по файлам быть не
   должно; при конфликте в `tests/` — объединить оба набора тестов).
2. Прогнать `python -m pytest tests/test_gateway_*.py` — все зелёные.
   Baseline до интеграции: 157 passed.
3. Прогнать полный suite `python -m pytest tests/` и сравнить с
   baseline (в проекте исторически ~29 падений, НЕ связанных со
   шлюзом, — их не чинить, новых падений не добавлять).
4. Убедиться, что ни в одной части токены не попали в логи
   (grep по `logger.*token`).
5. Дописать в `AI_CHANGELOG.md` одну итоговую запись об интеграции
   (формат — как записи 27–31).

## 4. Общие правила для всех трёх ИИ (из TZ1_FIX_LIST / handoff/README)

- Трогать ТОЛЬКО файлы своей части + добавлять новые тесты.
- Тесты — pytest, без реальной сети (aiohttp/Bot мокается monkeypatch'ем).
- Токены НИКОГДА не логировать (маска `abcd...wxyz`, есть `_mask()`).
- Callback-действия ≤ 64 байта.
- Гейтинг прав — через `bot.access.can_admin` (в dispatch уже есть
  owner-only для `gateway:*`).
- Свою запись в `AI_CHANGELOG.md` НЕ добавлять — это сделает интегратор
  (чтобы не конфликтовать в одном файле). Вместо этого приложить
  текст записи отдельным блоком в конце своего ответа.
