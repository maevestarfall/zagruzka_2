from __future__ import annotations

import re

# Strict naming convention regex for Coder personas
PERSONA_KEY_RE = re.compile(
    r"^coder_(boss|worker_\d{2}|reviewer|auditor|tester|integrator|researcher|judge)$"
)

# Sibling personas to check for collisions
SIBLING_PERSONAS = {
    "downloader", "analyzer", "editor", "seo", "publisher", "miro"
}

class PersonaNamespaceCollision(Exception):
    """Raised when coder personas collide with sibling personas."""
    pass

def validate_persona_key(key: str) -> bool:
    """Validate a persona key against the strict regex."""
    return bool(PERSONA_KEY_RE.match(key))

def assert_persona_namespace(registered_keys: list[str]) -> None:
    """Boot-time assertion that coder_personas ∩ sibling_personas = ∅."""
    for key in registered_keys:
        if not validate_persona_key(key):
            raise ValueError(f"Persona key '{key}' violates strict naming convention '{PERSONA_KEY_RE.pattern}'")
            
        # Extract base name to check for sibling collisions
        parts = key.split("_", 1)
        if len(parts) > 1:
            base_name = parts[1]
            if base_name in SIBLING_PERSONAS:
                raise PersonaNamespaceCollision(f"Coder persona '{key}' collides with sibling persona namespace '{base_name}'")

def list_personas() -> list[str]:
    """Return the list of canonical Coder Firm personas."""
    personas = [
        "coder_boss",
        "coder_reviewer",
        "coder_tester",
        "coder_auditor",
        "coder_integrator",
        "coder_researcher",
        "coder_judge"
    ]
    # Elastic workers
    for i in range(1, 21):
        personas.append(f"coder_worker_{i:02d}")
    return personas
