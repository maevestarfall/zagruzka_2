"""HTTP endpoints for Viktor/Fable5 to test the live bot.

Exposes a tiny REST surface under ``/fable5/<token>/...`` that lets an
off-platform agent (Viktor) drive the live bot for verification runs.
The token + enabled flag live in ``state.json`` under
``addons.fable5``; flipping ``enabled`` to ``False`` kills access
without rotating the token. :func:`generate_link` always rotates the
token and sets ``enabled = True`` so a leaked link is dead after one
extra tap.

Available endpoints (every one of them needs the path token to match
``addons.fable5.token`` AND ``addons.fable5.enabled is True``):

* ``GET  /fable5/<token>/`` — HTML dashboard (welcome page).
* ``GET  /fable5/<token>/ping`` — liveness, returns ``{ok, owner_id, brain}``.
* ``GET  /fable5/<token>/info`` — providers / addons summary.
* ``GET  /fable5/<token>/state`` — full ``state.json``; the link is the secret.
* ``POST /fable5/<token>/state`` — JSON body deep-merged into ``state.json``.
* ``POST /fable5/<token>/exec`` — ``{"cmd": "ls", "timeout": 20, "cwd": "..."}``
  runs a shell command, returns stdout / stderr / returncode.
* ``POST /fable5/<token>/send`` — ``{"text": "...", "chat_id": <optional>}``
  sends a Telegram message; default chat is the owner DM.
* ``GET  /fable5/<token>/logs?lines=200&file=inbox.log`` — tail a file
  inside ``storage.data_dir`` (path-traversal blocked).
* ``POST /fable5/<token>/db`` — ``{"sql": "SELECT ..."}`` runs a read-only
  SQLite query against ``debate_v3.db``.
* ``GET  /fable5/<token>/files?path=.`` — list files in ``storage.data_dir``.

Bad / disabled token → ``403 {"error": "forbidden"}`` (neutral, so the
URL doesn't leak whether it once existed).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from .. import state as addon_state
from ...storage import storage

logger = logging.getLogger(__name__)

ADDON = "fable5"
# repo root: bot/addons/fable5/server.py → bot/addons/fable5 → bot/addons → bot → repo
REPO_ROOT = Path(__file__).resolve().parents[3]
APP_BOT_KEY = "fable5_bot"

# Dashboard HTML — loaded once at import time from the sibling file.
_DASHBOARD_DIR = Path(__file__).resolve().parent
_DASHBOARD_HTML: str | None = None


def _load_dashboard() -> str:
    """Lazy-load dashboard HTML from the file next to this module."""
    global _DASHBOARD_HTML  # noqa: PLW0603
    if _DASHBOARD_HTML is None:
        p = _DASHBOARD_DIR / "dashboard.html"
        if p.exists():
            _DASHBOARD_HTML = p.read_text(encoding="utf-8")
        else:
            _DASHBOARD_HTML = (
                "<!DOCTYPE html><html><body>"
                "<h1>Fable5</h1><p>dashboard.html not found</p>"
                "</body></html>"
            )
    return _DASHBOARD_HTML


# --- token / state -----------------------------------------------------

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
    """Delegate to :func:`bot.config.resolve_public_url`.

    Kept as a thin wrapper so the addon's public API (and tests that
    monkey-patch it) stays stable across refactors. The real fallback
    chain (PUBLIC_URL → Render → Railway → Fly → Oracle) lives in
    :mod:`bot.config` so every addon mints links the same way.
    """
    from ...config import resolve_public_url
    return resolve_public_url()


def get_link(base_url: str | None = None) -> str:
    token = get_token()
    if not token:
        return ""
    base = (base_url if base_url is not None else _resolve_public_base()).rstrip("/")
    return f"{base}/fable5/{token}/" if base else f"/fable5/{token}/"


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


# --- HTTP handlers ----------------------------------------------------

def _check(token: str) -> Optional[web.Response]:
    stored = get_token()
    if not stored or not secrets.compare_digest(stored, token):
        return web.json_response({"error": "forbidden"}, status=403)
    if not is_enabled():
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def _dashboard(request: web.Request) -> web.Response:
    """Serve the HTML dashboard (welcome page)."""
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return web.Response(text=_load_dashboard(), content_type="text/html")


async def _ping(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return web.json_response(
        {
            "ok": True,
            "owner_id": storage.get_owner_id(),
            "brain": storage._settings().get("brain"),  # noqa: SLF001
            "rotated_at": get_rotated_at(),
            "version": "fable5/2",
        }
    )


async def _info(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    s = storage._settings()  # noqa: SLF001
    addons = sorted(
        k for k, v in (s.get("addons") or {}).items() if isinstance(v, dict)
    )
    return web.json_response(
        {
            "owner_id": storage.get_owner_id(),
            "brain": s.get("brain"),
            "provider": s.get("provider"),
            "providers_with_keys": sorted((s.get("keys") or {}).keys()),
            "addons": addons,
            "data_dir": str(storage.data_dir),
            "repo_root": str(REPO_ROOT),
        }
    )


async def _state_get(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return web.json_response(storage._state)  # noqa: SLF001


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


async def _state_post(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "expected object"}, status=400)
    _deep_merge(storage._state, payload)  # noqa: SLF001
    storage._save()  # noqa: SLF001
    return web.json_response({"ok": True})


async def _exec(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    cmd = (payload or {}).get("cmd", "")
    try:
        timeout = float((payload or {}).get("timeout", 20))
    except Exception:  # noqa: BLE001
        timeout = 20.0
    timeout = max(1.0, min(timeout, 120.0))
    cwd = (payload or {}).get("cwd") or str(REPO_ROOT)
    if not cmd or not isinstance(cmd, str):
        return web.json_response({"error": "cmd required"}, status=400)

    def _run() -> dict:
        try:
            p = subprocess.run(  # noqa: S602 — gated by Fable5 token
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "ok": True,
                "returncode": p.returncode,
                "stdout": p.stdout[-100_000:],
                "stderr": p.stderr[-100_000:],
            }
        except subprocess.TimeoutExpired as e:
            return {
                "ok": False,
                "error": "timeout",
                "stdout": (e.stdout or "")[-100_000:] if isinstance(e.stdout, str) else "",
                "stderr": (e.stderr or "")[-100_000:] if isinstance(e.stderr, str) else "",
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    result = await asyncio.to_thread(_run)
    return web.json_response(result)


async def _send(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    text = (payload or {}).get("text", "")
    chat_id = (payload or {}).get("chat_id") or storage.get_owner_id()
    parse_mode = (payload or {}).get("parse_mode")
    if not text or not isinstance(text, str):
        return web.json_response({"error": "text required"}, status=400)
    if not chat_id:
        return web.json_response(
            {"error": "no chat_id (owner unset, claim the bot first)"}, status=400
        )
    bot = request.app.get(APP_BOT_KEY)
    if bot is None:
        return web.json_response({"error": "bot not ready"}, status=503)
    try:
        msg = await bot.send_message(int(chat_id), text, parse_mode=parse_mode)
        return web.json_response(
            {"ok": True, "message_id": msg.message_id, "chat_id": msg.chat.id}
        )
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def _logs(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    try:
        lines = max(1, min(int(request.query.get("lines", "200")), 5000))
    except Exception:  # noqa: BLE001
        lines = 200
    fname = request.query.get("file", "inbox.log") or "inbox.log"
    data_dir = storage.data_dir.resolve()
    try:
        path = (storage.data_dir / fname).resolve()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad path"}, status=400)
    if data_dir not in path.parents and path != data_dir:
        return web.json_response({"error": "forbidden path"}, status=403)
    if not path.exists() or not path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    try:
        content = path.read_text(errors="replace").splitlines()[-lines:]
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"file": fname, "lines": content})


async def _db_query(request: web.Request) -> web.Response:
    """Run a read-only SQLite query against debate_v3.db."""
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    sql = (payload or {}).get("sql", "").strip()
    if not sql:
        return web.json_response({"error": "sql required"}, status=400)
    # This endpoint is advertised as read-only. Enforce that contract on the
    # server too; client-side validation alone can be bypassed.
    first_word = sql.lstrip().split(None, 1)[0].lower().rstrip(";")
    if first_word not in {"select", "with", "explain", "pragma"}:
        return web.json_response({"error": "read-only query required"}, status=400)

    db_path = storage.data_dir / "debate_v3.db"
    if not db_path.exists():
        return web.json_response({"error": "debate_v3.db not found"}, status=404)

    def _query() -> dict:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            cur = conn.execute(sql)
            if cur.description:
                columns = [d[0] for d in cur.description]
                rows = [list(row) for row in cur.fetchmany(2000)]
                conn.close()
                return {"columns": columns, "rows": rows, "count": len(rows)}
            else:
                conn.commit()
                conn.close()
                return {"ok": True, "rowcount": cur.rowcount}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    result = await asyncio.to_thread(_query)
    return web.json_response(result)


async def _files(request: web.Request) -> web.Response:
    """List files in storage.data_dir (one level, no traversal)."""
    if (resp := _check(request.match_info["token"])) is not None:
        return resp

    rel = request.query.get("path", ".") or "."
    data_dir = storage.data_dir.resolve()
    try:
        target = (storage.data_dir / rel).resolve()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad path"}, status=400)
    if target != data_dir and data_dir not in target.parents:
        return web.json_response({"error": "forbidden path"}, status=403)
    if not target.exists():
        return web.json_response({"error": "not found"}, status=404)
    if not target.is_dir():
        return web.json_response({"error": "not a directory"}, status=400)

    files = []
    try:
        for entry in sorted(target.iterdir()):
            stat = entry.stat()
            size = stat.st_size
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            files.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": size_str,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            })
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({"path": str(target.relative_to(data_dir)), "files": files})


async def _capabilities(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    path = REPO_ROOT / "data/capabilities.md"
    if not path.is_file():
        return web.json_response({"capabilities": []})
    try:
        content = path.read_text(encoding="utf-8")
        blocks = content.split("<!-- key: ")
        caps = []
        for b in blocks:
            if not b.strip():
                continue
            parts = b.split(" -->\n", 1)
            if len(parts) < 2:
                parts = b.split(" -->\r\n", 1)
            if len(parts) < 2:
                continue
            key = parts[0].strip()
            body = parts[1].strip()
            
            # Extract title and description
            lines = body.split("\n")
            title = ""
            desc_lines = []
            for line in lines:
                line = line.strip()
                if line.startswith("###"):
                    title = line.lstrip("#").strip()
                elif line:
                    desc_lines.append(line)
            desc = "\n".join(desc_lines)
            caps.append({"key": key, "title": title, "description": desc})
        return web.json_response({"capabilities": caps})
    except Exception as e:
        logger.exception("failed to load capabilities")
        return web.json_response({"error": str(e)}, status=500)


def _gateway_disabled_resp() -> web.Response | None:
    """Rollback flag (ТЗ-1 §8.1): gateway off → menu endpoints answer 404."""
    try:
        from ...gateway.screens import is_gateway_enabled
        if not is_gateway_enabled():
            return web.json_response({"error": "gateway_disabled"}, status=404)
    except Exception:  # noqa: BLE001
        logger.exception("gateway flag check failed")
        return web.json_response({"error": "gateway_unavailable"}, status=404)
    return None


async def _menu_get(request: web.Request) -> web.Response:
    """ТЗ-1 §4.7: GET /menu?screen=<id> → 200 {"screen": {...}} | 404.

    Fable5-токен = полный owner-доступ, поэтому экраны строятся от
    имени владельца (все кнопки видны).
    """
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    if (resp := _gateway_disabled_resp()) is not None:
        return resp
    from ...gateway import screens as _screens  # noqa: F401 (registers builders)
    from ...gateway.adapters.web import screen_json_for

    screen_id = request.query.get("screen", "main")
    owner_id = storage.get_owner_id()
    payload = screen_json_for(screen_id, owner_id)
    if payload is None:
        return web.json_response({"error": "unknown_screen"}, status=404)
    return web.json_response({"screen": payload})


async def _menu_action(request: web.Request) -> web.Response:
    """ТЗ-1 §4.7: POST /action {"action": "..."} — как нажатие кнопки в TG.

    Контракт ответов (строго по ТЗ):
      200 {"screen": {...}}                        — новый экран
      200 {"text": "Готово"}                       — текстовый результат
      403 {"error": "forbidden", "text": "..."}    — гейт не пустил
      409 {"error": "tg_only", "deeplink": "..."}  — экран ещё не мигрирован
    """
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    if (resp := _gateway_disabled_resp()) is not None:
        return resp
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    action = (payload or {}).get("action", "") if isinstance(payload, dict) else ""
    if not action or not isinstance(action, str):
        return web.json_response({"error": "action required"}, status=400)

    from ...gateway import screens as _screens  # noqa: F401
    from ...gateway.adapters.web import WebAdapter
    from ...gateway.dispatch import GatewayContext, dispatch_action

    owner_id = storage.get_owner_id()
    ctx = GatewayContext(
        platform="web",
        external_id=f"fable5:{request.match_info['token'][:8]}",
        user_id=owner_id,
    )
    result = await dispatch_action(action, ctx)
    if result.error == "tg_only":
        return web.json_response(
            {"error": "tg_only", "deeplink": result.deeplink}, status=409
        )
    if result.error:
        body = {"error": result.error}
        if result.text:
            body["text"] = result.text
        status = 403 if result.error == "forbidden" else 400
        return web.json_response(body, status=status)
    body = {}
    if result.screen is not None:
        body["screen"] = WebAdapter().render(result.screen, owner_id)
    if result.text:
        body["text"] = result.text
    if not body:
        body["text"] = "Готово"
    return web.json_response(body)


def register_http_endpoints(app: web.Application) -> None:
    """Attach Fable5 routes to the bot's aiohttp app.

    Call once at startup in both polling and webhook modes. The bot
    instance is stashed in ``app[APP_BOT_KEY]`` separately once the
    Telegram client is built — handlers tolerate it being absent and
    return ``503`` for the few endpoints that need the bot (currently
    only ``/send``).
    """
    # Dashboard (welcome page) — root of the Fable5 URL
    app.router.add_get("/fable5/{token}/", _dashboard)
    app.router.add_get("/fable5/{token}/dashboard", _dashboard)
    # Original API endpoints
    app.router.add_get("/fable5/{token}/ping", _ping)
    app.router.add_get("/fable5/{token}/info", _info)
    app.router.add_get("/fable5/{token}/state", _state_get)
    app.router.add_post("/fable5/{token}/state", _state_post)
    app.router.add_post("/fable5/{token}/exec", _exec)
    app.router.add_post("/fable5/{token}/send", _send)
    app.router.add_get("/fable5/{token}/logs", _logs)
    # New endpoints (v2)
    app.router.add_post("/fable5/{token}/db", _db_query)
    app.router.add_get("/fable5/{token}/files", _files)
    # New endpoints (v2)
    app.router.add_post("/fable5/{token}/db", _db_query)
    app.router.add_get("/fable5/{token}/files", _files)
    app.router.add_get("/fable5/{token}/capabilities", _capabilities)
    # ТЗ-1 (Этап 1): нейтральное меню бота — те же экраны, что и в TG.
    app.router.add_get("/fable5/{token}/menu", _menu_get)
    app.router.add_post("/fable5/{token}/action", _menu_action)
    logger.info("fable5 endpoints registered on aiohttp app (v2 + dashboard + menu)")

