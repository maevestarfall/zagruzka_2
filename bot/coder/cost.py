from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"

# Persona families that are Chain of Thought capable
COT_FAMILIES = {"coder_auditor", "coder_judge"}

@dataclass
class BudgetPermit:
    reservation_id: str
    run_id: str
    persona: str
    estimated_usd: float
    expires_at: float

def reserve_budget(run_id: str, persona: str, estimated_usd: float, client_timeout_s: float = 30.0) -> BudgetPermit | None:
    """Reserve budget with 2.5x Chain of Thought multiplier for CoT personas."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    
    # 2.5x CoT multiplier
    final_estimate = estimated_usd
    if persona in COT_FAMILIES:
        final_estimate *= 2.5
        logger.info("CoT multiplier applied to %s: %f USD -> %f USD", persona, estimated_usd, final_estimate)
        
    cursor = conn.cursor()
    
    with conn:
        # Check current run spend and budget cap
        cursor.execute("SELECT usd_spent, budget_cap FROM coder_runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
            
        usd_spent, budget_cap = row[0], row[1]
        
        # Check if reservation exceeds budget cap
        if usd_spent + final_estimate > budget_cap:
            logger.warning("Reservation of %f USD exceeds budget cap %f USD (Current spend: %f USD)", final_estimate, budget_cap, usd_spent)
            conn.close()
            return None
            
        reservation_id = f"res_{int(time.time())}_{random_hex(4)}"
        expires_at = time.time() + (2 * client_timeout_s)
        
        conn.execute("""
            INSERT INTO cost_reservations (reservation_id, run_id, persona, estimated_usd, state, expires_at)
            VALUES (?, ?, ?, ?, 'RESERVED', ?)
        """, (reservation_id, run_id, persona, final_estimate, expires_at))
        
        # Log to cost_buckets
        utc_day = time.strftime("%Y-%m-%d", time.gmtime())
        conn.execute("""
            INSERT INTO cost_buckets (persona_key, utc_day, usd_reserved)
            VALUES (?, ?, ?)
            ON CONFLICT (persona_key, utc_day) DO UPDATE SET usd_reserved = usd_reserved + ?
        """, (persona, utc_day, final_estimate, final_estimate))
        
    conn.close()
    return BudgetPermit(reservation_id, run_id, persona, final_estimate, expires_at)

def settle_budget(reservation_id: str, actual_usd: float) -> None:
    """Settle reserved budget in-place to 'SETTLED' and increment usd_spent."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    
    with conn:
        cursor.execute("SELECT run_id, persona, estimated_usd FROM cost_reservations WHERE reservation_id = ?", (reservation_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return
            
        run_id, persona, estimated = row
        
        # Update reservation
        conn.execute("""
            UPDATE cost_reservations
            SET actual_usd = ?, state = 'SETTLED'
            WHERE reservation_id = ?
        """, (actual_usd, reservation_id))
        
        # Increment actual spent on run
        conn.execute("UPDATE coder_runs SET usd_spent = usd_spent + ? WHERE run_id = ?", (actual_usd, run_id))
        
        # Update cost_buckets
        utc_day = time.strftime("%Y-%m-%d", time.gmtime())
        conn.execute("""
            UPDATE cost_buckets
            SET usd_reserved = usd_reserved - ?, usd_actual = usd_actual + ?
            WHERE persona_key = ? AND utc_day = ?
        """, (estimated, actual_usd, persona, utc_day))
        
    conn.close()

def check_hysteresis_alert(usd_spent: float, budget_cap: float, was_warned: bool) -> tuple[bool, str | None]:
    """Hysteresis-aware alerting: warn once at >=80%, clear only after usage drops below 70%."""
    ratio = usd_spent / budget_cap if budget_cap > 0 else 0.0
    
    if ratio >= 0.80 and not was_warned:
        return True, "⚠️ WARNING: Coder Firm has reached 80% of its budget cap!"
        
    if ratio < 0.70 and was_warned:
        return False, "✅ INFO: Budget usage has dropped below 70%. Alert cleared."
        
    return was_warned, None

def random_hex(length: int) -> str:
    import secrets
    return secrets.token_hex(length // 2)
