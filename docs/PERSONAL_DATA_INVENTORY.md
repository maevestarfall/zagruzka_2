# Инвентаризация персональных данных бота (ЧАСТЬ 18.1)

Read-only аудит всех мест хранения персональных данных и секретов в
репозитории `master-room-bot`. Документ — источник истины для модели
категорий синхронизации (`bot/addons/masterroom/sync/categories.py`)
и последующих частей 18.2–18.7.

Обозначения:

- **Категория** — одна из 8 категорий синхронизации (см. §3).
- **ИСКЛ** — запись реестра исключений: не синхронизируется ни в одну сторону.
- **ENV-секрет** — живёт только в env-переменных; НЕ данные бота, в
  синхронизацию не входит вовсе (восстанавливается env при деплое).
- **STATE-секрет** — plaintext-секрет в `state.json` (вход для ЧАСТИ 18.4).
- **FILE-секрет** — секрет-файл на диске (вход для ЧАСТИ 18.4/18.5;
  НИКОГДА не уходит в S3 открытым текстом как «обычный файл»).

---

## 1. `state.json` (общий стор, `bot/storage.py`)

Файл: `DATA_DIR/state.json` (chmod 600). Атомарная запись `_save()`:
tmp → fsync → `os.replace`; предыдущая версия сохраняется как
`state.json.bak`.

### 1.1 Сопутствующие файлы стораджа

| Файл | Что содержит | Классификация |
|---|---|---|
| `DATA_DIR/state.json.bak` | ПОЛНАЯ предыдущая версия state.json — те же секреты, что и в основном файле | **ИСКЛ** (реестр исключений; локальный артефакт атомарной записи) |
| `DATA_DIR/state.json.tmp` | Незавершённая запись (кратковременный) | **ИСКЛ** |

### 1.2 Разделы `state.json`

| Ключ | Что хранится | Секреты | Категория |
|---|---|---|---|
| `<user_id>` (числовые ключи) | per-user: `cwd`, `history` (переписка с LLM) | перс. данные (тексты пользователя) | `history` |
| `_settings.keys.*` | API-ключи LLM-провайдеров (openrouter / anthropic / openai), заданные через Telegram | **STATE-секрет** (plaintext) | `secrets` |
| `_settings.external_tools.*` | API-ключи внешних сервисов (apify, firecrawl, tavily, brave, exa, github_pat) | **STATE-секрет** (plaintext) | `secrets` |
| `_settings.media.slots.<id>.api_key` | API-ключи media-провайдеров (deAPI / fal / …) | **STATE-секрет** (plaintext) | `secrets` |
| `_settings.media.slots.<id>` (кроме api_key) | url, name, модели, custom-шаблон запроса | custom может содержать заголовки авторизации | `module_state` (custom c headers → `secrets`, см. резолвер) |
| `_settings.media.templates.*` | Библиотека custom-шаблонов запросов | могут содержать ключи в headers | `secrets` (консервативно: шаблоны содержат авторизацию) |
| `_settings.model`, `enabled`, `role_gate_enabled`, `tts_*` и др. тумблеры | Настройки бота | нет | `module_state` |
| `_settings.addons.*` | Все addon-бакеты (см. §2) | по-бакетно | по-бакетно |
| co-owners / доступ (`bot/access.py`, owner id, allowed ids в state) | Идентификаторы Telegram | перс. данные | `accounts` |

## 2. Addon-бакеты (`bot/addons/state.py`, ключи `_settings.addons.*`)

Все addon-данные лежат в том же `state.json` под `_settings.addons.<addon>`.

### 2.1 Plaintext-СЕКРЕТЫ в state (вход для ЧАСТИ 18.4)

| Ключ state | Источник (код) | Что это |
|---|---|---|
| `addons.slack.token` | `bot/addons/slack/credentials.py::save_token` | Slack OAuth token (`xoxb-…`/`xoxp-…`). **НЕ env-пример:** читается СНАЧАЛА из state (in-Telegram wizard), env `SLACK_BOT_TOKEN`/`SLACK_TOKEN` — лишь fallback №2. Двойной источник ⇒ инвентеризуется как STATE-секрет |
| `addons.mailbox.password` | `bot/addons/mailbox/credentials.py:66` | IMAP-пароль почтового ящика |
| `addons.helpzavr.groq_api_key`, `addons.helpzavr.openrouter_api_key` | `bot/addons/helpzavr/handlers.py:188,192` | API-ключи |
| `addons.editor_agent.password`, `addons.editor_agent.api_key` | `bot/addons/editor_agent/omni.py:83,91` | Пароль/ключ omni-сервиса |
| `addons.masterroom.token` | `bot/addons/masterroom/state.py:103` | Секрет входящих webhook-ссылок Master Room |
| `addons.masterroom.servers[].token` | `state.py::add_server` | Токены coder-bot серверов |
| `addons.masterroom.github_accounts[].token` | `state.py::add_github_account` (поле `token`, если заполнено) | GitHub PAT plaintext (опционально; основной путь — `token_ref`) |
| `addons.masterroom.render_bot_token` | `state.py:357` | Значение BOT_TOKEN для Render |
| `addons.masterroom.render_github_token` | `state.py:366` | GitHub-токен входа на render.com |
| `addons.masterroom.render_gh_password`, `gh_web_password`, `render_password` | `state.py:438,456,474` | Пароли GitHub web / Render |
| `addons.debates.debate_owner_token`, `addons.debates.admin_connect_secret` | `bot/addons/debates/debate_server.py:71,1272` | Токены доступа к debate-серверу |
| `addons.test.token`, `addons.fable5.token`, `addons.projectmap.token`, `addons.guide.token`, `addons.vision.token`, `addons.obuchalka.<role>_token` | server/store модули соответствующих addon'ов | URL-токены публичных ссылок |

### 2.2 Env-ref паттерн (НЕ секреты в state — эталон)

| Ключ state | Что лежит в state |
|---|---|
| `addons.masterroom.github_accounts[].token_ref` | Только ИМЯ env-переменной (например `GITHUB_TOKEN`). Сам токен — **ENV-секрет**, в sync не входит |
| `addons.swapface.user_token_ref` | Имя env-переменной Discord user token (`bot/addons/swapface/state.py`) |

### 2.3 Прочие addon-бакеты

| Данные | Категория |
|---|---|
| Логины/учётки: `addons.masterroom.render_email`, `render_gh_login`, `gh_web_login`, `github_accounts[]` (owner/name без токена), mailbox-адрес | `accounts` |
| Отпечатки: профили `bot/addons/stealth_fp.py` / `stealth_fp_check.py` (детерминированные fingerprint-профили, seed'ы) | `fingerprints` |
| `addons.masterroom.projects`, `defaults`, `targets`, тумблеры | `module_state` (projects → `history`) |
| `addons.vault.*` — конфигурация kill-switch (`bot/selfdestruct.py`) | **ИСКЛ** (реестр исключений) |
| Все прочие addon-тумблеры/настройки | `module_state`; неизвестное → `other` |

## 3. Секреты-ФАЙЛЫ на диске (FILE-секреты)

Найдено grep'ом по `cookies` / `token` / `session` / `storage_state` в путях `DATA_DIR`:

| Файл/директория | Источник (код) | Что содержит | Категория |
|---|---|---|---|
| `DATA_DIR/yt_cookies.txt` | `bot/config.py:178`, `bot/wizard.py:7515`, `bot/workers/downloader.py:335` | Netscape-cookies YouTube в ОТКРЫТОМ виде | **`secrets`** (FILE-секрет) |
| `DATA_DIR/session/` | `bot/wizard.py:4386`, `bot/addons/browser_panel/handlers.py`, `bot/addons/masterroom/render_deploy.py:249`, `handlers.py:3034` | Браузерные сессии stealth-браузера (storage state, скриншоты сессий входа) | **`secrets`** (FILE-секрет; вся директория) |

Правило (закреплено в резолвере): файл, помеченный как секрет, относится к
`secrets` независимо от расположения; `media`/`other` НИКОГДА не могут его
захватить.

## 4. Файлы `DATA_DIR` (не секреты)

| Путь | Что содержит | Объём (типовой) | Категория |
|---|---|---|---|
| `DATA_DIR/storage/` | Файловый менеджер экрана «💾 Хранилище» (mr:storage*) — произвольные файлы владельца | МБ–ГБ | `media` |
| `DATA_DIR/downloads/` | Скачанные видео (yt-dlp) | ГБ | `media` |
| `DATA_DIR/releases/` | Срежиссированные клипы + метаданные publisher'а | ГБ | `media` |
| `DATA_DIR/jobs.db` | SQLite-очередь заданий (`bot/jobs.py`, `JOBS_DB_PATH`) | МБ | `history` |
| `PROJECTS_DIR` (`DATA_DIR/projects/`) | Рабочие копии проектов (git-клоны, генерируемый код coder'а) | МБ–ГБ | `history` |
| `DATA_DIR/.frozen` | Необратимый маркер kill-switch freeze (`bot/selfdestruct.py`) | байты | **ИСКЛ** |
| журналы/логи в `DATA_DIR` (`*.log`) | Технические журналы | МБ | `history` |
| локальный журнал sync (появится в 18.3+, `DATA_DIR/sync_journal.db*`) | Журнал операций синхронизации | — | **ИСКЛ** |

## 5. ENV-секреты (в sync НЕ входят)

`BOT_TOKEN`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`SLACK_BOT_TOKEN`/`SLACK_TOKEN` (fallback), `GITHUB_TOKEN` и все имена из
`token_ref`, ключи внешних инструментов (`APIFY_API_TOKEN`, …) как
env-fallback, а также новые переменные 18.1: `DATABASE_URL`,
`SYNC_S3_ACCESS_KEY_ID`, `SYNC_S3_SECRET_ACCESS_KEY`, `SYNC_MASTER_KEY`.
Их восстанавливает окружение при деплое; они никогда не пишутся в
`state.json`/журнал/логи. Секрет с двойным источником (state + env-fallback,
как Slack) инвентаризуется как STATE-секрет по фактическому месту хранения.

## 6. Kill-switch (`bot/selfdestruct.py`) — только описание

- Конфигурация — namespace `vault` в state (`iplogger_*`, `kill_mode`).
- Режим `freeze` пишет необратимый маркер `DATA_DIR/.frozen`; режим
  `delete` стирает `state.json`, поддиректории `/data` и код.
- Sync подчиняется kill-switch: `.frozen`, ключи `vault`, tmp/bak-файлы и
  локальный журнал sync внесены в реестр исключений и не синхронизируются
  ни в одну сторону. После намеренного wipe бот НЕ восстанавливает данные
  из внешней БД автоматически.

## 7. Сводный реестр исключений (единственное законное «выпадение» из sync)

1. `DATA_DIR/.frozen`
2. Ключи namespace `vault` (`_settings.addons.vault.*`)
3. `DATA_DIR/state.json.tmp`, `DATA_DIR/state.json.bak`
4. Локальный журнал sync и его файлы (`DATA_DIR/sync_journal.db*`)

Реестр закреплён кодом в `bot/addons/masterroom/sync/categories.py`
(`EXCLUSION_*` константы) — catch-all `other` обязан пропускать эти записи.
