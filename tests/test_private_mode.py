"""Tests for the «Приват режим» fine-grained access layer.

Covers the pure :mod:`bot.private_mode` logic (feature/visibility toggles,
payment config, notifications, per-guest paid balance), the storage wrappers,
and the :func:`bot.access.guest_can_use` / ``guest_can_see`` gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import bot.private_mode as pm


# ---------------------------------------------------------------------------
# master switch + defaults
# ---------------------------------------------------------------------------
def test_defaults_are_all_off() -> None:
    s: dict = {}
    assert pm.is_enabled(s) is False
    # every feature OFF for both use + see by default
    for key in pm.FEATURE_KEYS:
        assert pm.can_guest_use(s, key) is False
        assert pm.can_guest_see(s, key) is False
    # every payment method OFF
    assert pm.enabled_methods(s) == []
    assert pm.get_require_on_join(s) is False
    assert pm.get_usd_per_slot(s) == 10.0


def test_toggle_enabled_round_trips() -> None:
    s: dict = {}
    assert pm.toggle_enabled(s) is True
    assert pm.is_enabled(s) is True
    assert pm.toggle_enabled(s) is False


# ---------------------------------------------------------------------------
# feature / visibility flags
# ---------------------------------------------------------------------------
def test_feature_and_visibility_flags_are_independent() -> None:
    s: dict = {}
    assert pm.toggle_flag(s, "features", "coding") is True
    assert pm.can_guest_use(s, "coding") is True
    # turning USE on must NOT turn SEE on
    assert pm.can_guest_see(s, "coding") is False
    pm.set_flag(s, "visibility", "coding", True)
    assert pm.can_guest_see(s, "coding") is True


def test_flags_snapshot_covers_all_features() -> None:
    s: dict = {}
    snap = pm.flags_snapshot(s, "features")
    assert set(snap) == set(pm.FEATURE_KEYS)
    assert all(v is False for v in snap.values())


def test_unknown_feature_key_raises() -> None:
    with pytest.raises(ValueError):
        pm.toggle_flag({}, "features", "nope")
    with pytest.raises(ValueError):
        pm.set_flag({}, "visibility", "nope", True)


# ---------------------------------------------------------------------------
# payment
# ---------------------------------------------------------------------------
def test_payment_method_toggles_and_enabled_list() -> None:
    s: dict = {}
    assert pm.toggle_method(s, "stars") is True
    assert pm.toggle_method(s, "card") is True
    assert pm.enabled_methods(s) == ["card", "stars"]  # canonical order
    pm.toggle_method(s, "stars")
    assert pm.enabled_methods(s) == ["card"]


def test_card_and_crypto_details_round_trip() -> None:
    s: dict = {}
    pm.set_method_details(s, "card", "MIR 2200 0000 0000 0000")
    pm.set_method_details(s, "crypto", "USDT TRC-20 TXXXX")
    assert pm.get_method_details(s, "card").startswith("MIR")
    assert "TRC-20" in pm.get_method_details(s, "crypto")


def test_stars_has_no_details() -> None:
    with pytest.raises(ValueError):
        pm.set_method_details({}, "stars", "x")


def test_require_on_join_and_price() -> None:
    s: dict = {}
    assert pm.toggle_require_on_join(s) is True
    assert pm.get_require_on_join(s) is True
    pm.set_usd_per_slot(s, 25)
    assert pm.get_usd_per_slot(s) == 25.0
    pm.set_usd_per_slot(s, -5)  # clamped to 0
    assert pm.get_usd_per_slot(s) == 0.0


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------
def test_notifications_round_trip() -> None:
    s: dict = {}
    pm.set_notification(s, "welcome", "Привет! Оплати доступ.")
    pm.set_notification(s, "limit_reached", "Лимит кончился.")
    assert pm.get_notification(s, "welcome").startswith("Привет")
    assert "кончился" in pm.get_notification(s, "limit_reached")
    with pytest.raises(ValueError):
        pm.get_notification(s, "bogus")


# ---------------------------------------------------------------------------
# per-guest paid balance
# ---------------------------------------------------------------------------
def test_balance_credit_deduct_and_access() -> None:
    s: dict = {}
    assert pm.has_paid_access(s, 111) is False
    assert pm.add_balance(s, 111, 10.0) == 10.0
    assert pm.has_paid_access(s, 111) is True
    assert pm.deduct_balance(s, 111, 4.0) == 6.0
    assert pm.deduct_balance(s, 111, 999) == 0.0  # clamps, never negative
    assert pm.has_paid_access(s, 111) is False


# ---------------------------------------------------------------------------
# storage wrappers + access gates
# ---------------------------------------------------------------------------
def test_storage_wrappers_persist(tmp_path: Path) -> None:
    from bot.storage import Storage

    st = Storage(data_dir=tmp_path)
    st.set_owner_id(1)
    assert st.pm_is_enabled() is False
    st.pm_toggle_enabled()
    st.pm_toggle_flag("features", "coding")
    st.pm_toggle_method("stars")
    st.pm_add_balance(555, 10.0)

    # reload from disk → values survived
    again = Storage(data_dir=tmp_path)
    assert again.pm_is_enabled() is True
    assert again.pm_can_guest_use("coding") is True
    assert again.pm_enabled_methods() == ["stars"]
    assert again.pm_get_balance(555) == 10.0


def test_guest_gates_respect_owner_flags_and_balance(tmp_path: Path, monkeypatch) -> None:
    import bot.access as access
    from bot.storage import Storage

    st = Storage(data_dir=tmp_path)
    st.set_owner_id(1)
    monkeypatch.setattr("bot.storage.storage", st)

    OWNER, GUEST = 1, 999

    # private mode OFF → guest gate defers to broad can_use (private → False)
    assert access.guest_can_use(GUEST, "coding") is False
    assert access.guest_can_use(OWNER, "coding") is True  # owner always

    # turn on the fine-grained layer
    st.pm_toggle_enabled()
    assert access.guest_can_use(GUEST, "coding") is False  # flag still off
    assert access.guest_can_see(GUEST, "coding") is False

    st.pm_toggle_flag("features", "coding")
    st.pm_toggle_flag("visibility", "coding")
    # no payment configured → USE flag alone is enough
    assert access.guest_can_use(GUEST, "coding") is True
    assert access.guest_can_see(GUEST, "coding") is True

    # enable a paid method → now the guest also needs a positive balance
    st.pm_toggle_method("stars")
    assert access.guest_can_use(GUEST, "coding") is False
    st.pm_add_balance(GUEST, 10.0)
    assert access.guest_can_use(GUEST, "coding") is True
    # visibility is independent of balance
    assert access.guest_can_see(GUEST, "coding") is True


def test_addon_router_builds() -> None:
    from bot.addons.private_mode import build_private_mode_router

    router = build_private_mode_router()
    assert router is not None


def test_guest_offer_shows_only_enabled_methods(tmp_path: Path, monkeypatch) -> None:
    from bot.addons.private_mode.handlers import guest_offer
    from bot.storage import Storage

    st = Storage(data_dir=tmp_path)
    monkeypatch.setattr("bot.addons.private_mode.handlers.storage", st)

    # nothing enabled → no keyboard
    text, kb = guest_offer("welcome")
    assert kb is None and isinstance(text, str)

    st.pm_toggle_method("stars")  # only stars on
    st.pm_set_notification("welcome", "Оплати доступ")
    text, kb = guest_offer("welcome")
    assert kb is not None
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("звёзд" in label.lower() for label in labels)
    assert not any("картой" in label.lower() for label in labels)  # card disabled
    assert "Оплати доступ" in text

    st.pm_toggle_method("card")  # now card too
    _, kb = guest_offer("limit_reached")
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("картой" in label.lower() for label in labels)
    assert any("звёзд" in label.lower() for label in labels)


# ---------------------------------------------------------------------------
# payment gate decision (drives the working middleware)
# ---------------------------------------------------------------------------
def test_gate_allows_when_disabled_or_no_payment() -> None:
    s: dict = {}
    # private mode off → allow
    assert pm.guest_gate_decision(s, 7) == "allow"
    pm.set_enabled(s, True)
    # on but no payment method configured → allow (paid access not set up)
    assert pm.guest_gate_decision(s, 7) == "allow"


def test_gate_welcome_then_limit_then_allow() -> None:
    s: dict = {}
    pm.set_enabled(s, True)
    pm.toggle_method(s, "stars")  # paid access configured
    # brand-new guest → welcome
    assert pm.guest_gate_decision(s, 7) == "welcome"
    # credit, then they have access → allow
    pm.add_balance(s, 7, 10.0)
    assert pm.guest_gate_decision(s, 7) == "allow"
    # drain → was credited before, so limit (not welcome)
    pm.deduct_balance(s, 7, 10.0)
    assert pm.guest_gate_decision(s, 7) == "limit"


def test_charge_record_and_refund_tracking() -> None:
    s: dict = {}
    assert pm.get_last_charge(s, 7) is None
    pm.record_charge(s, 7, "chg_A")
    pm.record_charge(s, 7, "chg_B")
    assert pm.get_charges(s, 7) == ["chg_A", "chg_B"]
    assert pm.get_last_charge(s, 7) == "chg_B"
    pm.clear_charge(s, 7, "chg_B")
    assert pm.get_last_charge(s, 7) == "chg_A"


def test_payment_gate_middleware_blocks_guest_and_passes_owner() -> None:
    import asyncio

    from bot.addons.private_mode.handlers import build_payment_gate_middleware

    class _FakeStorage:
        def __init__(self):
            self.owner = 1
            self.decision = "welcome"
        def is_owner_or_co_owner(self, uid):
            return uid == self.owner
        def pm_guest_gate_decision(self, uid):
            return self.decision
        # guest_offer() reads these:
        def pm_get_notification(self, kind):
            return "Оплати"
        def pm_enabled_methods(self):
            return ["stars"]
        def pm_get_usd_per_slot(self):
            return 10.0

    import bot.addons.private_mode.handlers as h
    fake = _FakeStorage()
    orig = h.storage
    h.storage = fake
    try:
        mw = build_payment_gate_middleware()

        calls = {"n": 0}
        async def handler(event, data):
            calls["n"] += 1
            return "passed"

        class _User:
            def __init__(self, uid): self.id = uid
        class _Msg:
            def __init__(self, uid):
                self.from_user = _User(uid)
                self.successful_payment = None
                self.sent = []
            async def answer(self, *a, **k):
                self.sent.append((a, k))
        class _Update:
            def __init__(self, msg):
                self.message = msg
                self.callback_query = None
                self.pre_checkout_query = None

        # guest → blocked, handler NOT called, offer sent
        guest_msg = _Msg(999)
        res = asyncio.run(mw(handler, _Update(guest_msg), {}))
        assert res is None
        assert calls["n"] == 0
        assert guest_msg.sent  # offer was shown

        # owner → passes through
        owner_msg = _Msg(1)
        res = asyncio.run(mw(handler, _Update(owner_msg), {}))
        assert res == "passed"
        assert calls["n"] == 1
    finally:
        h.storage = orig


# ---------------------------------------------------------------------------
# card provider token + currency (native Telegram Payments: ЮKassa / Stripe)
# ---------------------------------------------------------------------------
def test_card_token_and_currency_round_trip() -> None:
    s: dict = {}
    # defaults: no token, RUB
    assert pm.get_card_token(s) == ""
    assert pm.get_card_currency(s) == "RUB"
    pm.set_card_token(s, "  381764678:LIVE:abc  ")
    assert pm.get_card_token(s) == "381764678:LIVE:abc"  # trimmed
    pm.set_card_currency(s, "usd")
    assert pm.get_card_currency(s) == "USD"  # upper-cased
    # clearing the token returns to manual mode
    pm.set_card_token(s, "")
    assert pm.get_card_token(s) == ""


def test_card_currency_falls_back_to_rub_on_empty() -> None:
    s: dict = {}
    pm.set_card_currency(s, "")
    assert pm.get_card_currency(s) == "RUB"


# ---------------------------------------------------------------------------
# CryptoBot (Crypto Pay) token + paid-invoice ledger (anti-double-credit)
# ---------------------------------------------------------------------------
def test_crypto_token_round_trip() -> None:
    s: dict = {}
    assert pm.get_crypto_token(s) == ""
    pm.set_crypto_token(s, " 12345:AAbbCC ")
    assert pm.get_crypto_token(s) == "12345:AAbbCC"


def test_crypto_invoice_credit_ledger_blocks_double() -> None:
    s: dict = {}
    assert pm.crypto_was_credited(s, "INV1") is False
    pm.crypto_mark_credited(s, "INV1", 777)
    assert pm.crypto_was_credited(s, "INV1") is True
    # a different invoice is independent
    assert pm.crypto_was_credited(s, "INV2") is False


# ---------------------------------------------------------------------------
# CryptoBot webhook config + URL helper
# ---------------------------------------------------------------------------
def test_crypto_webhook_enabled_round_trip() -> None:
    s: dict = {}
    assert pm.get_crypto_webhook_enabled(s) is False
    pm.set_crypto_webhook_enabled(s, True)
    assert pm.get_crypto_webhook_enabled(s) is True


def test_crypto_public_url_normalised() -> None:
    s: dict = {}
    assert pm.get_crypto_public_url(s) == ""
    pm.set_crypto_public_url(s, "https://mybot.onrender.com/")
    assert pm.get_crypto_public_url(s) == "https://mybot.onrender.com"  # trailing slash stripped


def test_webhook_url_helper() -> None:
    from bot.addons.private_mode.crypto_webhook import CRYPTO_WEBHOOK_PATH, webhook_url
    assert webhook_url("https://x.com/") == "https://x.com" + CRYPTO_WEBHOOK_PATH
    # empty base just returns the path (UI shows a hint instead)
    assert webhook_url("") == CRYPTO_WEBHOOK_PATH


def test_storage_payment_token_wrappers_persist() -> None:
    from bot.storage import storage
    storage.pm_set_card_token("tok-card")
    storage.pm_set_card_currency("usd")
    storage.pm_set_crypto_token("tok-crypto")
    storage.pm_set_crypto_webhook_enabled(True)
    storage.pm_set_crypto_public_url("https://h.io/")
    try:
        assert storage.pm_get_card_token() == "tok-card"
        assert storage.pm_get_card_currency() == "USD"
        assert storage.pm_get_crypto_token() == "tok-crypto"
        assert storage.pm_get_crypto_webhook_enabled() is True
        assert storage.pm_get_crypto_public_url() == "https://h.io"
    finally:
        # reset so we don't leak into other tests sharing the storage singleton
        storage.pm_set_card_token("")
        storage.pm_set_card_currency("RUB")
        storage.pm_set_crypto_token("")
        storage.pm_set_crypto_webhook_enabled(False)
        storage.pm_set_crypto_public_url("")
