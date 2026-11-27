"""HTTP surface for Master Room — inbound webhooks from the coder-bot.

The coder-bot calls back into Lilush as it makes progress on a task.
This module owns those routes; they're all gated by the same token
that's printed in the addon UI («🔑 Настройки → 🔗 Сгенерировать ссылку»).

URLs are mounted under ``<PUBLIC_URL>/masterroom/<token>/...``:

* ``GET  /masterroom/<token>/``        — splash page (status + project list)
* ``GET  /masterroom/<token>/ping``    — liveness JSON
* ``GET  /masterroom/<token>/info``    — version + active-server snapshot
* ``POST /masterroom/<token>/event``   — live update from coder
* ``POST /masterroom/<token>/done``    — final task callback
* ``POST /masterroom/<token>/github``  — relayed GitHub webhook

Bad / disabled token → ``403 {"error": "forbidden"}`` (neutral; we
never confirm whether a URL existed previously).
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from ...coder_server import CoderService
from . import state as mr_state

_CODER_SERVICE = CoderService(Path("data") / "coder_server")

logger = logging.getLogger(__name__)

# --- back-compat shims used by main.py / wizard.py --------------------

is_enabled = mr_state.is_enabled
get_token = mr_state.get_token
get_link = mr_state.get_link


def reserve_defaults() -> dict[str, Any]:  # kept for main.py callsite
    return mr_state.reserve_defaults()


# --- token gate -------------------------------------------------------

def _check(token: str) -> Optional[web.Response]:
    stored = mr_state.get_token()
    if not stored or not secrets.compare_digest(stored, token):
        return web.json_response({"error": "forbidden"}, status=403)
    if not mr_state.is_enabled():
        return web.json_response({"error": "forbidden"}, status=403)
    return None


def _project_for_task(task_id: str) -> Optional[dict[str, Any]]:
    for p in mr_state.get_projects():
        if p.get("task_id") == task_id:
            return p
    return None


# --- splash -----------------------------------------------------------

_SPLASH_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="robots" content="noindex,nofollow" />
<title>🛠 Master Room — Lilush</title>
<style>
  :root {{ color-scheme: dark light; }}
  html, body {{ margin: 0; padding: 0; min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    background: #0b0d12; color: #e6e9ef; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 56px 24px; }}
  h1 {{ font-size: 28px; margin: 0 0 12px; }}
  p.lede {{ font-size: 16px; color: #a8b0bf; margin: 0 0 24px; }}
  .card {{ background: #131720; border: 1px solid #232a39;
    border-radius: 12px; padding: 18px 20px; margin: 12px 0; }}
  code {{ background: #1c2230; padding: 2px 6px; border-radius: 6px;
    font-size: 13px; }}
  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
    background: #1f3a2a; color: #6ee7a8; font-size: 12px; font-weight: 600; }}
  .row {{ display: flex; justify-content: space-between;
    align-items: center; gap: 12px; padding: 6px 0;
    border-bottom: 1px dashed #232a39; }}
  .row:last-child {{ border-bottom: none; }}
  .name {{ flex: 1; }}
  .status {{ font-size: 12px; color: #7e879a; }}
  footer {{ margin-top: 40px; color: #5d6577; font-size: 12px; }}
  a {{ color: #8ab4ff; }}
</style>
</head>
<body>
<main>
  <span class="pill">🛠 MASTER ROOM</span>
  <h1>Devin-style консоль</h1>
  <p class="lede">
    Здесь приземляются webhook'и от кодер-бота (см.
    <code>TZ_MASTER_ROOM_v0.3.md</code> §4.4). Сама страница — просто
    индикатор, что surface работает; вся реальная работа идёт через
    Telegram-бота.
  </p>
  <div class="card">
    <strong>Активный кодер:</strong> {server_summary}<br />
    <strong>Активный таргет:</strong> {target_summary}<br />
    <strong>GitHub-аккаунт:</strong> {github_summary}<br />
    <strong>Поднят:</strong> {rotated_human}
  </div>
  <div class="card">
    <strong>📂 Последние проекты ({n_projects}):</strong>
    {projects_block}
  </div>
  <footer>Lilush · <code>addons.masterroom</code> · token <code>{token_short}…</code></footer>
</main>
</body>
</html>
"""


_DASHBOARD_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><meta name="description" content="Devin-style autonomous coder operations dashboard"><title>Master Room · Operations</title>
<style>
:root{--bg:#0d0f12;--panel:#15181d;--line:#2a2f37;--text:#f1f3f5;--muted:#929aa6;--accent:#d6ad55;--ok:#71c89c;--danger:#e26d6d;color-scheme:dark}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,sans-serif}button{font:inherit}
.shell{min-height:100vh;display:flex;flex-direction:column}.topbar{min-height:64px;padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{display:flex;align-items:center;gap:12px}.mark{width:28px;height:28px;border:1px solid var(--accent);border-radius:50%;display:grid;place-items:center;color:var(--accent);font:700 11px ui-monospace,monospace}.eyebrow{color:var(--muted);font:11px ui-monospace,monospace;letter-spacing:.12em;text-transform:uppercase}.brand h1{font-size:16px;margin:0}.health{display:flex;align-items:center;gap:8px;color:var(--ok);font:12px ui-monospace,monospace}.dot{width:7px;height:7px;background:var(--ok);border-radius:50%}
.layout{display:grid;grid-template-columns:260px minmax(0,1fr) 360px;min-height:calc(100vh - 64px)}.rail,.inspector{padding:20px;border-right:1px solid var(--line)}.inspector{border-right:0;border-left:1px solid var(--line)}.section-title{display:flex;justify-content:space-between;color:var(--muted);font:11px ui-monospace,monospace;letter-spacing:.1em;text-transform:uppercase;margin-bottom:12px}.task-list{display:flex;flex-direction:column;gap:8px}.task{width:100%;border:1px solid transparent;background:transparent;color:var(--text);padding:12px;text-align:left;border-radius:8px;cursor:pointer}.task:hover,.task.active{background:var(--panel);border-color:var(--line)}.task strong,.task small{display:block}.task small{color:var(--muted);margin-top:3px}.status{color:var(--accent);font:11px ui-monospace,monospace;text-transform:uppercase}
.main{min-width:0;padding:24px}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:22px}.hero h2{font-size:26px;line-height:1.2;margin:4px 0 8px;max-width:720px}.hero p{color:var(--muted);margin:0}.controls{display:flex;flex-wrap:wrap;gap:8px}.control{border:1px solid var(--line);background:var(--panel);color:var(--text);padding:8px 12px;border-radius:7px;cursor:pointer}.control:hover{border-color:var(--accent)}.control.danger{color:var(--danger)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}.metric{border:1px solid var(--line);background:var(--panel);padding:14px;border-radius:9px}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{font:20px ui-monospace,monospace}.feed{border-top:1px solid var(--line)}.event{display:grid;grid-template-columns:90px 12px 1fr;gap:12px;padding:16px 0;border-bottom:1px solid var(--line)}.event time{color:var(--muted);font:11px ui-monospace,monospace}.event .node{width:9px;height:9px;border:2px solid var(--accent);border-radius:50%;margin-top:5px}.event strong{display:block}.event p{color:var(--muted);margin:3px 0 0}.empty{color:var(--muted);padding:24px 0}
.panel{border:1px solid var(--line);background:var(--panel);padding:14px;border-radius:9px;margin-bottom:12px}.panel dl{margin:0;display:grid;grid-template-columns:1fr auto;gap:8px}.panel dt{color:var(--muted)}.panel dd{margin:0;font:12px ui-monospace,monospace}.evidence{font:12px ui-monospace,monospace;color:var(--ok);overflow-wrap:anywhere}.notice{border-left:2px solid var(--accent);padding:10px 12px;background:var(--panel);color:var(--muted)}
@media(max-width:1050px){.layout{grid-template-columns:220px minmax(0,1fr)}.inspector{grid-column:1/-1;border-left:0;border-top:1px solid var(--line);display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.inspector .section-title{grid-column:1/-1}.panel{margin:0}}
@media(max-width:720px){.topbar{align-items:flex-start}.layout{display:block}.rail,.inspector{border:0;border-bottom:1px solid var(--line)}.task-list{flex-direction:row;overflow:auto}.task{min-width:210px}.main{padding:18px}.hero{display:block}.controls{margin-top:14px}.metrics{grid-template-columns:repeat(2,1fr)}.inspector{display:block}.panel{margin-bottom:10px}.event{grid-template-columns:64px 10px 1fr}}
</style></head><body><div class="shell"><header class="topbar"><div class="brand"><div class="mark">MR</div><div><div class="eyebrow">Coder Operations</div><h1>Master Room</h1></div></div><div class="health"><span class="dot"></span><span id="health">runtime online</span></div></header>
<div class="layout"><aside class="rail"><div class="section-title"><span>Задачи</span><span id="taskCount">0</span></div><div class="task-list" id="tasks"><div class="empty">Загрузка…</div></div></aside>
<main class="main"><section class="hero"><div><div class="eyebrow" id="taskId">Выберите задачу</div><h2 id="taskTitle">Инструментальная среда Кодера</h2><p id="taskRequest">Безопасные workspace, реальные tool calls и проверяемые доказательства.</p></div><div class="controls"><button class="control" data-action="pause">Пауза</button><button class="control" data-action="resume">Продолжить</button><button class="control" data-action="retry">Повторить</button><button class="control danger" data-action="cancel">Отменить</button></div></section>
<section class="metrics"><div class="metric"><span>Состояние</span><strong id="state">—</strong></div><div class="metric"><span>Фаза</span><strong id="phase">—</strong></div><div class="metric"><span>События</span><strong id="eventCount">0</strong></div><div class="metric"><span>Evidence</span><strong id="evidenceCount">0</strong></div></section><div class="section-title"><span>Безопасная live feed</span><span>SSE-ready</span></div><section class="feed" id="feed"><div class="empty">Выберите задачу слева</div></section></main>
<aside class="inspector"><div class="section-title"><span>Инспектор</span><span>live</span></div><section class="panel"><dl><dt>Coder server</dt><dd>{server_summary}</dd><dt>Target</dt><dd>{target_summary}</dd><dt>GitHub</dt><dd>{github_summary}</dd><dt>Tools</dt><dd id="toolCount">0</dd></dl></section><section class="panel"><div class="eyebrow">Evidence gate</div><p class="evidence" id="evidence">Нет выбранной задачи</p></section><section class="panel"><div class="eyebrow">Durable jobs</div><p class="evidence" id="jobs">Нет выбранной задачи</p></section><section class="panel"><div class="eyebrow">Approvals</div><p class="evidence" id="approvals">Нет выбранной задачи</p></section><section class="panel"><div class="eyebrow">Artifacts</div><p class="evidence" id="artifacts">Нет выбранной задачи</p></section><section class="notice">Показываются только краткие намерения, очищенные параметры и фактические результаты. Секреты и скрытые рассуждения не выводятся.</section></aside></div></div>
<script>
const base=location.pathname.endsWith('/')?location.pathname.slice(0,-1):location.pathname;let selected=null;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const r=await fetch(base+'/dashboard-data');if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();renderTasks(d.tasks);toolCount.textContent=d.capabilities.length;if(selected){const t=d.tasks.find(x=>x.id===selected);if(t)renderTask(t,d.events[selected]||[],d.evidence[selected]||[],d.jobs[selected]||[],d.approvals[selected]||[],d.artifacts[selected]||[],d.gates[selected]||{})}else if(d.tasks[0]){selected=d.tasks[0].id;renderTask(d.tasks[0],d.events[selected]||[],d.evidence[selected]||[],d.jobs[selected]||[],d.approvals[selected]||[],d.artifacts[selected]||[],d.gates[selected]||{})}document.getElementById('taskCount').textContent=d.tasks.length}
function renderTasks(tasks){document.getElementById('tasks').innerHTML=tasks.length?tasks.map(t=>`<button class="task ${t.id===selected?'active':''}" data-id="${esc(t.id)}"><span class="status">${esc(t.state)}</span><strong>${esc(t.title)}</strong><small>${esc(t.phase)} · ${new Date(t.updated_at*1000).toLocaleTimeString()}</small></button>`).join(''):'<div class="empty">Задач пока нет</div>';document.querySelectorAll('[data-id]').forEach(b=>b.onclick=()=>{selected=b.dataset.id;load()})}
function renderTask(t,events,evidence,jobItems,approvalItems,artifactItems,gate){taskId.textContent=t.id;taskTitle.textContent=t.title;taskRequest.textContent=t.request;state.textContent=t.state;phase.textContent=t.phase;eventCount.textContent=events.length;evidenceCount.textContent=evidence.length;document.getElementById('evidence').textContent=(gate.ok?'PASS':'BLOCKED')+' · '+(gate.missing?.length?'missing: '+gate.missing.join(', '):'required evidence is fresh')+'\n'+(evidence.length?evidence.map(e=>e.claim_type+' · '+e.id).join('\n'):'Доказательств пока нет');jobs.textContent=jobItems.length?jobItems.map(j=>j.kind+' · '+j.state+' · attempts '+j.attempts+'/'+j.max_attempts).join('\n'):'Заданий очереди нет';approvals.textContent=approvalItems.length?approvalItems.map(a=>a.kind+' · '+a.status+' · '+a.id).join('\n'):'Запросов подтверждения нет';artifacts.textContent=artifactItems.length?artifactItems.map(a=>a.kind+' · '+a.id+' · '+a.size+' bytes').join('\n'):'Артефактов пока нет';feed.innerHTML=events.length?events.slice().reverse().map(e=>`<article class="event"><time>${new Date(e.created_at*1000).toLocaleTimeString()}</time><span class="node"></span><div><strong>${esc(e.event_type)}</strong><p>${esc(e.summary)}</p></div></article>`).join(''):'<div class="empty">Событий пока нет</div>'}
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{if(!selected)return;b.disabled=true;try{await fetch(base+'/tasks/'+encodeURIComponent(selected)+'/'+b.dataset.action,{method:'POST'});await load()}finally{b.disabled=false}});load().catch(e=>health.textContent='runtime error: '+e.message);setInterval(load,3000);
</script></body></html>"""


def _human_ts(epoch: int) -> str:
    if not epoch:
        return "ещё не поднимали"
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:  # noqa: BLE001
        return str(epoch)


def _render_splash(token: str) -> str:
    srv = mr_state.get_active_server()
    server_summary = html.escape(
        f"{srv['name']} ({srv['url']})" if srv else "не задан"
    )
    tgt = mr_state.get_active_target()
    target_summary = html.escape(
        f"{tgt['name']}" + (f" — {tgt['url_template']}" if tgt and tgt.get('url_template') else "")
        if tgt else "не задан"
    )
    gh = mr_state.get_active_github_account()
    github_summary = html.escape(
        f"{gh['name']} ({gh['owner']})" if gh else "не задан"
    )
    projects = mr_state.get_projects()[:5]
    if projects:
        rows = []
        for p in projects:
            rows.append(
                f'<div class="row"><span class="name">{html.escape(str(p.get("title", "(без названия)")))}</span>'
                f'<span class="status">{html.escape(str(p.get("status", "")))}</span></div>'
            )
        projects_block = "\n".join(rows)
    else:
        projects_block = '<div class="status">пусто — заведи задачу в боте</div>'
    return (_DASHBOARD_HTML
            .replace("{server_summary}", server_summary)
            .replace("{target_summary}", target_summary)
            .replace("{github_summary}", github_summary))


# --- HTTP handlers ----------------------------------------------------

async def _root(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    if (resp := _check(token)) is not None:
        return resp
    return web.Response(text=_render_splash(token), content_type="text/html")


async def _ping(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return web.json_response({
        "ok": True,
        "rotated_at": mr_state.get_rotated_at(),
        "version": "masterroom/1",
        "active_server": bool(mr_state.get_active_server()),
        "active_github": bool(mr_state.get_active_github_account()),
        "n_projects": len(mr_state.get_projects()),
    })


async def _info(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    srv = mr_state.get_active_server()
    tgt = mr_state.get_active_target()
    gh = mr_state.get_active_github_account()
    return web.json_response({
        "rotated_at": mr_state.get_rotated_at(),
        "active_server": (
            {"id": srv["id"], "name": srv["name"], "url": srv["url"]}
            if srv else None
        ),
        "active_target": (
            {"id": tgt["id"], "name": tgt["name"], "url_template": tgt.get("url_template", "")}
            if tgt else None
        ),
        "active_github": (
            {"id": gh["id"], "name": gh["name"], "owner": gh["owner"]}
            if gh else None
        ),
        "defaults": mr_state.get_defaults(),
        "n_projects": len(mr_state.get_projects()),
        "n_servers": len(mr_state.get_servers()),
        "n_github_accounts": len(mr_state.get_github_accounts()),
    })


_MAX_BODY = 200_000


async def _event(request: web.Request) -> web.Response:
    """Live progress callback from coder-bot.

    Body: ``{task_id, status, log_line?, progress?, artifact_url?}``.
    """
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    if request.content_length and request.content_length > _MAX_BODY:
        return web.json_response({"error": "payload too large"}, status=413)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    task_id = str(body.get("task_id", "") or "")
    status = str(body.get("status", "") or "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)
    project = _project_for_task(task_id)
    if project is None:
        # Stash the orphan event but don't fail — gives operators a
        # way to recover if the project was deleted between poll
        # cycles.
        logger.warning("masterroom: orphan event for task_id=%s", task_id)
        return web.json_response({"ok": True, "stored": False, "reason": "no project"})
    updates: dict[str, Any] = {}
    if status:
        updates["status"] = status
    if "progress" in body:
        updates["progress"] = body["progress"]
    if "artifact_url" in body:
        updates["artifact_url"] = body["artifact_url"]
    if "log_line" in body:
        log = list(project.get("log", []))
        log.append({"ts": int(time.time()), "line": str(body["log_line"])[:1000]})
        # cap log size to last 500 lines
        if len(log) > 500:
            log = log[-500:]
        updates["log"] = log
    if updates:
        mr_state.update_project(project["id"], **updates)
    return web.json_response({"ok": True, "stored": True})


async def _done(request: web.Request) -> web.Response:
    """Terminal callback from coder-bot.

    Body: ``{task_id, status, deploy_url?, preview_url?, github_url?,
    manifest?, error?}``.
    """
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    task_id = str(body.get("task_id", "") or "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)
    project = _project_for_task(task_id)
    if project is None:
        return web.json_response({"ok": True, "stored": False, "reason": "no project"})
    status = str(body.get("status", "done") or "done")
    if status not in mr_state.PROJECT_STATUSES:
        status = "done"
    updates: dict[str, Any] = {
        "status": status,
        "finished_at": int(time.time()),
    }
    for k in ("deploy_url", "preview_url", "github_url", "manifest", "error"):
        if k in body:
            updates[k] = body[k]
    mr_state.update_project(project["id"], **updates)
    return web.json_response({"ok": True, "stored": True})


async def _github(request: web.Request) -> web.Response:
    """Relayed GitHub Actions webhook (deploy success/fail).

    Body shape depends on the workflow; we only care about
    ``{repo, status, conclusion, html_url, task_id?}``.
    """
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    # If the workflow embedded our task_id, target that project; else
    # try matching by repo URL substring.
    project = None
    task_id = str(body.get("task_id", "") or "")
    if task_id:
        project = _project_for_task(task_id)
    if project is None:
        repo = str(body.get("repo", "") or "")
        if repo:
            for p in mr_state.get_projects():
                if repo in (p.get("github_repo", "") or ""):
                    project = p
                    break
    if project is None:
        return web.json_response({"ok": True, "stored": False, "reason": "no project"})
    updates = {
        "deploy_status": body.get("conclusion") or body.get("status"),
        "deploy_check_url": body.get("html_url", ""),
    }
    if body.get("conclusion") == "success":
        updates["status"] = "done"
    elif body.get("conclusion") in {"failure", "timed_out", "cancelled"}:
        updates["status"] = "failed"
    mr_state.update_project(project["id"], **updates)
    return web.json_response({"ok": True, "stored": True})


async def _dashboard_data(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    tasks = _CODER_SERVICE.store.list_tasks()
    payload = {
        "tasks": tasks,
        "events": {},
        "evidence": {},
        "artifacts": {},
        "approvals": {},
        "jobs": {},
        "gates": {},
        "capabilities": _CODER_SERVICE.registry.describe(),
    }
    for task in tasks:
        task_id = task["id"]
        payload["events"][task_id] = _CODER_SERVICE.store.events(task_id)
        payload["evidence"][task_id] = _CODER_SERVICE.store.evidence(task_id)
        payload["artifacts"][task_id] = _CODER_SERVICE.store.artifacts(task_id)
        payload["approvals"][task_id] = _CODER_SERVICE.store.approvals(task_id)
        payload["jobs"][task_id] = _CODER_SERVICE.jobs.list(task_id)
        payload["gates"][task_id] = _CODER_SERVICE.evidence_gate(task_id, {"tests.passed", "review.approved"})
    return web.json_response(payload)


async def _dashboard_control(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    try:
        task = _CODER_SERVICE.control(request.match_info["task_id"], request.match_info["action"])
    except KeyError:
        return web.json_response({"error": "task not found"}, status=404)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response(task)


def register_http_endpoints(app: web.Application) -> None:
    """Mount Master Room routes on an aiohttp app.

    Order: specific routes before the catch-all root so ``/ping``
    isn't shadowed by the splash handler.
    """
    app.router.add_get("/masterroom/{token}/ping", _ping)
    app.router.add_get("/masterroom/{token}/info", _info)
    app.router.add_get("/masterroom/{token}/dashboard-data", _dashboard_data)
    app.router.add_post("/masterroom/{token}/tasks/{task_id}/{action:pause|resume|cancel|retry}", _dashboard_control)
    app.router.add_post("/masterroom/{token}/event", _event)
    app.router.add_post("/masterroom/{token}/done", _done)
    app.router.add_post("/masterroom/{token}/github", _github)
    app.router.add_get("/masterroom/{token}/", _root)
    app.router.add_get("/masterroom/{token}", _root)
    logger.info("masterroom endpoints registered on aiohttp app")
