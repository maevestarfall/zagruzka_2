# Local sync journal: SQLite outbox mirror per contract 18.3 §3 (ЧАСТЬ 18.5).
# Co-authored with CoCo
"""Локальная часть ЕДИНОГО журнала синхронизации (ЧАСТЬ 18.5 §3.2).

КРИТИЧНО (18.5): журнал живёт НЕ в ``state.json`` — ``storage._save()``
синхронно переписывает весь state при каждом изменении, журнал внутри
него создал бы лавину перезаписей и риск рекурсии. Поэтому журнал —
отдельный SQLite-файл ``DATA_DIR/sync_journal.db`` по ГОТОВОМУ образцу
``bot/jobs.py`` + ``JOBS_DB_PATH``: aiosqlite (уже в зависимостях),
connection-per-operation, WAL + busy_timeout.

Файл журнала входит в реестр исключений 18.1
(``sync_journal.db`` / ``sync_journal.db-*`` в
``categories.EXCLUSION_FILE_PATTERNS``) — сам журнал НИКОГДА не
синхронизируется наружу.

Контракт записей — тот же, что у внешней таблицы
``masterroom_sync.sync_outbox`` (канонический inline-контракт 18.3 §3):
стабильный ``operation_id`` (PK), entity type, entity ID, revision,
checksum, статус (``pending``/``delivering``/``confirmed``/``failed``),
число попыток. Повтор с тем же ``operation_id`` — upsert, НЕ дубль;
``operation_id`` существующих записей никогда не меняется (18.0 п.5).

В журнале — ТОЛЬКО метаданные. Значения секретов сюда не попадают
никогда (18.0 п.3); ``error_public`` проходит write-time санитайзер.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .sanitize import sanitize_error_public

logger = logging.getLogger(__name__)

STATUSES: tuple[str, ...] = ("pending", "delivering", "confirmed", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_outbox (
    operation_id  TEXT    PRIMARY KEY,
    category      TEXT    NOT NULL,
    entity_type   TEXT    NOT NULL,
    entity_id     TEXT    NOT NULL,
    revision      INTEGER NOT NULL,
    checksum      TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN
                  ('pending','delivering','confirmed','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error_public  TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_outbox_status
    ON sync_outbox(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_sync_outbox_category
    ON sync_outbox(category);

CREATE TABLE IF NOT EXISTS sync_local_meta (
    entity_type     TEXT    NOT NULL,
    entity_id       TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 0,
    value_checksum  TEXT    NOT NULL DEFAULT '',
    remote_checksum TEXT    NOT NULL DEFAULT '',
    tombstone       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS sync_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT    NOT NULL,
    entity_id       TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    local_revision  INTEGER NOT NULL,
    remote_revision INTEGER NOT NULL,
    local_checksum  TEXT    NOT NULL,
    remote_checksum TEXT    NOT NULL,
    local_value     TEXT,
    remote_value    TEXT,
    created_at      TEXT    NOT NULL,
    UNIQUE (entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS retention_dropped (
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    category    TEXT NOT NULL,
    dropped_at  TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS retention_intents (
    category   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# v2 (ЧАСТЬ 18.6): sync_local_meta (монотонные ревизии на сущность),
# sync_meta (курсор pull), sync_conflicts (неясные конфликты для owner'а).
# ВСЁ — в том же единственном файле журнала: это МЕТАДАННЫЕ единого
# журнала, а не второй outbox (18.6 «Запреты»). Значения секретов в
# sync_conflicts НЕ сохраняются никогда (local_value/remote_value = NULL
# для категории secrets).
#
# v3 (ЧАСТЬ 18.7): retention_dropped — реестр сущностей, локальная копия
# которых удалена retention-механизмом (внешняя копия ЦЕЛА): reconcile
# НЕ ставит на них tombstone, pull НЕ восстанавливает их, пока retention
# категории выключен. retention_intents — журнал намерения удаления
# («сначала журнал намерения, затем удаление, повторный запуск
# докатывает»). В payload — ТОЛЬКО метаданные (entity ids), никаких
# значений и тем более секретов.
_SCHEMA_VERSION = 3
_MIGRATIONS: dict[int, str] = {
    2: """
CREATE TABLE IF NOT EXISTS sync_local_meta (
    entity_type     TEXT    NOT NULL,
    entity_id       TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 0,
    value_checksum  TEXT    NOT NULL DEFAULT '',
    remote_checksum TEXT    NOT NULL DEFAULT '',
    tombstone       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS sync_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT    NOT NULL,
    entity_id       TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    local_revision  INTEGER NOT NULL,
    remote_revision INTEGER NOT NULL,
    local_checksum  TEXT    NOT NULL,
    remote_checksum TEXT    NOT NULL,
    local_value     TEXT,
    remote_value    TEXT,
    created_at      TEXT    NOT NULL,
    UNIQUE (entity_type, entity_id)
);
""",
    3: """
CREATE TABLE IF NOT EXISTS retention_dropped (
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    category    TEXT NOT NULL,
    dropped_at  TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS retention_intents (
    category   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SyncJournal:
    """Тонкая async-обёртка над ``sync_journal.db`` (паттерн JobQueue)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_ready = False

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA synchronous=NORMAL")
            yield db

    async def init(self) -> None:
        """Создать/мигрировать схему. Идемпотентно и дёшево."""
        if self._schema_ready and self.db_path.exists():
            return
        async with self._connect() as db:
            await db.executescript(_SCHEMA)
            cur = await db.execute("PRAGMA user_version")
            row = await cur.fetchone()
            current = row[0] if row else 0
            if current == 0:
                current = 1
            for version in range(current + 1, _SCHEMA_VERSION + 1):
                script = _MIGRATIONS.get(version)
                if script is None:
                    raise RuntimeError(
                        f"sync_journal.db at schema {current}, no migration "
                        f"to {version}"
                    )
                await db.executescript(script)
                current = version
            await db.execute(f"PRAGMA user_version = {int(_SCHEMA_VERSION)}")
            await db.commit()
        self._schema_ready = True

    # ─── операции ────────────────────────────────────────────────────────

    async def get(self, operation_id: str) -> dict[str, Any] | None:
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT operation_id, category, entity_type, entity_id, "
                "revision, checksum, status, attempt_count, error_public, "
                "created_at, updated_at FROM sync_outbox "
                "WHERE operation_id=?",
                (operation_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "operation_id": row[0], "category": row[1],
            "entity_type": row[2], "entity_id": row[3],
            "revision": row[4], "checksum": row[5], "status": row[6],
            "attempt_count": row[7], "error_public": row[8],
            "created_at": row[9], "updated_at": row[10],
        }

    async def upsert_pending(
        self,
        *,
        operation_id: str,
        category: str,
        entity_type: str,
        entity_id: str,
        revision: int,
        checksum: str,
    ) -> dict[str, Any]:
        """Идемпотентный upsert по стабильному ``operation_id``.

        Повтор с тем же ``operation_id`` НЕ создаёт дубль и НЕ меняет
        ``created_at``/``attempt_count``. Если запись уже ``confirmed``
        с ТЕМ ЖЕ checksum — статус сохраняется (resume пропускает её);
        checksum изменился → запись честно возвращается в ``pending``.
        """
        await self.init()
        now = _now()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO sync_outbox (operation_id, category, "
                "entity_type, entity_id, revision, checksum, status, "
                "attempt_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?) "
                "ON CONFLICT(operation_id) DO UPDATE SET "
                "  category=excluded.category, "
                "  revision=excluded.revision, "
                "  checksum=excluded.checksum, "
                "  status=CASE WHEN sync_outbox.status='confirmed' "
                "              AND sync_outbox.checksum=excluded.checksum "
                "         THEN 'confirmed' ELSE 'pending' END, "
                "  updated_at=excluded.updated_at",
                (operation_id, category, entity_type, entity_id,
                 int(revision), checksum, now, now),
            )
            await db.commit()
        row = await self.get(operation_id)
        assert row is not None
        return row

    async def mark_delivering(self, operation_id: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE sync_outbox SET status='delivering', "
                "attempt_count=attempt_count+1, updated_at=? "
                "WHERE operation_id=?",
                (_now(), operation_id),
            )
            await db.commit()

    async def mark_confirmed(self, operation_id: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE sync_outbox SET status='confirmed', "
                "error_public=NULL, updated_at=? WHERE operation_id=?",
                (_now(), operation_id),
            )
            await db.commit()

    async def mark_failed(self, operation_id: str, error: str) -> None:
        """Пометить операцию failed. Текст ошибки санитизируется НА ЗАПИСИ."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE sync_outbox SET status='failed', error_public=?, "
                "updated_at=? WHERE operation_id=?",
                (sanitize_error_public(error), _now(), operation_id),
            )
            await db.commit()

    async def stats_by_category(self) -> dict[str, dict[str, int]]:
        """{category: {status: count}} — для экрана прогресса."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT category, status, COUNT(*) FROM sync_outbox "
                "GROUP BY category, status"
            )
            rows = await cur.fetchall()
        out: dict[str, dict[str, int]] = {}
        for cat, status, n in rows:
            out.setdefault(cat, {})[status] = int(n)
        return out

    async def list_failed(self, limit: int = 200) -> list[dict[str, Any]]:
        """Ошибки для «Повторить ошибки» (обработка повторов — 18.6)."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT operation_id, category, entity_type, entity_id, "
                "revision, checksum, status, attempt_count, error_public, "
                "created_at, updated_at FROM sync_outbox "
                "WHERE status='failed' ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            )
            rows = await cur.fetchall()
        return [
            {
                "operation_id": r[0], "category": r[1], "entity_type": r[2],
                "entity_id": r[3], "revision": r[4], "checksum": r[5],
                "status": r[6], "attempt_count": r[7], "error_public": r[8],
                "created_at": r[9], "updated_at": r[10],
            }
            for r in rows
        ]

    # ─── ЧАСТЬ 18.6: инкрементальная синхронизация ───────────────────────

    async def list_pending(self, limit: int = 500) -> list[dict[str, Any]]:
        """Операции ``pending`` для воркера доставки (18.6 §1)."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT operation_id, category, entity_type, entity_id, "
                "revision, checksum, status, attempt_count, error_public, "
                "created_at, updated_at FROM sync_outbox "
                "WHERE status IN ('pending','delivering') "
                "ORDER BY updated_at ASC LIMIT ?",
                (int(limit),),
            )
            rows = await cur.fetchall()
        return [
            {
                "operation_id": r[0], "category": r[1], "entity_type": r[2],
                "entity_id": r[3], "revision": r[4], "checksum": r[5],
                "status": r[6], "attempt_count": r[7], "error_public": r[8],
                "created_at": r[9], "updated_at": r[10],
            }
            for r in rows
        ]

    async def retry_failed(self) -> int:
        """«Повторить ошибки» (18.6 §4): failed → pending с ТЕМ ЖЕ
        ``operation_id`` (никаких новых записей/дублей). Возвращает
        число переведённых операций."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE sync_outbox SET status='pending', updated_at=? "
                "WHERE status='failed'",
                (_now(),),
            )
            await db.commit()
            return int(cur.rowcount or 0)

    async def counts_by_status(self) -> dict[str, int]:
        """{status: count} — для экрана 18.2 (pending/failed/confirmed)."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT status, COUNT(*) FROM sync_outbox GROUP BY status"
            )
            rows = await cur.fetchall()
        return {str(s): int(n) for s, n in rows}

    # локальные ревизии на сущность (монотонные, 18.6 §1)

    async def local_meta_get(
        self, entity_type: str, entity_id: str,
    ) -> dict[str, Any] | None:
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT entity_type, entity_id, category, revision, "
                "value_checksum, remote_checksum, tombstone, updated_at "
                "FROM sync_local_meta WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "entity_type": row[0], "entity_id": row[1], "category": row[2],
            "revision": int(row[3]), "value_checksum": row[4],
            "remote_checksum": row[5], "tombstone": bool(row[6]),
            "updated_at": row[7],
        }

    async def local_meta_all(self) -> dict[tuple[str, str], dict[str, Any]]:
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT entity_type, entity_id, category, revision, "
                "value_checksum, remote_checksum, tombstone, updated_at "
                "FROM sync_local_meta"
            )
            rows = await cur.fetchall()
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            out[(str(r[0]), str(r[1]))] = {
                "entity_type": r[0], "entity_id": r[1], "category": r[2],
                "revision": int(r[3]), "value_checksum": r[4],
                "remote_checksum": r[5], "tombstone": bool(r[6]),
                "updated_at": r[7],
            }
        return out

    async def local_meta_upsert(
        self,
        *,
        entity_type: str,
        entity_id: str,
        category: str,
        revision: int,
        value_checksum: str = "",
        remote_checksum: str = "",
        tombstone: bool = False,
    ) -> None:
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO sync_local_meta (entity_type, entity_id, "
                "category, revision, value_checksum, remote_checksum, "
                "tombstone, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_type, entity_id) DO UPDATE SET "
                "  category=excluded.category, revision=excluded.revision, "
                "  value_checksum=excluded.value_checksum, "
                "  remote_checksum=excluded.remote_checksum, "
                "  tombstone=excluded.tombstone, "
                "  updated_at=excluded.updated_at",
                (entity_type, entity_id, category, int(revision),
                 value_checksum, remote_checksum, 1 if tombstone else 0,
                 _now()),
            )
            await db.commit()

    # key/value метаданные (курсор pull, 18.6 §2)

    async def meta_get(self, key: str) -> str | None:
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT v FROM sync_meta WHERE k=?", (key,)
            )
            row = await cur.fetchone()
        return str(row[0]) if row is not None else None

    async def meta_set(self, key: str, value: str) -> None:
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO sync_meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)),
            )
            await db.commit()

    # конфликты (18.6 §3) — обе версии, БЕЗ секретов

    async def conflict_add(
        self,
        *,
        entity_type: str,
        entity_id: str,
        category: str,
        local_revision: int,
        remote_revision: int,
        local_checksum: str,
        remote_checksum: str,
        local_value: str | None,
        remote_value: str | None,
    ) -> None:
        """Зафиксировать неясный конфликт. Повтор по той же сущности —
        upsert (обновляет версии), не дубль. Для категории secrets
        значения НЕ передавать (None) — метаданных достаточно."""
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO sync_conflicts (entity_type, entity_id, "
                "category, local_revision, remote_revision, local_checksum, "
                "remote_checksum, local_value, remote_value, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_type, entity_id) DO UPDATE SET "
                "  local_revision=excluded.local_revision, "
                "  remote_revision=excluded.remote_revision, "
                "  local_checksum=excluded.local_checksum, "
                "  remote_checksum=excluded.remote_checksum, "
                "  local_value=excluded.local_value, "
                "  remote_value=excluded.remote_value",
                (entity_type, entity_id, category, int(local_revision),
                 int(remote_revision), local_checksum, remote_checksum,
                 local_value, remote_value, _now()),
            )
            await db.commit()

    async def conflicts_list(self, limit: int = 50) -> list[dict[str, Any]]:
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT id, entity_type, entity_id, category, "
                "local_revision, remote_revision, local_checksum, "
                "remote_checksum, local_value, remote_value, created_at "
                "FROM sync_conflicts ORDER BY id ASC LIMIT ?",
                (int(limit),),
            )
            rows = await cur.fetchall()
        return [self._conflict_row(r) for r in rows]

    async def conflict_get(self, conflict_id: int) -> dict[str, Any] | None:
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT id, entity_type, entity_id, category, "
                "local_revision, remote_revision, local_checksum, "
                "remote_checksum, local_value, remote_value, created_at "
                "FROM sync_conflicts WHERE id=?",
                (int(conflict_id),),
            )
            row = await cur.fetchone()
        return self._conflict_row(row) if row is not None else None

    async def conflict_delete(self, conflict_id: int) -> None:
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM sync_conflicts WHERE id=?", (int(conflict_id),)
            )
            await db.commit()

    async def conflict_count(self) -> int:
        await self.init()
        async with self._connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM sync_conflicts")
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def conflict_entity_keys(self) -> set[tuple[str, str]]:
        """(entity_type, entity_id) сущностей в конфликте — они
        исключаются из авто-push/pull до решения owner'а (18.6 §3)."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT entity_type, entity_id FROM sync_conflicts"
            )
            rows = await cur.fetchall()
        return {(str(r[0]), str(r[1])) for r in rows}

    # ЧАСТЬ 18.7: retention — реестр удалённых локальных копий и журнал
    # намерения удаления. В обеих таблицах — ТОЛЬКО метаданные (никаких
    # значений, тем более секретов).

    async def retention_dropped_add(
        self, entity_type: str, entity_id: str, category: str,
    ) -> None:
        """Зафиксировать: локальная копия сущности удалена retention'ом
        (внешняя копия ЦЕЛА). Повтор — idempotent upsert."""
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO retention_dropped (entity_type, entity_id, "
                "category, dropped_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(entity_type, entity_id) DO UPDATE SET "
                "  category=excluded.category, dropped_at=excluded.dropped_at",
                (entity_type, entity_id, category, _now()),
            )
            await db.commit()

    async def retention_dropped_keys(self) -> set[tuple[str, str]]:
        """(entity_type, entity_id) всех retention-удалённых сущностей:
        reconcile НЕ ставит на них tombstone, pull НЕ восстанавливает."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT entity_type, entity_id FROM retention_dropped"
            )
            rows = await cur.fetchall()
        return {(str(r[0]), str(r[1])) for r in rows}

    async def retention_dropped_by_category(
        self, category: str,
    ) -> set[tuple[str, str]]:
        """(entity_type, entity_id) retention-удалённых сущностей категории."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT entity_type, entity_id FROM retention_dropped "
                "WHERE category=?",
                (category,),
            )
            rows = await cur.fetchall()
        return {(str(r[0]), str(r[1])) for r in rows}

    async def retention_dropped_remove(
        self, entity_type: str, entity_id: str,
    ) -> None:
        """Сущность снова появилась локально — убрать из реестра."""
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM retention_dropped "
                "WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id),
            )
            await db.commit()

    async def retention_dropped_clear_category(self, category: str) -> int:
        """Retention категории снова включён: реестр очищается, pull
        снова может восстанавливать сущности категории."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "DELETE FROM retention_dropped WHERE category=?",
                (category,),
            )
            await db.commit()
            return int(cur.rowcount or 0)

    async def local_meta_delete(
        self, entity_type: str, entity_id: str,
    ) -> None:
        """Удалить meta-строку сущности (только при повторном включении
        retention — чтобы pull мог восстановить внешнюю копию)."""
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM sync_local_meta "
                "WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id),
            )
            await db.commit()

    async def retention_intent_set(self, category: str, payload: str) -> None:
        """Журнал намерения удаления («сначала журнал намерения, затем
        удаление»). payload — JSON-список метаданных сущностей."""
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO retention_intents (category, payload, "
                "created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(category) DO UPDATE SET "
                "  payload=excluded.payload, created_at=excluded.created_at",
                (category, payload, _now()),
            )
            await db.commit()

    async def retention_intent_get(self, category: str) -> str | None:
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT payload FROM retention_intents WHERE category=?",
                (category,),
            )
            row = await cur.fetchone()
        return str(row[0]) if row is not None else None

    async def retention_intents_all(self) -> dict[str, str]:
        """{category: payload} всех незавершённых намерений (для resume)."""
        await self.init()
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT category, payload FROM retention_intents"
            )
            rows = await cur.fetchall()
        return {str(r[0]): str(r[1]) for r in rows}

    async def retention_intent_clear(self, category: str) -> None:
        await self.init()
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM retention_intents WHERE category=?",
                (category,),
            )
            await db.commit()

    @staticmethod
    def _conflict_row(r: Any) -> dict[str, Any]:
        return {
            "id": int(r[0]), "entity_type": r[1], "entity_id": r[2],
            "category": r[3], "local_revision": int(r[4]),
            "remote_revision": int(r[5]), "local_checksum": r[6],
            "remote_checksum": r[7], "local_value": r[8],
            "remote_value": r[9], "created_at": r[10],
        }


# ─── module-level singleton ──────────────────────────────────────────────────

_journal: SyncJournal | None = None


def journal_db_path() -> Path:
    from ....config import DATA_DIR  # noqa: PLC0415 — ленивый импорт

    return Path(DATA_DIR) / "sync_journal.db"


def get_sync_journal() -> SyncJournal:
    """Процесс-wide журнал, привязанный к ``DATA_DIR/sync_journal.db``."""
    global _journal
    if _journal is None:
        _journal = SyncJournal(journal_db_path())
    return _journal
