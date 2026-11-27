# Live project map: WS hub — epoch/seq/replay/snapshot/batching (ТЗ v6.1).
# Co-authored with CoCo
"""WebSocket hub for the live project map.

Model (раздел 6 ТЗ, prior art Centrifugo):

* ``epoch`` — random string per process start; ``seq`` is meaningful only
  inside its epoch. Client hello with a foreign epoch → always snapshot.
* Ring buffer of the last ``LIVEMAP_BUFFER`` (default 500) events with a
  monotonically growing ``seq`` → cheap replay after short disconnects.
* Batching: one 100 ms tick, everything accumulated goes in ONE ws frame
  ``{"v":1,"batch":[...]}``. Empty tick sends nothing. > 50 events in a
  tick → coalesced into a single ``snapshot`` frame (сценарий 5).
* Backpressure: max ``LIVEMAP_MAX_WS`` (5) connections (6th → 1013);
  slow client (> 256KB send buffer) → close 1008; deflate disabled.
* Heartbeat 25 s works ONLY while a receive loop runs (aiohttp грабля) —
  every connection keeps ``async for msg in ws`` alive; after the loop
  exits ``ws.exception()`` is checked and counted.
* Graceful shutdown: ``on_shutdown`` closes every WS with 1001 GOING_AWAY.
* Watchdog: a task silent for 30 min is force-closed (reason "stale").
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import weakref
from collections import deque
from typing import Any, Optional

from aiohttp import WSCloseCode, WSMsgType, web

logger = logging.getLogger(__name__)

# --- config -------------------------------------------------------------

BUFFER_SIZE = max(50, int(os.getenv("LIVEMAP_BUFFER", "500") or 500))
MAX_WS = max(1, int(os.getenv("LIVEMAP_MAX_WS", "5") or 5))
QUEUE_MAX = 1000
BATCH_TICK = 0.1          # 100 ms (6.1)
COALESCE_THRESHOLD = 50   # events per tick → snapshot (6.3)
SLOW_CLIENT_BYTES = 256 * 1024
MAX_TASKS = 50
STALE_AFTER_SEC = 30 * 60
HEARTBEAT_SEC = 25.0

# --- state (worst-case memory budget: раздел 8.3, < 2.5 MB) --------------

EPOCH: str = secrets.token_hex(8)

_queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
_buffer: deque = deque(maxlen=BUFFER_SIZE)   # replay ring buffer
_seq: int = 0
_pending: list[dict] = []                    # events for the next tick
_tasks: dict[str, dict] = {}                 # active tasks (≤ 50)
_skipped_tasks: set[str] = set()             # over-limit task ids
_clients: "weakref.WeakSet[web.WebSocketResponse]" = weakref.WeakSet()
_client_meta: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_started = False
_bg_tasks: list[asyncio.Task] = []

STATS: dict[str, int] = {
    "events_total": 0,
    "dropped_events": 0,
    "tasks_skipped": 0,
    "ws_clients": 0,
    "ws_rejected": 0,
    "ws_slow_closed": 0,
    "ws_recv_errors": 0,
    "page_opens": 0,
}


# --- per-node call stats: calls today + p50/p95, LRU top-200 (ТЗ 3.3) ----

NODE_STATS_MAX = 200
_node_stats: "dict[str, dict]" = {}  # node → {day, calls, durs(deque≤100)}


def _record_node_duration(node: str, duration_ms: float) -> None:
    try:
        today = time.strftime("%Y-%m-%d")
        st = _node_stats.get(node)
        if st is None:
            if len(_node_stats) >= NODE_STATS_MAX:
                # LRU: выкидываем самый давний по last_used
                oldest = min(_node_stats, key=lambda k: _node_stats[k]["used"])
                _node_stats.pop(oldest, None)
            st = _node_stats[node] = {"day": today, "calls": 0,
                                      "durs": deque(maxlen=100), "used": 0.0}
        if st["day"] != today:
            st["day"] = today
            st["calls"] = 0
            st["durs"].clear()
        st["calls"] += 1
        st["used"] = time.time()
        st["durs"].append(float(duration_ms))
    except Exception:  # noqa: BLE001
        pass


def node_stats_payload() -> dict:
    """``{node: {calls, p50, p95}}`` for the SPA node card."""
    out: dict[str, dict] = {}
    for node, st in _node_stats.items():
        durs = sorted(st["durs"])
        if durs:
            p50 = durs[min(len(durs) - 1, int(0.50 * len(durs)))]
            p95 = durs[min(len(durs) - 1, int(0.95 * len(durs)))]
        else:
            p50 = p95 = 0.0
        out[node] = {"calls": st["calls"], "p50": round(p50), "p95": round(p95)}
    return out


def get_stats() -> dict:
    lo = _buffer[0]["seq"] if _buffer else 0
    return {
        "enabled": True,
        "epoch": EPOCH,
        "ws_clients": len(_clients),
        "buffer_seq_range": [lo, _seq],
        "active_tasks": len(_tasks),
        **STATS,
    }


def graph_hash() -> str:
    """sha256 of the sorted node/edge ids — drives ``graph_update`` (3.4)."""
    try:
        from . import store
        g = store.get_graph() or {}
        nodes = sorted(str(n.get("id", "")) for n in g.get("nodes", []))
        edges = sorted(f"{e.get('source')}→{e.get('target')}" for e in g.get("edges", []))
        payload = "\n".join(nodes) + "|" + "\n".join(edges)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


# --- ingestion (called by tracer, sync, never blocks) ---------------------

def enqueue(event: dict) -> None:
    try:
        _queue.put_nowait(event)
    except asyncio.QueueFull:
        STATS["dropped_events"] += 1
    except Exception:  # noqa: BLE001
        STATS["dropped_events"] += 1


def process_event(event: dict) -> Optional[dict]:
    """Apply an event to task state; return the event with ``seq`` set,
    or ``None`` when the event must not reach the wire.

    Tolerant to garbage (8.1): duplicate task_end, unknown types and
    orphan node_end are silently ignored or passed through harmlessly.
    """
    global _seq
    etype = event.get("type")
    tid = event.get("task_id")

    # p50/p95 счётчики узла — независимо от наличия task_id (ТЗ 3.3).
    if etype == "node_end" and event.get("node") and event.get("duration_ms") is not None:
        _record_node_duration(str(event["node"]), event["duration_ms"])

    if etype == "task_start" and tid:
        if tid in _skipped_tasks:
            return None
        if tid not in _tasks and len(_tasks) >= MAX_TASKS:
            _skipped_tasks.add(tid)
            if len(_skipped_tasks) > 1000:
                _skipped_tasks.clear()
            STATS["tasks_skipped"] += 1
            return None
        _tasks.setdefault(tid, {
            "task_id": tid,
            "task_text": event.get("task_text"),
            "task_started_at": event.get("task_started_at", event.get("ts", time.time())),
            "node": "user",
            "last_event": time.time(),
        })
    elif etype in ("hop", "node_start", "node_end", "task_error") and tid:
        if tid in _skipped_tasks:
            return None
        task = _tasks.get(tid)
        if task is not None:
            task["last_event"] = time.time()
            if etype == "hop" and event.get("to"):
                task["node"] = event["to"]
            elif etype == "node_start" and event.get("node"):
                task["node"] = event["node"]
        # enrich waves with the task birth time so age colour is correct
        if task is not None and "task_started_at" not in event:
            event["task_started_at"] = task.get("task_started_at")
        if task is not None and task.get("task_text") and "task_text" not in event:
            event["task_text"] = task["task_text"]
    elif etype == "task_end" and tid:
        if tid in _skipped_tasks:
            _skipped_tasks.discard(tid)
            return None
        if tid not in _tasks:
            return None  # duplicate task_end — ignored (8.1)
        _tasks.pop(tid, None)

    _seq += 1
    event["seq"] = _seq
    _buffer.append(event)
    STATS["events_total"] += 1
    return event


def make_snapshot() -> dict:
    """Current state only — 'cache' resync mode (Centrifugo model)."""
    return {
        "v": 1,
        "type": "snapshot",
        "epoch": EPOCH,
        "seq": _seq,
        "graph_hash": graph_hash(),
        "server_ts": time.time(),
        "tasks": [
            {
                "task_id": t["task_id"],
                "task_text": t.get("task_text"),
                "task_started_at": t.get("task_started_at"),
                "node": t.get("node"),
            }
            for t in list(_tasks.values())
        ],
    }


def replay_slice(last_seq: int) -> Optional[list[dict]]:
    """Events after ``last_seq``, or None if they fell out of the buffer."""
    if not _buffer:
        return [] if last_seq == _seq else None
    lo = _buffer[0]["seq"]
    if last_seq + 1 < lo:
        return None
    if last_seq >= _seq:
        return []
    return [e for e in _buffer if e["seq"] > last_seq]


def watchdog_sweep(now: Optional[float] = None) -> list[dict]:
    """Force-close tasks silent for 30 min (reason 'stale', no modal)."""
    now = now if now is not None else time.time()
    closed: list[dict] = []
    for tid, task in list(_tasks.items()):
        if now - task.get("last_event", now) >= STALE_AFTER_SEC:
            ev = process_event({"v": 1, "type": "task_end", "task_id": tid,
                                "reason": "stale", "ts": now})
            if ev:
                closed.append(ev)
    return closed


def _write_buffer_size(ws: web.WebSocketResponse) -> int:
    """Bytes queued in the client's send buffer, 0 when unknown.

    Uses only try/except probes so an aiohttp upgrade that renames private
    attributes degrades to 'no slow-client detection' instead of crashing
    the batcher (the old code touched ``ws._req.transport`` directly).
    """
    for probe in (
        lambda: ws._writer.transport.get_write_buffer_size(),  # noqa: SLF001
        lambda: ws._req.transport.get_write_buffer_size(),     # noqa: SLF001
    ):
        try:
            size = probe()
            if isinstance(size, int):
                return size
        except Exception:  # noqa: BLE001
            continue
    return 0


# --- background loops ------------------------------------------------------

async def _dispatcher() -> None:
    """Drain the tracer queue → task state → ring buffer → pending batch."""
    while True:
        event = await _queue.get()
        try:
            ev = process_event(event)
            if ev is not None:
                _pending.append(ev)
        except Exception:  # noqa: BLE001
            logger.debug("livemap dispatcher error", exc_info=True)


def make_frame(batch: list[dict]) -> str:
    """ONE ws frame for a tick: normal batch, or a coalesced snapshot
    when the tick brought an event storm (> COALESCE_THRESHOLD, 6.3)."""
    if len(batch) > COALESCE_THRESHOLD:
        return json.dumps(make_snapshot(), ensure_ascii=False)
    return json.dumps({"v": 1, "batch": batch}, ensure_ascii=False)


async def _batcher() -> None:
    """100 ms tick: one frame per tick per client; coalesce storms."""
    last_watchdog = time.monotonic()
    while True:
        await asyncio.sleep(BATCH_TICK)
        try:
            if time.monotonic() - last_watchdog > 60:
                last_watchdog = time.monotonic()
                _pending.extend(watchdog_sweep())
            if not _pending:
                continue
            batch, _pending[:] = _pending[:], []
            frame = make_frame(batch)
            for ws in list(_clients):
                meta = _client_meta.get(ws)
                if not meta or not meta.get("ready"):
                    continue
                try:
                    if _write_buffer_size(ws) > SLOW_CLIENT_BYTES:
                        STATS["ws_slow_closed"] += 1
                        await ws.close(code=WSCloseCode.POLICY_VIOLATION,
                                       message=b"slow client")
                        continue
                    await ws.send_str(frame)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            logger.debug("livemap batcher error", exc_info=True)


def ensure_started(app: web.Application) -> None:
    """Start dispatcher/batcher once and hook graceful shutdown."""
    global _started
    if _started:
        return
    _started = True

    async def _on_startup(_app: web.Application) -> None:
        _bg_tasks.append(asyncio.create_task(_dispatcher()))
        _bg_tasks.append(asyncio.create_task(_batcher()))
        logger.info("livemap hub started (epoch=%s)", EPOCH)

    async def _on_shutdown(_app: web.Application) -> None:
        # Canonical aiohttp pattern: 1001 GOING_AWAY → client knows it is
        # a restart, not an accident (6.1).
        for ws in set(_clients):
            try:
                await ws.close(code=WSCloseCode.GOING_AWAY,
                               message=b"bot restarting")
            except Exception:  # noqa: BLE001
                pass
        for t in _bg_tasks:
            t.cancel()

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)


# --- WS handler --------------------------------------------------------------

def _origin_ok(request: web.Request) -> bool:
    """Origin must match the page host or be absent (anti CSWSH, 6.4)."""
    origin = request.headers.get("Origin", "")
    if not origin:
        return True
    try:
        from urllib.parse import urlparse
        return urlparse(origin).netloc == request.host
    except Exception:  # noqa: BLE001
        return False


async def ws_handler(request: web.Request) -> web.StreamResponse:
    """``WS /projectmap/<token>/ws`` — token gate happens in server.py."""
    if not _origin_ok(request):
        return web.Response(status=404, text="not found")
    if len(_clients) >= MAX_WS:
        STATS["ws_rejected"] += 1
        ws = web.WebSocketResponse(compress=False)
        await ws.prepare(request)
        await ws.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b"too many clients")
        return ws

    # permessage-deflate OFF: zlib contexts ≈ 300KB/conn (6.3).
    ws = web.WebSocketResponse(heartbeat=HEARTBEAT_SEC, compress=False)
    await ws.prepare(request)
    _clients.add(ws)
    _client_meta[ws] = {"ready": False}
    STATS["ws_clients"] = len(_clients)

    try:
        # Receive loop MUST keep running for heartbeat to work (aiohttp).
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue

            if "hello" in data:
                hello = data.get("hello") or {}
                client_epoch = str(hello.get("epoch") or "")
                try:
                    last_seq = int(hello.get("last_seq") or 0)
                except Exception:  # noqa: BLE001
                    last_seq = 0
                client_ghash = str(hello.get("graph_hash") or "")

                replay: Optional[list[dict]] = None
                if client_epoch == EPOCH:
                    replay = replay_slice(last_seq)
                # Foreign epoch OR seq out of buffer → snapshot (сценарий 2).
                if replay is not None:
                    await ws.send_str(json.dumps({
                        "v": 1, "type": "resync", "resync": "replay",
                        "epoch": EPOCH, "seq": _seq, "server_ts": time.time(),
                    }, ensure_ascii=False))
                    if replay:
                        await ws.send_str(json.dumps(
                            {"v": 1, "batch": replay}, ensure_ascii=False))
                else:
                    await ws.send_str(json.dumps({
                        "v": 1, "type": "resync", "resync": "snapshot",
                        "epoch": EPOCH, "seq": _seq, "server_ts": time.time(),
                    }, ensure_ascii=False))
                    await ws.send_str(json.dumps(make_snapshot(), ensure_ascii=False))
                gh = graph_hash()
                if client_ghash and gh and client_ghash != gh:
                    await ws.send_str(json.dumps({
                        "v": 1, "type": "graph_update", "graph_hash": gh,
                        "seq": _seq, "ts": time.time(),
                    }, ensure_ascii=False))
                meta = _client_meta.get(ws)
                if meta is not None:
                    meta["ready"] = True

            elif "ping" in data:
                # clock-offset probe (5.4)
                await ws.send_str(json.dumps({
                    "v": 1, "type": "pong",
                    "client_ts": data.get("ping"), "ts": time.time(),
                }, ensure_ascii=False))
    finally:
        # 'Silent' heartbeat death lands in the log, not nowhere (6.1).
        exc = ws.exception()
        if exc is not None:
            STATS["ws_recv_errors"] += 1
            logger.debug("livemap ws closed with error: %r", exc)
        _clients.discard(ws)
        STATS["ws_clients"] = len(_clients)

    return ws
