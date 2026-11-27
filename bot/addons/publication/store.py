# Publication store: up to 10 social-account slots (api/browser), cookies+params persisted.
# Co-authored with CoCo
"""Storage for «📢 Публикация».

Up to 10 account slots. Each slot:
* ``network``     — youtube / tiktok / instagram / custom
* ``login_type``  — "api" | "browser"
* ``site``/``login``/``api_token``  — credentials (browser uses site+login+pwd)
* ``mode``        — "wide" | "shorts" | "auto"  (YouTube split)
* ``auto``        — bool: let Analyzer PRO decide how to publish
* ``hardware``    — issuance PROFILE_PRESETS key
* ``proxy``       — proxy/VPN string

Secrets (password, cookies) live in the encrypted Vault; non-secret meta in
state.json; cookies/params files on persistent /data for import/export.
"""

from __future__ import annotations

import json
import logging
import uuid

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "publication"
MAX_SLOTS = 10
NETWORKS = ("youtube", "tiktok", "instagram", "custom")


def _dir():
    from ...config import DATA_DIR
    d = DATA_DIR / "storage" / "publication"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_slots() -> list[dict]:
    raw = addon_state.get(ADDON, "slots", []) or []
    return [s for s in raw if isinstance(s, dict)]


def _save(slots: list[dict]) -> None:
    addon_state.set_(ADDON, "slots", slots)


def get_slot(sid: str) -> dict | None:
    for s in get_slots():
        if s.get("id") == sid:
            return s
    return None


def add_slot(name: str = "") -> dict | None:
    slots = get_slots()
    if len(slots) >= MAX_SLOTS:
        return None
    sid = uuid.uuid4().hex[:8]
    slot = {
        "id": sid,
        "name": name.strip() or f"Аккаунт {len(slots) + 1}",
        "network": "youtube",
        "login_type": "",        # api | browser
        "site": "",
        "login": "",
        "api_token": "",          # stored here for simplicity; sensitive
        "mode": "auto",           # wide | shorts | auto
        "auto": False,
        "hardware": "",           # issuance preset key
        "proxy": "",
        "logged_in": False,
    }
    slots.append(slot)
    _save(slots)
    return slot


def update_slot(sid: str, **patch) -> dict | None:
    slots = get_slots()
    for s in slots:
        if s.get("id") == sid:
            s.update(patch)
            _save(slots)
            return s
    return None


def remove_slot(sid: str) -> bool:
    slots = get_slots()
    kept = [s for s in slots if s.get("id") != sid]
    if len(kept) == len(slots):
        return False
    _save(kept)
    import contextlib
    with contextlib.suppress(Exception):
        (_dir() / f"{sid}_cookies.json").unlink()
    with contextlib.suppress(Exception):
        (_dir() / f"{sid}_params.json").unlink()
    return True


# ─── Cookies + params (per account, for import/export) ──────────────────────

def save_cookies(sid: str, cookies: list | dict) -> None:
    (_dir() / f"{sid}_cookies.json").write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    update_slot(sid, logged_in=True)


def load_cookies(sid: str) -> list | dict | None:
    p = _dir() / f"{sid}_cookies.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_params(sid: str, params: dict) -> None:
    """Store the publish params actually used (hardware, proxy, settings)."""
    (_dir() / f"{sid}_params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")


def load_params(sid: str) -> dict | None:
    p = _dir() / f"{sid}_params.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ─── Pending video (selected from «сделанное видео» to publish) ──────────────

def set_pending_video(path: str) -> None:
    addon_state.set_(ADDON, "pending_video", path or "")


def get_pending_video() -> str:
    return str(addon_state.get(ADDON, "pending_video", "") or "")


def clear_pending_video() -> None:
    addon_state.set_(ADDON, "pending_video", "")


# ─── OpenReels (topic → publish-ready MP4) config ────────────────────────

def get_openreels_url() -> str:
    return str(addon_state.get(ADDON, "openreels_url", "") or "")


def set_openreels_url(url: str) -> None:
    addon_state.set_(ADDON, "openreels_url", (url or "").strip().rstrip("/"))


def get_openreels_mode() -> str:
    """One of: 'generate_then_publish' | 'generate_only' | 'openreels_publishes'."""
    return str(addon_state.get(ADDON, "openreels_mode", "generate_then_publish")
               or "generate_then_publish")


def set_openreels_mode(mode: str) -> None:
    addon_state.set_(ADDON, "openreels_mode", mode or "generate_then_publish")


def get_openreels_platform() -> str:
    return str(addon_state.get(ADDON, "openreels_platform", "youtube") or "youtube")


def set_openreels_platform(platform: str) -> None:
    addon_state.set_(ADDON, "openreels_platform", platform or "youtube")
