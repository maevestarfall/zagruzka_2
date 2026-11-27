"""20-slot brain pool with sequential failover, 3 models per slot, a
bulk «во все 20 слотов» template, per-slot token accounting and a
top-of-menu active-model selector.

This module is *pure logic* over a plain ``settings`` dict (the same dict
``Storage._settings()`` returns). Keeping it side-effect free (no disk I/O)
makes it trivial to unit-test and lets ``Storage`` stay the single owner of
``_save()``.

Schema (stored under ``settings["brain_slots_v2"]``)::

    {
      "slots": {
        "1": {"api_key": "", "base_url": "",
               "model1": "", "model2": "", "model3": "",
               "curl": "", "tokens_used": 0, "token_budget": 0,
               "provider": "custom"},
        ... up to "20" ...
      },
      "bulk": {"base_url": "", "image_url": DEFAULT_IMAGE_URL, "model1": "", "model2": "", "model3": ""},
      "active_model": "1",      # which of the 3 models is live: "1"|"2"|"3"
      "chain_pos": "1"          # slot currently answering (failover cursor)
    }

Design decisions confirmed with the owner:
* Voice/photo keeps its own dedicated slot (legacy ``brain_slot2``) OUTSIDE
  this pool — this pool is the chat-failover chain only.
* «во все 20 слотов» fills URL + the 3 model names into every slot and into
  the bulk template, but NEVER touches per-slot API keys (failover only
  makes sense when each slot has a different key).
* «Остаток токенов» = tokens counted from each response's ``usage`` summed
  per slot, with an optional per-slot ``token_budget`` to count down from.
"""

from __future__ import annotations

MAX_SLOTS = 20
SLOT_IDS = tuple(str(i) for i in range(1, MAX_SLOTS + 1))
MODEL_FIELDS = ("model1", "model2", "model3")
_ROOT_KEY = "brain_slots_v2"

# Default secondary site, lower priority than ``base_url`` — used as an image
# / photo prompt source (Nano Banana Pro). Pre-filled in every slot so the
# owner has a sensible default without typing it.
DEFAULT_IMAGE_URL = "https://youmind.com/nano-banana-pro-prompts"

_SLOT_DEFAULT = {
    "api_key": "",
    "base_url": "",
    "image_url": DEFAULT_IMAGE_URL,
    "model1": "",
    "model2": "",
    "model3": "",
    "curl": "",
    "tokens_used": 0,
    "token_budget": 0,
    "provider": "custom",
    # Real provider balance («остаток с провайдера», not the manual budget).
    # Filled by a live lookup (see :mod:`bot.balance`); left empty until then.
    "balance_url": "",          # optional explicit endpoint (e.g. iyh.app)
    "balance_remaining": None,  # last fetched remaining amount (float) or None
    "balance_currency": "",     # "USD" / "credits" / ...
    "balance_checked_ts": 0,    # unix ts of last successful lookup
}

_SETTABLE_FIELDS = {
    "api_key", "base_url", "image_url", "model1", "model2", "model3",
    "curl", "tokens_used", "token_budget", "provider",
    "balance_url", "balance_remaining", "balance_currency", "balance_checked_ts",
}


# --------------------------------------------------------------------------
# root / slot accessors
# --------------------------------------------------------------------------
def _root(settings: dict) -> dict:
    root = settings.setdefault(_ROOT_KEY, {})
    root.setdefault("slots", {})
    root.setdefault("bulk", {"base_url": "", "image_url": DEFAULT_IMAGE_URL, "model1": "", "model2": "", "model3": ""})
    root.setdefault("active_model", "1")
    root.setdefault("chain_pos", "1")
    return root


def get_slot(settings: dict, sid: str) -> dict:
    """Return slot ``sid`` (1..20) merged over the defaults. Read-only view —
    use :func:`set_slot_field` to mutate so the dict is created in state."""
    sid = str(sid)
    raw = _root(settings)["slots"].get(sid, {})
    merged = dict(_SLOT_DEFAULT)
    merged.update(raw)
    return merged


def _slot_mut(settings: dict, sid: str) -> dict:
    sid = str(sid)
    if sid not in SLOT_IDS:
        raise ValueError(f"slot id must be 1..{MAX_SLOTS}, got {sid!r}")
    slots = _root(settings)["slots"]
    slot = slots.setdefault(sid, dict(_SLOT_DEFAULT))
    # backfill any missing keys for slots created by older versions
    for k, v in _SLOT_DEFAULT.items():
        slot.setdefault(k, v)
    return slot


def set_slot_field(settings: dict, sid: str, field: str, value) -> None:
    if field not in _SETTABLE_FIELDS:
        raise ValueError(f"unknown slot field: {field!r}")
    slot = _slot_mut(settings, sid)
    if field in ("base_url", "balance_url"):
        value = (value or "").rstrip("/")
    if field in ("tokens_used", "token_budget", "balance_checked_ts"):
        value = max(0, int(value or 0))
    if field == "balance_remaining":
        value = None if value in (None, "") else float(value)
    slot[field] = value


def clear_slot(settings: dict, sid: str) -> None:
    _root(settings)["slots"][str(sid)] = dict(_SLOT_DEFAULT)


def list_slots(settings: dict) -> dict[str, dict]:
    """All 20 slots, each merged over defaults, keyed by id in order."""
    return {sid: get_slot(settings, sid) for sid in SLOT_IDS}


def is_configured(settings: dict, sid: str) -> bool:
    """A slot is usable for chat when it has an API key."""
    return bool(get_slot(settings, sid).get("api_key"))


def configured_slot_ids(settings: dict) -> list[str]:
    """Ordered list of slots that have an API key — the failover chain.

    Order starts at the current ``chain_pos`` so a slot that already failed
    over stays the head until it too is exhausted, then wraps around to try
    the rest. This keeps the *currently working* slot first.
    """
    usable = [sid for sid in SLOT_IDS if is_configured(settings, sid)]
    if not usable:
        return []
    pos = _root(settings).get("chain_pos", "1")
    if pos in usable:
        i = usable.index(pos)
        usable = usable[i:] + usable[:i]
    return usable


def set_chain_pos(settings: dict, sid: str) -> None:
    _root(settings)["chain_pos"] = str(sid)


def get_chain_pos(settings: dict) -> str:
    return str(_root(settings).get("chain_pos", "1"))


# --------------------------------------------------------------------------
# active model selector (top of menu): which of the 3 models is live
# --------------------------------------------------------------------------
def get_active_model_index(settings: dict) -> str:
    raw = str(_root(settings).get("active_model", "1"))
    return raw if raw in ("1", "2", "3") else "1"


def set_active_model_index(settings: dict, idx: str) -> None:
    idx = str(idx)
    if idx not in ("1", "2", "3"):
        raise ValueError("active model index must be 1, 2 or 3")
    _root(settings)["active_model"] = idx


def available_model_indexes(settings: dict) -> list[str]:
    """Which model buttons to show at the top of the menu.

    A model index N is offered when *any* configured slot OR the bulk
    template has a non-empty ``modelN``. So if the owner filled only 2
    models, the top menu shows just 2 buttons.
    """
    bulk = _root(settings)["bulk"]
    slots = list_slots(settings)
    out: list[str] = []
    for n, field in enumerate(MODEL_FIELDS, start=1):
        if bulk.get(field):
            out.append(str(n))
            continue
        if any(s.get(field) for s in slots.values()):
            out.append(str(n))
    return out or ["1"]


def active_model_for_slot(settings: dict, sid: str) -> str:
    """Resolve the live model name for a slot, honouring the global
    active-model index with sensible fallbacks:
    slot.model{idx} → slot.model1 → bulk.model{idx} → bulk.model1 → ""."""
    idx = get_active_model_index(settings)
    slot = get_slot(settings, sid)
    bulk = _root(settings)["bulk"]
    field = f"model{idx}"
    return (
        slot.get(field)
        or slot.get("model1")
        or bulk.get(field)
        or bulk.get("model1")
        or ""
    )


# --------------------------------------------------------------------------
# bulk «во все 20 слотов»
# --------------------------------------------------------------------------
def get_bulk(settings: dict) -> dict:
    return dict(_root(settings)["bulk"])


def set_bulk_field(settings: dict, field: str, value: str) -> None:
    if field not in ("base_url", "image_url", "model1", "model2", "model3"):
        raise ValueError(f"unknown bulk field: {field!r}")
    if field == "base_url":
        value = (value or "").rstrip("/")
    _root(settings)["bulk"][field] = value


def apply_bulk_to_all(settings: dict) -> int:
    """Push the bulk template's URL + 3 models into every slot.

    API keys are left untouched (per design). Returns the count of slots
    written (always 20). Empty template fields are still written so the
    owner can use the bulk screen to *clear* models across slots too.
    """
    bulk = _root(settings)["bulk"]
    for sid in SLOT_IDS:
        slot = _slot_mut(settings, sid)
        slot["base_url"] = (bulk.get("base_url") or "").rstrip("/")
        if bulk.get("image_url"):
            slot["image_url"] = bulk.get("image_url")
        for field in MODEL_FIELDS:
            slot[field] = bulk.get(field, "")
        slot["provider"] = "custom"
    return len(SLOT_IDS)


# --------------------------------------------------------------------------
# token accounting / «остаток»
# --------------------------------------------------------------------------
def add_tokens(settings: dict, sid: str, n: int) -> None:
    slot = _slot_mut(settings, sid)
    slot["tokens_used"] = max(0, int(slot.get("tokens_used", 0))) + max(0, int(n or 0))


def get_tokens_used(settings: dict, sid: str) -> int:
    return int(get_slot(settings, sid).get("tokens_used", 0))


def set_token_budget(settings: dict, sid: str, budget: int) -> None:
    set_slot_field(settings, sid, "token_budget", budget)


def reset_tokens(settings: dict, sid: str) -> None:
    set_slot_field(settings, sid, "tokens_used", 0)


def remaining_tokens(settings: dict, sid: str):
    """Remaining tokens for a slot, or ``None`` when no budget is set."""
    slot = get_slot(settings, sid)
    budget = int(slot.get("token_budget", 0))
    if budget <= 0:
        return None
    return max(0, budget - int(slot.get("tokens_used", 0)))


def is_exhausted_by_budget(settings: dict, sid: str) -> bool:
    rem = remaining_tokens(settings, sid)
    return rem is not None and rem <= 0


# --------------------------------------------------------------------------
# real provider balance («остаток с провайдера» / iyh.app)
# --------------------------------------------------------------------------
def set_slot_balance(
    settings: dict,
    sid: str,
    remaining,
    currency: str = "USD",
    ts: int | None = None,
) -> None:
    """Persist a freshly-fetched provider balance onto a slot.

    ``remaining`` is the amount left (float; may be USD credits or tokens
    depending on the provider). Pass ``None`` to mark "unknown".
    """
    import time as _time

    slot = _slot_mut(settings, sid)
    slot["balance_remaining"] = None if remaining is None else float(remaining)
    slot["balance_currency"] = currency or "USD"
    slot["balance_checked_ts"] = int(ts if ts is not None else _time.time())


def get_slot_balance(settings: dict, sid: str) -> dict:
    """Return the last fetched balance info for a slot.

    ``{"remaining": float|None, "currency": str, "checked_ts": int}``.
    """
    slot = get_slot(settings, sid)
    rem = slot.get("balance_remaining")
    return {
        "remaining": None if rem is None else float(rem),
        "currency": slot.get("balance_currency") or "USD",
        "checked_ts": int(slot.get("balance_checked_ts", 0) or 0),
    }


def is_exhausted_by_balance(settings: dict, sid: str) -> bool:
    """True only when a balance was actually fetched and came back <= 0.

    Never returns True for an unknown (un-fetched) balance — we'd rather keep
    a slot in the chain than drop it on a guess.
    """
    rem = get_slot_balance(settings, sid)["remaining"]
    return rem is not None and rem <= 0


def is_exhausted(settings: dict, sid: str) -> bool:
    """Combined exhaustion: manual token budget OR real provider balance."""
    return is_exhausted_by_budget(settings, sid) or is_exhausted_by_balance(settings, sid)


# --------------------------------------------------------------------------
# mass import — fill all 20 slots (+ bulk/active model) in one paste
# --------------------------------------------------------------------------
_IMPORT_KEY_ALIASES = {
    "key": "api_key", "api": "api_key", "api_key": "api_key", "token": "api_key",
    "url": "base_url", "base_url": "base_url", "endpoint": "base_url",
    "image_url": "image_url", "image": "image_url", "photo": "image_url",
    "photo_url": "image_url", "site2": "image_url", "url2": "image_url",
    "model": "model1", "model1": "model1", "m1": "model1",
    "model2": "model2", "m2": "model2", "model3": "model3", "m3": "model3",
    "budget": "token_budget", "token_budget": "token_budget",
    "curl": "curl",
    "balance_url": "balance_url", "balanceurl": "balance_url",
    "balance": "balance_url", "balance_endpoint": "balance_url",
}


def _coerce_slot_dict(raw: dict) -> dict:
    """Normalise a user-supplied slot dict to canonical field names."""
    out: dict = {}
    for k, v in raw.items():
        kk = _IMPORT_KEY_ALIASES.get(str(k).strip().lower())
        if kk:
            out[kk] = v
    return out


def import_slots(settings: dict, text: str) -> dict:
    """Mass-import the whole pool from one pasted payload.

    Accepts (auto-detected):

    1. **JSON object**::

        {
          "active_model": "1",
          "bulk":  {"base_url": "...", "model1": "...", "model2": "...", "model3": "..."},
          "slots": {"1": {"api_key": "...", "base_url": "...", "model1": "..."}, "2": {...}}
        }

    2. **JSON array** of up to 20 slot objects (position = slot id, 1-based).

    3. **Line format**, one slot per line::

        1 | key=sk-...; url=https://api.x/v1; model1=gpt-5.5; budget=100000
        2 | key=sk-...; url=https://api.y/v1

    Returns ``{"applied": [...], "errors": [...]}``. Never raises on bad
    rows — each problem becomes a friendly error string so the caller can
    show one summary banner.
    """
    import json

    applied: list[str] = []
    errors: list[str] = []
    text = (text or "").strip()
    if not text:
        return {"applied": [], "errors": ["Пустой ввод."]}

    def _apply_slot(sid: str, fields: dict) -> None:
        sid = str(sid).strip().lstrip("pP")
        if sid not in SLOT_IDS:
            errors.append(f"слот {sid!r}: вне диапазона 1..{MAX_SLOTS}")
            return
        canon = _coerce_slot_dict(fields)
        if not canon:
            errors.append(f"слот {sid}: не распознал ни одного поля")
            return
        for f, v in canon.items():
            try:
                set_slot_field(settings, sid, f, v)
            except Exception as e:  # noqa: BLE001
                errors.append(f"слот {sid}.{f}: {e}")
                continue
        set_slot_field(settings, sid, "provider", "custom")
        applied.append(f"слот {sid} ({', '.join(canon)})")

    # --- try JSON first ---
    parsed = None
    if text[0] in "[{":
        try:
            parsed = json.loads(text)
        except Exception as e:  # noqa: BLE001
            errors.append(f"JSON не разобран: {e}")
            return {"applied": applied, "errors": errors}

    if isinstance(parsed, dict) and ("slots" in parsed or "bulk" in parsed or "active_model" in parsed):
        bulk = parsed.get("bulk") or {}
        for f in ("base_url", "image_url", "model1", "model2", "model3"):
            if f in bulk:
                set_bulk_field(settings, f, bulk[f])
                applied.append(f"bulk.{f}")
        if "active_model" in parsed:
            try:
                set_active_model_index(settings, str(parsed["active_model"]))
                applied.append("active_model")
            except Exception as e:  # noqa: BLE001
                errors.append(f"active_model: {e}")
        for sid, fields in (parsed.get("slots") or {}).items():
            if isinstance(fields, dict):
                _apply_slot(sid, fields)
            else:
                errors.append(f"слот {sid}: ожидался объект")
        return {"applied": applied, "errors": errors}

    if isinstance(parsed, dict):
        # bare {"1": {...}, "2": {...}}
        for sid, fields in parsed.items():
            if isinstance(fields, dict):
                _apply_slot(sid, fields)
            else:
                errors.append(f"слот {sid}: ожидался объект")
        return {"applied": applied, "errors": errors}

    if isinstance(parsed, list):
        for i, fields in enumerate(parsed[:MAX_SLOTS], start=1):
            if isinstance(fields, dict):
                _apply_slot(str(i), fields)
            else:
                errors.append(f"позиция {i}: ожидался объект")
        return {"applied": applied, "errors": errors}

    # --- line format: "<sid> | key=val; key=val"  (also accepts ':' / tab) ---
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "|" in ln:
            head, body = ln.split("|", 1)
        elif ":" in ln and ln.split(":", 1)[0].strip().lstrip("pP").isdigit():
            head, body = ln.split(":", 1)
        else:
            errors.append(f"строка {ln[:30]!r}: нет разделителя слота (исп. «N | поля»)")
            continue
        fields: dict = {}
        for part in body.replace(",", ";").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()
        _apply_slot(head, fields)

    if not applied and not errors:
        errors.append("Ничего не распознал. Используй JSON или «N | key=val; ...».")
    return {"applied": applied, "errors": errors}
