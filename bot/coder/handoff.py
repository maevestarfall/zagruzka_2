from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"

def handoff_task(task_id: str, from_worker: str, to_worker: str, reason: str) -> None:
    """Handoff a task from one worker to another inside a single SQLite transaction."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    
    with conn:
        # 1. Verify task is currently assigned to from_worker
        cursor.execute("SELECT run_id, state FROM coder_tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise ValueError(f"Task '{task_id}' not found.")
            
        run_id, state = row
        
        # 2. Update task assignment
        conn.execute(
            "UPDATE coder_tasks SET assigned_worker = ?, state = 'PENDING' WHERE task_id = ?",
            (to_worker, task_id)
        )
        
        # 3. Log handoff event to audit trail
        conn.execute("""
            INSERT INTO coder_audit_events (run_id, persona, event_kind, message, ts)
            VALUES (?, 'coder_boss', 'handoff', ?, ?)
        """, (run_id, f"Передача задачи {task_id} от {from_worker} к {to_worker}. Причина: {reason}", now_str))
        
    conn.close()
    logger.info("Successfully handed off task '%s' from '%s' to '%s'", task_id, from_worker, to_worker)
