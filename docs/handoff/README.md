# Передача работ: этапы 2-6 ТЗ «Кроссплатформенный шлюз»

Этап 1 (каркас `bot/gateway/`, кнопка «Платформы» в TG, меню на сайте)
уже реализован и лежит в этом проекте. Оставшаяся работа разбита на
3 независимые части — их можно делать параллельно и в любом порядке.

## Части

| Часть | Файл задания | Что делает | Результат (файлы) |
|-------|--------------|------------|-------------------|
| A | `PART_A_DISCORD.md` | Адаптер Discord (этап 2 ТЗ) | `bot/gateway/adapters/discord_gw.py`, `tests/test_gateway_discord.py` |
| B | `PART_B_VK.md` | Адаптер VK (этап 3 ТЗ) | `bot/gateway/adapters/vk_gw.py`, `tests/test_gateway_vk.py` |
| C | `PART_C_SCREENS.md` | Покрытие экранов волн 2-3 (этап 4 ТЗ) | `bot/gateway/screens_wave2.py`, `tests/test_gateway_wave2.py` |

## Железные правила (иначе слияние сломается)

1. **Только новые файлы.** Ничего в существующих файлах проекта не менять:
   ни `bot/wizard.py`, ни `bot/main.py`, ни `bot/gateway/*.py`, ни
   `bot/addons/*`. Все точки подключения уже существуют — см. раздел
   «Публичные API» ниже. Если кажется, что без правки существующего
   файла не обойтись — НЕ править, а описать проблему в
   `docs/handoff/QUESTIONS.md` (создать файл) и продолжить остальное.
2. **Один файл реализации + один файл тестов на часть.** Имена файлов —
   строго как в таблице выше.
3. **Никаких новых зависимостей без флага.** `discord.py` / `vkbottle`
   (или иная библиотека) импортируются только лениво, внутри функций.
   Отсутствие библиотеки не должно ронять импорт модуля — модуль
   обязан импортироваться на голом Python + aiogram + aiohttp.
4. **Секреты** (токены Discord/VK) хранить только через
   `bot.addons.state.set_("gateway", ...)` — они попадают в state.json,
   который уже chmod 600. В логи токены не писать.
5. **Каждое действие/кнопка ≤ 64 байта** в UTF-8 (лимит Telegram,
   тест на это уже есть и будет гонять и ваши экраны).

## Публичные API этапа 1 (единственные точки интеграции)

```python
# Экраны: зарегистрировать нейтральный экран
from bot.gateway.menu import MenuScreen, MenuButton, register_screen
register_screen("myscreen", builder)   # builder(user_id) -> MenuScreen

# Действия: зарегистрировать обработчик кнопки
from bot.gateway.dispatch import register_action, register_action_prefix
register_action("gateway:discord:connect", handler)
register_action_prefix("gateway:discord:", handler)  # параметризованные

# Обработчик: async def handler(action: str, ctx: GatewayContext) -> ScreenResult
from bot.gateway.dispatch import GatewayContext, ScreenResult, dispatch_action

# Привязка аккаунтов (одноразовые коды, TTL 10 мин, бан за перебор)
from bot.gateway import identity
identity.resolve_user_id("discord", external_id)   # -> tg_user_id | None
identity.verify_code(code, "discord", external_id, label="Name#123")
identity.unlink("discord", external_id)

# Хранилище настроек (ветка gateway в state.json)
from bot.addons import state as addon_state
addon_state.get("gateway", "discord_token", None)
addon_state.set_("gateway", "discord_token", token)

# Проверка прав (как везде в проекте)
from bot import access
access.can_admin(tg_user_id)   # владелец/со-владелец
```

Экраны и действия из ваших модулей автоматически появятся в TG,
на сайте (Fable5 `/menu`, `/action`) и в панели VISION — рендеринг
уже написан в этапе 1, регистрируйте и всё.

## Блокеры этапа 0 (проверить ДО начала частей A и B)

Из ТЗ §9Б, обязательные research-пункты — выполняются на хостинге,
результат записать в `AI_CHANGELOG.md`:

1. **Persistent disk:** проверить, примонтирован ли `/data` на хостинге
   (тариф с диском). Если диска нет — токены Discord/VK и привязки
   будут теряться при каждом рестарте, и части A/B бессмысленны до
   решения этой проблемы.
2. **RAM:** замерить фактическую свободную память процесса бота на
   текущем тарифе. discord.py + vkbottle поднимаются в том же процессе;
   если запас < ~150 МБ — сначала решить вопрос с тарифом.

## Как сдавать

- Прогнать: `python -m pytest tests/test_gateway_stage1.py tests/<ваш файл> -q`
  — всё зелёное.
- `python -m compileall bot/` — без ошибок.
- Отдать только ваши новые файлы (2 шт. на часть + при необходимости
  `QUESTIONS.md`). Интеграцию в `main.py` и финальную сборку делает
  принимающая сторона.
