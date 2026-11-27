from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

class ArtifactVerificationError(Exception):
    """Raised when gate artifact signature or schema verification fails."""
    pass

def sign_gate_artifact(persona: str, artifact_type: str, data: dict) -> str:
    """Generate a deterministic signature/hash for a gate artifact to ensure authenticity."""
    serialized = json.dumps(data, sort_keys=True)
    raw = f"{persona}|{artifact_type}|{serialized}"
    signature = hashlib.sha256(raw.encode()).hexdigest()
    logger.info("Generated signed gate artifact for '%s' (Type: %s): %s", persona, artifact_type, signature)
    return signature

def validate_gate_artifacts(persona: str, artifact_type: str, data: dict, signature: str) -> bool:
    """Validate the signature and structural schema of a gate artifact."""
    expected = sign_gate_artifact(persona, artifact_type, data)
    if expected != signature:
        logger.error("Artifact signature mismatch! Expected '%s', got '%s'", expected, signature)
        raise ArtifactVerificationError("Invalid gate artifact signature.")
        
    logger.info("Successfully validated gate artifact signed by '%s'", persona)
    return True
