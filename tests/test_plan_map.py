# План карта addon tests: link lifecycle, plan validation, HTTP gate.
# Co-authored with CoCo
"""Tests for the «🗺 План карта» addon.

Covers:

* token rotation + disable round-trip (Fable5 principle)
* ``normalize_plan`` validation (shape, dedupe, dangling arrows)
* plan persistence in the Master Room storage file
* HTTP gate: forbidden on wrong/disabled token, editor + data round-trip
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from bot.addons import plan_map
from bot.addons.plan_map import server as pm_server
from bot.storage import storage


@pytest.fixture
def fresh_plan_map_state():
    """Wipe addons.plan_map + the plan file between tests."""
    def _wipe():
        settings = storage._settings()  # noqa: SLF001
        settings.setdefault("addons", {}).pop("plan_map", None)
        storage._save()  # noqa: SLF001
        p = pm_server.plan_file()
        if p.exists():
            p.unlink()

    _wipe()
    yield
    _wipe()


# --- link lifecycle (Fable5 principle) -----------------------------------


def test_generate_link_rotates_and_enables(fresh_plan_map_state):
    assert plan_map.get_token() == ""
    assert plan_map.is_enabled() is False

    t1, link1 = plan_map.generate_link(base_url="https://bot.example")
    assert t1
    assert plan_map.is_enabled() is True
    assert link1 == f"https://bot.example/planmap/{t1}/"

    t2, link2 = plan_map.generate_link(base_url="https://bot.example")
    assert t2 != t1, "every gen must rotate the token"
    assert link2.endswith(f"/{t2}/")


def test_disable_keeps_token_but_blocks_access(fresh_plan_map_state):
    token, _ = plan_map.generate_link(base_url="https://x.test")
    assert plan_map.disable_link() is True  # was enabled
    assert plan_map.is_enabled() is False
    assert plan_map.get_token() == token  # token persists


# --- normalize_plan --------------------------------------------------------


def test_normalize_rejects_non_dict_and_bad_lists():
    assert pm_server.normalize_plan(None) is None
    assert pm_server.normalize_plan([]) is None
    assert pm_server.normalize_plan({"nodes": {}, "edges": []}) is None
    assert pm_server.normalize_plan({"nodes": [], "edges": {}}) is None


def test_normalize_rejects_duplicate_or_missing_node_ids():
    assert pm_server.normalize_plan(
        {"nodes": [{"id": "", "x": 0, "y": 0}], "edges": []}
    ) is None
    assert pm_server.normalize_plan(
        {"nodes": [{"id": "a", "x": 0, "y": 0}, {"id": "a", "x": 1, "y": 1}],
         "edges": []}
    ) is None


def test_normalize_drops_dangling_and_duplicate_edges():
    plan = pm_server.normalize_plan(
        {
            "title": "t",
            "nodes": [
                {"id": "a", "name": "A", "text": "one", "x": 1, "y": 2},
                {"id": "b", "name": "B", "text": "two", "x": 3, "y": 4},
            ],
            "edges": [
                {"from": "a", "to": "b"},
                {"from": "a", "to": "b"},        # duplicate → dropped
                {"from": "a", "to": "a"},        # self-loop → dropped
                {"from": "a", "to": "ghost"},   # dangling → dropped
            ],
        }
    )
    assert plan is not None
    assert plan["edges"] == [
        {"from": "a", "to": "b", "style": 1, "from_side": ""}
    ]
    assert [n["id"] for n in plan["nodes"]] == ["a", "b"]


def test_save_and_load_roundtrip(fresh_plan_map_state):
    plan = pm_server.normalize_plan(
        {"title": "Мой план",
         "nodes": [{"id": "n1", "name": "Шаг 1", "text": "сделать", "x": 10, "y": 20}],
         "edges": []}
    )
    pm_server.save_plan(plan)
    assert pm_server.plan_file().exists()
    loaded = pm_server.load_plan()
    assert loaded["title"] == "Мой план"
    assert loaded["nodes"][0]["name"] == "Шаг 1"


def test_load_plan_survives_corrupt_file(fresh_plan_map_state):
    pm_server.plan_file().write_text("{not json", encoding="utf-8")
    plan = pm_server.load_plan()
    assert plan["nodes"] == [] and plan["edges"] == []


# --- HTTP gate -------------------------------------------------------------


async def _make_client():
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    plan_map.register_http_endpoints(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_http_forbidden_wrong_or_disabled_token(fresh_plan_map_state):
    token, _ = plan_map.generate_link(base_url="")
    client = await _make_client()
    try:
        r = await client.get("/planmap/WRONG/data")
        assert r.status == 403

        plan_map.disable_link()
        r = await client.get(f"/planmap/{token}/data")
        assert r.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_editor_and_data_roundtrip(fresh_plan_map_state):
    token, _ = plan_map.generate_link(base_url="")
    client = await _make_client()
    try:
        r = await client.get(f"/planmap/{token}/")
        assert r.status == 200
        assert "text/html" in r.headers["Content-Type"]

        payload = {
            "title": "chain",
            "nodes": [
                {"id": "a", "name": "A", "text": "1", "x": 0, "y": 0},
                {"id": "b", "name": "B", "text": "2", "x": 300, "y": 0},
            ],
            "edges": [{"from": "a", "to": "b"}],
        }
        r = await client.post(f"/planmap/{token}/data", json=payload)
        assert r.status == 200
        body = await r.json()
        assert body == {"ok": True, "nodes": 2, "edges": 1}

        r = await client.get(f"/planmap/{token}/data")
        assert r.status == 200
        data = await r.json()
        assert len(data["nodes"]) == 2
        assert data["edges"] == [
            {"from": "a", "to": "b", "style": 1, "from_side": ""}
        ]

        r = await client.post(f"/planmap/{token}/data", data="{oops")
        assert r.status == 400

        r = await client.post(f"/planmap/{token}/data", json={"nodes": "x"})
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_ping_reports_counts(fresh_plan_map_state):
    token, _ = plan_map.generate_link(base_url="")
    pm_server.save_plan(
        pm_server.normalize_plan(
            {"nodes": [{"id": "a", "x": 0, "y": 0}], "edges": []}
        )
    )
    client = await _make_client()
    try:
        r = await client.get(f"/planmap/{token}/ping")
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True
        assert body["nodes"] == 1
        assert body["edges"] == 0
    finally:
        await client.close()
