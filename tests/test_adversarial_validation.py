"""Tests for atom-adversarial validation (Mason's "дебаты встали" bug).

Root cause that blocked both AI debaters in atoms_adversarial:
- the code required ≥4 failure_modes while every debater doc said ≥3
  (a compliant debater sending the documented 3-item example was rejected);
- failure_modes must be a STRUCTURED JSON array — prose inside `content`
  is never counted, and the old error message did not say so.

These tests lock the contract so the 3-vs-4 mismatch can't come back.
"""

from __future__ import annotations

from bot.addons.debates.engine.enforcement import (
    ADVERSARIAL_MIN_FAILURE_MODES,
    _check_atom_adversarial,
)
from bot.addons.debates.engine.models import FailureMode, MsgEnvelope


TARGET = {
    "content": "def transfer(a, b): balance = a - b  "
    "# global lock LOCK_KEY; sql DELETE FROM ledger WHERE id=?"
}
ATOMS = {"atom_core_v3": TARGET}


def _fm(anchor: str) -> FailureMode:
    return FailureMode(
        name="mode-x",
        source_anchor=anchor,
        attack_scenario="a" * 85,
        expected_loss="$100",
    )


def test_min_failure_modes_matches_docs():
    # Docs (PRAVILO_DEBAT_v3 §6) say "Минимум 3" — code must agree.
    assert ADVERSARIAL_MIN_FAILURE_MODES == 3


def test_three_structured_failure_modes_accepted():
    env = MsgEnvelope(
        kind="atom-adversarial",
        content="adversarial on core",
        target_atom_id="atom_core_v3",
        failure_modes=[_fm("balance"), _fm("LOCK_KEY"), _fm("ledger")],
        revision_required=True,
    )
    assert _check_atom_adversarial(env, ATOMS) == []


def test_prose_in_content_is_rejected_with_actionable_hint():
    env = MsgEnvelope(
        kind="atom-adversarial",
        content="failure 1.. failure 2.. failure 3.. failure 4..",
        target_atom_id="atom_core_v3",
    )
    vs = _check_atom_adversarial(env, ATOMS)
    assert vs and vs[0]["rule"] == "adversarial_too_few_failures"
    detail = vs[0]["detail"]
    assert "JSON ARRAY" in detail
    assert "failure_modes" in detail


def test_anchor_must_be_substring_of_target():
    env = MsgEnvelope(
        kind="atom-adversarial",
        content="adversarial on core",
        target_atom_id="atom_core_v3",
        failure_modes=[_fm("balance"), _fm("LOCK_KEY"), _fm("not_in_target_xx")],
    )
    vs = _check_atom_adversarial(env, ATOMS)
    assert any(v["rule"] == "adversarial_anchor_not_found" for v in vs)
