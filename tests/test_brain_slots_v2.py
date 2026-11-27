"""Regression tests for the 20-slot brain pool (v2) logic.

The previous suite only checked the legacy single-slot chain. This file
locks down everything that was built for the 20-slot rebuild and was
previously untested:

* failover chain ordering + chain_pos cursor
* the top-of-menu active-model selector (how many buttons, which model wins)
* «во все 20 слотов» bulk apply (and that it never touches API keys)
* per-slot token accounting / remaining / budget exhaustion
* real provider-balance lookup + parsing + balance-based exhaustion
* mass-import (JSON object / JSON array / line format) into the pool
* the «🔌 API» batch parser routing into slots + slotsall
* flush_state_and_verify («дублировать → сравнить → переключиться»)

Pure-logic tests operate on a plain ``settings`` dict so they need no
disk; storage-level tests use a real ``Storage`` rooted at ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bot.brain_slots as bs
from bot import balance as bal
from bot.api_batch import parse_and_apply


# ---------------------------------------------------------------------------
# failover chain ordering
# ---------------------------------------------------------------------------
def test_configured_slot_ids_only_lists_slots_with_keys() -> None:
    s: dict = {}
    bs.set_slot_field(s, "1", "api_key", "sk-1")
    bs.set_slot_field(s, "5", "api_key", "sk-5")
    bs.set_slot_field(s, "3", "base_url", "https://x/v1")  # no key → not usable
    assert bs.configured_slot_ids(s) == ["1", "5"]


def test_chain_starts_at_current_chain_pos_and_wraps() -> None:
    s: dict = {}
    for sid in ("1", "2", "3", "4"):
        bs.set_slot_field(s, sid, "api_key", f"sk-{sid}")
    bs.set_chain_pos(s, "3")
    # currently-answering slot (3) stays at the head, then wraps around
    assert bs.configured_slot_ids(s) == ["3", "4", "1", "2"]


def test_chain_pos_not_in_usable_falls_back_to_natural_order() -> None:
    s: dict = {}
    bs.set_slot_field(s, "2", "api_key", "sk-2")
    bs.set_slot_field(s, "4", "api_key", "sk-4")
    bs.set_chain_pos(s, "9")  # 9 not configured
    assert bs.configured_slot_ids(s) == ["2", "4"]


# ---------------------------------------------------------------------------
# active-model selector
# ---------------------------------------------------------------------------
def test_available_model_indexes_reflects_filled_models() -> None:
    s: dict = {}
    # only model1 + model2 filled anywhere → exactly 2 buttons
    bs.set_bulk_field(s, "model1", "claude opus 4.8")
    bs.set_slot_field(s, "1", "model2", "gpt 5.5")
    assert bs.available_model_indexes(s) == ["1", "2"]
    # fill the 3rd → 3 buttons appear
    bs.set_bulk_field(s, "model3", "gemini 3.5 flash")
    assert bs.available_model_indexes(s) == ["1", "2", "3"]


def test_available_model_indexes_defaults_to_one_when_empty() -> None:
    assert bs.available_model_indexes({}) == ["1"]


def test_active_model_for_slot_honours_selector_with_fallback() -> None:
    s: dict = {}
    bs.set_slot_field(s, "1", "model1", "m1")
    bs.set_slot_field(s, "1", "model2", "m2")
    bs.set_active_model_index(s, "2")
    assert bs.active_model_for_slot(s, "1") == "m2"
    # selector points at model3 but slot has none → fall back to model1
    bs.set_active_model_index(s, "3")
    assert bs.active_model_for_slot(s, "1") == "m1"


def test_set_active_model_index_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        bs.set_active_model_index({}, "9")


# ---------------------------------------------------------------------------
# «во все 20 слотов» bulk apply
# ---------------------------------------------------------------------------
def test_apply_bulk_fills_url_and_models_but_keeps_keys() -> None:
    s: dict = {}
    bs.set_slot_field(s, "1", "api_key", "sk-keep-me")
    bs.set_bulk_field(s, "base_url", "https://api.x/v1")
    bs.set_bulk_field(s, "model1", "claude opus 4.8")
    bs.set_bulk_field(s, "model2", "gpt 5.5")
    n = bs.apply_bulk_to_all(s)
    assert n == 20
    for sid in bs.SLOT_IDS:
        slot = bs.get_slot(s, sid)
        assert slot["base_url"] == "https://api.x/v1"
        assert slot["model1"] == "claude opus 4.8"
        assert slot["model2"] == "gpt 5.5"
    # API key of slot 1 must be untouched
    assert bs.get_slot(s, "1")["api_key"] == "sk-keep-me"


# ---------------------------------------------------------------------------
# token accounting
# ---------------------------------------------------------------------------
def test_token_accounting_and_budget_exhaustion() -> None:
    s: dict = {}
    bs.set_slot_field(s, "1", "api_key", "sk-1")
    assert bs.remaining_tokens(s, "1") is None  # no budget → unknown
    bs.set_token_budget(s, "1", 100)
    bs.add_tokens(s, "1", 30)
    assert bs.get_tokens_used(s, "1") == 30
    assert bs.remaining_tokens(s, "1") == 70
    assert not bs.is_exhausted_by_budget(s, "1")
    bs.add_tokens(s, "1", 80)  # overshoot
    assert bs.remaining_tokens(s, "1") == 0
    assert bs.is_exhausted_by_budget(s, "1")
    bs.reset_tokens(s, "1")
    assert bs.get_tokens_used(s, "1") == 0


# ---------------------------------------------------------------------------
# real provider balance
# ---------------------------------------------------------------------------
def test_parse_balance_openrouter_auth_key_shape() -> None:
    payload = {"data": {"limit": 10.0, "usage": 2.5, "limit_remaining": 7.5}}
    out = bal.parse_balance_response(payload)
    assert out["remaining"] == 7.5


def test_parse_balance_credits_total_minus_usage() -> None:
    payload = {"data": {"total_credits": 10.0, "total_usage": 4.0}}
    out = bal.parse_balance_response(payload)
    assert out["remaining"] == 6.0


def test_parse_balance_generic_balance_key() -> None:
    payload = {"balance": 12.34, "currency": "USD"}
    out = bal.parse_balance_response(payload)
    assert out["remaining"] == 12.34
    assert out["currency"] == "USD"


def test_parse_balance_unknown_returns_none() -> None:
    assert bal.parse_balance_response({"hello": "world"})["remaining"] is None
    assert bal.parse_balance_response("not a dict")["remaining"] is None


def test_candidate_urls_prefers_explicit_then_standard() -> None:
    urls = bal.candidate_urls("https://api.x/v1", balance_url="https://iyh.app/me")
    assert urls[0] == "https://iyh.app/me"
    assert "https://api.x/v1/credits" in urls
    assert "https://api.x/auth/key" in urls  # root-relative variant


def test_fetch_balance_uses_injected_http_and_first_success() -> None:
    seen: list[str] = []

    def fake_get(url: str, api_key: str, timeout: float = 8.0):
        seen.append(url)
        if url.endswith("/auth/key"):
            return 404, "{}"  # first candidate misses
        if url.endswith("/credits"):
            return 200, json.dumps({"data": {"total_credits": 9, "total_usage": 1}})
        return 200, "{}"

    res = bal.fetch_balance("https://api.x/v1", "sk-1", http_get=fake_get)
    assert res["ok"] is True
    assert res["remaining"] == 8
    assert res["source_url"].endswith("/credits")
    assert any(u.endswith("/auth/key") for u in seen)


def test_fetch_balance_all_fail_reports_not_ok() -> None:
    res = bal.fetch_balance(
        "https://api.x/v1", "sk-1", http_get=lambda u, k, t=8.0: (401, "nope")
    )
    assert res["ok"] is False
    assert res["remaining"] is None
    assert res["errors"]


def test_balance_based_exhaustion() -> None:
    s: dict = {}
    bs.set_slot_field(s, "1", "api_key", "sk-1")
    assert not bs.is_exhausted_by_balance(s, "1")  # unknown → not exhausted
    bs.set_slot_balance(s, "1", 0.0, "USD")
    assert bs.is_exhausted_by_balance(s, "1")
    assert bs.is_exhausted(s, "1")
    bs.set_slot_balance(s, "1", 5.0, "USD")
    assert not bs.is_exhausted(s, "1")
    info = bs.get_slot_balance(s, "1")
    assert info["remaining"] == 5.0 and info["currency"] == "USD"


# ---------------------------------------------------------------------------
# mass-import into the pool
# ---------------------------------------------------------------------------
def test_import_slots_json_object_with_bulk_and_slots() -> None:
    s: dict = {}
    payload = json.dumps({
        "active_model": "2",
        "bulk": {"base_url": "https://api.x/v1", "model1": "a", "model2": "b"},
        "slots": {"1": {"api_key": "sk-1", "model1": "a"}, "2": {"api_key": "sk-2"}},
    })
    rep = bs.import_slots(s, payload)
    assert rep["errors"] == []
    assert bs.get_active_model_index(s) == "2"
    assert bs.get_slot(s, "1")["api_key"] == "sk-1"
    assert bs.get_slot(s, "2")["api_key"] == "sk-2"
    assert bs.get_bulk(s)["base_url"] == "https://api.x/v1"


def test_import_slots_json_array_positional() -> None:
    s: dict = {}
    payload = json.dumps([{"api_key": "sk-a", "model1": "m"}, {"api_key": "sk-b"}])
    rep = bs.import_slots(s, payload)
    assert rep["errors"] == []
    assert bs.get_slot(s, "1")["api_key"] == "sk-a"
    assert bs.get_slot(s, "2")["api_key"] == "sk-b"


def test_import_slots_line_format() -> None:
    s: dict = {}
    text = (
        "1 | key=sk-1; url=https://api.x/v1; model1=gpt 5.5; budget=100000\n"
        "2 | key=sk-2; url=https://api.y/v1"
    )
    rep = bs.import_slots(s, text)
    assert rep["errors"] == []
    assert bs.get_slot(s, "1")["api_key"] == "sk-1"
    assert bs.get_slot(s, "1")["token_budget"] == 100000
    assert bs.get_slot(s, "2")["base_url"] == "https://api.y/v1"


def test_import_slots_reports_out_of_range() -> None:
    s: dict = {}
    rep = bs.import_slots(s, "99 | key=sk-x")
    assert rep["applied"] == []
    assert any("99" in e for e in rep["errors"])


# ---------------------------------------------------------------------------
# «🔌 API» batch parser → slots
# ---------------------------------------------------------------------------
class _SlotRecordingStorage:
    """Test double recording brain-slot setter calls from the batch parser."""

    def __init__(self) -> None:
        self.slots: dict[str, dict] = {}
        self.bulk: dict[str, str] = {}
        self.applied_all = 0

    def set_brain_slot_field(self, sid: str, field: str, value) -> None:
        self.slots.setdefault(sid, {})[field] = value

    def set_brain_bulk_field(self, field: str, value: str) -> None:
        self.bulk[field] = value

    def apply_brain_bulk_to_all(self) -> int:
        self.applied_all += 1
        return 20


def test_batch_parser_routes_slot_targets() -> None:
    st = _SlotRecordingStorage()
    blob = "slot1:api:sk-aaa$$$slot2:api:sk-bbb;slot2:budget:100000;slot2:url:https://api.x/v1"
    report = parse_and_apply(blob, st)
    assert report.errors == []
    assert st.slots["1"]["api_key"] == "sk-aaa"
    assert st.slots["2"]["api_key"] == "sk-bbb"
    assert st.slots["2"]["token_budget"] == "100000"
    assert st.slots["2"]["base_url"] == "https://api.x/v1"


def test_batch_parser_slotsall_applies_bulk_once() -> None:
    st = _SlotRecordingStorage()
    blob = "slotsall:url:https://api.x/v1;slotsall:model1:claude;slotsall:model2:gpt"
    report = parse_and_apply(blob, st)
    assert report.errors == []
    assert st.bulk["base_url"] == "https://api.x/v1"
    assert st.bulk["model1"] == "claude"
    assert st.applied_all == 1  # applied exactly once, after parsing
    assert any("залито" in a for a in report.applied)


def test_batch_parser_slot_alias_and_balance_field() -> None:
    st = _SlotRecordingStorage()
    report = parse_and_apply("s3:balance:https://iyh.app/me", st)
    assert report.errors == []
    assert st.slots["3"]["balance_url"] == "https://iyh.app/me"


def test_batch_parser_slot_requires_key() -> None:
    st = _SlotRecordingStorage()
    report = parse_and_apply("slot1:sk-no-field", st)
    assert report.applied == []
    assert any("нужен ключ" in e for e in report.errors)


# ---------------------------------------------------------------------------
# storage-level: flush_state_and_verify
# ---------------------------------------------------------------------------
def test_flush_state_and_verify_roundtrips(tmp_path: Path) -> None:
    from bot.storage import Storage

    st = Storage(data_dir=tmp_path)
    st.set_brain_slot_field("1", "api_key", "sk-1")
    assert st.flush_state_and_verify() is True
    on_disk = json.loads(st.state_file.read_text())
    assert on_disk == st._state


def test_flush_state_and_verify_returns_false_on_persistent_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    from bot.storage import Storage

    st = Storage(data_dir=tmp_path)
    # Force every re-read to differ from in-memory state → all retries fail.
    monkeypatch.setattr(st, "_save", lambda: None)  # never actually writes
    st.state_file.write_text(json.dumps({"totally": "different"}))
    assert st.flush_state_and_verify(retries=3) is False
