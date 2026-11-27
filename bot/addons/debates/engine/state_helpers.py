"""Read-side helpers: load DoorCtx from SQLite for enforcement.

Kept separate from db.py (raw SQL) and enforcement.py (pure logic) for
testability — tests can build a DoorCtx without touching SQLite at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from .canonical import PartConfig, load_part_config
from .enforcement import DoorCtx


def load_door_row(con, door_id: str):
    cur = con.execute("SELECT * FROM doors WHERE door_id = ?", (door_id,))
    return cur.fetchone()


def load_atoms_by_id(con, door_id: str) -> dict:
    """Return {atom_id: row_dict} keyed by string canonical id_vN."""
    out = {}
    cur = con.execute(
        "SELECT * FROM atoms WHERE door_id = ? ORDER BY canonical_category_id, version",
        (door_id,))
    for r in cur.fetchall():
        atom_id = f"atom_{r['canonical_category_id']}_v{r['version']}"
        out[atom_id] = {
            "atom_id": atom_id,
            "category": r["canonical_category_id"],
            "version": r["version"],
            "author_debater_id": r["author_debater_id"],
            "msg_id": r["msg_id"],
            "pick": r["pick"],
            "content": r["content"],
            "locked": bool(r["locked"]),
            "approved_by": json.loads(r["approved_by_json"]),
        }
    return out


def load_pending_revision_atoms(con, door_id: str) -> dict:
    """For each atom_id that has an unanswered atom-adversarial with
    revision_required=true, map atom_id -> adversarial msg_id.

    'Unanswered' means: after the adversarial, no atom-revision from the
    *adversarial-receiving* debater on that atom_id references it via
    revision_triggered_by."""
    pending: dict[str, int] = {}
    cur = con.execute(
        """SELECT m.msg_id, m.debater_id, m.payload_json
           FROM msgs m
           WHERE m.door_id = ? AND m.kind = 'atom-adversarial'
             AND m.rejected_reason IS NULL
           ORDER BY m.msg_id ASC""", (door_id,))
    for r in cur.fetchall():
        payload = json.loads(r["payload_json"])
        if not payload.get("revision_required", True):
            continue
        atom_id = payload.get("target_atom_id")
        if not atom_id:
            continue
        # check if a later atom-revision references this msg_id
        cur2 = con.execute(
            """SELECT msg_id FROM msgs
               WHERE door_id = ? AND kind = 'atom-revision' AND msg_id > ?
                 AND rejected_reason IS NULL
                 AND payload_json LIKE ?""",
            (door_id, r["msg_id"], f'%"revision_triggered_by":{r["msg_id"]}%'))
        if not cur2.fetchone():
            pending[atom_id] = r["msg_id"]
    return pending


def load_per_debater_stats(con, door_id: str) -> dict:
    """Aggregate counters per debater_id used by enforcement caps."""
    out: dict[str, dict] = {}
    cur = con.execute(
        """SELECT debater_id, kind,
                  COUNT(*) AS cnt,
                  SUM(payload_bytes) AS bytes_sum,
                  SUM(token_count_reported) AS tok_sum
           FROM msgs
           WHERE door_id = ? AND rejected_reason IS NULL
           GROUP BY debater_id, kind""", (door_id,))
    for r in cur.fetchall():
        d = out.setdefault(r["debater_id"], {
            "messages": 0, "atoms_proposed": 0, "critiques_total": 0,
            "critiques_disagree": 0, "critiques_partial": 0, "critiques_agree": 0,
            "revisions_total": 0, "revisions_nrn": 0, "revisions_substantive": 0,
            "adversarial_total": 0, "approve_total": 0,
            "payload_bytes": 0, "token_count_reported": 0,
        })
        d["messages"] += r["cnt"]
        d["payload_bytes"] += r["bytes_sum"] or 0
        d["token_count_reported"] += r["tok_sum"] or 0
        if r["kind"] == "atom-propose":
            d["atoms_proposed"] += r["cnt"]
        elif r["kind"] == "atom-critique":
            d["critiques_total"] += r["cnt"]
        elif r["kind"] == "atom-revision":
            d["revisions_total"] += r["cnt"]
        elif r["kind"] == "atom-adversarial":
            d["adversarial_total"] += r["cnt"]
        elif r["kind"] == "atom-approve":
            d["approve_total"] += r["cnt"]

    # second pass: count verdict breakdown + NRN
    cur2 = con.execute(
        """SELECT debater_id, kind, payload_json FROM msgs
           WHERE door_id = ? AND rejected_reason IS NULL
             AND kind IN ('atom-critique', 'atom-revision')""", (door_id,))
    for r in cur2.fetchall():
        d = out.setdefault(r["debater_id"], {})
        payload = json.loads(r["payload_json"])
        if r["kind"] == "atom-critique":
            v = payload.get("verdict")
            if v == "DISAGREE":
                d["critiques_disagree"] = d.get("critiques_disagree", 0) + 1
            elif v == "PARTIAL":
                d["critiques_partial"] = d.get("critiques_partial", 0) + 1
            elif v == "AGREE":
                d["critiques_agree"] = d.get("critiques_agree", 0) + 1
        elif r["kind"] == "atom-revision":
            if payload.get("outcome") == "NO_REVISION_NEEDED":
                d["revisions_nrn"] = d.get("revisions_nrn", 0) + 1
            elif payload.get("outcome") == "SUBSTANTIVE":
                d["revisions_substantive"] = d.get("revisions_substantive", 0) + 1
    return out


def load_door_ctx(con, door_id: str, part_cfg: PartConfig) -> DoorCtx:
    door = load_door_row(con, door_id)
    return DoorCtx(
        door_id=door_id,
        current_phase=door["current_phase"],
        forced_validation_mode=bool(door["forced_validation_mode"]),
        part_cfg=part_cfg,
        atoms_by_id=load_atoms_by_id(con, door_id),
        pending_revision_atoms=load_pending_revision_atoms(con, door_id),
        per_debater_stats=load_per_debater_stats(con, door_id),
    )


def load_part_cfg(con, door_id: str, configs_dir: Path) -> PartConfig:
    door = load_door_row(con, door_id)
    return load_part_config(door["part_id"], configs_dir)


def door_rejection_rate_30m(con, door_id: str) -> float:
    cur = con.execute(
        """SELECT
              SUM(CASE WHEN rejected_reason IS NULL THEN 0 ELSE 1 END) AS rej,
              COUNT(*) AS tot
           FROM msgs
           WHERE door_id = ? AND created_at > strftime('%s','now') - 1800""",
        (door_id,))
    r = cur.fetchone()
    tot = r["tot"] or 0
    if tot == 0:
        return 0.0
    return (r["rej"] or 0) / tot


def door_disagree_rate(con, door_id: str) -> float:
    cur = con.execute(
        """SELECT
              SUM(CASE WHEN payload_json LIKE '%"verdict":"DISAGREE"%' THEN 1 ELSE 0 END) AS d,
              COUNT(*) AS tot
           FROM msgs
           WHERE door_id = ? AND kind = 'atom-critique' AND rejected_reason IS NULL""",
        (door_id,))
    r = cur.fetchone()
    tot = r["tot"] or 0
    if tot == 0:
        return 0.0
    return (r["d"] or 0) / tot
