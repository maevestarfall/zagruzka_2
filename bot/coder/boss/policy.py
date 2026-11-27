from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"

@dataclass
class ReassignDecision:
    worker_id: str
    should_reassign: bool
    reason: str

class Policy:
    def __init__(self, run_id: str):
        self.run_id = run_id

    def pick_worker(self, task_id: str, available_workers: list[str]) -> str | None:
        """Pick the best available worker for the task, checking cooldowns."""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        now = int(time.time())
        
        eligible_workers = []
        for worker in available_workers:
            cursor.execute("SELECT cooldown_until FROM coder_worker_runtime WHERE worker_id = ?", (worker,))
            row = cursor.fetchone()
            if row:
                cooldown_until = row[0]
                if cooldown_until > now:
                    logger.info("Worker '%s' is in cooldown until %d (now=%d), skipping.", worker, cooldown_until, now)
                    continue
            eligible_workers.append(worker)
            
        conn.close()
        return eligible_workers[0] if eligible_workers else None

    def should_reassign(self, worker_id: str) -> ReassignDecision:
        """Decide if a worker should be reassigned based on current cooldowns and liveness."""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        now = int(time.time())
        
        cursor.execute("SELECT cooldown_until FROM coder_worker_runtime WHERE worker_id = ?", (worker_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0] > now:
            return ReassignDecision(
                worker_id=worker_id,
                should_reassign=False,
                reason=f"Worker is in cooldown until {row[0]}"
            )
            
        return ReassignDecision(
            worker_id=worker_id,
            should_reassign=True,
            reason="Worker has no active cooldowns."
        )
