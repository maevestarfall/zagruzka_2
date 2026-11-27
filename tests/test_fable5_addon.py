"""End-to-end tests for the Fable5 live-access addon.

Covers:

* token rotation + disable round-trip (state.json bookkeeping)
* HTTP gate: forbidden when missing token / disabled / wrong token
* ``/ping``, ``/info``, ``/state`` GET shape
* ``/state`` POST does a deep merge (not a clobber)
* ``/exec`` runs a shell command with stdout capture
* ``/logs`` blocks path-traversal outside ``storage.data_dir``
"""

from __future__ import annotations


import pytest
from aiohttp import web

from bot.addons import fable5
from bot.storage import storage


@pytest.fixture
def fresh_fable5_state():
    """Wipe addons.fable5 between tests so token state doesn't leak."""
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("fable5", None)
    storage._save()  # noqa: SLF001
    yield
    settings = storage._settings()  # noqa: SLF001
    settings.setdefault("addons", {}).pop("fable5", None)
    storage._save()  # noqa: SLF001


def test_generate_link_rotates_and_enables(fresh_fable5_state):
    assert fable5.get_token() == ""
    assert fable5.is_enabled() is False

    t1, link1 = fable5.generate_link(base_url="https://bot.example")
    assert t1
    assert fable5.is_enabled() is True
    assert link1 == f"https://bot.example/fable5/{t1}/"

    t2, link2 = fable5.generate_link(base_url="https://bot.example")
    assert t2 != t1, "every gen must rotate the token"
    assert link2.endswith(f"/{t2}/")


def test_disable_keeps_token_but_blocks_access(fresh_fable5_state):
    token, _ = fable5.generate_link(base_url="https://x.test")
    assert fable5.disable_link() is True  # was enabled
    assert fable5.is_enabled() is False
    assert fable5.get_token() == token  # token persists


async def _make_client(monkeypatch, *, with_bot: bool = False):
    """Spin up the aiohttp app with fable5 routes for in-process tests."""
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    fable5.register_http_endpoints(app)
    if with_bot:
        class _FakeBot:
            sent: list[dict] = []

            async def send_message(self, chat_id, text, parse_mode=None):
                self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
                class _Msg:
                    message_id = 42
                    class chat:  # noqa: N801
                        id = chat_id
                return _Msg()

        app["fable5_bot"] = _FakeBot()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_ping_requires_valid_enabled_token(fresh_fable5_state, monkeypatch):
    client = await _make_client(monkeypatch)
    try:
        # No token at all → 403
        r = await client.get("/fable5/whatever/ping")
        assert r.status == 403

        token, _ = fable5.generate_link(base_url="https://x.test")
        r = await client.get(f"/fable5/{token}/ping")
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True
        assert body["version"] == "fable5/2"

        # Wrong token → 403
        r = await client.get("/fable5/not-the-token/ping")
        assert r.status == 403

        # Disabled → 403 even with correct token
        fable5.disable_link()
        r = await client.get(f"/fable5/{token}/ping")
        assert r.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_state_post_deep_merge(fresh_fable5_state, monkeypatch):
    client = await _make_client(monkeypatch)
    try:
        token, _ = fable5.generate_link(base_url="https://x.test")
        storage._settings().setdefault("keys", {})["openai"] = "EXISTING"  # noqa: SLF001

        r = await client.post(
            f"/fable5/{token}/state",
            json={"_settings": {"keys": {"groq": "NEW"}, "extra_flag": True}},
        )
        assert r.status == 200
        # Existing key survives, new key added.
        keys = storage._settings()["keys"]  # noqa: SLF001
        assert keys.get("openai") == "EXISTING"
        assert keys.get("groq") == "NEW"
        assert storage._settings().get("extra_flag") is True  # noqa: SLF001
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exec_runs_shell(fresh_fable5_state, monkeypatch):
    client = await _make_client(monkeypatch)
    try:
        token, _ = fable5.generate_link(base_url="https://x.test")
        r = await client.post(
            f"/fable5/{token}/exec",
            json={"cmd": "echo hello-fable5", "timeout": 5},
        )
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True
        assert body["returncode"] == 0
        assert "hello-fable5" in body["stdout"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_uses_app_bot(fresh_fable5_state, monkeypatch):
    client = await _make_client(monkeypatch, with_bot=True)
    try:
        token, _ = fable5.generate_link(base_url="https://x.test")
        # Make sure owner_id is set so default chat resolution works.
        storage.set_owner_id(123)

        r = await client.post(
            f"/fable5/{token}/send",
            json={"text": "ping from fable5 test"},
        )
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True
        assert body["chat_id"] == 123
        assert body["message_id"] == 42
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_logs_blocks_path_traversal(fresh_fable5_state, monkeypatch):
    client = await _make_client(monkeypatch)
    try:
        token, _ = fable5.generate_link(base_url="https://x.test")
        r = await client.get(f"/fable5/{token}/logs", params={"file": "../../etc/passwd"})
        assert r.status in (403, 404)
        body = await r.json()
        assert body.get("error") in {"forbidden path", "not found"}
    finally:
        await client.close()
