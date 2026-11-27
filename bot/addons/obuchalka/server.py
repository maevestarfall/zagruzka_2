# «🎓 Обучалка» — HTTP-поверхность: /obuchalka/<token>/... (роль по токену).
"""aiohttp surface for the vessel.

The token in the URL resolves to a role:
* teacher — full write access (vessels, leaves, student runs, scores, unlock);
* revisor — read everything, score leaves, leave notes. NO content writes.

The student has no token — it is driven only through the teacher's
``POST /student/run`` and never touches the vessel itself.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from aiohttp import web

from . import export, seed, store, student

logger = logging.getLogger(__name__)

_DASHBOARD = Path(__file__).with_name("dashboard.html")


# ------------------------------------------------------------------ auth ----

def _role(request: web.Request) -> Optional[str]:
    return store.resolve_role(request.match_info.get("token", ""))


def _forbidden() -> web.Response:
    return web.json_response({"error": "forbidden"}, status=403)


def _need_teacher(role: Optional[str]) -> Optional[web.Response]:
    if role != "teacher":
        return web.json_response({"error": "teacher_only"}, status=403)
    return None


async def _json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- common ----

async def _root(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    seed.ensure_seed()
    try:
        html = _DASHBOARD.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return web.Response(text="dashboard missing", status=500)
    return web.Response(text=html, content_type="text/html")


async def _ping(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    return web.json_response({"ok": True, "role": role, "version": "obuchalka/1"})


async def _handoff(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    seed.ensure_seed()
    try:
        limit = int(request.query.get("limit", store.HANDOFF_LIMIT))
    except ValueError:
        limit = store.HANDOFF_LIMIT
    return web.Response(text=store.handoff_text(limit), content_type="text/plain",
                        charset="utf-8")


async def _vessels(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    seed.ensure_seed()
    return web.json_response({
        "role": role,
        "stats": store.stats(),
        "student_configured": student.is_configured(),
        "qwen_model": store.get_qwen_model(),
        "vessels": store.list_vessels(),
    })


async def _vessel(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    try:
        vessel_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    vessel = store.get_vessel(vessel_id)
    if vessel is None:
        return web.json_response({"error": "not_found"}, status=404)
    vessel["leaves"] = store.list_leaves(vessel_id)
    return web.json_response(vessel)


async def _leaf_get(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    try:
        leaf_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    leaf = store.get_leaf(leaf_id)
    if leaf is None:
        return web.json_response({"error": "not_found"}, status=404)
    leaf["attempts"] = store.list_attempts(leaf_id)
    leaf["notes"] = store.list_notes(leaf_id)
    return web.json_response(leaf)


async def _journal(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    return web.json_response({"journal": store.journal_recent(50)})


async def _export_jsonl(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    body = export.build_jsonl()
    return web.Response(
        text=body, content_type="application/jsonl", charset="utf-8",
        headers={"Content-Disposition": 'attachment; filename="cement.jsonl"'},
    )


async def _export_md(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    return web.Response(text=export.build_markdown(), content_type="text/markdown",
                        charset="utf-8")


# --------------------------------------------------------------- teacher ----

async def _vessel_create(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    if (resp := _need_teacher(role)) is not None:
        return resp
    data = await _json_body(request)
    title = str(data.get("title", "")).strip()
    if not title:
        return web.json_response({"error": "title_required"}, status=400)
    vessel_id = store.create_vessel(
        title, macro_map=str(data.get("macro_map", "")),
        priority=int(data.get("priority", 0) or 0),
    )
    store.journal_add(role, "vessel_create", f"vessel#{vessel_id} «{title}»")
    return web.json_response({"ok": True, "id": vessel_id})


async def _vessel_update(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    if (resp := _need_teacher(role)) is not None:
        return resp
    try:
        vessel_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    data = await _json_body(request)
    ok = store.update_vessel(
        vessel_id,
        title=data.get("title"),
        macro_map=data.get("macro_map"),
        priority=int(data["priority"]) if "priority" in data else None,
    )
    if ok:
        store.journal_add(role, "vessel_update", f"vessel#{vessel_id}")
    return web.json_response({"ok": ok})


async def _leaf_create(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    if (resp := _need_teacher(role)) is not None:
        return resp
    data = await _json_body(request)
    try:
        vessel_id = int(data.get("vessel_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "vessel_id_required"}, status=400)
    title = str(data.get("title", "")).strip()
    if not title:
        return web.json_response({"error": "title_required"}, status=400)
    if store.get_vessel(vessel_id) is None:
        return web.json_response({"error": "vessel_not_found"}, status=404)
    leaf_id = store.create_leaf(
        vessel_id, title,
        task=str(data.get("task", "")),
        parent_id=data.get("parent_id"),
        context_budget=int(data.get("context_budget", store.DEFAULT_CONTEXT_BUDGET)),
        position=int(data.get("position", 0) or 0),
    )
    store.journal_add(role, "leaf_create", f"leaf#{leaf_id} «{title}»")
    return web.json_response({"ok": True, "id": leaf_id})


async def _leaf_update(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    if (resp := _need_teacher(role)) is not None:
        return resp
    try:
        leaf_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    data = await _json_body(request)
    ok, why = store.update_leaf(
        leaf_id,
        title=data.get("title"),
        task=data.get("task"),
        content=data.get("content"),
        context_budget=int(data["context_budget"]) if "context_budget" in data else None,
        position=int(data["position"]) if "position" in data else None,
    )
    if ok:
        store.journal_add(role, "leaf_update", f"leaf#{leaf_id}")
        return web.json_response({"ok": True})
    status = {"not_found": 404, "locked": 409}.get(why, 400)
    return web.json_response({"ok": False, "error": why}, status=status)


async def _leaf_score(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    # Both teacher AND revisor may score.
    try:
        leaf_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    data = await _json_body(request)
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError):
        return web.json_response({"error": "score_required"}, status=400)
    ok, why = store.set_score(leaf_id, score, role)
    if ok:
        return web.json_response({"ok": True, "locked": score == 10})
    status = {"not_found": 404, "locked": 409}.get(why, 400)
    return web.json_response({"ok": False, "error": why}, status=status)


async def _leaf_unlock(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    if (resp := _need_teacher(role)) is not None:
        return resp
    try:
        leaf_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    ok, why = store.unlock_leaf(leaf_id, role)
    if ok:
        return web.json_response({"ok": True})
    status = {"not_found": 404}.get(why, 400)
    return web.json_response({"ok": False, "error": why}, status=status)


async def _leaf_note(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    # Notes are the revisor's main instrument, but the teacher may reply too.
    try:
        leaf_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    if store.get_leaf(leaf_id) is None:
        return web.json_response({"error": "not_found"}, status=404)
    data = await _json_body(request)
    text = str(data.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "text_required"}, status=400)
    note_id = store.add_note(leaf_id, role, text)
    return web.json_response({"ok": True, "id": note_id})


async def _student_run(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    if (resp := _need_teacher(role)) is not None:
        return resp
    data = await _json_body(request)
    try:
        leaf_id = int(data.get("leaf_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "leaf_id_required"}, status=400)
    result = await student.run_leaf(leaf_id, str(data.get("context", "")))
    return web.json_response(result, status=200 if result.get("ok") else 400)


async def _config(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    if request.method == "GET":
        return web.json_response({
            "qwen_base_url": store.get_qwen_base_url(),
            "qwen_model": store.get_qwen_model(),
        })
    if (resp := _need_teacher(role)) is not None:
        return resp
    data = await _json_body(request)
    if "qwen_base_url" in data:
        store.set_qwen_base_url(str(data["qwen_base_url"]))
    if "qwen_model" in data:
        store.set_qwen_model(str(data["qwen_model"]))
    store.journal_add(role, "config", "обновлены настройки ученика")
    return web.json_response({"ok": True})


async def _student_ping(request: web.Request) -> web.Response:
    role = _role(request)
    if role is None:
        return _forbidden()
    return web.json_response(await student.ping())


# ------------------------------------------------------------- registrar ----

def register_http_endpoints(app: web.Application) -> None:
    p = "/obuchalka/{token}"
    app.router.add_get(p + "/", _root)
    app.router.add_get(p, _root)
    app.router.add_get(p + "/ping", _ping)
    app.router.add_get(p + "/handoff", _handoff)
    app.router.add_get(p + "/vessels", _vessels)
    app.router.add_get(p + "/vessel/{id}", _vessel)
    app.router.add_post(p + "/vessel", _vessel_create)
    app.router.add_put(p + "/vessel/{id}", _vessel_update)
    app.router.add_get(p + "/leaf/{id}", _leaf_get)
    app.router.add_post(p + "/leaf", _leaf_create)
    app.router.add_put(p + "/leaf/{id}", _leaf_update)
    app.router.add_post(p + "/leaf/{id}/score", _leaf_score)
    app.router.add_post(p + "/leaf/{id}/unlock", _leaf_unlock)
    app.router.add_post(p + "/leaf/{id}/note", _leaf_note)
    app.router.add_get(p + "/journal", _journal)
    app.router.add_get(p + "/export.jsonl", _export_jsonl)
    app.router.add_get(p + "/export.md", _export_md)
    app.router.add_post(p + "/student/run", _student_run)
    app.router.add_get(p + "/student/ping", _student_ping)
    app.router.add_get(p + "/config", _config)
    app.router.add_post(p + "/config", _config)
    logger.info("obuchalka endpoints registered on aiohttp app")
