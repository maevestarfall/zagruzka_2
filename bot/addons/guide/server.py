# «🧭 Гид» — HTTP endpoint serving a self-contained step-by-step wizard.
# Co-authored with CoCo
"""Serves the guide at ``/guide/<token>/`` — a single self-contained HTML page
(no CDN): numbered steps with a progress bar and «Назад/Дальше» buttons. Pure
informational walkthrough of the bot's real setup; nothing is executed
server-side.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from aiohttp import web

from . import store

logger = logging.getLogger(__name__)


def _check(token: str) -> Optional[web.Response]:
    stored = store.get_token()
    if not stored or not secrets.compare_digest(stored, token):
        return web.json_response({"error": "forbidden"}, status=403)
    if not store.is_enabled():
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def _ping(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return web.json_response({"ok": True, "version": "guide/1"})


async def _root(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return web.Response(text=_PAGE_HTML, content_type="text/html")


def register_http_endpoints(app: web.Application) -> None:
    app.router.add_get("/guide/{token}/ping", _ping)
    app.router.add_get("/guide/{token}/", _root)
    app.router.add_get("/guide/{token}", _root)
    logger.info("guide endpoints registered on aiohttp app")


_PAGE_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>🧭 Гид — Lilush Bot</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:#0f1115; color:#e6e6e6;
         display:flex; flex-direction:column; min-height:100vh; }
  header { padding:14px 18px; background:#161a22; border-bottom:1px solid #262c38; }
  header h1 { margin:0; font-size:18px; }
  #bar { height:8px; background:#20262f; }
  #fill { height:8px; width:0%; background:linear-gradient(90deg,#2b6cb0,#63b3ed); transition:width .3s; }
  main { flex:1; display:flex; align-items:center; justify-content:center; padding:20px; }
  .card { max-width:640px; width:100%; background:#141821; border:1px solid #262c38;
          border-radius:14px; padding:24px; }
  .step-no { color:#63b3ed; font-size:13px; letter-spacing:.05em; }
  .card h2 { margin:6px 0 12px; font-size:22px; }
  .card p { color:#cbd5e1; line-height:1.6; font-size:15px; white-space:pre-line; }
  .nav { display:flex; justify-content:space-between; margin-top:22px; gap:10px; }
  button { background:#2b6cb0; color:#fff; border:0; border-radius:8px; padding:12px 18px;
           font-size:15px; cursor:pointer; }
  button.ghost { background:#2b3240; }
  button:disabled { opacity:.4; cursor:default; }
  #done { display:none; text-align:center; color:#9ae6b4; font-size:16px; margin-top:14px; }
</style>
</head>
<body>
<header><h1>🧭 Гид по настройке бота</h1></header>
<div id="bar"><div id="fill"></div></div>
<main>
  <div class="card">
    <div class="step-no" id="stepno"></div>
    <h2 id="title"></h2>
    <p id="body"></p>
    <div class="nav">
      <button class="ghost" id="prev">← Назад</button>
      <button id="next">Дальше →</button>
    </div>
    <div id="done">✅ Готово! Ты прошёл все шаги.</div>
  </div>
</main>
<script>
const STEPS = [
  {t:"Подключить GitHub",
   b:"В боте: «🐙 GitHub проекты» → «🔑 Подключить токен» (или «🔐 Вход в GitHub»).\nВставь свой GitHub-токен — бот сможет создавать и обновлять репозитории."},
  {t:"Залить проект в репозиторий",
   b:"Скинь zip проекта прямо в чат.\nПоявятся кнопки: «⚡ Создать репо авто» или «✏️ Редактировать все репозитории».\nВыбери нужное — бот зальёт код в GitHub."},
  {t:"Задеплоить на Render",
   b:"На Render подключи свой GitHub-репозиторий и запусти деплой.\nВажно: делай «Clear build cache & deploy», чтобы не подтянулся старый кэш."},
  {t:"Включить панель управления",
   b:"В боте: «🧠 Перенастроить мозг» → «🌀 Fable5» → сгенерировать ссылку.\nПотом открой «🖥 VISION» — веб-панель подтянется сама."},
  {t:"Настроить мозг",
   b:"«🧠 Перенастроить мозг» → «⚙️ Другое» — вставь API-ключ и модель (OpenRouter и т.п.).\nЛибо «🎼 Оркестр» — задать модели по ролям."},
  {t:"Проверить",
   b:"Напиши боту «привет».\nЕсли отвечает — всё настроено. Медленный ответ на бесплатной модели — норма."},
];
let i = 0;
const $ = id => document.getElementById(id);
function render(){
  const s = STEPS[i];
  $('stepno').textContent = `Шаг ${i+1} из ${STEPS.length}`;
  $('title').textContent = s.t;
  $('body').textContent = s.b;
  $('fill').style.width = ((i)/(STEPS.length-1)*100) + '%';
  $('prev').disabled = (i===0);
  $('next').textContent = (i===STEPS.length-1) ? 'Завершить ✓' : 'Дальше →';
  $('done').style.display = 'none';
}
$('prev').onclick = () => { if(i>0){i--; render();} };
$('next').onclick = () => {
  if(i < STEPS.length-1){ i++; render(); }
  else { $('fill').style.width='100%'; $('done').style.display='block'; }
};
render();
</script>
</body>
</html>
"""
