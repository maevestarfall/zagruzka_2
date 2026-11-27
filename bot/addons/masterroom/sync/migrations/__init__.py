# Versioned schema migrations for masterroom_sync (ЧАСТЬ 18.3 §3).
# Co-authored with CoCo
"""Versioned-миграции схемы внешнего PostgreSQL (ЧАСТЬ 18.3 §3).

Правила:

* ВСЕ объекты бота — ТОЛЬКО в выделенной схеме ``masterroom_sync``
  (18.0 п.5). Первый шаг v1 — ``CREATE SCHEMA IF NOT EXISTS``.
  ``public`` и любые таблицы каркаса сайта НЕ трогаются никогда,
  даже на общей с сайтом БД.
* Версия фиксируется в ``masterroom_sync.sync_schema_version``
  (version, applied_at) — НЕ «просто CREATE TABLE IF NOT EXISTS».
  Версионирование собственное и независимое от миграций каркаса.
* Миграции — упорядоченный список шагов, применяются последовательно,
  идемпотентно, каждый шаг — в своей транзакции; версия пишется в той
  же транзакции, что и шаг.
* Advisory-lock защищает от параллельного прогона (два инстанса).
* ``sync_outbox`` — ЕДИНСТВЕННЫЙ журнал; поля — канонический
  inline-контракт 18.3 §3 (совместим с контрактом ЧАСТИ 03 каркаса).
  Второго журнала нет и не будет.

Модуль не имеет внешних зависимостей на import (asyncpg сюда попадает
только как уже открытое соединение) — ленивость сохранена.
"""

from __future__ import annotations

from typing import Any

SCHEMA = "masterroom_sync"

# Стабильный ключ advisory-lock миграций (произвольная константа бота).
_MIGRATION_LOCK_KEY = 0x6D725F73796E63  # "mr_sync"

# ─── Шаги миграций ───────────────────────────────────────────────────────────
# Каждый элемент: (version, name, [SQL statements]). version — строго
# возрастающие целые. Statements идемпотентны (IF NOT EXISTS), но версия
# всё равно фиксируется: пропуск/повтор шагов определяется по
# sync_schema_version, а не по наличию таблиц.

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "initial masterroom_sync schema: entities, files, outbox, healthcheck",
        [
            f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
            # Сущности по категориям (18.1): payload JSONB + tombstone.
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_entities (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                category    TEXT        NOT NULL,
                entity_type TEXT        NOT NULL,
                entity_id   TEXT        NOT NULL,
                revision    BIGINT      NOT NULL,
                checksum    TEXT        NOT NULL,
                payload     JSONB,
                tombstone   BOOLEAN     NOT NULL DEFAULT FALSE,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (entity_type, entity_id)
            )
            """,
            f"""
            CREATE INDEX IF NOT EXISTS sync_entities_category_idx
                ON {SCHEMA}.sync_entities (category)
            """,
            # Метаданные крупных файлов: сами файлы в S3, тут только
            # object key + размер + checksum (18.3 §2).
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_files (
                object_key  TEXT        PRIMARY KEY,
                category    TEXT        NOT NULL,
                entity_type TEXT        NOT NULL,
                entity_id   TEXT        NOT NULL,
                byte_size   BIGINT      NOT NULL,
                checksum    TEXT        NOT NULL,
                tombstone   BOOLEAN     NOT NULL DEFAULT FALSE,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            # ЕДИНСТВЕННЫЙ журнал sync_outbox — канонический контракт
            # 18.3 §3: operation_id стабилен и никогда не меняется при
            # повторах; upsert — по operation_id.
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_outbox (
                operation_id  TEXT        PRIMARY KEY,
                entity_type   TEXT        NOT NULL,
                entity_id     TEXT        NOT NULL,
                revision      BIGINT      NOT NULL,
                checksum      TEXT        NOT NULL,
                status        TEXT        NOT NULL CHECK (status IN
                              ('pending','delivering','confirmed','failed')),
                attempt_count INTEGER     NOT NULL DEFAULT 0,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            f"""
            CREATE INDEX IF NOT EXISTS sync_outbox_status_updated_idx
                ON {SCHEMA}.sync_outbox (status, updated_at)
            """,
            # Служебная таблица для «Проверить подключение» (18.3 §4):
            # тестовая запись+чтение+удаление НЕ в таблицах данных.
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_healthcheck (
                probe_id   TEXT        PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
        ],
    ),
]


def latest_version() -> int:
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


async def get_current_version(conn: Any) -> int:
    """Текущая версия схемы бота (0 — схемы/таблицы версий ещё нет)."""
    exists = await conn.fetchval(
        "SELECT to_regclass($1)", f"{SCHEMA}.sync_schema_version"
    )
    if exists is None:
        return 0
    v = await conn.fetchval(
        f"SELECT COALESCE(MAX(version), 0) FROM {SCHEMA}.sync_schema_version"
    )
    return int(v or 0)


async def apply_migrations(conn: Any) -> int:
    """Применить недостающие миграции; вернуть итоговую версию.

    Идемпотентно: повторный вызов на актуальной схеме не выполняет
    ни одного шага и не дублирует записи версий. Advisory-lock
    сериализует параллельные прогоны.
    """
    await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
    try:
        # Bootstrap: схема + таблица версий (вне нумерованных шагов —
        # без них версию читать не из чего).
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_schema_version (
                version    BIGINT      PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        current = await get_current_version(conn)
        for version, _name, statements in MIGRATIONS:
            if version <= current:
                continue
            async with conn.transaction():
                for stmt in statements:
                    await conn.execute(stmt)
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.sync_schema_version (version) "
                    f"VALUES ($1) ON CONFLICT (version) DO NOTHING",
                    version,
                )
            current = version
        return current
    finally:
        try:
            await conn.execute(
                "SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY
            )
        except Exception:  # noqa: BLE001 — соединение могло умереть
            pass
