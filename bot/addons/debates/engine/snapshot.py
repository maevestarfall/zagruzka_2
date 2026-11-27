"""Snapshot / result-export / catch-up helpers for debate-server-v3.

Three capabilities used by the owner control panel:

1. ``build_result_markdown(door_id)`` — the *actual result* of the debate so
   far (current best answer per category), NOT the raw message logs.
2. ``export_snapshot`` / ``restore_snapshot`` — freeze the whole debate to a
   portable JSON and reload it after a redeploy so the server continues from
   exactly the frozen moment.
3. catch-up state (``resume_state`` table) — after a restore the debaters must,
   one by one, post the work they accumulated while the debate was paused
   (researched improvements) *before* the debate continues.  The status signal
   surfaces this ("идёт дозагрузка наработок").
"""

from __future__ import annotations

import json

from . import db, state_helpers

# Tables that belong to a single door, in FK-safe insert order
# (parents before children).  Delete order is the reverse.
_DOOR_TABLES = [
    "doors",
    "door_tokens",
    "msgs",
    "atoms",
    "violations",
    "phase_gates",
    "owner_directives",
    "validations",
    "lobby_notes",
    "admin_chat",
]

SNAPSHOT_SCHEMA_V = 1


# ───────────────────────── catch-up state table ──────────────────────────────

def ensure_resume_table(con) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS resume_state ("
        " door_id TEXT PRIMARY KEY,"
        " catchup_queue TEXT NOT NULL DEFAULT '[]',"
        " catchup_active INTEGER NOT NULL DEFAULT 0,"
        " updated_at REAL NOT NULL)")


def _debater_ids(con, door_id: str) -> list[str]:
    """Debaters that actually participated (have ≥1 accepted message) — these
    are the ones who must post catch-up work.  Avoids stalling on empty/claimed
    but inactive slots (e.g. a 3rd door that never joined)."""
    cur = con.execute(
        "SELECT DISTINCT debater_id FROM msgs WHERE door_id = ? "
        "AND rejected_reason IS NULL AND debater_id != 'owner' "
        "ORDER BY debater_id", (door_id,))
    ids = [r["debater_id"] for r in cur.fetchall()]
    if ids:
        return ids
    # fall back to atom authors, then to claimed debater tokens
    cur = con.execute(
        "SELECT DISTINCT author_debater_id FROM atoms WHERE door_id = ? "
        "ORDER BY author_debater_id", (door_id,))
    ids = [r["author_debater_id"] for r in cur.fetchall()]
    if ids:
        return ids
    cur = con.execute(
        "SELECT DISTINCT debater_id FROM door_tokens "
        "WHERE door_id = ? AND role = 'debater' ORDER BY debater_id",
        (door_id,))
    return [r["debater_id"] for r in cur.fetchall()]


def get_catchup(door_id: str) -> dict:
    with db.connect() as con:
        ensure_resume_table(con)
        row = con.execute(
            "SELECT catchup_queue, catchup_active FROM resume_state "
            "WHERE door_id = ?", (door_id,)).fetchone()
    if not row or not row["catchup_active"]:
        return {"active": False, "queue": [], "waiting_for": None}
    queue = json.loads(row["catchup_queue"] or "[]")
    return {
        "active": bool(queue),
        "queue": queue,
        "waiting_for": queue[0] if queue else None,
    }


def set_catchup(door_id: str, queue: list[str]) -> dict:
    with db.connect() as con:
        ensure_resume_table(con)
        con.execute(
            "INSERT INTO resume_state (door_id, catchup_queue, catchup_active, "
            "updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(door_id) DO UPDATE SET catchup_queue = excluded.catchup_queue, "
            "catchup_active = excluded.catchup_active, updated_at = excluded.updated_at",
            (door_id, db.jdump(queue), 1 if queue else 0, db.now_ts()))
    return get_catchup(door_id)


def catchup_post(door_id: str, debater_id: str, text: str) -> dict:
    """Record a debater's accumulated-work note and pop them from the queue.

    Returns {ok, accepted, catchup:{...}}.  ``accepted`` is False (with a
    ``reason``) when it is not this debater's turn.
    """
    with db.connect() as con:
        ensure_resume_table(con)
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return {"ok": False, "error": "unknown_door"}
        row = con.execute(
            "SELECT catchup_queue, catchup_active FROM resume_state "
            "WHERE door_id = ?", (door_id,)).fetchone()
        queue = json.loads(row["catchup_queue"]) if row else []
        active = bool(row and row["catchup_active"] and queue)
        if not active:
            return {"ok": True, "accepted": False,
                    "reason": "catchup_not_active",
                    "catchup": {"active": False, "queue": [], "waiting_for": None}}
        if queue[0] != debater_id:
            return {"ok": True, "accepted": False,
                    "reason": "not_your_turn",
                    "catchup": {"active": True, "queue": queue,
                                "waiting_for": queue[0]}}
        now = db.now_ts()
        body = (text or "").strip() or "(пустая дозагрузка)"
        con.execute(
            "INSERT INTO msgs (door_id, debater_id, kind, payload_json, "
            "body_text, rejected_reason, payload_bytes, token_count_reported, "
            "created_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (door_id, debater_id, "note",
             db.jdump({"kind": "note", "catchup": True, "content": body}),
             body[:6000], len(body.encode("utf-8")), 0, now))
        queue = queue[1:]
        con.execute(
            "UPDATE resume_state SET catchup_queue = ?, catchup_active = ?, "
            "updated_at = ? WHERE door_id = ?",
            (db.jdump(queue), 1 if queue else 0, now, door_id))
    return {"ok": True, "accepted": True, "catchup": get_catchup(door_id)}


# ───────────────────────────── result export ─────────────────────────────────

def build_result_markdown(door_id: str) -> str | None:
    """Human-readable *result* of the debate so far: current best answer per
    category (latest atom version per author), not the message log."""
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return None
        cur = con.execute(
            "SELECT author_debater_id, canonical_category_id, version, content, "
            "locked FROM atoms WHERE door_id = ? ORDER BY canonical_category_id, "
            "author_debater_id, version", (door_id,))
        latest: dict[tuple[str, str], dict] = {}
        for r in cur.fetchall():
            key = (r["author_debater_id"], r["canonical_category_id"])
            if key not in latest or r["version"] > latest[key]["version"]:
                latest[key] = {"version": r["version"], "content": r["content"],
                               "locked": r["locked"]}
    cats = sorted({c for (_, c) in latest})
    authors = sorted({a for (a, _) in latest})
    import datetime
    lines = [
        f"# Результат дебатов — {door['title']}",
        f"_Снимок: {datetime.datetime.now():%Y-%m-%d %H:%M}. "
        f"Фаза: {door['current_phase']} · Статус: {door['status']}._",
        "",
        "Текущий лучший ответ каждого дебатёра по каждой категории ТЗ "
        "(последняя версия атома). Это результат на данный момент, не логи.",
        "",
    ]
    if not cats:
        lines.append("_Пока нет ни одного атома (дебаты ещё не дошли до ответов)._")
    for cat in cats:
        lines.append(f"\n## {cat.replace('_', ' ').title()}")
        for author in authors:
            a = latest.get((author, cat))
            if a:
                lock = " 🔒" if a["locked"] else ""
                lines.append(f"\n### {author} — v{a['version']}{lock}")
                lines.append((a["content"] or "").strip())
        lines.append("")
    return "\n".join(lines)


# ──────────────────────────── snapshot export ────────────────────────────────

def export_snapshot(door_id: str) -> dict | None:
    with db.connect() as con:
        ensure_resume_table(con)
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            return None
        tables: dict[str, list[dict]] = {}
        for table in _DOOR_TABLES:
            cur = con.execute(f"SELECT * FROM {table} WHERE door_id = ?",
                              (door_id,))
            tables[table] = [dict(r) for r in cur.fetchall()]
        rs = con.execute("SELECT catchup_queue, catchup_active FROM resume_state "
                         "WHERE door_id = ?", (door_id,)).fetchone()
    return {
        "snapshot_schema_v": SNAPSHOT_SCHEMA_V,
        "door_id": door_id,
        "exported_at": db.now_ts(),
        "current_phase": door["current_phase"],
        "tables": tables,
        "resume_state": (
            {"catchup_queue": json.loads(rs["catchup_queue"]),
             "catchup_active": bool(rs["catchup_active"])}
            if rs else None),
    }


def restore_snapshot(snapshot: dict, start_catchup: bool = True) -> dict:
    """Recreate a door (and all its rows) from a snapshot and, by default, arm
    the catch-up round so debaters post their accumulated work before resuming.
    """
    if not isinstance(snapshot, dict) or "tables" not in snapshot:
        return {"ok": False, "error": "bad_snapshot"}
    door_rows = snapshot["tables"].get("doors") or []
    if not door_rows:
        return {"ok": False, "error": "snapshot_has_no_door"}
    door_id = door_rows[0]["door_id"]
    with db.connect() as con:
        ensure_resume_table(con)
        # Wipe any existing rows for this door (replace), children first.
        con.execute("PRAGMA foreign_keys=OFF")
        for table in reversed(_DOOR_TABLES):
            con.execute(f"DELETE FROM {table} WHERE door_id = ?", (door_id,))
        con.execute("DELETE FROM resume_state WHERE door_id = ?", (door_id,))
        # Re-insert in FK-safe order, preserving primary keys.
        for table in _DOOR_TABLES:
            for row in snapshot["tables"].get(table, []):
                cols = list(row.keys())
                placeholders = ",".join("?" for _ in cols)
                collist = ",".join(cols)
                con.execute(
                    f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
                    [row[c] for c in cols])
        con.execute("PRAGMA foreign_keys=ON")
        queue = _debater_ids(con, door_id) if start_catchup else []
        con.execute(
            "INSERT INTO resume_state (door_id, catchup_queue, catchup_active, "
            "updated_at) VALUES (?, ?, ?, ?)",
            (door_id, db.jdump(queue), 1 if queue else 0, db.now_ts()))
    counts = {t: len(snapshot["tables"].get(t, [])) for t in _DOOR_TABLES}
    # Surface the door's join_secret + phase so the bot UI can re-point its
    # door slot at the restored door (otherwise the menu keeps pointing at a
    # stale/empty door after a restart).
    door_row0 = door_rows[0]
    return {"ok": True, "door_id": door_id, "restored": counts,
            "join_secret": door_row0.get("join_secret"),
            "current_phase": door_row0.get("current_phase"),
            "catchup": get_catchup(door_id)}
