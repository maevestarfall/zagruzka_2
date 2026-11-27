from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"
DAILY_JUDGE_BUDGET_CAP = 1.00 # Max 1.00 USD spent on Judge daily

class JudgeBudgetExceeded(Exception):
    """Raised when the daily Judge-cost budget cap is exceeded."""
    pass

def should_escalate(reviewer_score: float, auditor_verdict: bool) -> bool:
    """Escalate to Judge if Reviewer and Auditor disagree (e.g. score >= 7 but Auditor fails, or vice versa)."""
    reviewer_pass = reviewer_score >= 7.0
    if reviewer_pass != auditor_verdict:
        logger.info("Conflict detected between Reviewer (Pass: %s) and Auditor (Pass: %s). Escalating to Judge.", reviewer_pass, auditor_verdict)
        return True
    return False

def resolve_conflict(run_id: str, dispute_desc: str) -> str:
    """Resolve the conflict using the Judge role, checking the daily budget cap."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    utc_day = time.strftime("%Y-%m-%d", time.gmtime())
    
    # Check Judge daily spend
    cursor.execute("SELECT SUM(usd_actual) FROM cost_buckets WHERE persona_key = 'coder_judge' AND utc_day = ?", (utc_day,))
    row = cursor.fetchone()
    spent = row[0] if row and row[0] is not None else 0.0
    
    if spent >= DAILY_JUDGE_BUDGET_CAP:
        logger.critical("Judge daily budget cap exceeded ($%.2f spent today)! Falling back to owner queue.", spent)
        conn.close()
        raise JudgeBudgetExceeded("Judge daily budget cap exceeded. Conflict escalated to Owner Telegram queue.")
        
    # Simulate Judge decision and cost
    judge_cost = 0.05
    with conn:
        conn.execute("""
            INSERT INTO cost_buckets (persona_key, utc_day, usd_actual)
            VALUES ('coder_judge', ?, ?)
            ON CONFLICT (persona_key, utc_day) DO UPDATE SET usd_actual = usd_actual + ?
        """, (utc_day, judge_cost, judge_cost))
        
        # Log to audit trail
        conn.execute("""
            INSERT INTO coder_audit_events (run_id, persona, event_kind, message, ts)
            VALUES (?, 'coder_judge', 'dispute_resolution', ?, ?)
        """, (run_id, f"Судья разрешил спор: {dispute_desc}. Вердикт вынесен (Стоимость: {judge_cost} USD).", time.strftime("%Y-%m-%d %H:%M:%S")))
        
    conn.close()
    logger.info("Judge resolved dispute for run '%s' (Cost: %f USD)", run_id, judge_cost)
    return "APPROVED_BY_JUDGE"
