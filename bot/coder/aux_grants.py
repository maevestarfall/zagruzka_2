from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"
MAX_GRANT_TTL_S = 72 * 3600 # 72 hours

class IncompatibleRoleError(Exception):
    """Raised when trying to grant a role that violates separation of duties (e.g. self-review)."""
    pass

def create_aux_grant(job_id: str, original_author: str, granted_persona: str, duration_s: float) -> None:
    """Create a time-bounded auxiliary grant with incompatible-role checks (no self-review/self-integration)."""
    if duration_s > MAX_GRANT_TTL_S:
        raise ValueError(f"Auxiliary grant TTL exceeds maximum limit of 72 hours (requested {duration_s / 3600:.1f}h).")
        
    # Incompatible-role checks:
    # A Worker who wrote the code (original_author) cannot review or integrate their own code
    if original_author == granted_persona:
        raise IncompatibleRoleError(f"Incompatible role! Original author '{original_author}' cannot be granted self-access.")
        
    if original_author.startswith("coder_worker_") and granted_persona in ("coder_reviewer", "coder_integrator"):
        # Duty separation: Worker cannot review or integrate their own PR
        raise IncompatibleRoleError(f"Incompatible role! Worker '{original_author}' cannot review or integrate their own work.")
        
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    
    granted_until = time.time() + duration_s
    
    with conn:
        conn.execute("""
            INSERT INTO coder_aux_grants (job_id, original_author, granted_persona, granted_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (job_id, original_author, granted_persona) 
            DO UPDATE SET granted_until = ?
        """, (job_id, original_author, granted_persona, granted_until, granted_until))
        
    conn.close()
    logger.info("Successfully created auxiliary grant: '%s' granted '%s' until %f", original_author, granted_persona, granted_until)

def revoke_expired_aux_grants() -> int:
    """Revoke and delete all expired auxiliary grants from the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    now = time.time()
    
    with conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM coder_aux_grants WHERE granted_until < ?", (now,))
        revoked = cursor.rowcount
        
    conn.close()
    if revoked > 0:
        logger.info("Revoked %d expired auxiliary grants.", revoked)
    return revoked
