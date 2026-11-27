from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"

class BossLease:
    def __init__(self, persona: str):
        self.persona = persona

    def acquire_lease(self, lease_duration_s: float = 60.0) -> bool:
        """Acquire the singleton Boss lease in coder_boss_leases."""
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        now = time.time()
        
        with conn:
            cursor.execute("SELECT lease_until, migrator_persona FROM coder_boss_leases WHERE lease_id = 1")
            row = cursor.fetchone()
            if row:
                lease_until, holder = row[0], row[1]
                if lease_until > now and holder != self.persona:
                    conn.close()
                    logger.warning("Boss lease is held by another replica: '%s' until %f", holder, lease_until)
                    return False
                # Acquire/renew lease
                conn.execute(
                    "UPDATE coder_boss_leases SET migrator_persona = ?, lease_until = ? WHERE lease_id = 1",
                    (self.persona, now + lease_duration_s)
                )
            else:
                conn.execute(
                    "INSERT INTO coder_boss_leases (lease_id, migrator_persona, lease_until) VALUES (1, ?, ?)",
                    (self.persona, now + lease_duration_s)
                )
        conn.close()
        logger.info("Successfully acquired Boss lease for '%s'", self.persona)
        return True

    def renew_lease(self, lease_duration_s: float = 60.0) -> bool:
        """Renew the active Boss lease."""
        return self.acquire_lease(lease_duration_s)
