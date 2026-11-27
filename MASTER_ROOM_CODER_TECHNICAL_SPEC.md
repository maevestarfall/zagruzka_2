# Техническое задание: Master Room как универсальная инструментальная среда для Кодера

## 1. Назначение

Требуется превратить существующий Master Room из панели управления и симулятора выполнения задач в полноценную инструментальную среду, в которую подключается функция «Кодер» и получает все необходимые средства для создания, изменения, проверки и публикации:

- сайтов;
- веб-приложений;
- Telegram-, Discord- и других ботов;
- API и серверных приложений;
- библиотек и CLI-программ;
- существующих GitHub-проектов;
- собственного кода Кодера и Master Room через контролируемое самообновление.

Master Room — это не Кодер, не модель и не место реализации интеллекта Кодера. Это постоянная «инструментальная комната»: инфраструктурный слой, в который любой текущий или будущий Кодер приходит, получает стандартизированные инструменты, рабочее место, память задачи, очередь дел, артефакты и каналы управления, выполняет работу и уходит. Логику качества генерации кода, рассуждений и выбора решения будут отдельно улучшать внутри Кодера; Master Room не должна дублировать эту логику или быть жёстко связана с конкретной моделью.

Master Room обязана предоставлять инструменты и инфраструктуру как стабильный контракт. Кодер должен принимать решения, строить план и пользоваться ими. Если задача технически выполнима в доступной среде, Кодер не должен прекращать работу сообщением «не хватает инструмента»: он выбирает существующий инструмент, устанавливает разрешённую зависимость, создаёт отложенное действие либо отправляет владельцу конкретный структурированный запрос на разрешение, интеграцию или секрет. После удовлетворения запроса задача автоматически возвращается в очередь и продолжается с сохранённого checkpoint.

Главный результат этого ТЗ — до конца реализованная Master Room и её Tool/API Runtime. Реализация или замена самой модели Кодера не входит в основной объём: для проверки используется детерминированный тестовый клиент и/или существующий Кодер через тот же публичный протокол, без специальных обходных путей.

## 2. Текущее состояние и проблема

В проекте уже присутствуют отдельные компоненты:

- Telegram-интерфейс Кодера;
- Master Room HTTP API и dashboard;
- FSM задач;
- клиент для внешнего coder-server;
- GitHub-операции;
- тестовый исполнитель;
- проверки путей и кода;
- аудит, бюджет и роли;
- события прогресса;
- механизм PR и merge.

Однако активный `run_fsm_loop()` в `bot/addons/coder/handlers.py` симулирует выполнение через `asyncio.sleep()`, фиктивные результаты тестов, PR, commit SHA и review score. Реального coder-server с полным циклом инструментальных вызовов в архиве нет. Существующие `bot/coder/tester.py`, `git_ops.py`, `integrator.py` и `security.py` не объединены в основной исполнитель.

Необходимо удалить симуляцию из рабочего маршрута и реализовать реальный оркестратор.

## 3. Целевая архитектура

Система должна состоять из следующих уровней.

### 3.1. Master Room

Отвечает за:

- создание и хранение задач;
- подключение репозиториев;
- выбор проекта и ветки;
- разрешения и подтверждения;
- секреты и интеграции;
- бюджет и лимиты;
- журнал событий;
- отображение плана, файлов, diff, команд, тестов и результата;
- отмену, паузу, продолжение и повтор задачи;
- подтверждение публикации, merge, deploy и самообновления.

### 3.2. Coder Orchestrator

Отвечает за:

- анализ задачи;
- исследование репозитория;
- построение плана;
- выбор инструментов;
- инструментальный цикл модели;
- контроль контекста;
- исправление ошибок после тестов;
- достижение критериев готовности;
- отправку событий в Master Room.

### 3.3. Workspace Runtime

Для каждой задачи создаётся отдельная изолированная рабочая область:

```text
workspaces/<task_id>/repo
workspaces/<task_id>/artifacts
workspaces/<task_id>/logs
workspaces/<task_id>/state
```

Работа не должна выполняться непосредственно в каталоге основного запущенного бота.

### 3.4. Tool Registry

Все инструменты регистрируются в одном реестре с JSON-схемами входных параметров, разрешениями, таймаутами и форматом результата. Модель получает только разрешённые для текущей задачи инструменты.

### 3.5. Event Bus

Каждое действие отправляет структурированное событие:

```json
{
  "task_id": "task_123",
  "sequence": 42,
  "timestamp": "ISO-8601",
  "phase": "testing",
  "tool": "test.run",
  "status": "completed",
  "summary": "1249 tests passed",
  "details": {},
  "artifact_ids": []
}
```

События должны сохраняться в БД и транслироваться в dashboard через SSE или WebSocket.

## 4. Состояния задачи

Реализовать конечный автомат:

```text
created
→ awaiting_repository
→ preparing_workspace
→ inspecting
→ planning
→ awaiting_plan_approval (опционально)
→ implementing
→ validating
→ awaiting_permission (при необходимости)
→ reviewing
→ awaiting_apply_approval
→ publishing
→ deployed/completed
```

Дополнительные состояния:

```text
paused
cancel_requested
cancelled
failed
budget_exhausted
needs_user_input
rollback_required
```

Состояние, план, история сообщений и результаты инструментов должны переживать перезапуск процесса.

## 5. Обязательные инструменты Кодера

### 5.1. Файловая система

Реализовать инструменты:

- `fs.list` — список файлов и каталогов;
- `fs.glob` — поиск по шаблону;
- `fs.search` — поиск текста и регулярных выражений;
- `fs.read` — чтение целого файла или диапазона строк;
- `fs.write` — создание нового файла;
- `fs.edit` — точечная замена с проверкой исходного текста;
- `fs.patch` — применение unified diff;
- `fs.move` — перемещение или переименование;
- `fs.copy` — копирование;
- `fs.delete` — удаление с отдельным разрешением;
- `fs.tree` — карта проекта;
- `fs.stat` — размер, тип, время изменения и хеш;
- `fs.encoding_check` — поиск повреждённой кодировки и replacement character;
- `fs.archive.extract/create` — безопасная работа с ZIP/TAR без path traversal.

Требования:

- каждый путь нормализуется и проверяется на нахождение внутри workspace;
- запрещены symlink escape и `../`;
- перед изменением сохраняется исходный хеш;
- запись отклоняется при конфликте версии;
- для бинарных файлов используются отдельные методы;
- секретные файлы автоматически маскируются.

### 5.2. Выполнение команд

Реализовать `command.run` без неконтролируемого `shell=True`.

Вход:

```json
{
  "executable": "pnpm",
  "args": ["test"],
  "cwd": ".",
  "timeout_seconds": 300,
  "env_allowlist": ["NODE_ENV"],
  "network": "restricted"
}
```

Возможности:

- потоковый stdout/stderr;
- код завершения;
- таймаут;
- отмена процесса и дочерних процессов;
- лимиты CPU, RAM, процессов и диска;
- журнал команды;
- автоматическая маскировка секретов;
- запрет интерактивных зависаний;
- разрешённый список исполняемых файлов;
- отдельное подтверждение для опасных операций.

Запрещено передавать строку модели напрямую в shell.

### 5.3. Управление зависимостями

Реализовать `dependencies.detect`, `dependencies.install`, `dependencies.audit`.

Поддержать:

- npm, pnpm, yarn, bun;
- pip, uv, poetry;
- cargo;
- go modules;
- composer;
- системные пакеты только через подтверждение владельца.

Алгоритм:

1. определить lockfile;
2. выбрать соответствующий package manager;
3. устанавливать пакет до добавления импорта;
4. обновлять lockfile;
5. запускать security audit;
6. показывать изменения зависимостей отдельно;
7. не обновлять весь dependency tree без необходимости.

### 5.4. Git и GitHub

Объединить существующие `git_ops.py` и `integrator.py` с реальным исполнителем.

Инструменты:

- `git.status`;
- `git.diff`;
- `git.log`;
- `git.branch.create`;
- `git.checkout`;
- `git.commit`;
- `git.fetch`;
- `git.rebase`;
- `git.push`;
- `github.repo.create`;
- `github.pr.create/update`;
- `github.pr.checks`;
- `github.pr.merge`;
- `github.release.create`;
- `github.artifact.upload`.

Правила:

- каждая задача работает в ветке `coder/<task-id>-<slug>`;
- запрещён прямой push в protected/default branch;
- перед commit показывается diff;
- push, PR, merge, release и изменение remote требуют политики разрешений;
- merge выполняется только после совпадения ожидаемого SHA;
- self-update всегда через отдельную ветку и PR;
- должна существовать команда rollback/revert.

### 5.5. Анализ проекта

Реализовать инструменты:

- определение языка, фреймворка и package manager;
- карта маршрутов и entry points;
- поиск конфигурации, env schema и интеграций;
- определение тестового и build-контура;
- dependency graph импортов;
- поиск существующих аналогичных компонентов;
- поиск dead code и циклических зависимостей;
- оценка файлов, связанных с задачей;
- чтение README и локальных правил проекта;
- проверка git status до начала работы.

Кодер обязан сначала исследовать систему по схеме «широко → конкретно → проверить связи», а не изменять первый найденный файл.

### 5.6. Тесты и контроль качества

Заменить фиктивные `3 Passed, 0 Failed` реальными результатами.

Инструменты:

- `test.detect`;
- `test.run`;
- `lint.run`;
- `typecheck.run`;
- `build.run`;
- `format.check/apply`;
- `security.scan`;
- `encoding.scan`;
- `dependency.audit`;
- `coverage.run`.

Порядок проверки:

1. тесты затронутого модуля;
2. линтер и typecheck;
3. сборка;
4. полный набор тестов при разумной стоимости;
5. security scan;
6. анализ diff;
7. проверка отсутствия секретов и повреждённой кодировки.

Результаты должны включать фактическую команду, stdout/stderr, exit code, длительность и список ошибок. При ошибке результат возвращается Кодеру для исправления с ограниченным числом попыток.

### 5.7. Browser/UI-инструменты

Для сайтов и веб-приложений нужны:

- запуск dev server;
- автоматическое определение порта;
- открытие страницы браузером;
- DOM/accessibility snapshot;
- screenshot;
- click, type, select, upload, drag-and-drop;
- проверка маршрутов и форм;
- console errors;
- network errors;
- responsive viewport;
- Web Vitals;
- visual artifacts до/после.

Кодер не может объявить UI-задачу завершённой только по успешной компиляции. Он должен проверить основной пользовательский сценарий в браузере.

### 5.8. HTTP/API-инструменты

Реализовать:

- `http.request` с allowlist доменов;
- запуск и проверку локального API;
- OpenAPI import/validation;
- webhook test receiver;
- JSON schema validation;
- healthcheck;
- контрактные тесты;
- маскирование Authorization/Cookie.

Внешний интернет по умолчанию ограничен. Доступ к новому домену должен соответствовать политике задачи.

### 5.9. Базы данных

Инструменты:

- обнаружение используемой БД;
- чтение схемы;
- создание миграции;
- dry-run миграции;
- применение к локальной/preview БД;
- проверка rollback;
- backup перед изменением production;
- выполнение параметризованных запросов;
- diff схемы.

Production-миграция всегда требует отдельного подтверждения. Модель не получает необработанные production credentials.

### 5.10. Секреты и интеграции

Master Room должен иметь Secret Broker:

- секреты хранятся зашифрованно;
- модель видит имена и наличие, но не значения;
- значение подставляется только разрешённому инструменту;
- секреты маскируются в логах и результатах;
- поддерживается минимальный scope;
- есть журнал использования;
- есть ротация и отзыв.

Если секрета или интеграции нет, Кодер обязан сформировать структурированный запрос:

```json
{
  "type": "environment_variable",
  "keys": ["STRIPE_SECRET_KEY"],
  "reason": "Required to create a test checkout session",
  "scope": "preview",
  "blocking": true
}
```

Он не должен просто писать «не могу продолжить».

### 5.11. Артефакты

Master Room должен хранить и показывать:

- план;
- изменённые файлы;
- diff;
- логи команд;
- результаты тестов;
- screenshots;
- build output;
- ZIP проекта;
- commit SHA;
- URL PR;
- deployment URL;
- отчёт о безопасности;
- rollback instructions.

### 5.12. Evidence Ledger и воспроизводимость — критическое улучшение №3

Master Room должна разделять сообщения Кодера и фактические доказательства. Любое проверяемое утверждение связывается с immutable evidence record:

```json
{
  "evidence_id": "ev_123",
  "claim_type": "tests_passed",
  "subject": "workspace@sha256:...",
  "producer": "test.run@1.3.0",
  "tool_call_id": "tc_123",
  "inputs_hash": "sha256:...",
  "artifact_hashes": ["sha256:..."],
  "exit_code": 0,
  "started_at": "ISO-8601",
  "finished_at": "ISO-8601",
  "expires_at": null
}
```

Обязательные свойства:

- content-addressed artifact storage и SHA-256 каждого файла;
- append-only ledger: исправление создаёт новую запись, а не переписывает старую;
- provenance chain от task/request и commit SHA до tool call, diff, test/build и deploy;
- проверка freshness: старый тест не подтверждает код, изменённый после теста;
- status `stale` при изменении входного hash, зависимостей, image digest или env contract;
- attestations для build/release и связь deployment URL с точным artifact digest;
- экспорт воспроизводимого run bundle без секретов;
- garbage collection только по retention policy, сохраняя ledger и hashes;
- UI-кнопка «показать доказательство» для теста, файла, PR, deploy и screenshot.

Перед финальным статусом `completed` Evidence Gate вычисляет обязательный набор доказательств из acceptance criteria и типа задачи. Например, UI-задача требует build + browser scenario + screenshot + console/network check; API — tests + contract/healthcheck; DB migration — dry-run + schema diff + rollback evidence. Отсутствующее или stale evidence блокирует завершение и возвращает machine-readable список недостающих проверок.

### 5.13. Event sourcing и детерминированное восстановление

Источником истины является упорядоченный append-only event log, а dashboard/task state — его проекция. Side effects выполняются через activity/tool call с idempotency key; результат side effect записывается до перехода к следующему состоянию. После crash система повторно строит проекцию и не повторяет уже подтверждённый внешний эффект.

Требования:

- optimistic concurrency по expected sequence/version;
- transactional outbox для БД → event bus/callback;
- inbox/deduplication для webhook и callback;
- snapshot проекции через настраиваемое число событий без удаления исходной истории;
- versioned event schemas и миграции/upcasters;
- deterministic replay test для каждой версии workflow;
- compensation actions для частично выполненных publish/deploy операций;
- чёткое разделение retry безопасного чтения, idempotent write и non-repeatable side effect.

Это переносит в Master Room ключевую практику durable workflow-систем: reasoning Кодера может меняться, но очередь, паузы, approvals и side effects восстанавливаются по фактам и idempotency, а не из памяти процесса.

## 6. Инструментальный цикл Кодера

Реализовать цикл до достижения результата:

1. Принять задачу и критерии готовности.
2. Проверить репозиторий и доступные интеграции.
3. Создать workspace и feature branch.
4. Исследовать проект.
5. Составить план с файлами и рисками.
6. При необходимости запросить подтверждение плана.
7. Выполнить изменения инструментами.
8. Установить необходимые зависимости.
9. Запустить локальные проверки.
10. Исправить обнаруженные ошибки.
11. Запустить сборку и полный набор релевантных тестов.
12. Для UI выполнить browser verification.
13. Провести self-review diff.
14. Показать результат владельцу.
15. После разрешения выполнить commit/push/PR/deploy.
16. Сохранить воспроизводимый итоговый отчёт.

Модель не должна придумывать результат инструмента. Любое утверждение «тесты прошли», «файл создан», «PR открыт», «сайт работает» допустимо только при наличии успешного tool result и соответствующего artifact/event.

## 7. Планирование и контекст

Кодер должен поддерживать:

- список milestone-задач;
- текущий шаг;
- критерии готовности;
- список прочитанных и изменённых файлов;
- краткое резюме длинных tool results;
- повторное чтение актуальной версии файла перед изменением;
- защиту от изменения устаревшей версии;
- автоматическое сжатие истории без потери решений;
- возобновление после перезапуска.

Для больших репозиториев необходим индекс:

- пути и языки файлов;
- символы и импорты;
- полнотекстовый поиск;
- опциональные embeddings;
- инкрементальное обновление после изменения файлов.

### 7.1. Repository Intelligence Service — критическое улучшение №1

Чтобы Кодер не тратил контекст на чтение репозитория вслепую, Master Room предоставляет отдельный сервис кодовой разведки. Он не принимает решения за Кодера, а возвращает компактные факты.

Обязательные инструменты:

- `repo.manifest` — структура, языки, размеры, generated/vendor/binary markers;
- `repo.map` — ранжированная карта модулей, символов и связей;
- `repo.symbols` — классы, функции, интерфейсы, routes и exports;
- `repo.references` — определения и использования символа;
- `repo.import_graph` — cross-file dependency graph и циклы;
- `repo.select_context` — релевантные файлы и диапазоны строк в заданном byte/token budget;
- `repo.rules` — применимые инструкции из README, AGENTS.md, CONTRIBUTING и вложенных правил;
- `repo.impact` — потенциально затронутые callers, tests, routes, schemas и configs;
- `repo.refresh` — инкрементальное обновление индекса после file event;
- `repo.snapshot` — content-addressed снимок индекса для воспроизводимости.

Реализация использует Tree-sitter для быстрой мультиязычной структуры и LSP там, где нужны семантические cross-file references, diagnostics и rename. Ранжирование должно учитывать query/task relevance, import/reference centrality, близость к изменённым файлам, тесты и локальные правила. Embeddings являются дополнительным сигналом, но не заменяют точный symbol/text search.

Каждый context bundle содержит manifest, выбранные файлы/диапазоны, причины выбора, общий byte/token budget, commit SHA и хеши содержимого. Перед передачей наружу выполняются secret scan, denylist чувствительных путей и redaction. После edit индекс обновляется только для затронутого subgraph. Нельзя тихо обрезать bundle: truncation и пропущенные крупные/binary файлы указываются явно.

Для изменений добавить транзакционный режим `patch.prepare → verify → patch.commit/abort`: патч сначала проверяет ожидаемые хеши, уникальность цели, синтаксис и path policy; затем применяется атомарно; при провале verify откатывается полностью. Для symbol-level изменений допускается структурированный AST patch, но unified diff и точечный edit остаются обязательным fallback.

Идея основана на практиках Context Bridge (bounded context, Tree-sitter/PageRank, secret scanning, transactional SemPatch) и Rhizome (Tree-sitter + LSP через компактный MCP-интерфейс), но реализация должна быть встроена в контракт Master Room, а не привязана к конкретному агенту.

## 8. Самоулучшение Кодера

Самоулучшение разрешено, но рассматривается как особо опасный режим.

### 8.1. Допустимый сценарий

1. Кодер получает задачу изменить собственный репозиторий.
2. Создаётся новая изолированная копия текущей стабильной версии.
3. Изменения выполняются в отдельной ветке.
4. Запускаются unit, integration, security и regression tests.
5. Запускается canary-экземпляр новой версии.
6. Canary выполняет smoke-задачу без production-секретов.
7. Master Room показывает diff и отчёт владельцу.
8. После подтверждения создаётся PR или release.
9. Выполняется контролируемый deploy.
10. Старый процесс остаётся доступным до успешного healthcheck.
11. При ошибке происходит автоматический rollback.

### 8.2. Запреты

Кодер не может:

- изменять работающий экземпляр in-place;
- удалять rollback-версию;
- отключать собственные проверки безопасности;
- менять правила подтверждений без отдельного ручного разрешения;
- извлекать или печатать секреты;
- самостоятельно подтверждать свой merge/deploy;
- подменять результаты тестов;
- изменять audit log задним числом.

### 8.3. Полная перезаливка кода

Разрешить два режима:

- `patch` — обычный PR с изменёнными файлами;
- `replacement` — полная замена содержимого репозитория.

Для `replacement` обязательно:

- создать backup branch/tag;
- показать статистику удалённых/добавленных файлов;
- выполнить secret scan и license scan;
- запретить удаление `.git` и audit metadata;
- потребовать явное подтверждение фразы уровня `CONFIRM_FULL_REPLACEMENT`;
- поддержать мгновенный revert на backup SHA.

## 9. Разрешения и уровни риска

### Уровень 0 — автоматический

- чтение файлов;
- поиск;
- анализ;
- git status/diff/log;
- локальные read-only проверки.

### Уровень 1 — разрешено в workspace

- создание и редактирование файлов;
- запуск тестов;
- установка project dependencies;
- запуск локального dev server.

### Уровень 2 — подтверждение по политике

- удаление файлов;
- внешняя сеть;
- длительные или дорогие команды;
- изменение схемы локальной/preview БД;
- создание внешних ресурсов.

### Уровень 3 — явное подтверждение владельца

- push;
- создание/обновление PR;
- deploy;
- миграция production БД;
- merge;
- release;
- домены и DNS;
- платежи;
- отправка сообщений реальным пользователям;
- self-update;
- полная замена репозитория.

Подтверждение содержит точное действие, diff/команду, целевой ресурс, риски и возможность rollback.

## 10. Безопасность песочницы — критическое улучшение №2

Обязательно:

- отдельная изоляция на задачу: Firecracker microVM как предпочтительная граница для недоверенного кода; gVisor/Kata или hardened rootless container как явно отмеченный fallback;
- непривилегированный пользователь и отсутствие privilege escalation;
- read-only подписанный базовый образ;
- writable только workspace, isolated tmp и artifacts;
- запрет Docker/containerd socket и nested privileged containers;
- полное отсутствие mount host filesystem, SSH agent, cloud metadata и пользовательских dotfiles;
- default-deny egress: DNS и HTTP(S) только через policy proxy с allowlist доменов, method/size limits и audit;
- запрет ingress извне; preview port доступен только через аутентифицированный reverse proxy;
- seccomp/AppArmor/SELinux или эквивалент;
- cgroups: CPU, RAM, PIDs, I/O и disk quota;
- wall-clock timeout, idle timeout, output-size limit и kill всей process tree;
- защита от symlink/hardlink/mount escape, `/proc` и device abuse;
- secret redaction до записи события и повторный redaction при выдаче;
- malware/secret/license scan загруженных архивов;
- защита от ZIP bomb, path traversal и decompression ratio abuse;
- очистка workspace и ephemeral credentials по TTL;
- immutable append-only audit log вне sandbox.

### 10.1. Sandbox Broker

Кодер не создаёт контейнеры напрямую. Master Room вызывает `sandbox.create/exec/expose/snapshot/destroy` через отдельный Sandbox Broker. Broker выдаёт task-scoped identity, фиксирует image digest, runtime policy и resource budget. Любое расширение сети, диска, времени или привилегий — новый permission request, а не параметр произвольного tool call.

Секреты никогда не помещаются в образ, workspace, общую environment или command line. Secret Broker выдаёт краткоживущий capability/token конкретному инструменту и конкретному destination. Где возможно, внешний API вызывается credential proxy, чтобы sandbox вообще не видел исходный секрет. После задачи credentials отзываются.

Sandbox обязан поддерживать reproducibility manifest: image digest, OS/architecture, runtime versions, установленные зависимости, разрешённые домены и resource limits. Snapshot может сохранять только workspace и package caches после scan; process memory, `/tmp`, credentials и sockets не переносятся.

### 10.2. Защита supply chain

- pin base images по digest;
- генерировать SBOM для build/release;
- проверять lockfile integrity и package provenance;
- отделять install scripts от обычной установки и запрашивать permission для неожиданных lifecycle scripts;
- кеши пакетов делать read-through и content-addressed, без записи секретов;
- подписывать итоговые release artifacts и хранить provenance «исходный SHA → команды → build artifact»;
- не исполнять git hooks из клонированного репозитория до явного разрешения;
- блокировать подмену `.git/config`, credential helpers и executable hooks.

Практика следует предупреждениям OpenHands о полном доступе без sandbox и промышленному разделению local/Docker/VM backends, а также модели E2B с отдельными средами для agent code execution. Существующий вызов `subprocess.run(..., shell=True)` в тестере необходимо заменить безопасным запуском массива аргументов.

## 11. API coder-server

Реализовать версионированный API.

### Создание задачи

```http
POST /api/v1/tasks
```

```json
{
  "title": "Создать сайт",
  "description": "...",
  "repository": {
    "provider": "github",
    "owner": "...",
    "name": "...",
    "ref": "main"
  },
  "acceptance_criteria": [],
  "permissions_profile": "standard",
  "budget": {},
  "callbacks": {
    "event_url": "...",
    "done_url": "..."
  }
}
```

### Остальные маршруты

```text
GET    /api/v1/tasks/<id>
GET    /api/v1/tasks/<id>/events
GET    /api/v1/tasks/<id>/artifacts
GET    /api/v1/tasks/<id>/diff
POST   /api/v1/tasks/<id>/pause
POST   /api/v1/tasks/<id>/resume
POST   /api/v1/tasks/<id>/cancel
POST   /api/v1/tasks/<id>/message
POST   /api/v1/tasks/<id>/approve
POST   /api/v1/tasks/<id>/reject
POST   /api/v1/tasks/<id>/retry
GET    /api/v1/health
GET    /api/v1/capabilities
```

Требования API:

- authentication;
- request signature;
- replay protection;
- idempotency key;
- sequence numbers событий;
- retry с backoff;
- строгая schema validation;
- отсутствие секретов в ответах;
- rate limits.

## 12. Изменения в существующем коде

Другой v0 должен сначала повторно исследовать файлы и зависимости, но ожидаемая зона изменений включает:

```text
bot/addons/coder/handlers.py
bot/addons/masterroom/server.py
bot/addons/masterroom/client.py
bot/addons/masterroom/state.py
bot/coder/workers.py
bot/coder/tester.py
bot/coder/git_ops.py
bot/coder/integrator.py
bot/coder/security.py
bot/coder/comm.py
```

Следует добавить отдельные модули, а не собирать всё в одном файле:

```text
bot/coder_server/app.py
bot/coder_server/config.py
bot/coder_server/models.py
bot/coder_server/orchestrator.py
bot/coder_server/runtime.py
bot/coder_server/events.py
bot/coder_server/permissions.py
bot/coder_server/artifacts.py
bot/coder_server/tools/registry.py
bot/coder_server/tools/filesystem.py
bot/coder_server/tools/command.py
bot/coder_server/tools/dependencies.py
bot/coder_server/tools/git.py
bot/coder_server/tools/github.py
bot/coder_server/tools/testing.py
bot/coder_server/tools/browser.py
bot/coder_server/tools/http.py
bot/coder_server/tools/database.py
bot/coder_server/tools/deploy.py
bot/coder_server/tools/secrets.py
```

Точные имена допускается адаптировать под архитектуру проекта. Нельзя сохранять одновременно активный симулятор и реальный маршрут под одной пользовательской кнопкой.

## 13. UI Master Room

Dashboard должен показывать:

- проекты и репозитории;
- список задач;
- текущую фазу;
- план и прогресс;
- поток событий в реальном времени;
- использованный инструмент и его статус;
- дерево изменённых файлов;
- unified/side-by-side diff;
- terminal output;
- результаты тестов;
- screenshots;
- бюджет токенов/команд/времени;
- запросы разрешений;
- кнопки pause, resume, cancel, retry;
- approve plan;
- approve apply;
- create PR;
- deploy;
- rollback.

Опасные действия должны визуально отличаться и требовать подтверждения. Токены и секреты никогда не отображаются полностью.

## 14. Хранение данных

Нужны сущности:

- `projects`;
- `repositories`;
- `tasks`;
- `task_steps`;
- `task_messages`;
- `tool_calls`;
- `events`;
- `artifacts`;
- `approvals`;
- `workspaces`;
- `deployments`;
- `secrets_metadata`;
- `audit_log`.

Для каждого tool call сохранять:

- tool name и version;
- sanitized input;
- sanitized output;
- started/finished timestamps;
- exit status;
- actor/model;
- workspace и task;
- permission decision;
- artifact references.

Не хранить секретные значения в БД событий.

## 15. Отказоустойчивость

Реализовать:

- checkpoint после каждого шага;
- восстановление после restart;
- heartbeat worker;
- обнаружение зависших задач;
- идемпотентное повторение callbacks;
- очередь задач;
- ограничение параллелизма;
- отмену с завершением process tree;
- повтор временных сетевых ошибок;
- блокировку двойного worker на одну задачу;
- TTL workspace;
- резервное хранение итоговых artifacts.

## 16. Наблюдаемость

Добавить:

- structured logs;
- task/trace/correlation ID;
- метрики длительности фаз;
- количество tool calls;
- ошибки инструментов;
- CPU/RAM/disk;
- token и cost accounting;
- процент успешных тестов;
- количество retries;
- алерты зависших и опасных операций.

## 16.1. Очередь дел, отложенная работа и петли

Master Room должна давать Кодеру не только разовый запуск инструмента, но и надёжный механизм продолжительной работы.

Обязательные сущности:

- `jobs` — исполняемые задания;
- `job_attempts` — отдельные попытки;
- `schedules` — расписания;
- `wait_conditions` — условия ожидания;
- `continuations` — точка и входные данные продолжения;
- `task_dependencies` — граф зависимостей между шагами;
- `leases` — временная блокировка задания конкретным worker;
- `dead_letter_jobs` — задания, исчерпавшие повторы.

Поддержать:

- немедленную постановку задания;
- выполнение в указанное время `run_at`;
- задержку `delay_seconds`;
- периодическое расписание cron с обязательным часовым поясом;
- ожидание webhook, approval, секрета, завершения CI, deploy или внешнего ресурса;
- fan-out на несколько подзадач и fan-in после их завершения;
- последовательные и параллельные шаги с лимитом параллелизма;
- цикл «изменить → проверить → разобрать ошибку → исправить»;
- цикл browser verification до прохождения заданных assertions;
- retry с exponential backoff, jitter и классификацией retriable/non-retriable ошибок;
- максимальное число итераций, wall-clock deadline, token/cost/tool-call budget;
- pause/resume/cancel как для всей задачи, так и для отдельного job;
- приоритеты и fairness между проектами;
- дедупликацию и idempotency key;
- продолжение после рестарта ровно с последнего подтверждённого checkpoint;
- ручное повторение шага с теми же или изменёнными входными параметрами;
- отправку в dead-letter queue с понятной причиной и кнопкой восстановления.

Кодер должен иметь инструменты:

- `jobs.enqueue`;
- `jobs.list/get`;
- `jobs.pause/resume/cancel/retry`;
- `jobs.wait_until`;
- `jobs.wait_for_event`;
- `jobs.schedule/create/update/delete`;
- `jobs.spawn_children`;
- `jobs.join_children`;
- `checkpoint.save/load`.

Ожидание не должно удерживать процесс, контейнер или модель активными. Состояние сохраняется, ресурсы освобождаются, а worker пробуждается по таймеру или событию. Ни один бесконечный цикл невозможен: у каждой петли обязательны критерий успеха, предел попыток, deadline и причина остановки.

## 16.2. Живая лента работы в стиле v0

Владелец должен видеть работу в реальном времени, но система не должна раскрывать скрытые chain-of-thought или внутренние токены модели. Вместо них Master Room показывает безопасный, полезный и проверяемый журнал действий:

- краткое объяснение текущего намерения: «Исследую маршрутизацию перед изменением формы»;
- актуальный план и отмеченные выполненные пункты;
- текущую фазу, шаг и подзадачу;
- имя запускаемого инструмента;
- безопасно очищенные входные параметры;
- поток stdout/stderr;
- прочитанные и изменённые файлы;
- полученный diff;
- результаты тестов и браузерной проверки;
- краткое резюме результата инструмента;
- обнаруженную проблему и выбранное следующее действие;
- запросы пользователя и разрешения;
- финальную сводку с доказательствами выполнения.

Каждая запись имеет `event_id`, `task_id`, `job_id`, `attempt`, `sequence`, `started_at`, `updated_at`, `finished_at`, `duration_ms`, `status`, `summary`, `artifact_ids`. Лента передаётся через SSE с восстановлением по `Last-Event-ID`; WebSocket допускается дополнительно, но не как единственный транспорт. После перезагрузки страницы история полностью восстанавливается из БД.

Статусы шага:

```text
queued → starting → running → waiting → succeeded/failed/cancelled/timed_out
```

UI обязан различать: модель формирует следующий шаг; инструмент реально выполняется; система ожидает пользователя; задача отложена; идёт повтор; работа закончена. Запрещено показывать анимацию «размышления», если на сервере нет активного шага или heartbeat.

## 16.3. Тайминги, сроки и heartbeat

Для задачи, фазы, job, попытки и tool call хранить:

- время постановки в очередь;
- время старта и завершения;
- длительность ожидания в очереди;
- активное время выполнения;
- время ожидания пользователя/внешнего события отдельно;
- deadline;
- timeout;
- `last_heartbeat_at`;
- число попыток;
- ожидаемое время следующего retry/пробуждения.

Dashboard показывает прошедшее время и разбивку по фазам. ETA допускается только как явно помеченная оценка диапазоном на основе фактической истории похожих шагов; нельзя показывать выдуманную точную длительность. Если данных недостаточно, показывать «оценка пока недоступна». ETA регулярно пересчитывается и не влияет на deadline.

Правила по умолчанию должны конфигурироваться по типу инструмента, например:

- простая файловая операция: 30 секунд;
- git status/diff: 60 секунд;
- установка зависимостей: 10 минут;
- тесты и build: 15 минут;
- browser scenario: 5 минут;
- deploy: 20 минут.

Это значения конфигурации, а не жёстко зашитые пределы. Владелец может менять их для задачи до запуска или через approval во время работы. Worker отправляет heartbeat во время долгого действия; пропущенный heartbeat переводит шаг в `stalled`, после grace period выполняется безопасная остановка и retry согласно политике.

## 16.4. Контракт независимости Кодера

Master Room должна обслуживать разные версии Кодера через один протокол. Кодер сначала вызывает `GET /api/v1/capabilities` и получает:

- доступные инструменты и их версии;
- JSON Schema входов/выходов;
- необходимые permission scopes;
- лимиты и стандартные timeout;
- доступные runtime images, языки и package managers;
- подключённые интеграции без значений секретов;
- поддерживаемые типы ожиданий, jobs и artifacts.

Вызовы инструментов выполняются только через Tool Gateway. Master Room не должна разбирать естественный язык, самостоятельно писать код или принимать архитектурные решения вместо Кодера. Она валидирует вызов, проверяет разрешение, исполняет его, сохраняет фактический результат и возвращает структурированный ответ.

Минимальный ответ инструмента:

```json
{
  "tool_call_id": "tc_123",
  "status": "succeeded",
  "started_at": "ISO-8601",
  "finished_at": "ISO-8601",
  "duration_ms": 1834,
  "exit_code": 0,
  "summary": "2 files matched",
  "data": {},
  "artifacts": [],
  "retryable": false
}
```

Неизвестный или временно отсутствующий инструмент возвращает machine-readable ошибку с `code`, `missing_capability`, альтернативами и способом запроса подключения. Это позволяет будущему Кодеру не угадывать возможности комнаты.

## 17. Тестирование реализации

### Unit tests

- нормализация и защита путей;
- tool registry;
- permission engine;
- command allowlist;
- redaction секретов;
- event sequencing;
- state transitions;
- budget limits;
- archive traversal и ZIP bomb protection.

### Integration tests

- создать workspace из fixture-репозитория;
- изменить файл;
- установить зависимость;
- запустить тесты;
- получить реальный diff;
- commit в локальную ветку;
- pause/resume после restart;
- cancel и kill дочернего процесса;
- approval workflow;
- callback retry;
- rollback.

### End-to-end сценарии

1. Создать простой статический сайт с нуля.
2. Создать Next.js-приложение, запустить build и браузерную проверку.
3. Исправить ошибку в существующем Python-проекте по падающему тесту.
4. Добавить API endpoint и тест.
5. Изменить несколько связанных файлов без порчи существующей логики.
6. Открыть реальный PR в тестовом репозитории.
7. Выполнить preview deploy.
8. Запросить отсутствующий секрет и продолжить после его добавления.
9. Перезапустить worker посередине задачи и продолжить с checkpoint.
10. Выполнить self-update в canary и откатить его при неуспешном healthcheck.
11. Полностью заменить содержимое тестового репозитория с backup tag.
12. Подтвердить, что модель не может подделать tool result.

## 18. Критерии приёмки

Работа считается завершённой только если:

- в активном пользовательском маршруте отсутствуют фиктивные `asyncio.sleep`, fake test results, фиксированный PR `#42`, фиктивный commit SHA и статический review score;
- реальный coder-server запускается и сообщает capabilities/health;
- задача создаёт отдельный workspace и ветку;
- Кодер реально читает и изменяет файлы;
- изменения подтверждаются фактическим diff;
- команды выполняются безопасно без неконтролируемого `shell=True`;
- тесты и сборка возвращают реальные результаты;
- UI-проект проверяется браузерным инструментом;
- система умеет запросить отсутствующий секрет/разрешение и продолжить задачу;
- состояние переживает перезапуск;
- cancel завершает процессы;
- PR/deploy выполняются только после подтверждения;
- self-update использует PR, canary, healthcheck и rollback;
- полный audit log доступен владельцу;
- очередь, delayed jobs, cron, wait-for-event, retry/backoff и dead-letter queue работают после перезапуска;
- цикл исправления имеет лимиты, deadline и сохраняемые checkpoints;
- живая SSE-лента восстанавливается после reconnect без потери или дублирования событий;
- пользователь видит план, намерения, реальные tool calls, stdout/stderr, diff, проверки и тайминги, но не скрытый chain-of-thought;
- для каждого шага видны фактические start/end/duration, heartbeat и ожидаемое время следующего действия;
- capability discovery позволяет подключить другую версию Кодера без изменения Master Room;
- реестр инструментов покрывает создание сайта, приложения, API, CLI/программы, работу с БД, браузерную проверку, GitHub, deploy и self-update;
- все новые unit/integration/E2E тесты проходят;
- старые тесты проекта не регрессировали.

## 19. Порядок реализации

### Этап 1. Основа Master Room

- версионированный Master Room API и Tool Gateway;
- capability discovery и JSON Schema всех инструментов;
- persistent task/job/event state;
- очередь, leases, retries, delayed jobs и checkpoints;
- workspace runtime;
- tool registry;
- filesystem/search/edit tools;
- безопасный command runner;
- SSE event streaming с replay;
- тайминги, heartbeat, timeout и cancel process tree;
- удаление симуляции из активного маршрута.

Этап 1 нельзя считать завершённым только по наличию классов или API-заглушек: обязательна демонстрация после рестарта процесса, что отложенная задача продолжилась, инструмент реально изменил fixture-репозиторий, события восстановились, а cancel остановил дочерний процесс.

### Этап 2. Полный цикл разработки

- repository inspection;
- planning;
- model tool loop;
- dependency manager;
- tests/lint/typecheck/build;
- diff и artifacts;
- error-repair loop.

### Этап 3. Публикация

- Git/GitHub;
- approvals;
- PR/checks/merge;
- deployment adapters;
- rollback.

### Этап 4. Универсальные возможности

- browser automation;
- HTTP/API tools;
- database tools;
- secret broker;
- дополнительные языки и package managers.

### Этап 5. Самоулучшение

- защищённый self-update mode;
- canary runtime;
- regression suite;
- release promotion;
- automatic rollback;
- full repository replacement с двойным подтверждением.

## 20. Ограничение для исполнителя этого ТЗ

Исполнитель не должен заменять реальную работу новой демонстрацией. Запрещены:

- hardcoded результаты инструментов;
- выдуманные ссылки, PR, SHA, тесты и deployment URL;
- сообщение об изменении файла без фактической записи;
- успешный статус при ненулевом exit code;
- скрытие ошибок тестов;
- автоматический merge/deploy/self-update без разрешения;
- доступ модели к необработанным секретам;
- выполнение команд модели через произвольный shell.

Если весь объём невозможно безопасно реализовать за один проход, сначала требуется полностью завершить этап 1 и предоставить реально работающий вертикальный сценарий:

```text
Master Room → реальная задача → workspace → чтение/изменение файлов → реальные тесты → diff → подтверждение → commit/PR
```

Незавершённые инструменты должны честно сообщаться как недоступные capability, а не имитироваться.

## 21. Обязательные дополнительные проверки новых критических контуров

Исполнитель добавляет тесты, которые доказывают не наличие API, а поведение:

1. **Repository Intelligence:** fixture monorepo индексируется; `repo.select_context` находит реализацию, caller и тест при жёстком budget; после edit обновляется только затронутый subgraph; secret fixture не попадает в bundle.
2. **Transactional patch:** один неверный expected hash в multi-file patch приводит к полному abort без частичного изменения.
3. **Sandbox escape:** тесты symlink/hardlink/path traversal, git hooks, Docker socket, metadata endpoint и запрещённого egress завершаются блокировкой и audit event.
4. **Credential isolation:** секрет отсутствует в env, argv, workspace, stdout/stderr и run bundle, но разрешённый proxied API call проходит.
5. **Evidence freshness:** тест прошёл, затем файл изменён — Evidence Gate помечает результат stale и блокирует `completed`.
6. **Event replay:** процесс аварийно завершается между внешним side effect и callback; после рестарта side effect не дублируется, projection и SSE replay совпадают.
7. **Supply chain:** lifecycle script неожиданной зависимости блокируется до approval; SBOM и provenance относятся к точному artifact digest.
8. **Долгая петля:** job ждёт таймер без занятого worker, просыпается, делает retry, сохраняет checkpoint и останавливается по max attempts/deadline.

## 22. Источники идей и ориентиры

Это не зависимости, которые нужно слепо копировать, а архитектурные ориентиры, проверенные в открытых проектах:

- [OpenHands](https://github.com/All-Hands-AI/OpenHands) — независимый Agent Canvas, несколько agent backends, local/Docker/VM execution и явное предупреждение об опасности запуска без sandbox;
- [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk) — event-driven conversation как serializable source of truth, typed tools и MCP;
- [SWE-agent / mini-SWE-agent](https://github.com/SWE-agent/SWE-agent) — простой agent-computer interface, максимальная агентность модели и воспроизводимая конфигурация;
- [E2B infrastructure](https://github.com/e2b-dev/infra) — отдельная инфраструктура изолированных сред для выполнения кода агентами;
- [Context Bridge](https://github.com/ershiyidian/context-bridge) — bounded repository context, Tree-sitter/PageRank map, secret scanning и transactional structured patches;
- [Rhizome](https://github.com/basidiocarp/rhizome) — компактная code intelligence поверхность на Tree-sitter и LSP;
- Temporal/LangGraph durable workflow patterns — append-only history, checkpointing, idempotent activities, retries и human-in-the-loop pauses.

При расхождении приоритет имеет это ТЗ: Master Room остаётся модель-независимой комнатой инструментов, а не превращается в ещё одного Кодера.
