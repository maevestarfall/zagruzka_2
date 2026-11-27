from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"

def record_external_ref_decision(ref_url: str, integration_mode: str, applies_to_cluster: str) -> int:
    """Researcher: record structured external reference decision in SQLite DB."""
    if integration_mode not in ("pattern-only", "wrap-as-tool", "vendored-deps"):
        raise ValueError(f"Invalid integration mode: {integration_mode}")
        
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    
    with conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO coder_external_ref_decisions (ref_url, integration_mode, applies_to_cluster, decided_at)
            VALUES (?, ?, ?, ?)
        """, (ref_url, integration_mode, applies_to_cluster, time.strftime("%Y-%m-%d %H:%M:%S")))
        ref_id = cursor.lastrowid or 0
        
    conn.close()
    logger.info("Recorded external ref decision in DB (ID: %d, Mode: %s)", ref_id, integration_mode)
    return ref_id
