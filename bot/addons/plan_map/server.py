# План карта web server: token-gated flowchart editor + JSON storage.
# Co-authored with CoCo
"""HTTP endpoints for «🗺 План карта».

Token-gated, same shape as Fable5:

* ``GET  /planmap/<token>/``     — self-contained flowchart editor (SPA).
* ``GET  /planmap/<token>/data`` — plan JSON ``{title, nodes, edges}``.
* ``POST /planmap/<token>/data`` — save plan JSON (autosave from editor).
* ``GET  /planmap/<token>/ping`` — liveness JSON.

The token + enabled flag live in ``state.json`` under
``addons.plan_map``; flipping ``enabled`` to ``False`` kills access
without rotating the token. :func:`generate_link` always rotates the
token and sets ``enabled = True`` so a leaked link is dead after one
extra tap.

The plan itself is a single JSON file inside the Master Room storage
(``/data/storage/plan_map.json``) so it survives restarts, shows up in
the «💾 Хранилище» screen and can be exported / imported through the
Telegram bot.

Bad / disabled token → ``403 {"error": "forbidden"}`` (neutral, so the
URL doesn't leak whether it once existed).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "plan_map"

PLAN_FILE_NAME = "plan_map.json"
_MAX_PLAN_BYTES = 2 * 1024 * 1024  # 2 MB of JSON is plenty for a flowchart

# Editor HTML — loaded once at import time from the sibling file.
_EDITOR_DIR = Path(__file__).resolve().parent
_EDITOR_HTML: str | None = None

_SEC_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
}


def _secure(resp: web.Response) -> web.Response:
    for k, v in _SEC_HEADERS.items():
        resp.headers[k] = v
    return resp


def _load_editor() -> str:
    """Lazy-load editor HTML from the file next to this module."""
    global _EDITOR_HTML  # noqa: PLW0603
    if _EDITOR_HTML is None:
        p = _EDITOR_DIR / "editor.html"
        if p.exists():
            _EDITOR_HTML = p.read_text(encoding="utf-8")
        else:
            _EDITOR_HTML = (
                "<!DOCTYPE html><html><body>"
                "<h1>План карта</h1><p>editor.html not found</p>"
                "</body></html>"
            )
    return _EDITOR_HTML


# --- plan storage (Master Room «💾 Хранилище») --------------------------

def plan_file() -> Path:
    """Path to the plan JSON inside /data/storage (Master Room storage)."""
    from ...config import DATA_DIR
    d = DATA_DIR / "storage"
    d.mkdir(parents=True, exist_ok=True)
    return d / PLAN_FILE_NAME


PLAN_COUNT = 5
_EDGE_SIDES = ("t", "r", "b", "l", "")
# Ключ ячейки таблицы: "row_col" (например "0_2") — как в route.ts.
_CELL_KEY_RE = re.compile(r"^\d+_\d+$")
# Цвет (стрелки / фломастер): строгий hex, как в route.ts.
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# SVG-путь линии фломастера: только команды M/L/Q/C/Z и числа.
_DRAW_PATH_RE = re.compile(r"^[MLQCZmlqcz0-9 .,-]+$")
# Названия шрифтов из панели редактора: системный + 5 популярных из Word.
_FONT_IDS = ("system", "calibri", "arial", "times", "verdana", "georgia")


def _empty_single_plan(i: int) -> dict:
    return {"name": f"план {i + 1}", "nodes": [], "edges": [], "draw": []}


def _empty_plan() -> dict:
    return {
        "version": 2,
        "active": 0,
        "plans": [_empty_single_plan(i) for i in range(PLAN_COUNT)],
        "title": "План карта",
        "nodes": [],
        "edges": [],
        "updated_at": 0,
    }


def _normalize_single_plan(raw: Any, fallback_name: str) -> Optional[dict]:
    """Validate one plan ``{name?, nodes, edges}``; ``None`` if invalid."""
    if not isinstance(raw, dict):
        return None
    nodes_in = raw.get("nodes")
    edges_in = raw.get("edges")
    if not isinstance(nodes_in, list) or not isinstance(edges_in, list):
        return None
    nodes: list[dict] = []
    ids: set[str] = set()
    for n in nodes_in[:500]:
        if not isinstance(n, dict):
            return None
        nid = str(n.get("id", "") or "")
        if not nid or nid in ids:
            return None
        ids.add(nid)
        try:
            x = float(n.get("x", 0))
            y = float(n.get("y", 0))
        except (TypeError, ValueError):
            return None
        kind = str(n.get("kind", "") or "box")
        if kind not in ("box", "note", "table"):
            kind = "box"
        try:
            w = float(n.get("w", 0) or 0)
        except (TypeError, ValueError):
            w = 0.0
        # Ширина квадратика (тянется за правый край); 0 = по умолчанию.
        max_w = 1400.0 if kind == "table" else 800.0
        w = 0.0 if w <= 0 else min(max_w, max(160.0, w))
        # Ручная высота (тянется за кружок в центре); 0 = авто.
        try:
            h = float(n.get("h", 0) or 0)
        except (TypeError, ValueError):
            h = 0.0
        h = 0.0 if h <= 0 else min(1600.0, max(60.0, h))
        # Таблица: деления вниз × в ширину + ячейки "row_col" → текст.
        try:
            rows = int(n.get("rows", 0) or 0)
        except (TypeError, ValueError):
            rows = 0
        rows = min(30, max(0, rows))
        try:
            cols = int(n.get("cols", 0) or 0)
        except (TypeError, ValueError):
            cols = 0
        cols = min(12, max(0, cols))
        cells: dict[str, str] = {}
        cells_in = n.get("cells")
        if isinstance(cells_in, dict):
            for k, v in list(cells_in.items())[:400]:
                key = str(k)
                if _CELL_KEY_RE.match(key):
                    cells[key] = ("" if v is None else str(v))[:2000]
        # Свободный поток блоков квадратика: текст / действия / промт
        # в любом порядке (новый формат; старые поля ниже зеркалируются
        # редактором для совместимости с ботом).
        blocks: list[dict] | None = None
        blocks_in = n.get("blocks")
        if isinstance(blocks_in, list):
            blocks = []
            for b in blocks_in[:100]:
                if not isinstance(b, dict):
                    continue
                bt = b.get("t")
                if bt not in ("text", "action", "prompt"):
                    bt = "text"
                bv = b.get("v")
                blocks.append(
                    {"t": bt, "v": ("" if bv is None else str(bv))[:5000]}
                )
        node_out = {
            "id": nid,
            "kind": kind,
            "name": str(n.get("name", "") or "")[:200],
            "text": str(n.get("text", "") or "")[:5000],
            "actions": str(n.get("actions", "") or "")[:5000],
            "actions_note": str(n.get("actions_note", "") or "")[:5000],
            "prompt": str(n.get("prompt", "") or "")[:5000],
            "prompt_note": str(n.get("prompt_note", "") or "")[:5000],
            "show_actions": bool(n.get("show_actions", False)),
            "show_prompt": bool(n.get("show_prompt", False)),
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "rows": rows,
            "cols": cols,
            "cells": cells,
        }
        if blocks is not None:
            node_out["blocks"] = blocks
        # Личный шрифт квадратика: {family?, size?, bold?} — режим
        # «только выбранным» должен переживать перезагрузку страницы.
        font_in = n.get("font")
        if isinstance(font_in, dict):
            font_out: dict = {}
            fam = str(font_in.get("family", "") or "")
            if fam in _FONT_IDS:
                font_out["family"] = fam
            try:
                fsize = int(font_in.get("size", 0) or 0)
            except (TypeError, ValueError):
                fsize = 0
            if 9 <= fsize <= 32:
                font_out["size"] = fsize
            if isinstance(font_in.get("bold"), bool):
                font_out["bold"] = font_in["bold"]
            if font_out:
                node_out["font"] = font_out
        nodes.append(node_out)
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in edges_in[:2000]:
        if not isinstance(e, dict):
            return None
        src = str(e.get("from", "") or "")
        dst = str(e.get("to", "") or "")
        if src not in ids or dst not in ids or src == dst:
            continue  # drop dangling arrows silently (import tolerance)
        if (src, dst) in seen:
            continue
        seen.add((src, dst))
        try:
            style = int(e.get("style", 1) or 1)
        except (TypeError, ValueError):
            style = 1
        if style < 1 or style > 5:
            style = 1
        from_side = str(e.get("from_side", "") or "")
        if from_side not in _EDGE_SIDES:
            from_side = ""
        edges.append(
            {"from": src, "to": dst, "style": style, "from_side": from_side}
        )
    name = str(raw.get("name") or raw.get("title") or "")[:200] or fallback_name
    return {"name": name, "nodes": nodes, "edges": edges}


def normalize_plan(raw: Any) -> Optional[dict]:
    """Validate + normalize an imported/POSTed plan; ``None`` if invalid.

    Accepts both the v2 store shape::

        {"version": 2, "active": int,
         "plans": [{"name", "nodes", "edges"}, ... x5]}

    and the legacy v1 shape ``{"title", "nodes", "edges"}`` which is
    migrated into plan slot 0. The result always mirrors the active
    plan's ``nodes``/``edges`` at the top level for compatibility.
    """
    if not isinstance(raw, dict):
        return None
    store = _empty_plan()
    plans_in = raw.get("plans")
    if isinstance(plans_in, list):
        for i in range(PLAN_COUNT):
            p_raw = plans_in[i] if i < len(plans_in) else None
            p = _normalize_single_plan(p_raw, f"план {i + 1}")
            if p is not None:
                store["plans"][i] = p
        try:
            active = int(raw.get("active", 0) or 0)
        except (TypeError, ValueError):
            active = 0
        if active < 0 or active >= PLAN_COUNT:
            active = 0
        store["active"] = active
    else:
        p = _normalize_single_plan(raw, "план 1")
        if p is None:
            return None
        store["plans"][0] = p
        store["active"] = 0
    active_plan = store["plans"][store["active"]]
    store["title"] = active_plan["name"] or "План карта"
    store["nodes"] = active_plan["nodes"]
    store["edges"] = active_plan["edges"]
    store["updated_at"] = int(time.time())
    return store


def load_plan() -> dict:
    p = plan_file()
    if not p.exists():
        return _empty_plan()
    try:
        plan = normalize_plan(json.loads(p.read_text(encoding="utf-8")))
        return plan if plan is not None else _empty_plan()
    except Exception:  # noqa: BLE001
        logger.exception("plan_map: failed to read %s", p)
        return _empty_plan()


def save_plan(plan: dict) -> None:
    p = plan_file()
    p.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --- token / state (same principle as Fable5) --------------------------

def is_enabled() -> bool:
    return bool(addon_state.get(ADDON, "enabled", False))


def get_token() -> str:
    return str(addon_state.get(ADDON, "token", "") or "")


def _resolve_public_base() -> str:
    from ...config import resolve_public_url
    return resolve_public_url()


def get_link(base_url: str | None = None) -> str:
    token = get_token()
    if not token:
        return ""
    base = (base_url if base_url is not None else _resolve_public_base()).rstrip("/")
    return f"{base}/planmap/{token}/" if base else f"/planmap/{token}/"


def generate_link(base_url: str | None = None) -> tuple[str, str]:
    """Rotate token, flip ``enabled`` on, return ``(token, public_link)``."""
    token = secrets.token_urlsafe(24)
    addon_state.set_(ADDON, "token", token)
    addon_state.set_(ADDON, "enabled", True)
    addon_state.set_(ADDON, "rotated_at", int(time.time()))
    return token, get_link(base_url)


def disable_link() -> bool:
    """Flip ``enabled → False``. Returns the previous state."""
    was = is_enabled()
    addon_state.set_(ADDON, "enabled", False)
    return was


# --- HTTP handlers ------------------------------------------------------

def _check(token: str) -> Optional[web.Response]:
    stored = get_token()
    if not stored or not secrets.compare_digest(stored, token):
        return _secure(web.json_response({"error": "forbidden"}, status=403))
    if not is_enabled():
        return _secure(web.json_response({"error": "forbidden"}, status=403))
    return None


async def _editor(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return _secure(web.Response(text=_load_editor(), content_type="text/html"))


async def _ping(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    plan = load_plan()
    return _secure(
        web.json_response(
            {
                "ok": True,
                "nodes": len(plan.get("nodes", [])),
                "edges": len(plan.get("edges", [])),
                "version": "planmap/1",
            }
        )
    )


async def _data_get(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return _secure(web.json_response(load_plan()))


async def _data_post(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    if request.content_length and request.content_length > _MAX_PLAN_BYTES:
        return _secure(web.json_response({"error": "too large"}, status=413))
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return _secure(web.json_response({"error": "bad json"}, status=400))
    plan = normalize_plan(raw)
    if plan is None:
        return _secure(web.json_response({"error": "bad plan shape"}, status=400))
    try:
        save_plan(plan)
    except Exception as e:  # noqa: BLE001
        logger.exception("plan_map: save failed")
        return _secure(web.json_response({"error": str(e)}, status=500))
    return _secure(
        web.json_response(
            {"ok": True, "nodes": len(plan["nodes"]), "edges": len(plan["edges"])}
        )
    )


def register_http_endpoints(app: web.Application) -> None:
    """Attach План-карта routes to the bot's aiohttp app.

    Call once at startup in both polling and webhook modes (mirrors
    Fable5's registration).
    """
    app.router.add_get("/planmap/{token}/", _editor)
    app.router.add_get("/planmap/{token}", _editor)
    app.router.add_get("/planmap/{token}/ping", _ping)
    app.router.add_get("/planmap/{token}/data", _data_get)
    app.router.add_post("/planmap/{token}/data", _data_post)
    logger.info("plan_map: HTTP endpoints registered")
