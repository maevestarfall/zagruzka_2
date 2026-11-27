"""Coder FSM — atomic state transitions backed by SQLite.

State graph
-----------

    INIT
      ↓
    PLANNING
      ↓
    CODING  ←─────────────────────────────────────────────────┐
      ↓                                                        │
    REVIEW_PENDING ──(reject)──→ CODING                       │
      ↓ (approve)                                             │
    TEST_PENDING ──(FAIL / ERROR)──→ CODING (with error ctx)  │
      ↓ (PASS)                                                │
    AUDIT_PENDING ──(reject)──→ CODING                        │
      ↓ (approve)                                             │
    INTEGRATE_PENDING                                          │
      ↓                                                        │
    PR_REVIEW_PENDING ──(changes requested)──────────────────┘
      ↓ (approved)
    DONE

Terminal failure states reachable from (almost) anywhere:
    FAILED_BUDGET, FAILED_OWNER_INPUT, FAILED_GATE, FAILED_INFRA, PAUSED_BY_OWNER

PR_REVIEW_PENDING (new in v0.4)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
After the integrator opens a GitHub PR the FSM enters PR_REVIEW_PENDING.
Two outcomes:

* **Reviewer approves** → transition to DONE.
* **Reviewer requests changes** → transition back to CODING, storing the
  review comments as ``test_failure_context`` so the LLM gets the feedback
  as part of its next prompt.

Idempotent replay
~~~~~~~~~~~~~~~~~
Each event is content-addressed: ``event_id`` is a deterministic 96-bit
hash of ``run_id|kind|payload_sha256``.  Re-delivering the same event is
a no-op; the stored row is returned with ``replayed=True``.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from enum import Enum

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"


class CoderRunState(Enum):
    INIT                = "init"
    PLANNING            = "planning"
    CODING              = "coding"
    REVIEW_PENDING      = "review_pending"
    TEST_PENDING        = "test_pending"
    AUDIT_PENDING       = "audit_pending"
    INTEGRATE_PENDING   = "integrate_pending"
    PR_REVIEW_PENDING   = "pr_review_pending"   # ← new in v0.4
    DONE                = "done"
    FAILED_BUDGET       = "failed_budget"
    FAILED_OWNER_INPUT  = "failed_owner_input"
    FAILED_GATE         = "failed_gate"
    FAILED_INFRA        = "failed_infra"
    PAUSED_BY_OWNER     = "paused_by_owner"


# States that represent a successful / failed terminal — no further transitions.
TERMINAL_STATES: frozenset[CoderRunState] = frozenset({
    CoderRunState.DONE,
    CoderRunState.FAILED_BUDGET,
    CoderRunState.FAILED_OWNER_INPUT,
    CoderRunState.FAILED_GATE,
    CoderRunState.FAILED_INFRA,
})

# Kill-switch states — reachable from ANY state regardless of the graph.
KILL_SWITCH_STATES: frozenset[CoderRunState] = frozenset({
    CoderRunState.FAILED_INFRA,
    CoderRunState.PAUSED_BY_OWNER,
})

ALLOWED_TRANSITIONS: dict[CoderRunState, set[CoderRunState]] = {
    CoderRunState.INIT: {
        CoderRunState.PLANNING,
        CoderRunState.FAILED_INFRA,
    },
    CoderRunState.PLANNING: {
        CoderRunState.CODING,
        CoderRunState.FAILED_BUDGET,
        CoderRunState.FAILED_INFRA,
    },
    CoderRunState.CODING: {
        CoderRunState.REVIEW_PENDING,
        CoderRunState.FAILED_INFRA,
        CoderRunState.FAILED_BUDGET,
    },
    CoderRunState.REVIEW_PENDING: {
        CoderRunState.TEST_PENDING,     # reviewer approves
        CoderRunState.CODING,           # reviewer rejects → fix and re-submit
        CoderRunState.FAILED_GATE,
        CoderRunState.FAILED_INFRA,
    },
    CoderRunState.TEST_PENDING: {
        CoderRunState.AUDIT_PENDING,    # tests pass
        CoderRunState.CODING,           # tests fail → fix loop (with error ctx)
        CoderRunState.FAILED_GATE,
        CoderRunState.FAILED_INFRA,
    },
    CoderRunState.AUDIT_PENDING: {
        CoderRunState.INTEGRATE_PENDING,  # auditor approves
        CoderRunState.CODING,             # auditor rejects
        CoderRunState.FAILED_GATE,
        CoderRunState.FAILED_INFRA,
    },
    CoderRunState.INTEGRATE_PENDING: {
        CoderRunState.PR_REVIEW_PENDING,  # PR opened, waiting for reviewer
        CoderRunState.DONE,               # direct done (no PR flow configured)
        CoderRunState.FAILED_OWNER_INPUT,
        CoderRunState.FAILED_INFRA,
    },
    # ── new ──────────────────────────────────────────────────────────────────
    CoderRunState.PR_REVIEW_PENDING: {
        CoderRunState.DONE,             # reviewer approved → merge
        CoderRunState.CODING,           # reviewer requested changes → fix loop
        CoderRunState.FAILED_GATE,
        CoderRunState.FAILED_INFRA,
    },
}


class IllegalTransition(Exception):
    """Raised when trying to transition to an unallowed FSM state."""


class EventStateMismatch(Exception):
    """Raised when there is a concurrent state modification conflict."""


# ─── Event ID ─────────────────────────────────────────────────────────────────

def derive_event_id(run_id: str, kind: str, payload_sha256: str) -> str:
    """Derive a deterministic 96-bit (24 hex chars) event_id from content."""
    raw = f"{run_id}|{kind}|{payload_sha256}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ─── Core transition ──────────────────────────────────────────────────────────

def apply_event(
    run_id: str,
    event_id: str,
    expected: CoderRunState,
    target: CoderRunState,
    *,
    failure_context: str | None = None,
) -> dict:
    """Apply an FSM transition atomically with expected-state guard.

    Parameters
    ----------
    run_id : str
        The coder run being transitioned.
    event_id : str
        Content-addressed event identifier (idempotency key).
    expected : CoderRunState
        State the run must currently be in.
    target : CoderRunState
        State to transition into.
    failure_context : str | None
        When rolling back to CODING (e.g. after test failure or PR change
        request), store this text in ``coder_runs.test_failure_context`` so
        the orchestrator can attach it to the LLM prompt.

    Returns
    -------
    dict with ``event_id``, ``replayed``, ``from_state``, ``to_state``,
    ``applied_at``.
    """
    # Validate transition
    allowed = ALLOWED_TRANSITIONS.get(expected, set())
    if target not in allowed and target not in KILL_SWITCH_STATES:
        raise IllegalTransition(
            f"Transition {expected.value} → {target.value} is not allowed."
        )

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    applied_at: str | None = None

    try:
        with conn:
            # Ensure schema
            conn.execute("""
                CREATE TABLE IF NOT EXISTS coder_fsm_events (
                    event_id   TEXT PRIMARY KEY,
                    run_id     TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state   TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
            """)

            # Idempotent replay: if the event was already applied, return it.
            cursor.execute(
                "SELECT from_state, to_state, applied_at "
                "FROM coder_fsm_events WHERE event_id = ?",
                (event_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "event_id":   event_id,
                    "replayed":   True,
                    "from_state": row[0],
                    "to_state":   row[1],
                    "applied_at": row[2],
                }

            # Verify current run state
            cursor.execute(
                "SELECT state FROM coder_runs WHERE run_id = ?",
                (run_id,),
            )
            run_row = cursor.fetchone()
            if not run_row:
                raise ValueError(f"Run '{run_id}' not found.")

            current = run_row[0]
            if current != expected.value:
                raise EventStateMismatch(
                    f"Expected state '{expected.value}', but run is in '{current}'"
                )

            applied_at = time.strftime("%Y-%m-%d %H:%M:%S")

            # Insert event record
            conn.execute(
                "INSERT INTO coder_fsm_events "
                "(event_id, run_id, from_state, to_state, applied_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, run_id, expected.value, target.value, applied_at),
            )

            # Update run state
            conn.execute(
                "UPDATE coder_runs SET state = ? WHERE run_id = ?",
                (target.value, run_id),
            )

            # When rolling back to CODING, persist the error/review context
            # so the orchestrator can feed it to the LLM as context.
            if target == CoderRunState.CODING and failure_context:
                try:
                    conn.execute(
                        "UPDATE coder_runs SET test_failure_context = ? "
                        "WHERE run_id = ?",
                        (failure_context[:4000], run_id),  # cap at 4k chars
                    )
                except Exception:  # noqa: BLE001 — column may not exist yet
                    pass

        return {
            "event_id":   event_id,
            "replayed":   False,
            "from_state": expected.value,
            "to_state":   target.value,
            "applied_at": applied_at,
        }
    finally:
        conn.close()


# ─── Convenience helpers ──────────────────────────────────────────────────────

def get_run_state(run_id: str) -> CoderRunState | None:
    """Fetch the current FSM state for a run.  Returns None if not found."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT state FROM coder_runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return CoderRunState(row[0])
    finally:
        conn.close()


def get_failure_context(run_id: str) -> str:
    """Return the stored test/review failure context for a run (may be empty)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT test_failure_context FROM coder_runs WHERE run_id = ?",
                (run_id,),
            )
            row = cursor.fetchone()
            return str(row[0] or "") if row and row[0] else ""
        except Exception:  # noqa: BLE001 — column may not exist in old DBs
            return ""
    finally:
        conn.close()


def clear_failure_context(run_id: str) -> None:
    """Erase the failure context after the LLM has consumed it."""
    try:
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute(
                "UPDATE coder_runs SET test_failure_context = NULL "
                "WHERE run_id = ?",
                (run_id,),
            )
        conn.close()
    except Exception:  # noqa: BLE001
        pass
