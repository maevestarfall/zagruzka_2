from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"
DRAIN_TIMEOUT_S = 300 # 5 minutes

class DrainTimeoutExpired(Exception):
    """Raised when worker drain takes longer than 5 minutes and is force-aborted."""
    pass

def drain_worker(worker_id: str, deadline: float) -> bool:
    """Drain worker: no new claims, wait for in-flight tasks, force-abort after 5 minutes."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    
    # 1. Update worker runtime to 'draining'
    with conn:
        conn.execute(
            "UPDATE coder_worker_runtime SET current_role = 'draining' WHERE worker_id = ?",
            (worker_id,)
        )
        
    start_time = time.time()
    logger.info("Starting drain for worker '%s' (Timeout: %ds)...", worker_id, DRAIN_TIMEOUT_S)
    
    # 2. Poll active tasks for this worker
    while time.time() - start_time < DRAIN_TIMEOUT_S:
        cursor.execute("SELECT COUNT(*) FROM coder_tasks WHERE assigned_worker = ? AND state = 'CODING'", (worker_id,))
        count = cursor.fetchone()[0]
        if count == 0:
            logger.info("Worker '%s' successfully drained.", worker_id)
            conn.close()
            return True
            
        time.sleep(1.0)
        
    # 3. Timeout exceeded: force-abort in-flight tasks
    logger.warning("Drain timeout exceeded for worker '%s'! Force-aborting in-flight tasks.", worker_id)
    with conn:
        conn.execute(
            "UPDATE coder_tasks SET state = 'FAILED_INFRA' WHERE assigned_worker = ? AND state = 'CODING'",
            (worker_id,)
        )
    conn.close()
    raise DrainTimeoutExpired(f"Drain force-aborted for worker '{worker_id}' after 5 minutes.")
