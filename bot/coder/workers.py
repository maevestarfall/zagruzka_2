from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"
DEFAULT_COOLDOWN_S = 1800 # 30 minutes

def set_worker_cooldown(worker_id: str, cooldown_s: float = DEFAULT_COOLDOWN_S) -> None:
    """Set a persistent cooldown for a worker in SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    
    cooldown_until = int(time.time() + cooldown_s)
    now = int(time.time())
    
    with conn:
        conn.execute("""
            INSERT INTO coder_worker_runtime (worker_id, current_role, cooldown_until, last_swapped_at)
            VALUES (?, 'worker', ?, ?)
            ON CONFLICT (worker_id) DO UPDATE SET cooldown_until = ?, last_swapped_at = ?
        """, (worker_id, cooldown_until, now, cooldown_until, now))
        
    conn.close()
    logger.info("Persistent cooldown set for worker '%s' until %d", worker_id, cooldown_until)

def is_worker_on_cooldown(worker_id: str) -> bool:
    """Check if the worker has an active persistent cooldown."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = int(time.time())
    
    cursor.execute("SELECT cooldown_until FROM coder_worker_runtime WHERE worker_id = ?", (worker_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return row[0] > now
    return False
