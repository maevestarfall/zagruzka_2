# Project-map (graphify) addon: persisted state + export/import "base".
# Co-authored with CoCo
"""State for the «🗺 Карта проекта» feature.

Everything lives in ``state.json`` under the ``projectmap`` addon
namespace (via the shared addon-state helper), so it survives restarts
and rides along with every other setting.

What we persist:

* ``token`` / ``enabled`` / ``rotated_at`` — token-gated web link, exactly
  like VISION.
* ``use``   — bool. When ON the map's TEXT is injected into the agent's
  system prompt (see :func:`directive`).
* ``auto``  — bool. When ON the map is rebuilt automatically (lazily, when
  stale) and Hermes re-analyses it.
* ``edges`` — the user-authored «связки» (function-to-function links with a
  note). This is the part the owner curates by hand and exports/imports.
* ``graph`` — the last scanned graph cache ``{nodes, edges, built_at}``.
* ``install`` — graphify download/setup status: none | installing | ready | error.
"""

from __future__ import annotations

import logging
import time

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "projectmap"

# --- web link (mirror of vision) --------------------------------------

import secrets


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
    # Local dev fallback: relative links break Telegram url-buttons
    # (BUTTON_URL_INVALID), silently killing the project-map screen.
    try:
        from ...config import PORT as _port
    except Exception:  # noqa: BLE001
        _port = 8080
    logger.warning(
        "projectmap: PUBLIC_URL is not configured — falling back to "
        "http://127.0.0.1:%s. The link will only work on this machine; "
        "set PUBLIC_URL for production.", _port,
    )
    return f"http://127.0.0.1:{_port}"


def get_link(base_url: str | None = None) -> str:
    token = get_token()
    if not token:
        return ""
    base = (base_url if base_url is not None else _resolve_public_base()).rstrip("/")
    return f"{base}/projectmap/{token}/" if base else f"/projectmap/{token}/"


def generate_link(base_url: str | None = None) -> tuple[str, str]:
    """Rotate token, flip ``enabled`` on, return ``(token, public_link)``."""
    token = secrets.token_urlsafe(24)
    addon_state.set_(ADDON, "token", token)
    addon_state.set_(ADDON, "enabled", True)
    addon_state.set_(ADDON, "rotated_at", int(time.time()))
    return token, get_link(base_url)


def disable_link() -> bool:
    was = is_enabled()
    addon_state.set_(ADDON, "enabled", False)
    return was


# --- AI usage / auto toggles ------------------------------------------

def get_use() -> bool:
    return bool(addon_state.get(ADDON, "use", False))


def set_use(val: bool) -> None:
    addon_state.set_(ADDON, "use", bool(val))


def get_auto() -> bool:
    return bool(addon_state.get(ADDON, "auto", False))


def set_auto(val: bool) -> None:
    addon_state.set_(ADDON, "auto", bool(val))


# --- install status ---------------------------------------------------

def get_install() -> str:
    return str(addon_state.get(ADDON, "install", "none") or "none")


def set_install(status: str) -> None:
    addon_state.set_(ADDON, "install", status)


# --- user-authored "связки" (edges) -----------------------------------

def get_edges() -> list[dict]:
    val = addon_state.get(ADDON, "edges", None)
    return list(val) if isinstance(val, list) else []


def set_edges(edges: list[dict]) -> None:
    addon_state.set_(ADDON, "edges", list(edges or []))


def add_edge(src: str, dst: str, note: str = "", order: int = 0) -> None:
    edges = get_edges()
    edges.append({
        "from": (src or "").strip()[:200],
        "to": (dst or "").strip()[:200],
        "note": (note or "").strip()[:400],
        "order": int(order or 0),
        "ts": int(time.time()),
    })
    set_edges(edges)


def clear_edges() -> int:
    n = len(get_edges())
    set_edges([])
    return n


# --- scanned graph cache ----------------------------------------------

def get_graph() -> dict:
    val = addon_state.get(ADDON, "graph", None)
    return dict(val) if isinstance(val, dict) else {}


def set_graph(graph: dict) -> None:
    addon_state.set_(ADDON, "graph", dict(graph or {}))


def graph_age() -> int:
    g = get_graph()
    try:
        return int(time.time()) - int(g.get("built_at", 0))
    except Exception:  # noqa: BLE001
        return 10**9


# --- first_seen для бейджа «NEW» (ТЗ 3.4, живая карта) ------------------

def get_first_seen() -> dict:
    """``{node_id: unix_ts}`` — когда узел впервые появился на карте."""
    val = addon_state.get(ADDON, "first_seen", None)
    return dict(val) if isinstance(val, dict) else {}


def touch_first_seen(node_ids: list[str]) -> dict:
    """Register unseen nodes with ``now``; drop entries for removed nodes.

    Returns the updated map. Never raises (best effort — украшение)."""
    try:
        now = int(time.time())
        seen = get_first_seen()
        ids = set(node_ids)
        changed = False
        for nid in ids:
            if nid not in seen:
                seen[nid] = now
                changed = True
        for stale in [k for k in seen if k not in ids]:
            del seen[stale]
            changed = True
        if changed:
            addon_state.set_(ADDON, "first_seen", seen)
        return seen
    except Exception:  # noqa: BLE001
        return {}


# --- export / import "base" (portable between identical projects) -----

def export_base() -> dict:
    """The portable document the owner exports and pastes into another
    identical project. Holds the curated edges + the last graph + meta."""
    return {
        "kind": "lilush-projectmap",
        "version": 1,
        "exported_at": int(time.time()),
        "edges": get_edges(),
        "graph": get_graph(),
        "use": get_use(),
        "auto": get_auto(),
    }


def import_base(payload: dict, *, merge: bool = True) -> dict:
    """Load an exported base. Merges edges by default (dedup on from/to/note)."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    incoming = payload.get("edges") or []
    if not isinstance(incoming, list):
        incoming = []
    if merge:
        existing = get_edges()
        seen = {(e.get("from"), e.get("to"), e.get("note")) for e in existing}
        added = 0
        for e in incoming:
            key = (e.get("from"), e.get("to"), e.get("note"))
            if key not in seen:
                existing.append(e)
                seen.add(key)
                added += 1
        set_edges(existing)
    else:
        set_edges(incoming)
        added = len(incoming)
    if isinstance(payload.get("graph"), dict):
        set_graph(payload["graph"])
    return {"added": added, "total": len(get_edges())}


# --- prompt directive -------------------------------------------------

def directive() -> str:
    """System-prompt block describing the project map, or '' when use is off."""
    if not get_use():
        return ""
    from .scan import to_text
    try:
        text = to_text(get_graph(), get_edges())
    except Exception:  # noqa: BLE001
        text = ""
    if not text.strip():
        return ""
    return (
        "## КАРТА ПРОЕКТА (связки между функциями)\n"
        "Опирайся на эту карту, чтобы понимать какая функция что вызывает и в "
        "каком порядке (сначала → потом). Используй её при планировании связок.\n\n"
        + text
    )


# --- weak spots from the Context/TEMPORAL RELAY Assembler (single loop) ---

def set_weak_spots(items) -> None:
    """Store the Assembler's weak-spots so they can be highlighted on the map."""
    addon_state.set_(ADDON, "weak_spots", [str(x) for x in (items or [])][:50])


def get_weak_spots() -> list:
    v = addon_state.get(ADDON, "weak_spots", None)
    return list(v) if isinstance(v, list) else []
