# Часть B — Адаптер VK (этап 3 ТЗ)

## Цель

То же, что часть A, но для VK: владелец подключает токен группы VK,
бот отвечает в сообщениях сообщества, пользователи VK привязываются
одноразовым кодом и работают с теми же экранами меню.

## Файлы (создать, ничего больше не трогать)

- `bot/gateway/adapters/vk_gw.py` — вся логика
- `tests/test_gateway_vk.py` — тесты

## Что реализовать

### 1. Хранение и статус

Ключи в `addon_state` (ветка `gateway`):
- `vk_token: str | None` — токен группы (scope: messages)
- `vk_status: dict` — `{"state": "off|connecting|online|error", "error": str|None, "group": str|None, "since": float}`

Функции: `get_status()`, `set_token(token)`, `clear_token()`,
`async start()`, `async stop()` — сигнатуры как в части A.

### 2. VK-клиент (ленивая зависимость)

- Библиотека на выбор: `vkbottle` или чистый Long Poll через aiohttp
  (aiohttp уже есть в проекте — предпочтительный вариант, ноль новых
  зависимостей). Если выбираете vkbottle — импорт только внутри
  `start()`, отсутствие библиотеки не роняет модуль.
- Bots Long Poll API: `groups.getLongPollServer` → цикл `wait=25`.
  Реконнект с экспоненциальной задержкой (1с → 60с макс).
- Невалидный токен → статус `error`, цикл не запускается.

### 3. Команды в VK

Текстовые команды в личных сообщениях сообщества:
- `код <XXXXXX>` — привязка: `identity.verify_code(code, "vk", str(from_id), label=имя)`.
- `меню` — текущий экран. `tg_user_id = identity.resolve_user_id("vk", str(from_id))`;
  не привязан → подсказка про «код ...» и где взять код в TG.
- Кнопки экрана рендерить VK-клавиатурой (inline keyboard, JSON):
  `payload = {"action": "<action>"}`. Лимит VK: 10 кнопок (inline) —
  если экран больше, первые 10 + текст «продолжение в Telegram».
- По нажатию (`message_event` или text+payload):
  `dispatch_action(action, ctx)` с
  `GatewayContext(platform="vk", external_id=str(from_id), user_id=tg_user_id)`.
  Маппинг результата как в части A (screen → новое сообщение с
  клавиатурой, text → текст, tg_only → подсказка + deeplink,
  forbidden → отказ).

### 4. Экраны для TG

Зарегистрировать экран `gateway:vk:live` и действия:
- `gateway:vk:start` / `gateway:vk:stop` / `gateway:vk:forget`
- `gateway:vk:settoken:<token>` через `register_action_prefix`
  (ввод токена с сайта, как в части A).
Заглушку `gateway:vk` из этапа 1 НЕ трогать.

## Тесты (минимум)

1. Импорт модуля без vkbottle (если используете её).
2. `set_token`/`clear_token`/`get_status` с подменённым `addon_state`.
3. `settoken` через `dispatch_action`: гость → forbidden, владелец → ок.
4. Все action экрана `gateway:vk:live` ≤ 64 байта.
5. Парсинг команды «код XXXXXX» (регистронезависимо, лишние пробелы).

## Критерии приёмки (из ТЗ)

- A6: владелец включает VK из TG/сайта, статус виден на экране.
- A7: пользователь VK привязывается кодом и видит то же меню.
- A10: отсутствие библиотеки VK не ломает бота.
