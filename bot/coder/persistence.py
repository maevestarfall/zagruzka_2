from __future__ import annotations

import logging
import os
import sqlite3
import time

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    """ADD COLUMN is idempotent via this guard; SQLite has no ALTER TABLE IF NOT EXISTS."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # Column already exists — nothing to do.


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    """Initialize the SQLite database with WAL mode and create all schema tables."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    
    with conn:
        # Schema migration tracking
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_schema_migrations (
                version INTEGER PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
        """)
        
        # 1. coder_runs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_runs (
                run_id TEXT PRIMARY KEY,
                task_desc TEXT NOT NULL,
                state TEXT NOT NULL,
                workers_count INTEGER NOT NULL,
                budget_cap REAL NOT NULL,
                usd_spent REAL DEFAULT 0.0,
                status_card_message_id INTEGER,
                role_allocation_locked_at TEXT,
                audit_exported_at TEXT,
                created_at TEXT NOT NULL,
                test_failure_context TEXT,
                pr_number INTEGER,
                pr_url TEXT,
                pr_branch TEXT,
                pr_approved_sha TEXT,
                CHECK (workers_count >= 1 AND workers_count <= 20)
            )
        """)
        # Live migration: add new columns to existing databases that were
        # created before v0.4.  SQLite ignores "duplicate column" errors so
        # we guard with a try/except per column.
        _add_column_if_missing(conn, "coder_runs", "test_failure_context", "TEXT")
        _add_column_if_missing(conn, "coder_runs", "pr_number", "INTEGER")
        _add_column_if_missing(conn, "coder_runs", "pr_url", "TEXT")
        _add_column_if_missing(conn, "coder_runs", "pr_branch", "TEXT")
        _add_column_if_missing(conn, "coder_runs", "pr_approved_sha", "TEXT")
        
        # 2. coder_tasks
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_tasks (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                title TEXT NOT NULL,
                state TEXT NOT NULL,
                assigned_worker TEXT NOT NULL,
                boss_approved_no_test_rationale TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES coder_runs(run_id)
            )
        """)
        
        # 3. coder_messages
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                from_persona TEXT NOT NULL,
                to_persona TEXT NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                last_error_class TEXT,
                lease_until TEXT,
                payload_ref TEXT,
                ts TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES coder_runs(run_id),
                CHECK (last_error_class IN ('provider_429', 'provider_5xx', 'timeout', 'validation', 'integrity', 'unknown') OR last_error_class IS NULL)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_coder_messages_state_lease ON coder_messages(state, lease_until);")
        
        # 4. coder_audit_events
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                persona TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                message TEXT NOT NULL,
                retry_family TEXT,
                ts TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES coder_runs(run_id)
            )
        """)
        
        # 5. cost_reservations
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_reservations (
                reservation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                persona TEXT NOT NULL,
                estimated_usd REAL NOT NULL,
                actual_usd REAL DEFAULT 0.0,
                state TEXT NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY (run_id) REFERENCES coder_runs(run_id),
                CHECK (state IN ('RESERVED', 'SETTLED', 'EXPIRED'))
            )
        """)
        
        # 6. cost_buckets
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_buckets (
                persona_key TEXT NOT NULL,
                utc_day TEXT NOT NULL,
                usd_reserved REAL DEFAULT 0.0,
                usd_actual REAL DEFAULT 0.0,
                PRIMARY KEY (persona_key, utc_day)
            )
        """)
        
        # 7. coder_scorecards
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_scorecards (
                scorecard_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                owner_persona TEXT DEFAULT 'coder_tester',
                passed_tests INTEGER NOT NULL,
                failed_tests INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES coder_runs(run_id),
                FOREIGN KEY (task_id) REFERENCES coder_tasks(task_id),
                CHECK (owner_persona = 'coder_tester')
            )
        """)
        
        # 8. coder_external_ref_decisions
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_external_ref_decisions (
                ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_url TEXT NOT NULL,
                integration_mode TEXT NOT NULL,
                applies_to_cluster TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                decided_by TEXT DEFAULT 'coder_researcher',
                CHECK (integration_mode IN ('pattern-only', 'wrap-as-tool', 'vendored-deps'))
            )
        """)
        
        # 9. coder_tool_denied
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_tool_denied (
                run_id TEXT NOT NULL,
                persona TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        
        # 10. coder_worker_runtime
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_worker_runtime (
                worker_id TEXT PRIMARY KEY,
                current_role TEXT NOT NULL,
                cooldown_until INTEGER NOT NULL,
                last_swapped_at INTEGER
            )
        """)
        
        # 11. coder_channel_state
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_channel_state (
                channel TEXT PRIMARY KEY,
                tokens INTEGER NOT NULL,
                last_429_at INTEGER,
                rolling_429_count INTEGER DEFAULT 0
            )
        """)
        
        # 12. coder_bot_global_state
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_bot_global_state (
                last_429_at INTEGER PRIMARY KEY
            )
        """)
        
        # 13. coder_run_metrics
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_run_metrics (
                run_id TEXT PRIMARY KEY,
                total_calls INTEGER,
                total_tokens INTEGER,
                wall_clock_s REAL,
                end_state TEXT
            )
        """)
        
        # 14. coder_aux_grants
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_aux_grants (
                job_id TEXT NOT NULL,
                original_author TEXT NOT NULL,
                granted_persona TEXT NOT NULL,
                granted_until REAL NOT NULL,
                PRIMARY KEY (job_id, original_author, granted_persona)
            )
        """)
        
        # 15. coder_role_swap_events
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_role_swap_events (
                event_id TEXT PRIMARY KEY,
                persona TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                cooldown_until INTEGER NOT NULL,
                reason TEXT NOT NULL
            )
        """)
        
        # 16. coder_boss_leases
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_boss_leases (
                lease_id INTEGER PRIMARY KEY,
                migrator_persona TEXT NOT NULL,
                lease_until REAL NOT NULL
            )
        """)
        
    return conn

def wait_for_schema_version(target: int, max_wait_s: int = 300) -> None:
    """Non-migrator polling loop with exponential backoff; exits with 75 on timeout."""
    start_time = time.time()
    delay = 1.0
    while time.time() - start_time < max_wait_s:
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(version) FROM coder_schema_migrations")
            row = cursor.fetchone()
            conn.close()
            current_version = row[0] if row and row[0] is not None else 0
            if current_version >= target:
                logger.info("Database schema version is up to date: %d", current_version)
                return
        except Exception as e:
            logger.warning("Error checking schema version: %s", e)
            
        time.sleep(delay)
        delay = min(delay * 2.0, 60.0)
        
    logger.critical("Database schema migration timed out. Exiting with 75.")
    raise SystemExit(75)

def acquire_migrator_lease(persona: str, now: float) -> bool:
    """Acquire a 15-minute migrator lease to safely apply DDL changes."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coder_migrator_lease (
                lease_id INTEGER PRIMARY KEY,
                migrator_persona TEXT NOT NULL,
                lease_until REAL NOT NULL
            )
        """)
        cursor = conn.cursor()
        cursor.execute("SELECT lease_until, migrator_persona FROM coder_migrator_lease WHERE lease_id = 1")
        row = cursor.fetchone()
        if row:
            lease_until, holder = row[0], row[1]
            if lease_until > now and holder != persona:
                conn.close()
                return False # Lease is held by another persona
            # Acquire/renew lease
            conn.execute(
                "UPDATE coder_migrator_lease SET migrator_persona = ?, lease_until = ? WHERE lease_id = 1",
                (persona, now + 900.0)
            )
        else:
            conn.execute(
                "INSERT INTO coder_migrator_lease (lease_id, migrator_persona, lease_until) VALUES (1, ?, ?)",
                (persona, now + 900.0)
            )
    conn.close()
    return True
