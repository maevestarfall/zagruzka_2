"""SwapFace addon state (ТЗ SwapFace v2.0 §4).

Persist branch: ``addons.swapface`` inside lilush ``state.json``
(via :mod:`bot.addons.state`, chmod 600). The Discord **user token is
NEVER stored here** — only the env-var *name* (``user_token_ref``).

Face registry rules (§4):

* ``idname`` — ``^[a-zA-Z0-9]{1,10}$`` (InsightFaceSwap hard limit);
* no duplicates; soft cap of 20 faces (warning, not a hard error);
* composites ``a+b`` (blend) / ``a,b`` (multi-face) are allowed as
  *arguments* for ``/setid``/``/swapid`` but never stored in ``faces``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .. import state as addon_state

ADDON = "swapface"

IDNAME_RE = re.compile(r"^[a-zA-Z0-9]{1,10}$")
#: composite = simple idnames joined with ``+`` (blend) or ``,`` (multi)
COMPOSITE_RE = re.compile(r"^[a-zA-Z0-9]{1,10}([+,][a-zA-Z0-9]{1,10})*$")

MAX_FACES_SOFT = 20
DEFAULT_DAILY_LIMIT = 50
SWAP_COST_CREDITS = 3
DEFAULT_SWAP_TIMEOUT = 120
DEDUP_TTL_SECONDS = 600  # §10 идемпотентность: окно 10 минут

DEFAULT_USER_TOKEN_REF = "SWAPFACE_DISCORD_USER_TOKEN"


# ── generic get/set ────────────────────────────────────────────────────────

def _get(key: str, default: Any = None) -> Any:
    return addon_state.get(ADDON, key, default)


def _set(key: str, value: Any) -> None:
    addon_state.set_(ADDON, key, value)


# ── toggles / mode ─────────────────────────────────────────────────────────

def enabled_photo() -> bool:
    return bool(_get("enabled_photo", False))


def set_enabled_photo(value: bool) -> None:
    _set("enabled_photo", bool(value))


def enabled_video() -> bool:
    return bool(_get("enabled_video", False))


def set_enabled_video(value: bool) -> None:
    _set("enabled_video", bool(value))


def get_mode() -> str:
    mode = str(_get("mode", "manual") or "manual")
    return mode if mode in ("manual", "usertoken") else "manual"


def set_mode(mode: str) -> None:
    if mode not in ("manual", "usertoken"):
        raise ValueError(f"unknown swapface mode: {mode!r}")
    _set("mode", mode)


def usertoken_accepted_at() -> int | None:
    v = _get("usertoken_accepted_at", None)
    return int(v) if v else None


def set_usertoken_accepted() -> None:
    _set("usertoken_accepted_at", int(time.time()))


def consent_accepted_at() -> int | None:
    """§7 — deepfake/consent legal screen acknowledgement."""
    v = _get("consent_accepted_at", None)
    return int(v) if v else None


def set_consent_accepted() -> None:
    _set("consent_accepted_at", int(time.time()))


# ── discord coordinates ────────────────────────────────────────────────────

def get_guild_id() -> str | None:
    v = _get("guild_id", None)
    return str(v) if v else None


def set_guild_id(value: str | None) -> None:
    _set("guild_id", str(value).strip() if value else None)


def get_channel_id() -> str | None:
    v = _get("channel_id", None)
    return str(v) if v else None


def set_channel_id(value: str | None) -> None:
    _set("channel_id", str(value).strip() if value else None)


def user_token_ref() -> str:
    return str(_get("user_token_ref", DEFAULT_USER_TOKEN_REF) or DEFAULT_USER_TOKEN_REF)


def get_user_token() -> str | None:
    """Read the user token from env by its configured name — never persisted."""
    token = os.environ.get(user_token_ref(), "").strip()
    return token or None


# ── InsightFaceSwap command index (§5.2) ───────────────────────────────────

def get_insight_app_id() -> str | None:
    v = _get("insight_app_id", None)
    return str(v) if v else None


def get_commands() -> dict:
    v = _get("commands", None)
    return dict(v) if isinstance(v, dict) else {}


def set_command_index(app_id: str, commands: dict) -> None:
    _set("insight_app_id", str(app_id))
    _set("commands", commands)
    _set("commands_scanned_at", int(time.time()))


def commands_scanned_at() -> int | None:
    v = _get("commands_scanned_at", None)
    return int(v) if v else None


# ── idname validation ──────────────────────────────────────────────────────

def validate_idname(idname: str) -> str | None:
    """Return an error message (ru) or None if the idname is valid.

    Runs BEFORE any network calls (§4, §10).
    """
    idname = (idname or "").strip()
    if not idname:
        return "Пустой idname."
    if len(idname) > 10:
        return "idname длиннее 10 символов — InsightFaceSwap такое отклоняет."
    if not IDNAME_RE.match(idname):
        return "idname: только латинские буквы и цифры, без пробелов и спецсимволов."
    return None


def validate_composite(value: str) -> str | None:
    """Validate ``a+b`` / ``a,b`` composites for /setid & /swapid args."""
    value = (value or "").strip()
    if not value:
        return "Пустое значение."
    if not COMPOSITE_RE.match(value):
        return ("Композит должен состоять из idname (буквы/цифры, ≤10), "
                "разделённых «+» (бленд) или «,» (мульти-лицо).")
    return None


# ── face registry ──────────────────────────────────────────────────────────

def get_faces() -> list[dict]:
    v = _get("faces", None)
    return list(v) if isinstance(v, list) else []


def _save_faces(faces: list[dict]) -> None:
    _set("faces", faces)


def get_face(idname: str) -> dict | None:
    for f in get_faces():
        if f.get("idname") == idname:
            return f
    return None


def add_face(idname: str, file: str) -> tuple[bool, str | None]:
    """Add a face entry. Returns (ok, warn_or_error).

    On soft-cap overflow (>20) the face is still added but a warning
    string is returned alongside ``ok=True``.
    """
    err = validate_idname(idname)
    if err:
        return False, err
    if get_face(idname) is not None:
        return False, f"Лицо «{idname}» уже есть в реестре."
    faces = get_faces()
    warn = None
    if len(faces) >= MAX_FACES_SOFT:
        warn = (f"У InsightFaceSwap лимит ≈{MAX_FACES_SOFT} лиц на аккаунт — "
                "это лицо может не сохраниться в Discord.")
    faces.append({
        "idname": idname,
        "file": file,
        "created_at": int(time.time()),
        "synced": False,
        "last_error": None,
    })
    _save_faces(faces)
    return True, warn


def update_face(idname: str, **fields: Any) -> bool:
    faces = get_faces()
    for f in faces:
        if f.get("idname") == idname:
            f.update(fields)
            _save_faces(faces)
            return True
    return False


def remove_face(idname: str) -> bool:
    faces = get_faces()
    kept = [f for f in faces if f.get("idname") != idname]
    if len(kept) == len(faces):
        return False
    _save_faces(kept)
    if get_active_face() == idname:
        set_active_face(None)
    return True


def get_active_face() -> str | None:
    v = _get("active_face", None)
    return str(v) if v else None


def set_active_face(value: str | None) -> None:
    _set("active_face", value)


# ── timeouts / credits ─────────────────────────────────────────────────────

def swap_timeout() -> int:
    try:
        return int(_get("swap_timeout", DEFAULT_SWAP_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_SWAP_TIMEOUT


def set_swap_timeout(seconds: int) -> None:
    _set("swap_timeout", max(10, int(seconds)))


def get_credits() -> dict:
    v = _get("credits", None)
    out = {"remaining": None, "updated_at": None, "daily_limit": DEFAULT_DAILY_LIMIT}
    if isinstance(v, dict):
        out.update(v)
    return out


def set_credits_remaining(remaining: int | None) -> None:
    cur = get_credits()
    cur["remaining"] = remaining
    cur["updated_at"] = int(time.time())
    _set("credits", cur)


def credits_low() -> bool:
    """Preflight §5.6: warn if we know we're below one swap's cost."""
    remaining = get_credits().get("remaining")
    return remaining is not None and remaining < SWAP_COST_CREDITS


# ── idempotency cache (§10) ────────────────────────────────────────────────

def dedup_get(key: str) -> dict | None:
    cache = _get("dedup", None) or {}
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("ts", 0)) > DEDUP_TTL_SECONDS:
        return None
    return entry


def dedup_put(key: str, result: dict) -> None:
    cache = _get("dedup", None) or {}
    now = time.time()
    # prune expired entries so state.json doesn't grow forever
    cache = {
        k: v for k, v in cache.items()
        if isinstance(v, dict) and now - float(v.get("ts", 0)) <= DEDUP_TTL_SECONDS
    }
    result = dict(result)
    result["ts"] = now
    cache[key] = result
    _set("dedup", cache)


# ── storage paths (Master Room, §6) ────────────────────────────────────────

def storage_root() -> Path:
    from ...config import DATA_DIR
    d = Path(DATA_DIR) / "storage" / "swapface"
    d.mkdir(parents=True, exist_ok=True)
    return d


def faces_dir() -> Path:
    d = storage_root() / "faces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def results_dir() -> Path:
    d = storage_root() / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def audit_log(entry: dict) -> None:
    """Append-only swap audit log (§6). No tokens / tokenised URLs inside."""
    import json

    entry = dict(entry)
    entry.setdefault("ts", int(time.time()))
    path = storage_root() / "audit.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
