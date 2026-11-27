"""Risk-based permissions for tool calls and external side effects."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class Risk(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    needs_approval: bool
    reason: str


class PermissionEngine:
    """Deny-by-default capability and approval policy."""

    def __init__(self, capabilities: set[str] | None = None, auto_approve_through: Risk = Risk.LOW):
        self.capabilities = capabilities or set()
        self.auto_approve_through = auto_approve_through

    def decide(self, capability: str, risk: str | Risk, context: dict[str, Any] | None = None) -> Decision:
        level = risk if isinstance(risk, Risk) else Risk[risk.upper()]
        if capability not in self.capabilities:
            return Decision(False, False, f"capability '{capability}' is not granted")
        if level > self.auto_approve_through:
            return Decision(False, True, f"{level.name.lower()} risk requires owner approval")
        return Decision(True, False, "allowed by task policy")

    @staticmethod
    def publication_policy(action: str) -> Decision:
        if action in {"push", "merge", "publish", "deploy", "rollback", "self_update"}:
            return Decision(False, True, f"external action '{action}' requires explicit approval")
        return Decision(True, False, "local read-only action")
