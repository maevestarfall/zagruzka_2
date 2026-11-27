# Project-map web panel: token-gated interactive HTML graph + live layer.
# Co-authored with CoCo
"""HTTP endpoints for «🗺 Карта проекта» (+ живой слой, ТЗ v6.1).

Token-gated, same shape as VISION:

* ``GET /projectmap/<token>/``       — self-contained interactive graph SPA.
* ``GET /projectmap/<token>/data``   — JSON ``{nodes, edges, blocks, custom}``.
* ``GET /projectmap/<token>/export`` — download the portable base JSON.
* ``GET /projectmap/<token>/ping``   — liveness JSON + livemap metrics.
* ``WS  /projectmap/<token>/ws``     — live events (only when LIVEMAP_ENABLED=1).

Security (раздел 6.4 ТЗ):

* wrong token → ALWAYS 404 (no 403/401 — existence is not confirmed);
* per-IP throttling of bad-token attempts (>10/min → 60 s of instant 404);
* Referrer-Policy / Cache-Control / X-Robots-Tag / CSP headers everywhere.

The SPA stays 100% self-contained (inline vanilla-JS, no CDN).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Optional

from aiohttp import web

from . import store
from . import hermes
from . import live as livemap
from .tracer import LIVEMAP_ENABLED, CATEGORIES

logger = logging.getLogger(__name__)


# --- security headers (6.4) --------------------------------------------

_SEC_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src data:; connect-src 'self'"
    ),
}


def _secure(resp: web.Response) -> web.Response:
    for k, v in _SEC_HEADERS.items():
        resp.headers[k] = v
    return resp


def _not_found() -> web.Response:
    # Body indistinguishable from any missing page (anti-enumeration).
    return _secure(web.Response(status=404, text="404: Not Found"))


# --- anti-enumeration: per-IP throttle of bad tokens (6.4) ---------------

_BAD_ATTEMPTS: dict[str, list] = {}   # ip -> [count, window_start, banned_until]
_MAX_TRACKED_IPS = 1000


def _client_ip(request: web.Request) -> str:
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.remote or "?"))


def _throttled(ip: str) -> bool:
    rec = _BAD_ATTEMPTS.get(ip)
    return bool(rec and rec[2] > time.monotonic())


def _note_bad_attempt(ip: str) -> None:
    now = time.monotonic()
    rec = _BAD_ATTEMPTS.get(ip)
    if rec is None or now - rec[1] > 60:
        rec = [0, now, 0.0]
    rec[0] += 1
    if rec[0] > 10:
        rec[2] = now + 60  # 60 s of instant 404, no _check() call
    _BAD_ATTEMPTS[ip] = rec
    if len(_BAD_ATTEMPTS) > _MAX_TRACKED_IPS:  # cheap LRU-ish trim
        for k in list(_BAD_ATTEMPTS)[: len(_BAD_ATTEMPTS) - _MAX_TRACKED_IPS]:
            _BAD_ATTEMPTS.pop(k, None)


# --- token gate ----------------------------------------------------------

def _check(token: str) -> Optional[web.Response]:
    stored = store.get_token()
    if not stored or not secrets.compare_digest(stored, token):
        return _not_found()
    if not store.is_enabled():
        return _not_found()
    return None


def _gate(request: web.Request) -> Optional[web.Response]:
    """Throttle first (cheap), then constant-time token check."""
    ip = _client_ip(request)
    if _throttled(ip):
        return _not_found()
    resp = _check(request.match_info["token"])
    if resp is not None:
        _note_bad_attempt(ip)
    return resp


async def _ping(request: web.Request) -> web.Response:
    if (resp := _gate(request)) is not None:
        return resp
    payload = {"ok": True, "version": "projectmap/2", "livemap": {"enabled": LIVEMAP_ENABLED}}
    if LIVEMAP_ENABLED:
        try:
            payload["livemap"] = livemap.get_stats()
        except Exception:  # noqa: BLE001
            pass
    return _secure(web.json_response(payload))


# --- blocks mode: one node per addon (3.1) -------------------------------

def _block_of(module: str) -> str:
    parts = (module or "").split(".")
    if "addons" in parts:
        i = parts.index("addons")
        if i + 1 < len(parts):
            return parts[i + 1]
    # non-addon core modules → their top-level file ("handlers", "agent"…)
    for p in parts:
        if p not in ("bot",):
            return p
    return module or "core"


def _block_graph(graph: dict) -> dict:
    """Aggregate the function graph into addon-level blocks + spec nodes."""
    counts: dict[str, int] = {}
    for n in graph.get("nodes", []):
        b = _block_of(n.get("module", n.get("id", "")))
        counts[b] = counts.get(b, 0) + 1

    node_block: dict[str, str] = {}
    for n in graph.get("nodes", []):
        node_block[n.get("id", "")] = _block_of(n.get("module", n.get("id", "")))

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in graph.get("edges", []):
        a = node_block.get(e.get("source", ""))
        b = node_block.get(e.get("target", ""))
        if not a or not b or a == b:
            continue
        if (a, b) not in seen:
            seen.add((a, b))
            edges.append({"source": a, "target": b})

    nodes = [
        {
            "id": b,
            "name": b,
            "kind": "block",
            "size": c,
            "category": CATEGORIES.get(b, "neutral"),
        }
        for b, c in sorted(counts.items())
    ]
    # Special programmatic nodes (3.1): 👤 Пользователь + 🧠 Роутер.
    nodes.append({"id": "user", "name": "👤 Пользователь", "kind": "special",
                  "size": 0, "category": "special"})
    nodes.append({"id": "llm_router", "name": "🧠 Роутер", "kind": "special",
                  "size": 0, "category": "special"})
    edges.append({"source": "user", "target": "llm_router"})
    if "handlers" in counts:
        edges.append({"source": "llm_router", "target": "handlers"})
    return {"nodes": nodes, "edges": edges}


def _annotate_new(nodes: list[dict]) -> list[dict]:
    """Бейдж «NEW» первые 24 ч по ``first_seen`` из state.json (ТЗ 3.4).

    Защита от ложных «NEW»: > 30% узлов новые (эфемерный диск после
    деплоя) → бейджи не показываются вовсе."""
    try:
        ids = [str(n.get("id", "")) for n in nodes]
        seen = store.touch_first_seen(ids)
        now = time.time()
        fresh = {nid for nid in ids if now - seen.get(nid, now) < 24 * 3600}
        if not fresh or len(fresh) > 0.3 * max(1, len(ids)):
            return nodes  # свежий стейт после деплоя — бейджи не показываем
        return [dict(n, isNew=True) if n.get("id") in fresh else n
                for n in nodes]
    except Exception:  # noqa: BLE001
        return nodes


def _graph_payload() -> dict:
    graph = store.get_graph() or {"nodes": [], "edges": []}
    blocks = _block_graph(graph)
    blocks["nodes"] = _annotate_new(blocks.get("nodes", []))
    return {
        "nodes": _annotate_new(list(graph.get("nodes", []))),
        "edges": graph.get("edges", []),
        "blocks": blocks,
        "custom": store.get_edges(),
        "weak": store.get_weak_spots(),
        "built_at": graph.get("built_at", 0),
        "graph_hash": livemap.graph_hash(),
        "node_count": graph.get("node_count", len(graph.get("nodes", []))),
        "edge_count": graph.get("edge_count", len(graph.get("edges", []))),
        # вызовов сегодня + p50/p95 для карточки узла (ТЗ 3.3)
        "node_stats": livemap.node_stats_payload() if LIVEMAP_ENABLED else {},
    }


async def _data(request: web.Request) -> web.Response:
    if (resp := _gate(request)) is not None:
        return resp
    # Lazily refresh when auto-mode is on and the cache is stale.
    try:
        hermes.maybe_auto_refresh()
    except Exception:  # noqa: BLE001
        pass
    return _secure(web.json_response(_graph_payload()))


async def _export(request: web.Request) -> web.Response:
    if (resp := _gate(request)) is not None:
        return resp
    body = json.dumps(store.export_base(), ensure_ascii=False, indent=2)
    return _secure(web.Response(
        body=body.encode("utf-8"),
        content_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="projectmap_base.json"'},
    ))


async def _root(request: web.Request) -> web.Response:
    if (resp := _gate(request)) is not None:
        return resp
    # Make sure there is something to show on first open.
    if not store.get_graph():
        try:
            hermes.rebuild_graph()
        except Exception:  # noqa: BLE001
            pass
    if LIVEMAP_ENABLED:
        try:
            livemap.STATS["page_opens"] += 1
        except Exception:  # noqa: BLE001
            pass
    html = _PAGE_HTML.replace("__LIVE__", "1" if LIVEMAP_ENABLED else "0")
    return _secure(web.Response(text=html, content_type="text/html"))


async def _ws(request: web.Request) -> web.StreamResponse:
    if (resp := _gate(request)) is not None:
        return resp
    return await livemap.ws_handler(request)


def register_http_endpoints(app: web.Application) -> None:
    """Attach project-map routes to the bot's aiohttp app.

    Called from BOTH branches of ``bot/main.py`` (webhook + polling) —
    the WS route lives here so it automatically lands in both.
    """
    app.router.add_get("/projectmap/{token}/ping", _ping)
    app.router.add_get("/projectmap/{token}/data", _data)
    app.router.add_get("/projectmap/{token}/export", _export)
    if LIVEMAP_ENABLED:
        app.router.add_get("/projectmap/{token}/ws", _ws)
        livemap.ensure_started(app)
    app.router.add_get("/projectmap/{token}/", _root)
    app.router.add_get("/projectmap/{token}", _root)
    logger.info("projectmap endpoints registered on aiohttp app (live=%s)",
                LIVEMAP_ENABLED)


# --- self-contained SPA (static graph + live layer) -----------------------

_PAGE_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>🗺 Карта проекта — Lilush Bot</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:#0f1115; color:#e6e6e6; overflow:hidden; }
  header { padding:8px 12px; background:#161a22; border-bottom:1px solid #262c38;
           display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  header h1 { font-size:15px; margin:0; white-space:nowrap; }
  header .stat { font-size:11px; color:#8a93a6; }
  header button, header a.btn { background:#2b6cb0; color:#fff; border:0; border-radius:6px;
           padding:5px 9px; font-size:12px; cursor:pointer; text-decoration:none; }
  header button.mode { background:#252b38; }
  header button.mode.on { background:#2b6cb0; }
  #conn { display:inline-flex; align-items:center; gap:5px; font-size:11px; color:#8a93a6; }
  #conn .dot { width:9px; height:9px; border-radius:50%; background:#666; }
  #conn.live .dot { background:#48bb78; }
  #conn.err .dot { background:#e53e3e; }
  #conn.restart .dot { background:#ecc94b; }
  #banner { display:none; padding:5px 12px; background:#3b2f1d; color:#f6e05e; font-size:12px; }
  #wrap { position:relative; height: calc(100vh - 42px); }
  canvas.layer { position:absolute; inset:0; width:100%; height:100%; display:block; }
  #cv { z-index:1; background:#0f1115; }
  #fx { z-index:2; pointer-events:none; }
  #side { position:absolute; top:8px; right:8px; z-index:5; width:300px; max-height:70%;
          background:#141821ee; border:1px solid #262c38; border-radius:8px; padding:10px;
          overflow:auto; font-size:12px; display:none; }
  #side h2 { font-size:13px; margin:0 0 6px; }
  #side .seq { color:#9ae6b4; }
  #tasks { position:absolute; top:8px; left:8px; z-index:5; width:250px;
           background:#141821ee; border:1px solid #262c38; border-radius:8px;
           font-size:12px; overflow:hidden; }
  #tasks header2 { display:flex; justify-content:space-between; align-items:center;
           padding:7px 9px; cursor:pointer; background:#1a2030; font-weight:600; font-size:12px; }
  #tasklist { max-height:40vh; overflow:auto; }
  #tasklist .task { padding:6px 9px; border-top:1px solid #222836; cursor:pointer; }
  #tasklist .task .age { font-variant-numeric: tabular-nums; }
  #tasklist .task.err { color:#feb2b2; }
  #legend { position:absolute; bottom:8px; left:8px; z-index:5; font-size:10px; color:#8a93a6;
            background:#141821cc; border-radius:6px; padding:5px 8px; display:flex; gap:8px; align-items:center; }
  #legend .grad { width:70px; height:8px; border-radius:4px;
            background:linear-gradient(90deg,#ffffff,#ff2b2b); }
  #modal { position:fixed; inset:0; z-index:50; background:rgba(0,0,0,0.6);
           display:none; align-items:center; justify-content:center; }
  #modal .box { background:#1a1114; border:1px solid #7f2a2a; border-radius:10px;
           max-width:420px; width:92%; padding:16px; font-size:13px; }
  #modal .box h3 { margin:0 0 8px; font-size:15px; color:#feb2b2; }
  #modal .box code { display:block; margin:8px 0; padding:6px; background:#0f0a0c;
           border-radius:6px; font-size:11px; color:#fca5a5; word-break:break-all; }
  #modal .box .btns { display:flex; gap:8px; margin-top:10px; }
  #modal .box button { background:#2b6cb0; color:#fff; border:0; border-radius:6px;
           padding:6px 10px; font-size:12px; cursor:pointer; }
  #modal .box button.dim { background:#333b4d; }
  .muted { color:#6b7280; }
  ul,ol { padding-left:16px; margin:6px 0; }
  #search { background:#0f1115; color:#e6e6e6; border:1px solid #333b4d; border-radius:6px;
            padding:5px 8px; font-size:12px; width:130px; }
  #mini { position:absolute; bottom:8px; right:8px; z-index:5; width:150px; height:100px;
          background:#141821cc; border:1px solid #262c38; border-radius:6px; cursor:pointer; }
  #spark { display:block; width:100%; height:26px; border-top:1px solid #222836; }
  #fpsBox { position:absolute; top:8px; right:320px; z-index:6; font-size:11px;
            color:#9ae6b4; background:#141821cc; border-radius:6px; padding:3px 7px; display:none; }
  #undegrade { display:none; background:#744210; }
  @media (max-width: 767px) {
    #side { width:82%; }
    #tasks { width:190px; }
    #modal .box { max-width:none; width:96%; }
    #search { width:90px; }
    #mini { display:none; }
    #legend { padding:3px 6px; font-size:9px; }
  }
</style>
</head>
<body>
<header>
  <h1>🗺 Карта проекта</h1>
  <span id="conn"><span class="dot"></span><span id="connLabel">офлайн</span></span>
  <button class="mode on" id="mBlocks">Блоки</button>
  <button class="mode" id="mFuncs">Функции</button>
  <span class="stat" id="stat">загрузка…</span>
  <input id="search" placeholder="поиск…" />
  <a class="btn" id="exportBtn" href="./export">📤 Экспорт</a>
  <button id="reloadBtn">🔄</button>
  <button id="undegrade">Вернуть эффекты</button>
</header>
<div id="banner"></div>
<div id="weakbar" style="display:none;padding:5px 12px;background:#3b1d1d;color:#ffb4b4;font-size:12px;"></div>
<div id="wrap">
  <canvas id="cv" class="layer"></canvas>
  <canvas id="fx" class="layer"></canvas>
  <div id="tasks">
    <header2 id="tasksHead"><span>Активные задачи (<span id="taskCount">0</span>)</span><span id="tasksToggle">▾</span></header2>
    <div id="tasklist"></div>
    <canvas id="spark" width="250" height="26" title="событий/сек за 60 сек"></canvas>
  </div>
  <div id="side"></div>
  <div id="legend"><span>возраст задачи:</span><span class="grad"></span><span>0м → 10м+</span></div>
  <canvas id="mini" width="150" height="100" title="мини-карта"></canvas>
  <div id="fpsBox">FPS: —</div>
</div>
<div id="modal" role="alertdialog" aria-modal="true" aria-labelledby="modalTitle">
  <div class="box">
    <h3 id="modalTitle">⛔ Задача прервана из-за ошибки</h3>
    <div id="modalText"></div>
    <div id="modalNode" class="muted"></div>
    <code id="modalErr"></code>
    <div id="modalAge" class="muted"></div>
    <div id="modalMore" class="muted"></div>
    <div class="btns">
      <button id="modalShow">Показать на карте</button>
      <button id="modalClose" class="dim">Закрыть</button>
    </div>
  </div>
</div>
<script>
'use strict';
var LIVE = "__LIVE__" === "1";
var REDUCED = false;
try {
  var mq = window.matchMedia('(prefers-reduced-motion: reduce)');
  REDUCED = mq.matches;
  mq.addEventListener && mq.addEventListener('change', function(e){ REDUCED = e.matches; });
} catch(e){}

var DPR = Math.min(2, window.devicePixelRatio || 1);
var cv = document.getElementById('cv'), ctx = cv.getContext('2d');
var fx = document.getElementById('fx'), fctx = fx.getContext('2d');
var wrap = document.getElementById('wrap');

var MODE = 'blocks';                       // 'blocks' | 'funcs'
var DATA = null;                           // raw /data payload
var NODES = [], EDGES = [], byId = {};
var sel = null, hover = null;
var cam = { x: 0, y: 0, k: 1 };            // pan/zoom camera
var GRID = {};                             // spatial hash (64px cells)
var CAT_COLOR = { gen:'#63b3ed', tool:'#f6ad55', data:'#9ae6b4',
                  infra:'#b794f4', neutral:'#8a93a6', special:'#ffffff' };
var GLYPH = { gen:'▶', tool:'⚙', data:'🗄', infra:'⚡', neutral:'·', special:'' };

// ---------- camera helpers ----------
function toWorld(px, py){ return { x:(px - cam.x)/cam.k, y:(py - cam.y)/cam.k }; }
function toScreen(wx, wy){ return { x: wx*cam.k + cam.x, y: wy*cam.k + cam.y }; }

function resize(){
  var w = wrap.clientWidth, h = wrap.clientHeight;
  [cv, fx].forEach(function(c){
    c.width = Math.round(w * DPR); c.height = Math.round(h * DPR);
    c.getContext('2d').setTransform(DPR, 0, 0, DPR, 0, 0);
  });
  drawStatic();
}
window.addEventListener('resize', resize);

// ---------- positions in localStorage, keyed by mode (3.3) ----------
function posKey(){ return 'projectmap:pos:' + MODE; }
function savePositions(){
  try {
    var o = {};
    NODES.forEach(function(n){ o[n.id] = [Math.round(n.x), Math.round(n.y)]; });
    localStorage.setItem(posKey(), JSON.stringify(o));
  } catch(e){}
}
function loadPositions(){
  try { return JSON.parse(localStorage.getItem(posKey()) || '{}'); } catch(e){ return {}; }
}

// ---------- graph build ----------
var HOME = { gen:[0.25,0.22], tool:[0.75,0.22], data:[0.5,0.8], infra:[0.8,0.8],
             neutral:[0.5,0.45], special:[0.5,0.5] };

function buildGraph(){
  if (!DATA) return;
  var g = MODE === 'blocks' ? (DATA.blocks || {nodes:[],edges:[]})
                            : { nodes: DATA.nodes||[], edges: DATA.edges||[] };
  NODES = g.nodes.map(function(n){ return Object.assign({}, n); });
  EDGES = g.edges.slice();
  byId = {};
  var W = wrap.clientWidth || 800, H = wrap.clientHeight || 600;
  var saved = loadPositions();
  NODES.forEach(function(n, i){
    byId[n.id] = n;
    n.cat = n.category || (n.kind === 'async' ? 'neutral' : 'neutral');
    n.r = n.kind === 'special' ? (n.id === 'user' ? 16 : 12)
        : MODE === 'blocks' ? Math.min(14, 6 + Math.sqrt(n.size||1)) : 4;
    if (saved[n.id]) { n.x = saved[n.id][0]; n.y = saved[n.id][1]; n.pinned = true; }
    else {
      var home = HOME[n.cat] || HOME.neutral;
      n.x = home[0]*W + (Math.random()-0.5)*W*0.3;
      n.y = home[1]*H + (Math.random()-0.5)*H*0.3;
    }
    n.vx = 0; n.vy = 0;
  });
  if (byId['user'] && !saved['user']) { byId['user'].x = 40; byId['user'].y = H/2; }
  if (byId['llm_router'] && !saved['llm_router']) { byId['llm_router'].x = W*0.3; byId['llm_router'].y = H/2; }
  for (var s = 0; s < 180; s++) step();
  rebuildGrid();
  drawStatic();
}

function step(){
  var W = wrap.clientWidth, H = wrap.clientHeight;
  var i, j, n;
  for (i = 0; i < NODES.length; i++){ NODES[i].fx = 0; NODES[i].fy = 0; }
  for (i = 0; i < NODES.length; i++){
    for (j = i+1; j < NODES.length; j++){
      var a = NODES[i], b = NODES[j];
      var dx = a.x-b.x, dy = a.y-b.y, d2 = dx*dx+dy*dy || 1, d = Math.sqrt(d2);
      var f = 1600/d2;
      a.fx += dx/d*f; a.fy += dy/d*f; b.fx -= dx/d*f; b.fy -= dy/d*f;
    }
  }
  EDGES.forEach(function(e){
    var a = byId[e.source], b = byId[e.target]; if (!a || !b) return;
    var dx = b.x-a.x, dy = b.y-a.y, d = Math.sqrt(dx*dx+dy*dy) || 1;
    var f = (d - 110) * 0.02;
    a.fx += dx/d*f; a.fy += dy/d*f; b.fx -= dx/d*f; b.fy -= dy/d*f;
  });
  for (i = 0; i < NODES.length; i++){
    n = NODES[i];
    if (n.pinned) continue;
    var home = HOME[n.cat] || HOME.neutral;   // soft pull to category zone (7.3)
    n.fx += (home[0]*W - n.x) * 0.002; n.fy += (home[1]*H - n.y) * 0.002;
    if (n.id === 'user'){ n.fx += (40 - n.x) * 0.05; n.fy += (H/2 - n.y) * 0.01; }
    n.vx = (n.vx + n.fx) * 0.85; n.vy = (n.vy + n.fy) * 0.85;
    n.x += Math.max(-8, Math.min(8, n.vx)); n.y += Math.max(-8, Math.min(8, n.vy));
    n.x = Math.max(24, Math.min(W-24, n.x)); n.y = Math.max(24, Math.min(H-24, n.y));
  }
}

// ---------- spatial hash for hit-tests (7.2) ----------
function rebuildGrid(){
  GRID = {};
  NODES.forEach(function(n){
    var key = Math.floor(n.x/64) + ':' + Math.floor(n.y/64);
    (GRID[key] = GRID[key] || []).push(n);
  });
}
function hitTest(wx, wy){
  var cx = Math.floor(wx/64), cy = Math.floor(wy/64), best = null, bd = 1e9;
  for (var dx = -1; dx <= 1; dx++) for (var dy = -1; dy <= 1; dy++){
    var cell = GRID[(cx+dx) + ':' + (cy+dy)];
    if (!cell) continue;
    cell.forEach(function(n){
      var d = (n.x-wx)*(n.x-wx) + (n.y-wy)*(n.y-wy);
      if (d < bd){ bd = d; best = n; }
    });
  }
  return (best && bd < Math.pow((best.r+10)/cam.k + 6, 2)) ? best : null;
}

// ---------- static layer ----------
function drawStatic(){
  var W = wrap.clientWidth, H = wrap.clientHeight;
  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(cam.x, cam.y); ctx.scale(cam.k, cam.k);
  ctx.lineWidth = 1/cam.k;
  EDGES.forEach(function(e){
    var a = byId[e.source], b = byId[e.target]; if (!a || !b) return;
    var on = sel && (e.source === sel.id || e.target === sel.id);
    ctx.strokeStyle = on ? '#63b3ed' : 'rgba(120,130,150,0.18)';
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  });
  NODES.forEach(function(n){
    var on = sel && n.id === sel.id;
    var col = n.removed ? 'rgba(120,120,120,0.3)' : (CAT_COLOR[n.cat] || CAT_COLOR.neutral);
    // Рождение узла: вырастает из точки за ~0.8 сек со свечением (3.4)
    var br = n.birthScale !== undefined ? Math.max(0.05, n.birthScale) : 1;
    var r = n.r * br;
    if (!DEGRADED){ // cheap bloom: outer soft circle (7.3)
      ctx.fillStyle = col; ctx.globalAlpha = br < 1 ? 0.35 : 0.18;
      ctx.beginPath(); ctx.arc(n.x, n.y, r*1.9, 0, Math.PI*2); ctx.fill();
      ctx.globalAlpha = 1;
    }
    ctx.fillStyle = on ? '#63b3ed' : col;
    ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI*2); ctx.fill();
    if (MODE === 'blocks' || on || hover === n){
      ctx.fillStyle = '#e6e6e6'; ctx.font = (11/cam.k) + 'px system-ui';
      var label = n.name.length > 18 ? n.name.slice(0, 17) + '…' : n.name;
      ctx.fillText(label, n.x + n.r + 3, n.y + 3);
      var glyph = GLYPH[n.cat];
      if (glyph && n.kind !== 'special'){
        ctx.font = (Math.max(8, n.r) / cam.k * cam.k) + 'px system-ui';
        ctx.fillText(glyph, n.x - n.r/2, n.y + n.r/2);
      }
    }
    if (n.isNew && !n.removed){
      ctx.fillStyle = '#48bb78'; ctx.font = (9/cam.k) + 'px system-ui';
      ctx.fillText('NEW', n.x - 10, n.y - n.r - 4);
    }
  });
  ctx.restore();
}

// ---------- pan / zoom / drag (этап 1) ----------
var drag = null; // {type:'pan'|'node', ...}
cv.addEventListener('pointerdown', function(ev){
  cv.setPointerCapture(ev.pointerId);
  var w = toWorld(ev.offsetX, ev.offsetY);
  var n = hitTest(w.x, w.y);
  if (n) drag = { type:'node', n:n, moved:false };
  else drag = { type:'pan', sx:ev.offsetX, sy:ev.offsetY, cx:cam.x, cy:cam.y, moved:false };
});
cv.addEventListener('pointermove', function(ev){
  if (drag){
    drag.moved = true;
    if (drag.type === 'node'){
      var w = toWorld(ev.offsetX, ev.offsetY);
      drag.n.x = w.x; drag.n.y = w.y; drag.n.pinned = true;
      drawStatic();
    } else {
      cam.x = drag.cx + (ev.offsetX - drag.sx);
      cam.y = drag.cy + (ev.offsetY - drag.sy);
      drawStatic();
    }
    return;
  }
  var w2 = toWorld(ev.offsetX, ev.offsetY);
  var h = hitTest(w2.x, w2.y);
  if (h !== hover){ hover = h; drawStatic(); }
});
cv.addEventListener('pointerup', function(ev){
  if (drag && drag.type === 'node'){ rebuildGrid(); savePositions(); }
  if (drag && !drag.moved){
    var w = toWorld(ev.offsetX, ev.offsetY);
    var n = hitTest(w.x, w.y);
    if (n){ sel = n; showSide(n); } else { sel = null; document.getElementById('side').style.display = 'none'; }
    drawStatic();
  }
  drag = null;
});
cv.addEventListener('wheel', function(ev){
  ev.preventDefault();
  var k = Math.max(0.25, Math.min(4, cam.k * (ev.deltaY < 0 ? 1.12 : 0.89)));
  var w = toWorld(ev.offsetX, ev.offsetY);
  cam.x = ev.offsetX - w.x*k; cam.y = ev.offsetY - w.y*k; cam.k = k;
  drawStatic();
}, { passive:false });

// pinch zoom
var pinch = null;
cv.addEventListener('touchstart', function(ev){
  if (ev.touches.length === 2){
    var dx = ev.touches[0].clientX - ev.touches[1].clientX;
    var dy = ev.touches[0].clientY - ev.touches[1].clientY;
    pinch = { d: Math.sqrt(dx*dx+dy*dy), k: cam.k };
  }
}, { passive:true });
cv.addEventListener('touchmove', function(ev){
  if (pinch && ev.touches.length === 2){
    var dx = ev.touches[0].clientX - ev.touches[1].clientX;
    var dy = ev.touches[0].clientY - ev.touches[1].clientY;
    cam.k = Math.max(0.25, Math.min(4, pinch.k * Math.sqrt(dx*dx+dy*dy)/pinch.d));
    drawStatic();
  }
}, { passive:true });
cv.addEventListener('touchend', function(){ pinch = null; });

function flyTo(nodeId){
  var n = byId[nodeId]; if (!n) return;
  var W = wrap.clientWidth, H = wrap.clientHeight;
  cam.x = W/2 - n.x*cam.k; cam.y = H/2 - n.y*cam.k;
  drawStatic();
}

// ---------- mini-map (7.3): offscreen cache, redrawn every 500 ms ----------
var mini = document.getElementById('mini'), mctx = mini.getContext('2d');
var miniCache = document.createElement('canvas');
miniCache.width = 150; miniCache.height = 100;
var miniBounds = null;
function redrawMiniCache(){
  if (degradeLevel >= 1 || !NODES.length){ miniBounds = null; return; }
  var xs = NODES.map(function(n){ return n.x; }), ys = NODES.map(function(n){ return n.y; });
  var x0 = Math.min.apply(null, xs) - 30, x1 = Math.max.apply(null, xs) + 30;
  var y0 = Math.min.apply(null, ys) - 30, y1 = Math.max.apply(null, ys) + 30;
  var sx = 150/(x1-x0 || 1), sy = 100/(y1-y0 || 1), s = Math.min(sx, sy);
  miniBounds = { x0:x0, y0:y0, s:s };
  var c = miniCache.getContext('2d');
  c.clearRect(0, 0, 150, 100);
  c.strokeStyle = 'rgba(120,130,150,0.3)'; c.lineWidth = 0.5;
  EDGES.forEach(function(e){
    var a = byId[e.source], b = byId[e.target]; if (!a||!b) return;
    c.beginPath(); c.moveTo((a.x-x0)*s, (a.y-y0)*s); c.lineTo((b.x-x0)*s, (b.y-y0)*s); c.stroke();
  });
  NODES.forEach(function(n){
    c.fillStyle = CAT_COLOR[n.cat] || CAT_COLOR.neutral;
    c.beginPath(); c.arc((n.x-x0)*s, (n.y-y0)*s, Math.max(1.2, n.r*s), 0, Math.PI*2); c.fill();
  });
}
function drawMini(){
  if (degradeLevel >= 1){ mini.style.display = 'none'; return; }
  mini.style.display = '';
  if (!miniBounds) return;
  mctx.clearRect(0, 0, 150, 100);
  mctx.drawImage(miniCache, 0, 0);
  // viewport frame
  var W = wrap.clientWidth, H = wrap.clientHeight, mb = miniBounds;
  var wx0 = (0 - cam.x)/cam.k, wy0 = (0 - cam.y)/cam.k;
  var wx1 = (W - cam.x)/cam.k, wy1 = (H - cam.y)/cam.k;
  mctx.strokeStyle = '#63b3ed'; mctx.lineWidth = 1;
  mctx.strokeRect((wx0-mb.x0)*mb.s, (wy0-mb.y0)*mb.s, (wx1-wx0)*mb.s, (wy1-wy0)*mb.s);
}
setInterval(function(){ redrawMiniCache(); drawMini(); }, 500);
mini.addEventListener('click', function(ev){
  if (!miniBounds) return;
  var r = mini.getBoundingClientRect(), mb = miniBounds;
  var wx = (ev.clientX - r.left)/mb.s + mb.x0, wy = (ev.clientY - r.top)/mb.s + mb.y0;
  var W = wrap.clientWidth, H = wrap.clientHeight;
  cam.x = W/2 - wx*cam.k; cam.y = H/2 - wy*cam.k;
  drawStatic(); drawMini();
});

// ---------- sparkline «событий/сек» за 60 сек (7.3) ----------
var spark = document.getElementById('spark'), sctx = spark.getContext('2d');
var EPS = new Array(60).fill(0), epsNow = 0;
setInterval(function(){
  EPS.push(epsNow); EPS.shift(); epsNow = 0;
  if (tasksCollapsed) return;
  var w = spark.width, h = spark.height, max = Math.max(1, Math.max.apply(null, EPS));
  sctx.clearRect(0, 0, w, h);
  sctx.strokeStyle = '#48bb78'; sctx.lineWidth = 1; sctx.beginPath();
  EPS.forEach(function(v, i){
    var x = i/(EPS.length-1)*w, y = h - 2 - (v/max)*(h-6);
    i ? sctx.lineTo(x, y) : sctx.moveTo(x, y);
  });
  sctx.stroke();
  sctx.fillStyle = '#6b7280'; sctx.font = '8px system-ui';
  sctx.fillText('соб/с: ' + EPS[EPS.length-1] + ' (макс ' + max + ')', 4, 9);
}, 1000);

// ---------- side card ----------
function showSide(n){
  var side = document.getElementById('side');
  side.textContent = '';
  var h2 = document.createElement('h2'); h2.textContent = n.name; side.appendChild(h2);
  var meta = document.createElement('div'); meta.className = 'muted';
  meta.textContent = (n.id || '') + ' · ' + (n.kind || '') +
    (n.size ? ' · функций: ' + n.size : '');
  side.appendChild(meta);
  var st = (DATA && DATA.node_stats || {})[n.id];
  if (st){
    var stats = document.createElement('div'); stats.className = 'muted';
    stats.textContent = 'вызовов сегодня: ' + st.calls +
      ' · p50: ' + st.p50 + 'мс · p95: ' + st.p95 + 'мс';
    side.appendChild(stats);
  }
  var order = n.calls_order || [];
  if (order.length){
    var p = document.createElement('p'); p.innerHTML = '<b>Порядок вызовов:</b>'; side.appendChild(p);
    var ol = document.createElement('ol'); ol.className = 'seq';
    order.forEach(function(c){ var li = document.createElement('li'); li.textContent = c; ol.appendChild(li); });
    side.appendChild(ol);
  }
  var cust = (DATA && DATA.custom || []).filter(function(e){ return e.from === n.id || e.to === n.id; });
  if (cust.length){
    var p2 = document.createElement('p'); p2.innerHTML = '<b>Курируемые связки:</b>'; side.appendChild(p2);
    var ul = document.createElement('ul');
    cust.forEach(function(e){
      var li = document.createElement('li');
      li.textContent = e.from + ' → ' + e.to + (e.note ? ' (' + e.note + ')' : '');
      ul.appendChild(li);
    });
    side.appendChild(ul);
  }
  side.style.display = 'block';
}

// ---------- data load ----------
function load(){
  fetch('./data').then(function(r){ return r.json(); }).then(function(d){
    // Дифф графа → «🧩 Карта обновлена: +2 узла, −1 узел» (3.4)
    var prevIds = null;
    if (DATA){
      prevIds = {};
      NODES.forEach(function(n){ prevIds[n.id] = true; });
    }
    DATA = d;
    var wb = document.getElementById('weakbar');
    if ((d.weak||[]).length){ wb.style.display='block'; wb.textContent = '⚠️ Слабые места: ' + d.weak.join(' · '); }
    else wb.style.display = 'none';
    document.getElementById('stat').textContent =
      (d.node_count||0) + ' функций · ' + (d.edge_count||0) + ' связей · ' +
      ((d.blocks&&d.blocks.nodes||[]).length) + ' блоков';
    buildGraph();
    if (prevIds){
      var added = 0, removed = 0, freshIds = {};
      NODES.forEach(function(n){
        freshIds[n.id] = true;
        if (!prevIds[n.id]){ added++; n.born = performance.now(); }
      });
      Object.keys(prevIds).forEach(function(id){ if (!freshIds[id]) removed++; });
      if (added || removed){
        var b = document.getElementById('banner');
        b.style.display = 'block';
        b.textContent = '🧩 Карта обновлена: ' +
          (added ? '+' + added + ' узл.' : '') +
          (added && removed ? ', ' : '') +
          (removed ? '\u2212' + removed + ' узл.' : '');
        setTimeout(function(){ if (b.textContent.indexOf('🧩') === 0) b.style.display = 'none'; }, 8000);
        if (added && !REDUCED) animateBirths();
      }
    }
  }).catch(function(){ document.getElementById('stat').textContent = 'ошибка загрузки'; });
}

// Анимация рождения узла: вырастает из точки за ~0.8 сек со свечением (3.4)
function animateBirths(){
  var t0 = performance.now();
  (function tick(){
    var t = (performance.now() - t0)/800;
    NODES.forEach(function(n){
      if (n.born) n.birthScale = Math.min(1, t);
    });
    drawStatic();
    if (t < 1) requestAnimationFrame(tick);
    else NODES.forEach(function(n){ delete n.born; delete n.birthScale; });
  })();
}
document.getElementById('reloadBtn').onclick = load;
document.getElementById('mBlocks').onclick = function(){ setMode('blocks'); };
document.getElementById('mFuncs').onclick = function(){ setMode('funcs'); };
function setMode(m){
  if (m === MODE) return;
  savePositions(); MODE = m;
  document.getElementById('mBlocks').className = 'mode' + (m==='blocks'?' on':'');
  document.getElementById('mFuncs').className = 'mode' + (m==='funcs'?' on':'');
  sel = null; document.getElementById('side').style.display = 'none';
  buildGraph();
}
document.getElementById('search').addEventListener('input', function(e){
  var q = e.target.value.toLowerCase().trim(); if (!q) return;
  var n = NODES.find(function(x){ return (x.name||'').toLowerCase().indexOf(q) >= 0 ||
                                         (x.id||'').toLowerCase().indexOf(q) >= 0; });
  if (n){ sel = n; showSide(n); flyTo(n.id); }
});

// ---------- tasks panel ----------
var TASKS = {};   // task_id -> {text, started_at, node, error, endedAt, reason}
var tasksCollapsed = window.innerWidth < 768;
document.getElementById('tasksHead').onclick = function(){
  tasksCollapsed = !tasksCollapsed; renderTasks();
};
function fmtAge(sec){
  sec = Math.max(0, Math.floor(sec));
  return Math.floor(sec/60) + ':' + ('0' + (sec%60)).slice(-2);
}
function taskAgeSec(t){ return (Date.now()/1000 + clockOffset) - (t.started_at || Date.now()/1000); }
function ageColor(sec){
  var m = sec/60, r = Math.max(0, Math.min(1, (m-1)/9));
  var g = Math.round(255 - r*(255-43)), b = Math.round(255 - r*(255-43));
  return 'rgb(255,' + g + ',' + b + ')';
}
function renderTasks(){
  var list = document.getElementById('tasklist');
  document.getElementById('tasksToggle').textContent = tasksCollapsed ? '▸' : '▾';
  list.style.display = tasksCollapsed ? 'none' : 'block';
  var ids = Object.keys(TASKS);
  document.getElementById('taskCount').textContent = ids.filter(function(id){ return !TASKS[id].endedAt; }).length;
  if (tasksCollapsed) return;
  list.textContent = '';
  ids.forEach(function(id){
    var t = TASKS[id];
    var div = document.createElement('div');
    div.className = 'task' + (t.reason === 'error' ? ' err' : '');
    var age = taskAgeSec(t);
    var line1 = document.createElement('div');
    line1.textContent = t.text || ('задача ' + id);
    var line2 = document.createElement('div'); line2.className = 'muted';
    var status = t.endedAt ? (t.reason === 'error' ? '⛔ ошибка'
                            : t.reason === 'stale' ? 'истекла'
                            : t.reason === 'orphaned' ? 'потеряна при рестарте'
                            : 'готово')
                           : ('на узле: ' + (t.node || '—'));
    line2.innerHTML = '<span class="age"></span> · ';
    line2.querySelector('.age').textContent = fmtAge(age);
    line2.appendChild(document.createTextNode(status));
    div.appendChild(line1); div.appendChild(line2);
    div.onclick = function(){ if (t.node) flyTo(resolveNode(t.node)); };
    list.appendChild(div);
  });
}
setInterval(renderTasks, 1000);

// resolve a tracer node name to a node id on the current graph
function resolveNode(name){
  if (byId[name]) return name;
  var hit = NODES.find(function(n){ return n.id.indexOf(name) >= 0 || name.indexOf(n.id) >= 0; });
  return hit ? hit.id : name;
}

// ---------- live layer: waves, pulses, flashes ----------
var WAVES = [];      // {from,to,start,dur,taskStarted,thick}
var PULSES = {};     // nodeId -> {since, taskStarted}
var FLASHES = [];    // {node, start}
var TRAILS = [];     // {from,to,end}
var DEGRADED = false, degradeLevel = 0, lowFpsSince = 0;
var rafOn = false, lastFrame = 0;

// Возврат из DEGRADED — только вручную кнопкой (8.2, без осцилляции).
var undegradeBtn = document.getElementById('undegrade');
undegradeBtn.onclick = function(){
  degradeLevel = 0; DEGRADED = false; lowFpsSince = 0;
  undegradeBtn.style.display = 'none';
  drawStatic(); startRaf();
};

// dev-счётчик FPS в углу: ?fps=1 (8.4)
var SHOW_FPS = /(\?|&)fps=1/.test(location.search);
var fpsBox = document.getElementById('fpsBox');
if (SHOW_FPS) fpsBox.style.display = 'block';
var fpsAccum = 0, fpsFrames = 0, fpsLastShown = 0;

function addWave(fromName, toName, taskStarted){
  var a = byId[resolveNode(fromName)], b = byId[resolveNode(toName)];
  if (!a || !b) return;
  if (REDUCED){
    TRAILS.push({ from:a.id, to:b.id, end: performance.now() + 3000, color: ageColor(nowAge(taskStarted)) });
    startRaf(); return;
  }
  WAVES.push({ from:a.id, to:b.id, start: performance.now(),
               dur: 1500 + Math.random()*500, taskStarted: taskStarted,
               thick: fromName === 'user' });
  startRaf();
}
function nowAge(taskStarted){
  if (!taskStarted) return 0;
  return (Date.now()/1000 + clockOffset) - taskStarted;
}
function startRaf(){ if (!rafOn){ rafOn = true; requestAnimationFrame(frame); } }

function frame(ts){
  if (document.hidden){ rafOn = false; return; }
  // FPS degrade ladder (8.2)
  if (lastFrame){
    var fps = 1000/(ts - lastFrame);
    if (SHOW_FPS){
      fpsAccum += fps; fpsFrames++;
      if (ts - fpsLastShown > 500){
        fpsBox.textContent = 'FPS: ' + Math.round(fpsAccum/fpsFrames) +
          (degradeLevel ? ' · deg ' + degradeLevel : '');
        fpsAccum = 0; fpsFrames = 0; fpsLastShown = ts;
      }
    }
    if (fps < 30){ if (!lowFpsSince) lowFpsSince = ts;
      if (ts - lowFpsSince > 3000 && degradeLevel < 3){ degradeLevel++; DEGRADED = true;
        undegradeBtn.style.display = 'inline-block';
        console.log('[map] degraded to level ' + degradeLevel); lowFpsSince = 0; drawStatic(); } }
    else lowFpsSince = 0;
  }
  lastFrame = ts;
  var W = wrap.clientWidth, H = wrap.clientHeight;
  fctx.clearRect(0, 0, W, H);
  fctx.save(); fctx.translate(cam.x, cam.y); fctx.scale(cam.k, cam.k);
  var alive = false;

  if (degradeLevel < 2) TRAILS = TRAILS.filter(function(t){
    if (performance.now() > t.end) return false;
    var a = byId[t.from], b = byId[t.to]; if (!a||!b) return false;
    var left = (t.end - performance.now())/3000;
    fctx.strokeStyle = t.color || '#ffffff'; fctx.globalAlpha = 0.35*left;
    fctx.lineWidth = 2/cam.k;
    fctx.beginPath(); fctx.moveTo(a.x,a.y); fctx.lineTo(b.x,b.y); fctx.stroke();
    fctx.globalAlpha = 1; alive = true; return true;
  });

  WAVES = WAVES.filter(function(w){
    var p = (performance.now() - w.start)/w.dur;
    var a = byId[w.from], b = byId[w.to]; if (!a||!b) return false;
    if (p >= 1){
      TRAILS.push({ from:w.from, to:w.to, end: performance.now()+3000,
                    color: ageColor(nowAge(w.taskStarted)) });
      return false;
    }
    var x = a.x + (b.x-a.x)*p, y = a.y + (b.y-a.y)*p;
    var col = ageColor(nowAge(w.taskStarted));
    var rad = (w.thick ? 14 : 10)/Math.sqrt(cam.k);
    if (degradeLevel < 3){
      var grad = fctx.createRadialGradient(x, y, 0, x, y, rad);
      grad.addColorStop(0, col); grad.addColorStop(1, 'rgba(255,255,255,0)');
      fctx.fillStyle = grad;
    } else fctx.fillStyle = col;
    fctx.beginPath(); fctx.arc(x, y, rad, 0, Math.PI*2); fctx.fill();
    alive = true; return true;
  });

  Object.keys(PULSES).forEach(function(nid){
    var pu = PULSES[nid], n = byId[resolveNode(nid)]; if (!n) return;
    var age = nowAge(pu.taskStarted);
    var col = ageColor(age);
    if (REDUCED){
      fctx.strokeStyle = col; fctx.lineWidth = 2/cam.k;
      fctx.beginPath(); fctx.arc(n.x, n.y, n.r+5, 0, Math.PI*2); fctx.stroke();
    } else {
      var ph = (performance.now()/600) % (Math.PI*2);
      fctx.strokeStyle = col; fctx.globalAlpha = 0.5 + 0.4*Math.sin(ph);
      fctx.lineWidth = 2/cam.k;
      fctx.beginPath(); fctx.arc(n.x, n.y, n.r + 4 + 2*Math.sin(ph), 0, Math.PI*2); fctx.stroke();
      fctx.globalAlpha = 1;
    }
    fctx.fillStyle = '#e6e6e6'; fctx.font = (10/cam.k)+'px system-ui';
    fctx.fillText(fmtAge(age), n.x + n.r + 4, n.y - n.r - 2);
    if (age > 600){
      fctx.fillStyle = '#ff2b2b'; fctx.font = (9/cam.k)+'px system-ui';
      fctx.fillText('⚠ 10м+ — возможна потеря контекста', n.x + n.r + 4, n.y - n.r - 14);
    }
    alive = true;
  });

  FLASHES = FLASHES.filter(function(f){
    var n = byId[resolveNode(f.node)]; if (!n) return false;
    var p = (performance.now() - f.start)/900;
    if (p >= 1) return false;
    fctx.strokeStyle = 'rgba(255,43,43,' + (1-p) + ')';
    fctx.lineWidth = 3/cam.k;
    fctx.beginPath(); fctx.arc(n.x, n.y, n.r + p*38, 0, Math.PI*2); fctx.stroke();
    alive = true; return true;
  });

  fctx.restore();
  if (alive || Object.keys(PULSES).length){ requestAnimationFrame(frame); }
  else { rafOn = false; }
}

// ---------- error modal (4.5) ----------
var modalQueue = [], modalOpen = null, modalTimer = null, modalDedup = {};
function showError(ev){
  var key = (ev.node||'') + '|' + (ev.error||'');
  var now = Date.now();
  if (modalDedup[key] && now - modalDedup[key].ts < 60000){
    modalDedup[key].n++;
    if (modalOpen && modalOpen.key === key)
      document.getElementById('modalMore').textContent = '×' + modalDedup[key].n;
    return;
  }
  modalDedup[key] = { ts: now, n: 1 };
  if (modalOpen){
    if (modalQueue.length >= 2){
      document.getElementById('modalMore').textContent = 'и ещё ' + (modalQueue.length) + ' ошибок';
      return;
    }
    modalQueue.push(ev); return;
  }
  openModal(ev, key);
}
function openModal(ev, key){
  modalOpen = { ev: ev, key: key || ((ev.node||'')+'|'+(ev.error||'')) };
  var t = TASKS[ev.task_id] || {};
  document.getElementById('modalText').textContent = '«' + (ev.task_text || t.text || ev.task_id) + '»';
  document.getElementById('modalNode').textContent = 'Остановилась на узле: ' + (ev.node || '—');
  document.getElementById('modalErr').textContent = ev.error || '';
  var age = t.started_at ? taskAgeSec(t) : 0;
  document.getElementById('modalAge').textContent = age ? ('Прожила: ' + fmtAge(age)) : '';
  document.getElementById('modalMore').textContent = '';
  var m = document.getElementById('modal');
  m.style.display = 'flex';
  document.getElementById('modalClose').focus();
  clearTimeout(modalTimer);
  modalTimer = setTimeout(closeModal, 15000);
  document.getElementById('modalShow').onclick = function(){
    if (ev.node) flyTo(resolveNode(ev.node));
    closeModal();
  };
}
function closeModal(){
  clearTimeout(modalTimer);
  document.getElementById('modal').style.display = 'none';
  modalOpen = null;
  var next = modalQueue.shift();
  if (next) openModal(next);
}
document.getElementById('modalClose').onclick = closeModal;
document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && modalOpen) closeModal(); });

// ---------- event application ----------
function applyEvent(ev){
  if (!ev || (ev.v||1) > 1) return;   // forward compatibility
  epsNow++;                            // sparkline «событий/сек» (7.3)
  var t;
  switch (ev.type){
    case 'task_start':
      TASKS[ev.task_id] = { text: ev.task_text, started_at: ev.task_started_at || ev.ts,
                            node: 'user' };
      addWave('user', 'llm_router', ev.task_started_at || ev.ts);
      break;
    case 'hop':
      t = TASKS[ev.task_id];
      if (t){ t.node = ev.to; }
      addWave(ev.from, ev.to, ev.task_started_at || (t && t.started_at));
      break;
    case 'node_start':
      t = TASKS[ev.task_id];
      if (t) t.node = ev.node;
      PULSES[ev.node] = { since: performance.now(),
                          taskStarted: ev.task_started_at || (t && t.started_at) };
      startRaf();
      break;
    case 'node_end':
      delete PULSES[ev.node];
      break;
    case 'task_error':
      t = TASKS[ev.task_id];
      if (t){ t.reason = 'error'; }
      FLASHES.push({ node: ev.node, start: performance.now() });
      showError(ev);
      startRaf();
      break;
    case 'task_end':
      t = TASKS[ev.task_id];
      if (t){
        t.endedAt = Date.now(); t.reason = t.reason || ev.reason || 'done';
        if (ev.reason !== 'error' && t.node)
          addWave(t.node, 'user', t.started_at);
        delete PULSES[t.node];
        setTimeout(function(){ delete TASKS[ev.task_id]; renderTasks(); },
                   ev.reason === 'error' ? 60000 : 8000);
      }
      break;
    case 'graph_update':
      scheduleGraphReload();
      break;
    case 'snapshot':
      applySnapshot(ev);
      break;
  }
  renderTasks();
}
function applySnapshot(snap){
  // Задачи, которых нет в снапшоте (напр. до рестарта бота), помечаются
  // «orphaned» и видны в панели ещё 8 сек — «вечных» красных волн нет (8.1).
  var fresh = {};
  (snap.tasks || []).forEach(function(st){ fresh[st.task_id] = true; });
  Object.keys(TASKS).forEach(function(id){
    var t = TASKS[id];
    if (!fresh[id] && !t.endedAt){
      t.endedAt = Date.now(); t.reason = 'orphaned';
      setTimeout(function(){ delete TASKS[id]; renderTasks(); }, 8000);
    }
  });
  WAVES = []; PULSES = {};
  (snap.tasks || []).forEach(function(st){
    TASKS[st.task_id] = { text: st.task_text, started_at: st.task_started_at, node: st.node };
    if (st.node && st.node !== 'user')
      PULSES[st.node] = { since: performance.now(), taskStarted: st.task_started_at };
  });
  if (snap.graph_hash && DATA && DATA.graph_hash && snap.graph_hash !== DATA.graph_hash)
    scheduleGraphReload();
  renderTasks(); startRaf();
}
var graphReloadTimer = null;
function scheduleGraphReload(){   // debounced ≤ 1/10s (3.4)
  if (graphReloadTimer) return;
  graphReloadTimer = setTimeout(function(){ graphReloadTimer = null; load(); }, 2000);
}

// ---------- WS state machine (6.5): CONNECTING → SYNCING → LIVE ----------
var ws = null, wsState = 'IDLE', epoch = '', lastSeq = 0, clockOffset = 0;
var retry = 0, retryTimer = null, hiddenAt = 0, syncBuffer = [];

function setConn(cls, label){
  var c = document.getElementById('conn');
  c.className = cls; document.getElementById('connLabel').textContent = label;
  var b = document.getElementById('banner');
  if (cls === 'err'){ b.style.display = 'block'; b.textContent = 'переподключение…'; }
  else if (cls === 'restart'){ b.style.display = 'block'; b.textContent = 'Бот перезапускается…'; }
  else b.style.display = 'none';
}

function connect(){
  if (!LIVE) return;
  if (ws){ try { ws.onclose = ws.onerror = ws.onmessage = null; ws.close(); } catch(e){} ws = null; }
  wsState = 'CONNECTING'; setConn('', 'подключение…');
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try { ws = new WebSocket(proto + '//' + location.host + location.pathname.replace(/\/?$/, '/') + 'ws'); }
  catch(e){ scheduleReconnect(); return; }
  ws.onopen = function(){
    wsState = 'SYNCING'; syncBuffer = [];
    setConn('', 'синхронизация…');
    ws.send(JSON.stringify({ hello: { epoch: epoch, last_seq: lastSeq,
                                      graph_hash: DATA && DATA.graph_hash || '' } }));
    ws.send(JSON.stringify({ ping: Date.now()/1000 }));
  };
  ws.onmessage = function(m){
    var d; try { d = JSON.parse(m.data); } catch(e){ return; }
    if (!d || (d.v||1) > 1) return;
    if (d.type === 'resync'){
      epoch = d.epoch;
      if (d.resync === 'snapshot') lastSeq = d.seq;
      return;
    }
    if (d.type === 'pong'){
      clockOffset = d.ts - Date.now()/1000;  // coarse clock offset (5.4)
      return;
    }
    if (d.type === 'snapshot'){
      applySnapshot(d); lastSeq = d.seq || lastSeq;
      finishSync(); return;
    }
    if (d.batch){
      if (wsState === 'SYNCING'){
        // batches during SYNCING are buffered, applied after state (6.5)
        syncBuffer.push(d); finishSync(); flushSyncBuffer(); return;
      }
      d.batch.forEach(function(ev){
        if (ev.seq && ev.seq <= lastSeq) return; // duplicate seq — ignored
        if (ev.seq) lastSeq = ev.seq;
        applyEvent(ev);
      });
    }
  };
  ws.onclose = function(e){
    var was = wsState; wsState = 'CLOSED';
    if (e && e.code === 1001){ setConn('restart', 'бот перезапускается'); }
    else if (was !== 'IDLE'){ setConn('err', 'офлайн'); }
    scheduleReconnect();
  };
  ws.onerror = function(){ try { ws.close(); } catch(e){} };
}
function finishSync(){
  if (wsState === 'SYNCING'){ wsState = 'LIVE'; retry = 0; setConn('live', 'live'); }
}
function flushSyncBuffer(){
  var buf = syncBuffer; syncBuffer = [];
  buf.forEach(function(d){
    (d.batch||[]).forEach(function(ev){
      if (ev.seq && ev.seq <= lastSeq) return;
      if (ev.seq) lastSeq = ev.seq;
      applyEvent(ev);
    });
  });
}
function scheduleReconnect(){
  if (!LIVE) return;
  clearTimeout(retryTimer);
  if (document.hidden) return;         // no retries in background (6.2)
  var base = Math.min(30000, 1000 * Math.pow(2, retry++));
  var jitter = 0.8 + Math.random()*0.4;
  retryTimer = setTimeout(connect, base * jitter);
}

// Mobile Safari zombie sockets (6.2): forced resync after > 5 s in bg.
function forceResyncIfStale(){
  if (!LIVE) return;
  if (hiddenAt && Date.now() - hiddenAt > 5000){
    retry = 0; clearTimeout(retryTimer); connect();
  }
  hiddenAt = 0;
}
document.addEventListener('visibilitychange', function(){
  if (document.hidden){ hiddenAt = Date.now(); }
  else {
    forceResyncIfStale();
    if (wsState === 'CLOSED' || wsState === 'IDLE'){ retry = 0; connect(); }
    startRaf();
  }
});
window.addEventListener('pageshow', function(e){
  if (e.persisted){ hiddenAt = hiddenAt || (Date.now() - 6000); forceResyncIfStale(); }
});

// ---------- boot ----------
resize(); load();
if (LIVE){ connect(); setInterval(function(){
  if (ws && wsState === 'LIVE'){ try { ws.send(JSON.stringify({ ping: Date.now()/1000 })); } catch(e){} }
}, 30000); }
else setConn('', 'статичный режим');
</script>
</body>
</html>
"""
