# «🧭 Гид» — token-gated step-by-step web wizard (sequence of buttons).
# Co-authored with CoCo
"""State for the «🧭 Гид» — a self-contained HTML wizard the owner opens in a
browser: numbered steps, a progress bar, and «Назад/Дальше» buttons that walk
through the bot's real setup flow (GitHub → Render → Fable5/VISION → Мозг →
проверка). Token-gated exactly like VISION. NOT a card checker — a plain
guided checklist.
"""

from __future__ import annotations

import logging
import secrets
import time

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "guide"


def is_enabled() -> bool:
    return bool(addon_state.get(ADDON, "enabled", False))


def get_token() -> str:
    return str(addon_state.get(ADDON, "token", "") or "")


def get_rotated_at() -> int:
    try:
        return int(addon_state.get(ADDON, "rotated_at", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _resolve_public_base() -> str:
    try:
        from ...config import resolve_public_url
        base = resolve_public_url()
    except Exception:  # noqa: BLE001
        base = ""
    if base:
        return base
    # Same bug as buttons 18-23 (vision/projectmap/test): a *relative* link
    # (``/guide/<token>/``) is rejected by Telegram (BUTTON_URL_INVALID)
    # when used in a url-button — see guide/handlers.py — which silently
    # kills the «🧭 Гид» screen. Always return an absolute URL.
    try:
        from ...config import PORT as _port
    except Exception:  # noqa: BLE001
        _port = 8080
    logger.warning(
        "guide: PUBLIC_URL is not configured — falling back to "
        "http://127.0.0.1:%s. The link will only work on this machine; "
        "set PUBLIC_URL for production.", _port,
    )
    return f"http://127.0.0.1:{_port}"


def get_link(base_url: str | None = None) -> str:
    token = get_token()
    if not token:
        return ""
    base = (base_url if base_url is not None else _resolve_public_base()).rstrip("/")
    return f"{base}/guide/{token}/" if base else f"/guide/{token}/"


def generate_link(base_url: str | None = None) -> tuple[str, str]:
    token = secrets.token_urlsafe(24)
    addon_state.set_(ADDON, "token", token)
    addon_state.set_(ADDON, "enabled", True)
    addon_state.set_(ADDON, "rotated_at", int(time.time()))
    return token, get_link(base_url)


def disable_link() -> bool:
    was = is_enabled()
    addon_state.set_(ADDON, "enabled", False)
    return was
