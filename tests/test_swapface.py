"""SwapFace addon tests (ТЗ SwapFace v2.0 §12).

Covers: state CRUD/validation, reply parser, §5.5 correlation, the
REST retry policy, service.swap_image happy-path/timeout/idempotency/
single-flight, the media pipeline gate, and the on_message_edit catch
in watch.py.

Storage isolation: ``bot.addons.state`` piggybacks on the live
``storage`` singleton, so we point ``DATA_DIR`` at a tmp dir (session
conftest also snapshots state.json) and wipe the ``swapface`` bucket
before each test.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from bot.addons import state as addon_state
from bot.addons.swapface import discord_rest, parser, service
from bot.addons.swapface import state as sw_state


@pytest.fixture(autouse=True)
def _clean_swapface_bucket():
    """Reset the swapface settings bucket around every test."""
    root = addon_state._root()  # noqa: SLF001
    root.pop("swapface", None)
    yield
    root.pop("swapface", None)


# ── state: CRUD + validation ────────────────────────────────────────────────

def test_idname_validation_boundaries():
    assert sw_state.validate_idname("a") is None            # 1 char ok
    assert sw_state.validate_idname("abcde12345") is None   # 10 chars ok
    assert sw_state.validate_idname("abcde123456") is not None  # 11 rejected
    assert sw_state.validate_idname("") is not None
    assert sw_state.validate_idname("bad name") is not None     # space
    assert sw_state.validate_idname("bad-name") is not None     # dash
    assert sw_state.validate_idname("emoji😀") is not None


def test_composite_validation():
    assert sw_state.validate_composite("a+b") is None
    assert sw_state.validate_composite("a,b") is None
    assert sw_state.validate_composite("a+b+c") is None
    assert sw_state.validate_composite("a++b") is not None
    assert sw_state.validate_composite("toolongidname1+b") is not None


def test_face_crud_and_duplicates():
    ok, warn = sw_state.add_face("alice", "swapface/faces/alice.jpg")
    assert ok and warn is None
    # duplicate rejected
    ok2, err = sw_state.add_face("alice", "x")
    assert not ok2 and err
    # invalid idname rejected before storage
    ok3, err3 = sw_state.add_face("bad name", "x")
    assert not ok3 and err3
    assert {f["idname"] for f in sw_state.get_faces()} == {"alice"}

    sw_state.set_active_face("alice")
    assert sw_state.get_active_face() == "alice"
    # removing the active face clears active
    assert sw_state.remove_face("alice") is True
    assert sw_state.get_active_face() is None
    assert sw_state.remove_face("ghost") is False


def test_soft_cap_warns_but_adds():
    for i in range(sw_state.MAX_FACES_SOFT):
        ok, warn = sw_state.add_face(f"f{i}", "x")
        assert ok and warn is None
    ok, warn = sw_state.add_face("overflow", "x")
    assert ok and warn  # added, but warned


def test_mode_and_toggles():
    assert sw_state.get_mode() == "manual"          # default
    sw_state.set_mode("usertoken")
    assert sw_state.get_mode() == "usertoken"
    with pytest.raises(ValueError):
        sw_state.set_mode("nope")

    assert sw_state.enabled_photo() is False
    sw_state.set_enabled_photo(True)
    assert sw_state.enabled_photo() is True
    sw_state.set_enabled_video(True)
    assert sw_state.enabled_video() is True


def test_credit_state():
    c = sw_state.get_credits()
    assert c["remaining"] is None
    assert c["daily_limit"] == sw_state.DEFAULT_DAILY_LIMIT
    sw_state.set_credits_remaining(2)
    assert sw_state.get_credits()["remaining"] == 2
    assert sw_state.credits_low() is True           # 2 < 3
    sw_state.set_credits_remaining(40)
    assert sw_state.credits_low() is False


def test_user_token_never_persisted(monkeypatch):
    monkeypatch.setenv("SWAPFACE_DISCORD_USER_TOKEN", "secret-token")
    assert sw_state.get_user_token() == "secret-token"
    # token value must never land in the settings bucket
    bucket = addon_state._root().get("swapface", {})  # noqa: SLF001
    assert "secret-token" not in str(bucket)


# ── parser ──────────────────────────────────────────────────────────────────

def _link_button(url):
    return {"type": 1, "components": [{"type": 2, "style": 5, "url": url}]}


def test_parser_done_with_link_buttons():
    msg = {
        "id": "111", "channel_id": "222",
        "content": "Done! 47 credits left",
        "components": [_link_button("https://cdn.example/result1.png")],
        "attachments": [],
    }
    p = parser.parse_reply(msg)
    assert p.status == parser.STATUS_DONE
    assert p.image_urls == ["https://cdn.example/result1.png"]
    assert p.credits_remaining == 47


def test_parser_fallback_to_attachments():
    msg = {
        "id": "111", "channel_id": "222", "content": "",
        "components": [],
        "attachments": [{"id": "9", "url": "https://cdn/x.png", "filename": "x.png"}],
    }
    p = parser.parse_reply(msg)
    assert p.status == parser.STATUS_DONE
    assert p.attachment_refs[0]["attachment_id"] == "9"
    assert p.attachment_refs[0]["message_id"] == "111"


def test_parser_in_progress_vs_limit():
    assert parser.parse_reply({"content": "processing your swap…"}).status == (
        parser.STATUS_IN_PROGRESS
    )
    limit = parser.parse_reply(
        {"content": "You have no credits, daily limit reached (0 credits left)"}
    )
    assert limit.status == parser.STATUS_LIMIT
    assert limit.credits_remaining == 0


def test_parser_html_og_image():
    html = '<html><meta property="og:image" content="https://x/full.png"></html>'
    assert parser.extract_image_url_from_html(html) == "https://x/full.png"
    html2 = '<img src="https://y/first.jpg" />'
    assert parser.extract_image_url_from_html(html2) == "https://y/first.jpg"


def test_parse_credits_variants():
    assert parser.parse_credits("осталось 12 кредитов") == 12
    assert parser.parse_credits("credits: none") is None
    assert parser.parse_credits("47 credits remaining") == 47


# ── correlation §5.5 ─────────────────────────────────────────────────────────

def test_correlation_interaction_metadata_match():
    msg = {"interaction_metadata": {"id": "abc"}}
    assert parser.matches_interaction(msg, interaction_id="abc")
    assert not parser.matches_interaction(msg, interaction_id="zzz")


def test_correlation_legacy_interaction_id():
    msg = {"interaction": {"id": "leg"}}
    assert parser.matches_interaction(msg, interaction_id="leg")


def test_correlation_nonce_fallback():
    msg = {"nonce": "n-123"}
    assert parser.matches_interaction(msg, nonce="n-123")


def test_correlation_semi_manual_branch():
    now = time.time()
    msg = {
        "author": {"id": "INSIGHT"},
        "interaction_metadata": {"id": "", "user": {"id": "OWNER"}},
        "_timestamp": now + 1,
    }
    assert parser.matches_interaction(
        msg, insight_app_id="INSIGHT", invoker_discord_id="OWNER",
        window_start=now,
    )


def test_correlation_negative_foreign_swap():
    """A parallel/foreign swap in the same channel must NOT match."""
    now = time.time()
    foreign = {
        "author": {"id": "INSIGHT"},
        "interaction_metadata": {"id": "", "user": {"id": "SOMEONE_ELSE"}},
        "_timestamp": now + 1,
    }
    assert not parser.matches_interaction(
        foreign, insight_app_id="INSIGHT", invoker_discord_id="OWNER",
        window_start=now,
    )
    # author-only is never enough
    assert not parser.matches_interaction({"author": {"id": "INSIGHT"}})


# ── discord_rest retry policy ────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status, json_body=None, headers=None):
        self.status_code = status
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in recording calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def aclose(self):
        pass


async def test_rest_401_no_retry():
    client = _FakeClient([_FakeResp(401)])
    rest = discord_rest.DiscordRest("tok", is_bot=False, client=client)
    with pytest.raises(discord_rest.DiscordAuthError):
        await rest.request("GET", "/users/@me")
    assert len(client.calls) == 1  # no retry


async def test_rest_429_then_success():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    client = _FakeClient([
        _FakeResp(429, {"retry_after": 0.25}),
        _FakeResp(200, {"ok": True}),
    ])
    rest = discord_rest.DiscordRest("tok", is_bot=True, client=client, sleep=fake_sleep)
    resp = await rest.request("GET", "/x")
    assert resp.status_code == 200
    assert slept == [0.25]           # exactly one wait honoring Retry-After
    assert len(client.calls) == 2


async def test_rest_5xx_backoff_then_stop():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    client = _FakeClient([_FakeResp(500), _FakeResp(500), _FakeResp(500)])
    rest = discord_rest.DiscordRest("tok", is_bot=True, client=client, sleep=fake_sleep)
    with pytest.raises(discord_rest.DiscordRestError):
        await rest.request("GET", "/x")
    assert len(client.calls) == discord_rest.MAX_ATTEMPTS
    assert len(slept) == discord_rest.MAX_ATTEMPTS - 1


def test_rest_masks_authorization():
    masked = discord_rest.mask_headers({"Authorization": "Bot secret", "X": "1"})
    assert masked["Authorization"] == "***"
    assert masked["X"] == "1"


def test_build_interaction_payload_shape():
    payload = discord_rest.build_interaction_payload(
        app_id="APP", guild_id="G", channel_id="C",
        command={"id": "cid", "version": "v1"},
        command_name="swapid", options=[{"name": "idname", "value": "alice"}],
    )
    assert payload["type"] == 2
    assert payload["data"]["name"] == "swapid"
    assert payload["data"]["id"] == "cid"
    assert payload["nonce"]


def test_find_insight_commands():
    index = {
        "applications": [{"id": "INSIGHT"}],
        "application_commands": [
            {"application_id": "INSIGHT", "name": "swapid", "id": "1", "version": "10"},
            {"application_id": "INSIGHT", "name": "saveid", "id": "2", "version": "20"},
            {"application_id": "OTHER", "name": "help", "id": "9", "version": "1"},
        ],
    }
    app_id, cmds = discord_rest.find_insight_commands(index)
    assert app_id == "INSIGHT"
    assert set(cmds) == {"swapid", "saveid"}


# ── service.swap_image: happy path / idempotency / single-flight ─────────────

def _configure_ready(tmp_path, monkeypatch):
    """Wire up a ready state: channel + active face + tmp storage."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sw_state.set_channel_id("999")
    sw_state.add_face("alice", "swapface/faces/alice.jpg")
    sw_state.set_active_face("alice")
    sw_state.set_mode("manual")


async def test_swap_image_happy_path(tmp_path, monkeypatch):
    _configure_ready(tmp_path, monkeypatch)

    done_message = {
        "id": "m1", "channel_id": "999", "content": "Done! 44 credits left",
        "components": [_link_button("https://cdn/res.png")],
        "attachments": [],
    }

    async def fake_run_swap(frame, idname, mode, *, invoker_discord_id):
        return done_message

    async def fake_download(parsed):
        return [b"IMG-A", b"IMG-B"]

    monkeypatch.setattr(service, "_run_swap", fake_run_swap)
    monkeypatch.setattr(service, "download_results", fake_download)

    result = await service.swap_image(b"frame-bytes")
    assert result.variants == [b"IMG-A", b"IMG-B"]
    assert result.credits_remaining == 44
    assert len(result.files) == 2
    assert not result.from_cache


async def test_swap_image_idempotent_no_second_post(tmp_path, monkeypatch):
    _configure_ready(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def fake_run_swap(frame, idname, mode, *, invoker_discord_id):
        calls["n"] += 1
        return {
            "id": "m", "channel_id": "999", "content": "Done 40 credits",
            "components": [_link_button("https://cdn/a.png")], "attachments": [],
        }

    async def fake_download(parsed):
        return [b"ONE"]

    monkeypatch.setattr(service, "_run_swap", fake_run_swap)
    monkeypatch.setattr(service, "download_results", fake_download)

    r1 = await service.swap_image(b"same-frame")
    r2 = await service.swap_image(b"same-frame")
    assert calls["n"] == 1          # second call served from cache
    assert r2.from_cache is True
    assert r1.variants == r2.variants


async def test_swap_image_single_flight_serialized(tmp_path, monkeypatch):
    _configure_ready(tmp_path, monkeypatch)
    concurrent = {"cur": 0, "max": 0}

    async def fake_run_swap(frame, idname, mode, *, invoker_discord_id):
        concurrent["cur"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["cur"])
        await asyncio.sleep(0.05)
        concurrent["cur"] -= 1
        return {
            "id": "m", "channel_id": "999", "content": "Done",
            "components": [_link_button("https://cdn/a.png")], "attachments": [],
        }

    async def fake_download(parsed):
        return [b"X"]

    monkeypatch.setattr(service, "_run_swap", fake_run_swap)
    monkeypatch.setattr(service, "download_results", fake_download)

    # different frames → no dedup shortcut → both hit _run_swap
    await asyncio.gather(
        service.swap_image(b"frame-1"),
        service.swap_image(b"frame-2"),
    )
    assert concurrent["max"] == 1   # serialized by the per-channel lock


async def test_swap_image_timeout_propagates(tmp_path, monkeypatch):
    _configure_ready(tmp_path, monkeypatch)

    async def boom(frame, idname, mode, *, invoker_discord_id):
        raise service.SwapFaceError("InsightFaceSwap не ответил за 120 сек.")

    monkeypatch.setattr(service, "_run_swap", boom)
    with pytest.raises(service.SwapFaceError):
        await service.swap_image(b"frame")


async def test_swap_image_requires_active_face(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sw_state.set_channel_id("999")
    # no active face
    with pytest.raises(service.SwapFaceError):
        await service.swap_image(b"frame")


# ── media pipeline gate (§9.3) ───────────────────────────────────────────────

def test_readiness_disabled_short_circuits():
    ok, reason = service.readiness("photo")
    assert ok is False and "выключен" in reason.lower()


def test_readiness_enabled_but_unconfigured():
    sw_state.set_enabled_photo(True)
    ok, reason = service.readiness("photo")
    assert ok is False                      # no channel/face yet


def test_readiness_ready(tmp_path, monkeypatch):
    _configure_ready(tmp_path, monkeypatch)
    sw_state.set_enabled_video(True)
    ok, reason = service.readiness("video")
    assert ok is True


# ── watch.py: on_message_edit catch ──────────────────────────────────────────

class _FakeGateway:
    def __init__(self):
        self._cbs = {}

    def register_message_listener(self, channel_id, cb):
        self._cbs.setdefault(int(channel_id), []).append(cb)

    def unregister_message_listener(self, channel_id, cb):
        self._cbs.get(int(channel_id), []).remove(cb)

    async def emit(self, channel_id, message, event):
        for cb in list(self._cbs.get(int(channel_id), [])):
            await cb(message, event=event)


async def test_watch_catches_result_via_edit(tmp_path, monkeypatch):
    from bot.addons.swapface import watch

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sw_state.set_channel_id("555")
    sw_state.set_command_index("INSIGHT", {})   # app id for §5.5.4 fallback

    gw = _FakeGateway()
    now = time.time()

    async def driver():
        # first the placeholder arrives as create (in-progress → ignored)
        await asyncio.sleep(0.01)
        await gw.emit("555", {
            "id": "M", "channel_id": "555", "content": "processing…",
            "author": {"id": "INSIGHT"},
            "nonce": "N1", "_timestamp": now + 0.1,
        }, "create")
        # then the SAME message is edited into the done state
        await asyncio.sleep(0.01)
        await gw.emit("555", {
            "id": "M", "channel_id": "555", "content": "Done 30 credits",
            "author": {"id": "INSIGHT"},
            "components": [_link_button("https://cdn/final.png")],
            "attachments": [], "nonce": "N1", "_timestamp": now + 0.2,
        }, "edit")

    task = asyncio.create_task(driver())
    result = await watch.wait_for_reply(
        timeout=2.0, gw=gw,
    )
    await task
    parsed = parser.parse_reply(result)
    assert parsed.status == parser.STATUS_DONE
    assert parsed.image_urls == ["https://cdn/final.png"]


# ── §5.4 interactive variant picker + §5.6 credits preflight ───────────────

class _FakeChat:
    def __init__(self, chat_id: int = 777) -> None:
        self.id = chat_id


class _FakeMessage:
    """Minimal aiogram Message stand-in for handler-level tests."""

    def __init__(self, chat_id: int = 777) -> None:
        self.chat = _FakeChat(chat_id)
        self.sent: list[dict] = []

    async def answer(self, text, **kw):  # noqa: ANN001
        self.sent.append({"kind": "text", "text": text, **kw})
        return self

    async def answer_photo(self, photo, **kw):  # noqa: ANN001
        self.sent.append({"kind": "photo", "photo": photo, **kw})
        return self


async def test_choose_variant_returns_picked_bytes():
    from bot.addons.swapface import handlers as ui

    msg = _FakeMessage()
    v1, v2 = b"variant-one", b"variant-two"

    async def picker():
        await asyncio.sleep(0.02)
        ui._resolve_choice(msg.chat.id, "2")  # noqa: SLF001

    task = asyncio.create_task(picker())
    chosen = await ui.choose_variant(msg, [v1, v2], timeout=1.0)
    await task
    assert chosen == v2
    # both variants delivered, picker keyboard attached to the last one
    photos = [s for s in msg.sent if s["kind"] == "photo"]
    assert len(photos) == 2
    assert photos[-1].get("reply_markup") is not None


async def test_choose_variant_skip_returns_none():
    from bot.addons.swapface import handlers as ui

    msg = _FakeMessage()

    async def picker():
        await asyncio.sleep(0.02)
        ui._resolve_choice(msg.chat.id, "skip")  # noqa: SLF001

    task = asyncio.create_task(picker())
    chosen = await ui.choose_variant(msg, [b"a", b"b"], timeout=1.0)
    await task
    assert chosen is None


async def test_choose_variant_timeout_falls_back_to_first():
    from bot.addons.swapface import handlers as ui

    msg = _FakeMessage()
    chosen = await ui.choose_variant(msg, [b"a", b"b"], timeout=0.05)
    assert chosen == b"a"


async def test_confirm_low_credits_cancel_and_proceed():
    from bot.addons.swapface import handlers as ui

    msg = _FakeMessage()

    async def cancel():
        await asyncio.sleep(0.02)
        ui._resolve_choice(msg.chat.id, "cancel")  # noqa: SLF001

    task = asyncio.create_task(cancel())
    assert await ui.confirm_low_credits(msg, 2, timeout=1.0) is False
    await task

    async def go():
        await asyncio.sleep(0.02)
        ui._resolve_choice(msg.chat.id, "go")  # noqa: SLF001

    task = asyncio.create_task(go())
    assert await ui.confirm_low_credits(msg, 2, timeout=1.0) is True
    await task


async def test_confirm_low_credits_timeout_proceeds():
    from bot.addons.swapface import handlers as ui

    msg = _FakeMessage()
    assert await ui.confirm_low_credits(msg, 1, timeout=0.05) is True


def test_credits_low_thresholds():
    sw_state.set_credits_remaining(2)
    assert sw_state.credits_low() is True
    sw_state.set_credits_remaining(50)
    assert sw_state.credits_low() is False
    # unknown remaining → do not block the pipeline
    root = addon_state._root()  # noqa: SLF001
    root.get("swapface", {}).pop("credits", None)
    assert sw_state.credits_low() is False


def test_photo_video_keyboards_expose_toggle_and_settings():
    from bot import media_ui

    for kind in ("photo", "video"):
        row = media_ui._swapface_toggle_row(kind)  # noqa: SLF001
        callbacks = [button.callback_data for button in row]
        assert f"photo:swf:{kind}" in callbacks
        assert "swf:menu" in callbacks


async def test_manual_delete_posts_delid_hint_then_removes(tmp_path, monkeypatch):
    from bot.gateway.adapters import discord_gw

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sw_state.set_channel_id("777")
    sw_state.set_mode("manual")
    sw_state.add_face("alice", "swapface/faces/alice.jpg")
    face_path = sw_state.faces_dir() / "alice.jpg"
    face_path.write_bytes(b"face")
    sent = []

    async def fake_send(channel_id, content):
        sent.append((channel_id, content))
        return True

    monkeypatch.setattr(discord_gw, "send_channel_text", fake_send)
    await service.delete_face("alice")

    assert sent and sent[0][0] == 777
    assert "/delid idname:alice" in sent[0][1]
    assert sw_state.get_face("alice") is None
    assert not face_path.exists()


async def test_manual_delete_keeps_face_when_discord_delivery_fails(tmp_path, monkeypatch):
    from bot.gateway.adapters import discord_gw

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sw_state.set_channel_id("777")
    sw_state.set_mode("manual")
    sw_state.add_face("alice", "swapface/faces/alice.jpg")

    async def fake_send(channel_id, content):
        return False

    monkeypatch.setattr(discord_gw, "send_channel_text", fake_send)
    with pytest.raises(service.SwapFaceError):
        await service.delete_face("alice")
    assert sw_state.get_face("alice") is not None
