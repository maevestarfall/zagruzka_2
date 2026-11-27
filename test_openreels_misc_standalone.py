"""Standalone tests for the OpenReels client + new misc/publication wiring.

Run with:  python3 -m pytest -c /dev/null -p no:cacheprovider -v \\
               test_openreels_misc_standalone.py
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import os

import httpx

os.environ.setdefault("DATA_DIR", "/tmp/lilush_openreels_test")

from bot import openreels as orl  # noqa: E402


@contextlib.contextmanager
def mock_httpx(handler):
    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    @functools.wraps(orig)
    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    httpx.AsyncClient = patched  # type: ignore[assignment]
    try:
        yield
    finally:
        httpx.AsyncClient = orig  # type: ignore[assignment]


def test_find_mp4_recursive():
    assert orl._find_mp4({"data": {"output": {"video": "/files/x.mp4"}}}) == "/files/x.mp4"
    assert orl._find_mp4({"a": [{"url": "https://cdn/v.MOV"}]}) == "https://cdn/v.MOV"
    assert orl._find_mp4({"nope": 1}) == ""


def test_generate_submit_and_poll(monkeypatch):
    monkeypatch.setattr(orl, "base_url", lambda: "http://openreels.local")
    monkeypatch.setattr(orl, "api_key", lambda: "")
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "POST" and p == "/api/v1/jobs":
            return httpx.Response(200, json={"id": "job123", "status": "queued"})
        if request.method == "GET" and p == "/api/v1/jobs/job123":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"status": "running", "stage": "voiceover"})
            return httpx.Response(200, json={
                "status": "completed",
                "output": {"video": "https://openreels.local/outputs/job123.mp4"},
            })
        return httpx.Response(404, text="nope")

    with mock_httpx(handler):
        res = asyncio.run(orl.generate("apollo 13", platform="youtube", poll_interval=0.0))
    assert res["job_id"] == "job123"
    assert res["status"] == "completed"
    assert res["video_url"] == "https://openreels.local/outputs/job123.mp4"
    assert state["polls"] >= 2


def test_generate_submit_fallback_endpoint(monkeypatch):
    """First endpoint 404s; client falls back to /api/jobs."""
    monkeypatch.setattr(orl, "base_url", lambda: "http://or.local")
    monkeypatch.setattr(orl, "api_key", lambda: "")

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.method == "POST" and p == "/api/v1/jobs":
            return httpx.Response(404, text="not here")
        if request.method == "POST" and p == "/api/jobs":
            return httpx.Response(201, json={"jobId": "j9"})
        if p == "/api/jobs/j9" or p == "/api/v1/jobs/j9":
            return httpx.Response(200, json={"status": "done", "videoUrl": "http://or.local/v/j9.mp4"})
        return httpx.Response(404)

    with mock_httpx(handler):
        res = asyncio.run(orl.generate("x", poll_interval=0.0))
    assert res["job_id"] == "j9"
    assert res["video_url"] == "http://or.local/v/j9.mp4"


def test_generate_failure_status(monkeypatch):
    monkeypatch.setattr(orl, "base_url", lambda: "http://or.local")
    monkeypatch.setattr(orl, "api_key", lambda: "")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "jf"})
        return httpx.Response(200, json={"status": "failed", "error": "no credits"})

    with mock_httpx(handler):
        try:
            asyncio.run(orl.generate("x", poll_interval=0.0))
        except orl.OpenReelsError as exc:
            assert "no credits" in str(exc)
            return
    raise AssertionError("expected OpenReelsError on failed job")


def test_download_to(tmp_path, monkeypatch):
    monkeypatch.setattr(orl, "api_key", lambda: "")
    dest = str(tmp_path / "out.mp4")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".mp4"):
            return httpx.Response(200, content=b"FAKEMP4BYTES",
                                  headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    with mock_httpx(handler):
        out = asyncio.run(orl.download_to(dest, "http://or.local/v/x.mp4", []))
    assert out == dest
    assert open(dest, "rb").read() == b"FAKEMP4BYTES"


def test_health_ok(monkeypatch):
    monkeypatch.setattr(orl, "base_url", lambda: "http://or.local")
    monkeypatch.setattr(orl, "api_key", lambda: "")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with mock_httpx(handler):
        ok, msg = asyncio.run(orl.health())
    assert ok is True


# ---- publication store + wizard parser ----


def test_publication_store_openreels_roundtrip():
    from bot.addons.publication import store
    try:
        store.set_openreels_url("http://localhost:3000/")
        assert store.get_openreels_url() == "http://localhost:3000"  # trailing slash trimmed
        store.set_openreels_mode("generate_only")
        assert store.get_openreels_mode() == "generate_only"
        store.set_openreels_platform("tiktok")
        assert store.get_openreels_platform() == "tiktok"
    finally:
        store.set_openreels_url("")
        store.set_openreels_mode("generate_then_publish")
        store.set_openreels_platform("youtube")


def test_wizard_macro_parser():
    import bot.wizard as wz
    steps = wz._parse_macro_from_llm(
        '[{"op":"goto","url":"https://a.io"},'
        '{"op":"type","text":"hi","field":"q"},'
        '{"op":"click","target":"Go"},'
        '{"op":"copy","selector":".r"}]'
    )
    assert steps[0] == ("goto", "https://a.io")
    assert steps[1] == ("type", ("hi", "q"))
    assert ("copy", ".r") in steps
