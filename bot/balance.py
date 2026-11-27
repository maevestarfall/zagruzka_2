"""Real remaining-balance lookup for brain slots («остаток с провайдера»).

The 20-slot pool already tracks a *manual* token budget (the owner types a
number, the bot counts down from it). This module adds the missing piece the
owner asked for: actually **asking the provider** how much is left, instead of
only trusting a hand-typed number.

Two halves, split so the network part stays out of the unit tests:

* :func:`parse_balance_response` — *pure* function that turns a provider JSON
  payload into ``{"remaining": float|None, "currency": str, ...}``. Knows the
  common shapes (OpenRouter ``/auth/key`` + ``/credits``, OpenAI
  ``credit_grants``, and generic ``balance`` / ``credits`` keys used by
  aggregators such as iyh.app).
* :func:`fetch_balance` — does the HTTP GET against the provider and feeds the
  body to the parser. The HTTP call is injectable (``http_get=``) so tests can
  exercise the whole path without touching the network. Uses only the stdlib
  (``urllib``) — no new dependency.

Endpoint discovery: most OpenAI-compatible providers expose the remaining
credit under one of a handful of well-known paths relative to the slot's
``base_url``. We try the owner-supplied ``balance_url`` first (paste the exact
URL from your provider — including an iyh.app-style dashboard endpoint), then
fall back to the standard candidates.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# Candidate balance endpoints, tried in order, joined onto the slot base_url.
# ``base_url`` is typically ``https://host/v1``; some live at the host root, so
# we try both the v1-relative and the root-relative forms.
_CANDIDATE_PATHS: tuple[str, ...] = (
    "/auth/key",                       # OpenRouter: {"data":{"limit_remaining":...}}
    "/credits",                        # OpenRouter: {"data":{"total_credits","total_usage"}}
    "/dashboard/billing/credit_grants",  # OpenAI billing
    "/user/balance",                   # DeepSeek / iyh.app-style aggregators
    "/balance",                        # generic
)

# Keys that, anywhere in the payload, directly mean "this is what's left".
_REMAINING_KEYS: tuple[str, ...] = (
    "limit_remaining", "balance_remaining", "remaining", "total_available",
    "available", "balance", "credit", "credits", "remaining_credits",
)
# (total, used) pairs to subtract when there's no direct "remaining" field.
_TOTAL_USED_PAIRS: tuple[tuple[str, str], ...] = (
    ("total_credits", "total_usage"),
    ("limit", "usage"),
    ("total_granted", "total_used"),
    ("hard_limit_usd", "total_usage"),
)


def _coerce_float(v) -> float | None:
    try:
        if isinstance(v, bool):  # bool is an int subclass — reject it
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_balance_response(payload) -> dict:
    """Turn a provider JSON payload into a normalised balance dict.

    Returns ``{"remaining": float|None, "currency": str, "raw": payload}``.
    ``remaining`` is ``None`` when nothing recognisable was found — the caller
    should treat that as "unknown", never as "zero".
    """
    result = {"remaining": None, "currency": "USD", "raw": payload}
    if not isinstance(payload, dict):
        return result

    # Unwrap the common ``{"data": {...}}`` envelope (OpenRouter et al.).
    scopes: list[dict] = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        scopes.insert(0, data)

    if isinstance(payload.get("currency"), str):
        result["currency"] = payload["currency"]

    # 1) direct "remaining"-style key
    for scope in scopes:
        for key in _REMAINING_KEYS:
            if key in scope:
                val = _coerce_float(scope[key])
                if val is not None:
                    result["remaining"] = val
                    if key in ("credit", "credits", "remaining_credits"):
                        result["currency"] = scope.get("currency", "credits")
                    return result

    # 2) total - used
    for scope in scopes:
        for total_key, used_key in _TOTAL_USED_PAIRS:
            if total_key in scope:
                total = _coerce_float(scope[total_key])
                used = _coerce_float(scope.get(used_key, 0)) or 0.0
                if total is not None:
                    result["remaining"] = total - used
                    return result

    return result


def candidate_urls(base_url: str, balance_url: str | None = None) -> list[str]:
    """Ordered, de-duplicated list of balance URLs to try for a slot."""
    urls: list[str] = []

    def _add(u: str) -> None:
        u = (u or "").strip()
        if u and u not in urls:
            urls.append(u)

    if balance_url:
        _add(balance_url)
    base = (base_url or "").rstrip("/")
    if base:
        # root = base with a trailing ``/v1`` (or ``/v1/...``) stripped
        root = base
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        for path in _CANDIDATE_PATHS:
            _add(base + path)
            if root != base:
                _add(root + path)
    return urls


def _default_http_get(url: str, api_key: str, timeout: float = 8.0) -> tuple[int, str]:
    """Minimal stdlib GET. Returns ``(status_code, body_text)``.

    Raises on transport errors; HTTP error *statuses* (401/404/…) are returned
    as ``(status, body)`` so the caller can move on to the next candidate.
    """
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return getattr(resp, "status", 200) or 200, body
    except urllib.error.HTTPError as exc:  # 4xx/5xx — readable body, try next
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, body


def fetch_balance(
    base_url: str,
    api_key: str,
    *,
    balance_url: str | None = None,
    http_get=None,
    timeout: float = 8.0,
) -> dict:
    """Query the provider for the slot's remaining balance.

    Tries each candidate URL until one returns JSON with a recognisable
    balance. ``http_get(url, api_key, timeout) -> (status, body)`` is
    injectable for tests; defaults to a stdlib ``urllib`` GET.

    Returns ``{"ok": bool, "remaining": float|None, "currency": str,
    "source_url": str|None, "errors": [str, ...]}``. ``ok`` is ``True`` only
    when a balance number was successfully parsed.
    """
    getter = http_get or _default_http_get
    errors: list[str] = []
    for url in candidate_urls(base_url, balance_url):
        try:
            status, body = getter(url, api_key, timeout)
        except Exception as exc:  # noqa: BLE001 — network/transport
            errors.append(f"{url}: {exc}")
            continue
        if status and status >= 400:
            errors.append(f"{url}: HTTP {status}")
            continue
        try:
            payload = json.loads(body)
        except Exception:  # noqa: BLE001 — not JSON, try next
            errors.append(f"{url}: ответ не JSON")
            continue
        parsed = parse_balance_response(payload)
        if parsed["remaining"] is not None:
            return {
                "ok": True,
                "remaining": parsed["remaining"],
                "currency": parsed["currency"],
                "source_url": url,
                "errors": errors,
            }
        errors.append(f"{url}: остаток не найден в ответе")
    return {
        "ok": False,
        "remaining": None,
        "currency": "USD",
        "source_url": None,
        "errors": errors or ["нет URL для запроса остатка (заполни URL слота)"],
    }
