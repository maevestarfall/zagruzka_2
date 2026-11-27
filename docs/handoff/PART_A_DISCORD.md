# Часть A — Адаптер Discord (этап 2 ТЗ)

## Цель

Владелец нажимает в Telegram «Платформы → Discord», получает инструкцию,
вводит токен Discord-бота (через сайт или команду), бот подключается к
Discord и отвечает на команды меню. Пользователи Discord привязывают свой
аккаунт одноразовым кодом и получают те же экраны меню, что и в TG.

## Файлы (создать, ничего больше не трогать)

- `bot/gateway/adapters/discord_gw.py` — вся логика
- `tests/test_gateway_discord.py` — тесты

## Что реализовать

### 1. Хранение и статус подключения

Ключи в `addon_state` (ветка `gateway`):
- `discord_token: str | None` — токен бота
- `discord_status: dict` — `{"state": "off|connecting|online|error", "error": str|None, "guilds": int, "since": float}`

Функции модуля:
```python
def get_status() -> dict            # текущий статус для экранов
def set_token(token: str) -> None   # сохранить токен (не логировать!)
def clear_token() -> None           # отключение + удаление токена
async def start() -> None           # запустить клиент (если есть токен)
async def stop() -> None            # остановить клиент
```

### 2. Discord-клиент (ленивая зависимость)

- `import discord` — только внутри `start()`. Если библиотека не
  установлена: статус `error` с текстом «discord.py не установлен»,
  никаких исключений наружу.
- Клиент живёт как asyncio-задача. Реконнект при обрыве — с
  экспоненциальной задержкой (1с → 2с → ... → максимум 60с).
- Невалидный токен → статус `error`, задача завершается (не долбить API).

### 3. Команды в Discord

Slash-команды или префикс `!lilush` (на выбор, что проще и стабильнее):
- `/link <код>` — привязка: `identity.verify_code(code, "discord", str(user.id), label=str(user))`.
  Ответ: успех / неверный код / бан за перебор (текст из verify_code).
- `/menu` — показать текущий экран меню. Экран берётся через
  `bot.gateway.menu.build_screen(screen_id, tg_user_id)`, где
  `tg_user_id = identity.resolve_user_id("discord", str(user.id))`.
  Не привязан → подсказка про `/link` и как получить код в TG.
- Кнопки экрана рендерить как Discord UI-кнопки (components) с
  `custom_id = action`. По клику: `dispatch_action(action, ctx)` c
  `GatewayContext(platform="discord", external_id=str(user.id), user_id=tg_user_id)`.
  - `result.screen` → отредактировать сообщение новым экраном
  - `result.text` → ephemeral-ответ
  - `result.error == "tg_only"` → ответ «Этот раздел пока только в Telegram» + `result.deeplink`
  - `result.error == "forbidden"` → текст отказа
- Discord-лимит: до 5 кнопок в ряду, до 5 рядов. Если экран больше —
  показать первые 25 кнопок и текст «продолжение в Telegram».

### 4. Экраны для TG (замена заглушки этапа 1)

Зарегистрировать экран `gateway:discord:live` (builder через
`register_screen`) и действия:
- `gateway:discord:start` / `gateway:discord:stop` — вкл/выкл клиента
- `gateway:discord:forget` — удалить токен
Экран показывает: статус (`get_status()`), число серверов, ошибку если
есть, кнопки управления. Заглушку `gateway:discord` из этапа 1 НЕ
трогать — принимающая сторона сама переключит её на ваш экран.

### 5. Ввод токена

Токен вводится НЕ через TG-чат (FSM в существующих файлах трогать
нельзя). Вместо этого: действие `gateway:discord:settoken:<token>`
через `register_action_prefix("gateway:discord:settoken:", ...)` —
им пользуется сайт (Fable5 `POST /action`). В ответ — экран
`gateway:discord:live`. Проверка прав уже делается в dispatch
(action не начинается с `gateway:link`, значит owner-only).

## Тесты (минимум)

1. Модуль импортируется без установленного `discord.py`.
2. `set_token`/`clear_token`/`get_status` — с подменённым `addon_state`.
3. `settoken`-действие через `dispatch_action`: гость → forbidden,
   владелец → токен сохранён, экран вернулся.
4. Рендер экрана: у зарегистрированного `gateway:discord:live` все
   action ≤ 64 байта.
5. Логика «не привязан → подсказка /link» (функцией, без реального Discord).

## Критерии приёмки (из ТЗ)

- A4: владелец включает Discord из TG/сайта, статус виден на экране.
- A5: пользователь Discord привязывается кодом и видит то же меню.
- A10: отсутствие discord.py не ломает бота.
