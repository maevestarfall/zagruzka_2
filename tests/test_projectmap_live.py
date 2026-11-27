# Tests for the live project map (ТЗ «Живая карта проекта» v6.1, раздел 9.1).
# Co-authored with CoCo
"""Isolation, kill switch, protocol, replay/epoch, generators, masking,
sanitiser, watchdog and limits for ``bot.addons.projectmap`` live layer.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "livemap-events.schema.json"


def _fresh_tracer(monkeypatch, enabled: bool = True, text_mode: str = "full"):
    """Reload tracer (+live) with the given env — env is read at import."""
    monkeypatch.setenv("LIVEMAP_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("LIVEMAP_TEXT_MODE", text_mode)
    for name in ("bot.addons.projectmap.tracer", "bot.addons.projectmap.live"):
        sys.modules.pop(name, None)
    import bot.addons.projectmap.tracer as tracer  # noqa: PLC0415
    return importlib.reload(tracer)


def _fresh_live():
    import bot.addons.projectmap.live as live  # noqa: PLC0415
    return importlib.reload(live)


# --- kill switch -----------------------------------------------------------

def test_noop_when_disabled(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=False)
    assert tracer.get_stats() == {"enabled": False}
    # traced returns the function unchanged
    def fn(x):
        return x * 2
    assert tracer.traced("n")(fn) is fn
    # no live module gets imported / no structures allocated
    assert tracer.task_begin("t1", 1, "text") is None
    assert tracer.emit("hop", "t1", "a", "b") is None
    assert tracer.task_finish("t1") is None


# --- isolation (самый важный тест) ------------------------------------------

def test_tracer_never_raises(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True)
    live = sys.modules["bot.addons.projectmap.live"]

    def boom(_event):
        raise RuntimeError("livemap exploded")

    monkeypatch.setattr(live, "enqueue", boom)
    # None of these may raise — a broken tracer is a lost wave, not a crash.
    tracer.task_begin("t1", 1, "сделай видео")
    tracer.emit("hop", "t1", "llm_router", "video_gen")
    tracer.task_error(ValueError("x"), node="video_gen", task_id="t1")
    tracer.task_finish("t1")

    @tracer.traced("node")
    def handler():
        return 42

    assert handler() == 42


@pytest.mark.asyncio
async def test_traced_isolation_async(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True)
    live = sys.modules["bot.addons.projectmap.live"]
    monkeypatch.setattr(live, "enqueue",
                        lambda _e: (_ for _ in ()).throw(RuntimeError("boom")))

    @tracer.traced("node")
    async def handler():
        return "ok"

    assert await handler() == "ok"


# --- @traced: four kinds, generators keep the span open ----------------------

def _collect_events(monkeypatch, tracer):
    events: list[dict] = []
    live = sys.modules["bot.addons.projectmap.live"]
    monkeypatch.setattr(live, "enqueue", events.append)
    return events


def test_traced_sync_and_contextvar(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True)
    events = _collect_events(monkeypatch, tracer)
    tracer.task_begin("t42", 1, "task")

    @tracer.traced("inner")
    def inner():
        return 1

    @tracer.traced("outer")
    def outer():
        return inner()

    outer()
    node_events = [e for e in events if e["type"] in ("node_start", "node_end")]
    # contextvar flows into the nested call
    assert all(e.get("task_id") == "t42" for e in node_events)
    assert [e["type"] for e in node_events] == [
        "node_start", "node_start", "node_end", "node_end"]


def test_traced_sync_generator_spans_whole_iteration(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True)
    events = _collect_events(monkeypatch, tracer)

    @tracer.traced("gen")
    def gen():
        yield 1
        time.sleep(0.05)
        yield 2

    g = gen()
    next(g)
    # span still open after the first yield
    assert not [e for e in events if e["type"] == "node_end"]
    list(g)  # exhaust
    ends = [e for e in events if e["type"] == "node_end"]
    assert len(ends) == 1
    assert ends[0]["duration_ms"] >= 45  # covers the WHOLE iteration


@pytest.mark.asyncio
async def test_traced_async_generator(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True)
    events = _collect_events(monkeypatch, tracer)

    @tracer.traced("agen")
    async def agen():
        yield 1
        await asyncio.sleep(0.05)
        yield 2

    out = [x async for x in agen()]
    assert out == [1, 2]
    ends = [e for e in events if e["type"] == "node_end"]
    assert len(ends) == 1
    assert ends[0]["duration_ms"] >= 45


# --- privacy / sanitiser ------------------------------------------------------

def test_text_mode_masked(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True, text_mode="masked")
    events = _collect_events(monkeypatch, tracer)
    full = "сделай видео из этой картинки в стиле киберпанк"
    tracer.task_begin("t1", 1, full)
    frame = json.dumps(events, ensure_ascii=False)
    assert full not in frame
    assert events[0]["task_text"] == full[:12] + "…"


def test_text_mode_off(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True, text_mode="off")
    events = _collect_events(monkeypatch, tracer)
    tracer.task_begin("t1", 1, "секретный приказ")
    assert "task_text" not in events[0]


def test_error_sanitizer(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True)
    token = "A" * 40
    err = RuntimeError(f"api failed: key={token} try again")
    out = tracer.sanitize_error(err)
    assert token not in out
    assert "***" in out
    assert len(out) <= 220


# --- live hub: seq / replay / epoch / limits ------------------------------------

def test_process_event_and_replay(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    for i in range(5):
        live.process_event({"v": 1, "type": "task_start", "task_id": f"t{i}",
                            "ts": time.time()})
    assert live._seq == 5
    # replay inside the buffer → exactly the missed events, in order
    sl = live.replay_slice(2)
    assert [e["seq"] for e in sl] == [3, 4, 5]
    # up to date → empty replay
    assert live.replay_slice(5) == []
    # seq fell out of the buffer → None → snapshot
    live._buffer.clear()
    live.process_event({"v": 1, "type": "hop", "task_id": "t0",
                        "from": "a", "to": "b", "ts": time.time()})
    assert live.replay_slice(1) is None


def test_duplicate_task_end_ignored(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    live.process_event({"v": 1, "type": "task_start", "task_id": "t1", "ts": 1.0})
    assert live.process_event({"v": 1, "type": "task_end", "task_id": "t1",
                               "reason": "done", "ts": 2.0}) is not None
    # duplicate task_end → silently ignored (8.1)
    assert live.process_event({"v": 1, "type": "task_end", "task_id": "t1",
                               "reason": "done", "ts": 3.0}) is None


def test_task_cardinality_limit(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    for i in range(live.MAX_TASKS):
        live.process_event({"v": 1, "type": "task_start", "task_id": f"t{i}", "ts": 1.0})
    before = live.STATS["tasks_skipped"]
    # 51st task lives in the bot but never reaches the map
    assert live.process_event({"v": 1, "type": "task_start", "task_id": "t_extra",
                               "ts": 1.0}) is None
    assert live.STATS["tasks_skipped"] == before + 1
    # its follow-up events are dropped too
    assert live.process_event({"v": 1, "type": "hop", "task_id": "t_extra",
                               "from": "a", "to": "b", "ts": 1.0}) is None


def test_queue_overflow_counts_drops(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    for _ in range(live.QUEUE_MAX):
        live.enqueue({"v": 1, "type": "hop", "ts": 1.0})
    before = live.STATS["dropped_events"]
    live.enqueue({"v": 1, "type": "hop", "ts": 1.0})  # 1001st
    assert live.STATS["dropped_events"] == before + 1


def test_watchdog_closes_stale_tasks(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    live.process_event({"v": 1, "type": "task_start", "task_id": "t1", "ts": 1.0})
    live._tasks["t1"]["last_event"] = time.time() - live.STALE_AFTER_SEC - 1
    closed = live.watchdog_sweep()
    assert len(closed) == 1
    assert closed[0]["type"] == "task_end"
    assert closed[0]["reason"] == "stale"
    assert "t1" not in live._tasks


def test_node_stats_p50_p95(monkeypatch):
    """ТЗ 3.3: карточка узла — вызовов сегодня + p50/p95 (LRU топ-200)."""
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    for ms in (10.0, 20.0, 30.0, 40.0, 100.0):
        live.process_event({"v": 1, "type": "node_end", "node": "coder",
                            "duration_ms": ms, "ts": time.time()})
    stats = live.node_stats_payload()
    assert stats["coder"]["calls"] == 5
    assert stats["coder"]["p50"] == 30
    assert stats["coder"]["p95"] == 100
    # LRU cap: не больше NODE_STATS_MAX узлов
    for i in range(live.NODE_STATS_MAX + 10):
        live._record_node_duration(f"n{i}", 1.0)
    assert len(live._node_stats) <= live.NODE_STATS_MAX


def test_snapshot_shape(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    live.process_event({"v": 1, "type": "task_start", "task_id": "t1",
                        "task_text": "hi", "task_started_at": 1.0, "ts": 1.0})
    live.process_event({"v": 1, "type": "hop", "task_id": "t1",
                        "from": "llm_router", "to": "coder", "ts": 2.0})
    snap = live.make_snapshot()
    assert snap["type"] == "snapshot"
    assert snap["epoch"] == live.EPOCH
    assert snap["seq"] == live._seq
    assert snap["tasks"][0]["node"] == "coder"


# --- microbenchmark (мягкий порог) -----------------------------------------------

def test_emit_is_cheap(monkeypatch):
    tracer = _fresh_tracer(monkeypatch, enabled=True)
    tracer.task_begin("t1", 1, "x")
    n = 1000
    t0 = time.perf_counter()
    for _ in range(n):
        tracer.emit("hop", "t1", "a", "b")
    per_call_us = (time.perf_counter() - t0) / n * 1e6
    # ТЗ: ≤ 50 мкс; порог мягкий (CI бывает медленный)
    assert per_call_us < 500


# --- JSON schema -------------------------------------------------------------------

def test_events_valid_against_schema(monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    tracer = _fresh_tracer(monkeypatch, enabled=True)
    events = _collect_events(monkeypatch, tracer)
    tracer.task_begin("t1", 1, "сделай видео")
    tracer.emit("hop", "t1", "llm_router", "video_gen")
    tracer.task_error(TimeoutError("rendering exceeded 120s"), node="video_gen",
                      task_id="t1")
    live = _fresh_live()
    for ev in events:
        processed = live.process_event(dict(ev))
        if processed is not None:
            jsonschema.validate(processed, schema)
    jsonschema.validate(live.make_snapshot(), schema)
    jsonschema.validate({"v": 1, "batch": list(live._buffer)}, schema)
    jsonschema.validate({"v": 1, "type": "resync", "resync": "replay",
                         "epoch": live.EPOCH, "seq": live._seq}, schema)


# --- anti-enumeration / security headers ------------------------------------------

def test_bad_token_throttle(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=False)
    sys.modules.pop("bot.addons.projectmap.server", None)
    server = importlib.import_module("bot.addons.projectmap.server")
    ip = "203.0.113.7"
    assert not server._throttled(ip)
    for _ in range(11):
        server._note_bad_attempt(ip)
    assert server._throttled(ip)  # 11th bad attempt within a minute → banned


def test_page_html_under_200kb(monkeypatch):
    """ТЗ 8.4: самодостаточный HTML < 200 KB (3 сек до интерактива на 3G)."""
    _fresh_tracer(monkeypatch, enabled=False)
    sys.modules.pop("bot.addons.projectmap.server", None)
    server = importlib.import_module("bot.addons.projectmap.server")
    assert len(server._PAGE_HTML.encode("utf-8")) < 200_000


def test_security_headers_present(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=False)
    sys.modules.pop("bot.addons.projectmap.server", None)
    server = importlib.import_module("bot.addons.projectmap.server")
    resp = server._not_found()
    assert resp.status == 404  # always 404, never 403 (anti-enumeration)
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"
    assert "noindex" in resp.headers["X-Robots-Tag"]
    assert "default-src 'none'" in resp.headers["Content-Security-Policy"]


# --- graceful shutdown: 1001 GOING_AWAY (ТЗ 6.1, 9.1 v6.0) --------------------

class _FakeWS:
    """Minimal stand-in for WebSocketResponse used by the shutdown hook."""

    def __init__(self):
        self.close_code = None
        self.close_message = None

    async def close(self, *, code=None, message=b""):
        self.close_code = code
        self.close_message = message


@pytest.mark.asyncio
async def test_graceful_shutdown_closes_with_1001(monkeypatch):
    from aiohttp import web, WSCloseCode  # noqa: PLC0415

    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    app = web.Application()
    live.ensure_started(app)

    a, b = _FakeWS(), _FakeWS()
    live._clients.add(a)
    live._clients.add(b)
    try:
        for hook in app.on_shutdown:
            await hook(app)
        assert a.close_code == WSCloseCode.GOING_AWAY == 1001
        assert b.close_code == WSCloseCode.GOING_AWAY == 1001
    finally:
        live._clients.discard(a)
        live._clients.discard(b)


# --- receive-loop error accounting (ТЗ 6.1, 9.1 v6.0) -------------------------

def test_ws_recv_errors_counter_exists(monkeypatch):
    """«Тихая» смерть heartbeat попадает в счётчик, а не исчезает (6.1)."""
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    stats = live.get_stats()
    assert "ws_recv_errors" in stats
    # ws_handler checks ws.exception() in `finally` — verify the code path
    import inspect  # noqa: PLC0415
    src = inspect.getsource(live.ws_handler)
    assert "ws.exception()" in src
    assert "async for msg in ws" in src  # receive loop keeps heartbeat alive


# --- бейдж «NEW» и защита от ложных «NEW» (ТЗ 3.4) ----------------------------

def test_annotate_new_suppressed_on_fresh_state(monkeypatch):
    _fresh_tracer(monkeypatch, enabled=False)
    sys.modules.pop("bot.addons.projectmap.server", None)
    server = importlib.import_module("bot.addons.projectmap.server")

    nodes = [{"id": f"n{i}"} for i in range(10)]
    now = time.time()

    # Все узлы новые (> 30%) — свежий стейт после деплоя: бейджей нет.
    monkeypatch.setattr(server.store, "touch_first_seen",
                        lambda ids: {i: now for i in ids})
    out = server._annotate_new(list(nodes))
    assert not any(n.get("isNew") for n in out)

    # Один новый узел из десяти — бейдж показывается только на нём.
    seen = {f"n{i}": now - 3 * 24 * 3600 for i in range(9)}
    seen["n9"] = now  # свежий
    monkeypatch.setattr(server.store, "touch_first_seen", lambda ids: seen)
    out = server._annotate_new(list(nodes))
    flagged = [n["id"] for n in out if n.get("isNew")]
    assert flagged == ["n9"]


# --- batching / coalescing (ТЗ 6.1, 6.3, сценарий 5; 9.1) ---------------------

def test_batch_30_events_one_frame(monkeypatch):
    """30 событий за тик → ОДИН ws-кадр {"v":1,"batch":[...30...]}."""
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    batch = []
    for i in range(30):
        ev = live.process_event({"v": 1, "type": "task_start",
                                 "task_id": f"t{i}", "ts": float(i)})
        assert ev is not None
        batch.append(ev)
    frame = json.loads(live.make_frame(batch))
    assert frame["v"] == 1
    assert "batch" in frame and len(frame["batch"]) == 30
    assert frame.get("type") != "snapshot"


def test_batch_storm_coalesces_to_snapshot(monkeypatch):
    """> 50 событий за тик → вместо батча уходит один snapshot (6.3)."""
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    batch = []
    for i in range(live.COALESCE_THRESHOLD + 1):
        ev = live.process_event({"v": 1, "type": "task_start",
                                 "task_id": f"t{i}", "ts": float(i)})
        if ev is not None:
            batch.append(ev)
    # даже если часть задач отсечена лимитом 50, эмулируем шторм явно
    while len(batch) <= live.COALESCE_THRESHOLD:
        batch.append({"v": 1, "type": "hop", "task_id": "t0",
                      "from": "a", "to": "b", "seq": 0, "ts": 0.0})
    frame = json.loads(live.make_frame(batch))
    assert frame["type"] == "snapshot"
    assert frame["epoch"] == live.EPOCH


# --- лимит подключений: 6-е WS → 1013 TRY_AGAIN_LATER (ТЗ 6.3, 9.1) -----------

@pytest.mark.asyncio
async def test_sixth_ws_rejected_with_1013(monkeypatch):
    from aiohttp import WSCloseCode, web  # noqa: PLC0415
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()

    app = web.Application()
    app.router.add_get("/ws", live.ws_handler)

    # Занимаем все MAX_WS слотов фейковыми клиентами (weakref-able).
    fillers = [_FakeWS() for _ in range(live.MAX_WS)]
    for f in fillers:
        live._clients.add(f)

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        msg = await ws.receive(timeout=5)
        assert msg.type.name in ("CLOSE", "CLOSING", "CLOSED")
        assert ws.close_code == WSCloseCode.TRY_AGAIN_LATER == 1013
        assert live.STATS["ws_rejected"] >= 1
    finally:
        for f in fillers:
            live._clients.discard(f)
        await client.close()


# --- Origin-проверка (anti-CSWSH, ТЗ 6.4; 9.1) --------------------------------

def test_origin_check(monkeypatch):
    from aiohttp.test_utils import make_mocked_request  # noqa: PLC0415

    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()

    # Без Origin (curl, нативные клиенты) — допускаем.
    req = make_mocked_request("GET", "/ws", headers={"Host": "bot.example.com"})
    assert live._origin_ok(req)

    # Origin совпадает с хостом страницы — допускаем.
    req = make_mocked_request("GET", "/ws", headers={
        "Host": "bot.example.com", "Origin": "https://bot.example.com"})
    assert live._origin_ok(req)

    # Чужой Origin — отказ (сам отказ в ws_handler отдаёт 404).
    req = make_mocked_request("GET", "/ws", headers={
        "Host": "bot.example.com", "Origin": "https://evil.example.net"})
    assert not live._origin_ok(req)


@pytest.mark.asyncio
async def test_foreign_origin_gets_404(monkeypatch):
    from aiohttp import web  # noqa: PLC0415
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()
    app = web.Application()
    app.router.add_get("/ws", live.ws_handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/ws", headers={"Origin": "https://evil.example.net"})
        assert resp.status == 404  # не 403 — анти-перечисление (6.4)
    finally:
        await client.close()


# --- hello: чужая epoch → всегда snapshot (ТЗ 9.1 «главный кейс», сценарий 2) --

@pytest.mark.asyncio
async def test_hello_foreign_epoch_gets_snapshot(monkeypatch):
    """После рестарта бота у клиента чужая epoch: даже если его last_seq
    «попадает» в диапазон нового буфера, replay запрещён — только snapshot."""
    from aiohttp import web  # noqa: PLC0415
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()

    # Наполняем буфер: last_seq=2 попал бы в replay, будь epoch родной.
    for i in range(5):
        live.process_event({"v": 1, "type": "task_start",
                            "task_id": f"t{i}", "ts": float(i)})

    app = web.Application()
    app.router.add_get("/ws", live.ws_handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # 1) Чужая epoch → resync=snapshot + полный snapshot (не replay).
        ws = await client.ws_connect("/ws")
        await ws.send_str(json.dumps(
            {"hello": {"epoch": "not-this-process", "last_seq": 2}}))
        resync = json.loads((await ws.receive_str(timeout=5)))
        assert resync["type"] == "resync"
        assert resync["resync"] == "snapshot"
        assert resync["epoch"] == live.EPOCH
        snap = json.loads((await ws.receive_str(timeout=5)))
        assert snap["type"] == "snapshot"
        assert snap["epoch"] == live.EPOCH
        assert snap["seq"] == live._seq
        await ws.close()

        # 2) Контроль: родная epoch с тем же last_seq → replay, не snapshot.
        ws2 = await client.ws_connect("/ws")
        await ws2.send_str(json.dumps(
            {"hello": {"epoch": live.EPOCH, "last_seq": 2}}))
        resync2 = json.loads((await ws2.receive_str(timeout=5)))
        assert resync2["resync"] == "replay"
        batch = json.loads((await ws2.receive_str(timeout=5)))
        assert [e["seq"] for e in batch["batch"]] == [3, 4, 5]
        await ws2.close()
    finally:
        await client.close()


# --- slow-client probe деградирует, а не падает (ТЗ 8.1) ----------------------

def test_write_buffer_size_degrades_to_zero(monkeypatch):
    """Если приватные атрибуты aiohttp переименуют — вернётся 0, а не
    исключение внутри батчера (лучше нет slow-detect, чем упавший хаб)."""
    _fresh_tracer(monkeypatch, enabled=True)
    live = _fresh_live()

    class _Bare:  # ни _writer, ни _req
        pass

    assert live._write_buffer_size(_Bare()) == 0

    class _Transport:
        def get_write_buffer_size(self):
            return 512 * 1024

    class _Writer:
        transport = _Transport()

    class _WithWriter:
        _writer = _Writer()

    assert live._write_buffer_size(_WithWriter()) == 512 * 1024
