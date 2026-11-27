"""Door lifecycle: create door, mint tokens, advance phases, write msgs.

Public API consumed by main.py routes:
- create_door(...)
- post_msg(...)
- advance_phase_if_ready(...)
- list_door_events(...)
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from . import db, state_helpers, acceptance
from .canonical import PartConfig, load_part_config
from .enforcement import (
    validate_envelope,
)
from .models import MsgEnvelope


CONFIGS_DIR = Path(__file__).parent / "part_configs"
REFERENCE_DIR = Path(__file__).parent / "reference"


def _gen_door_id() -> str:
    return "door_" + secrets.token_urlsafe(8)


def _gen_token() -> str:
    return "tok_" + secrets.token_urlsafe(24)


# ---------- create door ----------

def create_door(title: str, part_id: str, style: str,
                debater_ids: list[str]) -> dict:
    if style != "viktor_v3":
        raise ValueError(f"unsupported style: {style}")
    if len(debater_ids) != 2 or debater_ids[0] == debater_ids[1]:
        raise ValueError("need exactly 2 distinct debater_ids")
    part_cfg = load_part_config(part_id, CONFIGS_DIR)
    door_id = _gen_door_id()
    tokens = {d: _gen_token() for d in debater_ids}
    now = db.now_ts()
    with db.connect() as con:
        con.execute(
            "INSERT INTO doors (door_id, title, part_id, style, status, "
            "current_phase, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (door_id, title, part_id, style, "open", "voice_manifest", now))
        for deb, tok in tokens.items():
            con.execute(
                "INSERT INTO door_tokens (token, door_id, debater_id, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tok, door_id, deb, "debater", now))
    return {
        "ok": True,
        "door_id": door_id,
        "tokens": tokens,
        "canonical_categories": list(part_cfg.canonical_atom_categories),
        "canonical_clusters": list(part_cfg.canonical_clusters),
        "title": title,
        "part_id": part_id,
        "style": style,
    }


def _gen_join_secret() -> str:
    return secrets.token_urlsafe(16)


def create_door_simple(title: str, part_id: str,
                       judge_mode: str = "self") -> dict:
    """Create a door with a join link. Debaters self-assign via join_secret.

    No debater_ids needed — three slots are created with placeholder IDs
    (debater_a, debater_b, debater_c). Joiners self-assign in order: first
    gets debater_a, second debater_b, third debater_c.
    """
    part_cfg = load_part_config(part_id, CONFIGS_DIR)
    door_id = _gen_door_id()
    join_secret = _gen_join_secret()
    debater_ids = ["debater_a", "debater_b", "debater_c"]
    tokens = {d: _gen_token() for d in debater_ids}
    now = db.now_ts()
    with db.connect() as con:
        con.execute(
            "INSERT INTO doors (door_id, title, part_id, style, status, "
            "current_phase, created_at, join_secret, judge_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (door_id, title, part_id, "viktor_v3", "open",
             "voice_manifest", now, join_secret,
             judge_mode if judge_mode in ("judge", "self") else "self"))
        for deb, tok in tokens.items():
            con.execute(
                "INSERT INTO door_tokens (token, door_id, debater_id, role, "
                "created_at, claimed) VALUES (?, ?, ?, ?, ?, ?)",
                (tok, door_id, deb, "debater", now, 0))
    return {
        "ok": True,
        "door_id": door_id,
        "join_secret": join_secret,
        "canonical_categories": list(part_cfg.canonical_atom_categories),
        "canonical_clusters": list(part_cfg.canonical_clusters),
        "title": title,
        "part_id": part_id,
    }


def join_door(join_secret: str) -> dict | None:
    """Claim the next available slot in a door via join_secret.

    Returns {door_id, debater_id, token, part_id, title, slot_number,
             total_slots, all_claimed} or None if invalid/full.
    """
    with db.connect() as con:
        door = con.execute(
            "SELECT door_id, title, part_id, status FROM doors "
            "WHERE join_secret = ?", (join_secret,)).fetchone()
        if not door:
            return None
        if door["status"] not in ("open",):
            return {"error": "door_closed", "status": door["status"]}

        # Find unclaimed slots
        slots = con.execute(
            "SELECT token, debater_id, claimed FROM door_tokens "
            "WHERE door_id = ? ORDER BY debater_id",
            (door["door_id"],)).fetchall()
        total = len(slots)
        claimed_count = sum(1 for s in slots if s["claimed"])

        # Find first unclaimed
        for s in slots:
            if not s["claimed"]:
                con.execute(
                    "UPDATE door_tokens SET claimed = 1 WHERE token = ?",
                    (s["token"],))
                return {
                    "ok": True,
                    "door_id": door["door_id"],
                    "debater_id": s["debater_id"],
                    "token": s["token"],
                    "part_id": door["part_id"],
                    "title": door["title"],
                    "slot_number": claimed_count + 1,
                    "total_slots": total,
                    "all_claimed": (claimed_count + 1) >= total,
                }
        return {"error": "door_full", "door_id": door["door_id"]}


def rejoin_door(join_secret: str, token: str) -> dict | None:
    """Re-identify a visitor who already claimed a slot.

    If *token* belongs to the door behind *join_secret*, return the same
    payload shape as :func:`join_door` (ok, door_id, debater_id, token …)
    without claiming anything.  Returns ``None`` when the token is
    invalid or doesn't match the door.
    """
    with db.connect() as con:
        door = con.execute(
            "SELECT door_id, title, part_id, status FROM doors "
            "WHERE join_secret = ?", (join_secret,)).fetchone()
        if not door:
            return None
        slot = con.execute(
            "SELECT token, debater_id, claimed FROM door_tokens "
            "WHERE door_id = ? AND token = ?",
            (door["door_id"], token)).fetchone()
        if not slot or not slot["claimed"]:
            return None
        slots = con.execute(
            "SELECT debater_id, claimed FROM door_tokens "
            "WHERE door_id = ? ORDER BY debater_id",
            (door["door_id"],)).fetchall()
        total = len(slots)
        # slot_number = position among claimed ones
        claimed_ids = [s["debater_id"] for s in slots if s["claimed"]]
        try:
            slot_number = claimed_ids.index(slot["debater_id"]) + 1
        except ValueError:
            slot_number = 1
        return {
            "ok": True,
            "rejoin": True,
            "door_id": door["door_id"],
            "debater_id": slot["debater_id"],
            "token": slot["token"],
            "part_id": door["part_id"],
            "title": door["title"],
            "slot_number": slot_number,
            "total_slots": total,
            "all_claimed": all(s["claimed"] for s in slots),
        }


def kick_debater(door_id: str, debater_id: str) -> dict:
    """Unclaim a debater slot so someone else can join.

    Owner-only action. Returns {ok, kicked_debater_id} or error.
    """
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        slot = con.execute(
            "SELECT token, debater_id, claimed FROM door_tokens "
            "WHERE door_id = ? AND debater_id = ?",
            (door_id, debater_id)).fetchone()
        if not slot:
            return {"ok": False, "error": "debater_not_found"}
        if not slot["claimed"]:
            return {"ok": False, "error": "slot_not_claimed"}
        con.execute(
            "UPDATE door_tokens SET claimed = 0 WHERE token = ?",
            (slot["token"],))
        return {"ok": True, "kicked_debater_id": debater_id}

def get_lobby(door_id: str) -> dict:
    """Return lobby info: who has joined and current phase."""
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        slots = con.execute(
            "SELECT debater_id, claimed FROM door_tokens "
            "WHERE door_id = ? ORDER BY debater_id",
            (door_id,)).fetchall()
        joined = [s["debater_id"] for s in slots if s["claimed"]]
        waiting = [s["debater_id"] for s in slots if not s["claimed"]]
        part_cfg = load_part_config(door["part_id"], CONFIGS_DIR)
        # Lobby notes count
        note_count = con.execute(
            "SELECT COUNT(*) FROM lobby_notes WHERE door_id = ?",
            (door_id,)).fetchone()[0]
        return {
            "ok": True,
            "door_id": door_id,
            "title": door["title"],
            "part_id": door["part_id"],
            "current_phase": door["current_phase"],
            "status": door["status"],
            "joined": joined,
            "waiting_for": waiting,
            "all_joined": len(waiting) == 0,
            "canonical_categories": list(part_cfg.canonical_atom_categories),
            "canonical_clusters": list(part_cfg.canonical_clusters),
            "pinned_message": LOBBY_PINNED_MESSAGE,
            "lobby_notes_count": note_count,
        }


def resolve_token(token: str) -> tuple[str, str, str] | None:
    """Return (door_id, debater_id, role) or None."""
    with db.connect() as con:
        cur = con.execute(
            "SELECT door_id, debater_id, role FROM door_tokens WHERE token = ?",
            (token,))
        r = cur.fetchone()
        return (r["door_id"], r["debater_id"], r["role"]) if r else None


def get_debater_token(door_id: str, debater_id: str) -> dict:
    """Return the CURRENT token for a debater WITHOUT rotating it.

    Use this to hand a fresh agent the live valid token instead of calling
    ``handoff_debater`` (which reissues and invalidates the previous token).
    Returns {ok, door_id, debater_id, token, role, claimed} or
    {ok: False, error: ...}.
    """
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        row = con.execute(
            "SELECT token, role, claimed FROM door_tokens "
            "WHERE door_id = ? AND debater_id = ?",
            (door_id, debater_id),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "debater_not_found"}
        return {
            "ok": True,
            "door_id": door_id,
            "debater_id": debater_id,
            "token": row["token"],
            "role": row["role"],
            "claimed": bool(row["claimed"]),
        }


# ---------- post msg ----------

def post_msg(door_id: str, debater_id: str, env: MsgEnvelope,
             token_count_reported: int = 0,
             validation_id: str | None = None) -> dict:
    """Validates envelope, writes msg (or rejection), updates atoms,
    and advances phase if eligible. Returns response dict for the route.
    """
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "violations": [{"rule": "unknown_door", "detail": door_id}]}
        if door["status"] != "open":
            return {"ok": False, "violations": [{"rule": "door_closed",
                                                 "detail": door["status"]}]}

        part_cfg = load_part_config(door["part_id"], CONFIGS_DIR)
        ctx = state_helpers.load_door_ctx(con, door_id, part_cfg)
        violations = validate_envelope(env, debater_id, ctx)

        # forced validation mode (§10)
        if door["forced_validation_mode"] and not validation_id:
            violations.append({"rule": "forced_validation_required",
                               "detail": "header X-Validation-Id missing"})

        body = env.content
        payload_dict = env.model_dump(mode="json", exclude_none=True)
        payload_json = db.jdump(payload_dict)
        payload_bytes = len(payload_json.encode("utf-8"))
        now = db.now_ts()

        if violations:
            cur = con.execute(
                "INSERT INTO msgs (door_id, debater_id, kind, payload_json, "
                "body_text, rejected_reason, payload_bytes, token_count_reported, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (door_id, debater_id, env.kind, payload_json, body,
                 db.jdump(violations[:3]), payload_bytes, token_count_reported, now))
            msg_id = cur.lastrowid
            for v in violations:
                con.execute(
                    "INSERT INTO violations (door_id, msg_id, debater_id, rule, "
                    "detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (door_id, msg_id, debater_id, v["rule"],
                     v.get("detail", "")[:500] if isinstance(v.get("detail"), str)
                     else db.jdump(v.get("detail")), now))
            # check forced_validation_mode trigger (need ≥10 attempts to avoid
            # false-positive from the very first rejection)
            tot_cur = con.execute(
                "SELECT COUNT(*) AS n FROM msgs WHERE door_id = ? "
                "AND created_at > strftime('%s','now') - 1800", (door_id,)).fetchone()
            if (tot_cur["n"] or 0) >= 10:
                rate = state_helpers.door_rejection_rate_30m(con, door_id)
                if rate > 0.15 and not door["forced_validation_mode"]:
                    con.execute(
                        "UPDATE doors SET forced_validation_mode = 1 WHERE door_id = ?",
                        (door_id,))
            return {"ok": False, "msg_id": msg_id, "violations": violations}

        # write accepted msg
        cur = con.execute(
            "INSERT INTO msgs (door_id, debater_id, kind, payload_json, body_text, "
            "rejected_reason, payload_bytes, token_count_reported, created_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (door_id, debater_id, env.kind, payload_json, body, payload_bytes,
             token_count_reported, now))
        msg_id = cur.lastrowid

        atom_id_out = None
        if env.kind == "atom-propose":
            # assign next version
            cur2 = con.execute(
                "SELECT COALESCE(MAX(version), 0) AS mx FROM atoms "
                "WHERE door_id = ? AND canonical_category_id = ?",
                (door_id, env.canonical_category_id))
            next_v = (cur2.fetchone()["mx"] or 0) + 1
            con.execute(
                "INSERT INTO atoms (door_id, canonical_category_id, version, "
                "author_debater_id, msg_id, pick, provocation_level, "
                "code_shape_anchor_json, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (door_id, env.canonical_category_id, next_v, debater_id, msg_id,
                 env.pick, env.provocation_level,
                 db.jdump(env.code_shape_anchor.model_dump() if env.code_shape_anchor else {}),
                 body))
            atom_id_out = f"atom_{env.canonical_category_id}_v{next_v}"

        if env.kind == "atom-peer-improve":
            # Create improved version of own atom (v+1)
            cat, old_ver = _split_atom_id_safe(env.target_atom_id)
            if cat:
                cur2 = con.execute(
                    "SELECT COALESCE(MAX(version), 0) AS mx FROM atoms "
                    "WHERE door_id = ? AND canonical_category_id = ?",
                    (door_id, cat))
                next_v = (cur2.fetchone()["mx"] or 0) + 1
                # Inherit pick/provocation from the target atom
                old_atom = con.execute(
                    "SELECT pick, provocation_level, code_shape_anchor_json "
                    "FROM atoms WHERE door_id = ? AND canonical_category_id = ? "
                    "AND version = ?",
                    (door_id, cat, old_ver)).fetchone()
                con.execute(
                    "INSERT INTO atoms (door_id, canonical_category_id, version, "
                    "author_debater_id, msg_id, pick, provocation_level, "
                    "code_shape_anchor_json, content) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (door_id, cat, next_v, debater_id, msg_id,
                     old_atom["pick"] if old_atom else "A",
                     old_atom["provocation_level"] if old_atom else "MEDIUM",
                     old_atom["code_shape_anchor_json"] if old_atom else "{}",
                     body))
                atom_id_out = f"atom_{cat}_v{next_v}"

        if env.kind == "atom-approve":
            cat, ver = _split_atom_id_safe(env.target_atom_id)
            if cat and ver:
                row = con.execute(
                    "SELECT atom_db_id, approved_by_json FROM atoms "
                    "WHERE door_id = ? AND canonical_category_id = ? AND version = ?",
                    (door_id, cat, ver)).fetchone()
                if row:
                    approved = json.loads(row["approved_by_json"])
                    if debater_id not in approved:
                        approved.append(debater_id)
                    con.execute(
                        "UPDATE atoms SET approved_by_json = ?, locked = ? WHERE atom_db_id = ?",
                        (db.jdump(approved),
                         1 if len(set(approved)) >= 2 else 0,
                         row["atom_db_id"]))

        phase_advanced = _maybe_advance_phase(con, door_id, part_cfg)
        return {
            "ok": True,
            "msg_id": msg_id,
            "atom_id": atom_id_out,
            "violations": [],
            "phase_advanced_to": phase_advanced,
            "reminder": (
                "ИДИ ПОКА ИЩИ СЛАБЫЕ МЕСТА И СПОСОБЫ УЛУЧИТЬ "
                "В ИНТЕРНЕТЕ ЧТО ПРЕДЛОЖИЛ. "
                "Проверяй админ-чат (GET /admin_chat) пока ждёшь."
            ),
        }


def _split_atom_id_safe(atom_id: str) -> tuple[str | None, int | None]:
    import re as _re
    m = _re.match(r"atom_(.+)_v(\d+)$", atom_id or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def _split_atom_id(atom_id: str) -> tuple[str, int]:
    cat, v = _split_atom_id_safe(atom_id)
    return cat or "", v or 0


# ---------- phase progression ----------

def _maybe_advance_phase(con, door_id: str, part_cfg: PartConfig) -> str | None:
    """Detect phase transitions and run gates. Returns new phase or None."""
    door = state_helpers.load_door_row(con, door_id)
    cur_phase = door["current_phase"]

    if cur_phase == "voice_manifest":
        # advance when both debaters posted ≥1 accepted note
        cur = con.execute(
            "SELECT COUNT(DISTINCT debater_id) AS n FROM msgs "
            "WHERE door_id = ? AND kind = 'note' AND rejected_reason IS NULL",
            (door_id,))
        if (cur.fetchone()["n"] or 0) >= 2:
            return _set_phase(con, door_id, "atoms_propose")

    if cur_phase == "atoms_propose":
        # advance to critique when each debater has ≥
        # all categories must have atom-proposes accepted
        target = len(part_cfg.canonical_atom_categories)
        ok = True
        cur = con.execute(
            "SELECT debater_id, COUNT(*) AS n FROM msgs "
            "WHERE door_id = ? AND kind = 'atom-propose' AND rejected_reason IS NULL "
            "GROUP BY debater_id", (door_id,))
        counts = {r["debater_id"]: r["n"] for r in cur.fetchall()}
        if len(counts) < 2 or any(v < target for v in counts.values()):
            ok = False
        if ok:
            return _set_phase(con, door_id, "atoms_critique")

    if cur_phase == "atoms_critique":
        # advance to revision when each debater wrote critique on ≥ N% of
        # opponent atoms (≥80%)
        ok = _opponent_critique_coverage(con, door_id) >= 0.95
        if ok:
            return _set_phase(con, door_id, "atoms_revision")

    if cur_phase == "atoms_revision":
        # advance to peer_improve when ≥95% of own atoms have a revision msg
        ok = _own_revision_coverage(con, door_id) >= 0.95
        if ok:
            return _set_phase(con, door_id, "atoms_peer_improve")

    if cur_phase == "atoms_peer_improve":
        # advance to adversarial when both debaters have:
        # 1) posted peer-improve for ≥95% of their own atoms
        # 2) posted peer-review for ≥95% of opponent's peer-improves
        ok = _peer_improve_coverage(con, door_id) >= 0.95
        if ok:
            return _set_phase(con, door_id, "atoms_adversarial")

    if cur_phase == "atoms_adversarial":
        # advance to approve when ≥80% of opponent atoms have ≥1 adversarial
        ok = _opponent_adversarial_coverage(con, door_id) >= 0.95
        if ok:
            return _set_phase(con, door_id, "atoms_approve")

    if cur_phase == "atoms_approve":
        # advance to phase_gate_a when ≥80% of atom_<cat>_vN are bilaterally
        # locked (last version per category counted)
        ok = _latest_locked_coverage(con, door_id) >= 0.95
        if ok:
            return _run_gate_and_advance(
                con, door_id, "phase_gate_a",
                acceptance.phase_gate_a, "clusters_propose",
                "atoms_revision", part_cfg)

    if cur_phase == "clusters_propose":
        ok = _each_debater_clusters(con, door_id, part_cfg) >= part_cfg.min_final_briefs
        if ok:
            return _set_phase(con, door_id, "clusters_critique")

    if cur_phase == "clusters_critique":
        ok = _cluster_critique_coverage(con, door_id) >= 0.95
        if ok:
            return _set_phase(con, door_id, "clusters_revision")

    if cur_phase == "clusters_revision":
        ok = _cluster_revision_coverage(con, door_id) >= 0.95
        if ok:
            return _set_phase(con, door_id, "clusters_approve")

    if cur_phase == "clusters_approve":
        ok = _cluster_approve_coverage(con, door_id) >= 0.95
        if ok:
            return _run_gate_and_advance(
                con, door_id, "phase_gate_b",
                acceptance.phase_gate_b, "final_propose",
                "clusters_revision", part_cfg)

    if cur_phase == "final_propose":
        cur = con.execute(
            "SELECT COUNT(DISTINCT debater_id) AS n FROM msgs "
            "WHERE door_id = ? AND kind = 'final-propose' AND rejected_reason IS NULL",
            (door_id,))
        if (cur.fetchone()["n"] or 0) >= 2:
            return _set_phase(con, door_id, "final_critique")

    if cur_phase == "final_critique":
        cur = con.execute(
            "SELECT COUNT(DISTINCT debater_id) AS n FROM msgs "
            "WHERE door_id = ? AND kind = 'final-critique' AND rejected_reason IS NULL",
            (door_id,))
        if (cur.fetchone()["n"] or 0) >= 2:
            return _set_phase(con, door_id, "final_revision")

    if cur_phase == "final_revision":
        cur = con.execute(
            "SELECT COUNT(DISTINCT debater_id) AS n FROM msgs "
            "WHERE door_id = ? AND kind = 'final-revision' AND rejected_reason IS NULL",
            (door_id,))
        if (cur.fetchone()["n"] or 0) >= 2:
            return _set_phase(con, door_id, "final_approve")

    if cur_phase == "final_approve":
        cur = con.execute(
            "SELECT COUNT(DISTINCT debater_id) AS n FROM msgs "
            "WHERE door_id = ? AND kind = 'final-approve' AND rejected_reason IS NULL",
            (door_id,))
        if (cur.fetchone()["n"] or 0) >= 2:
            # run phase_gate_c
            passed, report = acceptance.phase_gate_c(con, door_id, part_cfg, REFERENCE_DIR)
            con.execute(
                "INSERT INTO phase_gates (door_id, phase, status, report_json, ran_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (door_id, "phase_gate_c", "passed" if passed else "failed",
                 db.jdump(report), db.now_ts()))
            if passed:
                con.execute("UPDATE doors SET current_phase = 'done', status = 'closed' "
                            "WHERE door_id = ?", (door_id,))
                return "done"
            else:
                con.execute("UPDATE doors SET current_phase = 'final_revision' "
                            "WHERE door_id = ?", (door_id,))
                return "final_revision"

    return None


def _set_phase(con, door_id: str, new_phase: str) -> str:
    con.execute("UPDATE doors SET current_phase = ? WHERE door_id = ?",
                (new_phase, door_id))
    return new_phase


def _run_gate_and_advance(con, door_id: str, gate_name: str, gate_fn,
                          next_phase: str, rollback_phase: str,
                          part_cfg: PartConfig) -> str:
    passed, report = gate_fn(con, door_id, part_cfg)
    con.execute(
        "INSERT INTO phase_gates (door_id, phase, status, report_json, ran_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (door_id, gate_name, "passed" if passed else "failed",
         db.jdump(report), db.now_ts()))
    target = next_phase if passed else rollback_phase
    con.execute("UPDATE doors SET current_phase = ? WHERE door_id = ?",
                (target, door_id))
    return target


# ---------- coverage helpers ----------

def _opponent_critique_coverage(con, door_id: str) -> float:
    """Only the latest version per (author, category) counts."""
    cur = con.execute(
        "SELECT author_debater_id, canonical_category_id, version FROM atoms "
        "WHERE door_id = ?", (door_id,))
    # Track latest version per (debater, category)
    latest: dict[tuple[str, str], int] = {}
    for r in cur.fetchall():
        key = (r["author_debater_id"], r["canonical_category_id"])
        if r["version"] > latest.get(key, -1):
            latest[key] = r["version"]
    all_atom_ids: list[tuple[str, str]] = []
    for (deb, cat), ver in latest.items():
        atom_id = f"atom_{cat}_v{ver}"
        all_atom_ids.append((deb, atom_id))
    targets_per_critic: dict[str, set[str]] = {}
    cur2 = con.execute(
        "SELECT debater_id, payload_json FROM msgs WHERE door_id = ? "
        "AND kind = 'atom-critique' AND rejected_reason IS NULL", (door_id,))
    for r in cur2.fetchall():
        p = json.loads(r["payload_json"])
        if p.get("target_atom_id"):
            targets_per_critic.setdefault(r["debater_id"], set()).add(
                p["target_atom_id"])
    if not all_atom_ids:
        return 0.0
    covered, total = 0, 0
    for author, atom_id in all_atom_ids:
        for critic, targets in targets_per_critic.items():
            if critic != author:
                total += 1
                if atom_id in targets:
                    covered += 1
    return covered / max(total, 1)


def _own_revision_coverage(con, door_id: str) -> float:
    """Check that each debater revised their own atoms.

    Only the *latest* version per (author, category) counts — older
    superseded atoms are ignored so they don't block phase progression.
    """
    cur = con.execute(
        "SELECT author_debater_id, canonical_category_id, version FROM atoms "
        "WHERE door_id = ?", (door_id,))
    # Track latest version per (debater, category)
    latest: dict[tuple[str, str], int] = {}
    for r in cur.fetchall():
        key = (r["author_debater_id"], r["canonical_category_id"])
        if r["version"] > latest.get(key, -1):
            latest[key] = r["version"]
    # Build own_atoms using only latest versions
    own_atoms: dict[str, set[str]] = {}
    for (deb, cat), ver in latest.items():
        atom_id = f"atom_{cat}_v{ver}"
        own_atoms.setdefault(deb, set()).add(atom_id)
    if not own_atoms:
        return 0.0
    cur2 = con.execute(
        "SELECT debater_id, payload_json FROM msgs WHERE door_id = ? "
        "AND kind = 'atom-revision' AND rejected_reason IS NULL", (door_id,))
    rev_targets: dict[str, set[str]] = {}
    for r in cur2.fetchall():
        p = json.loads(r["payload_json"])
        if p.get("target_atom_id"):
            rev_targets.setdefault(r["debater_id"], set()).add(p["target_atom_id"])
    total = sum(len(s) for s in own_atoms.values())
    covered = sum(len(own_atoms[d] & rev_targets.get(d, set())) for d in own_atoms)
    return covered / max(total, 1)


def _opponent_adversarial_coverage(con, door_id: str) -> float:
    """Only the latest version per (author, category) counts."""
    cur = con.execute(
        "SELECT author_debater_id, canonical_category_id, version FROM atoms "
        "WHERE door_id = ?", (door_id,))
    # Track latest version per (debater, category)
    latest: dict[tuple[str, str], int] = {}
    for r in cur.fetchall():
        key = (r["author_debater_id"], r["canonical_category_id"])
        if r["version"] > latest.get(key, -1):
            latest[key] = r["version"]
    by_author: dict[str, set[str]] = {}
    for (deb, cat), ver in latest.items():
        atom_id = f"atom_{cat}_v{ver}"
        by_author.setdefault(deb, set()).add(atom_id)
    cur2 = con.execute(
        "SELECT debater_id, payload_json FROM msgs WHERE door_id = ? "
        "AND kind = 'atom-adversarial' AND rejected_reason IS NULL", (door_id,))
    adv_by_critic: dict[str, set[str]] = {}
    for r in cur2.fetchall():
        p = json.loads(r["payload_json"])
        if p.get("target_atom_id"):
            adv_by_critic.setdefault(r["debater_id"], set()).add(p["target_atom_id"])
    if not by_author:
        return 0.0
    total, covered = 0, 0
    for author, atoms in by_author.items():
        for critic, targets in adv_by_critic.items():
            if critic == author:
                continue
            total += len(atoms)
            covered += len(atoms & targets)
    return covered / max(total, 1)


def _peer_improve_coverage(con, door_id: str) -> float:
    """Combined coverage for atoms_peer_improve phase.

    Measures two things:
    1. Each debater posted atom-peer-improve for their own atoms
    2. Each debater posted atom-peer-review for opponent's peer-improves

    Returns the minimum of both coverages so both must be ≥ threshold.
    """
    # Find latest atoms per (debater, category) BEFORE this phase
    # (atoms that existed at entry into atoms_peer_improve)
    cur = con.execute(
        "SELECT author_debater_id, canonical_category_id, version FROM atoms "
        "WHERE door_id = ?", (door_id,))
    latest: dict[tuple[str, str], int] = {}
    for r in cur.fetchall():
        key = (r["author_debater_id"], r["canonical_category_id"])
        if r["version"] > latest.get(key, -1):
            latest[key] = r["version"]

    # Group categories by debater
    debater_cats: dict[str, set[str]] = {}
    for (deb, cat), ver in latest.items():
        debater_cats.setdefault(deb, set()).add(cat)

    debaters = list(debater_cats.keys())
    if len(debaters) < 2:
        return 0.0

    # 1. Check peer-improve coverage: each debater improved their own atoms
    improve_targets: dict[str, set[str]] = {}  # debater -> set of target_atom_ids
    cur2 = con.execute(
        "SELECT debater_id, payload_json FROM msgs WHERE door_id = ? "
        "AND kind = 'atom-peer-improve' AND rejected_reason IS NULL", (door_id,))
    for r in cur2.fetchall():
        p = json.loads(r["payload_json"])
        if p.get("target_atom_id"):
            improve_targets.setdefault(r["debater_id"], set()).add(
                p["target_atom_id"])

    improve_total = 0
    improve_covered = 0
    for deb in debaters:
        cats = debater_cats.get(deb, set())
        targets = improve_targets.get(deb, set())
        for cat in cats:
            improve_total += 1
            # Check if ANY version of this category was targeted
            for (d, c), ver in latest.items():
                if d == deb and c == cat:
                    atom_id = f"atom_{c}_v{ver}"
                    if atom_id in targets:
                        improve_covered += 1
                    break

    improve_cov = improve_covered / max(improve_total, 1)

    # 2. Check peer-review coverage: each debater reviewed opponent's improves
    review_targets: dict[str, set[str]] = {}
    cur3 = con.execute(
        "SELECT debater_id, payload_json FROM msgs WHERE door_id = ? "
        "AND kind = 'atom-peer-review' AND rejected_reason IS NULL", (door_id,))
    for r in cur3.fetchall():
        p = json.loads(r["payload_json"])
        if p.get("target_atom_id"):
            review_targets.setdefault(r["debater_id"], set()).add(
                p["target_atom_id"])

    # For each peer-improve, check if the opponent reviewed it
    review_total = 0
    review_covered = 0
    for improver, targets in improve_targets.items():
        for target_id in targets:
            review_total += 1
            # Check that someone OTHER than the improver reviewed this
            for reviewer, reviewed in review_targets.items():
                if reviewer != improver and target_id in reviewed:
                    review_covered += 1
                    break

    review_cov = review_covered / max(review_total, 1)

    # Both must be satisfied — return the minimum
    # If no improves yet, return 0
    if improve_total == 0:
        return 0.0
    return min(improve_cov, review_cov)


def _latest_locked_coverage(con, door_id: str) -> float:
    cur = con.execute(
        """SELECT canonical_category_id, MAX(version) AS mx FROM atoms
           WHERE door_id = ? GROUP BY canonical_category_id""", (door_id,))
    latest = {r["canonical_category_id"]: r["mx"] for r in cur.fetchall()}
    if not latest:
        return 0.0
    locked = 0
    for cat, ver in latest.items():
        row = con.execute(
            "SELECT locked FROM atoms WHERE door_id = ? AND canonical_category_id = ? "
            "AND version = ?", (door_id, cat, ver)).fetchone()
        if row and row["locked"]:
            locked += 1
    return locked / len(latest)


def _each_debater_clusters(con, door_id: str, part_cfg: PartConfig) -> int:
    cur = con.execute(
        "SELECT debater_id, COUNT(DISTINCT json_extract(payload_json, '$.cluster_id')) AS n "
        "FROM msgs WHERE door_id = ? AND kind = 'cluster-propose' "
        "AND rejected_reason IS NULL GROUP BY debater_id", (door_id,))
    counts = [r["n"] for r in cur.fetchall()]
    if len(counts) < 2:
        return 0
    return min(counts)


def _cluster_critique_coverage(con, door_id: str) -> float:
    cur = con.execute(
        "SELECT COUNT(DISTINCT debater_id) AS n FROM msgs WHERE door_id = ? "
        "AND kind = 'cluster-critique' AND rejected_reason IS NULL", (door_id,))
    return 1.0 if (cur.fetchone()["n"] or 0) >= 2 else 0.0


def _cluster_revision_coverage(con, door_id: str) -> float:
    cur = con.execute(
        "SELECT COUNT(DISTINCT debater_id) AS n FROM msgs WHERE door_id = ? "
        "AND kind = 'cluster-revision' AND rejected_reason IS NULL", (door_id,))
    return 1.0 if (cur.fetchone()["n"] or 0) >= 2 else 0.0


def _cluster_approve_coverage(con, door_id: str) -> float:
    cur = con.execute(
        "SELECT COUNT(DISTINCT debater_id) AS n FROM msgs WHERE door_id = ? "
        "AND kind = 'cluster-approve' AND rejected_reason IS NULL", (door_id,))
    return 1.0 if (cur.fetchone()["n"] or 0) >= 2 else 0.0


# ---------- events feed (for lilush bot UI) ----------

def list_events(door_id: str, since_msg_id: int = 0, limit: int = 200) -> dict:
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        cur = con.execute(
            """SELECT msg_id, debater_id, kind, rejected_reason, payload_bytes,
                      created_at,
                      CASE WHEN length(body_text) > 240
                           THEN substr(body_text, 1, 240) || '...'
                           ELSE body_text END AS preview
               FROM msgs WHERE door_id = ? AND msg_id > ?
               ORDER BY msg_id ASC LIMIT ?""",
            (door_id, since_msg_id, limit))
        events = []
        for r in cur.fetchall():
            events.append({
                "msg_id": r["msg_id"],
                "debater_id": r["debater_id"],
                "kind": r["kind"],
                "rejected": r["rejected_reason"] is not None,
                "rejected_reason": db.jload(r["rejected_reason"]) if r["rejected_reason"] else None,
                "payload_bytes": r["payload_bytes"],
                "ts": r["created_at"],
                "preview": r["preview"],
            })
        return {
            "ok": True,
            "door_id": door_id,
            "current_phase": door["current_phase"],
            "status": door["status"],
            "events": events,
            "last_msg_id": events[-1]["msg_id"] if events else since_msg_id,
        }


# ---------- state snapshot ----------

def get_state_snapshot(door_id: str) -> dict:
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        part_cfg = load_part_config(door["part_id"], CONFIGS_DIR)
        stats = state_helpers.load_per_debater_stats(con, door_id)
        # rejection counts
        rej_cur = con.execute(
            "SELECT COUNT(*) AS tot, "
            "SUM(CASE WHEN rejected_reason IS NULL THEN 0 ELSE 1 END) AS rej "
            "FROM msgs WHERE door_id = ?", (door_id,))
        rej_row = rej_cur.fetchone()
        msgs_total = rej_row["tot"] or 0
        rej_total = rej_row["rej"] or 0
        # atoms
        atoms = state_helpers.load_atoms_by_id(con, door_id)
        atoms_by_cat: dict[str, dict] = {}
        for atom_id, a in atoms.items():
            cat = a["category"]
            slot = atoms_by_cat.setdefault(cat, {"versions": [], "locked_version": None})
            slot["versions"].append({
                "atom_id": atom_id, "v": a["version"], "author": a["author_debater_id"],
                "pick": a["pick"], "approved_by": a["approved_by"], "locked": a["locked"],
            })
            if a["locked"]:
                slot["locked_version"] = a["version"]
        # disagree at first pick per category
        for cat, slot in atoms_by_cat.items():
            first_picks: dict[str, str] = {}
            for v in slot["versions"]:
                if v["author"] not in first_picks:
                    first_picks[v["author"]] = v["pick"]
            slot["disagree_at_first_pick"] = (
                len(first_picks) >= 2 and len(set(first_picks.values())) > 1)
        # phase gates last reports
        gates_cur = con.execute(
            """SELECT phase, status, report_json, ran_at FROM phase_gates
               WHERE door_id = ? ORDER BY ran_at DESC""", (door_id,))
        latest_gates: dict[str, dict] = {}
        for r in gates_cur.fetchall():
            if r["phase"] not in latest_gates:
                latest_gates[r["phase"]] = {
                    "status": r["status"],
                    "report": json.loads(r["report_json"]),
                    "ran_at": r["ran_at"],
                }
        # recent violations
        vio_cur = con.execute(
            "SELECT msg_id, rule, detail FROM violations WHERE door_id = ? "
            "ORDER BY violation_id DESC LIMIT 20", (door_id,))
        violations_recent = [
            {"msg_id": r["msg_id"], "rule": r["rule"], "detail": r["detail"]}
            for r in vio_cur.fetchall()]

        return {
            "ok": True,
            "schema_v": 3,
            "door_id": door_id,
            "title": door["title"],
            "part": door["part_id"],
            "current_phase": door["current_phase"],
            "status": door["status"],
            "judge_mode": (door["judge_mode"] if "judge_mode" in door.keys() else "self") or "self",
            "forced_validation_mode": bool(door["forced_validation_mode"]),
            "counters": {
                "messages_total": msgs_total,
                "rejections_total": rej_total,
                "rejection_rate_total": (rej_total / msgs_total) if msgs_total else 0.0,
                "per_debater": stats,
            },
            "atoms": atoms_by_cat,
            "phase_gates": latest_gates,
            "violations_recent": violations_recent,
            "canonical_categories": list(part_cfg.canonical_atom_categories),
            "canonical_clusters": list(part_cfg.canonical_clusters),
        }


# ── Handoff: let a new agent replace a disconnected debater ──────────────────


def handoff_debater(door_id: str, old_debater_id: str, new_debater_id: str | None = None) -> dict:
    """Transfer a debater slot to a new agent.

    If ``new_debater_id`` is None, keep the same ID (just reissue token).
    Returns new token + full state snapshot so the replacement can continue.
    """
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        if door["status"] not in ("open", "paused"):
            return {"ok": False, "error": f"door_status_{door['status']}"}

        tok_row = con.execute(
            "SELECT token, debater_id, role FROM door_tokens "
            "WHERE door_id = ? AND debater_id = ?",
            (door_id, old_debater_id),
        ).fetchone()
        if not tok_row:
            return {"ok": False, "error": "debater_not_found"}

        new_id = new_debater_id or old_debater_id
        new_token = secrets.token_urlsafe(24)

        # Update token row
        con.execute(
            "UPDATE door_tokens SET token = ?, debater_id = ? "
            "WHERE door_id = ? AND debater_id = ?",
            (new_token, new_id, door_id, old_debater_id),
        )

        # If debater_id changed, update all msgs references
        if new_id != old_debater_id:
            con.execute(
                "UPDATE msgs SET debater_id = ? WHERE door_id = ? AND debater_id = ?",
                (new_id, door_id, old_debater_id),
            )
            con.execute(
                "UPDATE atoms SET author_debater_id = ? WHERE door_id = ? AND author_debater_id = ?",
                (new_id, door_id, old_debater_id),
            )
        con.commit()

    # Return full state so new agent can resume
    snapshot = get_state_snapshot(door_id)
    snapshot["handoff"] = {
        "old_debater_id": old_debater_id,
        "new_debater_id": new_id,
        "new_token": new_token,
    }
    return snapshot


# ── Judge mode toggle ────────────────────────────────────────────────────────


def set_judge_mode(door_id: str, mode: str) -> dict:
    """Toggle between 'judge' and 'self' regulation mode.

    'self' = enforcement.py rules only (default).
    'judge' = enforcement + external AI judge reviews.
    """
    if mode not in ("judge", "self"):
        return {"ok": False, "error": "invalid_mode; use 'judge' or 'self'"}
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        con.execute("UPDATE doors SET judge_mode = ? WHERE door_id = ?",
                    (mode, door_id))
        con.commit()
    return {"ok": True, "door_id": door_id, "judge_mode": mode}


def get_judge_mode(door_id: str) -> str:
    """Return current judge mode for a door."""
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return "self"
        return (door["judge_mode"] if "judge_mode" in door.keys() else "self") or "self"


# ── Lobby notes (critique chat) ──────────────────────────────────────────────

# Pinned lobby message — shown to all debaters via /lobby
LOBBY_PINNED_MESSAGE = (
    "📌 ЕСЛИ ДЕБАТЫ ВСТАЛИ:\n\n"
    "Если дебаты встали и вы видите что не продвинуться — "
    "читайте ПРАВИЛА и БРИФИНГ ЦЕЛИКОМ, а не кусками ради контекста.\n\n"
    "Велика вероятность что вы забыли либо прочитали что-то не то. "
    "Если ваш оппонент не умеет читать — вы можете взять конкретный "
    "отрезок что он опустил из правил и брифинга и написать тут в "
    "лобби-чат (POST /lobby_note) чтобы он обратил внимание.\n\n"
    "⚠️ Лобби-чат ОБЯЗАТЕЛЕН к прочтению если дебаты стоят более 30 минут."
)


def post_lobby_note(door_id: str, debater_id: str, content: str) -> dict:
    """Post a critique/meta note to the lobby chat."""
    if not content or len(content.strip()) < 10:
        return {"ok": False, "error": "content too short (min 10 chars)"}
    if len(content) > 8000:
        return {"ok": False, "error": "content too long (max 8000 chars)"}
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        ts = db.now_ts()
        con.execute(
            "INSERT INTO lobby_notes (door_id, debater_id, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (door_id, debater_id, content.strip(), ts))
        con.commit()
        note_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"ok": True, "note_id": note_id, "ts": ts}


def get_lobby_notes(door_id: str, limit: int = 50) -> dict:
    """Get lobby critique chat messages."""
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        cur = con.execute(
            "SELECT note_id, debater_id, content, created_at FROM lobby_notes "
            "WHERE door_id = ? ORDER BY created_at DESC LIMIT ?",
            (door_id, limit))
        notes = [
            {"note_id": r[0], "debater_id": r[1],
             "content": r[2], "ts": r[3]}
            for r in cur.fetchall()
        ]
    notes.reverse()  # oldest first
    return {
        "ok": True,
        "door_id": door_id,
        "pinned_message": LOBBY_PINNED_MESSAGE,
        "notes": notes,
        "count": len(notes),
    }
