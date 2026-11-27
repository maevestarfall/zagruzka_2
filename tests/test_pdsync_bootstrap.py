# Tests for ЧАСТЬ 18.5: dry-run plan, стабильные operation_id, журнал.
# Co-authored with CoCo
"""Приёмка 18.5 (та часть, что проверяется без внешней БД/S3):

* план передачи (``build_plan``) — READ-ONLY: включает ВСЕ 8 категорий,
  включая catch-all ``other``, и НИКОГДА не включает реестр исключений
  18.1 (``.frozen``, namespace ``vault``, tmp/bak, локальный журнал);
* ``operation_id`` детерминирован и стабилен между запусками — повторный
  запуск даёт те же ID (идемпотентность = отсутствие дублей);
* локальный журнал ``sync_journal.db`` — ОТДЕЛЬНЫЙ SQLite-файл, НЕ в
  ``state.json``; upsert по ``operation_id`` не создаёт дублей; статус
  ``confirmed`` сохраняется при повторе с тем же checksum и честно
  сбрасывается в ``pending`` при изменившемся checksum;
* dry-run-отчёт показывает назначение, 8 категорий, retention-правила и
  способ защиты секретов — и не содержит секретов.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot.addons.masterroom.sync import bootstrap
from bot.addons.masterroom.sync.canonical import (
    canonical_json_bytes,
    payload_checksum,
)
from bot.addons.masterroom.sync.categories import (
    ALL_CATEGORIES,
    SyncCategory,
)
from bot.addons.masterroom.sync.journal import SyncJournal


# ─── план: реестр исключений и полнота категорий ────────────────────────────


def _sample_state() -> dict:
    return {
        "_settings": {
            "addons": {
                "vault": {"mode": "freeze", "pin": "0000"},  # исключение
                "masterroom": {"projects": {"p1": {"name": "demo"}}},
            },
        },
        "articles": {"a1": {"title": "Заголовок", "body": "text"}},
        "unclassified_blob": {"foo": "bar"},  # должен попасть в other
    }


def test_build_plan_covers_all_categories_keys(tmp_path):
    plan = bootstrap.build_plan(state=_sample_state(), data_dir=tmp_path)
    # все 8 категорий присутствуют в плане (даже пустые) — включая other
    assert set(plan.items.keys()) == {c.value for c in ALL_CATEGORIES}
    totals = plan.totals()
    assert set(totals.keys()) == {c.value for c in ALL_CATEGORIES}


def test_build_plan_excludes_exclusion_registry(tmp_path):
    # файлы реестра исключений 18.1 в DATA_DIR
    (tmp_path / ".frozen").write_text("x")
    (tmp_path / "state.json.tmp").write_text("x")
    (tmp_path / "state.json.bak").write_text("x")
    (tmp_path / "sync_journal.db").write_text("x")
    (tmp_path / "sync_journal.db-wal").write_text("x")
    (tmp_path / "normal.txt").write_text("data")

    plan = bootstrap.build_plan(state=_sample_state(), data_dir=tmp_path)
    all_ids = [
        i.entity_id for items in plan.items.values() for i in items
    ]
    # исключения не передаются никогда
    for banned in (".frozen", "state.json.tmp", "state.json.bak",
                   "sync_journal.db", "sync_journal.db-wal"):
        assert banned not in all_ids
    # namespace vault (kill-switch) не покидает инстанс
    assert not any(i.startswith("_settings.addons.vault") for i in all_ids)
    # обычный файл — в плане
    assert "normal.txt" in all_ids


def test_build_plan_catch_all_other_not_skipped(tmp_path):
    plan = bootstrap.build_plan(state=_sample_state(), data_dir=tmp_path)
    other_ids = [i.entity_id for i in plan.items[SyncCategory.OTHER.value]]
    assert any("unclassified_blob" in i for i in other_ids)


def test_build_plan_is_read_only(tmp_path):
    """План не изменяет ни state, ни DATA_DIR (побайтно)."""
    state = _sample_state()
    snapshot = json.dumps(state, sort_keys=True, ensure_ascii=False)
    f = tmp_path / "file.bin"
    f.write_bytes(b"\x00\x01\x02")
    before = f.read_bytes()
    bootstrap.build_plan(state=state, data_dir=tmp_path)
    assert json.dumps(state, sort_keys=True, ensure_ascii=False) == snapshot
    assert f.read_bytes() == before


# ─── operation_id: детерминизм ───────────────────────────────────────────────


def test_operation_id_stable_across_calls():
    a = bootstrap.operation_id_for("state_key", "articles.a1")
    b = bootstrap.operation_id_for("state_key", "articles.a1")
    assert a == b
    assert a != bootstrap.operation_id_for("state_key", "articles.a2")
    assert a != bootstrap.operation_id_for("file", "articles.a1")


def test_plan_items_have_stable_operation_ids(tmp_path):
    p1 = bootstrap.build_plan(state=_sample_state(), data_dir=tmp_path)
    p2 = bootstrap.build_plan(state=_sample_state(), data_dir=tmp_path)
    ids1 = sorted(
        i.operation_id for items in p1.items.values() for i in items
    )
    ids2 = sorted(
        i.operation_id for items in p2.items.values() for i in items
    )
    assert ids1 == ids2
    assert len(ids1) == len(set(ids1))  # без коллизий


# ─── журнал: отдельный SQLite, идемпотентный upsert ─────────────────────────


def test_journal_lives_outside_state_json():
    from bot.addons.masterroom.sync.journal import journal_db_path
    from bot.config import DATA_DIR

    p = journal_db_path()
    assert p.name == "sync_journal.db"
    assert str(p).startswith(str(DATA_DIR))
    assert p.name != "state.json"


async def test_journal_upsert_idempotent_no_duplicates(tmp_path):
    jr = SyncJournal(tmp_path / "sync_journal.db")
    op = bootstrap.operation_id_for("state_key", "articles.a1")
    for _ in range(3):  # повторы с тем же operation_id — НЕ дубль
        await jr.upsert_pending(
            operation_id=op, category="articles",
            entity_type="state_key", entity_id="articles.a1",
            revision=1, checksum="abc",
        )
    stats = await jr.stats_by_category()
    assert stats == {"articles": {"pending": 1}}  # ровно одна строка
    row = await jr.get(op)
    assert row is not None
    import aiosqlite

    async with aiosqlite.connect(tmp_path / "sync_journal.db") as db:
        cur = await db.execute("SELECT COUNT(*) FROM sync_outbox")
        (n,) = await cur.fetchone()
    assert n == 1


async def test_journal_confirmed_kept_on_same_checksum(tmp_path):
    """Resume: confirmed с тем же checksum остаётся confirmed;
    изменившийся checksum честно возвращает запись в pending."""
    jr = SyncJournal(tmp_path / "sync_journal.db")
    op = bootstrap.operation_id_for("state_key", "k")
    await jr.upsert_pending(
        operation_id=op, category="other", entity_type="state_key",
        entity_id="k", revision=1, checksum="c1",
    )
    await jr.mark_confirmed(op)
    row = await jr.upsert_pending(
        operation_id=op, category="other", entity_type="state_key",
        entity_id="k", revision=1, checksum="c1",
    )
    assert row["status"] == "confirmed"  # resume пропустит без повтора
    row = await jr.upsert_pending(
        operation_id=op, category="other", entity_type="state_key",
        entity_id="k", revision=2, checksum="c2",
    )
    assert row["status"] == "pending"  # данные изменились — передать заново


async def test_journal_failed_error_is_sanitized(tmp_path):
    """Секреты не попадают в журнал даже через тексты ошибок."""
    jr = SyncJournal(tmp_path / "sync_journal.db")
    op = bootstrap.operation_id_for("state_key", "k")
    await jr.upsert_pending(
        operation_id=op, category="other", entity_type="state_key",
        entity_id="k", revision=1, checksum="c1",
    )
    await jr.mark_failed(
        op, "connect failed: postgres://user:supersecret@host/db"
    )
    row = await jr.get(op)
    assert row is not None and row["status"] == "failed"
    assert "supersecret" not in (row["error_public"] or "")


# ─── canonical checksum: RFC 8785-style, без поля checksum ──────────────────


def test_payload_checksum_excludes_checksum_field():
    p1 = {"a": 1, "b": "x"}
    p2 = {"a": 1, "b": "x", "checksum": "whatever"}
    assert payload_checksum(p1) == payload_checksum(p2)


def test_canonical_json_bytes_key_order_independent():
    assert canonical_json_bytes({"b": 2, "a": 1}) == \
        canonical_json_bytes({"a": 1, "b": 2})


# ─── dry-run отчёт ───────────────────────────────────────────────────────────


def test_preview_report_sections(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    plan = bootstrap.build_plan(state=_sample_state(), data_dir=tmp_path)
    report = bootstrap.render_preview_report(plan)
    assert "dry-run" in report
    assert "Retention" in report
    assert "Защита секретов" in report
    from bot.addons.masterroom.sync.categories import CATEGORY_LABEL

    for cat in ALL_CATEGORIES:  # все 8 категорий, включая other
        assert CATEGORY_LABEL[cat] in report
    # предупреждение о privacy-риске постоянного дублирования (18.5 §2)
    assert "privacy-риск" in report


def test_preview_report_no_plaintext_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    state = _sample_state()
    state["accounts"] = {"github": {"token": "ghp_PLAINTEXTSECRET123"}}
    plan = bootstrap.build_plan(state=state, data_dir=tmp_path)
    report = bootstrap.render_preview_report(plan)
    assert "ghp_PLAINTEXTSECRET123" not in report
    assert "0000" not in report  # vault pin не утекает в отчёт


# ─── бонус: run_bootstrap уважает kill-switch ────────────────────────────────


async def test_run_bootstrap_respects_freeze(monkeypatch):
    """18.0 п.9: при заморозке kill-switch bootstrap не начинается."""
    monkeypatch.setattr(bootstrap, "_is_frozen", lambda: True)
    result = await bootstrap.run_bootstrap()
    assert result["status"] == "frozen"
