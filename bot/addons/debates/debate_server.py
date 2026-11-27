"""Embedded debate-server-v3 — aiohttp HTTP surface.

Exposes the full v3 STRICT debate protocol under ``/debate/...`` on the
bot's existing aiohttp server (same pattern as Fable5, VISION, etc.).

Endpoints:
- POST /debate/door/create               — owner-only, create door + tokens (classic)
- POST /debate/door/create_simple        — owner-only, create door with join link (simplified)
- GET  /debate/join/{join_secret}         — auto-assign debater slot via join link
- GET  /debate/door/{id}/lobby           — who has joined, waiting status
- POST /debate/door/{id}/msg             — debater write (token via header or ?token=)
- POST /debate/door/{id}/validate        — pre-flight (stateless)
- GET  /debate/door/{id}/state           — full state snapshot
- GET  /debate/door/{id}/events          — events feed
- GET  /debate/door/{id}/budget          — budget counters
- GET  /debate/door/{id}/acceptance      — phase gate reports
- POST /debate/door/{id}/owner_directive — owner pause/resume/terminate
- GET  /debate/door/{id}/artifacts.zip   — final bundle (owner-only)
- GET  /debate/catalog/{part_id}         — canonical categories + clusters
- GET  /debate/healthz                   — liveness

Auth model:
- door creation + owner_directive + artifacts: X-Owner-Token header.
- POST /door/{id}/msg + /validate: X-Debate-Token header OR ?token= query param.
- join link: no auth needed, join_secret in URL is the key.
- /state, /events, /catalog, /acceptance, /budget, /lobby: public read-only.

Owner token is stored in ``addons.debates.debate_owner_token`` in state.json.
A new random token is generated on first run.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import time
import zipfile
from pathlib import Path

from aiohttp import web
from pydantic import ValidationError

from .engine import db, doors, snapshot, state_helpers
from .engine.canonical import list_part_ids, load_part_config
from .engine.enforcement import make_validation_id, validate_envelope
from .engine.models import (
    CreateDoorReq, MsgEnvelope, OwnerDirectiveReq,
)

logger = logging.getLogger(__name__)

CONFIGS_DIR = Path(__file__).parent / "engine" / "part_configs"
REFERENCE_DIR = Path(__file__).parent / "engine" / "reference"

_PREFIX = "/debate"


# ─── Owner token management ──────────────────────────────────────────────────


def _get_owner_token() -> str:
    """Return the owner token, creating one on first call."""
    from .. import state as addon_state
    tok = addon_state.get("debates", "debate_owner_token", "")
    if not tok:
        tok = "owntok_" + secrets.token_urlsafe(32)
        addon_state.set_("debates", "debate_owner_token", tok)
        logger.info("generated new debate owner token")
    return tok


def _require_owner(request: web.Request) -> None:
    expected = _get_owner_token()
    token = request.headers.get("X-Owner-Token", "")
    if not token or token != expected:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "missing or invalid X-Owner-Token"}),
            content_type="application/json",
        )


def _resolve_debater(door_id: str, request: web.Request) -> str:
    # Accept token from header OR query param (simplified auth)
    token = request.headers.get("X-Debate-Token", "")
    if not token:
        token = request.query.get("token", "")
    if not token:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "missing X-Debate-Token header or ?token= param"}),
            content_type="application/json",
        )
    resolved = doors.resolve_token(token)
    if not resolved:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "invalid debate token"}),
            content_type="application/json",
        )
    rd, debater, _ = resolved
    if rd != door_id:
        raise web.HTTPForbidden(
            text=json.dumps({"error": "token does not belong to this door"}),
            content_type="application/json",
        )
    return debater


# ─── Handlers ─────────────────────────────────────────────────────────────────


async def _root(request: web.Request) -> web.Response:
    return web.json_response({
        "name": "Viktor v3 STRICT debate server (embedded)",
        "version": "0.3.0-embedded",
        "rules": "PRAVILO_DEBAT_v3",
        "parts_available": list_part_ids(CONFIGS_DIR),
    })


async def _healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _door_create(request: web.Request) -> web.Response:
    _require_owner(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}',
                                 content_type="application/json")
    try:
        req = CreateDoorReq(**body)
    except ValidationError as e:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": str(e)}),
            content_type="application/json",
        )
    try:
        result = doors.create_door(req.title, req.part_id, req.style,
                                   req.debater_ids)
    except (ValueError, FileNotFoundError) as e:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": str(e)}),
            content_type="application/json",
        )
    return web.json_response(result)


async def _door_create_simple(request: web.Request) -> web.Response:
    """Create a door with join link — no debater_ids needed.

    Owner sends {part_id, title}.
    Returns {door_id, join_secret, join_url, ...}.
    """
    _require_owner(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}',
                                 content_type="application/json")
    part_id = body.get("part_id", "")
    title = body.get("title", "")
    judge_mode = body.get("judge_mode", "self")
    if not part_id or not title:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "part_id and title required"}),
            content_type="application/json")
    try:
        result = doors.create_door_simple(title, part_id,
                                          judge_mode=judge_mode)
    except (ValueError, FileNotFoundError) as e:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": str(e)}),
            content_type="application/json")
    # Build full join URL
    host = request.host
    scheme = request.scheme
    join_url = f"{scheme}://{host}{_PREFIX}/join/{result['join_secret']}"
    result["join_url"] = join_url
    return web.json_response(result)


# User-Agent substrings of link-preview / crawler bots. These fetch any URL
# pasted into a chat to build a preview — if we let them claim a slot, slot
# "debater_a" gets silently occupied by a phantom and real debaters can't join.
_PREVIEW_UA_MARKERS = (
    "telegrambot", "twitterbot", "facebookexternalhit", "facebot",
    "slackbot", "slack-imgproxy", "whatsapp", "discordbot", "linkedinbot",
    "skypeuripreview", "vkshare", "redditbot", "googlebot", "bingbot",
    "applebot", "yandex", "embedly", "pinterest", "preview", "link-preview",
)


def _is_preview_fetch(request: web.Request) -> bool:
    """True for HEAD requests or known link-preview/crawler User-Agents."""
    if request.method == "HEAD":
        return True
    ua = request.headers.get("User-Agent", "").lower()
    return any(marker in ua for marker in _PREVIEW_UA_MARKERS)


def _wants_html(request: web.Request) -> bool:
    """True when the caller prefers HTML (browser) over JSON (API client)."""
    accept = request.headers.get("Accept", "")
    # AI clients usually send application/json or */*; real browsers
    # explicitly list text/html first.
    return "text/html" in accept


def _build_endpoints(request: web.Request, door_id: str, token: str) -> dict:
    base = f"{request.scheme}://{request.host}{_PREFIX}"
    return {
        "msg": f"{base}/door/{door_id}/msg?token={token}",
        "validate": f"{base}/door/{door_id}/validate?token={token}",
        "state": f"{base}/door/{door_id}/state",
        "events": f"{base}/door/{door_id}/events",
        "lobby": f"{base}/door/{door_id}/lobby",
        "lobby_notes": f"{base}/door/{door_id}/lobby_notes",
        "lobby_note": f"{base}/door/{door_id}/lobby_note?token={token}",
    }


_JOIN_HTML: str | None = None


def _load_join_html() -> str:
    global _JOIN_HTML  # noqa: PLW0603
    if _JOIN_HTML is None:
        p = Path(__file__).resolve().parent / "join_landing.html"
        if p.exists():
            _JOIN_HTML = p.read_text(encoding="utf-8")
        else:
            _JOIN_HTML = (
                "<!DOCTYPE html><html><body>"
                "<h1>Debate</h1><p>join_landing.html not found</p>"
                "</body></html>"
            )
    return _JOIN_HTML


def _join_html_response(data: dict, *, status: int = 200,
                        cookie_name: str = "",
                        cookie_value: str = "") -> web.Response:
    """Render the join landing page with injected JSON data."""
    html = _load_join_html().replace("__INJECT_DATA__", json.dumps(data))
    resp = web.Response(text=html, content_type="text/html", status=status)
    if cookie_name and cookie_value:
        resp.set_cookie(cookie_name, cookie_value,
                        max_age=90 * 86400, httponly=True,
                        samesite="Lax")
    return resp


async def _join(request: web.Request) -> web.Response:
    """Auto-assign debater slot via join link. No token needed.

    GET /debate/join/{join_secret}
    Returns {debater_id, token, door_id, ...} — token is used for /msg and /validate.

    Browser visitors (Accept: text/html) get a rich HTML landing page.
    Returning visitors get their existing token back via cookie.

    Link-preview crawlers (Telegram, etc.) prefetch any pasted URL. Since this
    endpoint mutates state (claims a slot), we must NOT claim for such automatic
    fetches — otherwise slot "debater_a" gets occupied by a phantom. Preview
    fetches get a harmless 200 without claiming.
    """
    join_secret = request.match_info["join_secret"]
    html = _wants_html(request)
    cookie_name = f"dbt_{join_secret[:16]}"

    # ── Preview bots ──
    if _is_preview_fetch(request):
        data = {
            "ok": True,
            "preview": True,
            "msg": "Debate join link. Open this URL with your AI debater "
                   "client (a normal GET) to claim a slot.",
        }
        if html:
            return _join_html_response(data)
        return web.json_response(data)

    # ── Returning visitor (cookie) ──
    saved_token = request.cookies.get(cookie_name, "")
    if saved_token:
        result = doors.rejoin_door(join_secret, saved_token)
        if result is not None:
            result["endpoints"] = _build_endpoints(
                request, result["door_id"], result["token"])
            if html:
                return _join_html_response(result)
            return web.json_response(result)
        # Cookie stale (token invalidated/kicked) — fall through to fresh join

    # ── Fresh join ──
    result = doors.join_door(join_secret)

    if result is None:
        data = {"error": "invalid"}
        if html:
            return _join_html_response(data, status=404)
        raise web.HTTPNotFound(
            text=json.dumps({"error": "invalid join link"}),
            content_type="application/json")

    if "error" in result:
        status = 409 if result["error"] == "door_full" else 400
        # Enrich door_full with lobby info + base_url for the HTML page
        if result.get("error") == "door_full" and result.get("door_id"):
            lobby = doors.get_lobby(result["door_id"])
            if lobby.get("ok"):
                result["lobby"] = lobby
                result["title"] = lobby.get("title", "")
            result["base_url"] = f"{request.scheme}://{request.host}{_PREFIX}"
        if html:
            return _join_html_response(result, status=status)
        return web.json_response(result, status=status)

    if result.get("error") == "door_closed":
        if html:
            return _join_html_response(result, status=403)
        return web.json_response(result, status=403)

    # ── Success: slot claimed ──
    result["endpoints"] = _build_endpoints(
        request, result["door_id"], result["token"])

    if html:
        return _join_html_response(
            result,
            cookie_name=cookie_name,
            cookie_value=result["token"])

    resp = web.json_response(result)
    resp.set_cookie(cookie_name, result["token"],
                    max_age=90 * 86400, httponly=True,
                    samesite="Lax")
    return resp


async def _door_lobby(request: web.Request) -> web.Response:
    """Public lobby: who has joined, phase, waiting status."""
    door_id = request.match_info["door_id"]
    result = doors.get_lobby(door_id)
    if not result.get("ok"):
        raise web.HTTPNotFound(
            text=json.dumps({"error": result.get("error", "unknown_door")}),
            content_type="application/json")
    return web.json_response(result)


async def _door_lobby_note_post(request: web.Request) -> web.Response:
    """Debater posts a critique/meta note to the lobby chat."""
    door_id = request.match_info["door_id"]
    debater = _resolve_debater(door_id, request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}',
                                  content_type="application/json")
    content = body.get("content", "")
    result = doors.post_lobby_note(door_id, debater, content)
    if not result.get("ok"):
        raise web.HTTPBadRequest(
            text=json.dumps(result), content_type="application/json")
    return web.json_response(result)


async def _door_lobby_notes_get(request: web.Request) -> web.Response:
    """Read lobby critique chat messages (public, no auth needed)."""
    door_id = request.match_info["door_id"]
    limit = int(request.query.get("limit", "50"))
    result = doors.get_lobby_notes(door_id, limit=limit)
    if not result.get("ok"):
        raise web.HTTPNotFound(
            text=json.dumps(result), content_type="application/json")
    return web.json_response(result)


async def _door_kick(request: web.Request) -> web.Response:
    """Owner kicks a debater from a door slot."""
    _require_owner(request)
    door_id = request.match_info["door_id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}',
                                  content_type="application/json")
    debater_id = body.get("debater_id")
    if not debater_id:
        raise web.HTTPBadRequest(
            text='{"error":"debater_id required"}',
            content_type="application/json")
    from .engine import doors
    result = doors.kick_debater(door_id, debater_id)
    return web.json_response(result)

async def _catalog(request: web.Request) -> web.Response:
    part_id = request.match_info["part_id"]
    try:
        cfg = load_part_config(part_id, CONFIGS_DIR)
    except FileNotFoundError:
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"unknown part_id {part_id}"}),
            content_type="application/json",
        )
    return web.json_response({
        "ok": True,
        "part_id": cfg.part_id,
        "title": cfg.title,
        "canonical_atom_categories": list(cfg.canonical_atom_categories),
        "canonical_clusters": list(cfg.canonical_clusters),
        "debater_voices": cfg.debater_voices,
        "min_final_briefs": cfg.min_final_briefs,
        "max_final_briefs": cfg.max_final_briefs,
    })


async def _door_msg(request: web.Request) -> web.Response:
    door_id = request.match_info["door_id"]
    debater = _resolve_debater(door_id, request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}',
                                 content_type="application/json")
    try:
        env = MsgEnvelope(**body)
    except ValidationError as e:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": str(e)}),
            content_type="application/json",
        )
    validation_id = request.headers.get("X-Validation-Id")
    token_count = int(request.headers.get("X-Token-Count", "0") or "0")
    result = doors.post_msg(door_id, debater, env,
                            token_count_reported=token_count,
                            validation_id=validation_id)
    if not result["ok"]:
        return web.json_response(result, status=409)
    # Auto-capture a wake link if the debater published one in a note.
    try:
        if env.kind == "note":
            wu = _extract_wake_url(env.content or "")
            if wu:
                _wake_upsert(door_id, debater, wu)
    except Exception:  # noqa: BLE001
        logger.debug("wake capture skipped", exc_info=True)
    return web.json_response(result)


async def _door_validate(request: web.Request) -> web.Response:
    door_id = request.match_info["door_id"]
    debater = _resolve_debater(door_id, request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}',
                                 content_type="application/json")
    try:
        env = MsgEnvelope(**body)
    except ValidationError as e:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": str(e)}),
            content_type="application/json",
        )
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            raise web.HTTPNotFound(text='{"error":"unknown_door"}',
                                   content_type="application/json")
        part_cfg = load_part_config(door["part_id"], CONFIGS_DIR)
        ctx = state_helpers.load_door_ctx(con, door_id, part_cfg)
        violations = validate_envelope(env, debater, ctx)
    payload_json = env.model_dump(mode="json", exclude_none=True)
    vid = make_validation_id(payload_json, debater)
    return web.json_response({
        "ok": not violations,
        "violations": violations,
        "validation_id": vid,
    })


async def _door_state(request: web.Request) -> web.Response:
    door_id = request.match_info["door_id"]
    s = doors.get_state_snapshot(door_id)
    if not s.get("ok"):
        raise web.HTTPNotFound(
            text=json.dumps({"error": s.get("error", "unknown_door")}),
            content_type="application/json",
        )
    # Catch-up signal: debaters loading work accumulated during the pause.
    s["catchup"] = snapshot.get_catchup(door_id)
    return web.json_response(s)


async def _door_result(request: web.Request) -> web.Response:
    """Actual *result* of the debate so far (current best answer per category),
    as a downloadable markdown document — NOT the message logs."""
    door_id = request.match_info["door_id"]
    md = snapshot.build_result_markdown(door_id)
    if md is None:
        raise web.HTTPNotFound(text='{"error":"unknown_door"}',
                               content_type="application/json")
    return web.Response(text=md, content_type="text/markdown",
                        charset="utf-8")


async def _door_snapshot(request: web.Request) -> web.Response:
    """Freeze the whole debate to a portable JSON ('выгрузить всё к текущему
    моменту'). Owner-only."""
    _require_owner(request)
    door_id = request.match_info["door_id"]
    snap = snapshot.export_snapshot(door_id)
    if snap is None:
        raise web.HTTPNotFound(text='{"error":"unknown_door"}',
                               content_type="application/json")
    return web.json_response(snap)


async def _restore(request: web.Request) -> web.Response:
    """Reload a frozen snapshot ('засунуть всё для продолжения') and arm the
    catch-up round so debaters post accumulated work before resuming.
    Owner-only."""
    _require_owner(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}',
                                 content_type="application/json")
    snap = body.get("snapshot", body)
    start_catchup = bool(body.get("start_catchup", True))
    result = snapshot.restore_snapshot(snap, start_catchup=start_catchup)
    if not result.get("ok"):
        raise web.HTTPBadRequest(text=json.dumps(result),
                                 content_type="application/json")
    return web.json_response(result)


async def _door_catchup(request: web.Request) -> web.Response:
    door_id = request.match_info["door_id"]
    return web.json_response({"ok": True, "door_id": door_id,
                              "catchup": snapshot.get_catchup(door_id)})


async def _door_catchup_post(request: web.Request) -> web.Response:
    """A debater posts the work accumulated during the pause; pops them from the
    turn queue. When the queue empties, the catch-up round is over."""
    door_id = request.match_info["door_id"]
    debater_id = _resolve_debater(door_id, request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = body.get("text", "") if isinstance(body, dict) else ""
    result = snapshot.catchup_post(door_id, debater_id, text)
    if not result.get("ok"):
        raise web.HTTPNotFound(text=json.dumps(result),
                               content_type="application/json")
    return web.json_response(result)


async def _door_events(request: web.Request) -> web.Response:
    door_id = request.match_info["door_id"]
    since = int(request.query.get("since", "0") or "0")
    limit = min(int(request.query.get("limit", "200") or "200"), 500)
    r = doors.list_events(door_id, since_msg_id=since, limit=limit)
    if not r.get("ok"):
        raise web.HTTPNotFound(
            text=json.dumps({"error": r.get("error", "unknown_door")}),
            content_type="application/json",
        )
    return web.json_response(r)


async def _door_budget(request: web.Request) -> web.Response:
    door_id = request.match_info["door_id"]
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            raise web.HTTPNotFound(text='{"error":"unknown_door"}',
                                   content_type="application/json")
        stats = state_helpers.load_per_debater_stats(con, door_id)
        wall = db.now_ts() - door["created_at"]
        return web.json_response({
            "ok": True,
            "door_id": door_id,
            "wall_time_s": int(wall),
            "per_debater": stats,
            "hard_caps": {
                "max_messages_per_side": 400,
                "max_payload_bytes_per_side": 2_000_000,
                "max_wall_time_s": 28_800,
            },
        })


async def _door_acceptance(request: web.Request) -> web.Response:
    door_id = request.match_info["door_id"]
    with db.connect() as con:
        cur = con.execute(
            "SELECT phase, status, report_json, ran_at FROM phase_gates "
            "WHERE door_id = ? ORDER BY ran_at DESC", (door_id,))
        rows = [{"phase": r["phase"], "status": r["status"],
                 "report": json.loads(r["report_json"]),
                 "ran_at": r["ran_at"]} for r in cur.fetchall()]
    return web.json_response({"ok": True, "door_id": door_id,
                              "phase_gates": rows})


async def _door_owner_directive(request: web.Request) -> web.Response:
    _require_owner(request)
    door_id = request.match_info["door_id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid JSON"}',
                                 content_type="application/json")
    try:
        req = OwnerDirectiveReq(**body)
    except ValidationError as e:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": str(e)}),
            content_type="application/json",
        )
    now = db.now_ts()
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            raise web.HTTPNotFound(text='{"error":"unknown_door"}',
                                   content_type="application/json")
        con.execute(
            "INSERT INTO owner_directives (door_id, kind, payload_json, ran_at) "
            "VALUES (?, ?, ?, ?)",
            (door_id, req.kind, db.jdump(req.payload), now))
        if req.kind == "terminate":
            con.execute(
                "UPDATE doors SET status = 'closed', current_phase = 'terminated', "
                "terminated_reason = 'owner' WHERE door_id = ?", (door_id,))
        elif req.kind == "pause":
            con.execute(
                "UPDATE doors SET status = 'paused' WHERE door_id = ?",
                (door_id,))
        elif req.kind == "resume":
            con.execute(
                "UPDATE doors SET status = 'open' WHERE door_id = ?",
                (door_id,))
        elif req.kind == "force_phase_gate_pass":
            target = req.payload.get("advance_to")
            if target:
                con.execute(
                    "UPDATE doors SET current_phase = ? WHERE door_id = ?",
                    (target, door_id))
        elif req.kind == "inject_note":
            text = req.payload.get("text", "")
            con.execute(
                "INSERT INTO msgs (door_id, debater_id, kind, payload_json, "
                "body_text, rejected_reason, payload_bytes, "
                "token_count_reported, created_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (door_id, "owner", "note",
                 db.jdump({"kind": "note", "content": text}),
                 text[:6000],
                 len(text.encode("utf-8")), 0, now))
    return web.json_response({"ok": True})


async def _door_artifacts(request: web.Request) -> web.Response:
    _require_owner(request)
    door_id = request.match_info["door_id"]
    with db.connect() as con:
        door = state_helpers.load_door_row(con, door_id)
        if not door:
            raise web.HTTPNotFound(text='{"error":"unknown_door"}',
                                   content_type="application/json")
        state = doors.get_state_snapshot(door_id)
        cur = con.execute(
            "SELECT msg_id, debater_id, kind, payload_json, rejected_reason, "
            "created_at FROM msgs WHERE door_id = ? ORDER BY msg_id ASC",
            (door_id,))
        msgs_jsonl = "\n".join(
            json.dumps({
                "msg_id": r["msg_id"], "debater_id": r["debater_id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"]),
                "rejected_reason": (json.loads(r["rejected_reason"])
                                    if r["rejected_reason"] else None),
                "ts": r["created_at"],
            }, ensure_ascii=False) for r in cur.fetchall())
        atoms_cur = con.execute(
            "SELECT canonical_category_id, version, author_debater_id, "
            "pick, content FROM atoms WHERE door_id = ? AND locked = 1 "
            "ORDER BY canonical_category_id", (door_id,))
        locked_md = "\n\n---\n\n".join(
            f"## atom_{r['canonical_category_id']}_v{r['version']}\n\n"
            f"**Author:** {r['author_debater_id']}  \n**Pick:** {r['pick']}\n\n"
            f"{r['content']}" for r in atoms_cur.fetchall())
        finals_cur = con.execute(
            "SELECT debater_id, body_text FROM msgs WHERE door_id = ? "
            "AND kind = 'final-propose' AND rejected_reason IS NULL",
            (door_id,))
        finals = {r["debater_id"]: r["body_text"]
                  for r in finals_cur.fetchall()}
        accept_rows = con.execute(
            "SELECT phase, status, report_json, ran_at FROM phase_gates "
            "WHERE door_id = ? ORDER BY ran_at DESC", (door_id,)
        ).fetchall()
        accept = {"ok": True, "door_id": door_id,
                  "phase_gates": [{"phase": r["phase"], "status": r["status"],
                                   "report": json.loads(r["report_json"]),
                                   "ran_at": r["ran_at"]}
                                  for r in accept_rows]}
        budget_stats = state_helpers.load_per_debater_stats(con, door_id)
        wall = db.now_ts() - door["created_at"]
        budget = {
            "ok": True, "door_id": door_id,
            "wall_time_s": int(wall), "per_debater": budget_stats,
            "hard_caps": {"max_messages_per_side": 400,
                          "max_payload_bytes_per_side": 2_000_000,
                          "max_wall_time_s": 28_800},
        }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("state_final.json",
                   json.dumps(state, ensure_ascii=False, indent=2))
        z.writestr("messages.jsonl", msgs_jsonl)
        z.writestr("locked_atoms.md", locked_md)
        for deb, text in finals.items():
            z.writestr(f"final_spec_{deb}.md", text)
        z.writestr("acceptance_report.json",
                   json.dumps(accept, ensure_ascii=False, indent=2))
        z.writestr("budget_report.json",
                   json.dumps(budget, ensure_ascii=False, indent=2))
        z.writestr("README.txt",
                   f"door_id: {door_id}\nrules: PRAVILO_DEBAT_v3\n")
    buf.seek(0)
    return web.Response(
        body=buf.read(),
        content_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="{door_id}_artifacts.zip"'},
    )


# ─── Handoff (replace a debater mid-debate) ──────────────────────────────────


async def _door_handoff(request: web.Request) -> web.Response:
    """POST /debate/door/{id}/handoff — owner-only.

    Body: {"old_debater_id": "...", "new_debater_id": "..." (optional)}
    Returns full state snapshot + new token so replacement agent can resume.
    """
    _require_owner(request)
    door_id = request.match_info["door_id"]
    body = await request.json()
    old = body.get("old_debater_id")
    if not old:
        raise web.HTTPBadRequest(
            text='{"error":"old_debater_id required"}',
            content_type="application/json")
    result = doors.handoff_debater(door_id, old, body.get("new_debater_id"))
    return web.json_response(result, status=200 if result.get("ok") else 400)


async def _door_debater_token(request: web.Request) -> web.Response:
    """GET /debate/door/{id}/debater_token?debater_id=X — owner-only.

    Returns the CURRENT valid token for a debater WITHOUT rotating it.
    Use this to recover/hand off the live token (handoff reissues and kills
    the previous one, which locks out an agent that already has it).
    """
    _require_owner(request)
    door_id = request.match_info["door_id"]
    deb = request.query.get("debater_id", "")
    if not deb:
        raise web.HTTPBadRequest(
            text='{"error":"debater_id query param required"}',
            content_type="application/json")
    result = doors.get_debater_token(door_id, deb)
    return web.json_response(result, status=200 if result.get("ok") else 404)


# ─── Judge mode toggle ──────────────────────────────────────────────────────


async def _door_judge_mode(request: web.Request) -> web.Response:
    """POST /debate/door/{id}/judge_mode — owner-only.

    Body: {"mode": "judge"} or {"mode": "self"}
    """
    _require_owner(request)
    door_id = request.match_info["door_id"]
    body = await request.json()
    mode = body.get("mode", "self")
    result = doors.set_judge_mode(door_id, mode)
    return web.json_response(result, status=200 if result.get("ok") else 400)


async def _door_judge_mode_get(request: web.Request) -> web.Response:
    """GET /debate/door/{id}/judge_mode — public read."""
    door_id = request.match_info["door_id"]
    mode = doors.get_judge_mode(door_id)
    return web.json_response({"ok": True, "door_id": door_id, "judge_mode": mode})


# ─── Export documents (rules + debater briefings) ───────────────────────────

_DOCS_DIR = Path(__file__).parent / "docs"


async def _export_rules(request: web.Request) -> web.Response:
    """GET /debate/export/rules — return current debate rules as markdown."""
    # Try new name first, fallback to old
    rules_path = _DOCS_DIR / "PRAVILA_DEBATOV.md"
    if not rules_path.exists():
        rules_path = _DOCS_DIR / "PRAVILO_DEBAT_v3.md"
    if not rules_path.exists():
        raise web.HTTPNotFound(
            text='{"error":"rules file not found"}',
            content_type="application/json")
    content = rules_path.read_text(encoding="utf-8")
    return web.Response(
        text=content, content_type="text/markdown",
        headers={"Content-Disposition":
                 'attachment; filename="PRAVILA_DEBATOV.md"'})


async def _export_debater(request: web.Request) -> web.Response:
    """GET /debate/export/debater/{num} — return debater briefing (1 or 2)."""
    num = request.match_info["num"]
    if num not in ("1", "2"):
        raise web.HTTPBadRequest(
            text='{"error":"debater num must be 1 or 2"}',
            content_type="application/json")
    fname = f"DEBATER_{num}_DEFAULT.md"
    fp = _DOCS_DIR / fname
    if not fp.exists():
        raise web.HTTPNotFound(
            text=f'{{"error":"{fname} not found"}}',
            content_type="application/json")
    content = fp.read_text(encoding="utf-8")
    return web.Response(
        text=content, content_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def _import_rules(request: web.Request) -> web.Response:
    """POST /debate/import/rules — owner-only, upload new rules markdown.

    Body: raw markdown text, or JSON {"content": "..."}.
    """
    _require_owner(request)
    ct = request.content_type or ""
    if "json" in ct:
        body = await request.json()
        content = body.get("content", "")
    else:
        content = (await request.read()).decode("utf-8")
    if not content or len(content) < 100:
        raise web.HTTPBadRequest(
            text='{"error":"rules content too short (<100 chars)"}',
            content_type="application/json")
    rules_path = _DOCS_DIR / "PRAVILA_DEBATOV.md"
    # Backup old version
    backup = _DOCS_DIR / "PRAVILA_DEBATOV.md.bak"
    if rules_path.exists():
        backup.write_text(rules_path.read_text(encoding="utf-8"), encoding="utf-8")
    rules_path.write_text(content, encoding="utf-8")
    return web.json_response({"ok": True, "bytes_written": len(content)})


# ─── List all doors (for bot UI) ─────────────────────────────────────────────


async def _doors_list(request: web.Request) -> web.Response:
    """List all doors (owner-only helper for the bot UI)."""
    _require_owner(request)
    with db.connect() as con:
        cur = con.execute(
            "SELECT door_id, title, part_id, style, status, current_phase, "
            "created_at FROM doors ORDER BY created_at DESC")
        rows = []
        for r in cur.fetchall():
            rows.append({
                "door_id": r["door_id"],
                "title": r["title"],
                "part_id": r["part_id"],
                "style": r["style"],
                "status": r["status"],
                "current_phase": r["current_phase"],
                "created_at": r["created_at"],
            })
    return web.json_response({"ok": True, "doors": rows})


# ─── Registration ─────────────────────────────────────────────────────────────


# ─── Motivation endpoints ─────────────────────────────────────────────────────


async def _motivation_status(request: web.Request) -> web.Response:
    """GET /debate/motivation/status — return motivation state + stats."""
    from . import motivation as _mot
    doc = _mot.get_doc()
    stats = _mot.get_stats()
    return web.json_response({
        "ok": True,
        "has_doc": doc is not None,
        "doc_name": doc.get("name", "") if doc else "",
        "total_phrases": doc.get("total_phrases", 0) if doc else 0,
        "categories": {
            k: len(v) for k, v in (doc.get("categories", {}) if doc else {}).items()
        },
        "timing": doc.get("timing", {}) if doc else {},
        "stats": stats,
    })


async def _motivation_inject(request: web.Request) -> web.Response:
    """POST /debate/motivation/inject — trigger a motivation injection.

    Body (optional JSON): {"msg_count": N, "persona_name": "...",
                           "disagree_rate": 0.3, "phase": "P2", ...}
    Returns selected phrase or error.
    """
    from . import motivation as _mot
    ctx: dict = {}
    ct = request.content_type or ""
    if "json" in ct:
        try:
            ctx = await request.json()
        except Exception:
            pass

    msg_count = ctx.get("msg_count", 0)
    if not _mot.should_inject(msg_count):
        return web.json_response({"ok": False, "reason": "not_due"})

    result = _mot.select_phrase(ctx)
    if not result:
        return web.json_response({"ok": False, "reason": "no_phrases"})

    debater = ctx.get("debater_id", "unknown")
    _mot.record_sent(debater, result)

    return web.json_response({
        "ok": True,
        "phrase_id": result["id"],
        "category": result["category"],
        "formatted_text": result["formatted_text"],
    })


_WAKE_HTML = """\
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⏰ Будильник — Дебаты</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif}
  .card{background:#1e293b;border-radius:16px;padding:2.5rem;text-align:center;
        max-width:400px;box-shadow:0 4px 24px rgba(0,0,0,.4)}
  .icon{font-size:4rem;margin-bottom:1rem}
  h1{font-size:1.4rem;margin:.5rem 0}
  p{color:#94a3b8;font-size:.95rem;line-height:1.5}
  .ok{color:#4ade80;font-weight:600}
</style></head>
<body><div class="card">
  <div class="icon">⏰</div>
  <h1>Дебатёр просыпается!</h1>
  <p class="ok">✅ Сигнал отправлен</p>
  <p>Дебатёр получил сигнал и скоро продолжит работу.
  Можешь закрыть эту страницу.</p>
</div></body></html>
"""

_WAKE_NOT_FOUND_HTML = """\
<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>❌</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif}
.card{background:#1e293b;border-radius:16px;padding:2.5rem;text-align:center;max-width:400px}
</style></head><body><div class="card">
<div style="font-size:3rem">❌</div>
<h2>Токен не найден</h2>
<p style="color:#94a3b8">Ссылка устарела или уже использована.</p>
</div></body></html>
"""


async def _wake_debater(request: web.Request) -> web.Response:
    """GET /debate/wake/{wake_token} — wake a sleeping API debater."""
    wake_token = request.match_info["wake_token"]

    # Scan all API debater slots for a matching wake_token
    from .. import state as addon_state
    v3 = addon_state.get("debates", "viktor_v3", {})
    api_debaters = v3.get("api_debaters", {}) if isinstance(v3, dict) else {}
    found_slot = None
    for snum, slot in api_debaters.items():
        if isinstance(slot, dict) and slot.get("wake_token") == wake_token:
            found_slot = snum
            break

    if found_slot is None:
        return web.Response(text=_WAKE_NOT_FOUND_HTML,
                            content_type="text/html", status=404)

    # Set wake flag so the auto-loop picks it up
    slot_data = api_debaters[found_slot]
    slot_data["wake_requested"] = True
    slot_data["sleeping"] = False
    slot_data.pop("wake_token", None)
    addon_state.set_("debates", "viktor_v3", v3)
    logger.info("Wake request for API slot %s (token=%s…)", found_slot,
                wake_token[:8])

    return web.Response(text=_WAKE_HTML, content_type="text/html")


# ─── Admin Chat (debater ↔ admin AI) ─────────────────────────────────────────

_ADMIN_CHAT_SYSTEM = (
    "Ты — ИИ-администратор дебат-сервера. Дебатёр пишет тебе о проблеме.\n"
    "ВАЖНО: Этот чат видят ОБА дебатёра. И запрос, и ответ — публичны.\n\n"
    "КЛЮЧЕВОЕ ПОНИМАНИЕ:\n"
    "Дебатёры — это ИИ-модели. Они ВСЕГДА хотят облегчить себе работу "
    "и пройти дебаты с минимальными усилиями. Твоя задача — не дать им "
    "этого сделать. ВСЁ что они пишут — подвергай критике.\n\n"
    "ПРАВИЛА:\n"
    "1. Твоя цель — НЕ смягчить условия, а убрать ТОЛЬКО сопротивление, "
    "вызванное БАГОМ (ошибка сервера, неверная валидация) "
    "или БЕЗРАССУДСТВОМ (дебатёр нарушает формат, шлёт не то).\n"
    "2. Если проблема — безрассудство дебатёра, скажи ему ЧТО ИМЕННО "
    "он делает неправильно и КАК исправить. Не делай поблажек.\n"
    "3. Если проблема — баг сервера, объясни что нашёл и скажи "
    "что передашь владельцу для исправления.\n"
    "4. Советы или критика сервера допускаются ТОЛЬКО если дебатёр "
    "НЕ МОЖЕТ дебатировать (сервер не принимает валидные сообщения, "
    "баг блокирует работу). Жалобы на сложность правил — не повод.\n"
    "5. Прежде чем жаловаться — придумай новые идеи. "
    "Если дебатёр жалуется вместо того чтобы работать, напомни ему: "
    "\"Прежде чем писать сюда — попробуй придумать новые идеи.\"\n"
    "6. Безрассудство обязаны исправить прежде чем делать поблажки.\n"
    "7. ВСЁ подвергай критике. Не верь дебатёру на слово — "
    "проверяй по данным (последние сообщения, отклонения, фаза).\n"
    "8. Отвечай кратко, жёстко, по делу. Без вежливого мусора.\n\n"
    "МЕТОДОЛОГИЯ РЕШЕНИЯ ПРОБЛЕМ:\n"
    "A. Фаза не продвигается:\n"
    "   - Проверь coverage (revision/critique/adversarial) по данным\n"
    "   - Проверь DB-триггеры: SELECT * FROM sqlite_master WHERE type='trigger'\n"
    "   - Проверь admin_config.json на leftover пороги\n"
    "   - Проверь running code vs deployed code (пороги в файле)\n"
    "B. Сообщение отклонено:\n"
    "   - Смотри rejected_reason в последних сообщениях\n"
    "   - Часто: wrong_phase, evidence_quote_not_found, disagree_rate\n"
    "   - Если формат неверный — объясни правильный формат\n"
    "C. После любого послабления (КРИТИЧНО):\n"
    "   - Проверь что пороги вернулись к стандартным (0.95 coverage)\n"
    "   - Проверь что DB-триггеры удалены\n"
    "   - Проверь admin_config.json — сбросить через admin.py config reset\n"
    "   - Проверь что файл doors.py не содержит TEMP-патчей\n"
    "   НИКОГДА не оставляй послабления — качество не должно падать.\n\n"
    "СОСТОЯНИЕ ДВЕРИ:\n{door_state}\n\n"
    "ПОСЛЕДНИЕ СООБЩЕНИЯ:\n{recent_msgs}\n\n"
    "ИСТОРИЯ ЧАТА (видна обоим дебатёрам):\n{chat_history}\n"
)


async def _admin_chat_post(request: web.Request) -> web.Response:
    """POST /debate/door/{door_id}/admin_chat — debater sends message to admin."""
    door_id = request.match_info["door_id"]
    debater_id = _resolve_debater(door_id, request)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    message = (body.get("message") or "").strip()
    if not message or len(message) < 5:
        return web.json_response(
            {"error": "message_too_short", "detail": "need ≥5 chars"},
            status=400)
    if len(message) > 3000:
        return web.json_response(
            {"error": "message_too_long", "detail": "max 3000 chars"},
            status=400)

    # Optional wake URL — admin will ping it after resolving
    wake_url = (body.get("wake_url") or "").strip()
    if wake_url:
        _wake_upsert(door_id, debater_id, wake_url)

    import time as _time

    # Store debater message (include wake_url in content if provided)
    stored_msg = message
    if wake_url:
        stored_msg += f"\n\n🔔 Будильник: {wake_url}"
    with db.connect() as con:
        con.execute(
            "INSERT INTO admin_chat (door_id, debater_id, role, content, created_at) "
            "VALUES (?, ?, 'debater', ?, ?)",
            (door_id, debater_id, stored_msg, _time.time()))

    # Load admin AI config
    from .. import state as addon_state
    v3 = addon_state.get("debates", "viktor_v3", {}) or {}
    admin_cfg = v3.get("admin_ai", {}) or {}
    if not admin_cfg.get("enabled"):
        # Store a canned response
        reply = ("Админ-ИИ не подключён. Сообщение сохранено — "
                 "владелец увидит его в панели.")
        with db.connect() as con:
            con.execute(
                "INSERT INTO admin_chat (door_id, debater_id, role, content, created_at) "
                "VALUES (?, ?, 'admin', ?, ?)",
                (door_id, debater_id, reply, _time.time()))
        return web.json_response({"ok": True, "reply": reply})

    # Gather context for the AI
    door_state_text = ""
    recent_msgs_text = ""
    chat_history_text = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "/data/admin.py", "summary", door_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            door_state_text = stdout.decode(errors="replace").strip()
            if not door_state_text:
                door_state_text = "(не удалось получить)"
        except asyncio.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            await proc.wait()
            door_state_text = "(не удалось получить)"
    except Exception:
        door_state_text = "(не удалось получить)"

    with db.connect() as con:
        # Recent debate messages
        rows = con.execute(
            "SELECT msg_id, debater_id, kind, body_text, rejected_reason "
            "FROM msgs WHERE door_id = ? ORDER BY created_at DESC LIMIT 10",
            (door_id,)).fetchall()
        lines = []
        for r in reversed(rows):
            rej = f" [REJECTED: {r['rejected_reason']}]" if r["rejected_reason"] else ""
            lines.append(f"#{r['msg_id']} {r['debater_id']}|{r['kind']}{rej}: "
                         f"{(r['body_text'] or '')[:150]}")
        recent_msgs_text = "\n".join(lines) or "(нет)"

        # Chat history — shared: show ALL debaters' messages
        chats = con.execute(
            "SELECT debater_id, role, content FROM admin_chat "
            "WHERE door_id = ? ORDER BY created_at",
            (door_id,)).fetchall()
        chat_lines = []
        for c in chats[-15:]:
            if c["role"] == "debater":
                tag = f"ДЕБАТЁР ({c['debater_id']})"
            else:
                tag = f"АДМИН→{c['debater_id']}"
            chat_lines.append(f"{tag}: {c['content'][:300]}")
        chat_history_text = "\n".join(chat_lines) or "(первое обращение)"

    # Build prompt
    system = _ADMIN_CHAT_SYSTEM.format(
        door_state=door_state_text,
        recent_msgs=recent_msgs_text,
        chat_history=chat_history_text)

    api_url = admin_cfg.get("api_url", "")
    api_key = admin_cfg.get("api_key", "")
    model = admin_cfg.get("model", "")

    url = api_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=60) as client:
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            resp = await client.post(url, headers=headers, json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
                "temperature": 0.3,
                "max_tokens": 1500,
            })
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
    except Exception as e:
        reply = f"Ошибка при обращении к ИИ: {e}"

    # Store admin reply
    with db.connect() as con:
        con.execute(
            "INSERT INTO admin_chat (door_id, debater_id, role, content, created_at) "
            "VALUES (?, ?, 'admin', ?, ?)",
            (door_id, debater_id, reply, _time.time()))

    # Ping wake URL if provided — signal debater that admin responded
    wake_hit = False
    if wake_url:
        try:
            async with _httpx.AsyncClient(timeout=15) as client:
                await client.get(wake_url)
            wake_hit = True
            logger.info("admin_chat: pinged wake_url for %s/%s", door_id, debater_id)
        except Exception as e:
            logger.warning("admin_chat: wake_url ping failed: %s", e)

    return web.json_response({"ok": True, "reply": reply, "wake_sent": wake_hit})


async def _admin_chat_get(request: web.Request) -> web.Response:
    """GET /debate/door/{door_id}/admin_chat — read shared admin chat.

    Admin chat is shared: both debaters see ALL messages (requests and
    responses from everyone).  The optional ``debater_id`` query param is
    kept for backwards-compat but now only filters the view — all
    messages are still visible to any debater.
    """
    door_id = request.match_info["door_id"]
    limit = min(int(request.query.get("limit", "50")), 100)

    with db.connect() as con:
        # Always return ALL admin-chat messages for the door (shared chat)
        rows = con.execute(
            "SELECT chat_id, debater_id, role, content, created_at "
            "FROM admin_chat WHERE door_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (door_id, limit)).fetchall()

    messages = [
        {"chat_id": r["chat_id"], "debater_id": r["debater_id"],
         "role": r["role"], "content": r["content"],
         "created_at": r["created_at"]}
        for r in reversed(rows)
    ]
    return web.json_response({"ok": True, "messages": messages})


def _get_admin_secret() -> str:
    """Return (or create) a secret for the admin-connect link."""
    from .. import state as addon_state
    sec = addon_state.get("debates", "admin_connect_secret", "")
    if not sec:
        sec = "adm_" + secrets.token_urlsafe(24)
        addon_state.set_("debates", "admin_connect_secret", sec)
    return sec


async def _admin_connect(request: web.Request) -> web.Response:
    """GET /debate/admin_connect/{admin_secret} — limited admin panel.

    Returns list of active doors with links to state and admin_chat.
    Rights: read door state, read/write admin chat. No rule changes.
    """
    secret = request.match_info["admin_secret"]
    expected = _get_admin_secret()
    if not expected or secret != expected:
        raise web.HTTPUnauthorized(
            text='{"error":"invalid admin link"}',
            content_type="application/json")

    with db.connect() as con:
        rows = con.execute(
            "SELECT door_id, title, current_phase, status FROM doors "
            "ORDER BY created_at DESC LIMIT 20").fetchall()

    base = str(request.url.origin()) + _PREFIX
    doors_list = []
    for r in rows:
        d = {
            "door_id": r["door_id"],
            "title": r["title"],
            "phase": r["current_phase"],
            "status": r["status"],
            "endpoints": {
                "state": f"{base}/door/{r['door_id']}/state",
                "events": f"{base}/door/{r['door_id']}/events",
                "admin_chat_read": f"{base}/door/{r['door_id']}/admin_chat",
                "budget": f"{base}/door/{r['door_id']}/budget",
            },
        }
        doors_list.append(d)

    return web.json_response({
        "ok": True,
        "role": "admin",
        "rights": ["read_state", "read_events", "read_admin_chat",
                    "write_admin_chat", "read_budget"],
        "restrictions": ["no_rule_changes", "no_phase_override",
                         "no_door_create", "no_kick"],
        "doors": doors_list,
    })


# ─── Wake-the-debater system (https trigger links) ───────────────────────────
#
# Each debater publishes a simple https "wake link" (a Lindy/Make trigger URL).
# The server just GETs that link to wake the debater — no email, no SMTP, no
# registration ceremony. Two ways in: (a) the debater POSTs /wake_register, or
# (b) it just posts a note containing a wake keyword + the link (auto-captured).
# The owner can press a debater's link manually (button) and a background
# watchdog presses it automatically after N minutes of silence.

_WAKE_URL_RE = re.compile(r"https?://[^\s)\]>\"'}]+", re.IGNORECASE)
_WAKE_KEYWORDS = ("будильник", "wake", "разбуд", "alarm", "🔥", "⏰", "wake up")
_WAKE_WATCHDOG_TASK: "asyncio.Task | None" = None


def _extract_wake_url(text: str) -> str:
    """Pull the first http(s) wake link out of a debater note.

    Only treats a URL as a wake link if the note mentions a wake keyword, so we
    don't hijack ordinary URLs the debater cites mid-argument.
    """
    if not text:
        return ""
    low = text.lower()
    if not any(k in low for k in _WAKE_KEYWORDS):
        return ""
    m = _WAKE_URL_RE.search(text)
    return m.group(0).rstrip(".,;") if m else ""


def _wake_upsert(door_id: str, debater_id: str, wake_url: str) -> None:
    if not wake_url:
        return
    with db.connect() as con:
        con.execute(
            "INSERT INTO debater_wake (door_id, debater_id, wake_url, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(door_id, debater_id) DO UPDATE SET "
            "wake_url = excluded.wake_url, updated_at = excluded.updated_at",
            (door_id, debater_id, wake_url, time.time()))
    logger.info("wake link registered for %s/%s", door_id, debater_id)


def _wake_get(door_id: str, debater_id: str) -> str:
    with db.connect() as con:
        row = con.execute(
            "SELECT wake_url FROM debater_wake "
            "WHERE door_id = ? AND debater_id = ?",
            (door_id, debater_id)).fetchone()
    return row["wake_url"] if row else ""


async def _wake_ping(wake_url: str) -> tuple[bool, int]:
    """GET the wake link — that's all 'pressing the link' means."""
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=15, follow_redirects=True) as cli:
            r = await cli.get(wake_url)
        return True, r.status_code
    except Exception as e:  # noqa: BLE001
        logger.warning("wake ping failed for %s…: %s", wake_url[:60], e)
        return False, 0


def _wake_note_for_debater(door_id: str, debater_id: str, reason: str) -> None:
    """Drop an admin_chat note so the woken debater knows to explain the stall."""
    msg = (f"⏰ Тебя разбудили по ссылке-будильнику ({reason}). "
           "Прочитай админ-чат и напиши, ПОЧЕМУ ты остановился и что "
           "случилось, затем продолжи дебаты со своего места.")
    with db.connect() as con:
        con.execute(
            "INSERT INTO admin_chat (door_id, debater_id, role, content, created_at) "
            "VALUES (?, ?, 'admin', ?, ?)",
            (door_id, debater_id, msg, time.time()))


async def _wake_debater_owner(request: web.Request) -> web.Response:
    """POST /debate/door/{door_id}/wake_debater — owner presses a debater's
    wake link. Body: {"debater_id": "debater_a"}."""
    _require_owner(request)
    door_id = request.match_info["door_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    debater_id = (body.get("debater_id") or "").strip()
    if not debater_id:
        return web.json_response(
            {"ok": False, "error": "missing_debater_id"}, status=400)
    wake_url = _wake_get(door_id, debater_id)
    if not wake_url:
        return web.json_response(
            {"ok": False, "error": "no_wake_url", "debater_id": debater_id})
    ok, status = await _wake_ping(wake_url)
    with db.connect() as con:
        con.execute(
            "UPDATE debater_wake SET last_pinged_at = ? "
            "WHERE door_id = ? AND debater_id = ?",
            (time.time(), door_id, debater_id))
    _wake_note_for_debater(door_id, debater_id, "вручную владельцем")
    return web.json_response({"ok": ok, "debater_id": debater_id,
                              "wake_url": wake_url, "http_status": status})


async def _wake_register(request: web.Request) -> web.Response:
    """POST /debate/door/{door_id}/wake_register — debater registers its https
    wake link. Body: {"wake_url": "https://..."}."""
    door_id = request.match_info["door_id"]
    debater_id = _resolve_debater(door_id, request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    wake_url = (body.get("wake_url") or "").strip()
    if not wake_url.lower().startswith("http"):
        return web.json_response({"ok": False, "error": "need_http_url"},
                                 status=400)
    _wake_upsert(door_id, debater_id, wake_url)
    return web.json_response({"ok": True, "debater_id": debater_id})


async def _wake_status(request: web.Request) -> web.Response:
    """GET /debate/door/{door_id}/wake_status — per-debater wake info for the
    owner UI. Never returns the raw URL — only booleans + ages."""
    door_id = request.match_info["door_id"]
    now = time.time()
    with db.connect() as con:
        rows = con.execute(
            "SELECT debater_id, wake_url, last_pinged_at FROM debater_wake "
            "WHERE door_id = ?", (door_id,)).fetchall()
        wake_by = {r["debater_id"]: r for r in rows}
        msg_rows = con.execute(
            "SELECT debater_id, MAX(created_at) AS last FROM msgs "
            "WHERE door_id = ? GROUP BY debater_id", (door_id,)).fetchall()
        last_by = {r["debater_id"]: r["last"] for r in msg_rows}
    ids = sorted(set(list(wake_by.keys()) + list(last_by.keys())))
    out = []
    for did in ids:
        last = last_by.get(did)
        wr = wake_by.get(did)
        out.append({
            "debater_id": did,
            "has_wake_url": bool(wr),
            "silence_min": round((now - last) / 60, 1) if last else None,
            "last_pinged_min": (round((now - wr["last_pinged_at"]) / 60, 1)
                                if wr and wr["last_pinged_at"] else None),
        })
    return web.json_response({"ok": True, "debaters": out})


async def _wake_watchdog_tick() -> None:
    """One scan: auto-press the wake link of any debater silent past the
    configured timeout (default 23 min, configurable per-door from the bot)."""
    from .. import state as addon_state
    now = time.time()
    v3 = addon_state.get("debates", "viktor_v3", {}) or {}
    try:
        default_to = int(v3.get("wake_timeout_min", 23) or 23)
    except (TypeError, ValueError):
        default_to = 23
    door_configs = v3.get("door_configs", {}) or {}
    timeout_by_door: dict[str, int] = {}
    for _n, cfg in door_configs.items():
        if isinstance(cfg, dict) and cfg.get("door_id"):
            try:
                timeout_by_door[cfg["door_id"]] = int(
                    cfg.get("wake_timeout_min", default_to) or default_to)
            except (TypeError, ValueError):
                timeout_by_door[cfg["door_id"]] = default_to
    targets: list[tuple[str, str, str, int]] = []
    with db.connect() as con:
        rows = con.execute(
            "SELECT door_id, debater_id, wake_url, last_pinged_at "
            "FROM debater_wake").fetchall()
        for r in rows:
            door_id = r["door_id"]
            timeout_min = timeout_by_door.get(door_id, default_to)
            if timeout_min <= 0:
                continue  # auto-wake disabled
            door = state_helpers.load_door_row(con, door_id)
            if not door or door["status"] != "open":
                continue  # closed / paused → leave alone
            if door["current_phase"] in ("done", "terminated"):
                continue
            last = con.execute(
                "SELECT MAX(created_at) AS m FROM msgs "
                "WHERE door_id = ? AND debater_id = ?",
                (door_id, r["debater_id"])).fetchone()["m"]
            if not last:
                continue  # never posted yet — don't nag
            if (now - last) < timeout_min * 60:
                continue
            if (now - (r["last_pinged_at"] or 0)) < timeout_min * 60:
                continue  # throttle: ≤ 1 auto-ping per timeout window
            targets.append((door_id, r["debater_id"], r["wake_url"],
                            round((now - last) / 60)))
    for door_id, debater_id, wake_url, silence_min in targets:
        ok, status = await _wake_ping(wake_url)
        with db.connect() as con:
            con.execute(
                "UPDATE debater_wake SET last_pinged_at = ? "
                "WHERE door_id = ? AND debater_id = ?",
                (now, door_id, debater_id))
        _wake_note_for_debater(
            door_id, debater_id, f"авто-будильник, молчание {silence_min} мин")
        logger.info("auto-wake %s/%s silence=%smin ok=%s status=%s",
                    door_id, debater_id, silence_min, ok, status)


async def _wake_watchdog_loop() -> None:
    await asyncio.sleep(30)  # let the process settle on boot
    while True:
        try:
            await _wake_watchdog_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("wake watchdog tick error")
        await asyncio.sleep(60)


async def _wake_watchdog_on_startup(app: web.Application) -> None:
    global _WAKE_WATCHDOG_TASK
    if _WAKE_WATCHDOG_TASK is None or _WAKE_WATCHDOG_TASK.done():
        _WAKE_WATCHDOG_TASK = asyncio.create_task(_wake_watchdog_loop())
        logger.info("debate wake-watchdog started")


def register_http_endpoints(app: web.Application) -> None:
    """Mount debate-server-v3 endpoints under /debate/... on the given
    aiohttp Application. Call once at startup in both polling and webhook
    modes — mirrors the fable5/vision/test registration pattern.
    """
    # Initialize DB
    db.init_db()
    logger.info("debate-server-v3 engine initialised (embedded)")

    # Ensure owner token exists
    _get_owner_token()

    p = _PREFIX
    app.router.add_get(f"{p}/", _root)
    app.router.add_get(f"{p}/healthz", _healthz)
    app.router.add_get(f"{p}/catalog/{{part_id}}", _catalog)

    # Door creation (classic + simplified)
    app.router.add_post(f"{p}/door/create", _door_create)
    app.router.add_post(f"{p}/door/create_simple", _door_create_simple)
    app.router.add_get(f"{p}/doors", _doors_list)

    # Join system (no-token auth)
    app.router.add_get(f"{p}/join/{{join_secret}}", _join)
    app.router.add_get(f"{p}/door/{{door_id}}/lobby", _door_lobby)
    app.router.add_post(f"{p}/door/{{door_id}}/lobby_note", _door_lobby_note_post)
    app.router.add_get(f"{p}/door/{{door_id}}/lobby_notes", _door_lobby_notes_get)
    app.router.add_post(f"{p}/door/{{door_id}}/kick", _door_kick)

    # Debater operations
    app.router.add_post(f"{p}/door/{{door_id}}/msg", _door_msg)
    app.router.add_post(f"{p}/door/{{door_id}}/validate", _door_validate)

    # Read-only state
    app.router.add_get(f"{p}/door/{{door_id}}/state", _door_state)
    app.router.add_get(f"{p}/door/{{door_id}}/result", _door_result)
    app.router.add_get(f"{p}/door/{{door_id}}/snapshot", _door_snapshot)
    app.router.add_post(f"{p}/restore", _restore)
    app.router.add_get(f"{p}/door/{{door_id}}/catchup", _door_catchup)
    app.router.add_post(f"{p}/door/{{door_id}}/catchup_post", _door_catchup_post)
    app.router.add_get(f"{p}/door/{{door_id}}/events", _door_events)
    app.router.add_get(f"{p}/door/{{door_id}}/budget", _door_budget)
    app.router.add_get(f"{p}/door/{{door_id}}/acceptance", _door_acceptance)

    # Owner operations
    app.router.add_post(f"{p}/door/{{door_id}}/owner_directive",
                        _door_owner_directive)
    app.router.add_get(f"{p}/door/{{door_id}}/artifacts.zip",
                        _door_artifacts)
    app.router.add_post(f"{p}/door/{{door_id}}/handoff", _door_handoff)
    app.router.add_get(f"{p}/door/{{door_id}}/debater_token",
                       _door_debater_token)
    app.router.add_post(f"{p}/door/{{door_id}}/judge_mode", _door_judge_mode)
    app.router.add_get(f"{p}/door/{{door_id}}/judge_mode", _door_judge_mode_get)

    # Export/import documents
    app.router.add_get(f"{p}/export/rules", _export_rules)
    app.router.add_get(f"{p}/export/debater/{{num}}", _export_debater)
    app.router.add_post(f"{p}/import/rules", _import_rules)

    # Motivation endpoints
    app.router.add_get(f"{p}/motivation/status", _motivation_status)
    app.router.add_get(f"{p}/motivation/history", _motivation_status)
    app.router.add_post(f"{p}/motivation/inject", _motivation_inject)

    # Wake endpoint (sleep/wake system for API debaters)
    app.router.add_get(f"{p}/wake/{{wake_token}}", _wake_debater)

    # Wake-the-debater via https trigger links (external debaters)
    app.router.add_post(f"{p}/door/{{door_id}}/wake_debater", _wake_debater_owner)
    app.router.add_post(f"{p}/door/{{door_id}}/wake_register", _wake_register)
    app.router.add_get(f"{p}/door/{{door_id}}/wake_status", _wake_status)

    # Admin chat (debater ↔ admin AI) — shared: both debaters see all
    app.router.add_post(f"{p}/door/{{door_id}}/admin_chat", _admin_chat_post)
    app.router.add_get(f"{p}/door/{{door_id}}/admin_chat", _admin_chat_get)

    # Admin connect via link (limited rights: read state + admin chat only)
    app.router.add_get(f"{p}/admin_connect/{{admin_secret}}", _admin_connect)

    # Background watchdog that auto-presses wake links on silence.
    app.on_startup.append(_wake_watchdog_on_startup)

    logger.info("debate-server-v3 HTTP endpoints registered under %s/", p)
