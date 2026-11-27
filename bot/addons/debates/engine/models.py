"""Pydantic schemas for POST bodies. v3 STRICT protocol.

Field-shape enforcement (lengths, regex) lives in enforcement.py — these models
just parse JSON into typed fields. Splitting parse vs validate lets us produce
clean 409 messages with specific rule names.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- Door / token ----------

class CreateDoorReq(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    part_id: str = Field(..., min_length=1, max_length=4)
    style: Literal["viktor_v3"] = "viktor_v3"
    debater_ids: list[str] = Field(..., min_length=2, max_length=2)


class CreateDoorRes(BaseModel):
    ok: bool = True
    door_id: str
    tokens: dict[str, str]  # debater_id -> token
    canonical_categories: list[str]
    canonical_clusters: list[str]
    base_url_hint: Optional[str] = None


# ---------- Generic message envelope ----------

class CodeShapeAnchor(BaseModel):
    type: Literal["function", "file", "sql", "regex", "env", "json_schema"]
    value: str = Field(..., min_length=4, max_length=2000)


class FailureMode(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    source_anchor: str = Field(..., min_length=4, max_length=400)
    attack_scenario: str = Field(..., min_length=80, max_length=2000)
    expected_loss: str = Field(..., min_length=4, max_length=200)


class MsgEnvelope(BaseModel):
    """Single envelope for all kinds. enforcement.py picks the relevant fields
    per kind and emits violations for missing/invalid ones."""
    kind: Literal[
        "note",
        "atom-propose",
        "atom-critique",
        "atom-revision",
        "atom-peer-improve",
        "atom-peer-review",
        "atom-adversarial",
        "atom-approve",
        "cluster-propose",
        "cluster-critique",
        "cluster-revision",
        "cluster-approve",
        "final-propose",
        "final-critique",
        "final-revision",
        "final-approve",
    ]
    content: str = Field(..., min_length=1, max_length=64_000)
    # atom-propose fields
    canonical_category_id: Optional[str] = None
    pick: Optional[Literal["A", "B"]] = None
    provocation_level: Optional[Literal["LOW", "MEDIUM", "HIGH"]] = None
    code_shape_anchor: Optional[CodeShapeAnchor] = None
    # atom-critique fields
    target_atom_id: Optional[str] = None
    verdict: Optional[Literal["AGREE", "PARTIAL", "DISAGREE"]] = None
    evidence_quote: Optional[str] = None
    actionable_objection: Optional[str] = None
    # atom-revision fields
    outcome: Optional[Literal["SUBSTANTIVE", "NO_REVISION_NEEDED"]] = None
    revision_triggered_by: Optional[int] = None
    substantive_change_summary: Optional[str] = None
    # atom-adversarial fields
    failure_modes: Optional[list[FailureMode]] = None
    revision_required: Optional[bool] = None
    reason_if_not_required: Optional[str] = None
    recommended_revision_actions: Optional[list[str]] = None
    # atom-peer-improve fields
    improvement_summary: Optional[str] = None
    # cluster-* fields
    cluster_id: Optional[str] = None
    locked_atom_ids: Optional[list[str]] = None


class MsgRes(BaseModel):
    ok: bool
    msg_id: Optional[int] = None
    atom_id: Optional[str] = None
    violations: list[dict] = []
    phase_advanced_to: Optional[str] = None


class ValidateRes(BaseModel):
    ok: bool
    violations: list[dict] = []
    validation_id: Optional[str] = None


class OwnerDirectiveReq(BaseModel):
    kind: Literal["terminate", "force_phase_gate_pass", "inject_note", "pause", "resume"]
    payload: dict = {}
