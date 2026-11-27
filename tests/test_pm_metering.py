"""Tests for pay-as-you-go metering («на сколько оплатил — на столько и пользуется»).

A guest's paid balance is debited proportionally to the LLM tokens they
consume: ``usd_per_slot / tokens_per_slot`` dollars per token. When it reaches
0 the existing payment gate flips them to «limit_reached». Owners/co-owners and
non-private bots are never charged.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import bot.private_mode as pm


# ---------------------------------------------------------------------------
# pure ledger maths
# ---------------------------------------------------------------------------
def test_tokens_per_slot_default_and_round_trip() -> None:
    s: dict = {}
    assert pm.get_tokens_per_slot(s) == 1_000_000
    pm.set_tokens_per_slot(s, 500_000)
    assert pm.get_tokens_per_slot(s) == 500_000
    pm.set_tokens_per_slot(s, 0)      # clamped to >= 1
    assert pm.get_tokens_per_slot(s) == 1


def test_usd_per_token_tracks_price_and_budget() -> None:
    s: dict = {}
    pm.set_usd_per_slot(s, 10)
    pm.set_tokens_per_slot(s, 1_000_000)
    assert pm.usd_per_token(s) == 10 / 1_000_000
    pm.set_tokens_per_slot(s, 500_000)
    assert pm.usd_per_token(s) == 10 / 500_000
    # a free slot never costs anything
    pm.set_usd_per_slot(s, 0)
    assert pm.usd_per_token(s) == 0.0


def test_meter_tokens_debits_proportionally() -> None:
    s: dict = {}
    pm.set_usd_per_slot(s, 10)
    pm.set_tokens_per_slot(s, 1_000_000)   # $1e-5 / token
    pm.add_balance(s, 7, 10.0)
    assert pm.meter_tokens(s, 7, 200_000) == 8.0     # $2 spent
    assert pm.meter_tokens(s, 7, 800_000) == 0.0     # drains exactly
    assert pm.has_paid_access(s, 7) is False


def test_meter_tokens_never_negative_or_throwing() -> None:
    s: dict = {}
    pm.set_usd_per_slot(s, 10)
    pm.add_balance(s, 7, 1.0)
    assert pm.meter_tokens(s, 7, 10_000_000) == 0.0  # clamps at 0
    # bad / zero / negative input is a harmless no-op
    assert pm.meter_tokens(s, 7, 0) == 0.0
    assert pm.meter_tokens(s, 7, "garbage") == 0.0   # type: ignore[arg-type]


def test_free_slot_never_debits() -> None:
    s: dict = {}
    pm.set_usd_per_slot(s, 0)            # free
    pm.add_balance(s, 7, 5.0)
    assert pm.meter_tokens(s, 7, 999_999) == 5.0


# ---------------------------------------------------------------------------
# storage wrapper guarding (owner exemption + enablement)
# ---------------------------------------------------------------------------
def _fresh_storage():
    from bot.storage import Storage

    st = Storage(data_dir=Path(tempfile.mkdtemp()))
    st.set_owner_id(1)
    return st


def test_wrapper_noop_when_private_off() -> None:
    st = _fresh_storage()
    st.pm_add_balance(999, 10.0)
    # private mode off → metering must not run
    assert st.pm_meter_guest_usage(999, 500_000) is None
    assert st.pm_get_balance(999) == 10.0


def test_wrapper_noop_without_paid_method() -> None:
    st = _fresh_storage()
    st.pm_toggle_enabled()  # on, but no payment method configured
    st.pm_add_balance(999, 10.0)
    assert st.pm_meter_guest_usage(999, 500_000) is None
    assert st.pm_get_balance(999) == 10.0


def test_wrapper_exempts_owner() -> None:
    st = _fresh_storage()
    st.pm_toggle_enabled()
    st.pm_toggle_method("stars")
    st.pm_add_balance(1, 10.0)          # owner somehow has a balance
    assert st.pm_meter_guest_usage(1, 500_000) is None
    assert st.pm_get_balance(1) == 10.0


def test_wrapper_debits_guest_and_persists() -> None:
    tmp = Path(tempfile.mkdtemp())
    from bot.storage import Storage

    st = Storage(data_dir=tmp)
    st.set_owner_id(1)
    st.pm_toggle_enabled()
    st.pm_toggle_method("stars")
    st.pm_set_usd_per_slot(10)
    st.pm_set_tokens_per_slot(1_000_000)
    st.pm_add_balance(999, 10.0)

    assert st.pm_meter_guest_usage(999, 300_000) == 7.0
    # survives a reload from disk
    again = Storage(data_dir=tmp)
    assert again.pm_get_balance(999) == 7.0
