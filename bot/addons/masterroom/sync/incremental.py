# Incremental bidirectional sync: outbox worker, pull, conflicts (ЧАСТЬ 18.6).
# Co-authored with CoCo
"""Инкрементальная двусторонняя синхронизация (ЧАСТЬ 18.6).

Работает ТОЛЬКО после успешного bootstrap 18.5 (``last_sync_at`` в state
непустой). Локальные данные НЕ удаляются никогда (удаление — 18.7).

Слои:

* **Hook отслеживания изменений** (:func:`install_change_hook`) —
  оборачивает ``storage._save``. Строго fail-open и нерекурсивный
  (18.6 §1 КРИТИЧНО): сначала выполняется оригинальный save, затем hook
  лишь помечает «dirty»-флаг и будит воркер; любое исключение внутри
  hook'а ловится и НЕ прерывает локальное сохранение; hook НИКОГДА не
  вызывает сохранение state сам (нет рекурсии). Без ``DATABASE_URL``
  hook вообще не устанавливается (no-op с нулевой стоимостью), воркер
  не запускается.
* **Reconcile** — сравнение текущего локального среза (read-only план
  18.5) с ``sync_local_meta`` единого журнала: изменённые сущности
  получают монотонно растущую ревизию и запись ``pending`` в ТОМ ЖЕ
  ``sync_outbox`` (второго журнала НЕТ); исчезнувшие — tombstone-запись
  (18.6 §1), НЕ физическое исчезновение из журнала.
* **Push-воркер** — доставляет ``pending`` во внешнюю БД/S3;
  подтверждение ТОЛЬКО read-after-write (правила 18.5/18.0 п.6);
  повтор ``failed`` — с ТЕМ ЖЕ ``operation_id``; экспоненциальный
  backoff с потолком; в начале каждого цикла — ``selfdestruct
  .is_frozen()`` (18.0 п.9).
* **Pull** — периодическое чтение изменений из
  ``masterroom_sync.sync_entities`` по курсору ``updated_at`` и
  идемпотентное применение локально через ``storage._save`` (атомарная
  запись). Pull никогда не пишет в реестр исключений 18.1, НЕ применяет
  tombstone (локальные данные не удаляются) и НЕ восстанавливает данные
  после wipe/freeze kill-switch (анти-воскрешение).
* **Конфликты** — «побеждает последняя ПОДТВЕРЖДЁННАЯ ревизия
  владельца»; неясный конфликт (обе стороны изменили сущность после
  последней подтверждённой ревизии) НЕ перетирается молча: обе версии
  сохраняются в журнале, owner'у показывается карточка на экране 18.2;
  до решения сущность исключена из авто-применения.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from .. import state as mr_state
from .canonical import canonical_json_bytes, checksum_of, payload_checksum
from .categories import (
    SyncCategory,
    category_for_state_path,
    is_excluded_state_path,
)

logger = logging.getLogger(__name__)

ENTITY_STATE = "state_key"
ENTITY_FILE = "file"

_SECRET_STORE_PREFIX = "_settings.addons.masterroom.secret_store"

# Ключ курсора pull в sync_meta единого журнала.
_PULL_CURSOR_KEY = "pull_cursor"

# Тайминги воркера.
POLL_INTERVAL_S = 30.0
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 600.0
PULL_BATCH = 200

# ─── Состояние процесса ──────────────────────────────────────────────────────

_task: asyncio.Task | None = None
_wake = asyncio.Event()
_hook_installed = False
_orig_save: Any = None
# Guard: применение pull локально не должно снова помечать dirty
# (иначе эхо-цикл push→pull→push). НЕ рекурсия — просто подавление.
_applying_pull = False
_dirty = False

# Кэш статуса для экрана 18.2 (render — синхронный, кэш обновляет воркер).
_status: dict[str, Any] = {}


def get_status() -> dict[str, Any]:
    """Снапшот статуса инкрементального sync для экрана (может быть {})."""
    return dict(_status)


def wake() -> None:
    """Разбудить воркер (hook / кнопки экрана)."""
    global _dirty
    _dirty = True
    with contextlib.suppress(Exception):
        _wake.set()


def is_running() -> bool:
    return _task is not None and not _task.done()


# ─── Вспомогательные ─────────────────────────────────────────────────────────


def _is_frozen() -> bool:
    from .... import selfdestruct  # noqa: PLC0415 — ленивый импорт

    try:
        return selfdestruct.is_frozen()
    except Exception:  # noqa: BLE001
        return True  # сомнение — в пользу kill-switch (18.0 п.9)


def _bootstrap_completed() -> bool:
    """18.6 работает только ПОСЛЕ успешного bootstrap 18.5."""
    try:
        return bool(mr_state.get_sync_state().get("last_sync_at"))
    except Exception:  # noqa: BLE001
        return False


def _local_state_is_fresh() -> bool:
    """Анти-воскрешение (18.6 §2): свежий/пустой state (например, после
    wipe kill-switch) → pull НЕ восстанавливает данные автоматически."""
    try:
        from ....storage import storage  # noqa: PLC0415

        return not storage.get_owner_id()
    except Exception:  # noqa: BLE001
        return True


def _get_dotted(state: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    """(exists, value) листа по dotted-пути."""
    node: Any = state
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False, None
    return True, node[parts[-1]]


def _set_dotted(state: dict[str, Any], dotted: str, value: Any) -> None:
    node: Any = state
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _value_sig(value: Any) -> str:
    """Checksum значения (canonical JSON) для детекта локальных изменений."""
    try:
        return checksum_of(value)
    except (TypeError, ValueError):
        return checksum_of(str(value))


def _secrets_available() -> bool:
    try:
        from .secret_store import is_available  # noqa: PLC0415

        return is_available()
    except Exception:  # noqa: BLE001
        return False


def _is_secret_store_path(entity_id: str) -> bool:
    return entity_id == _SECRET_STORE_PREFIX or entity_id.startswith(
        _SECRET_STORE_PREFIX + "."
    )


# ─── Hook отслеживания изменений (18.6 §1, строго fail-open) ────────────────


def install_change_hook() -> bool:
    """Обернуть ``storage._save`` детектором изменений.

    * без ``DATABASE_URL`` — НЕ устанавливается вовсе (no-op, нулевая
      стоимость);
    * оригинальный save выполняется ПЕРВЫМ и безусловно — сломанный
      sync никогда не ломает запись данных бота (fail-open);
    * hook не вызывает сохранение state (нет рекурсии) и не пишет
      никуда — только ставит dirty-флаг и будит воркер.
    """
    global _hook_installed, _orig_save
    if _hook_installed:
        return True
    from . import db  # noqa: PLC0415 — ленивый импорт

    if not db.is_configured():
        return False
    from ....storage import storage  # noqa: PLC0415

    orig = storage._save  # noqa: SLF001

    def _wrapped_save() -> None:
        orig()  # локальное сохранение — всегда, первым, безусловно
        try:
            if not _applying_pull:
                wake()
        except Exception:  # noqa: BLE001 — fail-open: hook не роняет save
            pass

    storage._save = _wrapped_save  # noqa: SLF001 — instance attribute
    _orig_save = orig
    _hook_installed = True
    return True


def uninstall_change_hook() -> None:
    """Снять hook (для тестов)."""
    global _hook_installed, _orig_save
    if not _hook_installed:
        return
    from ....storage import storage  # noqa: PLC0415

    with contextlib.suppress(Exception):
        if _orig_save is not None:
            storage._save = _orig_save  # noqa: SLF001
    _hook_installed = False
    _orig_save = None


# ─── Reconcile: локальные изменения → pending в ЕДИНОМ журнале ──────────────


async def reconcile(jr: Any, state: dict[str, Any] | None = None) -> int:
    """Сравнить локальный срез с ``sync_local_meta`` и поставить
    изменённые сущности в ``pending`` (тот же ``sync_outbox``, 18.6 §1).

    Возвращает число поставленных операций. Ничего не удаляет: пропажа
    сущности локально → tombstone-запись (revision+1), не исчезновение.
    """
    from .bootstrap import build_plan_async, operation_id_for  # noqa: PLC0415

    if state is not None:
        from .bootstrap import build_plan  # noqa: PLC0415

        plan = await asyncio.to_thread(build_plan, state, None)
    else:
        plan = await build_plan_async()
    meta_all = await jr.local_meta_all()
    conflicts = await jr.conflict_entity_keys()

    queued = 0
    seen: set[tuple[str, str]] = set()
    for items in plan.items.values():
        for item in items:
            key = (item.entity_type, item.entity_id)
            seen.add(key)
            if key in conflicts:
                continue  # до решения owner'а — не трогаем (18.6 §3)
            if item.entity_type == ENTITY_STATE:
                sig = await asyncio.to_thread(_value_sig, item.value)
            else:
                sig = f"file:{item.byte_size}"
            m = meta_all.get(key)
            if m is None:
                # Новая сущность ЛИБО первый reconcile после bootstrap:
                # если в журнале уже есть confirmed-запись bootstrap —
                # считаем её базой (revision из журнала), иначе rev=1.
                row = await jr.get(operation_id_for(*key))
                if row is not None and row["status"] == "confirmed":
                    await jr.local_meta_upsert(
                        entity_type=key[0], entity_id=key[1],
                        category=item.category,
                        revision=int(row["revision"]),
                        value_checksum=sig,
                        remote_checksum=str(row["checksum"] or ""),
                    )
                    continue
                revision = 1
            elif m["tombstone"]:
                revision = m["revision"] + 1  # сущность «воскресла» локально
            elif sig != m["value_checksum"]:
                revision = m["revision"] + 1
            else:
                continue  # не изменилась
            await jr.upsert_pending(
                operation_id=operation_id_for(*key),
                category=item.category,
                entity_type=key[0], entity_id=key[1],
                revision=revision, checksum=sig,
            )
            await jr.local_meta_upsert(
                entity_type=key[0], entity_id=key[1],
                category=item.category, revision=revision,
                value_checksum=m["value_checksum"] if m else "",
                remote_checksum=m["remote_checksum"] if m else "",
                tombstone=False,
            )
            queued += 1

    # Tombstones: сущность была в meta, но исчезла из локального среза.
    # 18.7: сущности, локальная копия которых удалена retention'ом,
    # НЕ получают tombstone — их внешняя копия обязана остаться целой.
    dropped = await jr.retention_dropped_keys()
    for key, m in meta_all.items():
        if key in seen or key in conflicts or m["tombstone"]:
            continue
        if key in dropped:
            continue  # удалено retention'ом (18.7) — внешняя копия цела
        revision = m["revision"] + 1
        await jr.upsert_pending(
            operation_id=operation_id_for(*key),
            category=m["category"],
            entity_type=key[0], entity_id=key[1],
            revision=revision, checksum="tombstone",
        )
        await jr.local_meta_upsert(
            entity_type=key[0], entity_id=key[1],
            category=m["category"], revision=revision,
            value_checksum=m["value_checksum"],
            remote_checksum=m["remote_checksum"],
            tombstone=True,
        )
        queued += 1
    return queued


# ─── Push: доставка pending с read-after-write ───────────────────────────────


async def _build_state_payload(
    entity_id: str, value: Any, category: str, revision: int,
) -> dict[str, Any] | None:
    """Payload state-записи. Секреты → ТОЛЬКО ciphertext (18.4)."""
    if category == SyncCategory.SECRETS.value and not _is_secret_store_path(
        entity_id
    ):
        if not _secrets_available():
            return None
        from . import crypto  # noqa: PLC0415

        raw = json.dumps(value, ensure_ascii=False)
        value = await crypto.encrypt_async(raw, aad=entity_id)
    return {
        "category": category,
        "entity_type": ENTITY_STATE,
        "entity_id": entity_id,
        "revision": int(revision),
        "value": value,
    }


async def _deliver_state_op(
    conn: Any, jr: Any, op: dict[str, Any], state: dict[str, Any],
) -> str:
    """Доставить одну state-операцию (upsert или tombstone) и подтвердить
    ТОЛЬКО read-after-write (18.0 п.6). Возвращает confirmed|failed|skipped."""
    entity_id = op["entity_id"]
    revision = int(op["revision"])
    meta = await jr.local_meta_get(ENTITY_STATE, entity_id)
    tombstone = bool(meta and meta["tombstone"])
    op_id = op["operation_id"]

    if tombstone:
        payload: dict[str, Any] | None = {
            "category": op["category"],
            "entity_type": ENTITY_STATE,
            "entity_id": entity_id,
            "revision": revision,
            "deleted": True,
        }
    else:
        exists, value = _get_dotted(state, entity_id)
        if not exists:
            # Значение исчезло между reconcile и доставкой — следующий
            # reconcile поставит tombstone; сейчас честно пропускаем.
            return "skipped"
        payload = await _build_state_payload(
            entity_id, value, op["category"], revision,
        )
        if payload is None:
            await jr.mark_failed(op_id, "секреты недоступны (нет master key)")
            return "failed"

    checksum = await asyncio.to_thread(payload_checksum, payload)
    expected_size = len(
        await asyncio.to_thread(canonical_json_bytes, payload)
    )
    # Финальный checksum в журнале — ТОТ ЖЕ operation_id, не дубль.
    row = await jr.upsert_pending(
        operation_id=op_id, category=op["category"],
        entity_type=ENTITY_STATE, entity_id=entity_id,
        revision=revision, checksum=checksum,
    )
    if row["status"] == "confirmed":
        return "confirmed"
    await jr.mark_delivering(op_id)

    payload_json = json.dumps(payload, ensure_ascii=False)
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
        op_id, ENTITY_STATE, entity_id, revision, checksum,
    )
    await conn.execute(
        "INSERT INTO masterroom_sync.sync_entities "
        "(category, entity_type, entity_id, revision, checksum, payload, "
        " tombstone) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) "
        "ON CONFLICT (entity_type, entity_id) DO UPDATE SET "
        "  category=EXCLUDED.category, revision=EXCLUDED.revision, "
        "  checksum=EXCLUDED.checksum, payload=EXCLUDED.payload, "
        "  tombstone=EXCLUDED.tombstone, updated_at=now()",
        op["category"], ENTITY_STATE, entity_id, revision, checksum,
        payload_json, tombstone,
    )

    # Read-after-write: повторное чтение и сверка ID/revision/size/checksum.
    back = await conn.fetchrow(
        "SELECT entity_id, revision, checksum, payload::text AS payload "
        "FROM masterroom_sync.sync_entities "
        "WHERE entity_type = $1 AND entity_id = $2",
        ENTITY_STATE, entity_id,
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
                back["entity_id"] == entity_id
                and int(back["revision"]) == revision
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
    # База подтверждена: локальный сиг и внешний checksum сущности.
    if tombstone:
        value_sig = meta["value_checksum"] if meta else ""
    else:
        exists, value = _get_dotted(state, entity_id)
        value_sig = await asyncio.to_thread(_value_sig, value) \
            if exists else ""
    await jr.local_meta_upsert(
        entity_type=ENTITY_STATE, entity_id=entity_id,
        category=op["category"], revision=revision,
        value_checksum=value_sig, remote_checksum=checksum,
        tombstone=tombstone,
    )
    return "confirmed"


async def _deliver_file_op(
    conn: Any, jr: Any, op: dict[str, Any],
) -> str:
    """Доставить файл (как в 18.5, но с инкрементальной ревизией)."""
    from . import objects  # noqa: PLC0415
    from ....config import DATA_DIR  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    entity_id = op["entity_id"]
    revision = int(op["revision"])
    op_id = op["operation_id"]
    meta = await jr.local_meta_get(ENTITY_FILE, entity_id)
    tombstone = bool(meta and meta["tombstone"])

    if not objects.is_configured():
        await jr.mark_failed(op_id, "не настроено S3 (SYNC_S3_*) для файлов")
        return "failed"

    from .categories import is_secret_file  # noqa: PLC0415

    data_dir = Path(DATA_DIR)
    abs_path = data_dir / entity_id
    secretish = is_secret_file(entity_id) or (
        op["category"] == SyncCategory.SECRETS.value
    )
    object_key = (
        f"sync/secrets/{entity_id}.enc" if secretish
        else f"sync/files/{op['category']}/{entity_id}"
    )

    if tombstone:
        # Tombstone файла: только метаданные (байты в S3 не трогаем —
        # удаление внешних копий не входит в 18.6).
        checksum = "tombstone"
        byte_size = 0
    else:
        if secretish:
            if not _secrets_available():
                await jr.mark_failed(
                    op_id, "секреты недоступны (нет master key)"
                )
                return "failed"
            from .secret_store import encrypt_file_payload  # noqa: PLC0415

            def _read_and_encrypt() -> bytes:
                return encrypt_file_payload(
                    abs_path.read_bytes(), rel_path=entity_id,
                )

            try:
                ciphertext = await asyncio.to_thread(_read_and_encrypt)
            except OSError:
                await jr.mark_failed(op_id, "не удалось прочитать файл")
                return "failed"
            put = await objects.put_bytes(object_key, ciphertext)
        else:
            try:
                put = await objects.put_file(object_key, str(abs_path))
            except OSError:
                await jr.mark_failed(op_id, "не удалось прочитать файл")
                return "failed"
        checksum = put["checksum"]
        byte_size = int(put["byte_size"])

    row = await jr.upsert_pending(
        operation_id=op_id, category=op["category"],
        entity_type=ENTITY_FILE, entity_id=entity_id,
        revision=revision, checksum=checksum,
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
        op_id, ENTITY_FILE, entity_id, revision, checksum,
    )
    await conn.execute(
        "INSERT INTO masterroom_sync.sync_files "
        "(object_key, category, entity_type, entity_id, byte_size, "
        " checksum, tombstone) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (object_key) DO UPDATE SET "
        "  category=EXCLUDED.category, entity_id=EXCLUDED.entity_id, "
        "  byte_size=EXCLUDED.byte_size, checksum=EXCLUDED.checksum, "
        "  tombstone=EXCLUDED.tombstone, updated_at=now()",
        object_key, op["category"], ENTITY_FILE, entity_id,
        byte_size, checksum, tombstone,
    )

    # Read-after-write: S3 head (кроме tombstone) + чтение метаданных.
    ok = True
    if not tombstone:
        head = await objects.head_object(object_key)
        ok = head is not None and int(head["byte_size"]) == byte_size
    back = await conn.fetchrow(
        "SELECT object_key, entity_id, byte_size, checksum "
        "FROM masterroom_sync.sync_files WHERE object_key = $1",
        object_key,
    )
    ok = (
        ok
        and back is not None
        and back["object_key"] == object_key
        and back["entity_id"] == entity_id
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
    try:
        size = abs_path.stat().st_size if not tombstone else 0
    except OSError:
        size = 0
    await jr.local_meta_upsert(
        entity_type=ENTITY_FILE, entity_id=entity_id,
        category=op["category"], revision=revision,
        value_checksum=f"file:{size}", remote_checksum=checksum,
        tombstone=tombstone,
    )
    return "confirmed"


async def deliver_pending(
    conn: Any, jr: Any, state: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Доставить все pending-операции. Проверки kill-switch и паузы —
    ПЕРЕД КАЖДОЙ операцией (18.6 §1/§4). Возвращает счётчики."""
    if state is None:
        from ....storage import storage  # noqa: PLC0415

        state = storage._state  # noqa: SLF001 — read-only

    counters = {"confirmed": 0, "failed": 0, "skipped": 0, "stopped": 0}
    conflicts = await jr.conflict_entity_keys()
    for op in await jr.list_pending():
        if _is_frozen():
            counters["stopped"] += 1
            break
        if mr_state.is_sync_paused():
            counters["stopped"] += 1
            break
        if (op["entity_type"], op["entity_id"]) in conflicts:
            continue  # конфликт — не пушим до решения owner'а
        try:
            if op["entity_type"] == ENTITY_STATE:
                res = await _deliver_state_op(conn, jr, op, state)
            else:
                res = await _deliver_file_op(conn, jr, op)
        except Exception as exc:  # noqa: BLE001
            res = "failed"
            with contextlib.suppress(Exception):
                await jr.mark_failed(op["operation_id"], str(exc))
        if res == "confirmed":
            # 18.7 §2 «на будущее»: при выключенном retention категории
            # локальная копия ТОЛЬКО ЧТО подтверждённой сущности удаляется.
            # Fail-open: ошибка удаления не ломает доставку.
            from . import retention  # noqa: PLC0415 — ленивый импорт

            await retention.maybe_drop_after_confirm(jr, op)
        counters[res if res in counters else "failed"] += 1
        await asyncio.sleep(0)  # бот продолжает отвечать
    return counters


# ─── Pull: внешние изменения → бот (18.6 §2) ─────────────────────────────────


async def _decode_remote_value(
    entity_id: str, category: str, payload: dict[str, Any],
) -> tuple[bool, Any]:
    """(ok, value) из внешнего payload'а; секреты расшифровываются."""
    value = payload.get("value")
    if category == SyncCategory.SECRETS.value and not _is_secret_store_path(
        entity_id
    ):
        if not _secrets_available():
            return False, None
        try:
            from . import crypto  # noqa: PLC0415

            raw = await crypto.decrypt_async(value, aad=entity_id)
            return True, json.loads(raw)
        except Exception:  # noqa: BLE001 — не логируем значения
            logger.warning("pdsync pull: secret decrypt failed (no values)")
            return False, None
    return True, value


async def pull_changes(
    conn: Any, jr: Any, state: dict[str, Any] | None = None,
    *,
    save_state: Any = None,
) -> dict[str, int]:
    """Периодический pull по курсору ``updated_at`` (18.6 §2).

    Идемпотентно: повторный pull того же изменения — no-op (сверка
    checksum'ов). НИКОГДА: не пишет в реестр исключений 18.1, не
    применяет tombstone (локальные данные не удаляются до 18.7), не
    восстанавливает данные после wipe/freeze (анти-воскрешение).
    """
    counters = {
        "applied": 0, "skipped": 0, "conflicts": 0, "blocked": 0,
    }
    # Анти-воскрешение и kill-switch: до любых чтений из внешней БД.
    if _is_frozen():
        counters["blocked"] += 1
        return counters
    if _local_state_is_fresh():
        # wipe → pull не восстанавливает автоматически; восстановление —
        # только явным действием owner'а (18.6 §2).
        counters["blocked"] += 1
        return counters

    if state is None:
        from ....storage import storage  # noqa: PLC0415

        state = storage._state  # noqa: SLF001

        def _default_save() -> None:
            storage._save()  # noqa: SLF001

        if save_state is None:
            save_state = _default_save
    if save_state is None:
        def save_state() -> None:  # type: ignore[misc]
            return None

    cursor = await jr.meta_get(_PULL_CURSOR_KEY) or "1970-01-01T00:00:00+00:00"
    rows = await conn.fetch(
        "SELECT category, entity_type, entity_id, revision, checksum, "
        "payload::text AS payload, tombstone, updated_at::text AS updated_at "
        "FROM masterroom_sync.sync_entities "
        "WHERE updated_at > $1::timestamptz AND entity_type = $2 "
        "ORDER BY updated_at ASC LIMIT $3",
        cursor, ENTITY_STATE, PULL_BATCH,
    )
    conflicts = await jr.conflict_entity_keys()
    dropped = await jr.retention_dropped_keys()
    new_cursor = cursor
    dirty_state = False
    global _applying_pull
    for row in rows:
        new_cursor = str(row["updated_at"])
        entity_id = str(row["entity_id"])
        category = str(row["category"])
        # Реестр исключений 18.1: pull никогда не пишет в эти записи,
        # даже если они как-то оказались во внешней БД.
        if is_excluded_state_path(entity_id) \
                or category_for_state_path(entity_id) is None:
            counters["skipped"] += 1
            continue
        if (ENTITY_STATE, entity_id) in conflicts:
            counters["skipped"] += 1
            continue
        if (ENTITY_STATE, entity_id) in dropped:
            # 18.7: локальная копия удалена retention'ом — pull НЕ
            # восстанавливает её, пока retention категории выключен
            # (реестр очищается только в retention.reenable_category).
            counters["skipped"] += 1
            continue
        meta = await jr.local_meta_get(ENTITY_STATE, entity_id)
        remote_checksum = str(row["checksum"])
        if meta and meta["remote_checksum"] == remote_checksum:
            counters["skipped"] += 1  # эхо собственного push / повтор
            continue
        if bool(row["tombstone"]):
            # Локальные данные НЕ удаляются (18.6 «Запреты») — фиксируем
            # внешний tombstone только в meta, локальный лист цел.
            if meta:
                await jr.local_meta_upsert(
                    entity_type=ENTITY_STATE, entity_id=entity_id,
                    category=category,
                    revision=max(int(meta["revision"]),
                                 int(row["revision"])),
                    value_checksum=meta["value_checksum"],
                    remote_checksum=remote_checksum,
                    tombstone=meta["tombstone"],
                )
            counters["skipped"] += 1
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            counters["skipped"] += 1
            continue
        ok, remote_value = await _decode_remote_value(
            entity_id, category, payload,
        )
        if not ok:
            counters["skipped"] += 1
            continue
        remote_sig = await asyncio.to_thread(_value_sig, remote_value)
        exists, local_value = _get_dotted(state, entity_id)
        local_sig = await asyncio.to_thread(_value_sig, local_value) \
            if exists else ""
        base_sig = meta["value_checksum"] if meta else ""

        if exists and local_sig == remote_sig:
            # Уже совпадает — идемпотентный no-op, только meta.
            await jr.local_meta_upsert(
                entity_type=ENTITY_STATE, entity_id=entity_id,
                category=category, revision=int(row["revision"]),
                value_checksum=local_sig, remote_checksum=remote_checksum,
            )
            counters["skipped"] += 1
            continue
        if exists and local_sig != base_sig:
            # Обе стороны изменили после последней подтверждённой
            # ревизии — неясный конфликт: НЕ перетирать молча (18.6 §3).
            secret_cat = category == SyncCategory.SECRETS.value
            await jr.conflict_add(
                entity_type=ENTITY_STATE, entity_id=entity_id,
                category=category,
                local_revision=int(meta["revision"]) if meta else 0,
                remote_revision=int(row["revision"]),
                local_checksum=local_sig, remote_checksum=remote_sig,
                local_value=None if secret_cat else json.dumps(
                    local_value, ensure_ascii=False,
                ),
                remote_value=None if secret_cat else json.dumps(
                    remote_value, ensure_ascii=False,
                ),
            )
            counters["conflicts"] += 1
            continue

        # Применяем локально через стандартный механизм (атомарная
        # запись); guard подавляет dirty-эхо hook'а.
        _set_dotted(state, entity_id, remote_value)
        dirty_state = True
        await jr.local_meta_upsert(
            entity_type=ENTITY_STATE, entity_id=entity_id,
            category=category, revision=int(row["revision"]),
            value_checksum=remote_sig, remote_checksum=remote_checksum,
        )
        counters["applied"] += 1
        await asyncio.sleep(0)

    if dirty_state:
        _applying_pull = True
        try:
            await asyncio.to_thread(save_state)
        finally:
            _applying_pull = False
    if new_cursor != cursor:
        await jr.meta_set(_PULL_CURSOR_KEY, new_cursor)
    return counters


# ─── Конфликты: решение owner'а (18.6 §3) ────────────────────────────────────


async def resolve_conflict(conflict_id: int, choice: str) -> str:
    """Применить выбор owner'а: ``local`` | ``remote``.

    * ``local`` — локальная версия побеждает: ревизия повышается и
      сущность ставится в pending (push доставит её в обе стороны);
    * ``remote`` — внешняя версия применяется локально (для секретов —
      повторное чтение из внешней БД с расшифровкой).

    Возвращает безопасный текст результата для UI.
    """
    from .bootstrap import operation_id_for  # noqa: PLC0415
    from .journal import get_sync_journal  # noqa: PLC0415

    jr = get_sync_journal()
    c = await jr.conflict_get(int(conflict_id))
    if c is None:
        return "Конфликт не найден (уже решён?)"
    if choice not in ("local", "remote"):
        return "Неизвестный выбор"

    if choice == "local":
        revision = max(c["local_revision"], c["remote_revision"]) + 1
        await jr.upsert_pending(
            operation_id=operation_id_for(c["entity_type"], c["entity_id"]),
            category=c["category"],
            entity_type=c["entity_type"], entity_id=c["entity_id"],
            revision=revision, checksum=c["local_checksum"],
        )
        await jr.local_meta_upsert(
            entity_type=c["entity_type"], entity_id=c["entity_id"],
            category=c["category"], revision=revision,
            value_checksum="", remote_checksum="",
        )
        await jr.conflict_delete(c["id"])
        wake()
        return "Выбрана локальная версия — будет передана во внешнюю БД."

    # remote
    if c["remote_value"] is None:
        # Секреты: значения в журнале не хранятся — читаем из внешней БД.
        from . import db  # noqa: PLC0415

        if not db.is_configured():
            return "Внешняя БД недоступна — попробуйте позже."
        conn = await db.connect()
        try:
            row = await conn.fetchrow(
                "SELECT payload::text AS payload, revision, checksum "
                "FROM masterroom_sync.sync_entities "
                "WHERE entity_type = $1 AND entity_id = $2",
                c["entity_type"], c["entity_id"],
            )
        finally:
            with contextlib.suppress(Exception):
                await conn.close()
        if row is None:
            return "Внешняя запись не найдена."
        payload = json.loads(row["payload"] or "{}")
        ok, remote_value = await _decode_remote_value(
            c["entity_id"], c["category"], payload,
        )
        if not ok:
            return "Не удалось расшифровать внешнюю версию (нет ключа)."
        remote_revision = int(row["revision"])
        remote_checksum = str(row["checksum"])
    else:
        remote_value = json.loads(c["remote_value"])
        remote_revision = c["remote_revision"]
        remote_checksum = ""

    from ....storage import storage  # noqa: PLC0415

    global _applying_pull
    _set_dotted(storage._state, c["entity_id"], remote_value)  # noqa: SLF001
    _applying_pull = True
    try:
        await asyncio.to_thread(storage._save)  # noqa: SLF001
    finally:
        _applying_pull = False
    remote_sig = await asyncio.to_thread(_value_sig, remote_value)
    await jr.local_meta_upsert(
        entity_type=c["entity_type"], entity_id=c["entity_id"],
        category=c["category"], revision=remote_revision,
        value_checksum=remote_sig, remote_checksum=remote_checksum,
    )
    await jr.conflict_delete(c["id"])
    return "Применена внешняя версия."


# ─── Воркер (18.6 §1/§5) ─────────────────────────────────────────────────────


async def _update_status_cache(jr: Any, extra: dict[str, Any]) -> None:
    global _status
    try:
        counts = await jr.counts_by_status()
        conflicts = await jr.conflict_count()
        _status = {
            "pending": int(counts.get("pending", 0))
            + int(counts.get("delivering", 0)),
            "failed": int(counts.get("failed", 0)),
            "confirmed": int(counts.get("confirmed", 0)),
            "conflicts": conflicts,
            "last_cycle_at": int(time.time()),
            **extra,
        }
    except Exception:  # noqa: BLE001 — кэш не роняет воркер
        logger.exception("pdsync incremental: status cache failed")


async def worker_loop() -> None:
    """Фоновый воркер инкрементальной синхронизации.

    Правила: kill-switch проверяется в начале КАЖДОГО цикла (18.0 п.9);
    пауза останавливает после текущей операции; после обрыва —
    продолжение с последней подтверждённой операции (журнал);
    экспоненциальный backoff с потолком; локальный режим продолжает
    работать при любой недоступности внешней БД.
    """
    from . import db  # noqa: PLC0415
    from .journal import get_sync_journal  # noqa: PLC0415

    if not db.is_configured():
        return  # без DATABASE_URL воркер вообще не работает (18.6 §1)

    jr = get_sync_journal()
    # 18.7 §3: докатить прерванное retention-удаление (журнал намерения
    # был записан ДО удаления — повторный запуск завершает его).
    with contextlib.suppress(Exception):
        from . import retention  # noqa: PLC0415 — ленивый импорт

        await retention.resume_pending_intents()
    backoff = 0.0
    global _dirty
    while True:
        # kill-switch — приоритет над sync: немедленная остановка.
        if _is_frozen():
            _status.update({"state": "frozen"})
            logger.warning("pdsync incremental: frozen — worker stopped")
            return
        if mr_state.is_sync_paused():
            _status.update({"state": "paused"})
            await _wait_wake(POLL_INTERVAL_S)
            continue
        if not _bootstrap_completed():
            _status.update({"state": "waiting_bootstrap"})
            await _wait_wake(POLL_INTERVAL_S)
            continue
        from . import bootstrap as _bs  # noqa: PLC0415

        if _bs.is_running():
            await _wait_wake(POLL_INTERVAL_S)
            continue

        _dirty = False
        conn = None
        try:
            await reconcile(jr)
            conn = await db.connect()
            if conn is None:
                raise RuntimeError("нет подключения к внешней БД")
            pushed = await deliver_pending(conn, jr)
            pulled = await pull_changes(conn, jr)
            backoff = 0.0
            mr_state.set_sync_last_error_public("")
            await _update_status_cache(jr, {
                "state": "ok", "pushed": pushed, "pulled": pulled,
            })
        except Exception as exc:  # noqa: BLE001
            backoff = min(backoff * 2 if backoff else BACKOFF_BASE_S,
                          BACKOFF_CAP_S)
            try:
                err = db.classify_error(exc)
            except Exception:  # noqa: BLE001
                err = "ошибка синхронизации"
            mr_state.set_sync_last_error_public(err)
            await _update_status_cache(jr, {
                "state": "error", "backoff_s": backoff,
            })
            logger.warning("pdsync incremental cycle failed: %s", err)
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(conn.close(), timeout=5)
        await _wait_wake(backoff or POLL_INTERVAL_S)


async def _wait_wake(timeout: float) -> None:
    _wake.clear()
    if _dirty:
        return  # изменения пришли во время цикла — не спим
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_wake.wait(), timeout=timeout)


def start_incremental_sync() -> bool:
    """Установить hook и запустить воркер. False — не настроено/уже идёт.

    Без ``DATABASE_URL`` — полный no-op: hook не ставится, задача не
    создаётся, стоимость нулевая (18.6 §1).
    """
    global _task
    from . import db  # noqa: PLC0415

    if not db.is_configured():
        return False
    if _is_frozen():
        return False
    install_change_hook()
    if is_running():
        return False
    _task = asyncio.create_task(worker_loop(), name="pdsync-incremental")
    return True
