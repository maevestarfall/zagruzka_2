"""HTTP-client tests for ``bot.addons.masterroom.client``.

Uses respx to mock the coder-bot API so we never hit the network.
Covers:

* Health → returns JSON
* create_task posts the expected body + bearer header
* get_status / get_logs / cancel / redeploy hit the right paths
* Non-2xx raises ``CoderClientError`` with status + payload
* Network failure raises ``CoderClientError`` with no status
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bot.addons.masterroom.client import CoderClientError, MasterRoomCoderClient


SERVER = {
    "id": "s1",
    "name": "prod",
    "url": "https://coder.example.com",
    "token": "tok-xyz",
}


@pytest.mark.asyncio
async def test_health_returns_json():
    async with respx.mock(base_url=SERVER["url"]) as m:
        m.get("/coder/health").respond(
            200, json={"ok": True, "version": "1.0", "queue_depth": 0}
        )
        async with MasterRoomCoderClient(SERVER) as cli:
            out = await cli.health()
        assert out["ok"] is True
        assert out["version"] == "1.0"


@pytest.mark.asyncio
async def test_create_task_sends_bearer_and_body():
    async with respx.mock(base_url=SERVER["url"]) as m:
        route = m.post("/coder/task").respond(
            200, json={"task_id": "t1", "status": "queued", "accepted_at": 1}
        )
        async with MasterRoomCoderClient(SERVER) as cli:
            out = await cli.create_task(
                prompt="build a static site",
                stack="static",
                target_id="render",
                budget=200_000,
                callback_url="https://lilush.example/masterroom/abc/",
                github={"account_id": "a1", "owner": "chloe",
                        "token_ref": "GITHUB_TOKEN"},
                title="hello",
            )
        assert out["task_id"] == "t1"
        request = route.calls[0].request
        assert request.headers["authorization"] == "Bearer tok-xyz"
        import json as _json
        body = _json.loads(request.content)
        assert body["prompt"] == "build a static site"
        assert body["stack"] == "static"
        assert body["target_id"] == "render"
        assert body["budget"] == 200_000
        assert body["title"] == "hello"
        assert body["github"]["owner"] == "chloe"


@pytest.mark.asyncio
async def test_get_status_params_query():
    async with respx.mock(base_url=SERVER["url"]) as m:
        m.get("/coder/status", params={"task_id": "t1"}).respond(
            200, json={"task_id": "t1", "status": "coding", "progress": 0.5}
        )
        async with MasterRoomCoderClient(SERVER) as cli:
            out = await cli.get_status("t1")
        assert out["status"] == "coding"


@pytest.mark.asyncio
async def test_get_logs_paths_and_lines_param():
    async with respx.mock(base_url=SERVER["url"]) as m:
        m.get("/coder/task/t1/logs", params={"lines": "50"}).respond(
            200, json={"task_id": "t1", "lines": ["a", "b"], "truncated": False}
        )
        async with MasterRoomCoderClient(SERVER) as cli:
            out = await cli.get_logs("t1", lines=50)
        assert out["lines"] == ["a", "b"]


@pytest.mark.asyncio
async def test_cancel_and_redeploy():
    async with respx.mock(base_url=SERVER["url"]) as m:
        m.post("/coder/task/t1/cancel").respond(200, json={"task_id": "t1", "status": "cancelled"})
        m.post("/coder/task/t1/redeploy", params={"target_id": "oracle"}).respond(
            200, json={"task_id": "t1", "status": "deploying"}
        )
        async with MasterRoomCoderClient(SERVER) as cli:
            cancel = await cli.cancel("t1")
            redeploy = await cli.redeploy("t1", "oracle")
        assert cancel["status"] == "cancelled"
        assert redeploy["status"] == "deploying"


@pytest.mark.asyncio
async def test_4xx_raises_with_status_and_payload():
    async with respx.mock(base_url=SERVER["url"]) as m:
        m.get("/coder/health").respond(401, json={"error": "no token"})
        with pytest.raises(CoderClientError) as ei:
            async with MasterRoomCoderClient(SERVER) as cli:
                await cli.health()
        assert ei.value.status == 401
        assert ei.value.payload == {"error": "no token"}


@pytest.mark.asyncio
async def test_network_failure_raises_no_status():
    async with respx.mock(base_url=SERVER["url"]) as m:
        m.get("/coder/health").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(CoderClientError) as ei:
            async with MasterRoomCoderClient(SERVER) as cli:
                await cli.health()
        assert ei.value.status is None


@pytest.mark.asyncio
async def test_non_json_2xx_returns_raw():
    """Coder shouldn't do this, but we don't want to crash on it."""
    async with respx.mock(base_url=SERVER["url"]) as m:
        m.get("/coder/health").respond(200, text="OK")
        async with MasterRoomCoderClient(SERVER) as cli:
            out = await cli.health()
        assert out.get("raw") == "OK"


@pytest.mark.asyncio
async def test_url_trailing_slash_stripped():
    server = dict(SERVER, url="https://coder.example.com/")
    async with respx.mock(base_url="https://coder.example.com") as m:
        m.get("/coder/health").respond(200, json={"ok": True})
        async with MasterRoomCoderClient(server) as cli:
            await cli.health()
            assert cli.base_url == "https://coder.example.com"
