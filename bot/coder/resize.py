from __future__ import annotations

import logging
import sqlite3
import time
from .drain import drain_worker

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"

def resize_workers(run_id: str, target_count: int) -> None:
    """Resize workers: drain old workers first if reducing count, update DB."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT workers_count FROM coder_runs WHERE run_id = ?", (run_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return
        
    current_count = row[0]
    if target_count < current_count:
        # We are scaling down. Drain workers that will be removed (e.g. from index target_count to current_count)
        for i in range(target_count + 1, current_count + 1):
            worker_id = f"coder_worker_{i:02d}"
            logger.info("Scaling down: draining worker '%s'...", worker_id)
            try:
                drain_worker(worker_id, time.time() + 300.0)
            except Exception as e:
                logger.warning("Failed to drain worker '%s': %s", worker_id, e)
                
    # Update count in DB
    with conn:
        conn.execute("UPDATE coder_runs SET workers_count = ? WHERE run_id = ?", (target_count, run_id))
        conn.execute("""
            INSERT INTO coder_audit_events (run_id, persona, event_kind, message, ts)
            VALUES (?, 'coder_boss', 'resize', ?, ?)
        """, (run_id, f"Штат воркеров изменен: {current_count} -> {target_count}", time.strftime("%Y-%m-%d %H:%M:%S")))
        
    conn.close()
    logger.info("Successfully resized worker count for run '%s' to %d", run_id, target_count)
