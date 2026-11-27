# Улучшения Master Room по ТЗ (MASTER_ROOM_CODER_TECHNICAL_SPEC)

Дата: 2026-07-11

Примечание: ТЗ рассматривалось критически (по договорённости). Там, где уже
существующая реализация в проекте была лучше буквы ТЗ, сохранён существующий
подход — такие места отмечены ниже.

## 1. Безопасность: устранён shell=True (ТЗ §10.2, §18, §20)

`bot/coder/tester.py` — команда тестов больше не передаётся строкой в shell.
Команда разбирается через `shlex.split()` в argv-список и исполняется с
`shell=False`. Это закрывает инъекции через метасимволы (`&&`, `|`, `;`, `$()`).

## 2. Новые инструменты coder-server (ТЗ §5)

Добавлено в `bot/coder_server/tools.py`:

- `fs.edit` — точечная замена old_text/new_text с опциональным precondition
  `expected_sha256` (защита от правок по устаревшему чтению) и защитой от
  неоднозначного якоря (несколько совпадений → ошибка, если нет `replace_all`).
- `fs.patch_multi` — транзакционный multi-file patch (приёмочный сценарий 2 ТЗ):
  сначала verify всех hash/якорей, затем атомарное применение. Один неверный
  hash — полный abort, ни один файл не изменён.
- `fs.copy` — копирование файлов/директорий внутри workspace.
- `fs.tree` — карта проекта без vendor/generated шума (node_modules, .git...).
- `fs.encoding_check` — поиск файлов не в UTF-8 и replacement characters (ТЗ §14).
- `repository.rules` — сбор применимых инструкций (AGENTS.md, CONTRIBUTING.md,
  README.md, .editorconfig) — ТЗ §7.2.
- `dependencies.install` — установка строго через менеджер пакетов (pip/npm/pnpm),
  argv собирается детерминированно, спецификации пакетов валидируются
  (запрещены флаги вида `--index-url=...`) — ТЗ §5.5.
- `dependencies.audit` — pip check / npm audit / pnpm audit.

Структурированные ошибки команд: `command.run`/тест-инструменты при ненулевом
exit code теперь возвращают полный stdout/stderr/exit code в `output`
(`ToolExecutionError`), а не только текст ошибки — ТЗ §5.6 (Кодеру нужен полный
вывод для цикла исправления).

## 3. Durable runtime (ТЗ §6)

- `JobQueue.reclaim_expired()` — джобы с истёкшим lease возвращаются в очередь
  (или уходят в dead letter при исчерпании попыток); вызывается при каждом
  lease. Раньше джоба умершего воркера зависала в `running` навсегда.
- `CoderService.recover()` + `POST /api/v1/recover` — восстановление после
  рестарта процесса: reclaim lease и перезапуск воркеров для нетерминальных
  задач без живого воркера (кроме paused/waiting_approval).
- Pause теперь записывает checkpoint с `resume_state`, resume валидирует
  целевое состояние против FSM (недопустимое → безопасный откат в queued).

## 4. Evidence-gated завершение (ТЗ §11)

- `CoderService.complete()` + `POST /api/v1/tasks/{id}/complete` — задача не
  может стать `completed` без свежих tool-результатов, привязанных к текущему
  workspace hash. Без evidence — событие `verification.failed` и возврат в
  `executing`.

## 5. Честность данных в Telegram-роутах

- Убран fallback с «правдоподобными» фейковыми логами в `/coder logs`.
- `merge_approve` больше не выдумывает PR #42 и commit-хэш: фиксируется только
  реальный факт (решение владельца), решение пробрасывается в durable runtime
  (`approve`), фактический merge подтверждается evidence.
- Экран статуса показывает фактические метрики хоста (platform/psutil) вместо
  захардкоженных «Oracle Cloud A1, 24 GB RAM, 412 часов Render».
- `run_id` генерируется через uuid вместо `random.randint(100, 999)`
  (исключены коллизии PRIMARY KEY).

## 6. Отклонения от буквы ТЗ (осознанные)

- ТЗ предлагает отдельные микросервисы/контейнеры для воркеров; существующая
  реализация с asyncio-воркерами + SQLite job queue сохранена — она проще,
  соответствует duty ТЗ (durability, lease, dead letter) и не требует Docker
  на хосте бота.
- `apply_patch` в формате unified diff не добавлялся: `fs.patch` /
  `fs.patch_multi` с якорями и hash-precondition надёжнее для LLM-агентов
  (нет проблемы смещения номеров строк).

## Тесты

`tests/test_coder_server_improvements.py` — 12 новых тестов: hash precondition,
транзакционность patch_multi, tree/encoding_check, repo.rules, валидация
dependencies.install, структурированные ошибки команд, lease reclaim,
pause/resume checkpoint, evidence gate, recover, отсутствие shell=True.

Итог: 80/80 тестов coder/masterroom проходят
(`pytest tests/test_coder_server*.py tests/test_masterroom*.py`).
