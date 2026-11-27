"""Зашифрованные прокси-туннели: VLESS + Reality и Shadowsocks.

Браузер умеет ходить только в http/socks. VLESS/Reality и Shadowsocks — это
шифрованные транспорты, которые напрямую браузер не понимает. Поэтому здесь
поднимается локальный клиент **sing-box**: он слушает локальный SOCKS5
(``127.0.0.1:10808``) и заворачивает трафик в зашифрованный канал до твоего
сервера. Движок браузера в итоге использует обычный ``socks5://127.0.0.1:10808``
— а наружу всё идёт через VLESS/Reality либо Shadowsocks.

Поддерживаемые ссылки (их обычно даёт панель/провайдер):
* ``vless://<uuid>@host:port?security=reality&pbk=...&sni=...&sid=...&flow=...#name``
* ``ss://<base64(method:password)>@host:port#name``  (SIP002)
* ``ss://<base64(method:password@host:port)>#name``   (legacy)

Туннель запускается при активации профиля «Железо и прокси» и кладёт свой
локальный SOCKS5-адрес в ``issuance.fingerprint_proxy`` — дальше всё работает
как с обычным прокси.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request

from . import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "proxy_tunnel"

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 10808
LOCAL_SOCKS = f"socks5://{LOCAL_HOST}:{LOCAL_PORT}"

_DATA = os.environ.get("DATA_DIR", "/data")
_BIN_DIR = os.path.join(_DATA, "bin")
_BIN = os.path.join(_BIN_DIR, "sing-box")
_CONFIG = os.path.join(_DATA, "sing-box.json")
_PID_KEY = "pid"

# Pinned sing-box release (single static binary, no deps).
SINGBOX_VERSION = "1.9.3"


# --------------------------------------------------------------------------
# detection / install
# --------------------------------------------------------------------------


def is_tunnel_uri(value: str) -> bool:
    v = (value or "").strip().lower()
    return v.startswith("vless://") or v.startswith("ss://")


def proto_of(value: str) -> str:
    v = (value or "").strip().lower()
    if v.startswith("vless://"):
        return "vless"
    if v.startswith("ss://"):
        return "shadowsocks"
    return ""


def _arch() -> str:
    m = os.uname().machine.lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if "armv7" in m or m.startswith("arm"):
        return "armv7"
    return "amd64"


def singbox_installed() -> bool:
    return os.path.isfile(_BIN) and os.access(_BIN, os.X_OK)


def ensure_singbox() -> tuple[bool, str]:
    """Download the pinned sing-box static binary if it's missing."""
    if singbox_installed():
        return True, "sing-box уже установлен."
    os.makedirs(_BIN_DIR, exist_ok=True)
    arch = _arch()
    base = f"sing-box-{SINGBOX_VERSION}-linux-{arch}"
    url = (
        f"https://github.com/SagerNet/sing-box/releases/download/"
        f"v{SINGBOX_VERSION}/{base}.tar.gz"
    )
    tgz = os.path.join(_BIN_DIR, "sing-box.tar.gz")
    try:
        urllib.request.urlretrieve(url, tgz)  # noqa: S310 — pinned trusted URL
        import tarfile
        with tarfile.open(tgz) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("/sing-box"))
            member.name = "sing-box"
            tf.extract(member, _BIN_DIR)
        os.chmod(_BIN, 0o755)
    except Exception as e:  # noqa: BLE001
        return False, f"не удалось скачать sing-box ({arch}): {e}"
    finally:
        with contextlib.suppress(Exception):
            os.remove(tgz)
    return (singbox_installed(), "sing-box установлен." if singbox_installed()
            else "бинарь sing-box не появился после распаковки.")


# --------------------------------------------------------------------------
# URI parsing -> sing-box outbound
# --------------------------------------------------------------------------


def _parse_vless(uri: str) -> dict:
    """vless://uuid@host:port?security=reality&pbk=..&sni=..&sid=..&flow=..#tag"""
    u = urllib.parse.urlparse(uri)
    uuid = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = int(u.port or 443)
    q = urllib.parse.parse_qs(u.query)

    def g(k, default=""):
        return q.get(k, [default])[0]

    out: dict = {
        "type": "vless",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "packet_encoding": "xudp",
    }
    flow = g("flow")
    if flow:
        out["flow"] = flow
    security = g("security", "none")
    if security in ("reality", "tls"):
        tls: dict = {"enabled": True, "server_name": g("sni") or g("host") or host}
        fp = g("fp")
        if fp:
            tls["utls"] = {"enabled": True, "fingerprint": fp}
        if security == "reality":
            tls["reality"] = {
                "enabled": True,
                "public_key": g("pbk"),
                "short_id": g("sid"),
            }
        out["tls"] = tls
    return out


def _parse_ss(uri: str) -> dict:
    """ss URIs: SIP002 (ss://b64(method:pass)@host:port#tag) or legacy."""
    raw = uri[len("ss://"):]
    tag = "proxy"
    if "#" in raw:
        raw, frag = raw.split("#", 1)
        tag = urllib.parse.unquote(frag) or "proxy"
    raw = raw.split("?", 1)[0]

    method = password = host = ""
    port = 0
    if "@" in raw:
        # SIP002: base64(method:password)@host:port
        userinfo, hostpart = raw.rsplit("@", 1)
        userinfo = _b64d(userinfo) or urllib.parse.unquote(userinfo)
        if ":" in userinfo:
            method, password = userinfo.split(":", 1)
        host, _, p = hostpart.partition(":")
        port = int(p or 0)
    else:
        # legacy: base64(method:password@host:port)
        dec = _b64d(raw)
        creds, _, hostpart = dec.rpartition("@")
        method, _, password = creds.partition(":")
        host, _, p = hostpart.partition(":")
        port = int(p or 0)
    return {
        "type": "shadowsocks",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "method": method,
        "password": password,
    }


def _b64d(s: str) -> str:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    for variant in (s + pad, s.replace("-", "+").replace("_", "/") + pad):
        with contextlib.suppress(Exception):
            return base64.b64decode(variant).decode("utf-8", "replace")
    return ""


def parse_uri(uri: str) -> dict | None:
    uri = (uri or "").strip()
    try:
        if uri.lower().startswith("vless://"):
            return _parse_vless(uri)
        if uri.lower().startswith("ss://"):
            return _parse_ss(uri)
    except Exception as e:  # noqa: BLE001
        logger.warning("parse_uri failed: %s", e)
    return None


def build_config(uri: str) -> dict | None:
    outbound = parse_uri(uri)
    if not outbound or not outbound.get("server"):
        return None
    return {
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "socks",
            "tag": "in",
            "listen": LOCAL_HOST,
            "listen_port": LOCAL_PORT,
        }],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
    }


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def _port_open() -> bool:
    with contextlib.suppress(Exception):
        with socket.create_connection((LOCAL_HOST, LOCAL_PORT), timeout=1.5):
            return True
    return False


def is_running() -> bool:
    pid = addon_state.get(ADDON, _PID_KEY, 0)
    if pid:
        with contextlib.suppress(Exception):
            os.kill(int(pid), 0)  # signal 0 = liveness check
            return _port_open()
    return False


def stop() -> None:
    pid = addon_state.get(ADDON, _PID_KEY, 0)
    if pid:
        with contextlib.suppress(Exception):
            os.kill(int(pid), 15)
    addon_state.set_(ADDON, _PID_KEY, 0)


def start(uri: str) -> tuple[bool, str]:
    """(Re)start the local tunnel for *uri*. Returns (ok, message)."""
    cfg = build_config(uri)
    if cfg is None:
        return False, "не смог разобрать ссылку (vless:// или ss://)."

    ok, msg = ensure_singbox()
    if not ok:
        return False, msg

    stop()  # kill any previous instance before rebinding the port
    try:
        with open(_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception as e:  # noqa: BLE001
        return False, f"не смог записать конфиг: {e}"

    try:
        proc = subprocess.Popen(  # noqa: S603
            [_BIN, "run", "-c", _CONFIG],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"не смог запустить sing-box: {e}"

    addon_state.set_(ADDON, _PID_KEY, proc.pid)
    addon_state.set_(ADDON, "uri", uri)
    # Wait for the SOCKS port to come up.
    for _ in range(12):
        if _port_open():
            proto = proto_of(uri)
            return True, f"туннель {proto} поднят → {LOCAL_SOCKS}"
        if proc.poll() is not None:
            return False, "sing-box завершился сразу (проверь ссылку/сервер)."
        time.sleep(0.5)
    return False, "локальный SOCKS-порт не открылся за отведённое время."


def status_line() -> str:
    uri = addon_state.get(ADDON, "uri", "")
    if not uri:
        return "туннель не настроен"
    proto = proto_of(uri) or "?"
    state = "🟢 работает" if is_running() else "🔴 остановлен"
    return f"{proto.upper()} · {state} · {LOCAL_SOCKS}"


# --------------------------------------------------------------------------
# per-protocol storage + enable/disable (VLESS / Shadowsocks buttons)
# --------------------------------------------------------------------------

_URIS_KEY = "uris"            # {"vless": "...", "shadowsocks": "..."}
_ENABLED_KEY = "enabled_proto"  # "" | "vless" | "shadowsocks"


def get_uri(proto: str) -> str:
    uris = addon_state.get(ADDON, _URIS_KEY, {}) or {}
    return str(uris.get(proto, "") or "")


def set_uri(proto: str, uri: str) -> None:
    uris = dict(addon_state.get(ADDON, _URIS_KEY, {}) or {})
    uris[proto] = (uri or "").strip()
    addon_state.set_(ADDON, _URIS_KEY, uris)


def get_enabled_proto() -> str:
    return str(addon_state.get(ADDON, _ENABLED_KEY, "") or "")


def _push_engine_proxy(value: str) -> None:
    """Mirror the effective proxy into issuance.core for the browser engine."""
    with contextlib.suppress(Exception):
        from .issuance import core as ic
        ic.set_fingerprint_proxy(value)


def enable(proto: str) -> tuple[bool, str]:
    """Start the tunnel for *proto* (vless|shadowsocks) and route the browser."""
    uri = get_uri(proto)
    if not uri:
        return False, "сначала вставь ссылку для этого протокола."
    if proto_of(uri) != proto:
        return False, f"сохранённая ссылка не похожа на {proto}."
    ok, msg = start(uri)
    if not ok:
        return False, msg
    addon_state.set_(ADDON, _ENABLED_KEY, proto)
    _push_engine_proxy(LOCAL_SOCKS)
    return True, msg


def disable() -> tuple[bool, str]:
    """Stop the tunnel and hand the browser back to the profile's own proxy."""
    stop()
    addon_state.set_(ADDON, _ENABLED_KEY, "")
    # Restore the active hwproxy profile's plain proxy (skip if it's itself a
    # tunnel URI — then just clear, to avoid re-spawning a tunnel here).
    restored = ""
    with contextlib.suppress(Exception):
        from . import hwproxy
        prof = hwproxy.active_profile() or {}
        raw = str(prof.get("proxy", "") or "")
        restored = "" if is_tunnel_uri(raw) else raw
    _push_engine_proxy(restored)
    return True, "туннель остановлен."


def proto_status_line(proto: str) -> str:
    uri = get_uri(proto)
    if not uri:
        return "ссылка не задана"
    on = get_enabled_proto() == proto and is_running()
    return ("🟢 включён · " if on else "🔴 выключен · ") + f"{LOCAL_SOCKS}"
