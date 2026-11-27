# FIX_PART_1 — FSM-ввод токена в TG + уведомления владельцу

Ты — один из трёх ИИ, работающих параллельно над остатком
`TZ1_FIX_LIST.md`. Прочитай сначала `TZ1_ORIGINAL.md` (§4.4, §4.5, R6),
`TZ1_FIX_LIST.md` (пункты 1.1 и 1.3) и `docs/handoff/FIX_STATUS_REPORT.md`
(общие правила, раздел 4).

**Твои файлы (другие НЕ трогать):**
- `bot/gateway/wizard_ui.py`
- `bot/gateway/identity.py`
- новые тесты: `tests/test_gateway_fsm_token.py`,
  `tests/test_gateway_owner_notify.py`

Фиксы 1.2/2.3 уже сделаны (см. отчёт) — `discord_gw.validate_token`
существует, используй его.

---

## Задача A — фикс 1.1: FSM-ввод токена в Telegram (MUST, ТЗ §4.4, R6)

Сейчас в `bot/gateway/wizard_ui.py` есть `build_gateway_router()` с
одним callback-хендлером `on_gateway_callback` — он рендерит экраны
через `dispatch_action`. Кнопки «Подключить» на экранах
`gateway:discord:live` / `gateway:vk:live` не умеют принимать токен
сообщением.

Сделать:

1. `StatesGroup` (aiogram.fsm) с состоянием «жду токен», в
   `state.data` хранить платформу (`discord` | `vk`).
2. Новые callback-действия `gateway:discord:enter_token` и
   `gateway:vk:enter_token` (константа `ENTER_TOKEN_ACTION` для discord
   уже объявлена в `discord_gw.py`). Обработать их в
   `on_gateway_callback` ДО `dispatch_action`: проверить
   `bot.access.can_admin(user_id)` (не владелец → `query.answer` с
   отказом), поставить FSM-состояние, показать текст «Пришли Bot Token
   сообщением. Сообщение будет немедленно удалено.» + inline-кнопка
   «Отмена» (`gateway:<platform>:cancel_token`).
3. Message-хендлер в том же роутере (фильтр по FSM-состоянию):
   - НЕМЕДЛЕННО `await message.delete()` в try/except — ДО валидации,
     даже если токен кривой;
   - Discord: `ok, tag, err = await discord_gw.validate_token(text)`;
     ok → `discord_gw.set_token(...)` + сохранить bot_tag
     (см. `_set_bot_tag` / state-ключ `discord_bot_tag`), ответ —
     рендер экрана `gateway:discord:live`; не ok → текст ошибки,
     остаёмся в состоянии (повторный ввод) с кнопкой «Отмена»;
   - VK: симметрично через существующую валидацию vk_gw
     (`_resolve_group` через `groups.getById`; если удобнее — добавь
     в vk_gw ТОЛЬКО тонкую обёртку `validate_token`, ничего больше
     в vk_gw не меняй — файл принадлежит части 2; при конфликте
     интегратор объединит);
   - токен НИКОГДА не логировать и не цитировать в ответе.
4. «Отмена» — `state.clear()` + вернуть live-экран платформы.
5. Кнопку «Подключить (ввести Bot Token)» добавить на live-экраны:
   discord — в `discord_gw.build_live_screen` она уже уместна, но
   этот файл не твой; поэтому добавляй кнопку на СТОРОНЕ РЕНДЕРА в
   `wizard_ui._render_screen`/`_screen_text` только если экран
   `gateway:discord:live`/`gateway:vk:live` и токена нет — либо
   опиши интегратору одну-строчную вставку в builder'ы. Выбери
   вариант, не трогающий чужие файлы.

## Задача B — фикс 1.3: уведомления владельцу (MUST, ТЗ §4.5)

Файл `bot/gateway/identity.py`:

1. Ленивый доступ к aiogram Bot — тот же паттерн, что `bot_username`
   в addon-state (посмотри, как это сделано в `bot/addons/state.py` /
   как бот кладётся в state при старте `bot/main.py`). Обёртка
   `async def _notify_owner(text, reply_markup=None)`: получает
   owner_id (см. `bot/access.py` / `bot/config.py`), шлёт сообщение,
   всё в try/except + `logger.warning` — отправка НЕ должна ломать
   flow привязки.
2. В `verify_code(...)` при успехе — уведомить владельца:
   «К аккаунту привязан <platform>-аккаунт <label> (id <external_id>)»
   + inline-кнопка «Отвязать»
   (`gateway:unlink:<platform>:<external_id>`, ≤ 64 байта — проверь
   длину, при переполнении обрезать/хешировать external_id и хранить
   соответствие в state).
3. В `_register_failure(...)` при бане (5-я неверная попытка) —
   уведомить: «Попытка перебора кодов привязки с <platform>
   id <external_id>. Аккаунт заблокирован на 1 час.»
4. Обработчик действия `gateway:unlink:<platform>:<external_id>` —
   через `dispatch.register_action_prefix("gateway:unlink:", ...)`,
   внутри `identity.unlink(...)`, owner-only (гейтинг `gateway:*`
   в dispatch уже есть).

## Тесты (обязательно, без сети)

- FSM: состояние ставится только для can_admin; сообщение с токеном
  удаляется (мок message.delete) даже при невалидном токене; валидный
  токен сохраняется (мок validate_token); «Отмена» чистит состояние.
- Notify: verify_code ok → _notify_owner вызван с нужным текстом
  (мок); бан → уведомление; исключение в отправке НЕ ломает
  verify_code (привязка всё равно создаётся).

## Сдача

- `python -m pytest tests/test_gateway_*.py` — все зелёные
  (baseline 157 passed).
- Запись для `AI_CHANGELOG.md` приложить текстом в ответе,
  НЕ вписывая в файл (см. FIX_STATUS_REPORT.md §4).
