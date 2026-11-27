# Retention: безопасное удаление локальных копий (ЧАСТЬ 18.7).
# Co-authored with CoCo
"""Retention-переключатели и безопасное удаление локальных копий (18.7).

ЕДИНСТВЕННЫЙ модуль репозитория, которому разрешено удалять локальные
данные и legacy plaintext-секреты — и только по правилам ТЗ 18.7:

* переключатели «Оставлять локально» НЕ влияют на состав передачи
  (внешняя копия всегда полная) — только на локальную копию;
* выключение переключателя ≠ мгновенное удаление: существующая копия
  удаляется ТОЛЬКО после показа объёма и ОТДЕЛЬНОГО явного подтверждения
  owner'а; без подтверждения переключатель действует «на будущее»
  (новые данные категории не оседают локально после подтверждения sync);
* удалять локальную запись/файл можно ТОЛЬКО когда: операция категории
  ``confirmed`` в едином журнале (read-after-write пройден); для файлов
  дополнительно сверены object key + размер + checksum в S3; проверена
  ВСЯ категория (частично проверенная не удаляется); retention категории
  выключен И owner дал отдельное подтверждение;
* порядок: сначала журнал намерения (``retention_intents``), затем
  удаление, затем снятие намерения — прерывание посреди удаления не
  оставляет неконсистентного state, повторный запуск докатывает
  (:func:`resume_pending_intents`);
* записи реестра исключений 18.1 (``.frozen``, ``vault``, локальный
  журнал) retention-удалению НЕ подлежат: они не синхронизируются, и
  условие «confirmed во внешней БД» для них невыполнимо по построению —
  план (:func:`bootstrap.build_plan`) их не содержит, а прямые вызовы
  дополнительно отфильтровываются здесь;
* хвост 18.4: при purge категории ``secrets`` (миграция проверена
  обратным чтением, категория подтверждена во внешней БД, retention
  выключен, отдельное подтверждение) удаляются plaintext-оригиналы из
  ``state.json``; выполняется дополнительный ``_save()``-цикл, чтобы
  ``state.json.bak`` перестал содержать удалённые секреты, и оба файла
  проверяются grep'ом; plaintext секрет-файлы (``yt_cookies.txt`` и пр.)
  удаляются по тем же условиям.

Операционная целостность (задокументированный guard, НЕ обход условий
ТЗ): ключи ``_settings.owner_id`` и ``_settings.addons.masterroom.sync``
не удаляются retention'ом никогда — первый нужен строгой owner-проверке
и анти-воскрешению (без него бот теряет владельца), второй — сама
конфигурация sync/retention (её удаление откатило бы переключатели).
Внешняя копия обоих ключей при этом полная.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from .. import state as mr_state
from .categories import (
    SyncCategory,
    is_excluded_file,
    is_excluded_state_path,
    is_secret_file,
)

logger = logging.getLogger(__name__)

ENTITY_STATE = "state_key"
ENTITY_FILE = "file"

# Guard операционной целостности (см. docstring). Полные dotted-пути
# и префиксы, которые retention-удаление пропускает всегда.
KEEP_STATE_PREFIXES: tuple[str, ...] = (
    "_settings.owner_id",
    "_settings.addons.masterroom.sync",
)


def _is_kept_state_path(dotted: str) -> bool:
    return any(
        dotted == p or dotted.startswith(p + ".")
        for p in KEEP_STATE_PREFIXES
    )


def _is_frozen() -> bool:
    from .... import selfdestruct  # noqa: PLC0415 — ленивый импорт

    try:
        return selfdestruct.is_frozen()
    except Exception:  # noqa: BLE001
        return True  # сомнение — в пользу kill-switch (18.0 п.9)


def is_retention_kept(category: str) -> bool:
    """True — «Оставлять локально» для категории включён."""
    return bool(mr_state.get_sync_retention().get(str(category), True))


# ─── Объём затрагиваемых данных (18.7 §2, показ перед подтверждением) ────────


async def estimate_category(category: str) -> dict[str, int]:
    """{records, bytes} существующей локальной копии категории.

    Read-only: строится на том же плане 18.5. Guard-ключи операционной
    целостности не учитываются (они не удаляются).
    """
    from .bootstrap import build_plan_async  # noqa: PLC0415

    plan = await build_plan_async()
    records = 0
    total = 0
    for item in plan.items.get(str(category), []):
        if item.entity_type == ENTITY_STATE \
                and _is_kept_state_path(item.entity_id):
            continue
        records += 1
        total += int(item.byte_size)
    return {"records": records, "bytes": total}


# ─── Проверка «вся категория confirmed» (18.7 §3) ────────────────────────────


def _file_object_key(category: str, entity_id: str) -> str:
    """Тот же object key, что у доставки 18.5/18.6 (единый контракт)."""
    secretish = is_secret_file(entity_id) or (
        category == SyncCategory.SECRETS.value
    )
    return (
        f"sync/secrets/{entity_id}.enc" if secretish
        else f"sync/files/{category}/{entity_id}"
    )


async def verify_category_confirmed(
    jr: Any, category: str, conn: Any | None = None,
) -> tuple[bool, str]:
    """Проверить, что ВСЯ категория подтверждена и пригодна к удалению.

    Для каждой сущности локального среза категории:

    * операция в едином журнале имеет статус ``confirmed`` (значит
      read-after-write уже пройден при доставке);
    * локальная копия НЕ изменилась после подтверждения (текущий сиг
      совпадает с ``value_checksum`` в ``sync_local_meta``);
    * для файлов дополнительно сверяются object key + размер + checksum
      во внешней БД (``sync_files``) и размер в S3 (HEAD).

    Возвращает (ok, безопасная причина отказа).
    """
    from .bootstrap import build_plan_async, operation_id_for  # noqa: PLC0415
    from .canonical import checksum_of  # noqa: PLC0415

    plan = await build_plan_async()
    items = plan.items.get(str(category), [])
    checkable = [
        i for i in items
        if not (i.entity_type == ENTITY_STATE
                and _is_kept_state_path(i.entity_id))
    ]
    if not checkable:
        return False, "локальная копия категории пуста — удалять нечего"

    for item in checkable:
        op = await jr.get(operation_id_for(item.entity_type, item.entity_id))
        if op is None or op["status"] != "confirmed":
            return False, (
                "категория проверена не полностью: есть неподтверждённые "
                "записи — сначала завершите синхронизацию"
            )
        meta = await jr.local_meta_get(item.entity_type, item.entity_id)
        if item.entity_type == ENTITY_STATE:
            try:
                sig = await asyncio.to_thread(checksum_of, item.value)
            except (TypeError, ValueError):
                sig = await asyncio.to_thread(checksum_of, str(item.value))
        else:
            sig = f"file:{item.byte_size}"
        if meta is None or meta["value_checksum"] != sig:
            return False, (
                "локальные данные изменились после подтверждения — "
                "дождитесь следующего цикла синхронизации"
            )
        if item.entity_type == ENTITY_FILE:
            ok, reason = await _verify_file_external(
                conn, str(category), item.entity_id, op,
            )
            if not ok:
                return False, reason
    return True, ""


async def _verify_file_external(
    conn: Any | None, category: str, entity_id: str, op: dict[str, Any],
) -> tuple[bool, str]:
    """Файл: сверка object key + размер + checksum в БД и S3 (18.7 §3)."""
    from . import objects  # noqa: PLC0415

    if conn is None:
        return False, "нет подключения к внешней БД для сверки файлов"
    if not objects.is_configured():
        return False, "не настроено S3 (SYNC_S3_*) для сверки файлов"
    object_key = _file_object_key(category, entity_id)
    back = await conn.fetchrow(
        "SELECT object_key, entity_id, byte_size, checksum "
        "FROM masterroom_sync.sync_files WHERE object_key = $1",
        object_key,
    )
    if (
        back is None
        or back["object_key"] != object_key
        or back["entity_id"] != entity_id
        or str(back["checksum"]) != str(op["checksum"])
    ):
        return False, "сверка файла во внешней БД не сошлась"
    head = await objects.head_object(object_key)
    if head is None or int(head["byte_size"]) != int(back["byte_size"]):
        return False, "сверка файла в S3 (object key/размер) не сошлась"
    return True, ""


# ─── Удаление: intent → delete → done (18.7 §3, атомарно докатываемое) ───────


def _delete_dotted(state: dict[str, Any], dotted: str) -> bool:
    """Удалить лист state по dotted-пути. True — удалён."""
    node: Any = state
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    if isinstance(node, dict) and parts[-1] in node:
        del node[parts[-1]]
        return True
    return False


async def _delete_entities(
    jr: Any, category: str, entities: list[dict[str, str]],
) -> dict[str, int]:
    """Удалить локальные копии перечисленных сущностей.

    Порядок для каждой сущности: удаление → запись в ``retention_dropped``.
    Реестр исключений 18.1 и guard-ключи пропускаются безусловно.
    State сохраняется штатным ``storage._save()`` (атомарная запись),
    ДВАЖДЫ — чтобы ``state.json.bak`` не содержал удалённых данных.
    """
    from ....config import DATA_DIR  # noqa: PLC0415
    from ....storage import storage  # noqa: PLC0415

    deleted = {"state": 0, "files": 0, "skipped": 0}
    state_dirty = False
    for ent in entities:
        etype = str(ent.get("entity_type") or "")
        eid = str(ent.get("entity_id") or "")
        if not eid:
            continue
        if etype == ENTITY_STATE:
            if is_excluded_state_path(eid) or _is_kept_state_path(eid):
                deleted["skipped"] += 1
                continue
            if _delete_dotted(storage._state, eid):  # noqa: SLF001
                state_dirty = True
                deleted["state"] += 1
            await jr.retention_dropped_add(ENTITY_STATE, eid, category)
        elif etype == ENTITY_FILE:
            if is_excluded_file(eid):
                deleted["skipped"] += 1
                continue
            abs_path = Path(DATA_DIR) / eid
            try:
                await asyncio.to_thread(abs_path.unlink)
                deleted["files"] += 1
            except FileNotFoundError:
                pass  # повторный запуск докатывает — уже удалён
            except OSError:
                logger.warning(
                    "pdsync retention: unlink failed for a file (kept)"
                )
                deleted["skipped"] += 1
                continue
            await jr.retention_dropped_add(ENTITY_FILE, eid, category)
        await asyncio.sleep(0)

    if state_dirty:
        # Двойной _save(): после первого .bak хранит state ДО удаления;
        # второй перезаписывает .bak уже очищенной версией (18.7 §4).
        await asyncio.to_thread(storage._save)  # noqa: SLF001
        await asyncio.to_thread(storage._save)  # noqa: SLF001
    return deleted


async def purge_category(category: str) -> tuple[bool, str]:
    """Полное удаление существующей локальной копии категории (18.7 §3).

    Вызывается ТОЛЬКО после отдельного явного подтверждения owner'а на
    экране (UI 18.2/18.7). Проверяет все условия и возвращает
    (ok, безопасный текст результата для UI).
    """
    from . import db  # noqa: PLC0415
    from .bootstrap import build_plan_async  # noqa: PLC0415

    category = str(category)
    if _is_frozen():
        return False, "Kill-switch активен — удаление невозможно."
    if is_retention_kept(category):
        return False, (
            "Retention категории включён («Оставлять локально») — "
            "удаление не выполняется."
        )
    if not db.is_configured():
        return False, "Внешняя БД не настроена — подтверждённой копии нет."

    from .journal import get_sync_journal  # noqa: PLC0415

    jr = get_sync_journal()
    conn = None
    try:
        conn = await db.connect()
        ok, reason = await verify_category_confirmed(jr, category, conn)
        if not ok:
            return False, f"Удаление отклонено: {reason}."

        # Хвост 18.4: перед удалением секретов миграция обязана быть
        # проверена обратным чтением (идемпотентно).
        if category == SyncCategory.SECRETS.value:
            from .migrate_secrets import (  # noqa: PLC0415
                migrate_plaintext_secrets_async,
            )

            report = await migrate_plaintext_secrets_async()
            if not report.ok:
                return False, (
                    "Удаление отклонено: миграция секретов не подтверждена "
                    "обратным чтением."
                )

        plan = await build_plan_async()
        entities = [
            {"entity_type": i.entity_type, "entity_id": i.entity_id}
            for i in plan.items.get(category, [])
            if not (i.entity_type == ENTITY_STATE
                    and _is_kept_state_path(i.entity_id))
        ]
        # 1) журнал намерения; 2) удаление; 3) снятие намерения.
        await jr.retention_intent_set(
            category, json.dumps(entities, ensure_ascii=False),
        )
        deleted = await _delete_entities(jr, category, entities)

        leftovers = 0
        if category == SyncCategory.SECRETS.value:
            leftovers = await asyncio.to_thread(_grep_leftover_secrets)

        await jr.retention_intent_clear(category)
        if leftovers:
            return False, (
                "Локальная копия удалена, но проверка нашла остатки "
                "секретов в state-файлах — удаление будет докачено."
            )
        return True, (
            f"Локальная копия удалена: {deleted['state']} записей, "
            f"{deleted['files']} файлов. Внешняя копия цела."
        )
    except Exception as exc:  # noqa: BLE001 — безопасный класс наружу
        try:
            err = db.classify_error(exc)
        except Exception:  # noqa: BLE001
            err = "ошибка удаления"
        logger.warning("pdsync retention purge failed: %s", err)
        return False, f"Удаление не выполнено: {err}."
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()


# ─── Grep-проверка отсутствия plaintext-секретов (18.7 §4) ───────────────────


def _grep_leftover_secrets() -> int:
    """Число известных STATE-секрет-путей, оставшихся в ``state.json``
    или ``state.json.bak``. Значения секретов НЕ логируются и НЕ
    возвращаются — только счётчик."""
    from ....config import DATA_DIR  # noqa: PLC0415
    from .categories import (  # noqa: PLC0415
        _SECRET_STATE_PATTERNS,
        _flatten_state,
        _match_dotted,
    )

    store_prefix = "_settings.addons.masterroom.secret_store"
    leftovers = 0
    for name in ("state.json", "state.json.bak"):
        path = Path(DATA_DIR) / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for dotted, value in _flatten_state(data):
            if dotted == store_prefix or dotted.startswith(store_prefix + "."):
                continue  # ciphertext-бакет — не plaintext
            if not _match_dotted(dotted, _SECRET_STATE_PATTERNS):
                continue
            if isinstance(value, str) and value.strip():
                leftovers += 1
            elif isinstance(value, (dict, list)) and value:
                leftovers += 1
    return leftovers


# ─── «На будущее»: авто-удаление после подтверждения sync (18.7 §2) ──────────


async def maybe_drop_after_confirm(jr: Any, op: dict[str, Any]) -> bool:
    """Вызывается воркером 18.6 ПОСЛЕ ``confirmed`` очередной операции.

    Если retention категории выключен — локальная копия ЭТОЙ сущности
    (уже подтверждённой read-after-write) удаляется: «новые данные
    категории не оседают локально после подтверждения sync». Существующая
    копия категории при этом не трогается (её удаляет только
    :func:`purge_category` после отдельного подтверждения). Fail-open:
    любая ошибка не ломает доставку.
    """
    try:
        category = str(op.get("category") or "")
        if not category or is_retention_kept(category):
            return False
        if str(op.get("checksum") or "") == "tombstone":
            return False  # сущности уже нет локально
        entity = {
            "entity_type": str(op.get("entity_type") or ""),
            "entity_id": str(op.get("entity_id") or ""),
        }
        await _delete_entities(jr, category, [entity])
        return True
    except Exception:  # noqa: BLE001 — fail-open
        logger.exception("pdsync retention: drop-after-confirm failed")
        return False


# ─── Повторное включение retention ───────────────────────────────────────────


async def reenable_category(category: str) -> None:
    """Retention снова включён: очистить реестр удалённых и meta-строки —
    pull снова имеет право восстановить внешние копии категории."""
    from .journal import get_sync_journal  # noqa: PLC0415

    jr = get_sync_journal()
    dropped = await jr.retention_dropped_keys()
    meta_all = await jr.local_meta_all()
    for key in dropped:
        m = meta_all.get(key)
        if m is None or str(m.get("category")) != str(category):
            continue
        await jr.local_meta_delete(*key)
    await jr.retention_dropped_clear_category(str(category))
    # Сбросить курсор pull: следующий цикл идемпотентно перечитает
    # внешние сущности и восстановит недостающие записи категории.
    with contextlib.suppress(Exception):
        await jr.meta_set("pull_cursor", "1970-01-01T00:00:00+00:00")


# ─── Resume: докатить прерванное удаление (18.7 §3) ──────────────────────────


async def resume_pending_intents() -> int:
    """Докатить незавершённые намерения удаления (после рестарта).

    Намерение пишется ТОЛЬКО после полной проверки категории и
    отдельного подтверждения owner'а, поэтому здесь условия не
    перепроверяются — выполняется только само (идемпотентное) удаление.
    Возвращает число докаченных категорий.
    """
    if _is_frozen():
        return 0
    from .journal import get_sync_journal  # noqa: PLC0415

    jr = get_sync_journal()
    try:
        intents = await jr.retention_intents_all()
    except Exception:  # noqa: BLE001
        return 0
    done = 0
    for category, payload in intents.items():
        if is_retention_kept(category):
            # owner успел включить retention обратно — намерение отменяется.
            await jr.retention_intent_clear(category)
            continue
        try:
            entities = json.loads(payload)
            if not isinstance(entities, list):
                entities = []
        except (ValueError, TypeError):
            entities = []
        await _delete_entities(jr, category, entities)
        if category == SyncCategory.SECRETS.value:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_grep_leftover_secrets)
        await jr.retention_intent_clear(category)
        done += 1
        logger.info(
            "pdsync retention: resumed pending deletion for category %s",
            category,
        )
    return done
