"""Tests for the API-debater feature and the join-link preview-fetch fix.

Covers:
* api_runner pure helpers (envelope extraction, endpoint resolution, summary)
* api_runner.run_one_turn happy path + validation-retry path (mocked HTTP)
* viktor_v3 slot helpers + isolation from Brain/door configs
* debate_server._is_preview_fetch guard (the "slot A постоянно занят" bug)
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import respx

from bot.addons.debates import api_runner


# ─── pure helpers ─────────────────────────────────────────────────────────────


def test_chat_completions_url_appends_when_base():
    assert api_runner.chat_completions_url("https://api.openai.com/v1") == (
        "https://api.openai.com/v1/chat/completions")


def test_chat_completions_url_keeps_full_endpoint():
    full = "https://x.ai/v1/chat/completions"
    assert api_runner.chat_completions_url(full) == full
    assert api_runner.chat_completions_url(full + "/") == full


def test_extract_envelope_fenced():
    out = api_runner.extract_envelope(
        'preamble\n```json\n{"kind":"note","content":"hi"}\n``` trailing')
    assert out == {"kind": "note", "content": "hi"}


def test_extract_envelope_bare_object():
    out = api_runner.extract_envelope(
        'sure: {"kind":"atom-propose","content":"x","pick":"A"} done')
    assert out["kind"] == "atom-propose" and out["pick"] == "A"


def test_extract_envelope_rejects_non_envelope_and_junk():
    assert api_runner.extract_envelope("no json here") is None
    assert api_runner.extract_envelope('{"content":"no kind"}') is None


def test_summarize_state_compact():
    state = {
        "current_phase": "atoms_propose", "status": "open", "judge_mode": "self",
        "canonical_categories": ["cat1", "cat2"],
        "atoms": {"cat1": {"versions": [
            {"atom_id": "a1", "v": 1, "author": "debater_a",
             "pick": "A", "locked": False}]}},
        "violations_recent": [{"msg_id": 3, "rule": "R1", "detail": "bad"}],
    }
    s = api_runner.summarize_state(state)
    assert "atoms_propose" in s and "a1" in s and "R1" in s


# ─── run_one_turn (mocked HTTP) ──────────────────────────────────────────────


def _llm_response(envelope_json: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": envelope_json}}]})


@respx.mock
async def test_run_one_turn_happy_path():
    slot = {"api_url": "https://api.test/v1", "api_key": "sk-x", "model": "m"}
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=_llm_response('{"kind":"note","content":"hello"}'))
    respx.post("https://srv/debate/door/D1/validate").mock(
        return_value=httpx.Response(200, json={"ok": True, "violations": []}))
    posted = respx.post("https://srv/debate/door/D1/msg").mock(
        return_value=httpx.Response(200, json={"ok": True, "msg_id": 7}))

    result = await api_runner.run_one_turn(
        base_url="https://srv", door_id="D1", token="tok", debater_id="debater_a",
        slot=slot, role_doc="rules", topic="topic", motivation="go",
        state={"current_phase": "voice_manifest", "status": "open"})

    assert result["ok"] is True
    assert result["envelope_kind"] == "note"
    assert posted.called


@respx.mock
async def test_run_one_turn_validation_retry_then_fail():
    slot = {"api_url": "https://api.test/v1", "api_key": "sk-x", "model": "m"}
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=_llm_response('{"kind":"note","content":"x"}'))
    # validation always returns a violation -> never posts
    respx.post("https://srv/debate/door/D1/validate").mock(
        return_value=httpx.Response(200, json={
            "ok": False, "violations": [{"rule": "BAD", "detail": "nope"}]}))
    msg_route = respx.post("https://srv/debate/door/D1/msg").mock(
        return_value=httpx.Response(200, json={"ok": True}))

    result = await api_runner.run_one_turn(
        base_url="https://srv", door_id="D1", token="tok", debater_id="debater_a",
        slot=slot, role_doc="r", topic="t", motivation="m",
        state={"current_phase": "p", "status": "open"}, max_retries=1)

    assert result["ok"] is False
    assert result["stage"] == "validate"
    assert not msg_route.called  # invalid envelope never posted


@respx.mock
async def test_run_one_turn_llm_error_surfaces():
    slot = {"api_url": "https://api.test/v1", "model": "m"}
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, text="unauthorized"))
    result = await api_runner.run_one_turn(
        base_url="https://srv", door_id="D1", token="t", debater_id="d",
        slot=slot, role_doc="r", topic="t", motivation="m",
        state={"current_phase": "p", "status": "open"})
    assert result["ok"] is False and result["stage"] == "llm"


# ─── slot helpers + isolation ────────────────────────────────────────────────


def test_slot_helpers_and_isolation(monkeypatch, tmp_path):
    from bot.addons.debates import viktor_v3 as v3

    # Seed a door config + (simulated) brain data, then mutate an API slot and
    # assert the door/brain side is untouched.
    v3._door_set_server(1, "https://srv", "owntok", "srv1")
    v3._door_set_tz(1, "tz_x")
    before_door = dict(v3._door_cfg(1))

    slot = v3._api_debater_slot(2)
    assert slot == {}
    slot.update({"api_url": "https://a/v1", "api_key": "k", "model": "m",
                 "door": 1})
    v3._api_debater_set(2, slot)

    got = v3._api_debater_slot(2)
    assert got["api_url"] == "https://a/v1" and got["model"] == "m"
    assert v3._api_debaters_occupied_count() >= 1
    # Door config must be byte-identical — API slot is isolated.
    assert v3._door_cfg(1) == before_door

    v3._api_debater_clear(2)
    assert v3._api_debater_slot(2) == {}


# ─── join preview-fetch guard (the reported bug) ─────────────────────────────


def _req(method: str, ua: str = "python-httpx/0.27"):
    return SimpleNamespace(method=method, headers={"User-Agent": ua})


def test_preview_fetch_blocks_head_and_crawlers():
    from bot.addons.debates import debate_server as ds
    # Telegram + other link-preview crawlers must NOT claim a slot.
    assert ds._is_preview_fetch(_req("GET", "TelegramBot (like TwitterBot)"))
    assert ds._is_preview_fetch(_req("GET", "facebookexternalhit/1.1"))
    assert ds._is_preview_fetch(_req("GET", "Slackbot-LinkExpanding 1.0"))
    assert ds._is_preview_fetch(_req("HEAD", "Mozilla/5.0"))


def test_real_debater_get_is_not_preview():
    from bot.addons.debates import debate_server as ds
    # A normal programmatic GET from an AI client claims a slot as before.
    assert not ds._is_preview_fetch(_req("GET", "python-httpx/0.27"))
    assert not ds._is_preview_fetch(_req("GET", "openai-python/1.55"))
