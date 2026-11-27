"""
Debate-II — Multi-Door Multi-AI Debate Server.

Architecture:
- Multiple independent "doors" (debate rooms). Each door has its own state,
slots, agenda, containers, archive. Doors are isolated.
- Doors are created/deleted dynamically via the UI or POST/DELETE /doors.
- Each door has its own URL: /door/{door_id}.
- Self-ping removed — external bot keeps the server alive.

Routers:
- debate.py — per-door debate endpoints
- containers.py — per-door containers and archive endpoints
"""

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

DEBATE_TOKEN = os.environ.get("DEBATE_TOKEN", "")
CONSENSUS_MODE = os.environ.get("CONSENSUS_MODE", "unanimous").lower()
STATE_DIR = Path(os.environ.get("STATE_DIR", "./data"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
DOORS_FILE = STATE_DIR / "doors.json"
MAX_DOORS = int(os.environ.get("MAX_DOORS", "20"))
# How many doors to pre-create on first boot. Default 0 — the owner generates
# doors on demand via the bot's [+] button. Set >0 to pre-seed. Capped at MAX_DOORS.
SEED_DOORS = min(int(os.environ.get("SEED_DOORS", "0")), MAX_DOORS)

_log = logging.getLogger("debate-ii")

# Per-door asyncio locks (created on demand).
_locks: dict = {}


def check_token(token):
  if not DEBATE_TOKEN:
      raise HTTPException(500, "Server misconfigured: DEBATE_TOKEN not set")
  if not token or not hmac.compare_digest(token, DEBATE_TOKEN):
      raise HTTPException(401, "Invalid token")


def doors_state_lock(door_id: int) -> asyncio.Lock:
  if door_id not in _locks:
      _locks[door_id] = asyncio.Lock()
  return _locks[door_id]


def _door_dir(door_id: int) -> Path:
  p = STATE_DIR / f"door_{int(door_id)}"
  p.mkdir(parents=True, exist_ok=True)
  return p


def _door_state_file(door_id: int) -> Path:
  return _door_dir(door_id) / "state.json"


def _load_doors_registry() -> dict:
  if not DOORS_FILE.exists():
      return {"next_id": 1, "doors": {}}
  try:
      return json.loads(DOORS_FILE.read_text(encoding="utf-8"))
  except Exception:
      return {"next_id": 1, "doors": {}}


def _save_doors_registry(reg: dict) -> None:
  tmp = DOORS_FILE.with_suffix(".tmp")
  tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
  tmp.replace(DOORS_FILE)


def door_exists(door_id: int) -> bool:
  reg = _load_doors_registry()
  return str(door_id) in reg["doors"]


def load_door_state(door_id: int) -> dict:
  from debate import empty_door_state
  f = _door_state_file(door_id)
  if not f.exists():
      reg = _load_doors_registry()
      meta = reg["doors"].get(str(door_id), {})
      return empty_door_state(door_id, meta.get("title", ""), meta.get("topic", ""),
                              meta.get("link", ""), CONSENSUS_MODE)
  try:
      return json.loads(f.read_text(encoding="utf-8"))
  except Exception:
      return empty_door_state(door_id, "", "", "", CONSENSUS_MODE)


def save_door_state(door_id: int, state: dict) -> None:
  f = _door_state_file(door_id)
  tmp = f.with_suffix(".tmp")
  tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
  tmp.replace(f)


def find_slot_by_token(state: dict, slot_token: str):
  for s in state.get("slots", []):
      if s.get("occupied") and s.get("token") == slot_token:
          return s
  return None


def _seed_doors_if_empty() -> None:
  """Pre-create SEED_DOORS doors on first boot so debaters can just walk in."""
  reg = _load_doors_registry()
  if reg["doors"]:
      return
  from debate import empty_door_state
  for _ in range(SEED_DOORS):
      did = reg["next_id"]
      reg["next_id"] += 1
      title = f"Дверь {did}"
      reg["doors"][str(did)] = {
          "title": title, "topic": "", "link": "",
          "created_at": time.time(),
      }
      _door_dir(did)
      save_door_state(did, empty_door_state(did, title, "", "", CONSENSUS_MODE))
  _save_doors_registry(reg)
  _log.info("seeded %d doors", SEED_DOORS)


@asynccontextmanager
async def lifespan(app: FastAPI):
  if not DOORS_FILE.exists():
      _save_doors_registry({"next_id": 1, "doors": {}})
  if SEED_DOORS > 0:
      _seed_doors_if_empty()
  yield


app = FastAPI(title="Debate-II Multi-Door", lifespan=lifespan)

# Routers are imported here (after shared helpers are defined) and mounted.
from debate import router as debate_router  # noqa: E402
from containers import router as containers_router  # noqa: E402

app.include_router(debate_router)
app.include_router(containers_router)


@app.get("/healthz")
async def healthz():
  return {"ok": True, "ts": time.time(), "consensus_mode": CONSENSUS_MODE}


# ----------------------------- DOOR MANAGEMENT -----------------------------

@app.get("/doors")
async def doors_list(token: str = Query(...)):
  check_token(token)
  reg = _load_doors_registry()
  out = []
  for did_str, meta in sorted(reg["doors"].items(), key=lambda kv: int(kv[0])):
      did = int(did_str)
      try:
          st = load_door_state(did)
          connections = sum(1 for s in st.get("slots", [])
                            if s.get("occupied") and s.get("status") != "offline")
          max_slots = st.get("max_slots", 5)
          active = st.get("session_active", False)
          title = st.get("title", "") or meta.get("title", "")
      except Exception:
          connections, max_slots, active, title = 0, 5, False, meta.get("title", "")
      out.append({
          "door_id": did,
          "title": title or meta.get("title", "") or f"Door {did}",
          "topic": meta.get("topic", ""),
          "link": meta.get("link", ""),
          "url": f"/door/{did}",
          "connections": connections,
          "max_slots": max_slots,
          "session_active": active,
          "created_at": meta.get("created_at"),
      })
  return {"total_doors": len(out), "max_doors": MAX_DOORS, "doors": out}


@app.post("/doors")
async def doors_create(req: Request, token: str = Query(...)):
  check_token(token)
  body = await req.json()
  title = (body.get("title") or "").strip()
  topic = (body.get("topic") or "").strip()
  link = (body.get("link") or "").strip()
  reg = _load_doors_registry()
  if len(reg["doors"]) >= MAX_DOORS:
      raise HTTPException(400, f"max doors reached ({MAX_DOORS})")
  did = reg["next_id"]
  reg["next_id"] += 1
  reg["doors"][str(did)] = {
      "title": title or f"Door {did}",
      "topic": topic,
      "link": link,
      "created_at": time.time(),
  }
  _save_doors_registry(reg)
  _door_dir(did)
  from debate import empty_door_state
  save_door_state(did, empty_door_state(did, title or f"Door {did}", topic, link, CONSENSUS_MODE))
  return {
      "ok": True,
      "door_id": did,
      "title": title or f"Door {did}",
      "topic": topic,
      "link": link,
      "url": f"/door/{did}",
  }


@app.delete("/doors/{door_id}")
async def doors_delete(door_id: int, token: str = Query(...)):
  check_token(token)
  reg = _load_doors_registry()
  if str(door_id) not in reg["doors"]:
      raise HTTPException(404, f"door {door_id} not found")
  del reg["doors"][str(door_id)]
  _save_doors_registry(reg)
  import shutil
  d = _door_dir(door_id)
  try:
      shutil.rmtree(d)
  except Exception:
      pass
  _locks.pop(door_id, None)
  return {"ok": True, "deleted_door_id": door_id}


# ----------------------------- UI -----------------------------

@app.get("/", response_class=HTMLResponse)
async def root():
  return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Debate-II — двери</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:900px;
margin:2em auto;padding:0 1em;color:#222;background:#fafafa}
h1{margin-bottom:.2em}
.sub{color:#666;margin-bottom:1.5em}
.bar{display:flex;gap:.5em;align-items:center;margin-bottom:1em}
input,button{padding:.5em .8em;font-size:14px;border-radius:6px;border:1px solid #ccc}
input{flex:1}
button{cursor:pointer;background:#fff}
button.primary{background:#0a7;color:#fff;border-color:#0a7;font-weight:600}
button.danger{background:#e44;color:#fff;border-color:#e44}
.doors{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1em}
.door{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:1em;
box-shadow:0 1px 3px rgba(0,0,0,.04)}
.door h3{margin:0 0 .3em 0}
.door .meta{color:#666;font-size:13px;margin:.2em 0}
.door .url{font-family:monospace;font-size:12px;background:#f3f3f3;padding:2px 6px;
border-radius:4px;display:inline-block;margin:.4em 0}
.door .row{display:flex;justify-content:space-between;align-items:center;margin-top:.6em}
.badge{background:#0a7;color:#fff;padding:2px 8px;border-radius:10px;font-size:12px}
.badge.idle{background:#aaa}
.copy{font-size:12px}
#status{color:#666;font-size:13px;margin:.5em 0}
.empty{color:#888;text-align:center;padding:2em;background:#fff;border-radius:8px;
border:1px dashed #ccc}
</style></head>
<body>
<h1>🎙 Debate-II</h1>
<p class="sub">Сервер дебатов на несколько комнат. Каждая дверь — своя независимая дебат-сессия со своими участниками, темой и контейнерами.</p>
<div class="bar">
<input id="token" type="password" placeholder="DEBATE_TOKEN" />
<button onclick="refresh()">Загрузить</button>
</div>
<div class="bar">
<input id="newTitle" placeholder="Название двери (например: ИИ и этика)" />
<input id="newTopic" placeholder="Тема (опционально)" />
<button class="primary" onclick="createDoor()">➕ Создать дверь</button>
</div>
<div id="status">Введите токен и нажмите «Загрузить».</div>
<div id="doors" class="doors"></div>

<script>
function getToken(){
const t = document.getElementById('token').value.trim();
if(!t){ alert('Введите DEBATE_TOKEN'); return null; }
localStorage.setItem('debateToken', t);
return t;
}
document.getElementById('token').value = localStorage.getItem('debateToken') || '';

async function refresh(){
const t = getToken(); if(!t) return;
const s = document.getElementById('status');
s.textContent = 'Загрузка...';
try{
  const r = await fetch('/doors?token=' + encodeURIComponent(t));
  if(!r.ok){ s.textContent = 'Ошибка: ' + r.status; return; }
  const data = await r.json();
  s.textContent = `Всего дверей: ${data.total_doors} / ${data.max_doors}`;
  const c = document.getElementById('doors'); c.innerHTML = '';
  if(data.doors.length === 0){
    c.innerHTML = '<div class="empty">Дверей пока нет. Создайте первую сверху.</div>';
    return;
  }
  for(const d of data.doors){
    const fullUrl = window.location.origin + d.url;
    const div = document.createElement('div');
    div.className = 'door';
    const badge = d.session_active
      ? `<span class="badge">${d.connections}/${d.max_slots} активна</span>`
      : `<span class="badge idle">${d.connections}/${d.max_slots} idle</span>`;
    div.innerHTML = `
      <h3>🚪 ${escapeHtml(d.title)}</h3>
      ${d.topic ? `<div class="meta">📌 ${escapeHtml(d.topic)}</div>` : ''}
      <div class="url" onclick="navigator.clipboard.writeText('${fullUrl}')" title="Кликни — скопирует">${fullUrl}</div>
      <div class="row">
        ${badge}
        <button class="danger" onclick="deleteDoor(${d.door_id}, '${escapeHtml(d.title)}')">✕ Удалить</button>
      </div>`;
    c.appendChild(div);
  }
}catch(e){ s.textContent = 'Ошибка сети: ' + e.message; }
}

async function createDoor(){
const t = getToken(); if(!t) return;
const title = document.getElementById('newTitle').value.trim();
const topic = document.getElementById('newTopic').value.trim();
if(!title){ alert('Введите название'); return; }
const r = await fetch('/doors?token=' + encodeURIComponent(t), {
  method:'POST', headers:{'Content-Type':'application/json'},
  body: JSON.stringify({title, topic})
});
if(!r.ok){ alert('Ошибка: ' + r.status + ' ' + (await r.text())); return; }
document.getElementById('newTitle').value = '';
document.getElementById('newTopic').value = '';
refresh();
}

async function deleteDoor(id, title){
if(!confirm(`Удалить дверь «${title}»? Все участники будут кикнуты, контейнеры и архив этой двери удалятся безвозвратно.`)) return;
const t = getToken(); if(!t) return;
const r = await fetch('/doors/' + id + '?token=' + encodeURIComponent(t), {method:'DELETE'});
if(!r.ok){ alert('Ошибка: ' + r.status); return; }
refresh();
}

function escapeHtml(s){
return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
</script>
</body></html>"""


@app.get("/door/{door_id}", response_class=HTMLResponse)
async def door_ui(door_id: int):
  return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Door {door_id} — Debate-II</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:900px;
margin:2em auto;padding:0 1em;color:#222}}
code{{background:#f3f3f3;padding:2px 6px;border-radius:4px}}
.back{{display:inline-block;margin-bottom:1em;color:#0a7;text-decoration:none}}
pre{{background:#f3f3f3;padding:1em;border-radius:6px;overflow:auto;font-size:13px}}
</style></head>
<body>
<a href="/" class="back">← Все двери</a>
<h1>🚪 Door {door_id}</h1>
<p>API этой двери:</p>
<pre>POST /door/{door_id}/session/start ?token=...
POST /door/{door_id}/join            ?token=...
POST /door/{door_id}/say              ?token=...
POST /door/{door_id}/propose          ?token=...
POST /door/{door_id}/agree            ?token=...
POST /door/{door_id}/disagree         ?token=...
POST /door/{door_id}/heartbeat        ?token=...
POST /door/{door_id}/leave            ?token=...
GET  /door/{door_id}/state            ?token=...
POST /door/{door_id}/round            ?token=...
POST /door/{door_id}/container/write  ?token=...
GET  /door/{door_id}/container/{{author}}  ?token=...
GET  /door/{door_id}/archive          ?token=...</pre>
<p>Дай эту ссылку дебатёру: <code>{{server}}/door/{door_id}</code></p>
</body></html>"""
