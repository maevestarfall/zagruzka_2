"""get_debater_token reads the live token WITHOUT rotating it (handoff does)."""
import importlib
import time

import pytest


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBATE_DB_PATH", str(tmp_path / "tok.db"))
    from bot.addons.debates.engine import db as _db
    importlib.reload(_db)
    _db.init_db()
    from bot.addons.debates.engine import doors as _doors
    importlib.reload(_doors)
    return _db, _doors


def _mk_door_with_token(db, door_id="d1", deb="debater_b", token="tok_orig"):
    with db.connect() as con:
        con.execute(
            "INSERT INTO doors (door_id, title, part_id, style, status, "
            "current_phase, created_at, join_secret) "
            "VALUES (?,?,?,?, 'open', 'atoms_approve', ?, 's')",
            (door_id, "T", "generic", "strict", time.time()))
        con.execute(
            "INSERT INTO door_tokens (token, door_id, debater_id, role, "
            "claimed, created_at) VALUES (?,?,?, 'debater', 1, ?)",
            (token, door_id, deb, time.time()))


def test_get_debater_token_no_rotate(fresh_db):
    db, doors = fresh_db
    _mk_door_with_token(db)
    r1 = doors.get_debater_token("d1", "debater_b")
    assert r1["ok"] and r1["token"] == "tok_orig" and r1["claimed"] is True
    # reading twice must return the SAME token (no rotation side-effect)
    r2 = doors.get_debater_token("d1", "debater_b")
    assert r2["token"] == "tok_orig"
    # and resolve_token still works for it
    assert doors.resolve_token("tok_orig") == ("d1", "debater_b", "debater")


def test_get_debater_token_errors(fresh_db):
    db, doors = fresh_db
    _mk_door_with_token(db)
    assert doors.get_debater_token("nope", "debater_b")["error"] == "unknown_door"
    assert doors.get_debater_token("d1", "debater_z")["error"] == "debater_not_found"


def test_handoff_rotates_and_kills_old(fresh_db):
    db, doors = fresh_db
    _mk_door_with_token(db)
    snap = doors.handoff_debater("d1", "debater_b")
    new_tok = snap["handoff"]["new_token"]
    assert new_tok != "tok_orig"
    # old token is now dead, new one resolves to same debater
    assert doors.resolve_token("tok_orig") is None
    assert doors.resolve_token(new_tok) == ("d1", "debater_b", "debater")
    # get_debater_token now returns the NEW token
    assert doors.get_debater_token("d1", "debater_b")["token"] == new_tok
