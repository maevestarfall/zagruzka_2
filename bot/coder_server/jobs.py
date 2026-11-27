"""Persistent jobs, leases, retries, waits and checkpoints."""
from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class JobQueue:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def _init(self) -> None:
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
              payload TEXT NOT NULL, state TEXT NOT NULL, priority INTEGER NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
              run_at REAL NOT NULL, deadline REAL, next_retry_at REAL,
              lease_owner TEXT, lease_token INTEGER NOT NULL DEFAULT 0, lease_until REAL,
              last_heartbeat_at REAL, wait_event TEXT, result TEXT, error TEXT,
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              UNIQUE(task_id, kind, payload)
            );
            CREATE TABLE IF NOT EXISTS job_attempts(
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
              worker_id TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
              status TEXT NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS checkpoints(
              task_id TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
              value TEXT NOT NULL, created_at REAL NOT NULL,
              PRIMARY KEY(task_id,name)
            );
            CREATE TABLE IF NOT EXISTS dead_letter_jobs(
              job_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, reason TEXT NOT NULL,
              snapshot TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbox(
              message_id TEXT PRIMARY KEY, received_at REAL NOT NULL
            );
            """)

    def enqueue(self, task_id: str, kind: str, payload: dict[str, Any], *,
                run_at: float | None = None, delay_seconds: float = 0,
                priority: int = 100, max_attempts: int = 3,
                deadline: float | None = None) -> dict[str, Any]:
        now = time.time(); body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        with self._db() as db:
            try:
                db.execute("INSERT INTO jobs(id,task_id,kind,payload,state,priority,max_attempts,run_at,deadline,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (job_id,task_id,kind,body,"queued",priority,max_attempts,run_at or now+delay_seconds,deadline,now,now))
            except sqlite3.IntegrityError:
                row=db.execute("SELECT * FROM jobs WHERE task_id=? AND kind=? AND payload=?",(task_id,kind,body)).fetchone()
                return dict(row)
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._db() as db: row=db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not row: raise KeyError(job_id)
        return self._decode(row)

    def list(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._db() as db:
            rows=db.execute("SELECT * FROM jobs WHERE task_id=? ORDER BY priority,created_at",(task_id,)).fetchall() if task_id else db.execute("SELECT * FROM jobs ORDER BY priority,created_at").fetchall()
        return [self._decode(r) for r in rows]

    def reclaim_expired(self) -> int:
        """ТЗ §6: lease expiry — «мёртвые» running-джобы возвращаются в очередь.

        Без этого джоба, чей воркер умер (краш процесса, OOM, перезапуск),
        навсегда зависала бы в state='running'. Джобы с исчерпанными
        попытками уходят в dead letter.
        """
        now=time.time(); reclaimed=0
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows=db.execute("SELECT * FROM jobs WHERE state='running' AND lease_until IS NOT NULL AND lease_until<?",(now,)).fetchall()
            for row in rows:
                job=dict(row); exhausted=job["attempts"] >= job["max_attempts"]
                if exhausted:
                    db.execute("UPDATE jobs SET state='dead_letter',error=COALESCE(error,'lease expired'),lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",(now,job["id"]))
                    db.execute("INSERT OR REPLACE INTO dead_letter_jobs VALUES(?,?,?,?,?)",(job["id"],job["task_id"],"lease expired, attempts exhausted",json.dumps({k:job[k] for k in ("id","task_id","kind","attempts","max_attempts")}),now))
                else:
                    db.execute("UPDATE jobs SET state='queued',run_at=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",(now,now,job["id"]))
                db.execute("UPDATE job_attempts SET status='lost',finished_at=?,error='lease expired' WHERE job_id=? AND status='running'",(now,job["id"]))
                reclaimed+=1
        return reclaimed

    def lease(self, worker_id: str, lease_seconds: int = 30) -> dict[str, Any] | None:
        self.reclaim_expired()
        now=time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM jobs WHERE state='queued' AND run_at<=? AND (deadline IS NULL OR deadline>?) ORDER BY priority,run_at LIMIT 1",(now,now)).fetchone()
            if not row: return None
            token=int(row["lease_token"])+1
            db.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease_owner=?,lease_token=?,lease_until=?,last_heartbeat_at=?,updated_at=? WHERE id=?",(worker_id,token,now+lease_seconds,now,now,row["id"]))
            db.execute("INSERT INTO job_attempts VALUES(?,?,?,?,?,?,?,?)",(f"attempt_{uuid.uuid4().hex[:16]}",row["id"],int(row["attempts"])+1,worker_id,now,None,"running",None))
        return self.get(row["id"])

    def heartbeat(self, job_id: str, worker_id: str, token: int, lease_seconds: int = 30) -> bool:
        now=time.time()
        with self._db() as db:
            cur=db.execute("UPDATE jobs SET lease_until=?,last_heartbeat_at=?,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=? AND state='running'",(now+lease_seconds,now,now,job_id,worker_id,token))
        return cur.rowcount == 1

    def finish(self, job_id: str, worker_id: str, token: int, result: dict[str, Any]) -> dict[str, Any]:
        now=time.time()
        with self._db() as db:
            cur=db.execute("UPDATE jobs SET state='succeeded',result=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=?",(json.dumps(result),now,job_id,worker_id,token))
            if cur.rowcount != 1: raise RuntimeError("stale lease")
            db.execute("UPDATE job_attempts SET status='succeeded',finished_at=? WHERE job_id=? AND status='running'",(now,job_id))
        return self.get(job_id)

    def fail(self, job_id: str, worker_id: str, token: int, error: str, retryable: bool = True) -> dict[str, Any]:
        job=self.get(job_id); now=time.time(); exhausted=job["attempts"] >= job["max_attempts"] or not retryable or (job["deadline"] and now>=job["deadline"])
        state="dead_letter" if exhausted else "queued"; next_at=None if exhausted else now+min(300,2**job["attempts"]+random.random())
        with self._db() as db:
            cur=db.execute("UPDATE jobs SET state=?,error=?,run_at=COALESCE(?,run_at),next_retry_at=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND lease_owner=? AND lease_token=?",(state,error,next_at,next_at,now,job_id,worker_id,token))
            if cur.rowcount != 1: raise RuntimeError("stale lease")
            db.execute("UPDATE job_attempts SET status='failed',finished_at=?,error=? WHERE job_id=? AND status='running'",(now,error,job_id))
            if exhausted: db.execute("INSERT OR REPLACE INTO dead_letter_jobs VALUES(?,?,?,?,?)",(job_id,job["task_id"],error,json.dumps(job),now))
        return self.get(job_id)

    def control(self, job_id: str, action: str) -> dict[str, Any]:
        states={"pause":"paused","resume":"queued","cancel":"cancelled","retry":"queued"}
        if action not in states: raise ValueError(action)
        with self._db() as db: db.execute("UPDATE jobs SET state=?,run_at=?,updated_at=? WHERE id=?",(states[action],time.time(),time.time(),job_id))
        return self.get(job_id)

    def wait_for_event(self, job_id: str, event: str) -> dict[str, Any]:
        with self._db() as db: db.execute("UPDATE jobs SET state='waiting',wait_event=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",(event,time.time(),job_id))
        return self.get(job_id)

    def signal(self, message_id: str, event: str, data: dict[str, Any]) -> int:
        now=time.time()
        with self._db() as db:
            try: db.execute("INSERT INTO inbox VALUES(?,?)",(message_id,now))
            except sqlite3.IntegrityError: return 0
            cur=db.execute("UPDATE jobs SET state='queued',payload=json_set(payload,'$.event_data',json(?)),wait_event=NULL,run_at=?,updated_at=? WHERE state='waiting' AND wait_event=?",(json.dumps(data),now,now,event))
        return cur.rowcount

    def save_checkpoint(self, task_id: str, name: str, value: dict[str, Any], expected_version: int | None = None) -> dict[str, Any]:
        now=time.time()
        with self._db() as db:
            row=db.execute("SELECT version FROM checkpoints WHERE task_id=? AND name=?",(task_id,name)).fetchone(); current=int(row[0]) if row else 0
            if expected_version is not None and current != expected_version: raise RuntimeError("checkpoint version conflict")
            version=current+1
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?) ON CONFLICT(task_id,name) DO UPDATE SET version=excluded.version,value=excluded.value,created_at=excluded.created_at",(task_id,name,version,json.dumps(value),now))
        return {"task_id":task_id,"name":name,"version":version,"value":value,"created_at":now}

    def load_checkpoint(self, task_id: str, name: str) -> dict[str, Any] | None:
        with self._db() as db: row=db.execute("SELECT * FROM checkpoints WHERE task_id=? AND name=?",(task_id,name)).fetchone()
        if not row: return None
        item=dict(row); item["value"]=json.loads(item["value"]); return item

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item=dict(row); item["payload"]=json.loads(item["payload"]); item["result"]=json.loads(item["result"]) if item["result"] else None; return item
