# Changelog — OpenReels + «🧩 Прочие»

## 🎬 OpenReels в Публикации (тема → готовый ролик)

`📢 Публикация → 🎬 OpenReels` — кнопка, через которую кидаешь тему, а
OpenReels (self-hosted, github.com/tsensei/OpenReels) генерит вертикальный
MP4 (research → сценарий → озвучка → визуал → музыка → субтитры → сборка).

- **`bot/openreels.py`** — async-клиент Fastify REST API OpenReels:
  `POST /api/v1/jobs` (submit) → poll `GET /api/v1/jobs/{id}` → скачивание
  готового MP4. Эндпоинты/поля разнятся между версиями, поэтому клиент
  пробует несколько форм (`/api/v1/jobs`, `/api/jobs`, …), рекурсивно ищет
  ссылку на mp4 и даёт понятные ошибки. Есть `health()` (🔌 Проверить связь).
- **Режимы** (кнопка 🎛 Режим): `generate_then_publish` (сгенерить → выбрать
  слот и опубликовать через существующую stealth-публикацию), `generate_only`
  (просто прислать MP4 в Telegram), `openreels_publishes` (OpenReels сам
  публикует — бот шлёт тему и показывает статус).
- **Настройки** (`store.py`): URL сервера (🔧), режим, платформа
  (youtube/tiktok/instagram). URL можно задать и через env `OPENREELS_URL`,
  ключ — `OPENREELS_API_KEY`.
- Переиспользует `store.set_pending_video()` + поток `pub:go:` для публикации.

## 🧩 «Прочие» в главном меню

Новая кнопка `🧩 Прочие` (`/start`, админ-секция → `misc:home`). Внутри:

- **🌐 Браузер-автоматизация** — единая точка ко всем браузер-функциям бота
  (всё через `workers.browser_clicker.run_browser_macro`: Camoufox →
  CloakBrowser → nodriver → Playwright, человекоподобная мышь,
  прокси/фингерпринт): публикация через сайт, Weavy через браузер, VISION,
  постинг-макросы (память бота).
- **🤖 Инструкции через ИИ** (`misc:ai`) — пишешь задачу обычными словами,
  LLM (`addons._llm.chat_once`) переводит её в сценарий шагов
  (`goto/click/type/upload/copy/pause`), бот выполняет его в stealth-браузере
  и присылает скриншот + извлечённый текст. Парсер строгий: первый шаг
  обязан быть переходом на сайт, лимит 20 шагов, терпит markdown-обёртки.

## Тесты

- `test_openreels_misc_standalone.py` — 8 кейсов: submit+poll, fallback
  эндпоинтов, провал задачи, скачивание, health, round-trip настроек,
  парсер макроса.
- Регрессия `test_media_custom_standalone.py` — 18 кейсов, зелёные.
