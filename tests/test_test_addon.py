"""End-to-end tests for the «🧪 Тест» web preview slot addon.

Covers:

* token rotation + disable round-trip (state.json bookkeeping)
* HTTP gate: forbidden when missing / wrong / disabled token
* ``GET /test/<token>/`` splash returns HTML with status info
* ``GET /test/<token>/ping`` JSON shape + version
* ``GET /test/<token>/info`` payload metadata
* ``POST /test/<token>/payload`` accepts html/url/json kinds, rejects bad ones
* Stashed HTML payload is rendered by the root route
* Payload size cap blocks oversized bodies
"""

from __future__ import annotations


import pytest
from aiohttp import web

from bot.addons import test as test_addon
from bot.storage import storage


@pytest.fixture
def fresh_test_state():
    """Wipe addons.test between tests so token state doesn't leak."""
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("test", None)
    storage._save()  # noqa: SLF001
    yield
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("test", None)
    storage._save()  # noqa: SLF001


def test_generate_link_rotates_and_enables(fresh_test_state):
    assert test_addon.get_token() == ""
    assert test_addon.is_enabled() is False

    t1, link1 = test_addon.generate_link(base_url="https://bot.example")
    assert t1
    assert test_addon.is_enabled() is True
    assert link1 == f"https://bot.example/test/{t1}/"

    t2, link2 = test_addon.generate_link(base_url="https://bot.example")
    assert t2 != t1, "every gen must rotate the token"
    assert link2.endswith(f"/{t2}/")


def test_disable_keeps_token_but_blocks_access(fresh_test_state):
    token, _ = test_addon.generate_link(base_url="https://x.test")
    assert test_addon.disable_link() is True  # was enabled
    assert test_addon.is_enabled() is False
    assert test_addon.get_token() == token  # token persists


def test_get_link_without_base_uses_resolver(fresh_test_state, monkeypatch):
    # Wipe everything, then set ORACLE_PUBLIC_HOST so we exercise the
    # full chain through bot.config.resolve_public_url. This is the
    # Oracle Free Tier path Chloe asked us to support.
    for env in (
        "PUBLIC_URL", "RENDER_EXTERNAL_URL", "RAILWAY_PUBLIC_DOMAIN",
        "FLY_APP_NAME", "ORACLE_PUBLIC_HOST", "KEEP_ALIVE_URL",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("ORACLE_PUBLIC_HOST", "bot.chloe.example")
    token, _ = test_addon.generate_link()
    link = test_addon.get_link()
    assert link == f"https://bot.chloe.example/test/{token}/"


def test_get_link_no_envs_returns_absolute_localhost(fresh_test_state, monkeypatch):
    # Relative links (/test/<token>/) are rejected by Telegram url-buttons
    # (BUTTON_URL_INVALID); without any public base we must fall back to an
    # absolute local URL so the «🧪 Тест» screen always renders.
    for env in (
        "PUBLIC_URL", "RENDER_EXTERNAL_URL", "RAILWAY_PUBLIC_DOMAIN",
        "FLY_APP_NAME", "ORACLE_PUBLIC_HOST", "KEEP_ALIVE_URL",
    ):
        monkeypatch.delenv(env, raising=False)
    token, link = test_addon.generate_link()
    assert link.startswith("http://127.0.0.1:")
    assert link.endswith(f"/test/{token}/")


async def _make_client():
    """Spin up the aiohttp app with test routes for in-process tests."""
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    test_addon.register_http_endpoints(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_root_requires_valid_enabled_token(fresh_test_state):
    client = await _make_client()
    try:
        # No token at all → 403 JSON
        r = await client.get("/test/whatever/")
        assert r.status == 403
        assert (await r.json()).get("error") == "forbidden"

        token, _ = test_addon.generate_link(base_url="https://x.test")
        r = await client.get(f"/test/{token}/")
        assert r.status == 200
        body = await r.text()
        assert "🧪" in body or "Тест" in body
        assert r.content_type == "text/html"

        # Wrong token → 403
        r = await client.get("/test/not-the-token/")
        assert r.status == 403

        # Disabled → 403 even with correct token
        test_addon.disable_link()
        r = await client.get(f"/test/{token}/")
        assert r.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ping_shape(fresh_test_state):
    client = await _make_client()
    try:
        token, _ = test_addon.generate_link(base_url="https://x.test")
        r = await client.get(f"/test/{token}/ping")
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True
        assert body["version"] == "test/1"
        assert body["has_payload"] is False
        assert isinstance(body["rotated_at"], int)
        assert body["rotated_at"] > 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_info_reflects_payload(fresh_test_state):
    client = await _make_client()
    try:
        token, _ = test_addon.generate_link(base_url="https://x.test")
        # Empty → payload_kind None.
        r = await client.get(f"/test/{token}/info")
        assert r.status == 200
        body = await r.json()
        assert body["payload_kind"] is None
        assert body["payload_size"] == 0

        # Post a payload, then re-check.
        r = await client.post(
            f"/test/{token}/payload",
            json={"kind": "url", "content": "https://preview.example.com/x"},
        )
        assert r.status == 200
        assert (await r.json())["stored"] == "url"

        r = await client.get(f"/test/{token}/info")
        body = await r.json()
        assert body["payload_kind"] == "url"
        assert body["payload_size"] == len("https://preview.example.com/x")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_payload_html_is_rendered_by_root(fresh_test_state):
    client = await _make_client()
    try:
        token, _ = test_addon.generate_link(base_url="https://x.test")
        html_body = "<!doctype html><html><body><h1>hello from coder-bot</h1></body></html>"
        r = await client.post(
            f"/test/{token}/payload",
            json={"kind": "html", "content": html_body},
        )
        assert r.status == 200

        r = await client.get(f"/test/{token}/")
        assert r.status == 200
        body = await r.text()
        assert "hello from coder-bot" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_payload_rejects_bad_kind(fresh_test_state):
    client = await _make_client()
    try:
        token, _ = test_addon.generate_link(base_url="https://x.test")
        r = await client.post(
            f"/test/{token}/payload",
            json={"kind": "rogue", "content": "x"},
        )
        assert r.status == 400
        assert "kind" in (await r.json())["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_payload_rejects_non_string_content(fresh_test_state):
    client = await _make_client()
    try:
        token, _ = test_addon.generate_link(base_url="https://x.test")
        r = await client.post(
            f"/test/{token}/payload",
            json={"kind": "json", "content": {"not": "a string"}},
        )
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_payload_size_cap(fresh_test_state):
    client = await _make_client()
    try:
        token, _ = test_addon.generate_link(base_url="https://x.test")
        too_big = "A" * 200_001
        r = await client.post(
            f"/test/{token}/payload",
            json={"kind": "html", "content": too_big},
        )
        assert r.status == 413
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_payload_post_forbidden_without_token(fresh_test_state):
    client = await _make_client()
    try:
        r = await client.post(
            "/test/nope/payload",
            json={"kind": "url", "content": "https://x"},
        )
        assert r.status == 403
    finally:
        await client.close()


# --- target picker (Render / Oracle / Custom) -------------------------


def test_default_active_target_is_lilush(fresh_test_state):
    """No setup → falls back to the bot's own surface."""
    active = test_addon.get_active_target()
    assert active["id"] == "lilush"
    # Name is non-empty.
    assert active["name"]


def test_set_render_and_oracle_urls(fresh_test_state):
    test_addon.set_target_url("render", "https://my-bot.onrender.com")
    test_addon.set_target_url("oracle", "https://bot.example.com")
    assert test_addon.get_target_url("render") == "https://my-bot.onrender.com"
    assert test_addon.get_target_url("oracle") == "https://bot.example.com"


def test_set_target_url_rejects_unknown_slot(fresh_test_state):
    with pytest.raises(ValueError):
        test_addon.set_target_url("lilush", "https://x")
    with pytest.raises(ValueError):
        test_addon.set_target_url("custom", "https://x")


def test_set_active_round_trips(fresh_test_state):
    test_addon.set_target_url("render", "https://r.example")
    test_addon.set_target_url("oracle", "https://o.example")
    test_addon.set_active_target("render")
    assert test_addon.get_active_target()["url"] == "https://r.example"
    test_addon.set_active_target("oracle")
    assert test_addon.get_active_target()["url"] == "https://o.example"
    test_addon.set_active_target("lilush")
    assert test_addon.get_active_target()["id"] == "lilush"


def test_custom_targets_list(fresh_test_state):
    idx_a = test_addon.add_custom_target("https://a.example")
    idx_b = test_addon.add_custom_target("https://b.example", name="Bee")
    assert idx_a == 0 and idx_b == 1
    customs = test_addon.get_custom_targets()
    assert len(customs) == 2
    # Auto-derived name from hostname for the first one.
    assert customs[0]["name"] == "a.example"
    # Explicit name preserved for the second.
    assert customs[1]["name"] == "Bee"

    test_addon.set_active_target("custom:1")
    active = test_addon.get_active_target()
    assert active["id"] == "custom:1"
    assert active["url"] == "https://b.example"
    assert "Bee" in active["name"]


def test_remove_custom_target_falls_back_to_lilush_if_active(fresh_test_state):
    idx = test_addon.add_custom_target("https://gone.example")
    test_addon.set_active_target(f"custom:{idx}")
    assert test_addon.get_active_target_id() == f"custom:{idx}"
    ok = test_addon.remove_custom_target(idx)
    assert ok is True
    # Active should have fallen back to lilush.
    assert test_addon.get_active_target_id() == "lilush"


def test_remove_custom_target_out_of_range(fresh_test_state):
    assert test_addon.remove_custom_target(99) is False


def test_active_target_for_lilush_uses_bot_link(fresh_test_state, monkeypatch):
    for env in (
        "PUBLIC_URL", "RENDER_EXTERNAL_URL", "RAILWAY_PUBLIC_DOMAIN",
        "FLY_APP_NAME", "ORACLE_PUBLIC_HOST", "KEEP_ALIVE_URL",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://bot.live")
    token, _ = test_addon.generate_link()
    active = test_addon.get_active_target()
    assert active["id"] == "lilush"
    assert active["url"] == f"https://bot.live/test/{token}/"
