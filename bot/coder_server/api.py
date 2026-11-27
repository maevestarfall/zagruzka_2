"""Authenticated aiohttp API and resumable SSE stream for coder-server."""
from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from aiohttp import web

from .service import CoderService

_MAX_BODY = 1_000_000


def create_app(service: CoderService, token: str = "") -> web.Application:
    app = web.Application(client_max_size=_MAX_BODY)
    app["coder_service"] = service
    app["coder_token"] = token

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
        configured = request.app["coder_token"]
        if configured and request.path != "/api/v1/health":
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(configured, supplied):
                return web.json_response({"error": "forbidden"}, status=403)
        return await handler(request)

    app.middlewares.append(auth)
    app.router.add_get("/api/v1/health", _health)
    app.router.add_get("/api/v1/capabilities", _capabilities)
    app.router.add_get("/api/v1/tools", _tools)
    app.router.add_post("/api/v1/tasks", _create_task)
    app.router.add_get("/api/v1/tasks", _list_tasks)
    app.router.add_get("/api/v1/tasks/{task_id}", _get_task)
    app.router.add_get("/api/v1/tasks/{task_id}/events", _events)
    app.router.add_get("/api/v1/tasks/{task_id}/stream", _stream)
    app.router.add_get("/api/v1/tasks/{task_id}/evidence", _evidence)
    app.router.add_post("/api/v1/tasks/{task_id}/evidence/gate", _evidence_gate)
    app.router.add_get("/api/v1/tasks/{task_id}/artifacts", _artifacts)
    app.router.add_get("/api/v1/tasks/{task_id}/approvals", _approvals)
    app.router.add_post("/api/v1/approvals/{approval_id}", _approval_resolve)
    app.router.add_get("/api/v1/tasks/{task_id}/secret-refs", _secret_refs)
    app.router.add_post("/api/v1/tasks/{task_id}/secret-refs", _secret_ref_add)
    app.router.add_post("/api/v1/tasks/{task_id}/tools/{tool}", _invoke)
    app.router.add_post("/api/v1/tasks/{task_id}/{action:pause|resume|cancel|retry}", _control)
    app.router.add_post("/api/v1/tasks/{task_id}/approval", _approval)
    app.router.add_post("/api/v1/tasks/{task_id}/complete", _complete)
    app.router.add_post("/api/v1/recover", _recover)
    app.router.add_get("/api/v1/tasks/{task_id}/jobs", _jobs_list)
    app.router.add_post("/api/v1/tasks/{task_id}/jobs", _jobs_enqueue)
    app.router.add_get("/api/v1/jobs/{job_id}", _job_get)
    app.router.add_post("/api/v1/jobs/{job_id}/{action:pause|resume|cancel|retry}", _job_control)
    app.router.add_post("/api/v1/jobs/{job_id}/wait", _job_wait)
    app.router.add_post("/api/v1/events/{event}", _signal_event)
    app.router.add_get("/api/v1/tasks/{task_id}/checkpoints/{name}", _checkpoint_get)
    app.router.add_put("/api/v1/tasks/{task_id}/checkpoints/{name}", _checkpoint_put)
    return app


def _service(request: web.Request) -> CoderService:
    return request.app["coder_service"]


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid json"}), content_type="application/json") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "object required"}), content_type="application/json")
    return body


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "coder-server", "version": "1.0.0"})


async def _capabilities(request: web.Request) -> web.Response:
    service = _service(request)
    return web.json_response({
        "protocol": "master-room/1",
        "tools": service.registry.describe(),
        "runtime": {"isolation": "task-workspace", "languages": ["python", "javascript"], "package_managers": ["pip", "npm", "pnpm"]},
        "features": {"sse_replay": True, "checkpoints": True, "idempotency": True, "evidence_ledger": True, "approvals": True},
        "integrations": [],
    })


async def _tools(request: web.Request) -> web.Response:
    return web.json_response({"tools": _service(request).registry.describe()})


async def _create_task(request: web.Request) -> web.Response:
    body = await _json(request)
    request_text = str(body.get("request") or body.get("description") or "").strip()
    if not request_text:
        return web.json_response({"error": "description required", "code": "validation_error"}, status=400)
    service = _service(request)
    task = service.create_task(str(body.get("title", "")), request_text, body.get("budget"))
    if body.get("auto_start", True):
        service.start(task["id"])
    return web.json_response(task, status=201)


async def _list_tasks(request: web.Request) -> web.Response:
    return web.json_response({"tasks": _service(request).store.list_tasks(int(request.query.get("limit", 100)))})


async def _get_task(request: web.Request) -> web.Response:
    return web.json_response(_service(request).store.get_task(request.match_info["task_id"]))


async def _events(request: web.Request) -> web.Response:
    after = int(request.query.get("after", request.headers.get("Last-Event-ID", 0)))
    return web.json_response({"events": _service(request).store.events(request.match_info["task_id"], after)})


async def _evidence(request: web.Request) -> web.Response:
    return web.json_response({"evidence": _service(request).store.evidence(request.match_info["task_id"])})


async def _evidence_gate(request: web.Request) -> web.Response:
    body = await _json(request)
    gate = _service(request).evidence_gate(request.match_info["task_id"], {str(item) for item in body.get("required", [])})
    return web.json_response(gate, status=200 if gate["ok"] else 409)


async def _artifacts(request: web.Request) -> web.Response:
    return web.json_response({"artifacts": _service(request).store.artifacts(request.match_info["task_id"])})


async def _approvals(request: web.Request) -> web.Response:
    return web.json_response({"approvals": _service(request).store.approvals(request.match_info["task_id"])})


async def _approval_resolve(request: web.Request) -> web.Response:
    body = await _json(request)
    try:
        return web.json_response(_service(request).store.resolve_approval(request.match_info["approval_id"], bool(body.get("approved")), body.get("response")))
    except KeyError:
        return web.json_response({"error": "approval not found"}, status=404)


async def _secret_refs(request: web.Request) -> web.Response:
    return web.json_response({"secret_refs": _service(request).store.secret_refs(request.match_info["task_id"])})


async def _secret_ref_add(request: web.Request) -> web.Response:
    body = await _json(request)
    name, env_key = str(body.get("name", "")).strip(), str(body.get("env_key", "")).strip()
    if not name or not env_key or not env_key.replace("_", "").isalnum() or env_key.upper() != env_key:
        return web.json_response({"error": "valid uppercase env_key required"}, status=400)
    return web.json_response(_service(request).store.add_secret_ref(request.match_info["task_id"], name, env_key), status=201)


async def _invoke(request: web.Request) -> web.Response:
    body = await _json(request)
    result = await _service(request).invoke(request.match_info["task_id"], request.match_info["tool"], body.get("arguments", {}), request.headers.get("Idempotency-Key"))
    return web.json_response(result, status=200 if result["ok"] else 422)


async def _control(request: web.Request) -> web.Response:
    try:
        return web.json_response(_service(request).control(request.match_info["task_id"], request.match_info["action"]))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)


async def _approval(request: web.Request) -> web.Response:
    body = await _json(request)
    try:
        return web.json_response(_service(request).approve(request.match_info["task_id"], bool(body.get("approved"))))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)


async def _complete(request: web.Request) -> web.Response:
    """Evidence-gated завершение задачи: completed только при свежих доказательствах."""
    body = await _json(request)
    required = {str(item) for item in body.get("required", [])} or None
    try:
        result = _service(request).complete(request.match_info["task_id"], required)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response(result, status=200 if result["completed"] else 409)


async def _recover(request: web.Request) -> web.Response:
    """Восстановление после рестарта: reclaim просроченных lease и перезапуск воркеров."""
    recovered = _service(request).recover()
    return web.json_response({"recovered": recovered})


async def _jobs_list(request: web.Request) -> web.Response:
    return web.json_response({"jobs": _service(request).jobs.list(request.match_info["task_id"])})


async def _jobs_enqueue(request: web.Request) -> web.Response:
    body = await _json(request)
    if not body.get("kind"):
        return web.json_response({"error": "kind required", "code": "validation_error"}, status=400)
    job = _service(request).jobs.enqueue(
        request.match_info["task_id"], str(body["kind"]), body.get("payload", {}),
        run_at=body.get("run_at"), delay_seconds=float(body.get("delay_seconds", 0)),
        priority=int(body.get("priority", 100)), max_attempts=int(body.get("max_attempts", 3)),
        deadline=body.get("deadline"),
    )
    return web.json_response(job, status=201)


async def _job_get(request: web.Request) -> web.Response:
    try:
        return web.json_response(_service(request).jobs.get(request.match_info["job_id"]))
    except KeyError:
        return web.json_response({"error": "job not found"}, status=404)


async def _job_control(request: web.Request) -> web.Response:
    try:
        return web.json_response(_service(request).jobs.control(request.match_info["job_id"], request.match_info["action"]))
    except (KeyError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=409)


async def _job_wait(request: web.Request) -> web.Response:
    body = await _json(request)
    event = str(body.get("event", "")).strip()
    if not event:
        return web.json_response({"error": "event required"}, status=400)
    return web.json_response(_service(request).jobs.wait_for_event(request.match_info["job_id"], event))


async def _signal_event(request: web.Request) -> web.Response:
    body = await _json(request)
    message_id = request.headers.get("Idempotency-Key") or str(body.get("message_id", ""))
    if not message_id:
        return web.json_response({"error": "Idempotency-Key required"}, status=400)
    count = _service(request).jobs.signal(message_id, request.match_info["event"], body.get("data", {}))
    return web.json_response({"resumed": count})


async def _checkpoint_get(request: web.Request) -> web.Response:
    item = _service(request).jobs.load_checkpoint(request.match_info["task_id"], request.match_info["name"])
    return web.json_response(item or {"error": "checkpoint not found"}, status=200 if item else 404)


async def _checkpoint_put(request: web.Request) -> web.Response:
    body = await _json(request)
    try:
        item = _service(request).jobs.save_checkpoint(request.match_info["task_id"], request.match_info["name"], body.get("value", {}), body.get("expected_version"))
        return web.json_response(item)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc), "code": "version_conflict"}, status=409)


async def _stream(request: web.Request) -> web.StreamResponse:
    task_id = request.match_info["task_id"]
    after = int(request.query.get("after", request.headers.get("Last-Event-ID", 0)))
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await response.prepare(request)
    idle = 0
    while idle < 300:
        events = _service(request).store.events(task_id, after)
        if events:
            idle = 0
            for event in events:
                after = event["seq"]
                payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                await response.write(f"id: {after}\nevent: {event['event_type']}\ndata: {payload}\n\n".encode())
        else:
            idle += 1
            await response.write(b": heartbeat\n\n")
        await asyncio.sleep(1)
    return response
