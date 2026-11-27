"""Standalone tests for the universal custom-request media provider.

Run with:  python3 -m pytest -c /dev/null -p no:cacheprovider -v \\
               test_media_custom_standalone.py

Kept outside ``tests/`` so the repo's session conftest (which needs the
full bot dependency stack) is not loaded. Async cases use ``asyncio.run``
so no pytest-asyncio plugin is required.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import os

import httpx

os.environ.setdefault("DATA_DIR", "/tmp/lilush_media_custom_test")

from bot import media_custom as mc  # noqa: E402


# ---------------------------------------------------------------------------
# cURL parsing
# ---------------------------------------------------------------------------


def test_parse_curl_json_multiline():
    curl = """curl https://api.example.com/v1/generate \\
      -H 'Authorization: Bearer sk-abc123' \\
      -H 'Content-Type: application/json' \\
      -d '{"model": "my-model", "prompt": "a cat", "image": "DATA"}'"""
    t = mc.parse_curl(curl)
    assert t["method"] == "POST"
    assert t["url"] == "https://api.example.com/v1/generate"
    assert t["headers"]["Authorization"] == "Bearer sk-abc123"
    assert t["body_type"] == "json"
    assert json.loads(t["body"])["model"] == "my-model"


def test_parse_curl_explicit_method_and_url_flag():
    t = mc.parse_curl("curl -X GET --url https://api.x.io/status -H 'X-Key: 1'")
    assert t["method"] == "GET"
    assert t["url"] == "https://api.x.io/status"
    assert t["headers"]["X-Key"] == "1"


def test_parse_curl_basic_auth_to_header():
    t = mc.parse_curl("curl https://api.x.io/p -u user:pass -d 'x=1'")
    assert t["headers"]["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(t["headers"]["Authorization"].split()[1]).decode()
    assert decoded == "user:pass"


def test_parse_curl_form():
    t = mc.parse_curl("curl https://api.x.io/p -F 'prompt=hi' -F 'n=2'")
    assert t["body_type"] == "form"
    assert "prompt=hi" in t["body"]


def test_parse_curl_missing_url_raises():
    try:
        mc.parse_curl("curl -H 'X: 1' -d '{}'")
    except mc.CustomMediaError:
        return
    raise AssertionError("expected CustomMediaError for missing URL")


# ---------------------------------------------------------------------------
# substitution
# ---------------------------------------------------------------------------


def test_substitute_str_basic_and_unknown():
    ctx = {"PROMPT": "hello", "MODEL": "m1"}
    assert mc.substitute_str("p={{PROMPT}} m={{MODEL}}", ctx) == "p=hello m=m1"
    # unknown token preserved
    assert mc.substitute_str("x={{NOPE}}", ctx) == "x={{NOPE}}"


def test_substitute_obj_keeps_native_types():
    ctx = {"WIDTH": 768, "PROMPT": "hi", "IMAGE_B64": "B64DATA"}
    tpl = {"width": "{{WIDTH}}", "prompt": "{{PROMPT}}",
           "image": "data:image/jpeg;base64,{{IMAGE_B64}}"}
    out = mc.substitute_obj(tpl, ctx)
    assert out["width"] == 768 and isinstance(out["width"], int)
    assert out["prompt"] == "hi"
    assert out["image"] == "data:image/jpeg;base64,B64DATA"


# ---------------------------------------------------------------------------
# path + url extraction
# ---------------------------------------------------------------------------


def test_get_path_nested_and_array():
    obj = {"data": {"output": [{"url": "https://r/v.mp4"}]}}
    assert mc.get_path(obj, "data.output[0].url") == "https://r/v.mp4"
    assert mc.get_path(obj, "data.missing") is None


def test_auto_find_url_prefers_extension():
    obj = {"thumb": "https://x/p.png", "video": {"url": "https://x/v.mp4"}}
    assert mc.auto_find_url(obj, (".mp4",)) == "https://x/v.mp4"
    assert mc.auto_find_url(obj).startswith("https://")


def test_auto_find_url_data_url():
    assert mc.auto_find_url({"out": "data:image/png;base64,Zm9v"}) == "data:image/png;base64,Zm9v"


# ---------------------------------------------------------------------------
# auto tokenize
# ---------------------------------------------------------------------------


def test_auto_tokenize_recognizes_fields():
    custom = {"body_type": "json",
              "body": json.dumps({"model": "m", "prompt": "x", "image_url": "y"})}
    new, inserted = mc.auto_tokenize(custom)
    body = json.loads(new["body"])
    assert body["prompt"] == "{{PROMPT}}"
    assert body["model"] == "{{MODEL}}"
    assert body["image_url"] == "{{IMAGE_DATA_URL}}"
    assert "{{PROMPT}}" in inserted


def test_auto_tokenize_skips_if_tokens_present():
    custom = {"body_type": "json", "body": json.dumps({"prompt": "{{PROMPT}}"})}
    new, inserted = mc.auto_tokenize(custom)
    assert inserted == []


# ---------------------------------------------------------------------------
# build_request
# ---------------------------------------------------------------------------


def test_build_request_json_with_image():
    custom = {
        "method": "POST",
        "url": "https://api.x.io/g",
        "headers": {"Authorization": "Bearer {{API_KEY}}", "Content-Type": "application/json"},
        "body_type": "json",
        "body": json.dumps({"prompt": "{{PROMPT}}", "image": "{{IMAGE_DATA_URL}}", "w": "{{WIDTH}}"}),
    }
    ctx = mc.build_context(image_bytes=b"img", prompt="hello", api_key="KEY", width=640)
    req = mc.build_request(custom, ctx)
    assert req["headers"]["Authorization"] == "Bearer KEY"
    assert req["json"]["prompt"] == "hello"
    assert req["json"]["w"] == 640
    assert req["json"]["image"].startswith("data:image/jpeg;base64,")


# ---------------------------------------------------------------------------
# run_custom — sync + poll, via httpx MockTransport
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def mock_httpx(handler):
    """Force every httpx.AsyncClient to use a MockTransport(handler)."""
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


def test_run_custom_sync():
    custom = {
        "method": "POST", "url": "https://api.x.io/g",
        "headers": {"Content-Type": "application/json"},
        "body_type": "json", "body": json.dumps({"prompt": "{{PROMPT}}"}),
        "result_path": "images[0].url",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["prompt"] == "make art"
        return httpx.Response(200, json={"images": [{"url": "https://cdn/out.png"}]})

    ctx = mc.build_context(image_bytes=b"i", prompt="make art")
    with mock_httpx(handler):
        url = asyncio.run(mc.run_custom(custom, ctx, task="photo"))
    assert url == "https://cdn/out.png"


def test_run_custom_poll():
    custom = {
        "method": "POST", "url": "https://api.x.io/submit",
        "headers": {"Content-Type": "application/json"},
        "body_type": "json", "body": json.dumps({"prompt": "{{PROMPT}}"}),
        "poll": {
            "url_path": "urls.get",
            "status_path": "status",
            "done_values": ["succeeded"],
            "result_path": "output[0]",
            "interval": 0.0,
            "timeout": 10.0,
        },
    }
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/submit":
            return httpx.Response(200, json={"urls": {"get": "https://api.x.io/poll/1"}, "status": "starting"})
        # poll endpoint
        state["polls"] += 1
        if state["polls"] < 2:
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(200, json={"status": "succeeded", "output": ["https://cdn/v.mp4"]})

    ctx = mc.build_context(image_bytes=b"i", prompt="go")
    with mock_httpx(handler):
        url = asyncio.run(mc.run_custom(custom, ctx, task="video"))
    assert url == "https://cdn/v.mp4"
    assert state["polls"] >= 2


def test_run_custom_http_error():
    custom = {"method": "POST", "url": "https://api.x.io/g",
              "headers": {}, "body_type": "json", "body": "{}"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    ctx = mc.build_context(image_bytes=b"i")
    try:
        with mock_httpx(handler):
            asyncio.run(mc.run_custom(custom, ctx))
    except mc.CustomMediaError as exc:
        assert "401" in str(exc)
        return
    raise AssertionError("expected CustomMediaError on HTTP 401")


# ---------------------------------------------------------------------------
# storage round-trip + templates
# ---------------------------------------------------------------------------


def test_storage_custom_slot_roundtrip_and_templates():
    from bot.storage import storage

    sid = "1"
    custom = {
        "method": "POST", "url": "https://api.roundtrip.io/g",
        "headers": {"Content-Type": "application/json"},
        "body_type": "json", "body": json.dumps({"prompt": "{{PROMPT}}"}),
        "result_path": "url",
    }
    try:
        storage.set_media_slot_custom(sid, custom)
        slot = storage.get_media_slot(sid)
        assert slot is not None
        assert slot.is_custom is True
        assert slot.configured is True  # custom slots configured w/o api_key
        assert slot.custom["url"] == "https://api.roundtrip.io/g"

        # templates library
        storage.save_media_template("rt-test", custom)
        assert "rt-test" in storage.list_media_templates()
        loaded = storage.get_media_template("rt-test")
        assert loaded["url"] == custom["url"]

        # load into slot 2
        storage.set_media_slot_custom("2", loaded)
        assert storage.get_media_slot("2").is_custom is True
    finally:
        storage.set_media_slot_custom("1", None)
        storage.set_media_slot_custom("2", None)
        storage.delete_media_template("rt-test")
        assert storage.get_media_slot("1") is None or not storage.get_media_slot("1").is_custom


# ---------------------------------------------------------------------------
# media.py dispatch routes custom slots through run_custom
# ---------------------------------------------------------------------------


def test_media_dispatch_image_to_image_custom():
    from bot import media
    from bot.media import MediaSlot

    custom = {
        "method": "POST", "url": "https://api.disp.io/edit",
        "headers": {"Authorization": "Bearer {{API_KEY}}", "Content-Type": "application/json"},
        "body_type": "json",
        "body": json.dumps({"model": "{{MODEL}}", "prompt": "{{PROMPT}}", "image": "{{IMAGE_DATA_URL}}"}),
        "result_path": "data[0].url",
    }
    slot = MediaSlot(slot_id="1", url="", api_key="secretkey", name="disp",
                     video_model="", photo_model="photo-x", rmbg_model="", custom=custom)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(body)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"url": "https://cdn/edited.png"}]})

    orig = media._load_slot
    media._load_slot = lambda task: slot  # type: ignore[assignment]
    try:
        with mock_httpx(handler):
            png, jpg = asyncio.run(media.image_to_image(b"imgbytes", "turn blue"))
    finally:
        media._load_slot = orig  # type: ignore[assignment]

    assert png == "https://cdn/edited.png"
    assert jpg == ""
    assert seen["prompt"] == "turn blue"
    assert seen["model"] == "photo-x"
    assert seen["auth"] == "Bearer secretkey"
    assert seen["image"].startswith("data:image/jpeg;base64,")
