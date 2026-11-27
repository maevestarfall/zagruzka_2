from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = "data/coder.db"
LILUSH_CODER_INLINE_MAX_BYTES = 3072
LILUSH_CODER_PRIORITY_BYPASS_PER_PERSONA_PER_DAY = 200

# Controlled error classes
ERROR_CLASSES = {"provider_429", "provider_5xx", "timeout", "validation", "integrity", "unknown"}

# Valid persona keys
PERSONA_KEYS = {
    "coder_boss", "coder_reviewer", "coder_tester", "coder_auditor",
    "coder_integrator", "coder_researcher", "coder_judge", "coder_worker_01"
}

class PayloadIntegrityError(Exception):
    """Raised when read-side sha256 or byte-count verification fails."""
    pass

@dataclass(frozen=True)
class PayloadRef:
    path: str | None  # None for inline
    sha256: str
    byte_count: int
    producer_persona: str
    kind: str
    created_at_utc: str

def write_payload_ref(run_id: str, persona: str, kind: str, payload_bytes: bytes) -> str:
    """Size-tiered payload write: inline if small, else content-addressed file with fsync + atomic rename."""
    if len(payload_bytes) <= LILUSH_CODER_INLINE_MAX_BYTES:
        # Save inline
        payload_str = payload_bytes.decode(errors="replace")
        return f"inline:{payload_str}"
        
    # Content-addressed file storage
    sha256_hash = hashlib.sha256(payload_bytes).hexdigest()
    dir_path = Path(f"coder_runs/{run_id}/artifacts")
    dir_path.mkdir(parents=True, exist_ok=True)
    
    file_path = dir_path / f"{sha256_hash}.json"
    temp_file_path = dir_path / f"{sha256_hash}.tmp"
    
    # Write to temp file first, fsync and rename atomically
    with open(temp_file_path, "wb") as f:
        f.write(payload_bytes)
        f.flush()
        os.fsync(f.fileno())
        
    os.rename(temp_file_path, file_path)
    
    # Return file ref
    ref_dict = {
        "path": str(file_path),
        "sha256": sha256_hash,
        "byte_count": len(payload_bytes),
        "producer_persona": persona,
        "kind": kind,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    return f"file:{json.dumps(ref_dict)}"

def read_payload_ref(payload_ref_str: str) -> bytes:
    """Read and verify the payload, raising PayloadIntegrityError on mismatch."""
    if payload_ref_str.startswith("inline:"):
        return payload_ref_str[7:].encode()
        
    if not payload_ref_str.startswith("file:"):
        raise ValueError("Invalid payload reference format.")
        
    ref_dict = json.loads(payload_ref_str[5:])
    path = ref_dict["path"]
    expected_sha256 = ref_dict["sha256"]
    expected_bytes = ref_dict["byte_count"]
    producer = ref_dict["producer_persona"]
    
    # Verify producer persona
    if producer not in PERSONA_KEYS:
        raise PayloadIntegrityError(f"Payload producer persona '{producer}' is not registered.")
        
    if not os.path.exists(path):
        raise PayloadIntegrityError(f"Payload file not found: {path}")
        
    # Read content
    with open(path, "rb") as f:
        content = f.read()
        
    # Verification
    actual_bytes = len(content)
    actual_sha256 = hashlib.sha256(content).hexdigest()
    
    if actual_bytes != expected_bytes:
        raise PayloadIntegrityError(f"Byte count mismatch: expected {expected_bytes}, got {actual_bytes}")
        
    if actual_sha256 != expected_sha256:
        raise PayloadIntegrityError(f"SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}")
        
    return content

# ---- SQLite Message Bus ----

def acquire_next_message(persona: str, now_str: str) -> dict | None:
    """Atomically acquire the next leased message for the given persona from the queue."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    
    with conn:
        # Find oldest pending or expired lease message
        cursor.execute("""
            SELECT id, run_id, from_persona, to_persona, kind, payload_ref, retry_count
            FROM coder_messages
            WHERE to_persona = ? AND (state = 'pending' OR (state = 'processing' AND lease_until < ?))
            ORDER BY ts ASC LIMIT 1
        """, (persona, now_str))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
            
        msg_id, run_id, from_p, to_p, kind, payload_ref, retry_count = row
        # Set lease to now + 30 minutes
        lease_until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 1800))
        
        conn.execute("""
            UPDATE coder_messages
            SET state = 'processing', lease_until = ?, retry_count = retry_count + 1
            WHERE id = ?
        """, (lease_until, msg_id))
        
    conn.close()
    return {
        "id": msg_id,
        "run_id": run_id,
        "from_persona": from_p,
        "to_persona": to_p,
        "kind": kind,
        "payload_ref": payload_ref,
        "retry_count": retry_count + 1
    }

def release_lease(message_id: int, terminal_state: str, error_class: str = None) -> None:
    """Release the lease and set the terminal state (e.g., 'completed', 'failed', 'dead')."""
    if error_class and error_class not in ERROR_CLASSES:
        error_class = "unknown"
        
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    with conn:
        conn.execute("""
            UPDATE coder_messages
            SET state = ?, lease_until = NULL, last_error_class = ?
            WHERE id = ?
        """, (terminal_state, error_class, message_id))
    conn.close()

# ---- Hourly Janitor Pass ----

def janitor_pass() -> dict[str, int]:
    """Janitor pass: re-queues stuck messages and cleans stale channel rows."""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    
    requeued_count = 0
    cleaned_channels = 0
    
    with conn:
        # 1. Re-queue stuck messages
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE coder_messages
            SET state = 'pending', lease_until = NULL, last_error_class = 'timeout'
            WHERE state = 'processing' AND lease_until < ?
        """, (now_str,))
        requeued_count = cursor.rowcount
        
        # 2. Clean up stale channel state rows (inactive > 7 days)
        # For simplicity, we just clear older rows in this dmeo database
        cursor.execute("DELETE FROM coder_channel_state WHERE last_429_at < ?", (int(time.time()) - 7 * 86400,))
        cleaned_channels = cursor.rowcount
        
    conn.close()
    return {
        "requeued_messages": requeued_count,
        "cleaned_channels": cleaned_channels
    }
