"""Tests for debate door slots: 3 debaters per door, join order, kick, lobby.

Covers Mason's v14 requests:
- a door must allow THREE AI debaters to join (not two);
- the owner can kick any occupant to free a stuck slot (the "slot A
  постоянно занят" complaint).
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def doors(tmp_path, monkeypatch):
    """Fresh debate engine bound to a temp sqlite DB."""
    db_path = tmp_path / "debate_test.db"
    monkeypatch.setenv("DEBATE_DB_PATH", str(db_path))
    from bot.addons.debates.engine import db as _db

    importlib.reload(_db)
    _db.init_db()
    from bot.addons.debates.engine import doors as _doors

    importlib.reload(_doors)
    return _doors


def test_door_has_three_slots(doors):
    res = doors.create_door_simple("Тест", "generic")
    assert res["ok"]
    lobby = doors.get_lobby(res["door_id"])
    assert lobby["ok"]
    # three placeholder slots, none claimed yet
    assert sorted(lobby["waiting_for"]) == ["debater_a", "debater_b", "debater_c"]
    assert lobby["joined"] == []


def test_three_debaters_can_join_in_order(doors):
    res = doors.create_door_simple("Тест", "generic")
    secret = res["join_secret"]
    j1 = doors.join_door(secret)
    j2 = doors.join_door(secret)
    j3 = doors.join_door(secret)
    assert j1["debater_id"] == "debater_a"
    assert j2["debater_id"] == "debater_b"
    assert j3["debater_id"] == "debater_c"
    assert j3["all_claimed"] is True
    assert j3["total_slots"] == 3
    # fourth join → door full
    j4 = doors.join_door(secret)
    assert j4.get("error") == "door_full"


def test_kick_frees_a_slot(doors):
    res = doors.create_door_simple("Тест", "generic")
    did = res["door_id"]
    secret = res["join_secret"]
    doors.join_door(secret)  # debater_a
    doors.join_door(secret)  # debater_b
    lobby = doors.get_lobby(did)
    assert set(lobby["joined"]) == {"debater_a", "debater_b"}

    kick = doors.kick_debater(did, "debater_a")
    assert kick["ok"] and kick["kicked_debater_id"] == "debater_a"

    lobby2 = doors.get_lobby(did)
    assert "debater_a" not in lobby2["joined"]
    assert "debater_a" in lobby2["waiting_for"]

    # the freed slot is reusable
    rejoin = doors.join_door(secret)
    assert rejoin["debater_id"] == "debater_a"


def test_kick_unknown_or_unclaimed(doors):
    res = doors.create_door_simple("Тест", "generic")
    did = res["door_id"]
    assert doors.kick_debater(did, "debater_a")["error"] == "slot_not_claimed"
    assert doors.kick_debater(did, "debater_z")["error"] == "debater_not_found"
    assert doors.kick_debater("nope", "debater_a")["error"] == "unknown_door"


def test_door_view_kb_has_server_and_third_debater():
    """Door control keyboard exposes the new 🖥 Сервер button and a 3rd
    debater export button."""
    from bot.addons.debates import viktor_v3

    kb = viktor_v3._kb_door_view(1, "🟢 open", "self")
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row
           if btn.callback_data]
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "vsd:server:1" in cbs
    # 3rd debater is reachable via the per-debater history/export button (C)
    assert "vsd:debhist:1:c" in cbs
    # result + restart (snapshot) controls present
    assert "vsd:result:1" in cbs
    assert "vsd:restart:1" in cbs
    assert any("Сервер" in t for t in texts)
