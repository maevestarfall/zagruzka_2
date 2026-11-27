from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Capability matrix mapping persona keys to allowed actions
CAPABILITIES = {
    "coder_boss": {"plan_tasks", "reserve_budget", "assign_workers", "replan"},
    "coder_reviewer": {"review_code", "score_diff"},
    "coder_tester": {"run_tests", "generate_scorecard"},
    "coder_auditor": {"audit_compliance", "verify_gate"},
    "coder_integrator": {"open_pr", "merge_pr", "git_push"},
    "coder_researcher": {"search_refs", "record_ref_decision"},
    "coder_judge": {"resolve_gate_conflict"},
    "coder_worker_01": {"claim_subtask", "write_code", "request_review"}
}

class PermissionDenied(Exception):
    """Raised when a persona tries to perform an action outside its capability set."""
    pass

def assert_capability(persona: str, action: str) -> None:
    """Enforce capability check before executing any restricted action."""
    # Workers are elastic, so normalize worker keys (e.g. coder_worker_02 -> coder_worker_01)
    norm_persona = persona
    if persona.startswith("coder_worker_"):
        norm_persona = "coder_worker_01"
        
    if norm_persona not in CAPABILITIES or action not in CAPABILITIES[norm_persona]:
        logger.error("Permission denied! Persona '%s' is not allowed to perform action '%s'", persona, action)
        raise PermissionDenied(f"Persona '{persona}' lacks required capability '{action}'")
        
    logger.info("Capability verified: Persona '%s' is allowed to perform action '%s'", persona, action)
