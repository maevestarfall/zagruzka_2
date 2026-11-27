"""Tests for snapshot/result/catch-up (engine/snapshot.py)."""
import importlib
import json
import os

import pytest


@pytest.fixture()
def eng(tmp_path):
    os.environ["DEBATE_DB_PATH"] = str(tmp_path / "t.db")
    from bot.addons.debates.engine import db as _db
    importlib.reload(_db)
    _db.init_db()
    from bot.addons.debates.engine import state_helpers as _sh
    importlib.reload(_sh)
    from bot.addons.debates.engine import snapshot as _snap
    importlib.reload(_snap)
    return _db, _snap


def _seed(db, door_id="door_T"):
    now = db.now_ts()
    with db.connect() as con:
        con.execute(
            "INSERT INTO doors (door_id,title,part_id,style,status,current_phase,"
            "created_at,judge_mode) VALUES (?,?,?,?,?,?,?,?)",
            (door_id, "Test door", "generic", "default", "open",
             "atoms_adversarial", now, "self"))
        for d in ("debater_a", "debater_b"):
            con.execute(
                "INSERT INTO door_tokens (token,door_id,debater_id,role,"
                "created_at,claimed) VALUES (?,?,?,?,?,1)",
                ("tok_" + d, door_id, d, "debater", now))
        for d in ("debater_a", "debater_b"):
            con.execute(
                "INSERT INTO msgs (door_id,debater_id,kind,payload_json,"
                "body_text,payload_bytes,token_count_reported,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (door_id, d, "atom-propose", "{}", "x", 1, 0, now))
        mid = con.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        for author, ver, content in (
                ("debater_a", 1, "A api answer"),
                ("debater_b", 2, "B api answer")):
            con.execute(
                "INSERT INTO atoms (door_id,canonical_category_id,version,"
                "author_debater_id,msg_id,pick,provocation_level,"
                "code_shape_anchor_json,content) VALUES (?,?,?,?,?,?,?,?,?)",
                (door_id, "api_design", ver, author, mid, "x", "low", "[]",
                 content))
    return door_id


def test_result_markdown(eng):
    db, snap = eng
    did = _seed(db)
    md = snap.build_result_markdown(did)
    assert "Результат дебатов" in md
    assert "A api answer" in md and "B api answer" in md
    assert snap.build_result_markdown("nope") is None


def test_snapshot_roundtrip_and_catchup(eng):
    db, snap = eng
    did = _seed(db)
    snap_obj = snap.export_snapshot(did)
    assert snap_obj["tables"]["doors"]
    assert len(snap_obj["tables"]["atoms"]) == 2
    blob = json.dumps(snap_obj)  # must be JSON-serialisable

    # Wipe the whole DB (simulate fresh redeploy) then restore.
    db.reset()
    res = snap.restore_snapshot(json.loads(blob), start_catchup=True)
    assert res["ok"] and res["door_id"] == did
    assert res["restored"]["atoms"] == 2
    # door + atoms came back
    assert snap.build_result_markdown(did) is not None
    # catch-up armed with both debaters in turn order
    cu = snap.get_catchup(did)
    assert cu["active"] and cu["waiting_for"] == "debater_a"
    assert cu["queue"] == ["debater_a", "debater_b"]


def test_catchup_turn_order(eng):
    db, snap = eng
    did = _seed(db)
    snap.set_catchup(did, ["debater_a", "debater_b"])
    # wrong turn rejected
    r = snap.catchup_post(did, "debater_b", "stuff")
    assert r["accepted"] is False and r["reason"] == "not_your_turn"
    # correct turn accepted, pops queue
    r = snap.catchup_post(did, "debater_a", "my accumulated research")
    assert r["accepted"] and r["catchup"]["waiting_for"] == "debater_b"
    # now b
    r = snap.catchup_post(did, "debater_b", "b research")
    assert r["accepted"] and r["catchup"]["active"] is False
    # further posts rejected (round over)
    r = snap.catchup_post(did, "debater_a", "late")
    assert r["accepted"] is False and r["reason"] == "catchup_not_active"
    # the notes were written
    with db.connect() as con:
        n = con.execute(
            "SELECT COUNT(*) c FROM msgs WHERE kind='note' AND "
            "json_extract(payload_json,'$.catchup')=1").fetchone()["c"]
    assert n == 2
