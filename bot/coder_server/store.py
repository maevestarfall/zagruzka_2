"""SQLite event store, projections and content-addressed evidence ledger."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import ALLOWED_TRANSITIONS, Event, TaskState

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL,
 phase TEXT NOT NULL, workspace TEXT, trace_id TEXT NOT NULL, created_at REAL NOT NULL,
 updated_at REAL NOT NULL, checkpoint TEXT NOT NULL DEFAULT '{}', error TEXT,
 budget_json TEXT NOT NULL DEFAULT '{}', lease_owner TEXT, lease_until REAL
);
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, seq INTEGER NOT NULL,
 event_type TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
 summary TEXT NOT NULL, details_json TEXT NOT NULL, artifacts_json TEXT NOT NULL,
 trace_id TEXT NOT NULL, tool_call_id TEXT, created_at REAL NOT NULL,
 UNIQUE(task_id, seq)
);
CREATE TABLE IF NOT EXISTS idempotency (
 key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL,
 path TEXT NOT NULL, size INTEGER NOT NULL, metadata_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, claim_type TEXT NOT NULL, subject TEXT NOT NULL,
 producer TEXT NOT NULL, tool_call_id TEXT, artifact_ids_json TEXT NOT NULL,
 workspace_hash TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
 request_json TEXT NOT NULL, response_json TEXT, created_at REAL NOT NULL, resolved_at REAL
);
CREATE TABLE IF NOT EXISTS secret_refs (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, name TEXT NOT NULL, env_key TEXT NOT NULL,
 created_at REAL NOT NULL, UNIQUE(task_id, name)
);
CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS evidence_task ON evidence(task_id, created_at);
"""


class EventStore:
    def __init__(self, db_path: str | Path, artifact_root: str | Path):
        self.db_path = Path(db_path)
        self.artifact_root = Path(artifact_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as db:
            db.executescript(_SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create_task(self, title: str, request: str, budget: dict[str, Any] | None = None) -> dict[str, Any]:
        now, task_id, trace = time.time(), f"task_{uuid.uuid4().hex[:12]}", uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO tasks(id,title,request,state,phase,trace_id,created_at,updated_at,budget_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (task_id, title, request, TaskState.CREATED, "intake", trace, now, now, json.dumps(budget or {})))
        self.append_event(task_id, "task.created", "intake", "completed", "Задача принята")
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        out = dict(row)
        out["checkpoint"] = json.loads(out.pop("checkpoint"))
        out["budget"] = json.loads(out.pop("budget_json"))
        return out

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),))]
        return [self.get_task(i) for i in ids]

    def transition(self, task_id: str, target: TaskState, phase: str, summary: str) -> dict[str, Any]:
        with self._lock:
            task = self.get_task(task_id)
            current = TaskState(task["state"])
            if target != current and target not in ALLOWED_TRANSITIONS[current]:
                raise ValueError(f"invalid transition: {current} -> {target}")
            now = time.time()
            with self.connect() as db:
                db.execute("UPDATE tasks SET state=?,phase=?,updated_at=? WHERE id=?", (target, phase, now, task_id))
            self.append_event(task_id, "task.transition", phase, str(target), summary, {"from": current, "to": target})
            return self.get_task(task_id)

    def checkpoint(self, task_id: str, value: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET checkpoint=?,updated_at=? WHERE id=?", (json.dumps(value), time.time(), task_id))

    def append_event(self, task_id: str, event_type: str, phase: str, status: str, summary: str,
                     details: dict[str, Any] | None = None, artifact_ids: list[str] | None = None,
                     tool_call_id: str | None = None) -> Event:
        with self._lock, self.connect() as db:
            task = db.execute("SELECT trace_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            seq = int(db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE task_id=?", (task_id,)).fetchone()[0])
            event = Event(uuid.uuid4().hex, task_id, seq, event_type, phase, status, summary[:500], time.time(), task[0], tool_call_id, details or {}, artifact_ids or [])
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (event.event_id,event.task_id,event.seq,event.event_type,event.phase,event.status,event.summary,json.dumps(event.details),json.dumps(event.artifact_ids),event.trace_id,event.tool_call_id,event.created_at))
        return event

    def events(self, task_id: str, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (task_id, after, min(limit, 5000))).fetchall()
        return [{**dict(r), "details": json.loads(r["details_json"]), "artifact_ids": json.loads(r["artifacts_json"])} for r in rows]

    def remember(self, key: str, result: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO idempotency VALUES(?,?,?)", (key, json.dumps(result), time.time()))

    def recalled(self, key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT result_json FROM idempotency WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def artifact(self, task_id: str, kind: str, data: bytes, suffix: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        path = self.artifact_root / digest[:2] / f"{digest}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        artifact_id = f"art_{digest[:20]}"
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (artifact_id,task_id,kind,digest,str(path),len(data),json.dumps(metadata or {}),time.time()))
        return {"id": artifact_id, "kind": kind, "sha256": digest, "size": len(data), "metadata": metadata or {}}

    def artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT id,task_id,kind,sha256,size,metadata_json,created_at FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata_json"])} for row in rows]

    def request_approval(self, task_id: str, kind: str, request: dict[str, Any]) -> dict[str, Any]:
        item = {"id": f"apr_{uuid.uuid4().hex[:16]}", "task_id": task_id, "kind": kind, "status": "pending", "request": request, "created_at": time.time()}
        with self.connect() as db:
            db.execute("INSERT INTO approvals(id,task_id,kind,status,request_json,created_at) VALUES(?,?,?,?,?,?)", (item["id"],task_id,kind,item["status"],json.dumps(request),item["created_at"]))
        return item

    def approvals(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM approvals WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        return [{**dict(row), "request": json.loads(row["request_json"]), "response": json.loads(row["response_json"]) if row["response_json"] else None} for row in rows]

    def resolve_approval(self, approval_id: str, approved: bool, response: dict[str, Any] | None = None) -> dict[str, Any]:
        status = "approved" if approved else "rejected"
        with self.connect() as db:
            db.execute("UPDATE approvals SET status=?,response_json=?,resolved_at=? WHERE id=? AND status='pending'", (status,json.dumps(response or {}),time.time(),approval_id))
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return {**dict(row), "request": json.loads(row["request_json"]), "response": json.loads(row["response_json"]) if row["response_json"] else None}

    def add_secret_ref(self, task_id: str, name: str, env_key: str) -> dict[str, Any]:
        item = {"id": f"sec_{uuid.uuid4().hex[:16]}", "task_id": task_id, "name": name, "env_key": env_key, "created_at": time.time()}
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO secret_refs VALUES(?,?,?,?,?)", tuple(item.values()))
        return item

    def secret_refs(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,task_id,name,env_key,created_at FROM secret_refs WHERE task_id=? ORDER BY created_at", (task_id,))]

    def add_evidence(self, task_id: str, claim_type: str, subject: str, producer: str,
                     tool_call_id: str | None, artifact_ids: list[str], workspace_hash: str | None = None) -> dict[str, Any]:
        item = {"id": f"ev_{uuid.uuid4().hex[:16]}", "task_id": task_id, "claim_type": claim_type, "subject": subject,
                "producer": producer, "tool_call_id": tool_call_id, "artifact_ids": artifact_ids,
                "workspace_hash": workspace_hash, "created_at": time.time()}
        with self.connect() as db:
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)", (item["id"],task_id,claim_type,subject,producer,tool_call_id,json.dumps(artifact_ids),workspace_hash,item["created_at"]))
        return item

    def evidence(self, task_id: str, current_workspace_hash: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM evidence WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        output = []
        for row in rows:
            item = {**dict(row), "artifact_ids": json.loads(row["artifact_ids_json"])}
            item["fresh"] = current_workspace_hash is None or not item["workspace_hash"] or item["workspace_hash"] == current_workspace_hash
            output.append(item)
        return output
