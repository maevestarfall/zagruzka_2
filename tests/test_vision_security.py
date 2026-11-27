from __future__ import annotations

import pytest

from bot.addons.vision import server as vision


@pytest.fixture()
def memory_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    values: dict[str, object] = {}
    monkeypatch.setattr(vision.addon_state, "get", lambda addon, key, default=None: values.get(key, default))
    monkeypatch.setattr(vision.addon_state, "set_", lambda addon, key, value: values.__setitem__(key, value))
    return values


def test_generate_reuses_safe_metadata_without_leaking_token(memory_state, monkeypatch):
    monkeypatch.setattr(vision.time, "time", lambda: 1_000)
    token, link = vision.generate_link("https://example.test", ttl_seconds=600)

    status = vision.get_status(now=1_001)
    assert link == f"https://example.test/vision/{token}/"
    assert status["active"] is True
    assert status["expires_at"] == 1_600
    assert status["seconds_left"] == 599
    assert token not in repr(status)
    assert len(str(status["fingerprint"])) == 8


def test_rotate_invalidates_old_token(memory_state, monkeypatch):
    monkeypatch.setattr(vision.time, "time", lambda: 2_000)
    old_token, _ = vision.generate_link("", ttl_seconds=600)
    new_token, _ = vision.generate_link("", ttl_seconds=600)

    assert old_token != new_token
    assert vision._check(old_token).status == 403
    assert vision._check(new_token) is None


def test_expired_session_is_rejected(memory_state, monkeypatch):
    monkeypatch.setattr(vision.time, "time", lambda: 3_000)
    token, _ = vision.generate_link("", ttl_seconds=600)
    monkeypatch.setattr(vision.time, "time", lambda: 3_601)

    assert vision.is_expired()
    assert vision.get_status()["active"] is False
    assert vision._check(token).status == 403


def test_revoke_erases_bearer_credential(memory_state, monkeypatch):
    monkeypatch.setattr(vision.time, "time", lambda: 4_000)
    token, _ = vision.generate_link("", ttl_seconds=600)

    assert vision.disable_link() is True
    assert vision.get_token() == ""
    assert vision.get_status()["active"] is False
    assert vision._check(token).status == 403


def test_preflight_recovers_fable5_without_exposing_token(memory_state, monkeypatch):
    class FakeFable5:
        enabled = False
        token = ""

        @classmethod
        def is_enabled(cls):
            return cls.enabled

        @classmethod
        def get_token(cls):
            return cls.token

        @classmethod
        def generate_link(cls, base=""):
            cls.enabled = True
            cls.token = "secret-backend-token"
            return cls.token, f"{base}/fable5/{cls.token}"

    import bot.addons.fable5.server as fable5
    monkeypatch.setattr(fable5, "is_enabled", FakeFable5.is_enabled)
    monkeypatch.setattr(fable5, "get_token", FakeFable5.get_token)
    monkeypatch.setattr(fable5, "generate_link", FakeFable5.generate_link)

    result = vision.preflight(recover=True)
    assert result["ok"] is True
    assert result["backend"] == "ready"
    assert result["recovered"] is True
    assert "secret-backend-token" not in repr(result)
