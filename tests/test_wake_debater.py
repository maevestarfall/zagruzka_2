"""Functional tests for the wake-the-debater system + restore re-point."""
import importlib
import time

import pytest


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "wake.db"
    monkeypatch.setenv("DEBATE_DB_PATH", str(db_path))
    from bot.addons.debates.engine import db as _db
    importlib.reload(_db)
    _db.init_db()
    # reload modules that bound `db` at import time
    from bot.addons.debates import debate_server as _srv
    from bot.addons.debates.engine import snapshot as _snap
    importlib.reload(_snap)
    importlib.reload(_srv)
    return _db, _srv, _snap


def _mk_door(db, door_id="door_test1", secret="secret123", phase="atoms_adversarial"):
    with db.connect() as con:
        con.execute(
            "INSERT INTO doors (door_id, title, part_id, style, status, "
            "current_phase, created_at, join_secret) "
            "VALUES (?,?,?,?, 'open', ?, ?, ?)",
            (door_id, "T", "generic", "strict", phase, time.time(), secret))
    return door_id


def test_extract_wake_url(fresh_db):
    _db, srv, _ = fresh_db
    assert srv._extract_wake_url(
        "Моя ссылка-будильник: https://hook.lindy.ai/abc?x=1") \
        == "https://hook.lindy.ai/abc?x=1"
    # no keyword -> ignored
    assert srv._extract_wake_url("see https://example.com for refs") == ""
    # markdown link with keyword
    assert srv._extract_wake_url(
        "[🔥 Wake Up](https://hook.make.com/zzz)").startswith("https://hook.make.com")


def test_upsert_and_get(fresh_db):
    _db, srv, _ = fresh_db
    _mk_door(_db)
    srv._wake_upsert("door_test1", "debater_a", "https://hook/a")
    srv._wake_upsert("door_test1", "debater_a", "https://hook/a2")  # overwrite
    assert srv._wake_get("door_test1", "debater_a") == "https://hook/a2"
    assert srv._wake_get("door_test1", "debater_b") == ""


def test_restore_returns_join_secret(fresh_db):
    _db, _srv, snap = fresh_db
    _mk_door(_db, door_id="door_src", secret="abc_secret", phase="atoms_approve")
    exported = snap.export_snapshot("door_src")
    assert exported is not None
    # wipe and restore
    with _db.connect() as con:
        con.execute("DELETE FROM doors WHERE door_id = ?", ("door_src",))
    res = snap.restore_snapshot(exported, start_catchup=False)
    assert res["ok"]
    assert res["door_id"] == "door_src"
    assert res["join_secret"] == "abc_secret"
    assert res["current_phase"] == "atoms_approve"


def test_watchdog_pings_stale_debater(fresh_db, monkeypatch):
    _db, srv, _ = fresh_db
    _mk_door(_db, door_id="door_w", secret="s")
    # debater posted 40 min ago
    old = time.time() - 40 * 60
    with _db.connect() as con:
        con.execute(
            "INSERT INTO msgs (door_id, debater_id, kind, payload_json, "
            "body_text, payload_bytes, created_at) VALUES "
            "(?, 'debater_a', 'note', '{}', 'hi', 2, ?)",
            ("door_w", old))
    srv._wake_upsert("door_w", "debater_a", "https://hook/wake_a")

    # configure the door slot in addon state with a 23-min timeout
    from bot.addons import state as addon_state
    addon_state.set_("debates", "viktor_v3",
                     {"door_configs": {"1": {"door_id": "door_w",
                                             "wake_timeout_min": 23}}})

    pinged = {}

    async def fake_ping(url):
        pinged["url"] = url
        return True, 200

    monkeypatch.setattr(srv, "_wake_ping", fake_ping)

    import asyncio
    asyncio.run(srv._wake_watchdog_tick())

    assert pinged.get("url") == "https://hook/wake_a"
    with _db.connect() as con:
        lp = con.execute(
            "SELECT last_pinged_at FROM debater_wake WHERE door_id='door_w' "
            "AND debater_id='debater_a'").fetchone()["last_pinged_at"]
        note = con.execute(
            "SELECT content FROM admin_chat WHERE door_id='door_w' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
    assert lp > 0
    assert note is not None and "разбудили" in note["content"]


def test_watchdog_skips_fresh_and_disabled(fresh_db, monkeypatch):
    _db, srv, _ = fresh_db
    _mk_door(_db, door_id="door_f", secret="s")
    fresh = time.time() - 60  # 1 min ago
    with _db.connect() as con:
        con.execute(
            "INSERT INTO msgs (door_id, debater_id, kind, payload_json, "
            "body_text, payload_bytes, created_at) VALUES "
            "(?, 'debater_a', 'note', '{}', 'hi', 2, ?)",
            ("door_f", fresh))
    srv._wake_upsert("door_f", "debater_a", "https://hook/x")
    from bot.addons import state as addon_state
    addon_state.set_("debates", "viktor_v3",
                     {"door_configs": {"1": {"door_id": "door_f",
                                             "wake_timeout_min": 23}}})
    calls = {"n": 0}

    async def fake_ping(url):
        calls["n"] += 1
        return True, 200

    monkeypatch.setattr(srv, "_wake_ping", fake_ping)
    import asyncio
    asyncio.run(srv._wake_watchdog_tick())
    assert calls["n"] == 0  # too fresh
