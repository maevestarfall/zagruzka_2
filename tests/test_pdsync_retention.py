# Tests for ЧАСТЬ 18.7: retention-переключатели и безопасное удаление.
# Co-authored with CoCo
"""Приёмка 18.7 (без реального PostgreSQL/S3 — через фейки):

* выключение retention НЕ удаляет существующую копию — только «на
  будущее» (:func:`maybe_drop_after_confirm` удаляет ТОЛЬКО ЧТО
  подтверждённую сущность, остальное цело);
* перед удалением показывается объём (records + bytes) —
  :func:`estimate_category`;
* purge отклоняется: при включённом retention, при kill-switch, без
  внешней БД, при не полностью подтверждённой категории, при локальном
  изменении после подтверждения, для файлов — при расхождении в S3;
* успешный purge удаляет ТОЛЬКО локальную копию (внешняя цела),
  реестр исключений 18.1 и guard-ключи не трогаются;
* хвост 18.4: purge категории secrets удаляет plaintext из state.json
  И из state.json.bak (двойной _save) — grep обоих файлов чист; при
  неподтверждённой миграции purge отклоняется;
* журнал намерения: прерванное удаление докатывается
  (:func:`resume_pending_intents`); включённый обратно retention
  отменяет намерение;
* повторное включение retention чистит реестр удалённых и сбрасывает
  pull-курсор (pull имеет право восстановить внешние копии).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bot.addons.masterroom.sync import bootstrap, retention
from bot.addons.masterroom.sync import db as db_mod
from bot.addons.masterroom.sync import journal as journal_mod
from bot.addons.masterroom.sync import migrate_secrets as mig_mod
from bot.addons.masterroom.sync import objects as objects_mod
from bot.addons.masterroom.sync.canonical import checksum_of
from bot.addons.masterroom.sync.categories import SyncCategory
from bot.addons.masterroom.sync.journal import SyncJournal

ENTITY_STATE = retention.ENTITY_STATE
ENTITY_FILE = retention.ENTITY_FILE

SECRET_VALUE = "sk-PLAINTEXT-SECRET-123"


# ─── FakeConn: только то, что нужно retention (sync_files) ───────────────────


class FakeConn:
    """Внешняя БД для сверки файлов; фиксирует, что retention НИЧЕГО
    не пишет во внешнее назначение (только SELECT)."""

    def __init__(self) -> None:
        self.sync_files: dict[str, dict] = {}
        self.writes = 0

    async def fetchrow(self, sql: str, *args):
        assert "SELECT" in sql  # retention внешнюю БД не изменяет
        if "sync_files" in sql:
            row = self.sync_files.get(args[0])
            return dict(row) if row is not None else None
        return None

    async def execute(self, sql: str, *args) -> None:  # pragma: no cover
        self.writes += 1
        raise AssertionError("retention не должен писать во внешнюю БД")

    async def close(self) -> None:
        pass


# ─── обвязка ─────────────────────────────────────────────────────────────────


def _state() -> dict:
    return {
        "_settings": {
            "owner_id": 42,                      # accounts + guard-ключ
            "co_owners": [777],                  # accounts (удаляемый)
            "keys": {"openrouter": SECRET_VALUE},  # secrets (plaintext)
            "addons": {
                "promptbox": {"p1": "статья один", "p2": "статья два"},
                "vault": {"pin": "0000"},        # реестр исключений 18.1
                "masterroom": {"sync": {"paused": False}},  # guard-ключ
            },
        },
    }


@pytest.fixture()
def jr(tmp_path) -> SyncJournal:
    return SyncJournal(tmp_path / "journal" / "sync_journal.db")


@pytest.fixture()
def env(monkeypatch, tmp_path, jr):
    """Изолированное окружение 18.7: state-дерево + DATA_DIR в tmp,
    retention-настройки в обычном dict, журнал в tmp."""
    state = _state()
    files_dir = tmp_path / "data"
    files_dir.mkdir()
    (files_dir / "media").mkdir()
    (files_dir / "media" / "pic.bin").write_bytes(b"\x07" * 10)
    (files_dir / "yt_cookies.txt").write_text("cookie-plaintext")
    (files_dir / ".frozen").write_text("")  # реестр исключений — не в плане

    ret: dict[str, bool] = {c.value: True for c in SyncCategory}

    orig_build_plan = bootstrap.build_plan
    monkeypatch.setattr(
        bootstrap, "build_plan",
        lambda s=None, d=None: orig_build_plan(state, files_dir),
    )
    monkeypatch.setattr(retention, "_is_frozen", lambda: False)
    monkeypatch.setattr(
        retention.mr_state, "get_sync_retention", lambda: dict(ret),
    )
    monkeypatch.setattr(journal_mod, "get_sync_journal", lambda: jr)
    monkeypatch.setattr(db_mod, "is_configured", lambda: True)

    conn = FakeConn()

    async def _connect() -> FakeConn:
        return conn

    monkeypatch.setattr(db_mod, "connect", _connect)

    import bot.config as bot_config

    monkeypatch.setattr(bot_config, "DATA_DIR", str(files_dir))

    from bot.storage import storage

    monkeypatch.setattr(storage, "_state", state)
    saves: list[int] = []

    def _fake_save() -> None:
        """Мимикрия bot/storage.py: state.json → .bak, затем новая версия."""
        sf = files_dir / "state.json"
        if sf.exists():
            (files_dir / "state.json.bak").write_text(
                sf.read_text(), encoding="utf-8",
            )
        sf.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8",
        )
        saves.append(1)

    monkeypatch.setattr(storage, "_save", _fake_save)

    async def _mig_ok():
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(mig_mod, "migrate_plaintext_secrets_async", _mig_ok)
    monkeypatch.setattr(objects_mod, "is_configured", lambda: True)

    s3: dict[str, dict] = {}

    async def _head(key: str):
        return s3.get(key)

    monkeypatch.setattr(objects_mod, "head_object", _head)

    return SimpleNamespace(
        state=state, files_dir=files_dir, ret=ret, conn=conn, s3=s3,
        saves=saves,
    )


async def _plan_items(category: str) -> list:
    plan = await bootstrap.build_plan_async()
    return plan.items.get(category, [])


async def _confirm_category(jr: SyncJournal, env, category: str) -> None:
    """Сымитировать доставку 18.5/18.6: все сущности категории confirmed
    (read-after-write пройден) + актуальные meta-строки; для файлов —
    строки в sync_files и объекты в S3."""
    for item in await _plan_items(category):
        if item.entity_type == ENTITY_STATE:
            sig = checksum_of(item.value)
        else:
            sig = f"file:{item.byte_size}"
        op_id = bootstrap.operation_id_for(item.entity_type, item.entity_id)
        await jr.upsert_pending(
            operation_id=op_id, category=category,
            entity_type=item.entity_type, entity_id=item.entity_id,
            revision=1, checksum=sig,
        )
        await jr.mark_confirmed(op_id)
        await jr.local_meta_upsert(
            entity_type=item.entity_type, entity_id=item.entity_id,
            category=category, revision=1, value_checksum=sig,
        )
        if item.entity_type == ENTITY_FILE:
            key = retention._file_object_key(category, item.entity_id)
            env.conn.sync_files[key] = {
                "object_key": key, "entity_id": item.entity_id,
                "byte_size": item.byte_size, "checksum": sig,
            }
            env.s3[key] = {"byte_size": item.byte_size}


def _dotted_exists(state: dict, dotted: str) -> bool:
    node = state
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


# ─── 1. выключение ≠ удаление; «на будущее» ──────────────────────────────────


async def test_toggle_off_does_not_delete_existing_copy(jr, env):
    env.ret[SyncCategory.ARTICLES.value] = False  # выключили переключатель
    # существующая копия цела: ничего не удалено без отдельного purge
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p1")
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p2")
    assert await jr.retention_dropped_keys() == set()


async def test_maybe_drop_after_confirm_future_only(jr, env):
    env.ret[SyncCategory.ARTICLES.value] = False
    op = {
        "category": SyncCategory.ARTICLES.value,
        "entity_type": ENTITY_STATE,
        "entity_id": "_settings.addons.promptbox.p1",
        "checksum": "c1",
    }
    assert await retention.maybe_drop_after_confirm(jr, op) is True
    # удалена ТОЛЬКО подтверждённая сущность, остальная копия цела
    assert not _dotted_exists(env.state, "_settings.addons.promptbox.p1")
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p2")
    # tombstone — нечего удалять
    op2 = dict(op, entity_id="_settings.addons.promptbox.p2",
               checksum="tombstone")
    assert await retention.maybe_drop_after_confirm(jr, op2) is False
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p2")


async def test_maybe_drop_noop_when_retention_kept(jr, env):
    op = {
        "category": SyncCategory.ARTICLES.value,
        "entity_type": ENTITY_STATE,
        "entity_id": "_settings.addons.promptbox.p1",
        "checksum": "c1",
    }
    assert await retention.maybe_drop_after_confirm(jr, op) is False
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p1")


# ─── 2. объём перед подтверждением ───────────────────────────────────────────


async def test_estimate_category_records_and_bytes(env):
    vol = await retention.estimate_category(SyncCategory.ARTICLES.value)
    assert vol["records"] == 2  # promptbox.p1 + promptbox.p2
    assert vol["bytes"] > 0
    # guard-ключи не учитываются: они не удаляются
    vol_acc = await retention.estimate_category(SyncCategory.ACCOUNTS.value)
    ids = [i.entity_id for i in await _plan_items(SyncCategory.ACCOUNTS.value)]
    assert "_settings.owner_id" in ids  # в плане передачи есть (внешняя копия)
    assert vol_acc["records"] == len(ids) - 1  # а в объёме удаления — нет


# ─── 3. отказы purge ─────────────────────────────────────────────────────────


async def test_purge_refused_when_retention_kept(jr, env):
    ok, msg = await retention.purge_category(SyncCategory.ARTICLES.value)
    assert not ok
    assert "включён" in msg
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p1")


async def test_purge_refused_when_frozen(jr, env, monkeypatch):
    monkeypatch.setattr(retention, "_is_frozen", lambda: True)
    env.ret[SyncCategory.ARTICLES.value] = False
    ok, msg = await retention.purge_category(SyncCategory.ARTICLES.value)
    assert not ok
    assert "Kill-switch" in msg


async def test_purge_refused_without_external_db(jr, env, monkeypatch):
    monkeypatch.setattr(db_mod, "is_configured", lambda: False)
    env.ret[SyncCategory.ARTICLES.value] = False
    ok, _ = await retention.purge_category(SyncCategory.ARTICLES.value)
    assert not ok
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p1")


async def test_purge_refused_when_category_not_fully_confirmed(jr, env):
    env.ret[SyncCategory.ARTICLES.value] = False
    # подтверждена только p1 — p2 не имеет confirmed-операции
    items = await _plan_items(SyncCategory.ARTICLES.value)
    item = next(i for i in items if i.entity_id.endswith("p1"))
    sig = checksum_of(item.value)
    op_id = bootstrap.operation_id_for(ENTITY_STATE, item.entity_id)
    await jr.upsert_pending(
        operation_id=op_id, category=SyncCategory.ARTICLES.value,
        entity_type=ENTITY_STATE, entity_id=item.entity_id,
        revision=1, checksum=sig,
    )
    await jr.mark_confirmed(op_id)
    await jr.local_meta_upsert(
        entity_type=ENTITY_STATE, entity_id=item.entity_id,
        category=SyncCategory.ARTICLES.value, revision=1, value_checksum=sig,
    )
    ok, msg = await retention.purge_category(SyncCategory.ARTICLES.value)
    assert not ok
    # частично проверенная категория НЕ удаляется — даже подтверждённая часть
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p1")
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p2")


async def test_purge_refused_when_local_changed_after_confirm(jr, env):
    await _confirm_category(jr, env, SyncCategory.ARTICLES.value)
    env.ret[SyncCategory.ARTICLES.value] = False
    # локальная правка ПОСЛЕ подтверждения — checksum разошёлся
    env.state["_settings"]["addons"]["promptbox"]["p1"] = "новая правка"
    ok, msg = await retention.purge_category(SyncCategory.ARTICLES.value)
    assert not ok
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p1")


async def test_purge_files_refused_on_s3_mismatch(jr, env):
    # категория файла media/pic.bin — берём из плана
    plan = await bootstrap.build_plan_async()
    cat = next(
        c for c, items in plan.items.items()
        if any(i.entity_type == ENTITY_FILE
               and i.entity_id == "media/pic.bin" for i in items)
    )
    await _confirm_category(jr, env, cat)
    env.ret[cat] = False
    # S3-объект «усох» — размер не совпадает
    key = retention._file_object_key(cat, "media/pic.bin")
    env.s3[key] = {"byte_size": 1}
    ok, _ = await retention.purge_category(cat)
    assert not ok
    assert (env.files_dir / "media" / "pic.bin").exists()


# ─── 4. успешный purge ───────────────────────────────────────────────────────


async def test_purge_deletes_local_only_external_intact(jr, env):
    await _confirm_category(jr, env, SyncCategory.ARTICLES.value)
    env.ret[SyncCategory.ARTICLES.value] = False
    ok, msg = await retention.purge_category(SyncCategory.ARTICLES.value)
    assert ok, msg
    assert not _dotted_exists(env.state, "_settings.addons.promptbox.p1")
    assert not _dotted_exists(env.state, "_settings.addons.promptbox.p2")
    # внешняя БД не тронута ни одной записью
    assert env.conn.writes == 0
    # реестр удалённых заполнен, намерение снято
    dropped = await jr.retention_dropped_keys()
    assert (ENTITY_STATE, "_settings.addons.promptbox.p1") in dropped
    assert await jr.retention_intents_all() == {}
    # другие категории целы
    assert _dotted_exists(env.state, "_settings.keys.openrouter")


async def test_purge_never_touches_exclusions_and_guard_keys(jr, env):
    await _confirm_category(jr, env, SyncCategory.ACCOUNTS.value)
    env.ret[SyncCategory.ACCOUNTS.value] = False
    ok, msg = await retention.purge_category(SyncCategory.ACCOUNTS.value)
    assert ok, msg
    assert not _dotted_exists(env.state, "_settings.co_owners")
    # guard-ключ операционной целостности цел
    assert _dotted_exists(env.state, "_settings.owner_id")
    # реестр исключений 18.1 цел (vault не был в плане и не тронут)
    assert _dotted_exists(env.state, "_settings.addons.vault.pin")
    assert (env.files_dir / ".frozen").exists()


async def test_purge_files_verified_and_unlinked(jr, env):
    plan = await bootstrap.build_plan_async()
    cat = next(
        c for c, items in plan.items.items()
        if any(i.entity_type == ENTITY_FILE
               and i.entity_id == "media/pic.bin" for i in items)
    )
    await _confirm_category(jr, env, cat)
    env.ret[cat] = False
    ok, msg = await retention.purge_category(cat)
    assert ok, msg
    assert not (env.files_dir / "media" / "pic.bin").exists()
    dropped = await jr.retention_dropped_keys()
    assert (ENTITY_FILE, "media/pic.bin") in dropped


# ─── 5. хвост 18.4: plaintext-секреты + state.json.bak ───────────────────────


async def test_secrets_purge_cleans_state_and_bak(jr, env):
    # state.json уже существует и содержит секрет (как на живом диске)
    from bot.storage import storage

    storage._save()  # noqa: SLF001 — пишет state.json с секретом
    storage._save()  # noqa: SLF001 — и .bak с секретом
    assert SECRET_VALUE in (env.files_dir / "state.json").read_text()
    assert SECRET_VALUE in (env.files_dir / "state.json.bak").read_text()

    await _confirm_category(jr, env, SyncCategory.SECRETS.value)
    env.ret[SyncCategory.SECRETS.value] = False
    ok, msg = await retention.purge_category(SyncCategory.SECRETS.value)
    assert ok, msg
    # plaintext исчез из state, ИЗ ОБОИХ файлов на диске (grep) и с диска
    assert not _dotted_exists(env.state, "_settings.keys.openrouter")
    assert SECRET_VALUE not in (env.files_dir / "state.json").read_text()
    assert SECRET_VALUE not in (env.files_dir / "state.json.bak").read_text()
    assert not (env.files_dir / "yt_cookies.txt").exists()
    assert retention._grep_leftover_secrets() == 0


async def test_secrets_purge_refused_if_migration_not_verified(
    jr, env, monkeypatch,
):
    async def _mig_bad():
        return SimpleNamespace(ok=False)

    monkeypatch.setattr(
        mig_mod, "migrate_plaintext_secrets_async", _mig_bad,
    )
    await _confirm_category(jr, env, SyncCategory.SECRETS.value)
    env.ret[SyncCategory.SECRETS.value] = False
    ok, msg = await retention.purge_category(SyncCategory.SECRETS.value)
    assert not ok
    assert "миграция" in msg
    assert _dotted_exists(env.state, "_settings.keys.openrouter")
    assert (env.files_dir / "yt_cookies.txt").exists()


# ─── 6. журнал намерения: докат и отмена ─────────────────────────────────────


async def test_interrupted_deletion_resumed_from_intent(jr, env):
    env.ret[SyncCategory.ARTICLES.value] = False
    entities = [
        {"entity_type": ENTITY_STATE,
         "entity_id": "_settings.addons.promptbox.p1"},
        {"entity_type": ENTITY_STATE,
         "entity_id": "_settings.addons.promptbox.p2"},
    ]
    # «обрыв»: намерение записано, само удаление не успело выполниться
    await jr.retention_intent_set(
        SyncCategory.ARTICLES.value, json.dumps(entities),
    )
    done = await retention.resume_pending_intents()
    assert done == 1
    assert not _dotted_exists(env.state, "_settings.addons.promptbox.p1")
    assert not _dotted_exists(env.state, "_settings.addons.promptbox.p2")
    assert await jr.retention_intents_all() == {}


async def test_intent_cancelled_if_retention_reenabled(jr, env):
    entities = [{"entity_type": ENTITY_STATE,
                 "entity_id": "_settings.addons.promptbox.p1"}]
    await jr.retention_intent_set(
        SyncCategory.ARTICLES.value, json.dumps(entities),
    )
    # retention снова включён ДО доката — намерение отменяется, копия цела
    done = await retention.resume_pending_intents()
    assert done == 0
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p1")
    assert await jr.retention_intents_all() == {}


async def test_resume_noop_when_frozen(jr, env, monkeypatch):
    monkeypatch.setattr(retention, "_is_frozen", lambda: True)
    env.ret[SyncCategory.ARTICLES.value] = False
    await jr.retention_intent_set(
        SyncCategory.ARTICLES.value,
        json.dumps([{"entity_type": ENTITY_STATE,
                     "entity_id": "_settings.addons.promptbox.p1"}]),
    )
    assert await retention.resume_pending_intents() == 0
    assert _dotted_exists(env.state, "_settings.addons.promptbox.p1")


# ─── 7. повторное включение retention ────────────────────────────────────────


async def test_reenable_clears_dropped_and_resets_pull_cursor(jr, env):
    await _confirm_category(jr, env, SyncCategory.ARTICLES.value)
    env.ret[SyncCategory.ARTICLES.value] = False
    ok, _ = await retention.purge_category(SyncCategory.ARTICLES.value)
    assert ok
    assert await jr.retention_dropped_keys()
    await jr.meta_set("pull_cursor", "2026-07-22T12:00:00+00:00")

    env.ret[SyncCategory.ARTICLES.value] = True
    await retention.reenable_category(SyncCategory.ARTICLES.value)
    # реестр удалённых пуст — pull снова имеет право восстановить копии
    assert await jr.retention_dropped_keys() == set()
    # meta-строки категории сняты, курсор pull сброшен на эпоху
    meta = await jr.local_meta_all()
    assert not any(
        m["category"] == SyncCategory.ARTICLES.value for m in meta.values()
    )
    assert (await jr.meta_get("pull_cursor")).startswith("1970-")
