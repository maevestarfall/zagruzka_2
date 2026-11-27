# Dry-run preview + first full sync (bootstrap) engine (ЧАСТЬ 18.5).
# Co-authored with CoCo
"""Dry-run, предпросмотр и первый полный sync (ЧАСТЬ 18.5).

Три слоя:

* :func:`build_plan` — READ-ONLY план передачи: обход state (по ключам,
  через реестр категорий 18.1) и файлов ``DATA_DIR``. Ни одной записи
  куда бы то ни было — ни во внешнюю БД, ни в журнал, ни в state.
* :func:`build_preview_report` — текст dry-run-отчёта (18.5 §1) поверх
  плана: назначение без секретов, 8 категорий с числом записей и байтов,
  retention-правила, способ защиты секретов, предупреждения.
* :func:`run_bootstrap` — сама передача (18.5 §3): каждая запись идёт
  через ЕДИНЫЙ журнал (локальный ``sync_journal.db`` + внешняя
  ``masterroom_sync.sync_outbox``, контракт 18.3 §3), подтверждение —
  ТОЛЬКО read-after-write (ID, revision, byte size, checksum — SHA-256
  от canonical JSON без поля checksum; файлы — object key + размер +
  checksum). Локальные данные НЕ изменяются и НЕ удаляются ни при каком
  исходе (удаление — 18.7).

Инварианты (18.5 «Запреты»):

* ``operation_id`` детерминирован (uuid5 от entity_type|entity_id) и
  никогда не меняется при повторах — повторный запуск / рестарт даёт
  upsert, а не дубли;
* реестр исключений 18.1 (``.frozen``, namespace ``vault``, tmp/bak,
  локальный журнал) не передаётся наружу никогда — план строится
  ТОЛЬКО через резолверы categories.py, у которых исключения = None;
* секреты уходят наружу ТОЛЬКО ciphertext (18.4); без master key
  категория ``secrets`` честно помечается недоступной и пропускается;
* kill-switch: ``selfdestruct.is_frozen()`` проверяется перед стартом
  и в цикле — при заморозке bootstrap не начинается / останавливается;
* event loop не блокируется: хеши/шифрование больших значений — через
  ``asyncio.to_thread`` (паттерн bot/tts.py), между операциями —
  ``await asyncio.sleep(0)``; файлы в S3 — стримингово (objects.py);
* ``last_sync_at`` обновляется только когда ВСЕ категории подтверждены.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import state as mr_state
from .canonical import (
    canonical_json_bytes,
    payload_checksum,
)
from .categories import (
    ALL_CATEGORIES,
    CATEGORY_LABEL,
    SyncCategory,
    _flatten_state,
    category_for_file,
    category_for_state_path,
    is_secret_file,
)

logger = logging.getLogger(__name__)

# Стабильный namespace для operation_id (18.0 п.5): uuid5 от
# "entity_type|entity_id" — одинаков между запусками и рестартами.
_OPERATION_NS = uuid.UUID("6d617374-6572-526f-6f6d-53796e633035")

# Префикс dotted-путей защищённого хранилища 18.4: записи там УЖЕ
# ciphertext-envelope — уходят как есть, без повторного шифрования.
_SECRET_STORE_PREFIX = "_settings.addons.masterroom.secret_store"

# Bootstrap-ревизия: полная передача всего среза = revision 1.
_BOOTSTRAP_REVISION = 1

ENTITY_STATE = "state_key"
ENTITY_FILE = "file"


def operation_id_for(entity_type: str, entity_id: str) -> str:
    """Детерминированный стабильный operation_id (НЕ меняется при повторах)."""
    return str(uuid.uuid5(_OPERATION_NS, f"{entity_type}|{entity_id}"))


# ─── План (READ-ONLY, общий для dry-run и bootstrap) ─────────────────────────


@dataclass
class PlanItem:
    """Одна операция передачи. value заполняется только для state-записей."""

    category: str
    entity_type: str          # ENTITY_STATE | ENTITY_FILE
    entity_id: str            # dotted-путь ключа state ИЛИ rel-путь файла
    byte_size: int
    value: Any = None         # живое значение state-листа (state-записи)
    is_secret_file: bool = False

    @property
    def operation_id(self) -> str:
        return operation_id_for(self.entity_type, self.entity_id)


@dataclass
class SyncPlan:
    """Полный план передачи: 8 категорий, включая catch-all ``other``."""

    items: dict[str, list[PlanItem]] = field(default_factory=dict)

    def totals(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for cat in ALL_CATEGORIES:
            lst = self.items.get(cat.value, [])
            out[cat.value] = {
                "records": len(lst),
                "bytes": sum(i.byte_size for i in lst),
            }
        return out


def _leaf_bytes(value: Any) -> int:
    try:
        return len(canonical_json_bytes(value))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))


def build_plan(
    state: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
) -> SyncPlan:
    """READ-ONLY план: обход state-дерева + файлов ``DATA_DIR``.

    Ничего не пишет: ни в журнал, ни в state, ни во внешнее назначение.
    Реестр исключений 18.1 отфильтровывается резолверами categories.py
    (категория ``None``); retention-переключатели на состав НЕ влияют
    (18.5 §3.1 — внешняя копия всегда полная).
    """
    if state is None:
        from ....storage import storage  # noqa: PLC0415 — ленивый импорт

        state = storage._state  # noqa: SLF001 — read-only доступ
    if data_dir is None:
        from ....config import DATA_DIR  # noqa: PLC0415

        data_dir = DATA_DIR

    plan = SyncPlan(items={c.value: [] for c in ALL_CATEGORIES})

    for dotted, value in _flatten_state(state or {}):
        cat = category_for_state_path(dotted)
        if cat is None:
            continue  # реестр исключений — единственное законное изъятие
        plan.items[cat.value].append(PlanItem(
            category=cat.value,
            entity_type=ENTITY_STATE,
            entity_id=dotted,
            byte_size=_leaf_bytes(value),
            value=value,
        ))

    root = Path(data_dir)
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            try:
                if not path.is_file():
                    continue
                rel = path.relative_to(root)
                cat = category_for_file(rel)
                if cat is None:
                    continue
                plan.items[cat.value].append(PlanItem(
                    category=cat.value,
                    entity_type=ENTITY_FILE,
                    entity_id=str(rel.as_posix()),
                    byte_size=path.stat().st_size,
                    is_secret_file=is_secret_file(rel),
                ))
            except OSError:
                continue
    return plan


async def build_plan_async(
    state: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
) -> SyncPlan:
    """План в ``to_thread`` — обход диска не блокирует event loop."""
    return await asyncio.to_thread(build_plan, state, data_dir)


# ─── Доступность назначений/категорий ────────────────────────────────────────


def _secrets_available() -> bool:
    try:
        from .secret_store import is_available  # noqa: PLC0415

        return is_available()
    except Exception:  # noqa: BLE001
        return False


def _secrets_unavailable_reason() -> str:
    try:
        from .secret_store import secrets_unavailable_reason  # noqa: PLC0415

        return secrets_unavailable_reason() or "нет ключа шифрования"
    except Exception:  # noqa: BLE001
        return "нет ключа шифрования"


def _s3_configured() -> bool:
    from . import objects  # noqa: PLC0415 — ленивый импорт

    return objects.is_configured()


def _category_unavailable_reason(plan: SyncPlan, cat: SyncCategory) -> str:
    """Причина недоступности категории для передачи ('' — доступна)."""
    items = plan.items.get(cat.value, [])
    if not items:
        return ""
    if cat is SyncCategory.SECRETS and not _secrets_available():
        return _secrets_unavailable_reason()
    if any(i.entity_type == ENTITY_FILE for i in items) and not _s3_configured():
        # файлы этой категории требуют S3 (в PostgreSQL — только метаданные)
        if all(i.entity_type == ENTITY_FILE for i in items):
            return "не настроено S3 (SYNC_S3_*) для файлов"
    return ""


# ─── Dry-run отчёт (18.5 §1) ─────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} МБ"
    return f"{n / (1024 * 1024 * 1024):.2f} ГБ"


def _html_escape(s: Any) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_preview_report(plan: SyncPlan) -> str:
    """Текст dry-run-отчёта (18.5 §1). БЕЗ единой записи куда-либо.

    Показывает: назначение (db_label, bucket — без секретов), все 8
    категорий с числом записей/байтов, retention-правила, способ защиты
    секретов, предупреждения о недоступных категориях.
    """
    from . import db, objects  # noqa: PLC0415 — ленивые импорты

    lines = ["<b>👁 Предпросмотр синхронизации (dry-run)</b>", ""]

    # Назначение — только безопасные метки (18.0 п.3).
    if db.is_configured():
        label = mr_state.get_sync_state().get("db_label") or db.db_label()
        lines.append(f"Назначение БД: <b>{_html_escape(label)}</b>")
    else:
        lines.append("Назначение БД: <b>не настроено (нет DATABASE_URL)</b>")
    if objects.is_configured():
        lines.append(
            f"Файлы (S3): <b>bucket {_html_escape(objects.bucket_label())}</b>"
        )
    else:
        lines.append("Файлы (S3): <b>не настроено (SYNC_S3_*)</b>")
    lines.append("")

    # 8 категорий — полный список, включая catch-all.
    lines.append("<b>Будет передано (все категории, полная копия):</b>")
    totals = plan.totals()
    warnings: list[str] = []
    for cat in ALL_CATEGORIES:
        agg = totals[cat.value]
        reason = _category_unavailable_reason(plan, cat)
        suffix = ""
        if reason:
            suffix = " ⚠️"
            warnings.append(f"{CATEGORY_LABEL[cat]} — {reason}")
        lines.append(
            f"• {CATEGORY_LABEL[cat]}: {agg['records']} зап., "
            f"{_fmt_bytes(agg['bytes'])}{suffix}"
        )
    lines.append("")

    # Retention-правила: что останется локально (применяются в 18.7).
    retention = mr_state.get_sync_retention()
    keep_all = all(retention.get(c.value, True) for c in ALL_CATEGORIES)
    lines.append("<b>Retention (что останется локально):</b>")
    if keep_all:
        lines.append(
            "• все переключатели ВКЛЮЧЕНЫ — «Оставить полную локальную "
            "копию» по всем категориям"
        )
    else:
        for cat in ALL_CATEGORIES:
            keep = retention.get(cat.value, True)
            lines.append(
                f"• {CATEGORY_LABEL[cat]}: "
                + ("оставить локально" if keep else "удалить локально (18.7)")
            )
    lines.append(
        "⚠️ Постоянное полное дублирование локально и во внешней БД "
        "повышает privacy-риск."
    )
    lines.append("")

    # Способ защиты секретов (18.4).
    lines.append("<b>Защита секретов:</b>")
    if _secrets_available():
        lines.append(
            "• envelope encryption (AES-256-GCM, data key на запись, "
            "master key из env) — наружу уходит ТОЛЬКО ciphertext;"
        )
        lines.append("• пароли пользователей — только argon2id-хеши.")
    else:
        lines.append(
            "• недоступна: " + _html_escape(_secrets_unavailable_reason())
        )

    if warnings:
        lines.append("")
        lines.append("<b>⚠️ Недоступные категории:</b>")
        for w in warnings:
            lines.append(f"• {_html_escape(w)}")

    lines.append("")
    lines.append(
        "<i>Это dry-run: НИ ОДНА запись не передана и не будет передана "
        "без явного подтверждения. Локальные данные не изменяются.</i>"
    )
    return "\n".join(lines)


async def build_preview_report() -> str:
    plan = await build_plan_async()
    return render_preview_report(plan)


# ─── Прогресс (компактные статусы — 18.1) ────────────────────────────────────

# Живой прогресс — в памяти процесса (экран читает отсюда). В state.json
# уходят только компактные снапшоты на границах категорий и в конце —
# иначе лавина перезаписей state.json (18.5 §2 КРИТИЧНО).
_progress: dict[str, Any] = {}
_task: asyncio.Task | None = None


def is_running() -> bool:
    return _task is not None and not _task.done()


def get_progress() -> dict[str, Any]:
    """Живой снапшот прогресса для экрана 18.2 (может быть пустым)."""
    if _progress:
        return dict(_progress)
    persisted = mr_state.get_sync_state().get("bootstrap")
    return dict(persisted) if isinstance(persisted, dict) else {}


def _persist_progress() -> None:
    """Компактный снапшот в state (только счётчики, никаких payload'ов)."""
    try:
        mr_state.set_sync_bootstrap(dict(_progress))
    except Exception:  # noqa: BLE001 — прогресс не должен ронять sync
        logger.exception("pdsync bootstrap: persist progress failed")


# ─── Bootstrap: полная передача (18.5 §3) ────────────────────────────────────


def _is_frozen() -> bool:
    from .... import selfdestruct  # noqa: PLC0415 — ленивый импорт

    try:
        return selfdestruct.is_frozen()
    except Exception:  # noqa: BLE001
        return True  # сомнение трактуем в пользу kill-switch


async def _state_payload(item: PlanItem) -> dict[str, Any] | None:
    """Payload state-записи. Секреты → ТОЛЬКО ciphertext (18.4/18.5 §3.5).

    ``None`` — запись передать нельзя (нет master key): вызывающий
    пропускает её как недоступную, plaintext наружу не уходит НИКОГДА.
    """
    value = item.value
    if item.category == SyncCategory.SECRETS.value:
        if item.entity_id == _SECRET_STORE_PREFIX or item.entity_id.startswith(
            _SECRET_STORE_PREFIX + "."
        ):
            # защищённое хранилище 18.4 — уже ciphertext-envelope
            pass
        else:
            if not _secrets_available():
                return None
            from . import crypto  # noqa: PLC0415

            raw = json.dumps(value, ensure_ascii=False)
            value = await crypto.encrypt_async(raw, aad=item.entity_id)
    payload = {
        "category": item.category,
        "entity_type": ENTITY_STATE,
        "entity_id": item.entity_id,
        "revision": _BOOTSTRAP_REVISION,
        "value": value,
    }
    return payload


async def _sync_state_item(conn: Any, jr: Any, item: PlanItem) -> str:
    """Передать одну state-запись: журнал → запись → read-after-write.

    Возвращает итоговый статус: confirmed | skipped | failed.
    """
    payload = await _state_payload(item)
    if payload is None:
        return "skipped"
    checksum = await asyncio.to_thread(payload_checksum, payload)
    expected_size = len(await asyncio.to_thread(canonical_json_bytes, payload))

    op_id = item.operation_id
    row = await jr.upsert_pending(
        operation_id=op_id, category=item.category,
        entity_type=ENTITY_STATE, entity_id=item.entity_id,
        revision=_BOOTSTRAP_REVISION, checksum=checksum,
    )
    if row["status"] == "confirmed":
        return "confirmed"  # resume: уже подтверждена с этим checksum

    await jr.mark_delivering(op_id)
    payload_json = json.dumps(payload, ensure_ascii=False)

    # Внешний журнал: upsert по стабильному operation_id (не дубль).
    await conn.execute(
        "INSERT INTO masterroom_sync.sync_outbox "
        "(operation_id, entity_type, entity_id, revision, checksum, "
        " status, attempt_count) "
        "VALUES ($1, $2, $3, $4, $5, 'delivering', 1) "
        "ON CONFLICT (operation_id) DO UPDATE SET "
        "  revision=EXCLUDED.revision, checksum=EXCLUDED.checksum, "
        "  status='delivering', "
        "  attempt_count=masterroom_sync.sync_outbox.attempt_count+1, "
        "  updated_at=now()",
        op_id, ENTITY_STATE, item.entity_id,
        _BOOTSTRAP_REVISION, checksum,
    )

    # Данные: upsert по уникальному ключу сущности (не дубль).
    await conn.execute(
        "INSERT INTO masterroom_sync.sync_entities "
        "(category, entity_type, entity_id, revision, checksum, payload, "
        " tombstone) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, FALSE) "
        "ON CONFLICT (entity_type, entity_id) DO UPDATE SET "
        "  category=EXCLUDED.category, revision=EXCLUDED.revision, "
        "  checksum=EXCLUDED.checksum, payload=EXCLUDED.payload, "
        "  tombstone=FALSE, updated_at=now()",
        item.category, ENTITY_STATE, item.entity_id,
        _BOOTSTRAP_REVISION, checksum, payload_json,
    )

    # Read-after-write (18.0 п.6): повторное чтение и сверка ID,
    # revision, byte size, checksum. Только потом confirmed.
    back = await conn.fetchrow(
        "SELECT entity_id, revision, checksum, payload::text AS payload "
        "FROM masterroom_sync.sync_entities "
        "WHERE entity_type = $1 AND entity_id = $2",
        ENTITY_STATE, item.entity_id,
    )
    ok = False
    if back is not None:
        try:
            back_payload = json.loads(back["payload"])
            back_checksum = await asyncio.to_thread(
                payload_checksum, back_payload
            )
            back_size = len(await asyncio.to_thread(
                canonical_json_bytes, back_payload
            ))
            ok = (
                back["entity_id"] == item.entity_id
                and int(back["revision"]) == _BOOTSTRAP_REVISION
                and back["checksum"] == checksum
                and back_checksum == checksum
                and back_size == expected_size
            )
        except (ValueError, TypeError, KeyError):
            ok = False
    if not ok:
        await conn.execute(
            "UPDATE masterroom_sync.sync_outbox SET status='failed', "
            "updated_at=now() WHERE operation_id=$1", op_id,
        )
        await jr.mark_failed(op_id, "read-after-write: сверка не сошлась")
        return "failed"

    await conn.execute(
        "UPDATE masterroom_sync.sync_outbox SET status='confirmed', "
        "updated_at=now() WHERE operation_id=$1", op_id,
    )
    await jr.mark_confirmed(op_id)
    return "confirmed"


async def _sync_file_item(
    conn: Any, jr: Any, item: PlanItem, data_dir: Path,
) -> str:
    """Передать файл: S3 (put→head) + метаданные в PostgreSQL.

    Секреты-файлы уходят ТОЛЬКО ciphertext (18.5 §3.4). Локальный файл
    не изменяется. Возвращает confirmed | skipped | failed.
    """
    from . import objects  # noqa: PLC0415

    if not objects.is_configured():
        return "skipped"

    abs_path = data_dir / item.entity_id
    if item.is_secret_file or item.category == SyncCategory.SECRETS.value:
        # Секрет-файл: ТОЛЬКО ciphertext, никогда открытым текстом.
        if not _secrets_available():
            return "skipped"
        from .secret_store import encrypt_file_payload  # noqa: PLC0415

        object_key = f"sync/secrets/{item.entity_id}.enc"

        def _read_and_encrypt() -> bytes:
            data = abs_path.read_bytes()
            return encrypt_file_payload(data, rel_path=item.entity_id)

        try:
            ciphertext = await asyncio.to_thread(_read_and_encrypt)
        except OSError:
            return "failed_local_read"
        put = await objects.put_bytes(object_key, ciphertext)
    else:
        object_key = f"sync/files/{item.category}/{item.entity_id}"
        try:
            put = await objects.put_file(object_key, str(abs_path))
        except OSError:
            return "failed_local_read"

    checksum = put["checksum"]
    byte_size = int(put["byte_size"])
    op_id = item.operation_id

    row = await jr.upsert_pending(
        operation_id=op_id, category=item.category,
        entity_type=ENTITY_FILE, entity_id=item.entity_id,
        revision=_BOOTSTRAP_REVISION, checksum=checksum,
    )
    if row["status"] == "confirmed":
        return "confirmed"
    await jr.mark_delivering(op_id)

    await conn.execute(
        "INSERT INTO masterroom_sync.sync_outbox "
        "(operation_id, entity_type, entity_id, revision, checksum, "
        " status, attempt_count) "
        "VALUES ($1, $2, $3, $4, $5, 'delivering', 1) "
        "ON CONFLICT (operation_id) DO UPDATE SET "
        "  revision=EXCLUDED.revision, checksum=EXCLUDED.checksum, "
        "  status='delivering', "
        "  attempt_count=masterroom_sync.sync_outbox.attempt_count+1, "
        "  updated_at=now()",
        op_id, ENTITY_FILE, item.entity_id, _BOOTSTRAP_REVISION, checksum,
    )

    # Метаданные файла в PostgreSQL (сами байты — только в S3, 18.3 §2).
    await conn.execute(
        "INSERT INTO masterroom_sync.sync_files "
        "(object_key, category, entity_type, entity_id, byte_size, "
        " checksum, tombstone) "
        "VALUES ($1, $2, $3, $4, $5, $6, FALSE) "
        "ON CONFLICT (object_key) DO UPDATE SET "
        "  category=EXCLUDED.category, entity_id=EXCLUDED.entity_id, "
        "  byte_size=EXCLUDED.byte_size, checksum=EXCLUDED.checksum, "
        "  tombstone=FALSE, updated_at=now()",
        object_key, item.category, ENTITY_FILE, item.entity_id,
        byte_size, checksum,
    )

    # Read-after-write: head/stat в S3 (object key + размер) + сверка
    # метаданных, прочитанных назад из PostgreSQL (checksum).
    head = await objects.head_object(object_key)
    back = await conn.fetchrow(
        "SELECT object_key, entity_id, byte_size, checksum "
        "FROM masterroom_sync.sync_files WHERE object_key = $1",
        object_key,
    )
    ok = (
        head is not None
        and int(head["byte_size"]) == byte_size
        and back is not None
        and back["object_key"] == object_key
        and back["entity_id"] == item.entity_id
        and int(back["byte_size"]) == byte_size
        and back["checksum"] == checksum
    )
    if not ok:
        await conn.execute(
            "UPDATE masterroom_sync.sync_outbox SET status='failed', "
            "updated_at=now() WHERE operation_id=$1", op_id,
        )
        await jr.mark_failed(op_id, "read-after-write: сверка не сошлась")
        return "failed"

    await conn.execute(
        "UPDATE masterroom_sync.sync_outbox SET status='confirmed', "
        "updated_at=now() WHERE operation_id=$1", op_id,
    )
    await jr.mark_confirmed(op_id)
    return "confirmed"


async def run_bootstrap() -> dict[str, Any]:
    """Первый полный sync (18.5 §3–4). Вызывать ТОЛЬКО после явного
    подтверждения owner («Подтверждаю передачу всех данных»).

    Категории независимы: ошибка одной не блокирует остальные.
    Возвращает итоговый снапшот прогресса.
    """
    from . import db  # noqa: PLC0415
    from .journal import get_sync_journal  # noqa: PLC0415
    from ....config import DATA_DIR  # noqa: PLC0415

    global _progress
    _progress = {
        "status": "running",
        "started_at": int(time.time()),
        "finished_at": None,
        "categories": {
            c.value: {"total": 0, "confirmed": 0, "failed": 0, "skipped": 0}
            for c in ALL_CATEGORIES
        },
    }

    # Kill-switch имеет приоритет над sync (18.0 п.9).
    if _is_frozen():
        _progress["status"] = "frozen"
        _persist_progress()
        return dict(_progress)
    if not db.is_configured():
        _progress["status"] = "error"
        mr_state.set_sync_last_error_public(
            "внешняя БД не настроена (нет DATABASE_URL)"
        )
        _persist_progress()
        return dict(_progress)

    plan = await build_plan_async()
    for cat in ALL_CATEGORIES:
        _progress["categories"][cat.value]["total"] = len(
            plan.items.get(cat.value, [])
        )

    jr = get_sync_journal()
    await jr.init()

    conn = None
    stopped = False
    try:
        conn = await db.connect()
        if conn is None:
            raise RuntimeError("нет подключения к внешней БД")
        from .migrations import apply_migrations  # noqa: PLC0415

        await apply_migrations(conn)

        data_dir = Path(DATA_DIR)
        for cat in ALL_CATEGORIES:
            if stopped:
                break
            counters = _progress["categories"][cat.value]
            try:
                for item in plan.items.get(cat.value, []):
                    # kill-switch / пауза — проверка в цикле (18.5 §3.6-7):
                    # «Пауза» останавливает после текущей операции.
                    if _is_frozen():
                        _progress["status"] = "frozen"
                        stopped = True
                        break
                    if mr_state.is_sync_paused():
                        _progress["status"] = "paused"
                        stopped = True
                        break
                    try:
                        if item.entity_type == ENTITY_STATE:
                            res = await _sync_state_item(conn, jr, item)
                        else:
                            res = await _sync_file_item(
                                conn, jr, item, data_dir
                            )
                    except Exception as exc:  # noqa: BLE001
                        res = "failed"
                        try:
                            await jr.upsert_pending(
                                operation_id=item.operation_id,
                                category=item.category,
                                entity_type=item.entity_type,
                                entity_id=item.entity_id,
                                revision=_BOOTSTRAP_REVISION,
                                checksum="",
                            )
                            await jr.mark_failed(
                                item.operation_id, str(exc),
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "pdsync bootstrap: journal write failed"
                            )
                    if res == "confirmed":
                        counters["confirmed"] += 1
                    elif res == "skipped":
                        counters["skipped"] += 1
                    else:
                        counters["failed"] += 1
                    # бот продолжает отвечать во время bootstrap (18.5 §5)
                    await asyncio.sleep(0)
            except Exception:  # noqa: BLE001 — категория не блокирует другие
                logger.exception(
                    "pdsync bootstrap: category %s crashed", cat.value
                )
            # компактный снапшот на границе категории (не лавина записей)
            _persist_progress()
    except Exception as exc:  # noqa: BLE001
        _progress["status"] = "error"
        mr_state.set_sync_last_error_public(str(exc))
        _persist_progress()
        return dict(_progress)
    finally:
        if conn is not None:
            try:
                await asyncio.wait_for(conn.close(), timeout=5)
            except Exception:  # noqa: BLE001
                pass

    if _progress["status"] == "running":
        all_confirmed = all(
            c["failed"] == 0 and c["skipped"] == 0
            and c["confirmed"] == c["total"]
            for c in _progress["categories"].values()
        )
        _progress["status"] = "done" if all_confirmed else "done_with_errors"
        if all_confirmed:
            # last_sync_at — только когда ВСЕ категории подтверждены.
            mr_state.set_sync_last_sync_at(int(time.time()))
            # ЧАСТЬ 18.6: после успешного bootstrap сразу переходим к
            # инкрементальной синхронизации (если воркер ещё не запущен —
            # запускаем; если запущен и ждал bootstrap — будим).
            try:
                from . import incremental  # noqa: PLC0415

                incremental.start_incremental_sync()
                incremental.wake()
            except Exception:  # noqa: BLE001 — не роняет итог bootstrap
                logger.exception("pdsync: incremental start after bootstrap failed")
    _progress["finished_at"] = int(time.time())
    _persist_progress()
    return dict(_progress)


def start_bootstrap() -> bool:
    """Запустить bootstrap фоновой задачей. False — уже выполняется."""
    global _task
    if is_running():
        return False
    _task = asyncio.create_task(run_bootstrap())
    return True
