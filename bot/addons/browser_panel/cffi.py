# Standalone Chrome-TLS HTTP fetch (curl_cffi) — decoupled from cards/issuance.
# Co-authored with CoCo
"""Reusable curl_cffi Chrome-impersonation fetch.

HTTP-level technology: sends requests with a REAL Chrome TLS/JA3 fingerprint
so anti-bot layers (Cloudflare) accept them as a genuine browser — used for
the owner's legitimate access to sites that false-block plain scripts. This
is NOT card checking; it's a general browsing/fetch helper. The original
copy in ``bot/addons/issuance`` is left untouched.

``cffi_fetch`` returns ``{ok, engine, status, text, error}``. Falls back to
aiohttp when curl_cffi isn't installed, so it never hard-crashes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from curl_cffi.requests import AsyncSession
    _CFFI_OK = True
except Exception:  # noqa: BLE001
    _CFFI_OK = False


def cffi_available() -> bool:
    return _CFFI_OK


async def cffi_fetch(url: str, method: str = "GET", timeout: float = 20.0,
                     headers: dict | None = None, data=None) -> dict:
    """Fetch ``url`` with a real-Chrome TLS fingerprint (passes Cloudflare).

    Returns {"ok", "engine", "status", "text", "error"}. HTTP only — no
    browser/clicks. Never raises: on failure returns ok=False.
    """
    method = (method or "GET").upper()
    if _CFFI_OK:
        try:
            async with AsyncSession(impersonate="chrome120") as s:
                r = await s.request(method, url, timeout=timeout,
                                    headers=headers or None, data=data)
                return {"ok": True, "engine": "curl_cffi (chrome120)",
                        "status": getattr(r, "status_code", 0),
                        "text": getattr(r, "text", "") or "", "error": ""}
        except Exception as e:  # noqa: BLE001
            logger.warning("cffi_fetch curl_cffi failed: %s", e)
    # Fallback: plain aiohttp (no TLS impersonation).
    try:
        import aiohttp
        to = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=to) as s:
            async with s.request(method, url, headers=headers or None, data=data) as r:
                body = await r.text()
                return {"ok": True, "engine": "aiohttp (fallback, без TLS-маскировки)",
                        "status": r.status, "text": body, "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "engine": "none", "status": 0, "text": "", "error": str(e)}
