"""Private-mode access control: per-feature permissions, payment config and
guest notifications («приват режим» in the ⛔ Доступ menu).

The existing access model has three coarse modes (private / public /
full_public). This module adds a *fine-grained* layer on top of "private":
the owner sees and uses everything, and for everyone else the owner picks
exactly which features they may **use** and which they may **see**, plus how
they pay to unlock usage.

Pure logic over a plain ``settings`` dict (the dict ``Storage._settings()``
returns) so it unit-tests without disk. Stored under
``settings["private_mode"]``::

    {
      "enabled": False,            # master switch for the fine-grained layer
      "features":   {key: bool},   # what guests may USE      (default all OFF)
      "visibility": {key: bool},   # what guests may SEE       (default all OFF)
      "payment": {
        "card":   {"enabled": False, "details": ""},   # РФ / Mastercard
        "stars":  {"enabled": False},                  # Telegram Stars
        "crypto": {"enabled": False, "details": ""},   # crypto address/memo
        "require_on_join": False,  # demand payment as soon as a guest joins
        "usd_per_slot": 10.0       # $10 == 1 API-slot worth of usage
      },
      "notifications": {
        "welcome": "",        # shown on join (with pay buttons)
        "limit_reached": ""   # shown when the guest's paid limit runs out
      },
      "balances": {"<uid>": float}  # remaining paid USD per guest
    }

Defaults are deliberately the safest possible: the fine-grained layer is OFF,
and every feature/visibility flag is OFF, so a freshly-built bot never leaks a
capability to a guest until the owner explicitly turns it on (green ✅).
"""

from __future__ import annotations

_ROOT_KEY = "private_mode"

# Canonical feature catalogue. ``key`` is the stable storage id, ``label`` is
# the Russian menu caption. The same catalogue drives both «Функции» (use) and
# «Что они видят» (see). Add new features here and both screens pick them up.
FEATURES: tuple[tuple[str, str], ...] = (
    ("coding",      "💻 Кодинг"),
    ("analysis",    "📊 Анализ"),
    ("github",      "🐙 Гитхаб скачивание"),
    ("terminal",    "🖥 Терминал"),
    ("media",       "📷 Фото / видео"),
    ("helpzavr",    "🦖 Helpzavr"),
    ("pretty_text", "✨ Красивый текст"),
    ("editor",      "🎬 Монтажёр"),
    ("cleanup",     "🧹 Скинуть весь мусор"),
    ("debates",     "🗣 Дебаты"),
    ("osint",       "🔎 OSINT"),
    ("mailbox",     "📬 Почта"),
    ("memory",      "🧠 Память"),
    ("tts",         "🔊 Голос (TTS)"),
    ("promptlab",   "📚 Справочник промтов"),
)

FEATURE_KEYS: tuple[str, ...] = tuple(k for k, _ in FEATURES)
FEATURE_LABELS: dict[str, str] = {k: v for k, v in FEATURES}

PAYMENT_METHODS: tuple[tuple[str, str], ...] = (
    ("card",   "💳 Карта (РФ / Mastercard)"),
    ("stars",  "⭐ Звёздами Telegram"),
    ("crypto", "🪙 Криптой"),
)
PAYMENT_METHOD_KEYS: tuple[str, ...] = tuple(k for k, _ in PAYMENT_METHODS)


# --------------------------------------------------------------------------
# root / defaults
# --------------------------------------------------------------------------
def _root(settings: dict) -> dict:
    root = settings.setdefault(_ROOT_KEY, {})
    root.setdefault("enabled", False)
    root.setdefault("features", {})
    root.setdefault("visibility", {})
    pay = root.setdefault("payment", {})
    for m in PAYMENT_METHOD_KEYS:
        slot = pay.setdefault(m, {})
        slot.setdefault("enabled", False)
        if m in ("card", "crypto"):
            slot.setdefault("details", "")
    # Native Telegram Payments provider token for card (from @BotFather →
    # Payments → YooKassa/Stripe). When set, the card button sends a real
    # in-Telegram Visa/Mastercard invoice instead of just showing requisites.
    pay["card"].setdefault("token", "")
    pay["card"].setdefault("currency", "RUB")
    pay.setdefault("require_on_join", False)
    pay.setdefault("usd_per_slot", 10.0)
    # Pay-as-you-go metering: how many LLM tokens one paid «slot»
    # (== usd_per_slot dollars) is worth. The guest's balance is debited
    # proportionally as they consume tokens; when it hits 0 the payment gate
    # shows «limit_reached». Default: $10 ≈ 1M tokens.
    pay.setdefault("tokens_per_slot", 1_000_000)
    notif = root.setdefault("notifications", {})
    notif.setdefault("welcome", "")
    notif.setdefault("limit_reached", "")
    root.setdefault("balances", {})
    return root


# --------------------------------------------------------------------------
# master switch
# --------------------------------------------------------------------------
def is_enabled(settings: dict) -> bool:
    return bool(_root(settings)["enabled"])


def set_enabled(settings: dict, on: bool) -> None:
    _root(settings)["enabled"] = bool(on)


def toggle_enabled(settings: dict) -> bool:
    root = _root(settings)
    root["enabled"] = not root["enabled"]
    return root["enabled"]


# --------------------------------------------------------------------------
# feature (USE) + visibility (SEE) flags
# --------------------------------------------------------------------------
def _flagmap(settings: dict, kind: str) -> dict:
    if kind not in ("features", "visibility"):
        raise ValueError(f"unknown flag map: {kind!r}")
    return _root(settings)[kind]


def get_flag(settings: dict, kind: str, key: str) -> bool:
    if key not in FEATURE_LABELS:
        raise ValueError(f"unknown feature key: {key!r}")
    return bool(_flagmap(settings, kind).get(key, False))


def set_flag(settings: dict, kind: str, key: str, on: bool) -> None:
    if key not in FEATURE_LABELS:
        raise ValueError(f"unknown feature key: {key!r}")
    _flagmap(settings, kind)[key] = bool(on)


def toggle_flag(settings: dict, kind: str, key: str) -> bool:
    cur = get_flag(settings, kind, key)
    set_flag(settings, kind, key, not cur)
    return not cur


def can_guest_use(settings: dict, key: str) -> bool:
    """Whether a guest may USE feature ``key`` (only meaningful when enabled)."""
    return get_flag(settings, "features", key)


def can_guest_see(settings: dict, key: str) -> bool:
    """Whether a guest may SEE feature ``key`` (only meaningful when enabled)."""
    return get_flag(settings, "visibility", key)


def flags_snapshot(settings: dict, kind: str) -> dict[str, bool]:
    """All feature keys mapped to their current on/off state for ``kind``."""
    return {k: get_flag(settings, kind, k) for k in FEATURE_KEYS}


# --------------------------------------------------------------------------
# payment config
# --------------------------------------------------------------------------
def get_payment(settings: dict) -> dict:
    """Deep-ish copy of the payment config for read-only display."""
    pay = _root(settings)["payment"]
    return {
        **{m: dict(pay[m]) for m in PAYMENT_METHOD_KEYS},
        "require_on_join": bool(pay["require_on_join"]),
        "usd_per_slot": float(pay["usd_per_slot"]),
    }


def is_method_enabled(settings: dict, method: str) -> bool:
    if method not in PAYMENT_METHOD_KEYS:
        raise ValueError(f"unknown payment method: {method!r}")
    return bool(_root(settings)["payment"][method]["enabled"])


def toggle_method(settings: dict, method: str) -> bool:
    if method not in PAYMENT_METHOD_KEYS:
        raise ValueError(f"unknown payment method: {method!r}")
    slot = _root(settings)["payment"][method]
    slot["enabled"] = not slot["enabled"]
    return slot["enabled"]


def set_method_details(settings: dict, method: str, details: str) -> None:
    if method not in ("card", "crypto"):
        raise ValueError("only card/crypto carry free-text details")
    _root(settings)["payment"][method]["details"] = details or ""


def get_method_details(settings: dict, method: str) -> str:
    if method not in ("card", "crypto"):
        raise ValueError("only card/crypto carry free-text details")
    return _root(settings)["payment"][method].get("details", "")


def enabled_methods(settings: dict) -> list[str]:
    """Methods the owner switched ON — these are the only ones offered to guests."""
    return [m for m in PAYMENT_METHOD_KEYS if is_method_enabled(settings, m)]


# --- native Telegram Payments token + currency for the card method ---------
def get_card_token(settings: dict) -> str:
    """Provider token (BotFather → Payments). Empty == manual-requisites mode."""
    return _root(settings)["payment"]["card"].get("token", "")


def set_card_token(settings: dict, token: str) -> None:
    _root(settings)["payment"]["card"]["token"] = (token or "").strip()


def get_card_currency(settings: dict) -> str:
    return _root(settings)["payment"]["card"].get("currency", "RUB") or "RUB"


def set_card_currency(settings: dict, currency: str) -> None:
    _root(settings)["payment"]["card"]["currency"] = (currency or "RUB").strip().upper()


# --- CryptoBot (Crypto Pay API) token + paid-invoice bookkeeping -----------
def get_crypto_token(settings: dict) -> str:
    """Crypto Pay API token (@CryptoBot → Crypto Pay → My Apps → Create App).

    Empty == manual mode (just show the wallet address the owner typed in).
    """
    return _root(settings)["payment"]["crypto"].get("token", "")


def set_crypto_token(settings: dict, token: str) -> None:
    _root(settings)["payment"]["crypto"]["token"] = (token or "").strip()


def crypto_was_credited(settings: dict, invoice_id) -> bool:
    """True once a Crypto Pay invoice has already credited a guest (anti-double)."""
    return str(invoice_id) in _root(settings).get("crypto_invoices", {})


def crypto_mark_credited(settings: dict, invoice_id, user_id) -> None:
    inv = _root(settings).setdefault("crypto_invoices", {})
    inv[str(invoice_id)] = str(user_id)


# --- CryptoBot webhook (instant auto-credit instead of "check" button) -----
def get_crypto_webhook_enabled(settings: dict) -> bool:
    return bool(_root(settings)["payment"]["crypto"].get("webhook_enabled", False))


def set_crypto_webhook_enabled(settings: dict, on: bool) -> None:
    _root(settings)["payment"]["crypto"]["webhook_enabled"] = bool(on)


def get_crypto_public_url(settings: dict) -> str:
    """Operator override for the bot's public HTTPS base (auto-detect fallback)."""
    return _root(settings)["payment"]["crypto"].get("public_url", "")


def set_crypto_public_url(settings: dict, url: str) -> None:
    _root(settings)["payment"]["crypto"]["public_url"] = (url or "").strip().rstrip("/")


def toggle_require_on_join(settings: dict) -> bool:
    pay = _root(settings)["payment"]
    pay["require_on_join"] = not pay["require_on_join"]
    return pay["require_on_join"]


def get_require_on_join(settings: dict) -> bool:
    return bool(_root(settings)["payment"]["require_on_join"])


def set_usd_per_slot(settings: dict, usd: float) -> None:
    _root(settings)["payment"]["usd_per_slot"] = max(0.0, float(usd or 0))


def get_usd_per_slot(settings: dict) -> float:
    return float(_root(settings)["payment"]["usd_per_slot"])


def set_tokens_per_slot(settings: dict, tokens: int) -> None:
    """How many LLM tokens one paid slot ($usd_per_slot) buys. Min 1."""
    _root(settings)["payment"]["tokens_per_slot"] = max(1, int(tokens or 0))


def get_tokens_per_slot(settings: dict) -> int:
    return int(_root(settings)["payment"].get("tokens_per_slot", 1_000_000)) or 1_000_000


def usd_per_token(settings: dict) -> float:
    """USD cost of a single token under the current price/budget.

    ``usd_per_slot / tokens_per_slot``. Returns 0.0 when the slot is free
    (``usd_per_slot == 0``) so a free bot never debits anyone.
    """
    per_slot = get_usd_per_slot(settings)
    if per_slot <= 0:
        return 0.0
    return per_slot / float(get_tokens_per_slot(settings))


def meter_tokens(settings: dict, user_id, tokens: int) -> float:
    """Debit a guest's balance for ``tokens`` of usage. Returns the new balance.

    Pure ledger maths — callers decide *whether* a user should be metered
    (owners/co-owners are exempt; see ``storage.pm_meter_guest_usage``).
    Never throws on bad input and never goes negative.
    """
    try:
        n = max(0, int(tokens or 0))
    except (TypeError, ValueError):
        return get_balance(settings, user_id)
    if n == 0:
        return get_balance(settings, user_id)
    cost = usd_per_token(settings) * n
    if cost <= 0:
        return get_balance(settings, user_id)
    return deduct_balance(settings, user_id, cost)


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------
def get_notification(settings: dict, kind: str) -> str:
    if kind not in ("welcome", "limit_reached"):
        raise ValueError(f"unknown notification: {kind!r}")
    return _root(settings)["notifications"].get(kind, "")


def set_notification(settings: dict, kind: str, text: str) -> None:
    if kind not in ("welcome", "limit_reached"):
        raise ValueError(f"unknown notification: {kind!r}")
    _root(settings)["notifications"][kind] = text or ""


# --------------------------------------------------------------------------
# per-guest paid balance (pay-as-you-go: $usd_per_slot == 1 API slot)
# --------------------------------------------------------------------------
def get_balance(settings: dict, user_id) -> float:
    return float(_root(settings)["balances"].get(str(user_id), 0.0))


def add_balance(settings: dict, user_id, usd: float) -> float:
    """Credit ``usd`` to a guest (e.g. after a successful payment). Returns new balance."""
    bal = _root(settings)["balances"]
    new = round(get_balance(settings, user_id) + float(usd or 0), 4)
    bal[str(user_id)] = new
    return new


def deduct_balance(settings: dict, user_id, usd: float) -> float:
    bal = _root(settings)["balances"]
    new = max(0.0, round(get_balance(settings, user_id) - float(usd or 0), 4))
    bal[str(user_id)] = new
    return new


def has_paid_access(settings: dict, user_id) -> bool:
    """A guest has access while their paid balance is positive."""
    return get_balance(settings, user_id) > 0


def was_credited(settings: dict, user_id) -> bool:
    """True once a guest has *ever* been credited (key exists in balances).

    Lets the payment gate tell a brand-new guest (show «welcome») apart from
    one whose paid limit ran out (show «limit_reached»).
    """
    return str(user_id) in _root(settings)["balances"]


# --------------------------------------------------------------------------
# Telegram Stars transaction ids (for refunds — matches the official
# aiogram example + bohd4nx/stars-payment reference)
# --------------------------------------------------------------------------
def record_charge(settings: dict, user_id, charge_id: str) -> None:
    """Remember a successful Stars charge id so the owner can refund it later."""
    charges = _root(settings).setdefault("charges", {})
    lst = charges.setdefault(str(user_id), [])
    if charge_id and charge_id not in lst:
        lst.append(charge_id)


def charge_exists(settings: dict, user_id, charge_id: str) -> bool:
    """True if this charge_id was already recorded (anti-double-credit)."""
    if not charge_id:
        return False
    return charge_id in _root(settings).get("charges", {}).get(str(user_id), [])


def get_charges(settings: dict, user_id) -> list[str]:
    return list(_root(settings).get("charges", {}).get(str(user_id), []))


def get_last_charge(settings: dict, user_id) -> str | None:
    lst = get_charges(settings, user_id)
    return lst[-1] if lst else None


def clear_charge(settings: dict, user_id, charge_id: str) -> None:
    """Drop a charge id after it has been refunded."""
    lst = _root(settings).get("charges", {}).get(str(user_id), [])
    if charge_id in lst:
        lst.remove(charge_id)


# --------------------------------------------------------------------------
# payment gate decision (drives the outer middleware that actually enforces
# paid access for guests — owner/co-owners are handled by the caller)
# --------------------------------------------------------------------------
def guest_gate_decision(settings: dict, user_id) -> str:
    """Decide what a *guest* (non-owner) may do right now.

    Returns one of:
      * ``"allow"``   — let the update through.
      * ``"welcome"`` — brand-new guest, show the welcome offer + pay buttons.
      * ``"limit"``   — paid before but ran out, show the limit-reached offer.

    Only meaningful when private mode is ON and at least one payment method is
    enabled (paid access configured). Otherwise always ``"allow"`` — the broad
    access mode / per-feature flags handle non-paid setups.
    """
    if not is_enabled(settings):
        return "allow"
    if not enabled_methods(settings):
        return "allow"
    if has_paid_access(settings, user_id):
        return "allow"
    return "limit" if was_credited(settings, user_id) else "welcome"
