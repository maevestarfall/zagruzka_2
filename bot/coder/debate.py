from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"

def propose_atom(run_id: str, atom_id: str, pick: str, reasoning: str) -> None:
    """Record a debate atom proposal in the audit trail."""
    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute("""
            INSERT INTO coder_audit_events (run_id, persona, event_kind, message, ts)
            VALUES (?, 'coder_judge', 'debate_propose', ?, ?)
        """, (run_id, f"Предложен атом {atom_id} (Выбор: {pick}). Обоснование: {reasoning[:50]}...", time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.close()
    logger.info("Recorded debate atom proposal for '%s' (Run: %s)", atom_id, run_id)
