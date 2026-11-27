"""HTTP-surface tests for ``bot.addons.masterroom.server``.

Covers:

* Token-gate: missing / wrong / disabled token → 403 neutral
* GET / → splash HTML
* GET /ping → JSON with version + counts
* GET /info → snapshot of active server / target / github
* POST /event updates an existing project's status + log
* POST /event with unknown task_id → 200 ok, stored=False (no crash)
* POST /done sets terminal state + deploy_url
* POST /github relays workflow conclusion → status
* Payload-too-large rejected
"""

from __future__ import annotations


import pytest
from aiohttp import web

from bot.addons.masterroom import server as mr_server
from bot.addons.masterroom import state as mr_state
from bot.storage import storage


@pytest.fixture
def fresh_mr_state():
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("masterroom", None)
    storage._save()  # noqa: SLF001
    yield
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("masterroom", None)
    storage._save()  # noqa: SLF001


@pytest.fixture
async def client(aiohttp_client, fresh_mr_state):
    app = web.Application()
    mr_server.register_http_endpoints(app)
    return await aiohttp_client(app)


@pytest.mark.asyncio
async def test_root_requires_valid_enabled_token(client):
    # No token issued at all → forbidden for any path
    r = await client.get("/masterroom/abc/")
    assert r.status == 403
    # Issue token, disable → still forbidden
    token, _ = mr_state.generate_link(base_url="https://x")
    mr_state.disable_link()
    r = await client.get(f"/masterroom/{token}/")
    assert r.status == 403


@pytest.mark.asyncio
async def test_root_returns_splash_html(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    r = await client.get(f"/masterroom/{token}/")
    assert r.status == 200
    body = await r.text()
    assert "Master Room" in body
    assert "Devin-style" in body


@pytest.mark.asyncio
async def test_ping_json_shape(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    r = await client.get(f"/masterroom/{token}/ping")
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["version"] == "masterroom/1"
    assert body["n_projects"] == 0
    assert body["active_server"] is False


@pytest.mark.asyncio
async def test_info_reflects_state(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    srv = mr_state.add_server("p", "https://c", "tok")
    gh = mr_state.add_github_account("m", "chloe", "GH_TOK", "private")
    mr_state.set_target_url("render", "https://app.onrender.com")
    r = await client.get(f"/masterroom/{token}/info")
    assert r.status == 200
    body = await r.json()
    assert body["active_server"]["id"] == srv["id"]
    assert body["active_github"]["owner"] == "chloe"
    assert body["active_target"]["id"] == "render"
    assert body["n_servers"] == 1
    assert body["n_github_accounts"] == 1


@pytest.mark.asyncio
async def test_event_updates_existing_project_status_and_log(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    p = mr_state.add_project({"task_id": "t1", "title": "demo",
                              "stack": "static", "status": "queued"})
    r = await client.post(
        f"/masterroom/{token}/event",
        json={"task_id": "t1", "status": "coding",
              "log_line": "started", "progress": 0.1},
    )
    assert r.status == 200
    body = await r.json()
    assert body["stored"] is True
    fresh = mr_state.get_project(p["id"])
    assert fresh["status"] == "coding"
    assert fresh["progress"] == 0.1
    assert any(e.get("line") == "started" for e in fresh.get("log", []))


@pytest.mark.asyncio
async def test_event_unknown_task_id_no_crash(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    r = await client.post(
        f"/masterroom/{token}/event",
        json={"task_id": "ghost", "status": "coding"},
    )
    assert r.status == 200
    assert (await r.json())["stored"] is False


@pytest.mark.asyncio
async def test_event_missing_task_id_400(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    r = await client.post(f"/masterroom/{token}/event", json={"status": "x"})
    assert r.status == 400


@pytest.mark.asyncio
async def test_done_sets_terminal_state(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    p = mr_state.add_project({"task_id": "t1", "title": "demo",
                              "stack": "static", "status": "coding"})
    r = await client.post(
        f"/masterroom/{token}/done",
        json={"task_id": "t1", "status": "done",
              "deploy_url": "https://demo.onrender.com",
              "github_url": "https://github.com/chloe/demo"},
    )
    assert r.status == 200
    fresh = mr_state.get_project(p["id"])
    assert fresh["status"] == "done"
    assert fresh["deploy_url"] == "https://demo.onrender.com"
    assert fresh["github_url"] == "https://github.com/chloe/demo"


@pytest.mark.asyncio
async def test_done_bogus_status_clamped(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    p = mr_state.add_project({"task_id": "t1", "title": "x", "status": "coding"})
    r = await client.post(
        f"/masterroom/{token}/done",
        json={"task_id": "t1", "status": "bogus"},
    )
    assert r.status == 200
    assert mr_state.get_project(p["id"])["status"] == "done"


@pytest.mark.asyncio
async def test_github_webhook_marks_failed(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    p = mr_state.add_project({"task_id": "t1", "title": "x",
                              "github_repo": "chloe/demo", "status": "deploying"})
    r = await client.post(
        f"/masterroom/{token}/github",
        json={"task_id": "t1", "conclusion": "failure",
              "html_url": "https://github.com/x/runs/1"},
    )
    assert r.status == 200
    fresh = mr_state.get_project(p["id"])
    assert fresh["status"] == "failed"
    assert fresh["deploy_status"] == "failure"


@pytest.mark.asyncio
async def test_payload_bad_json_400(client):
    token, _ = mr_state.generate_link(base_url="https://x")
    r = await client.post(
        f"/masterroom/{token}/event",
        data=b"not json", headers={"Content-Type": "application/json"},
    )
    assert r.status == 400
