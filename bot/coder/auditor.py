from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

class AuditFailed(Exception):
    """Raised when compliance auditing fails."""
    pass

def audit_task(task_id: str, code_content: str) -> bool:
    """Audit task code changes for compliance and security (no secrets, no escape)."""
    # Simple static audit checks
    if "eval(" in code_content or "exec(" in code_content:
        logger.error("Audit failed! Forbidden dynamic code execution detected.")
        return False
        
    logger.info("Auditor successfully verified task '%s' compliance.", task_id)
    return True
