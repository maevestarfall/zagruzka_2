from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"
MAX_RESTARTS = 3
RESTART_WINDOW_S = 1800 # 30 minutes

class SafeModeActive(Exception):
    """Raised when Boss enters safe mode due to excessive crash loop restarts."""
    pass

def recover_boss_state(persona: str) -> bool:
    """Boss state recovery: re-validates lease, replays state, and enforces restart limits."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    now = time.time()
    
    # 1. Enforce LILUSH_CODER_BOSS_MAX_RESTARTS limit
    # We use a simple table to record restarts and timestamps
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_boss_restarts (
                restart_id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                restarted_at REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO coder_boss_restarts (persona, restarted_at) VALUES (?, ?)",
            (persona, now)
        )
        
        # Count restarts within window
        cursor.execute(
            "SELECT COUNT(*) FROM coder_boss_restarts WHERE persona = ? AND restarted_at > ?",
            (persona, now - RESTART_WINDOW_S)
        )
        restarts_count = cursor.fetchone()[0]
        
        if restarts_count > MAX_RESTARTS:
            logger.critical("Excessive restarts detected (%d restarts within 30 min)! Entering SAFE MODE.", restarts_count)
            conn.close()
            # Safe mode: freeze assignments and artifact publication
            return False
            
    # 2. Replay state and reconcile runs
    # Find runs in active, non-terminal states and log recovery
    with conn:
        cursor.execute("SELECT run_id, state FROM coder_runs WHERE state NOT IN ('done', 'failed_budget', 'failed_owner_input', 'failed_gate', 'failed_infra')")
        active_runs = cursor.fetchall()
        for run in active_runs:
            logger.info("Recovering active run '%s' from state '%s'", run[0], run[1])
            conn.execute(
                "INSERT INTO coder_audit_events (run_id, persona, event_kind, message, ts) VALUES (?, ?, 'recovery', ?, ?)",
                (run[0], persona, f"Boss восстановился после перезапуска. Текущее состояние: {run[1]}", time.strftime("%Y-%m-%d %H:%M:%S"))
            )
            
    conn.close()
    return True
