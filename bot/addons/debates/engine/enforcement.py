"""Enforcement engine — all PRAVILO_DEBAT_v3 validators.

`validate_envelope` is stateless: given an envelope + door context, returns a
list of violations. Empty list = OK.

`apply_envelope` is the stateful entry: writes msg to DB, updates atoms table,
runs phase gates when transitions happen, and emits the response.

Designed so that POST /validate calls `validate_envelope` only; POST /msg runs
validate first and only writes when violations == [].
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

from . import banned_phrases
from .canonical import PartConfig
from .models import MsgEnvelope


# Minimum structured failure_modes required for an atom-adversarial message.
# Kept in sync with the debater docs (PRAVILO_DEBAT_v3 §6 says "Минимум 3").
ADVERSARIAL_MIN_FAILURE_MODES = 3

PHASE_ORDER = (
    "voice_manifest",
    "atoms_propose",
    "atoms_critique",
    "atoms_revision",
    "atoms_peer_improve",
    "atoms_adversarial",
    "atoms_approve",
    "phase_gate_a",
    "clusters_propose",
    "clusters_critique",
    "clusters_revision",
    "clusters_approve",
    "phase_gate_b",
    "final_propose",
    "final_critique",
    "final_revision",
    "final_approve",
    "phase_gate_c",
    "done",
    "terminated",
)


# Map kind -> allowed phases. Server rejects out-of-phase POSTs (anti-skip §2).
KIND_TO_PHASES = {
    "note": ("voice_manifest", "atoms_propose", "atoms_critique", "atoms_revision",
             "atoms_peer_improve", "atoms_adversarial", "atoms_approve",
             "clusters_propose", "clusters_critique", "clusters_revision",
             "clusters_approve", "final_propose", "final_critique",
             "final_revision", "final_approve"),
    "atom-propose": ("atoms_propose", "atoms_revision"),
    "atom-critique": ("atoms_critique",),
    "atom-revision": ("atoms_revision",),
    "atom-peer-improve": ("atoms_peer_improve",),
    "atom-peer-review": ("atoms_peer_improve",),
    "atom-adversarial": ("atoms_adversarial",),
    "atom-approve": ("atoms_approve",),
    "cluster-propose": ("clusters_propose",),
    "cluster-critique": ("clusters_critique",),
    "cluster-revision": ("clusters_revision",),
    "cluster-approve": ("clusters_approve",),
    "final-propose": ("final_propose",),
    "final-critique": ("final_critique",),
    "final-revision": ("final_revision",),
    "final-approve": ("final_approve",),
}


# ---------- voice manifest regex (§3) ----------

VOICE_MANIFEST_RE = re.compile(
    r"###\s*Voice manifest.*"
    r"\*\*I will PUSH BACK ON:\*\*.*"
    r"\*\*I will REFUSE TO COMPROMISE ON:\*\*.*"
    r"\*\*Adversarial targets I will look for.*"
    r"\*\*Self-declared bias warning:\*\*",
    re.DOTALL,
)

NUMBERED_ITEM_RE = re.compile(r"\n\s*\d+\.\s+(.{40,})")


def voice_manifest_violations(text: str) -> list[dict]:
    """Check §3 voice manifest format. Returns violations list."""
    vs: list[dict] = []
    if not VOICE_MANIFEST_RE.search(text):
        vs.append({"rule": "voice_manifest_malformed", "detail": "missing one of the 4 mandatory headers"})
        return vs
    # split sections and count ≥5 items each ≥40 chars
    push = _section(text, "PUSH BACK ON")
    refuse = _section(text, "REFUSE TO COMPROMISE ON")
    adv = _section(text, "Adversarial targets")
    for label, body in (("PUSH BACK ON", push), ("REFUSE TO COMPROMISE ON", refuse),
                        ("Adversarial targets", adv)):
        items = NUMBERED_ITEM_RE.findall(body or "")
        if len(items) < 5:
            vs.append({"rule": "voice_manifest_malformed",
                       "detail": f"section '{label}' has {len(items)} numbered items ≥40 chars; need ≥5"})
    if "Self-declared bias warning" not in text:
        vs.append({"rule": "voice_manifest_malformed", "detail": "missing Self-declared bias warning"})
    return vs


def _section(text: str, start_marker: str) -> str:
    """Slice text from **start_marker...** header up to the next \\n** header
    line or end. Robust to nested `**` characters in the body."""
    i = text.find(start_marker)
    if i < 0:
        return ""
    # find end of the header line (start of the body)
    nl = text.find("\n", i)
    if nl < 0:
        return ""
    # find next markdown header that starts a new section: line starts with **
    rest = text[nl:]
    j = rest.find("\n**")
    if j < 0:
        return rest
    return rest[:j]


# ---------- implementer brief (§8) ----------

BRIEF_REQUIRED_HEADERS = (
    "**Locked atoms in this cluster:**",
    "**Files to create / modify:**",
    "**Hard rules (do NOT violate):**",
    "**Out of scope (do NOT touch):**",
    "**Definition of done:**",
    "**PR title:**",
    "**Public API",   # "Public API exposed" — match prefix
    "**Threat model",
    "**Fail-closed mode",
)


def implementer_brief_violations(text: str, *, context: str = "cluster-propose") -> list[dict]:
    vs: list[dict] = []
    for h in BRIEF_REQUIRED_HEADERS:
        if h not in text:
            vs.append({"rule": "implementer_brief_malformed",
                       "detail": f"[{context}] missing header: {h.strip('*')}"})
    # ≥3 hard rules (numbered list items inside the section)
    hr_sec = _between(text, "**Hard rules (do NOT violate):**", "**Out of scope")
    if hr_sec is not None and len(re.findall(r"\n\s*\d+\.\s+\S", hr_sec)) < 3:
        vs.append({"rule": "implementer_brief_malformed",
                   "detail": f"[{context}] need ≥3 numbered hard rules"})
    # ≥2 out-of-scope items (bullet list)
    oos_sec = _between(text, "**Out of scope (do NOT touch):**", "**Definition of done:**")
    if oos_sec is not None and len(re.findall(r"\n\s*[-*]\s+\S", oos_sec)) < 2:
        vs.append({"rule": "implementer_brief_malformed",
                   "detail": f"[{context}] need ≥2 out-of-scope bullets"})
    # ≥4 definition-of-done checkbox items
    dod_sec = _between(text, "**Definition of done:**", "**PR title:**")
    if dod_sec is not None and len(re.findall(r"\n\s*-\s*\[\s*\]\s+\S", dod_sec)) < 4:
        vs.append({"rule": "implementer_brief_malformed",
                   "detail": f"[{context}] need ≥4 '- [ ]' done items"})
    # ≥2 files in files-to-create section (table row OR bullet)
    files_sec = _between(text, "**Files to create / modify:**", "**Hard rules")
    if files_sec is not None:
        rows = len(re.findall(r"\n\s*\|[^|\n]+\|[^\n]+\|", files_sec))
        bullets = len(re.findall(r"\n\s*[-*]\s+`[^`]+`", files_sec))
        if rows + bullets < 2:
            vs.append({"rule": "implementer_brief_malformed",
                       "detail": f"[{context}] need ≥2 file entries"})
    return vs


def _between(text: str, start: str, end: str) -> Optional[str]:
    i = text.find(start)
    if i < 0:
        return None
    j = text.find(end, i + len(start))
    return text[i: j if j > 0 else len(text)]


# ---------- final-propose structure (§9) ----------

FINAL_SECTIONS = ("## A1.", "## A2.", "## A3.", "## A4.", "## A5.", "## A6.")


def final_propose_violations(text: str, part_cfg: PartConfig) -> list[dict]:
    vs: list[dict] = []
    for sec in FINAL_SECTIONS:
        if sec not in text:
            vs.append({"rule": "final_spec_malformed", "detail": f"missing section {sec}"})
    # implementer briefs in A2
    a2 = _between(text, "## A2.", "## A3.")
    brief_count = len(re.findall(r"###\s*IMPLEMENTER BRIEF\s*[—-]", a2 or ""))
    if brief_count < part_cfg.min_final_briefs:
        vs.append({"rule": "final_spec_malformed",
                   "detail": f"A2 has {brief_count} implementer briefs, need ≥{part_cfg.min_final_briefs}"})
    if brief_count > part_cfg.max_final_briefs:
        vs.append({"rule": "final_spec_malformed",
                   "detail": f"A2 has {brief_count} briefs, max {part_cfg.max_final_briefs}"})
    # validate each brief structure (§8)
    for m in re.finditer(r"(###\s*IMPLEMENTER BRIEF[\s\S]+?)(?=\n###\s*IMPLEMENTER BRIEF|\Z)",
                         a2 or ""):
        sub_vs = implementer_brief_violations(m.group(1), context="final-propose:brief")
        vs.extend(sub_vs)
    # A3 must have markdown table with at least header row
    a3 = _between(text, "## A3.", "## A4.")
    if a3 and not re.search(r"\|\s*cluster_id\s*\|", a3, re.IGNORECASE):
        vs.append({"rule": "final_spec_malformed",
                   "detail": "A3 missing markdown table header with cluster_id column"})
    # size bounds 8KB..40KB
    size = len(text.encode("utf-8"))
    if size < 8_000:
        vs.append({"rule": "final_spec_malformed",
                   "detail": f"final-propose only {size} bytes, need ≥8000"})
    if size > 40_000:
        vs.append({"rule": "final_spec_malformed",
                   "detail": f"final-propose {size} bytes, max 40000"})
    return vs


# ---------- per-kind envelope validators ----------

def _check_atom_propose(env: MsgEnvelope, part_cfg: PartConfig) -> list[dict]:
    vs: list[dict] = []
    if not env.canonical_category_id:
        vs.append({"rule": "missing_field", "detail": "canonical_category_id"})
    elif env.canonical_category_id not in part_cfg.canonical_atom_categories:
        vs.append({"rule": "unknown_canonical_category",
                   "detail": env.canonical_category_id})
    if env.pick not in ("A", "B"):
        vs.append({"rule": "missing_field", "detail": "pick must be A or B"})
    if env.provocation_level not in ("LOW", "MEDIUM", "HIGH"):
        vs.append({"rule": "missing_field", "detail": "provocation_level"})
    if not env.code_shape_anchor:
        vs.append({"rule": "missing_field", "detail": "code_shape_anchor"})
    # Quality: minimum content length for substantive proposals
    if len(env.content) < 200:
        vs.append({"rule": "atom_content_too_short",
                   "detail": f"atom content must be ≥200 chars, got {len(env.content)}"})
    return vs


def _check_atom_critique(env: MsgEnvelope, atoms_by_id: dict) -> list[dict]:
    vs: list[dict] = []
    if not env.target_atom_id:
        vs.append({"rule": "missing_field", "detail": "target_atom_id"})
        return vs
    target = atoms_by_id.get(env.target_atom_id)
    if not target:
        vs.append({"rule": "unknown_target_atom", "detail": env.target_atom_id})
        return vs
    if env.verdict not in ("AGREE", "PARTIAL", "DISAGREE"):
        vs.append({"rule": "missing_field", "detail": "verdict"})
    if not env.evidence_quote or len(env.evidence_quote) < 30:
        vs.append({"rule": "evidence_quote_too_short", "detail": "need ≥30 chars"})
    elif env.evidence_quote.lower() not in target["content"].lower():
        vs.append({"rule": "evidence_quote_not_found",
                   "detail": f"quote not substring of {env.target_atom_id}"})
    if not env.actionable_objection or len(env.actionable_objection) < 80:
        vs.append({"rule": "actionable_objection_too_short",
                   "detail": "need ≥80 chars"})
    return vs


def _check_atom_revision(env: MsgEnvelope, atoms_by_id: dict,
                         pending_revision_atoms: dict) -> list[dict]:
    vs: list[dict] = []
    if not env.target_atom_id:
        vs.append({"rule": "missing_field", "detail": "target_atom_id"})
        return vs
    if env.target_atom_id not in atoms_by_id:
        vs.append({"rule": "unknown_target_atom", "detail": env.target_atom_id})
    if env.outcome not in ("SUBSTANTIVE", "NO_REVISION_NEEDED"):
        vs.append({"rule": "missing_field", "detail": "outcome"})
    if env.outcome == "SUBSTANTIVE":
        if not env.substantive_change_summary or len(env.substantive_change_summary) < 50:
            vs.append({"rule": "substantive_change_summary_too_short",
                       "detail": "need ≥50 chars"})
    # adversarial→revision linkage (§6): if target atom has pending adversarial
    # from this debater's opponent flagged revision_required, this revision
    # MUST link back via revision_triggered_by
    pending = pending_revision_atoms.get(env.target_atom_id)
    if pending and env.revision_triggered_by != pending:
        vs.append({"rule": "adversarial_revision_link_missing",
                   "detail": f"target {env.target_atom_id} has pending_revision_from={pending}; revision_triggered_by must reference it"})
    return vs


def _check_atom_peer_improve(env: MsgEnvelope, debater_id: str,
                              atoms_by_id: dict) -> list[dict]:
    """Validate atom-peer-improve: debater improves their OWN atom."""
    vs: list[dict] = []
    if not env.target_atom_id:
        vs.append({"rule": "missing_field", "detail": "target_atom_id"})
        return vs
    target = atoms_by_id.get(env.target_atom_id)
    if not target:
        vs.append({"rule": "unknown_target_atom", "detail": env.target_atom_id})
        return vs
    # Must be YOUR OWN atom
    if target["author_debater_id"] != debater_id:
        vs.append({"rule": "peer_improve_wrong_owner",
                   "detail": f"atom {env.target_atom_id} belongs to "
                             f"{target['author_debater_id']}, not you ({debater_id})"})
    if not env.improvement_summary or len(env.improvement_summary) < 50:
        vs.append({"rule": "improvement_summary_too_short",
                   "detail": "need ≥50 chars"})
    if len(env.content) < 200:
        vs.append({"rule": "atom_content_too_short",
                   "detail": f"improved atom content must be ≥200 chars, got {len(env.content)}"})
    return vs


def _check_atom_peer_review(env: MsgEnvelope, debater_id: str,
                             atoms_by_id: dict) -> list[dict]:
    """Validate atom-peer-review: debater reviews OPPONENT's improvement."""
    vs: list[dict] = []
    if not env.target_atom_id:
        vs.append({"rule": "missing_field", "detail": "target_atom_id"})
        return vs
    target = atoms_by_id.get(env.target_atom_id)
    if not target:
        vs.append({"rule": "unknown_target_atom", "detail": env.target_atom_id})
        return vs
    # Must be OPPONENT's atom
    if target["author_debater_id"] == debater_id:
        vs.append({"rule": "peer_review_own_atom",
                   "detail": "cannot peer-review your own atom"})
    if env.verdict not in ("AGREE", "PARTIAL", "DISAGREE"):
        vs.append({"rule": "missing_field", "detail": "verdict"})
    if not env.content or len(env.content) < 80:
        vs.append({"rule": "peer_review_content_too_short",
                   "detail": "review content must be ≥80 chars"})
    return vs


def _check_atom_adversarial(env: MsgEnvelope, atoms_by_id: dict) -> list[dict]:
    vs: list[dict] = []
    if not env.target_atom_id:
        vs.append({"rule": "missing_field", "detail": "target_atom_id"})
        return vs
    target = atoms_by_id.get(env.target_atom_id)
    if not target:
        vs.append({"rule": "unknown_target_atom", "detail": env.target_atom_id})
        return vs
    got = len(env.failure_modes) if env.failure_modes else 0
    if got < ADVERSARIAL_MIN_FAILURE_MODES:
        vs.append({
            "rule": "adversarial_too_few_failures",
            "detail": (
                f"need ≥{ADVERSARIAL_MIN_FAILURE_MODES} failure_modes, got {got}. "
                "failure_modes MUST be a top-level JSON ARRAY field (NOT prose inside "
                "`content`). Each item is an object with these 4 keys: "
                "name (≥2 chars), source_anchor (≥4 chars AND an exact case-insensitive "
                "substring of the target atom's content), attack_scenario (≥80 chars), "
                "expected_loss (≥4 chars, concrete unit e.g. $/hours/atoms-leaked). "
                "Example: {\"kind\":\"atom-adversarial\",\"target_atom_id\":\"atom_<cat>_vN\","
                "\"content\":\"<short summary>\",\"failure_modes\":[{\"name\":\"...\","
                "\"source_anchor\":\"<verbatim snippet from target>\",\"attack_scenario\":"
                "\"<≥80 chars>\",\"expected_loss\":\"$X\"}, {...}, {...}],"
                "\"revision_required\":true}"
            ),
        })
        return vs
    target_text_lower = target["content"].lower()
    for i, fm in enumerate(env.failure_modes):
        if fm.source_anchor.lower() not in target_text_lower:
            vs.append({"rule": "adversarial_anchor_not_found",
                       "detail": (f"failure_modes[{i}].source_anchor "
                                  f"({fm.source_anchor[:60]!r}) is not a substring of "
                                  f"target atom {env.target_atom_id}. Copy a verbatim "
                                  "snippet from the target atom's content.")})
    if env.revision_required is False and not env.reason_if_not_required:
        vs.append({"rule": "missing_field",
                   "detail": "reason_if_not_required required when revision_required=false"})
    return vs


def _check_atom_approve(env: MsgEnvelope, atoms_by_id: dict) -> list[dict]:
    vs: list[dict] = []
    if not env.target_atom_id:
        vs.append({"rule": "missing_field", "detail": "target_atom_id"})
    elif env.target_atom_id not in atoms_by_id:
        vs.append({"rule": "unknown_target_atom", "detail": env.target_atom_id})
    return vs


def _check_cluster_propose(env: MsgEnvelope, part_cfg: PartConfig) -> list[dict]:
    vs: list[dict] = []
    if not env.cluster_id:
        vs.append({"rule": "missing_field", "detail": "cluster_id"})
    elif env.cluster_id not in part_cfg.canonical_clusters:
        vs.append({"rule": "unknown_cluster_id", "detail": env.cluster_id})
    if not env.locked_atom_ids:
        vs.append({"rule": "missing_field", "detail": "locked_atom_ids"})
    vs.extend(implementer_brief_violations(env.content, context="cluster-propose"))
    ok, _ = banned_phrases.prose_density_ok(env.content)
    if not ok:
        vs.append({"rule": "prose_density_too_high",
                   "detail": "cluster-propose body lacks ≥3 structural anchors"})
    return vs


def _check_final_propose(env: MsgEnvelope, part_cfg: PartConfig) -> list[dict]:
    vs: list[dict] = []
    vs.extend(final_propose_violations(env.content, part_cfg))
    ok, _ = banned_phrases.prose_density_ok(env.content)
    if not ok:
        vs.append({"rule": "prose_density_too_high",
                   "detail": "final-propose body lacks structural anchors"})
    return vs


# ---------- public: stateless validator ----------

@dataclass
class DoorCtx:
    """Snapshot of door state needed for stateless validation."""
    door_id: str
    current_phase: str
    forced_validation_mode: bool
    part_cfg: PartConfig
    atoms_by_id: dict  # atom_id -> {content, author_debater_id, locked, ...}
    pending_revision_atoms: dict  # atom_id -> adversarial_msg_id
    per_debater_stats: dict  # debater_id -> {messages, atoms_proposed, ...}


def validate_envelope(env: MsgEnvelope, debater_id: str, ctx: DoorCtx) -> list[dict]:
    vs: list[dict] = []

    # size cap — 6 KB for most, 40 KB for cluster/final-propose
    size = len(env.content.encode("utf-8"))
    big_kinds = ("cluster-propose", "final-propose", "final-revision", "cluster-revision")
    cap = 40_000 if env.kind in big_kinds else 6_000
    if size > cap:
        vs.append({"rule": "size_cap_exceeded", "detail": f"{size}B > {cap}B for {env.kind}"})

    # phase check
    allowed = KIND_TO_PHASES.get(env.kind, ())
    if ctx.current_phase not in allowed:
        vs.append({"rule": "wrong_phase",
                   "detail": f"kind={env.kind} not allowed in phase={ctx.current_phase}"})

    # banned phrases on all kinds
    vs.extend(banned_phrases.find_banned(env.content))

    # voice_manifest required on first note
    if env.kind == "note" and ctx.current_phase == "voice_manifest":
        stats = ctx.per_debater_stats.get(debater_id, {})
        if stats.get("messages", 0) == 0:
            vs.extend(voice_manifest_violations(env.content))

    # per-kind shape
    if env.kind == "atom-propose":
        vs.extend(_check_atom_propose(env, ctx.part_cfg))
    elif env.kind == "atom-critique":
        vs.extend(_check_atom_critique(env, ctx.atoms_by_id))
    elif env.kind == "atom-revision":
        vs.extend(_check_atom_revision(env, ctx.atoms_by_id, ctx.pending_revision_atoms))
    elif env.kind == "atom-peer-improve":
        vs.extend(_check_atom_peer_improve(env, debater_id, ctx.atoms_by_id))
    elif env.kind == "atom-peer-review":
        vs.extend(_check_atom_peer_review(env, debater_id, ctx.atoms_by_id))
    elif env.kind == "atom-adversarial":
        vs.extend(_check_atom_adversarial(env, ctx.atoms_by_id))
    elif env.kind == "atom-approve":
        vs.extend(_check_atom_approve(env, ctx.atoms_by_id))
    elif env.kind == "cluster-propose":
        vs.extend(_check_cluster_propose(env, ctx.part_cfg))
    elif env.kind == "final-propose":
        vs.extend(_check_final_propose(env, ctx.part_cfg))

    # disagree-rate floor (§5): if we're about to add a critique with
    # verdict ∈ {AGREE, PARTIAL} and current rate < 25% AND debater already
    # made ≥4 critiques, block.  (Lowered from 10 → 4 so the enforcement
    # kicks in before the debater exhausts all atoms with AGREE/PARTIAL.)
    if env.kind == "atom-critique" and env.verdict and env.verdict != "DISAGREE":
        stats = ctx.per_debater_stats.get(debater_id, {})
        total = stats.get("critiques_total", 0)
        disagree = stats.get("critiques_disagree", 0)
        if total >= 4 and (disagree / max(total, 1)) < 0.25:
            vs.append({"rule": "disagree_rate_floor_violated",
                       "detail": f"current rate {disagree}/{total} < 0.25; require DISAGREE"})

    # NRN cap 30% (§6): if revision with NRN, debater already has ≥10
    # revisions, and adding this NRN would push rate >30%, block.
    if env.kind == "atom-revision" and env.outcome == "NO_REVISION_NEEDED":
        stats = ctx.per_debater_stats.get(debater_id, {})
        tot = stats.get("revisions_total", 0)
        nrn = stats.get("revisions_nrn", 0)
        if tot >= 10 and ((nrn + 1) / (tot + 1)) > 0.30:
            vs.append({"rule": "nrn_rate_cap_exceeded",
                       "detail": f"NRN rate would be {nrn+1}/{tot+1} > 0.30"})

    return vs


# ---------- payload hash for /validate caching ----------

def payload_hash(env_json: dict, debater_id: str) -> str:
    blob = json.dumps({"e": env_json, "d": debater_id}, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def make_validation_id(env_json: dict, debater_id: str) -> str:
    h = payload_hash(env_json, debater_id)
    return f"val_{h[:32]}"
