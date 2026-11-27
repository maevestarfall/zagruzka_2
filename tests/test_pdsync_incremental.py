# Tests for ЧАСТЬ 18.6: инкрементальная двусторонняя синхронизация.
# Co-authored with CoCo
"""Приёмка 18.6 (без реального PostgreSQL — через FakeConn):

* локальное изменение попадает во внешнюю БД и подтверждается
  (read-after-write); повтор доставки НЕ создаёт дублей (тот же
  ``operation_id``);
* внешнее изменение применяется локально; повторный pull — no-op;
* двусторонний конфликт НЕ перетирается молча: фиксируется карточка,
  локальное значение цело; решение owner'а («локальная») ставит
  сущность в pending с ТЕМ ЖЕ operation_id;
* обрыв сети посреди доставки → failed; «Повторить ошибки» → pending с
  тем же operation_id; после восстановления — confirmed;
* tombstone доставляется и подтверждается как обычная операция;
* заморозка kill-switch останавливает push и pull; wipe (пустой state)
  блокирует pull (анти-воскрешение);
* hook отслеживания изменений строго fail-open: исключение внутри hook'а
  не ломает локальное сохранение;
* pull не пишет в реестр исключений 18.1;
* в дампе локального журнала нет значений секретов.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot.addons.masterroom.sync import bootstrap, incremental
from bot.addons.masterroom.sync.journal import SyncJournal


# ─── FakeConn: имитация asyncpg-подключения к masterroom_sync ────────────────


class FakeConn:
    """Хранит sync_outbox / sync_entities в памяти; поведение SQL из
    incremental.py воспроизводится по подстрокам запросов."""

    def __init__(self, *, fail_on_entities: bool = False) -> None:
        self.outbox: dict[str, dict] = {}
        self.entities: dict[tuple[str, str], dict] = {}
        self.queries = 0
        self.fail_on_entities = fail_on_entities
        self._ts = 0

    def _next_ts(self) -> str:
        self._ts += 1
        return f"2026-07-22T00:00:{self._ts:02d}+00:00"

    async def execute(self, sql: str, *args) -> None:
        self.queries += 1
        if "INSERT INTO masterroom_sync.sync_outbox" in sql:
            op_id, etype, eid, rev, chk = args
            row = self.outbox.get(op_id)
            if row is None:
                self.outbox[op_id] = {
                    "operation_id": op_id, "entity_type": etype,
                    "entity_id": eid, "revision": rev, "checksum": chk,
                    "status": "delivering", "attempt_count": 1,
                }
            else:
                row.update(
                    revision=rev, checksum=chk, status="delivering",
                    attempt_count=row["attempt_count"] + 1,
                )
        elif "INSERT INTO masterroom_sync.sync_entities" in sql:
            if self.fail_on_entities:
                raise RuntimeError("connection reset (симуляция обрыва)")
            cat, etype, eid, rev, chk, payload, tomb = args
            self.entities[(etype, eid)] = {
                "category": cat, "entity_type": etype, "entity_id": eid,
                "revision": rev, "checksum": chk, "payload": payload,
                "tombstone": tomb, "updated_at": self._next_ts(),
            }
        elif "SET status='failed'" in sql:
            if args[0] in self.outbox:
                self.outbox[args[0]]["status"] = "failed"
        elif "SET status='confirmed'" in sql:
            if args[0] in self.outbox:
                self.outbox[args[0]]["status"] = "confirmed"

    async def fetchrow(self, sql: str, *args):
        self.queries += 1
        etype, eid = args
        row = self.entities.get((etype, eid))
        return dict(row) if row is not None else None

    async def fetch(self, sql: str, *args):
        self.queries += 1
        cursor, etype, limit = args
        rows = [
            dict(r) for r in self.entities.values()
            if r["entity_type"] == etype and r["updated_at"] > cursor
        ]
        rows.sort(key=lambda r: r["updated_at"])
        return rows[: int(limit)]


# ─── общая обвязка ───────────────────────────────────────────────────────────


def _state() -> dict:
    return {"articles": {"a1": {"title": "Заголовок", "body": "текст"}}}


@pytest.fixture()
def jr(tmp_path) -> SyncJournal:
    return SyncJournal(tmp_path / "sync_journal.db")


@pytest.fixture(autouse=True)
def _unfrozen(monkeypatch, tmp_path):
    """Тесты по умолчанию: не заморожен, не wipe, не на паузе; план
    строится только из state (пустой DATA_DIR = tmp)."""
    monkeypatch.setattr(incremental, "_is_frozen", lambda: False)
    monkeypatch.setattr(incremental, "_local_state_is_fresh", lambda: False)
    from bot.addons.masterroom import state as mr_state

    monkeypatch.setattr(mr_state, "is_sync_paused", lambda: False)
    orig_build_plan = bootstrap.build_plan
    files_dir = tmp_path / "empty_data"
    files_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        bootstrap, "build_plan",
        lambda state, data_dir=None: orig_build_plan(state, files_dir),
    )


async def _push_cycle(jr: SyncJournal, conn: FakeConn, state: dict) -> dict:
    await incremental.reconcile(jr, state)
    return await incremental.deliver_pending(conn, jr, state)


# ─── 1. push: подтверждение и отсутствие дублей ──────────────────────────────


async def test_local_change_confirmed_no_dupes_on_repeat(jr):
    conn = FakeConn()
    state = _state()
    counters = await _push_cycle(jr, conn, state)
    assert counters["confirmed"] >= 1
    assert counters["failed"] == 0
    # внешняя БД получила сущности; все операции подтверждены
    assert conn.entities
    assert all(r["status"] == "confirmed" for r in conn.outbox.values())
    ops_before = set(conn.outbox)
    counts = await jr.counts_by_status()
    confirmed_before = counts.get("confirmed", 0)

    # повтор без изменений: дублей нет, всё остаётся confirmed
    counters2 = await _push_cycle(jr, conn, state)
    assert counters2["failed"] == 0
    assert set(conn.outbox) == ops_before  # те же operation_id, не больше
    counts2 = await jr.counts_by_status()
    assert counts2.get("confirmed", 0) == confirmed_before
    assert counts2.get("pending", 0) == 0


async def test_local_edit_bumps_revision_same_operation_id(jr):
    conn = FakeConn()
    state = _state()
    await _push_cycle(jr, conn, state)
    meta_before = await jr.local_meta_all()

    state["articles"]["a1"]["title"] = "Новый заголовок"
    await _push_cycle(jr, conn, state)
    meta_after = await jr.local_meta_all()
    changed = [
        k for k in meta_after
        if k in meta_before
        and meta_after[k]["revision"] > meta_before[k]["revision"]
    ]
    assert changed  # ревизия монотонно выросла
    # operation_id стабилен: внешний outbox не разросся
    assert len(conn.outbox) == len(meta_before)


# ─── 2. pull: применение и идемпотентность ───────────────────────────────────


async def _seed_remote_change(
    conn: FakeConn, new_value,
    entity_id: str = "articles.a1.title",
) -> tuple[str, str]:
    """Изменить state-сущность «снаружи» (лист ``entity_id``).
    Возвращает (entity_id, checksum)."""
    key = (incremental.ENTITY_STATE, entity_id)
    row = conn.entities[key]
    payload = json.loads(row["payload"])
    payload["value"] = new_value
    payload["revision"] = int(row["revision"]) + 1
    row["payload"] = json.dumps(payload, ensure_ascii=False)
    row["revision"] = int(row["revision"]) + 1
    row["checksum"] = "remote-" + str(row["revision"])
    row["updated_at"] = conn._next_ts()
    return key[1], row["checksum"]


async def test_remote_change_applied_locally_and_idempotent(jr):
    conn = FakeConn()
    state = _state()
    await _push_cycle(jr, conn, state)
    entity_id, _ = await _seed_remote_change(conn, "Внешняя правка")
    saves: list[int] = []
    res = await incremental.pull_changes(
        conn, jr, state, save_state=lambda: saves.append(1),
    )
    assert res["applied"] == 1
    assert saves  # применено через атомарное сохранение
    exists, value = incremental._get_dotted(state, entity_id)
    assert exists and value == "Внешняя правка"

    # повторный pull того же изменения — no-op
    res2 = await incremental.pull_changes(
        conn, jr, state, save_state=lambda: saves.append(1),
    )
    assert res2["applied"] == 0
    assert len(saves) == 1


async def test_pull_never_touches_exclusion_registry(jr):
    conn = FakeConn()
    state = _state()
    await _push_cycle(jr, conn, state)
    # запись реестра исключений «как-то оказалась» во внешней БД
    conn.entities[(incremental.ENTITY_STATE, "_settings.addons.vault.pin")] = {
        "category": "other",
        "entity_type": incremental.ENTITY_STATE,
        "entity_id": "_settings.addons.vault.pin",
        "revision": 1, "checksum": "evil",
        "payload": json.dumps({"value": "0000"}),
        "tombstone": False, "updated_at": conn._next_ts(),
    }
    await incremental.pull_changes(conn, jr, state, save_state=lambda: None)
    exists, _ = incremental._get_dotted(
        state, "_settings.addons.vault.pin",
    )
    assert not exists  # реестр исключений не тронут


# ─── 3. конфликты ────────────────────────────────────────────────────────────


async def test_conflict_not_silently_overwritten(jr):
    conn = FakeConn()
    state = _state()
    await _push_cycle(jr, conn, state)
    # обе стороны изменили после последней подтверждённой ревизии
    state["articles"]["a1"]["title"] = "Локальная правка"
    entity_id, _ = await _seed_remote_change(conn, "Внешняя правка")
    res = await incremental.pull_changes(
        conn, jr, state, save_state=lambda: None,
    )
    assert res["conflicts"] == 1
    # локальное значение ЦЕЛО — ничего не перетёрто
    _, value = incremental._get_dotted(state, entity_id)
    assert value == "Локальная правка"
    conflicts = await jr.conflicts_list()
    assert len(conflicts) == 1
    assert conflicts[0]["entity_id"] == entity_id

    # до решения: сущность исключена и из push, и из pull
    counters = await incremental.deliver_pending(conn, jr, state)
    assert counters["confirmed"] == 0
    res2 = await incremental.pull_changes(
        conn, jr, state, save_state=lambda: None,
    )
    assert res2["applied"] == 0


async def test_conflict_resolution_local_requeues_same_operation_id(jr):
    conn = FakeConn()
    state = _state()
    await _push_cycle(jr, conn, state)
    state["articles"]["a1"]["title"] = "Локальная правка"
    await _seed_remote_change(conn, "Внешняя правка")
    await incremental.pull_changes(conn, jr, state, save_state=lambda: None)
    (conflict,) = await jr.conflicts_list()

    from bot.addons.masterroom.sync import journal as journal_mod

    orig_get = journal_mod.get_sync_journal
    journal_mod.get_sync_journal = lambda: jr
    try:
        msg = await incremental.resolve_conflict(conflict["id"], "local")
    finally:
        journal_mod.get_sync_journal = orig_get
    assert "локальн" in msg.lower()
    assert await jr.conflict_count() == 0
    pending = await jr.list_pending()
    assert len(pending) == 1
    # ТОТ ЖЕ operation_id, что и раньше (никаких новых записей)
    assert pending[0]["operation_id"] == bootstrap.operation_id_for(
        conflict["entity_type"], conflict["entity_id"],
    )
    # ревизия выше обеих конфликтующих
    assert pending[0]["revision"] > max(
        conflict["local_revision"], conflict["remote_revision"],
    )


# ─── 4. обрыв сети, retry, продолжение ───────────────────────────────────────


async def test_network_break_then_retry_resumes_same_operation(jr):
    bad = FakeConn(fail_on_entities=True)
    state = _state()
    await incremental.reconcile(jr, state)
    counters = await incremental.deliver_pending(bad, jr, state)
    assert counters["failed"] >= 1
    failed = await jr.list_failed()
    assert failed
    op_ids = {f["operation_id"] for f in failed}

    # «Повторить ошибки»: failed → pending с ТЕМ ЖЕ operation_id
    moved = await jr.retry_failed()
    assert moved == len(failed)
    pending = await jr.list_pending()
    assert {p["operation_id"] for p in pending} == op_ids

    # сеть восстановилась → продолжение с последней подтверждённой
    good = FakeConn()
    counters2 = await incremental.deliver_pending(good, jr, state)
    assert counters2["confirmed"] == len(op_ids)
    counts = await jr.counts_by_status()
    assert counts.get("failed", 0) == 0
    assert counts.get("pending", 0) == 0


# ─── 5. tombstone ────────────────────────────────────────────────────────────


async def test_tombstone_delivered_and_confirmed(jr):
    conn = FakeConn()
    state = _state()
    await _push_cycle(jr, conn, state)
    n_ops = len(conn.outbox)

    # сущность исчезла локально → tombstone, НЕ исчезновение из журнала
    del state["articles"]
    queued = await incremental.reconcile(jr, state)
    assert queued >= 1
    counters = await incremental.deliver_pending(conn, jr, state)
    assert counters["confirmed"] >= 1
    assert counters["failed"] == 0
    # дублей нет: те же operation_id
    assert len(conn.outbox) == n_ops
    meta = await jr.local_meta_all()
    tombs = [m for m in meta.values() if m["tombstone"]]
    assert tombs
    # tombstone дошёл до внешней БД
    assert any(r["tombstone"] for r in conn.entities.values())


async def test_pull_tombstone_never_deletes_local_data(jr):
    conn = FakeConn()
    state = _state()
    await _push_cycle(jr, conn, state)
    key = next(k for k in conn.entities if k[0] == incremental.ENTITY_STATE)
    row = conn.entities[key]
    row["tombstone"] = True
    row["checksum"] = "remote-tomb"
    row["updated_at"] = conn._next_ts()
    await incremental.pull_changes(conn, jr, state, save_state=lambda: None)
    exists, _ = incremental._get_dotted(state, key[1])
    assert exists  # локальные данные НЕ удаляются (удаление — 18.7)


# ─── 6. kill-switch: freeze и wipe ───────────────────────────────────────────


async def test_frozen_stops_push_and_pull(jr, monkeypatch):
    monkeypatch.setattr(incremental, "_is_frozen", lambda: True)
    conn = FakeConn()
    state = _state()
    await incremental.reconcile(jr, state)
    counters = await incremental.deliver_pending(conn, jr, state)
    assert counters["confirmed"] == 0
    assert counters["stopped"] == 1
    assert not conn.entities  # ничего не доставлено
    res = await incremental.pull_changes(
        conn, jr, state, save_state=lambda: None,
    )
    assert res["blocked"] == 1
    assert conn.queries == 0  # к внешней БД вообще не ходили


async def test_wipe_blocks_pull_no_auto_restore(jr, monkeypatch):
    monkeypatch.setattr(incremental, "_local_state_is_fresh", lambda: True)
    conn = FakeConn()
    res = await incremental.pull_changes(
        conn, jr, {}, save_state=lambda: None,
    )
    assert res["blocked"] == 1
    assert conn.queries == 0  # никакого автоматического восстановления


async def test_pause_stops_delivery_after_current(jr, monkeypatch):
    from bot.addons.masterroom import state as mr_state

    monkeypatch.setattr(mr_state, "is_sync_paused", lambda: True)
    conn = FakeConn()
    state = _state()
    await incremental.reconcile(jr, state)
    counters = await incremental.deliver_pending(conn, jr, state)
    assert counters["stopped"] == 1
    assert counters["confirmed"] == 0
    assert not conn.entities


# ─── 7. hook: строго fail-open ───────────────────────────────────────────────


def test_change_hook_is_fail_open(monkeypatch):
    from bot.addons.masterroom.sync import db as db_mod
    from bot.storage import storage

    monkeypatch.setattr(db_mod, "is_configured", lambda: True)
    calls: list[int] = []
    monkeypatch.setattr(storage, "_save", lambda: calls.append(1))
    # искусственное исключение внутри hook'а
    monkeypatch.setattr(
        incremental, "wake",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert incremental.install_change_hook()
    try:
        storage._save()  # noqa: SLF001
        # локальное сохранение прошло, исключение hook'а проглочено
        assert calls == [1]
    finally:
        incremental.uninstall_change_hook()


def test_hook_not_installed_without_database_url(monkeypatch):
    from bot.addons.masterroom.sync import db as db_mod

    monkeypatch.setattr(db_mod, "is_configured", lambda: False)
    assert incremental.install_change_hook() is False
    assert incremental.start_incremental_sync() is False


# ─── 8. секреты не попадают в журнал ─────────────────────────────────────────


async def test_journal_dump_contains_no_secret_values(jr, tmp_path):
    secret = "postgres://user:SuPeRsEcReTpW@db.example.com:5432/app"
    await jr.upsert_pending(
        operation_id="op-x", category="other",
        entity_type="state_key", entity_id="articles.a1",
        revision=1, checksum="c1",
    )
    await jr.mark_failed("op-x", f"ошибка подключения: {secret}")
    dump = (tmp_path / "sync_journal.db").read_bytes()
    assert b"SuPeRsEcReTpW" not in dump
    row = await jr.get("op-x")
    assert "SuPeRsEcReTpW" not in str(row["error_public"])
