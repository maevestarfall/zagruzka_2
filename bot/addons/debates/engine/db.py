"""SQLite WAL store for debate-server-v3 — embedded edition.

Default DB path: ``$DATA_DIR/debate_v3.db``.  Falls back to
``$DEBATE_DB_PATH`` → ``./debate.db`` for standalone / test use.

Tables:
- doors(door_id, title, part_id, style, status, created_at, join_secret, judge_mode)
- door_tokens(token, door_id, debater_id, role, claimed)
- msgs(msg_id, door_id, debater_id, kind, payload_json, body_text,
       rejected_reason, payload_bytes, token_count_reported, created_at)
- atoms(canonical_category_id, door_id, version, author_debater_id,
        msg_id, pick, locked, ...)
- violations(violation_id, msg_id, rule, detail)
- phase_gates(door_id, phase, status, report_json, ran_at)
- owner_directives(directive_id, door_id, kind, payload, ran_at)
- validations(validation_id, door_id, payload_sha256, ok, created_at)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS doors (
  door_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  part_id TEXT NOT NULL,
  style TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  current_phase TEXT NOT NULL DEFAULT 'voice_manifest',
  created_at REAL NOT NULL,
  terminated_reason TEXT,
  forced_validation_mode INTEGER NOT NULL DEFAULT 0,
  join_secret TEXT,
  judge_mode TEXT NOT NULL DEFAULT 'self'
);
CREATE INDEX IF NOT EXISTS idx_doors_join_secret ON doors(join_secret);

CREATE TABLE IF NOT EXISTS door_tokens (
  token TEXT PRIMARY KEY,
  door_id TEXT NOT NULL REFERENCES doors(door_id),
  debater_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'debater',
  created_at REAL NOT NULL,
  claimed INTEGER NOT NULL DEFAULT 0,
  UNIQUE(door_id, debater_id)
);

CREATE TABLE IF NOT EXISTS msgs (
  msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
  door_id TEXT NOT NULL REFERENCES doors(door_id),
  debater_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  body_text TEXT NOT NULL,
  rejected_reason TEXT,
  payload_bytes INTEGER NOT NULL,
  token_count_reported INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msgs_door_created ON msgs(door_id, created_at);
CREATE INDEX IF NOT EXISTS idx_msgs_door_kind ON msgs(door_id, kind, rejected_reason);

CREATE TABLE IF NOT EXISTS atoms (
  atom_db_id INTEGER PRIMARY KEY AUTOINCREMENT,
  door_id TEXT NOT NULL REFERENCES doors(door_id),
  canonical_category_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  author_debater_id TEXT NOT NULL,
  msg_id INTEGER NOT NULL REFERENCES msgs(msg_id),
  pick TEXT NOT NULL,
  provocation_level TEXT NOT NULL,
  code_shape_anchor_json TEXT NOT NULL,
  content TEXT NOT NULL,
  approved_by_json TEXT NOT NULL DEFAULT '[]',
  locked INTEGER NOT NULL DEFAULT 0,
  UNIQUE(door_id, canonical_category_id, version)
);

CREATE TABLE IF NOT EXISTS violations (
  violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  door_id TEXT NOT NULL REFERENCES doors(door_id),
  msg_id INTEGER,
  debater_id TEXT,
  rule TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_violations_door ON violations(door_id, created_at);

CREATE TABLE IF NOT EXISTS phase_gates (
  gate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  door_id TEXT NOT NULL REFERENCES doors(door_id),
  phase TEXT NOT NULL,
  status TEXT NOT NULL,
  report_json TEXT NOT NULL,
  ran_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_phase_gates_door ON phase_gates(door_id, phase, ran_at);

CREATE TABLE IF NOT EXISTS owner_directives (
  directive_id INTEGER PRIMARY KEY AUTOINCREMENT,
  door_id TEXT NOT NULL REFERENCES doors(door_id),
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  ran_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS validations (
  validation_id TEXT PRIMARY KEY,
  door_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  ok INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validations_door ON validations(door_id, created_at);

CREATE TABLE IF NOT EXISTS lobby_notes (
  note_id INTEGER PRIMARY KEY AUTOINCREMENT,
  door_id TEXT NOT NULL REFERENCES doors(door_id),
  debater_id TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lobby_notes_door ON lobby_notes(door_id, created_at);

CREATE TABLE IF NOT EXISTS admin_chat (
  chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
  door_id TEXT NOT NULL REFERENCES doors(door_id),
  debater_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_chat_door ON admin_chat(door_id, created_at);
CREATE TABLE IF NOT EXISTS debater_wake (
  door_id TEXT NOT NULL,
  debater_id TEXT NOT NULL,
  wake_url TEXT NOT NULL,
  updated_at REAL NOT NULL,
  last_pinged_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (door_id, debater_id)
);
"""

# Migration queries for existing DBs (add new columns safely)
_MIGRATIONS = [
    "ALTER TABLE doors ADD COLUMN join_secret TEXT",
    "ALTER TABLE door_tokens ADD COLUMN claimed INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_doors_join_secret ON doors(join_secret)",
    "ALTER TABLE doors ADD COLUMN judge_mode TEXT NOT NULL DEFAULT 'self'",
    "CREATE TABLE IF NOT EXISTS lobby_notes (note_id INTEGER PRIMARY KEY AUTOINCREMENT, door_id TEXT NOT NULL REFERENCES doors(door_id), debater_id TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_lobby_notes_door ON lobby_notes(door_id, created_at)",
    "CREATE TABLE IF NOT EXISTS admin_chat (chat_id INTEGER PRIMARY KEY AUTOINCREMENT, door_id TEXT NOT NULL REFERENCES doors(door_id), debater_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_admin_chat_door ON admin_chat(door_id, created_at)",
    "CREATE TABLE IF NOT EXISTS resume_state (door_id TEXT PRIMARY KEY, catchup_queue TEXT NOT NULL DEFAULT '[]', catchup_active INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS debater_wake (door_id TEXT NOT NULL, debater_id TEXT NOT NULL, wake_url TEXT NOT NULL, updated_at REAL NOT NULL, last_pinged_at REAL NOT NULL DEFAULT 0, PRIMARY KEY (door_id, debater_id))",
]


def _resolve_db_path() -> Path:
    # 1. Explicit env override
    explicit = os.environ.get("DEBATE_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).resolve()
    # 2. Bot DATA_DIR
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        return (Path(data_dir) / "debate_v3.db").resolve()
    # 3. Fallback
    return Path("./debate_v3.db").resolve()


def init_db(db_path: Path | None = None) -> Path:
    p = db_path or _resolve_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, sqlite3.connect(p) as con:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        con.executescript(SCHEMA)
        # Run safe migrations for existing DBs
        for mig in _MIGRATIONS:
            try:
                con.execute(mig)
            except sqlite3.OperationalError:
                pass  # column/index already exists
        con.commit()
    return p


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = db_path or _resolve_db_path()
    with _LOCK:
        con = sqlite3.connect(p)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON;")
        try:
            yield con
            con.commit()
        finally:
            con.close()


def reset(db_path: Path | None = None) -> None:
    """For tests: delete db file and recreate."""
    p = db_path or _resolve_db_path()
    if p.exists():
        p.unlink()
    for ext in ("-wal", "-shm"):
        sibling = p.with_name(p.name + ext)
        if sibling.exists():
            sibling.unlink()
    init_db(p)


# ---------- helpers ----------

def now_ts() -> float:
    import time
    return time.time()


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def jload(s: str):
    return json.loads(s) if s else None
