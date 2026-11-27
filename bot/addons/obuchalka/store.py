# «🎓 Обучалка» — сосуд знаний: дерево листиков, учителя по ссылке, ученик Qwen.
"""State & storage for the «Обучалка» addon.

Two layers, mirroring the rest of the project:

* Tokens / role flags / Qwen endpoint — in ``state.json`` via
  :mod:`bot.addons.state` (key ``addons.obuchalka``), like guide/projectmap.
* The knowledge tree itself («сосуд») — SQLite at ``data/obuchalka.db``:
  it must survive bot restarts AND teacher rotations, so it never lives in
  a chat session or in an external AI's context window.

Roles:
* ``teacher``  — writes tasks/content, runs the student, scores leaves.
* ``revisor``  — read-only on knowledge; may score and leave notes so the
  vessel stays «в правильном русле».
The student (Qwen) has NO token at all — it cannot touch the vessel.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .. import state as addon_state

ADDON = "obuchalka"

ROLES = ("teacher", "revisor")

# Leaf lifecycle: empty → draft → review → foundation (score 10, locked).
STATUS_EMPTY = "empty"
STATUS_DRAFT = "draft"
STATUS_REVIEW = "review"
STATUS_FOUNDATION = "foundation"

DEFAULT_CONTEXT_BUDGET = 2000  # chars of context handed to the student per run
DEFAULT_QWEN_MODEL = "qwen2.5:14b"
HANDOFF_LIMIT = 5000  # chars — the «бриф» must fit a fresh teacher's first read


# ---------------------------------------------------------------- tokens ----

def get_token(role: str) -> str:
    return str(addon_state.get(ADDON, f"{role}_token", "") or "")


def is_enabled(role: str) -> bool:
    return bool(addon_state.get(ADDON, f"{role}_enabled", False))


def _resolve_public_base() -> str:
    try:
        from ...config import resolve_public_url
        base = resolve_public_url()
    except Exception:  # noqa: BLE001
        base = ""
    if base:
        return base
    # Same bug as buttons 18-23 (vision/projectmap/test): a *relative* link
    # (``/obuchalka/<token>/``) is rejected by Telegram (BUTTON_URL_INVALID)
    # when used in a url-button — see wizard.py «Открыть панель» — which
    # silently kills the «🎓 Обучалка» screen. Always return an absolute URL.
    try:
        from ...config import PORT as _port
    except Exception:  # noqa: BLE001
        _port = 8080
    logging.getLogger(__name__).warning(
        "obuchalka: PUBLIC_URL is not configured — falling back to "
        "http://127.0.0.1:%s. The link will only work on this machine; "
        "set PUBLIC_URL for production.", _port,
    )
    return f"http://127.0.0.1:{_port}"


def get_link(role: str, base_url: str | None = None) -> str:
    token = get_token(role)
    if not token:
        return ""
    base = (base_url if base_url is not None else _resolve_public_base()).rstrip("/")
    return f"{base}/obuchalka/{token}/" if base else f"/obuchalka/{token}/"


def generate_link(role: str, base_url: str | None = None) -> tuple[str, str]:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    token = secrets.token_urlsafe(24)
    addon_state.set_(ADDON, f"{role}_token", token)
    addon_state.set_(ADDON, f"{role}_enabled", True)
    addon_state.set_(ADDON, f"{role}_rotated_at", int(time.time()))
    return token, get_link(role, base_url)


def disable_link(role: str) -> bool:
    was = is_enabled(role)
    addon_state.set_(ADDON, f"{role}_enabled", False)
    return was


def resolve_role(token: str) -> Optional[str]:
    """Map an incoming token to a role — or None if unknown/disabled."""
    if not token:
        return None
    for role in ROLES:
        stored = get_token(role)
        if stored and secrets.compare_digest(stored, token) and is_enabled(role):
            return role
    return None


# ------------------------------------------------------------- qwen config --

def get_qwen_base_url() -> str:
    return str(addon_state.get(ADDON, "qwen_base_url", "") or "")


def set_qwen_base_url(url: str) -> None:
    addon_state.set_(ADDON, "qwen_base_url", url.strip())


def get_qwen_model() -> str:
    return str(addon_state.get(ADDON, "qwen_model", DEFAULT_QWEN_MODEL) or DEFAULT_QWEN_MODEL)


def set_qwen_model(model: str) -> None:
    addon_state.set_(ADDON, "qwen_model", model.strip())


# ---------------------------------------------------------------- sqlite ----

def db_path() -> Path:
    override = os.environ.get("OBUCHALKA_DB_PATH")
    if override:
        return Path(override)
    from ...storage import storage
    return storage.data_dir / "obuchalka.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS vessels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                macro_map TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leaves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vessel_id INTEGER NOT NULL REFERENCES vessels(id),
                parent_id INTEGER REFERENCES leaves(id),
                title TEXT NOT NULL,
                task TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                score INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'empty',
                context_budget INTEGER NOT NULL DEFAULT 2000,
                locked INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leaf_id INTEGER NOT NULL REFERENCES leaves(id),
                prompt TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                score INTEGER,
                verdict TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leaf_id INTEGER NOT NULL REFERENCES leaves(id),
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )


def _row(r: sqlite3.Row | None) -> Optional[dict]:
    return dict(r) if r is not None else None


# --------------------------------------------------------------- vessels ----

def create_vessel(title: str, macro_map: str = "", priority: int = 0) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO vessels (title, priority, macro_map, created_at) VALUES (?,?,?,?)",
            (title, priority, macro_map, time.time()),
        )
        return int(cur.lastrowid)


def list_vessels() -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM vessels ORDER BY priority DESC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_vessel(vessel_id: int) -> Optional[dict]:
    with connect() as con:
        return _row(con.execute("SELECT * FROM vessels WHERE id=?", (vessel_id,)).fetchone())


def update_vessel(vessel_id: int, *, title: str | None = None,
                  macro_map: str | None = None, priority: int | None = None) -> bool:
    sets, args = [], []
    if title is not None:
        sets.append("title=?"); args.append(title)
    if macro_map is not None:
        sets.append("macro_map=?"); args.append(macro_map)
    if priority is not None:
        sets.append("priority=?"); args.append(priority)
    if not sets:
        return False
    args.append(vessel_id)
    with connect() as con:
        cur = con.execute(f"UPDATE vessels SET {', '.join(sets)} WHERE id=?", args)
        return cur.rowcount > 0


# ---------------------------------------------------------------- leaves ----

def create_leaf(vessel_id: int, title: str, task: str = "",
                parent_id: int | None = None,
                context_budget: int = DEFAULT_CONTEXT_BUDGET,
                position: int = 0) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO leaves (vessel_id, parent_id, title, task, context_budget,"
            " position, updated_at) VALUES (?,?,?,?,?,?,?)",
            (vessel_id, parent_id, title, task, context_budget, position, time.time()),
        )
        return int(cur.lastrowid)


def get_leaf(leaf_id: int) -> Optional[dict]:
    with connect() as con:
        return _row(con.execute("SELECT * FROM leaves WHERE id=?", (leaf_id,)).fetchone())


def list_leaves(vessel_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM leaves WHERE vessel_id=? ORDER BY position ASC, id ASC",
            (vessel_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_leaf(leaf_id: int, *, title: str | None = None, task: str | None = None,
                content: str | None = None, context_budget: int | None = None,
                position: int | None = None) -> tuple[bool, str]:
    """Teacher-only edit. Locked leaves (score 10, «основание») reject edits."""
    leaf = get_leaf(leaf_id)
    if leaf is None:
        return False, "not_found"
    if leaf["locked"]:
        return False, "locked"
    sets, args = [], []
    if title is not None:
        sets.append("title=?"); args.append(title)
    if task is not None:
        sets.append("task=?"); args.append(task)
    if content is not None:
        sets.append("content=?"); args.append(content)
        # fresh content resets an old score back into draft
        sets.append("status=?"); args.append(STATUS_DRAFT if content else STATUS_EMPTY)
    if context_budget is not None:
        sets.append("context_budget=?"); args.append(int(context_budget))
    if position is not None:
        sets.append("position=?"); args.append(int(position))
    if not sets:
        return False, "no_fields"
    sets.append("updated_at=?"); args.append(time.time())
    args.append(leaf_id)
    with connect() as con:
        con.execute(f"UPDATE leaves SET {', '.join(sets)} WHERE id=?", args)
    return True, "ok"


def set_score(leaf_id: int, score: int, role: str) -> tuple[bool, str]:
    """Score a leaf 0–10. 10 ⇒ it becomes «основание» and locks."""
    if not 0 <= score <= 10:
        return False, "bad_score"
    leaf = get_leaf(leaf_id)
    if leaf is None:
        return False, "not_found"
    if leaf["locked"] and score != 10:
        return False, "locked"
    if score == 10:
        status, locked = STATUS_FOUNDATION, 1
    elif score > 0:
        status, locked = STATUS_REVIEW, 0
    else:
        status = STATUS_DRAFT if leaf["content"] else STATUS_EMPTY
        locked = 0
    with connect() as con:
        con.execute(
            "UPDATE leaves SET score=?, status=?, locked=?, updated_at=? WHERE id=?",
            (score, status, locked, time.time(), leaf_id),
        )
    journal_add(role, "score", f"leaf#{leaf_id} «{leaf['title']}» → {score}/10")
    return True, "ok"


def unlock_leaf(leaf_id: int, role: str) -> tuple[bool, str]:
    """Explicit teacher unlock of a foundation leaf — the ONLY way to edit it."""
    leaf = get_leaf(leaf_id)
    if leaf is None:
        return False, "not_found"
    if not leaf["locked"]:
        return False, "not_locked"
    with connect() as con:
        con.execute(
            "UPDATE leaves SET locked=0, status=?, updated_at=? WHERE id=?",
            (STATUS_REVIEW, time.time(), leaf_id),
        )
    journal_add(role, "unlock", f"leaf#{leaf_id} «{leaf['title']}» разблокирован")
    return True, "ok"


# --------------------------------------------------------------- attempts ---

def add_attempt(leaf_id: int, prompt: str, answer: str,
                score: int | None = None, verdict: str = "") -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO attempts (leaf_id, prompt, answer, score, verdict, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (leaf_id, prompt, answer, score, verdict, time.time()),
        )
        return int(cur.lastrowid)


def list_attempts(leaf_id: int, limit: int = 20) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM attempts WHERE leaf_id=? ORDER BY id DESC LIMIT ?",
            (leaf_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------- notes ----

def add_note(leaf_id: int, role: str, text: str) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO notes (leaf_id, role, text, created_at) VALUES (?,?,?,?)",
            (leaf_id, role, text, time.time()),
        )
    journal_add(role, "note", f"leaf#{leaf_id}: {text[:80]}")
    return int(cur.lastrowid)


def list_notes(leaf_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM notes WHERE leaf_id=? ORDER BY id DESC", (leaf_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------- journal ----

def journal_add(role: str, action: str, detail: str = "") -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO journal (role, action, detail, created_at) VALUES (?,?,?,?)",
            (role, action, detail, time.time()),
        )


def journal_recent(limit: int = 30) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------- stats ----

def stats() -> dict[str, Any]:
    with connect() as con:
        vessels = con.execute("SELECT COUNT(*) c FROM vessels").fetchone()["c"]
        total = con.execute("SELECT COUNT(*) c FROM leaves").fetchone()["c"]
        foundation = con.execute(
            "SELECT COUNT(*) c FROM leaves WHERE score=10").fetchone()["c"]
        review = con.execute(
            "SELECT COUNT(*) c FROM leaves WHERE score BETWEEN 7 AND 9").fetchone()["c"]
        empty = con.execute(
            "SELECT COUNT(*) c FROM leaves WHERE status='empty'").fetchone()["c"]
    return {
        "vessels": vessels, "leaves": total, "foundation": foundation,
        "near": review, "empty": empty,
    }


# --------------------------------------------------------------- handoff ----

def handoff_text(limit: int = HANDOFF_LIMIT) -> str:
    """Compressed brief for a fresh teacher: continue the work, don't restart.

    Macro map of each vessel + per-leaf status line + recent journal.
    Always fits within ``limit`` characters (tail-truncated per section).
    """
    parts: list[str] = [
        "# БРИФ ДЛЯ НОВОГО УЧИТЕЛЯ (обучалка Гермеса)",
        "Ты продолжаешь работу предыдущего учителя. Ничего не начинай заново.",
        "Правила: листики с баллом 10 — «основание», они заблокированы; "
        "твоя задача — наполнять пустые листики по их task и доводить "
        "остальные до 10. Ученик Qwen прогоняется stateless: только task + "
        "контекст в пределах бюджета листика.",
        "",
    ]
    for v in list_vessels():
        parts.append(f"## Сосуд: {v['title']} (id={v['id']})")
        macro = (v["macro_map"] or "").strip()
        if macro:
            parts.append(macro[:800])
        for leaf in list_leaves(v["id"]):
            flag = "🔒10" if leaf["locked"] else f"{leaf['score']}/10"
            has = "есть" if leaf["content"] else "ПУСТО"
            parts.append(
                f"- leaf#{leaf['id']} [{flag}] «{leaf['title']}» — контент: {has}; "
                f"задание: {(leaf['task'] or '')[:120]}"
            )
        parts.append("")
    parts.append("## Последние действия (журнал)")
    for j in journal_recent(15):
        parts.append(f"- [{j['role']}] {j['action']}: {j['detail'][:100]}")
    text = "\n".join(parts)
    if len(text) > limit:
        text = text[: limit - 20].rsplit("\n", 1)[0] + "\n…(бриф усечён)"
    return text
