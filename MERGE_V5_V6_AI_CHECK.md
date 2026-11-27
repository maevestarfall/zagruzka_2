# lilush-bot v5 — merged + AI CHECK + media-provider fix

This archive is **v5** as the base, with the useful v6 changes overlaid,
the `🛡 AI CHECK` addon added, and the **image-generation provider lock-in
fix** Chloe asked about.

## What's new vs. the previous merged archive

### 🐛 Image generation no longer forces deAPI
**Reported issue:** when the user pasted any non-deAPI URL into a media
slot, the bot crashed with *«Сейчас работает только deAPI.»*

**Fix (`bot/media.py`, `bot/media_ui.py`):**
* Rewrote `bot/media.py` around a provider-family dispatcher.
* Auto-detection added — anything unknown maps to `openai_compatible`
  so the bot stops insisting on deAPI.
* Adapters wired up for **deAPI** (original), **OpenAI / OpenAI-compatible**
  (`/v1/images/edits`, `/v1/images/generations`), **fal.ai** (queue API),
  **Replicate** (predictions API). See `KNOWN_MEDIA_PROVIDERS` and
  `_provider_family()`.
* `get_balance` / `list_models` no longer throw `MediaError` on non-deAPI
  slots — they return `0.0` / a static catalogue instead.
* `_fetch_url` understands `data:image/...;base64,...` so the OpenAI-style
  inline payload flows straight into the existing send-as-document /
  send-as-photo path.
* UI hint text (`bot/media_ui.py`) now explicitly lists every supported
  provider family and tells the user that unknown URLs are treated as
  OpenAI-compatible.
* Error labels in `media_ui.py` rewritten from `deAPI:` to `Провайдер:`
  so the message no longer lies about which provider raised.

**How to use:**
1. /setup → 🎬 Мозг для видео и фото → выбери слот → URL.
2. Вставь URL своего провайдера, например `https://api.openai.com` или
   любой OpenAI-совместимый прокси.
3. Вставь API key.
4. В поле photo_model укажи slug модели (для OpenAI — `gpt-image-1`).
5. Готово. Никаких «обязательно deAPI» в логах больше нет.

### v6 deltas (carried over from the previous merged archive)
* `addons/memory/handlers.py` — экран *📂 Файлы*: размеры
  downloads / releases / projects, свободное место, последние видео.
* `addons/pretty_text/handlers.py` — красивый текст включён по умолчанию
  (явный OFF от юзера не ломается).
* `wizard.py` — общий `_dispatch_download`, авто-роут на голую видео-ссылку,
  кнопка *🔄 Убрать выбор* на экранах /dl и bash2mp4.
* `workers/base.py` — `send_chat_html`, `notify_stage_progress`,
  `notify_job_failed`, `start_notice`, авто-пинг в чат при падении этапа.
* `workers/analyzer.py` — *🔎 Видео скачано — анализирую…*, батч
  *🎬 Нашёл N моментов — нарезаю*.
* `workers/downloader.py` — стартовый пинг *📥 Качаю видео…* и пинг при
  падении download.
* Тесты v6: `test_analyzer.py`, `test_memory_addon.py`,
  `test_wizard_natural_language_dl.py`, `test_workers.py`.

### 🛡 AI CHECK addon (carried over)
* `bot/addons/ai_check/` — тонкая UI-обёртка над `ai-tool-inspector`.
* `services/ai-tool-inspector/` — Node.js inspector. Запуск:
  ```
  cd services/ai-tool-inspector
  cp env.example .env   # TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  pnpm install
  node server.mjs       # API на :3001
  ```
* Кнопка `🛡 AI CHECK` появляется в главном меню (admin-only). Внутри:
  Health, Модели и инструменты, Сводка / TrustScore, Токен для ИИ,
  Сервер инспектора.

## Sanity checks
* `find bot -name '*.py' -exec python3 -m py_compile {} \;` → clean.
* Все импорты сохранены; пути и роутеры не двинулись.
* Никакие v5-сценарии не сломаны — deAPI-слоты ходят по тем же
  эндпоинтам, что и раньше.

## 2026-06-10: AI CHECK redesign (под UX inspector-бота)

Полностью переписал `bot/addons/ai_check/handlers.py` так, чтобы экран
выглядел как у standalone Telegram-бота из `services/ai-tool-inspector/bot.mjs`.

* Главное меню — 6 inline-кнопок в 2 колонки:
  `📊 Статистика` / `🌐 Сервисы` / `🤖 Модели` /
  `🔗 Ссылки для ИИ` / `🔑 Токены` / `❓ Помощь`,
  плюс `➕ Добавить сервер` и `« Назад`.
* Поддержка **нескольких** inspector-серверов (раньше был один URL):
  - state: `addons.ai_check.servers` (список `{id,url,name,addedAt}`).
  - старый ключ `server_url` мигрирует автоматически.
  - добавить через `➕ Добавить сервер` (URL + опц. имя через пробел),
    удалить — кнопкой 🗑 на экране «Сервисы».
* «📊 Статистика» — сбор `/api/health` со всех серверов, прогресс-бар
  достоверности (TrustScore) `[██████░░░░] 60%`.
* «🌐 Сервисы» — статус, ID, uptime, дата добавления, кнопки 🗑.
* «🤖 Модели» — список моделей первого онлайн-сервера, доли
  verified/failed/unchecked, TrustScore, inline `🔗 Ссылка` и
  `✅ Проверить все`.
* «🔗 Ссылки для ИИ» — выбор модели → персональный probeUrl/verifyUrl
  + пример тела `{toolId, result, message}`.
* «🔑 Токены» — `/api/tokens` первого онлайн-сервера.
* «✅ Проверить все» — массовый `POST /api/models/:id/tools/:tid/verify`
  с задержкой 300 мс (как в bot.mjs).
* `❓ Помощь` — короткая встроенная справка.

Сервисы и сам inspector живут в `services/ai-tool-inspector/`
(обновлён до v26 — добавились `.dockerignore`, `.gitattributes`,
`.gitignore`, `.npmrc`; код `server.mjs`/`bot.mjs` идентичен).

Sanity:
* `python3 -m compileall bot/` → clean.
* `ast.parse(handlers.py)` → ok.
* Wizard / `bot/addons/__init__.py` уже триггерят аддон по
  `callback_data="main:ai_check"` — ничего не двигал.

## 2026-06-10: Новая кнопка «🎤 Дебаты» (бандл debate-ii)

В архив завёрнут **проект `debate-ii`** (FastAPI + Telegram-бот для
многодверных ИИ-дебатов) и встроена отдельная кнопка в главное меню
Lilush.

* Сам проект — в `services/debate-ii/`
  (`app.py`, `debate.py`, `containers.py`, `telegram_bot.py`, `run.py`,
  `render.yaml`, `requirements.txt`, `README.md`, `.gitignore`).
* Новый аддон — `bot/addons/debates/` (handlers.py, __init__.py).
* `bot/addons/__init__.py` подключает `build_debates_router()` после
  `ai_check` (тем же try/except-паттерном).
* `bot/wizard.py` — в главном меню под AI CHECK добавлена строка
  `[🎤 Дебаты] → main:debates`.

### UX (повторяет standalone telegram_bot.py из debate-ii)

Главное меню (callback prefix `debates:`):

```
🚪 Двери          🌐 Сервисы
🆕 Создать дверь  ❓ Помощь
➕ Добавить сервер
« Назад
```

* **🚪 Двери** — список со всех онлайн-серверов, прямо в списке кнопки
  для перехода в каждую дверь + `🆕 Создать в <server>` под каждым.
* **🌐 Сервисы** — статус, ID, маска токена, дата, 🗑 удалить.
* **🆕 Создать дверь** — если серверов несколько, спрашивает на каком.
* **➕ Добавить сервер** — двухшаговый FSM (URL → DEBATE_TOKEN).
  `/healthz` пинг + проверка `/doors` для валидации токена.

Внутри двери (callback prefix `debates:door:`):

* Шапка с заголовком, ссылкой `{server}/door/{id}`, режимом
  («идут дебаты» / «ждём набора N/M»), участниками и повесткой.
* **🔔 События** — фетчит `/door/{id}/events`, показывает последние
  30 событий ленты (➡️ передал ход, ⚡ взял пункт параллельно,
  ✅ пункт решён). Обновление — повторным тапом.
* **🎙 Запустить сессию** — FSM: пользователь шлёт текст или
  `.txt`/`.md` файл; первая строка / подпись = `<кол-во> <тема>`,
  остальное → `tz_text`. Размер ТЗ ограничен 200 КБ (как в
  оригинальном `telegram_bot.py`).
* **🏁 Завершить сессию** — `POST /door/{id}/session/end`.
* **✕ Удалить дверь** — `DELETE /doors/{id}`.

### Что под капотом

* Состояние под `addons.debates.servers` — список
  `{id, url, name, token, addedAt}`.
* HTTP вызовы через `httpx.AsyncClient(timeout=10)`, токен сервера
  подсовывается в `?token=...` (как и в `services/debate-ii/telegram_bot.py`).
* Owner-gate тот же, что у AI CHECK — `bot.wizard._is_owner`.
* Callback’ы изолированы под `debates:*`, ни с чем не пересекаются.

### Проверки

* `python3 -m compileall bot/` — exit 0.
* `ast.parse(bot/addons/debates/handlers.py)` — ok.
* End-to-end проверка против поднятого `services/debate-ii`:
  `add_server → /healthz → /doors → POST /doors → /session/start →
  /state → /session/end → DELETE /doors/{id}` — все 200 OK,
  рендеры дверей/сервисов/сессии собираются как ожидается.
* `build_addon_routers()` — поднимает все 12 роутеров, в том числе
  новый `debates` (15 callback-handler’ов + 4 message-handler’а).

### Запуск debate-ii

```bash
cd services/debate-ii
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DEBATE_TOKEN=devsecret
python run.py     # FastAPI на :8080 + опц. собственный TG-бот
```

Потом в Lilush → «🎤 Дебаты» → «➕ Добавить сервер» → URL → токен.
