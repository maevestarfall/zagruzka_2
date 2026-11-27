"""Централизованные конфигурации «🖥 Железо и прокси».

Один источник правды для антидетект-профилей: несколько конфигураций
железа (fingerprint) + прокси, чтобы ротировать и не дать себя отследить.
Профили хранятся в зашифрованном «сейфе» (vault.encrypt_data) внутри
state.json, активный профиль применяется к движку фингерпринта
(``issuance.core``), который уже используют браузерные функции —
Омни, GitHub-вход, Render-вход.

Формат профиля (он же формат импорта/экспорта):
{
  "id": "win_intel_rtx3080",
  "name": "Windows (Intel i7, RTX 3080)",
  "os": "Windows",
  "hardware": "Intel Core i7-12700K / NVIDIA GeForce RTX 3080",
  "user_agent": "",
  "webgl_vendor": "",
  "webgl_renderer": "",
  "seed": "win_intel_rtx3080_seed_abc",
  "proxy": "http://user:pass@host:port"
}
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid

from . import state as addon_state

logger = logging.getLogger(__name__)

_NS = "hwproxy"
_SAFE_KEY = "safe"          # encrypted blob of the profiles list
_ACTIVE_KEY = "active_id"   # plain — not a secret


# ---- safe (encrypted) storage ------------------------------------------


def _encrypt(payload: dict) -> str | None:
    with contextlib.suppress(Exception):
        from .vault.handlers import encrypt_data
        return encrypt_data(payload)
    return None


def _decrypt(blob: str) -> dict | None:
    with contextlib.suppress(Exception):
        from .vault.handlers import decrypt_data
        return decrypt_data(blob)
    return None


def _seed_defaults() -> list[dict]:
    """Build the default profile set from the issuance presets."""
    out: list[dict] = []
    with contextlib.suppress(Exception):
        from .issuance.core import PROFILE_PRESETS
        for key, preset in PROFILE_PRESETS.items():
            out.append({
                "id": key,
                "name": str(preset.get("name", key)),
                "os": str(preset.get("os", "")),
                "hardware": str(preset.get("hardware", "")),
                "user_agent": "",
                "webgl_vendor": "",
                "webgl_renderer": "",
                "seed": str(preset.get("seed", "")),
                "proxy": "",
            })
    return out


def _load() -> list[dict]:
    raw = addon_state.get(_NS, _SAFE_KEY, None)
    if isinstance(raw, str) and raw.strip():
        data = _decrypt(raw)
        if isinstance(data, dict) and isinstance(data.get("profiles"), list):
            return [p for p in data["profiles"] if isinstance(p, dict)]
    # No safe yet (or decrypt failed) — seed defaults and persist.
    defaults = _seed_defaults()
    _save(defaults)
    return defaults


def _save(profiles: list[dict]) -> None:
    blob = _encrypt({"profiles": profiles})
    if blob is not None:
        addon_state.set_(_NS, _SAFE_KEY, blob)
    else:
        # Fallback: store as plain list if encryption unavailable.
        addon_state.set_(_NS, _SAFE_KEY + "_plain", profiles)


# ---- public CRUD --------------------------------------------------------


def list_profiles() -> list[dict]:
    return _load()


def get_profile(pid: str) -> dict | None:
    for p in _load():
        if p.get("id") == pid:
            return p
    return None


def add_profile(name: str, *, hardware: str = "", os_name: str = "",
                proxy: str = "", seed: str = "") -> dict:
    profiles = _load()
    if not seed:
        with contextlib.suppress(Exception):
            from .issuance.core import generate_random_seed
            seed = generate_random_seed()
    prof = {
        "id": uuid.uuid4().hex[:12],
        "name": name or "Новый профиль",
        "os": os_name,
        "hardware": hardware,
        "user_agent": "",
        "webgl_vendor": "",
        "webgl_renderer": "",
        "seed": seed or uuid.uuid4().hex,
        "proxy": proxy,
    }
    profiles.append(prof)
    _save(profiles)
    return prof


def remove_profile(pid: str) -> None:
    profiles = [p for p in _load() if p.get("id") != pid]
    _save(profiles)
    if get_active_id() == pid:
        set_active_id(profiles[0]["id"] if profiles else "")


def update_profile(pid: str, **fields) -> None:
    profiles = _load()
    for p in profiles:
        if p.get("id") == pid:
            p.update({k: v for k, v in fields.items() if k in p})
            break
    _save(profiles)


def set_proxy(pid: str, proxy: str) -> None:
    update_profile(pid, proxy=proxy)
    if get_active_id() == pid:
        apply_active()


# ---- active selection ---------------------------------------------------


def get_active_id() -> str:
    aid = str(addon_state.get(_NS, _ACTIVE_KEY, "") or "")
    if aid and get_profile(aid):
        return aid
    profiles = _load()
    return profiles[0]["id"] if profiles else ""


def set_active_id(pid: str) -> None:
    addon_state.set_(_NS, _ACTIVE_KEY, pid or "")
    if pid:
        apply_active()


def active_profile() -> dict | None:
    return get_profile(get_active_id())


def effective_proxy(prof: dict | None = None) -> str:
    """Return the proxy string the browser engine should actually use.

    For plain http/socks proxies this is just the stored value. For encrypted
    transports (``vless://`` / ``ss://``) we (re)start the local sing-box
    tunnel and return its local SOCKS5 endpoint instead — the browser can't
    speak VLESS/Shadowsocks directly.
    """
    prof = prof or active_profile()
    raw = str((prof or {}).get("proxy", "") or "")
    with contextlib.suppress(Exception):
        from . import proxy_tunnel
        # A standalone VLESS/Shadowsocks tunnel (its own button) wins outright.
        if proxy_tunnel.get_enabled_proto() and proxy_tunnel.is_running():
            return proxy_tunnel.LOCAL_SOCKS
        if raw and proxy_tunnel.is_tunnel_uri(raw):
            if not proxy_tunnel.is_running():
                proxy_tunnel.start(raw)
            return proxy_tunnel.LOCAL_SOCKS
    return raw


def apply_active() -> None:
    """Push the active profile's fingerprint + proxy into issuance.core,
    so existing browser flows (Omni, GitHub, Render) pick it up."""
    prof = active_profile()
    if not prof:
        return
    prof = derive_consistent(prof)
    with contextlib.suppress(Exception):
        from .issuance import core as ic
        if prof.get("seed"):
            ic.set_fingerprint_seed(str(prof["seed"]))
        if prof.get("user_agent"):
            ic.set_fingerprint_user_agent(str(prof["user_agent"]))
        if prof.get("webgl_vendor"):
            ic.set_fingerprint_webgl_vendor(str(prof["webgl_vendor"]))
        if prof.get("webgl_renderer"):
            ic.set_fingerprint_webgl_renderer(str(prof["webgl_renderer"]))
        # Proxy: resolve encrypted transports to the local tunnel SOCKS.
        ic.set_fingerprint_proxy(effective_proxy(prof))


# ---- import / export ----------------------------------------------------


def export_text() -> str:
    return json.dumps(_load(), ensure_ascii=False, indent=2)


def import_text(text: str, *, replace: bool = False) -> tuple[bool, int, str]:
    """Import profiles from JSON (a list or a single object).

    Returns (ok, imported_count, error_message).
    """
    try:
        data = json.loads((text or "").strip())
    except Exception as e:  # noqa: BLE001
        return False, 0, f"невалидный JSON: {e}"
    items = data if isinstance(data, list) else [data]
    cleaned: list[dict] = []
    for it in items:
        if not isinstance(it, dict) or not (it.get("name") or it.get("hardware")):
            continue
        cleaned.append({
            "id": str(it.get("id") or uuid.uuid4().hex[:12]),
            "name": str(it.get("name", "Импорт")),
            "os": str(it.get("os", "")),
            "hardware": str(it.get("hardware", "")),
            "user_agent": str(it.get("user_agent", "")),
            "webgl_vendor": str(it.get("webgl_vendor", "")),
            "webgl_renderer": str(it.get("webgl_renderer", "")),
            "seed": str(it.get("seed", "") or uuid.uuid4().hex),
            "proxy": str(it.get("proxy", "")),
        })
    if not cleaned:
        return False, 0, "не нашёл ни одного профиля (нужны поля name/hardware)"
    if replace:
        _save(cleaned)
    else:
        existing = _load()
        ids = {p.get("id") for p in existing}
        for c in cleaned:
            if c["id"] in ids:
                c["id"] = uuid.uuid4().hex[:12]
            existing.append(c)
        _save(existing)
    return True, len(cleaned), ""


def derive_consistent(profile: dict) -> dict:
    """Return a copy of *profile* with user_agent / webgl_* filled in so the
    whole fingerprint is internally consistent with the chosen os/hardware.

    Presets only ship name/os/hardware/seed; without this the engine would fall
    back to RANDOM user-agent and WebGL, producing a mismatched fingerprint
    (e.g. Windows hardware but an Apple GPU / Mac UA) that is easy to flag.
    Explicit values already on the profile are always preserved.
    """
    p = dict(profile or {})
    os_name = (p.get("os") or "").lower()
    hw = (p.get("hardware") or "").lower()

    # Pick a coherent (UA, webgl_vendor, webgl_renderer) triple.
    ua = vendor = renderer = ""
    if "android" in os_name or "pixel" in hw or "tensor" in hw or "mali" in hw:
        ua = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
        vendor = "Google Inc. (ARM)"
        renderer = "ANGLE (ARM, Mali-G710, OpenGL ES 3.2)"
    elif "mac" in os_name or "apple" in hw:
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        vendor = "Apple Inc."
        renderer = "Apple GPU"
    elif "linux" in os_name:
        ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        if "radeon" in hw or "amd" in hw:
            vendor = "Google Inc. (AMD)"
            renderer = "ANGLE (AMD, AMD Radeon RX 6700 XT, OpenGL 4.6)"
        elif "nvidia" in hw or "geforce" in hw:
            vendor = "Google Inc. (NVIDIA)"
            renderer = "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080, OpenGL 4.6)"
        else:
            vendor = "Google Inc. (Intel)"
            renderer = "ANGLE (Intel, Mesa Intel(R) UHD Graphics, OpenGL 4.6)"
    else:  # default: Windows
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        if "radeon" in hw or "amd" in hw:
            vendor = "Google Inc. (AMD)"
            renderer = ("ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, "
                        "D3D11)")
        elif "nvidia" in hw or "geforce" in hw or "rtx" in hw:
            vendor = "Google Inc. (NVIDIA)"
            renderer = ("ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, "
                        "D3D11)")
        else:
            vendor = "Google Inc. (Intel)"
            renderer = ("ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, "
                        "D3D11)")

    if not p.get("user_agent"):
        p["user_agent"] = ua
    if not p.get("webgl_vendor"):
        p["webgl_vendor"] = vendor
    if not p.get("webgl_renderer"):
        p["webgl_renderer"] = renderer
    return p


def request_refresh() -> None:
    """Force the browser engine to rebuild the per-domain fingerprint cache.

    The engine caches a fingerprint per visited domain. Setting this flag makes
    it drop the cached profile on the next visit and re-apply the active hwproxy
    profile, so a profile/proxy switch takes effect even on already-visited
    domains. Also re-pushes the active fingerprint into issuance.core.
    """
    with contextlib.suppress(Exception):
        addon_state.set_("browser", "refresh_fingerprint", True)
    apply_active()


def reset_defaults() -> None:
    _save(_seed_defaults())
    set_active_id("")


def summary_line() -> str:
    """Short one-liner for other screens (GitHub/Render/Omni/анкета)."""
    prof = active_profile()
    if not prof:
        return "профиль не выбран"
    hw = prof.get("name") or prof.get("hardware") or prof.get("id")
    proxy = "прокси есть" if prof.get("proxy") else "без прокси"
    return f"{hw} · {proxy}"
