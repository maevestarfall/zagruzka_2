"""HTTP endpoints for the «👁 VISION» web control panel.

Exposes a token-gated single-page dashboard at ``/vision/<token>/``
that lets the owner control the bot through a web browser. Unlike
«🧪 Тест» (preview slot for an external coder-bot), VISION is the
bot's own management panel.

The dashboard talks to Fable5 REST endpoints (``/fable5/<token>/...``)
for data — state, logs, exec, send. VISION only serves the frontend;
all backend logic reuses the existing Fable5 surface.

Available endpoints:

* ``GET  /vision/<token>/``       — the main dashboard SPA.
* ``GET  /vision/<token>/ping``   — liveness JSON.

Bad / disabled token → ``403 {"error": "forbidden"}``.
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from typing import Optional

from aiohttp import web

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "vision"
DEFAULT_SESSION_TTL = 24 * 60 * 60
MIN_SESSION_TTL = 5 * 60
MAX_SESSION_TTL = 7 * 24 * 60 * 60


# --- token / state -----------------------------------------------------

def is_enabled() -> bool:
    return bool(addon_state.get(ADDON, "enabled", False))


def get_token() -> str:
    return str(addon_state.get(ADDON, "token", "") or "")


def get_rotated_at() -> int:
    try:
        return int(addon_state.get(ADDON, "rotated_at", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def get_expires_at() -> int:
    try:
        return int(addon_state.get(ADDON, "expires_at", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def is_expired(now: int | None = None) -> bool:
    expires_at = get_expires_at()
    return bool(expires_at and expires_at <= int(now if now is not None else time.time()))


def token_fingerprint() -> str:
    """Return a non-secret identifier suitable for status screens and logs."""
    token = get_token()
    if not token:
        return "—"
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def get_status(now: int | None = None) -> dict[str, object]:
    """Return safe session metadata. Never includes the token or private URL."""
    current = int(now if now is not None else time.time())
    expires_at = get_expires_at()
    expired = bool(expires_at and expires_at <= current)
    enabled = is_enabled()
    return {
        "enabled": enabled,
        "active": bool(enabled and get_token() and not expired),
        "expired": expired,
        "rotated_at": get_rotated_at(),
        "expires_at": expires_at,
        "seconds_left": max(0, expires_at - current) if expires_at else 0,
        "fingerprint": token_fingerprint(),
    }


def _resolve_public_base() -> str:
    try:
        from ...config import resolve_public_url
        base = resolve_public_url()
    except Exception:  # noqa: BLE001
        base = ""
    if base:
        return base
    # Local dev fallback: no PUBLIC_URL / platform host is resolvable.
    # A *relative* link (``/vision/<token>/``) is rejected by Telegram
    # (BUTTON_URL_INVALID) when used in url-buttons, which silently
    # kills the whole VISION screen.  Always return an absolute URL.
    try:
        from ...config import PORT as _port
    except Exception:  # noqa: BLE001
        _port = 8080
    logger.warning(
        "VISION: PUBLIC_URL is not configured — falling back to "
        "http://127.0.0.1:%s. The link will only work on this machine; "
        "set PUBLIC_URL for production.", _port,
    )
    return f"http://127.0.0.1:{_port}"


def get_link(base_url: str | None = None) -> str:
    token = get_token()
    if not token:
        return ""
    base = (base_url if base_url is not None else _resolve_public_base()).rstrip("/")
    return f"{base}/vision/{token}/" if base else f"/vision/{token}/"


def generate_link(
    base_url: str | None = None,
    *,
    ttl_seconds: int = DEFAULT_SESSION_TTL,
) -> tuple[str, str]:
    """Rotate the access token and create a bounded owner session."""
    ttl = max(MIN_SESSION_TTL, min(int(ttl_seconds), MAX_SESSION_TTL))
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    addon_state.set_(ADDON, "token", token)
    addon_state.set_(ADDON, "enabled", True)
    addon_state.set_(ADDON, "rotated_at", now)
    addon_state.set_(ADDON, "expires_at", now + ttl)
    return token, get_link(base_url)


def disable_link() -> bool:
    """Revoke the current session and erase its bearer credential."""
    was = bool(is_enabled() or get_token())
    addon_state.set_(ADDON, "enabled", False)
    addon_state.set_(ADDON, "token", "")
    addon_state.set_(ADDON, "expires_at", int(time.time()))
    return was


# --- HTTP handlers ----------------------------------------------------

def _check(token: str) -> Optional[web.Response]:
    stored = get_token()
    # Keep the external response intentionally generic to avoid revealing
    # whether a credential is valid, disabled, or merely expired.
    if not stored or not secrets.compare_digest(stored, token):
        return web.json_response({"error": "forbidden"}, status=403)
    if not is_enabled() or is_expired():
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def _ping(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    status = get_status()
    return web.json_response({
        "ok": True,
        "rotated_at": status["rotated_at"],
        "expires_at": status["expires_at"],
        "seconds_left": status["seconds_left"],
        "version": "vision/2",
    })


def _get_fable5_api_base() -> str:
    """Return the Fable5 API base URL for the dashboard to call.

    VISION is just the frontend; its backend is the Fable5 REST API. To make
    VISION «just work» without the owner manually enabling Fable5, we
    AUTO-PROVISION it here: if Fable5 is off or tokenless, enable + mint a
    token on the fly. Called on every VISION page render, so opening VISION is
    enough — no separate Fable5 toggle needed.
    """
    from ..fable5 import server as f5
    try:
        if not f5.is_enabled() or not f5.get_token():
            f5.generate_link()  # mint token + flip enabled=True
    except Exception:  # noqa: BLE001 — never break the page over this
        logger.exception("VISION: failed to auto-enable Fable5 backend")
    base = _resolve_public_base().rstrip("/")
    token = ""
    try:
        token = f5.get_token()
    except Exception:  # noqa: BLE001
        token = ""
    if not token:
        return ""
    # Same-origin fallback: VISION and Fable5 live on the SAME aiohttp
    # app, so when no public base URL is configured (local dev, or a
    # host where RENDER_EXTERNAL_URL/PUBLIC_URL isn't set yet) a
    # relative path always works from the browser. Never show «No API»
    # when the backend is literally on the same origin.
    return f"{base}/fable5/{token}" if base else f"/fable5/{token}"


def preflight(*, recover: bool = True) -> dict[str, object]:
    """Validate VISION dependencies and optionally recover Fable5.

    The result is deliberately safe for Telegram diagnostics: it contains no
    bearer tokens or token-bearing URLs.
    """
    result: dict[str, object] = {
        "ok": False,
        "vision": get_status(),
        "backend": "unavailable",
        "recovered": False,
        "detail": "Fable5 backend is unavailable",
    }
    try:
        from ..fable5 import server as f5
        before = bool(f5.is_enabled() and f5.get_token())
        if recover and not before:
            f5.generate_link("")
        ready = bool(f5.is_enabled() and f5.get_token())
        result.update({
            "ok": ready,
            "backend": "ready" if ready else "unavailable",
            "recovered": bool(ready and not before),
            "detail": "Backend ready" if ready else "Fable5 could not be started",
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("VISION preflight failed: %s", type(exc).__name__)
        result["detail"] = f"Backend check failed ({type(exc).__name__})"
    return result


async def _root(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    if (resp := _check(token)) is not None:
        return resp
    fable5_api = _get_fable5_api_base()
    return web.Response(
        text=_render_dashboard(token, fable5_api),
        content_type="text/html",
    )


def _human_ts(epoch: int) -> str:
    if not epoch:
        return "—"
    try:
        import datetime as _dt
        return (
            _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC")
        )
    except Exception:  # noqa: BLE001
        return str(epoch)


def _render_dashboard(token: str, fable5_api: str) -> str:
    # Резолвим путь к jobs.db на сервере: subprocess из /exec может не
    # иметь зависимостей проекта (dotenv и т.п.), поэтому фронту нужен
    # готовый абсолютный путь, а не import bot.config в подпроцессе.
    jobs_db = ""
    try:
        from ...config import JOBS_DB_PATH
        jobs_db = str(JOBS_DB_PATH)
    except Exception:  # noqa: BLE001
        jobs_db = ""
    return (
        _DASHBOARD_HTML.replace("{{FABLE5_API}}", html.escape(fable5_api))
        .replace("{{TOKEN_SHORT}}", html.escape(token_fingerprint()))
        .replace("{{EXPIRES_AT}}", html.escape(_human_ts(get_expires_at())))
        .replace("{{JOBS_DB}}", html.escape(jobs_db))
    )


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="robots" content="noindex,nofollow" />
<title>👁 VISION — Lilush Bot Panel</title>
<style>
  :root {
    --bg: #0b0d12;
    --bg2: #131720;
    --border: #232a39;
    --text: #e6e9ef;
    --muted: #8892a4;
    --accent: #6ee7a8;
    --accent-bg: #1f3a2a;
    --danger: #f87171;
    --blue: #8ab4ff;
    --radius: 12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  .top-bar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
  }
  .top-bar h1 { font-size: 20px; }
  .pill {
    display: inline-block; padding: 3px 12px; border-radius: 999px;
    background: var(--accent-bg); color: var(--accent);
    font-size: 12px; font-weight: 600;
  }
  .container {
    max-width: 960px; margin: 0 auto; padding: 24px;
  }
  .grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
    margin-bottom: 24px;
  }
  @media (max-width: 640px) {
    .grid { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px;
  }
  .card h2 { font-size: 16px; margin-bottom: 12px; }
  .card p, .card pre { font-size: 14px; color: var(--muted); line-height: 1.6; }
  .card pre {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px; overflow-x: auto;
    white-space: pre-wrap; word-break: break-all;
    max-height: 300px; overflow-y: auto;
    font-size: 13px; color: var(--text);
  }
  .status-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent); margin-right: 6px;
  }
  .status-dot.off { background: var(--danger); }
  label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 4px; }
  input, textarea, select {
    width: 100%; padding: 10px 12px;
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); font-size: 14px;
    outline: none; resize: vertical;
  }
  input:focus, textarea:focus { border-color: var(--accent); }
  textarea { min-height: 80px; font-family: monospace; }
  .btn {
    display: inline-block; padding: 10px 20px;
    border: none; border-radius: 8px; cursor: pointer;
    font-size: 14px; font-weight: 600;
    background: var(--accent-bg); color: var(--accent);
    transition: opacity 0.15s;
  }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-danger { background: #3a1f1f; color: var(--danger); }
  .btn-blue { background: #1f2a3a; color: var(--blue); }
  .row { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
  .mt { margin-top: 16px; }
  .kv { display: flex; justify-content: space-between; padding: 6px 0;
    border-bottom: 1px solid var(--border); font-size: 13px; }
  .kv:last-child { border: none; }
  .kv .k { color: var(--muted); }
  footer { text-align: center; padding: 32px; color: #5d6577; font-size: 12px; }
  a { color: var(--blue); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .tabs {
    display: flex; gap: 0; border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }
  .tab {
    padding: 10px 20px; cursor: pointer; font-size: 14px;
    color: var(--muted); border-bottom: 2px solid transparent;
    transition: all 0.15s;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .log-line { font-family: monospace; font-size: 12px; line-height: 1.5; color: var(--muted); }
  .jobs-table { width: 100%; font-size: 13px; border-collapse: collapse; }
  .jobs-table th, .jobs-table td {
    padding: 8px; text-align: left;
    border-bottom: 1px solid var(--border);
  }
  .jobs-table th { color: var(--muted); font-weight: 600; }
  .badge { padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }
  .badge-done { background: #1f3a2a; color: #6ee7a8; }
  .badge-failed { background: #3a1f1f; color: #f87171; }
  .badge-running { background: #1f2a3a; color: #8ab4ff; }
  .badge-queued { background: #2a2a1f; color: #e7d76e; }
  #no-api-warning {
    background: #3a2a1f; border: 1px solid #5a4020; border-radius: 8px;
    padding: 16px; margin-bottom: 24px; display: none;
  }
  :focus-visible { outline: 3px solid var(--blue); outline-offset: 3px; }
  button, .tab, input, select { min-height: 44px; }
  @media (max-width: 640px) {
    .top-bar { align-items: flex-start; gap: 12px; padding: 16px; }
    .container { padding: 16px; }
    .tabs { overflow-x: auto; scroll-snap-type: x mandatory; }
    .tab { flex: 0 0 auto; scroll-snap-align: start; padding: 12px 16px; }
    .row { align-items: stretch; flex-direction: column; }
    .row > input, .row > select, .row > .btn { width: 100% !important; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
  }
</style>
</head>
<body>

<div class="top-bar">
  <h1>👁 VISION</h1>
  <span class="pill" id="connection-status" role="status" aria-live="polite">⏳ Connecting…</span>
</div>

<div class="container">

  <div id="no-api-warning">
    <strong>⚠️ Fable5 API не подключён</strong><br/>
    <span style="color:#a8b0bf;font-size:13px;">
      Для работы VISION нужен включённый Fable5 endpoint. Зайди в бота и активируй Fable5.
    </span>
  </div>

  <div class="tabs" role="tablist" aria-label="Разделы VISION">
    <div class="tab active" role="tab" tabindex="0" aria-selected="true" data-tab="overview">📊 Обзор</div>
    <div class="tab" role="tab" tabindex="-1" aria-selected="false" data-tab="terminal">🖥 Терминал</div>
    <div class="tab" role="tab" tabindex="-1" aria-selected="false" data-tab="send">💬 Отправить</div>
    <div class="tab" role="tab" tabindex="-1" aria-selected="false" data-tab="logs">📜 Логи</div>
    <div class="tab" role="tab" tabindex="-1" aria-selected="false" data-tab="state">State</div>
    <div class="tab" role="tab" tabindex="-1" aria-selected="false" data-tab="data">🗄 Данные</div>
    <div class="tab" role="tab" tabindex="-1" aria-selected="false" data-tab="coder">💻 Кодер</div>
    <div class="tab" role="tab" tabindex="-1" aria-selected="false" data-tab="botmenu">🤖 Меню бота</div>
  </div>

  <!-- OVERVIEW TAB -->
  <div class="tab-content active" id="tab-overview">
    <div class="grid">
      <div class="card">
        <h2>🤖 Бот</h2>
        <div id="bot-info">
          <p>Загрузка…</p>
        </div>
      </div>
      <div class="card">
        <h2>🔑 Ключи</h2>
        <div id="keys-info">
          <p>Загрузка…</p>
        </div>
      </div>
    </div>
    <div class="card mt">
      <h2>📋 Аддоны</h2>
      <div id="addons-info">
        <p>Загрузка…</p>
      </div>
    </div>
    <div class="card mt">
      <h2>🗂 Последние джобы</h2>
      <div id="jobs-info">
        <p>Загрузка…</p>
      </div>
    </div>
  </div>

  <!-- TERMINAL TAB -->
  <div class="tab-content" id="tab-terminal">
    <div class="card">
      <h2>🖥 Выполнить команду</h2>
      <label for="exec-cmd">Команда (bash)</label>
      <input type="text" id="exec-cmd" placeholder="ls -la data/" />
      <div class="row">
        <label for="exec-timeout" style="margin:0;">Timeout (с):</label>
        <input type="number" id="exec-timeout" value="20" min="1" max="120" style="width:80px;" />
        <button class="btn" id="exec-btn" onclick="runExec()">▶ Выполнить</button>
      </div>
      <pre id="exec-output" class="mt" style="display:none;"></pre>
    </div>
  </div>

  <!-- SEND TAB -->
  <div class="tab-content" id="tab-send">
    <div class="card">
      <h2>💬 Отправить сообщение в Telegram</h2>
      <label for="send-chat">Chat ID (пусто = владелец)</label>
      <input type="text" id="send-chat" placeholder="" />
      <label for="send-text" class="mt">Текст (HTML)</label>
      <textarea id="send-text" placeholder="Привет от VISION!"></textarea>
      <div class="row">
        <button class="btn" id="send-btn" onclick="sendMessage()">📤 Отправить</button>
      </div>
      <pre id="send-output" class="mt" style="display:none;"></pre>
    </div>
  </div>

  <!-- LOGS TAB -->
  <div class="tab-content" id="tab-logs">
    <div class="card">
      <h2>📜 Логи</h2>
      <div class="row" style="margin-top:0;">
        <label for="log-file" style="margin:0;">Файл:</label>
        <input type="text" id="log-file" value="inbox.log" style="width:200px;" />
        <label for="log-lines" style="margin:0;">Строк:</label>
        <input type="number" id="log-lines" value="100" min="1" max="5000" style="width:80px;" />
        <button class="btn" onclick="loadLogs()">🔄 Загрузить</button>
      </div>
      <pre id="log-output" class="mt"></pre>
    </div>
  </div>

  <!-- STATE TAB -->
  <div class="tab-content" id="tab-state">
    <div class="card">
      <h2>⚙️ state.json</h2>
      <p style="margin-bottom:12px;">Полное состояние бота. Редактировать осторожно!</p>
      <pre id="state-output" style="max-height:400px;">Загрузка…</pre>
      <label for="state-edit" class="mt">Редактор (JSON, применяется как deep-merge)</label>
      <textarea id="state-edit" style="min-height:160px;" spellcheck="false"></textarea>
      <div class="row">
        <button class="btn btn-blue" onclick="loadState()">🔄 Обновить</button>
        <button class="btn" onclick="saveState()">💾 Сохранить</button>
      </div>
      <pre id="state-save-msg" class="mt" style="display:none;"></pre>
    </div>
  </div>

  <!-- DATA TAB: exposes every remaining Fable5 capability -->
  <div class="tab-content" id="tab-data">
    <div class="grid">
      <div class="card">
        <h2>🗂 Файлы data</h2>
        <label for="files-path">Относительный каталог</label>
        <input type="text" id="files-path" value="." />
        <div class="row">
          <button class="btn btn-blue" id="files-btn" onclick="loadFiles()">Обновить список</button>
        </div>
        <pre id="files-output" class="mt">Загрузка…</pre>
      </div>
      <div class="card">
        <h2>📚 Возможности</h2>
        <p>Полный перечень зарегистрированных возможностей из data/capabilities.md.</p>
        <div class="row">
          <button class="btn btn-blue" id="capabilities-btn" onclick="loadCapabilities()">Обновить возможности</button>
        </div>
        <div id="capabilities-output" class="mt"><p>Загрузка…</p></div>
      </div>
    </div>
    <div class="card mt">
      <h2>🧮 SQLite-запрос</h2>
      <p>Запрос к debate_v3.db. Для безопасности интерфейс принимает только SELECT, WITH, EXPLAIN и PRAGMA.</p>
      <label for="db-sql">SQL</label>
      <textarea id="db-sql" spellcheck="false" placeholder="SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"></textarea>
      <div class="row">
        <button class="btn" id="db-btn" onclick="runDbQuery()">Выполнить запрос</button>
      </div>
      <pre id="db-output" class="mt" style="display:none;"></pre>
    </div>
  </div>

  <!-- CODER TAB -->
  <div class="tab-content" id="tab-coder">
    <div class="grid">
      <div class="card">
        <h2>🚀 Запустить проект в Coder Firm</h2>
        <label for="coder-task-desc">Описание задачи</label>
        <textarea id="coder-task-desc" placeholder="Например: Напиши асинхронный логгер транзакций для базы данных SQLite WAL..." style="min-height: 120px;"></textarea>
        <div class="row">
          <button class="btn" id="coder-run-btn" onclick="runCoderTask()">▶ Запустить проект</button>
        </div>
        <pre id="coder-run-output" class="mt" style="display:none;"></pre>
      </div>

      <div class="card">
        <h2>📊 Метрики и Статус Coder Firm</h2>
        <div id="coder-metrics">
          <p>Загрузка…</p>
        </div>
        <div class="row">
          <button class="btn btn-blue" onclick="loadCoderMetrics()">🔄 Обновить метрики</button>
        </div>
      </div>
    </div>

    <div class="card mt">
      <h2>🏆 Активные проекты и Скоркарды (Scorecards)</h2>
      <div id="coder-runs-list">
        <p>Загрузка…</p>
      </div>
    </div>

    <div class="card mt">
      <h2>📜 Хронологические Логи Coder Firm (Audit CLI)</h2>
      <div class="row" style="margin-top:0;">
        <label for="coder-log-lines" style="margin:0;">Строк:</label>
        <input type="number" id="coder-log-lines" value="30" min="1" max="500" style="width:80px;" />
        <button class="btn btn-blue" onclick="loadCoderLogs()">🔄 Обновить логи</button>
      </div>
      <pre id="coder-log-output" class="mt" style="max-height: 400px; overflow-y: auto; font-family: monospace; font-size: 12px; line-height: 1.5; color: var(--muted); background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px;"></pre>
    </div>
  </div>

  <!-- BOT MENU TAB (ТЗ-1 Этап 1: те же экраны, что и в Telegram) -->
  <div class="tab-content" id="tab-botmenu">
    <div class="card">
      <h2 id="botmenu-title">🤖 Меню бота</h2>
      <p id="botmenu-body" style="margin-bottom:12px; white-space:pre-wrap;"></p>
      <div id="botmenu-crumbs" class="row" style="margin-top:0; margin-bottom:8px;"></div>
      <div id="botmenu-rows"><p>Загрузка…</p></div>
      <pre id="botmenu-msg" class="mt" style="display:none;"></pre>
    </div>
  </div>

</div>

<footer>
  VISION · защищённая сессия <code>{{TOKEN_SHORT}}</code> · доступ до {{EXPIRES_AT}}
</footer>

<script>
const API = "{{FABLE5_API}}";
const JOBS_DB = "{{JOBS_DB}}";

// Tabs — переключение + ленивая загрузка данных вкладки.
const _tabLoaders = {
  overview: () => loadOverview(),
  logs: () => loadLogs(),
  state: () => loadState(),
  data: () => loadDataTools(),
  coder: () => loadCoder(),
  botmenu: () => loadBotMenu()
};
const _tabLoaded = {};
const tabs = Array.from(document.querySelectorAll('.tab'));
function activateTab(tab) {
  tabs.forEach(t => {
    const active = t === tab;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', String(active));
    t.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  const key = tab.dataset.tab;
  if (_tabLoaders[key] && !_tabLoaded[key]) {
    _tabLoaded[key] = true;
    _tabLoaders[key]();
  }
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => activateTab(tab));
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    let next = index;
    if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    const target = ['Enter', ' '].includes(event.key) ? tab : tabs[next];
    target.focus();
    activateTab(target);
  });
});

function badge(status) {
  const cls = {done:'badge-done',failed:'badge-failed',running:'badge-running',queued:'badge-queued'}[status]||'';
  return `<span class="badge ${cls}">${status}</span>`;
}

// POSIX shell single-quote: safe for any user text passed to /exec.
function shq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

let lastApiError = '';

async function apiFetch(path, opts) {
  if (!API) { showNoApi(); return null; }
  lastApiError = '';
  try {
    const r = await fetch(API + path, opts);
    if (!r.ok) {
      // Сервер отвечает JSON с полем error — показываем реальную причину.
      let detail = '';
      try {
        const body = await r.json();
        if (body && body.error) detail = String(body.error);
      } catch(_) { /* not json */ }
      lastApiError = `HTTP ${r.status}` + (detail ? `: ${detail}` : '');
      throw new Error(lastApiError);
    }
    return await r.json();
  } catch(e) {
    if (!lastApiError) lastApiError = e && e.message ? e.message : 'network error';
    console.error(path, e);
    return null;
  }
}

function apiErrText(prefix) {
  return (prefix || '❌ Ошибка запроса') + (lastApiError ? ' (' + lastApiError + ')' : '');
}

function showNoApi() {
  document.getElementById('no-api-warning').style.display = 'block';
  document.getElementById('connection-status').textContent = '❌ No API';
  document.getElementById('connection-status').style.background = '#3a1f1f';
  document.getElementById('connection-status').style.color = '#f87171';
}

function showConnected() {
  const pill = document.getElementById('connection-status');
  document.getElementById('no-api-warning').style.display = 'none';
  pill.textContent = '🟢 Connected · ' + new Date().toLocaleTimeString();
  pill.style.background = '';
  pill.style.color = '';
}

async function loadOverview() {
  if (!API) { showNoApi(); return; }

  // Ping
  const ping = await apiFetch('/ping');
  if (!ping) { showNoApi(); return; }

  showConnected();

  // Info
  const info = await apiFetch('/info');
  if (info) {
    document.getElementById('bot-info').innerHTML = `
      <div class="kv"><span class="k">Owner ID</span><span>${info.owner_id||'—'}</span></div>
      <div class="kv"><span class="k">Brain</span><span>${info.brain||'не задан'}</span></div>
      <div class="kv"><span class="k">Provider</span><span>${info.provider||'—'}</span></div>
      <div class="kv"><span class="k">Data dir</span><span style="font-family:monospace;font-size:12px;">${info.data_dir||'—'}</span></div>
    `;
    document.getElementById('keys-info').innerHTML =
      (info.providers_with_keys||[]).length
        ? info.providers_with_keys.map(k => `<span class="badge badge-done" style="margin:2px;">${k}</span>`).join(' ')
        : '<p>Нет ключей</p>';
    document.getElementById('addons-info').innerHTML =
      (info.addons||[]).length
        ? info.addons.map(a => `<span class="badge badge-running" style="margin:2px;">${a}</span>`).join(' ')
        : '<p>Нет аддонов</p>';
  }

  // Последние джобы — всегда пробуем загрузить, не зависим от /state.
  loadJobs();
}

async function loadJobs() {
  const box = document.getElementById('jobs-info');
  if (!JOBS_DB) { box.innerHTML = '<p>Джобов пока нет</p>'; return; }
  // Только stdlib (sqlite3/json/os) — работает в любом python3,
  // путь к БД подставлен сервером, кавычки экранируются через shq.
  const py = "import sqlite3,json,os;" +
    "db=" + JSON.stringify(JOBS_DB) + ";" +
    "c=sqlite3.connect(db) if os.path.exists(db) else None;" +
    "rows=c.execute('SELECT id,kind,status,created_at FROM jobs ORDER BY id DESC LIMIT 15').fetchall() if c else [];" +
    "print(json.dumps(rows))";
  const result = await apiFetch('/exec', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cmd: 'python3 -c ' + shq(py), timeout: 10 })
  });
  if (result && result.ok && result.stdout) {
    try {
      const jobs = JSON.parse(result.stdout.trim());
      if (jobs.length) {
        let html = '<table class="jobs-table"><tr><th>#</th><th>Kind</th><th>Status</th><th>Created</th></tr>';
        for (const [id,kind,status,created] of jobs) {
          html += `<tr><td>${esc(id)}</td><td>${esc(kind)}</td><td>${badge(status)}</td><td style="font-size:12px;">${esc(String(created||'').slice(0,16))}</td></tr>`;
        }
        html += '</table>';
        box.innerHTML = html;
      } else {
        box.innerHTML = '<p>Джобов пока нет</p>';
      }
    } catch(e) {
      box.innerHTML = '<p>Не удалось загрузить джобы</p>';
    }
  } else {
    box.innerHTML = '<p>Джобов пока нет</p>';
  }
}

async function runExec() {
  const cmd = document.getElementById('exec-cmd').value.trim();
  if (!cmd) return;
  const timeout = parseInt(document.getElementById('exec-timeout').value) || 20;
  const out = document.getElementById('exec-output');
  out.style.display = 'block';
  out.textContent = '⏳ Выполняю…';
  document.getElementById('exec-btn').disabled = true;

  const result = await apiFetch('/exec', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cmd, timeout})
  });

  document.getElementById('exec-btn').disabled = false;
  if (!result) { out.textContent = apiErrText(); return; }
  let text = '';
  if (result.stdout) text += result.stdout;
  if (result.stderr) text += '\n--- stderr ---\n' + result.stderr;
  if (result.error) text += '\n--- error ---\n' + result.error;
  text += '\n\nreturncode: ' + (result.returncode ?? '?');
  out.textContent = text || '(пусто)';
}

async function sendMessage() {
  const chatId = document.getElementById('send-chat').value.trim();
  const text = document.getElementById('send-text').value.trim();
  if (!text) return;
  const out = document.getElementById('send-output');
  out.style.display = 'block';
  out.textContent = '⏳ Отправляю…';
  document.getElementById('send-btn').disabled = true;

  const body = {text, parse_mode: 'HTML'};
  if (chatId) body.chat_id = chatId;

  const result = await apiFetch('/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });

  document.getElementById('send-btn').disabled = false;
  if (!result) { out.textContent = apiErrText(); return; }
  out.textContent = result.ok
    ? `✅ Отправлено (msg_id: ${result.message_id})`
    : `❌ ${result.error || 'Ошибка'}`;
}

async function loadLogs() {
  const file = document.getElementById('log-file').value.trim() || 'inbox.log';
  const lines = parseInt(document.getElementById('log-lines').value) || 100;
  const out = document.getElementById('log-output');
  out.textContent = '⏳ Загружаю…';

  const result = await apiFetch(`/logs?file=${encodeURIComponent(file)}&lines=${lines}`);
  if (!result) { out.textContent = apiErrText(); return; }
  if (result.error) { out.textContent = '❌ ' + result.error; return; }
  out.textContent = (result.lines || []).join('\n') || '(пусто)';
}

async function loadState() {
  const out = document.getElementById('state-output');
  out.textContent = '⏳ Загружаю…';
  const result = await apiFetch('/state');
  if (!result) { out.textContent = apiErrText(); return; }
  out.textContent = JSON.stringify(result, null, 2);
  document.getElementById('state-edit').value = JSON.stringify(result, null, 2);
}

async function saveState() {
  const msg = document.getElementById('state-save-msg');
  msg.style.display = 'block';
  let payload;
  try {
    payload = JSON.parse(document.getElementById('state-edit').value);
  } catch(e) {
    msg.textContent = '❌ Невалидный JSON: ' + e.message;
    return;
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    msg.textContent = '❌ Ожидается JSON-объект';
    return;
  }
  msg.textContent = '⏳ Сохраняю…';
  const result = await apiFetch('/state', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (result && result.ok) {
    msg.textContent = '✅ Сохранено (deep-merge). Обновляю…';
    await loadState();
    msg.textContent = '✅ Сохранено';
  } else {
    msg.textContent = '❌ Ошибка сохранения';
  }
}

async function loadDataTools() {
  await Promise.all([loadFiles(), loadCapabilities()]);
}

async function loadFiles() {
  const path = document.getElementById('files-path').value.trim() || '.';
  const out = document.getElementById('files-output');
  out.textContent = 'Загрузка…';
  const result = await apiFetch('/files?path=' + encodeURIComponent(path));
  if (!result) { out.textContent = apiErrText(); return; }
  const files = result.files || [];
  out.textContent = files.length
    ? files.map(file => `${file.is_dir ? '[DIR]' : '     '} ${file.name}  ${file.size}`).join('\n')
    : '(каталог пуст)';
}

async function loadCapabilities() {
  const out = document.getElementById('capabilities-output');
  out.innerHTML = '<p>Загрузка…</p>';
  const result = await apiFetch('/capabilities');
  if (!result) { out.textContent = apiErrText(); return; }
  const items = result.capabilities || [];
  out.innerHTML = items.length
    ? items.map(item => `<div class="kv"><span class="k">${esc(item.title || item.key)}</span><span>${esc(item.description || '—')}</span></div>`).join('')
    : '<p>Зарегистрированных возможностей пока нет.</p>';
}

async function runDbQuery() {
  const sql = document.getElementById('db-sql').value.trim();
  const out = document.getElementById('db-output');
  if (!sql) return;
  if (!/^(select|with|explain|pragma)\b/i.test(sql)) {
    out.style.display = 'block';
    out.textContent = 'Разрешены только запросы чтения: SELECT, WITH, EXPLAIN или PRAGMA.';
    return;
  }
  out.style.display = 'block';
  out.textContent = 'Выполняю…';
  const result = await apiFetch('/db', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sql})
  });
  if (!result) { out.textContent = apiErrText(); return; }
  out.textContent = JSON.stringify(result, null, 2);
}

async function loadCoder() {
  loadCoderMetrics();
  loadCoderLogs();
}

async function loadCoderMetrics() {
  const metricsDiv = document.getElementById('coder-metrics');
  const runsListDiv = document.getElementById('coder-runs-list');
  metricsDiv.innerHTML = '<p>⏳ Загрузка метрик…</p>';
  runsListDiv.innerHTML = '<p>⏳ Загрузка проектов…</p>';
  
  const cmd = `python3 -c "
import sqlite3, json, os
db = 'data/coder.db'
if not os.path.exists(db):
    print(json.dumps({'error': 'DB_NOT_FOUND'}))
else:
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*), SUM(usd_spent) FROM coder_runs')
        row = cursor.fetchone()
        total_runs = row[0] if row else 0
        total_spent = row[1] if row and row[1] is not None else 0.0
        
        cursor.execute('SELECT run_id, state, usd_spent, task_desc, created_at FROM coder_runs ORDER BY created_at DESC LIMIT 1')
        active_row = cursor.fetchone()
        active_run = dict(active_row) if active_row else None
        
        cursor.execute('SELECT run_id, task_desc, state, workers_count, budget_cap, usd_spent, created_at FROM coder_runs ORDER BY created_at DESC LIMIT 10')
        runs = [dict(r) for r in cursor.fetchall()]
        
        cursor.execute('SELECT scorecard_id, run_id, task_id, passed_tests, failed_tests, verdict, created_at FROM coder_scorecards ORDER BY scorecard_id DESC LIMIT 5')
        scorecards = [dict(s) for s in cursor.fetchall()]
        
        print(json.dumps({
            'total_runs': total_runs,
            'total_spent': total_spent,
            'active_run': active_run,
            'runs': runs,
            'scorecards': scorecards
        }))
        conn.close()
    except Exception as e:
        print(json.dumps({'error': str(e)}))
"`;

  const result = await apiFetch('/exec', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cmd, timeout: 10 })
  });

  if (!result || !result.ok || !result.stdout) {
    metricsDiv.innerHTML = '<p style="color:var(--danger);">❌ Ошибка загрузки метрик</p>';
    runsListDiv.innerHTML = '<p style="color:var(--danger);">❌ Ошибка загрузки проектов</p>';
    return;
  }

  try {
    const data = JSON.parse(result.stdout.trim());
    if (data.error === 'DB_NOT_FOUND') {
      metricsDiv.innerHTML = '<p>База данных Coder Firm пуста. Запусти первый проект!</p>';
      runsListDiv.innerHTML = '<p>Проекты не найдены.</p>';
      return;
    }
    if (data.error) {
      metricsDiv.innerHTML = `<p style="color:var(--danger);">❌ Ошибка: ${data.error}</p>`;
      runsListDiv.innerHTML = '<p>Ошибка.</p>';
      return;
    }

    let activeText = 'Idle';
    if (data.active_run && !['DONE', 'FAILED_INFRA', 'FAILED_BUDGET'].includes(data.active_run.state)) {
      activeText = `🔄 Run ${data.active_run.run_id} (${data.active_run.state})`;
    }
    metricsDiv.innerHTML = `
      <div class="kv"><span class="k">Всего запусков</span><span>${data.total_runs}</span></div>
      <div class="kv"><span class="k">Суммарный расход</span><span>$${data.total_spent.toFixed(4)} USD</span></div>
      <div class="kv"><span class="k">Текущий статус</span><span>${activeText}</span></div>
    `;

    if (data.runs.length === 0) {
      runsListDiv.innerHTML = '<p>Проектов пока нет</p>';
    } else {
      let html = '<table class="jobs-table"><tr><th>Run ID</th><th>Задача</th><th>Статус</th><th>Затраты</th><th>Дата</th></tr>';
      for (const r of data.runs) {
        const state = String(r.state || '?');
        let statusBadge;
        if (state === 'DONE') statusBadge = '<span class="badge badge-done">DONE</span>';
        else if (state.startsWith('FAILED')) statusBadge = '<span class="badge badge-failed">' + esc(state) + '</span>';
        else statusBadge = '<span class="badge badge-running">' + esc(state) + '</span>';

        html += `<tr>
          <td><code>${esc(r.run_id)}</code></td>
          <td>${esc(r.task_desc)}</td>
          <td>${statusBadge}</td>
          <td>$${Number(r.usd_spent || 0).toFixed(4)}</td>
          <td>${String(r.created_at || '').slice(5, 16)}</td>
        </tr>`;
      }
      html += '</table>';

      if (data.scorecards.length > 0) {
        html += '<h3 style="margin-top:16px; margin-bottom:8px; font-size:14px;">🏆 Последние Scorecards</h3>';
        html += '<table class="jobs-table"><tr><th>Run ID</th><th>Task ID</th><th>Passed</th><th>Failed</th><th>Verdict</th></tr>';
        for (const s of data.scorecards) {
          const vBadge = s.verdict === 'PASS' ? '<span class="badge badge-done">PASS</span>' : '<span class="badge badge-failed">FAIL</span>';
          html += `<tr>
            <td><code>${esc(s.run_id)}</code></td>
            <td><code>${esc(s.task_id)}</code></td>
            <td><span style="color:#6ee7a8;">${esc(s.passed_tests)}</span></td>
            <td><span style="color:#f87171;">${esc(s.failed_tests)}</span></td>
            <td>${vBadge}</td>
          </tr>`;
        }
        html += '</table>';
      }

      runsListDiv.innerHTML = html;
    }
  } catch(e) {
    metricsDiv.innerHTML = `<p style="color:var(--danger);">❌ Ошибка парсинга: ${e.message}</p>`;
    runsListDiv.innerHTML = '<p>Ошибка.</p>';
  }
}

async function loadCoderLogs() {
  const lines = parseInt(document.getElementById('coder-log-lines').value) || 30;
  const out = document.getElementById('coder-log-output');
  out.textContent = '⏳ Загрузка логов…';

  const cmd = `python3 -c "
import sqlite3, json, os
db = 'data/coder.db'
if not os.path.exists(db):
    print(json.dumps([]))
else:
    try:
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute('SELECT ts, persona, event_kind, message FROM coder_audit_events ORDER BY event_id DESC LIMIT ${lines}')
        rows = cursor.fetchall()
        print(json.dumps(rows))
        conn.close()
    except Exception as e:
        print(json.dumps([['', 'ERROR', 'error', str(e)]]))
"`;

  const result = await apiFetch('/exec', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cmd, timeout: 10 })
  });

  if (!result || !result.ok || !result.stdout) {
    out.textContent = apiErrText('❌ Ошибка запроса логов');
    return;
  }

  try {
    const rows = JSON.parse(result.stdout.trim());
    if (rows.length === 0) {
      out.textContent = '(логи пусты)';
      return;
    }
    const formatted = rows.reverse().map(r => {
      const timePart = r[0].split(' ').pop() || r[0];
      return `[${timePart}] [${r[1].toUpperCase()}] [${r[2].toUpperCase()}] ${r[3]}`;
    }).join('\n');
    out.textContent = formatted;
    out.scrollTop = out.scrollHeight;
  } catch(e) {
    out.textContent = '❌ Ошибка парсинга логов: ' + e.message;
  }
}

async function runCoderTask() {
  const desc = document.getElementById('coder-task-desc').value.trim();
  if (!desc) return;
  const out = document.getElementById('coder-run-output');
  out.style.display = 'block';
  out.textContent = '⏳ Инициализация проекта и запуск FSM-симуляции…';
  document.getElementById('coder-run-btn').disabled = true;

  // Shell-safe quoting: wrap in single quotes, escape embedded ones.
  const cmd = 'python3 -m bot.main --coder-run ' + shq(desc);

  const result = await apiFetch('/exec', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cmd, timeout: 120 })
  });

  document.getElementById('coder-run-btn').disabled = false;
  if (!result) { out.textContent = apiErrText(); return; }

  let text = '';
  if (result.stdout) text += result.stdout;
  if (result.stderr) text += '\n' + result.stderr;
  if (result.error) text += '\n--- error ---\n' + result.error;
  
  out.textContent = text || '(пусто)';
  document.getElementById('coder-task-desc').value = '';
  
  loadCoder();
}

// --- Bot menu (ТЗ-1 Этап 1): те же экраны, что и кнопки в Telegram ---
// Контракт §4.7: GET /menu → {"screen": {...}};
// POST /action → 200 {"screen"|"text"} | 403 {"error","text"} | 409 tg_only.
function renderBotScreen(screen) {
  document.getElementById('botmenu-title').textContent = screen.title || '🤖 Меню бота';
  document.getElementById('botmenu-body').textContent = screen.body || '';
  // Хлебные крошки — по parent экрана (§4.7), без клиентской истории.
  const crumbs = document.getElementById('botmenu-crumbs');
  crumbs.innerHTML = '';
  if (screen.parent) {
    const back = document.createElement('button');
    back.className = 'btn btn-blue';
    back.textContent = '← Назад';
    back.onclick = () => loadBotMenu(screen.parent);
    crumbs.appendChild(back);
  }
  const wrap = document.getElementById('botmenu-rows');
  wrap.innerHTML = '';
  (screen.rows || []).forEach(row => {
    const div = document.createElement('div');
    div.className = 'row';
    row.forEach(btn => {
      if (btn.url) {
        const a = document.createElement('a');
        a.className = 'btn btn-blue';
        a.href = btn.url; a.target = '_blank'; a.rel = 'noopener';
        a.textContent = btn.text;
        div.appendChild(a);
      } else {
        const b = document.createElement('button');
        b.className = 'btn';
        b.textContent = btn.text;
        b.onclick = () => botMenuAction(btn.action);
        div.appendChild(b);
      }
    });
    wrap.appendChild(div);
  });
  // Этапы 2–3: на live-экранах Discord/VK токен вводится с сайта
  // (action-префиксы gateway:discord:settoken: / gateway:vk:settoken:).
  const tokenTargets = {
    'gateway:discord:live': { prefix: 'gateway:discord:settoken:', ph: 'Discord Bot Token' },
    'gateway:vk:live': { prefix: 'gateway:vk:settoken:', ph: 'VK токен сообщества (messages)' }
  };
  const target = tokenTargets[screen.screen_id];
  if (target) {
    const form = document.createElement('div');
    form.className = 'row';
    const input = document.createElement('input');
    input.type = 'password';
    input.placeholder = target.ph;
    input.style.cssText = 'flex:1; min-width:200px; padding:8px; border-radius:6px; border:1px solid #444; background:#1a1a2a; color:#eee;';
    const save = document.createElement('button');
    save.className = 'btn btn-blue';
    save.textContent = '💾 Сохранить токен';
    save.onclick = () => {
      const v = input.value.trim();
      if (!v) return;
      input.value = '';
      botMenuAction(target.prefix + v);
    };
    form.appendChild(input);
    form.appendChild(save);
    wrap.appendChild(form);
  }
}

async function loadBotMenu(screenId) {
  const data = await apiFetch('/menu?screen=' + encodeURIComponent(screenId || 'main'));
  if (!data || !data.screen) {
    document.getElementById('botmenu-rows').innerHTML = '<p>❌ Не удалось загрузить меню</p>';
    return;
  }
  renderBotScreen(data.screen);
}

async function botMenuAction(action) {
  const msg = document.getElementById('botmenu-msg');
  msg.style.display = 'none';
  if (!API) { showNoApi(); return; }
  let res, body;
  try {
    res = await fetch(API + '/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    body = await res.json();
  } catch (e) {
    msg.style.display = 'block'; msg.textContent = apiErrText();
    return;
  }
  if (res.status === 409 && body && body.error === 'tg_only') {
    msg.style.display = 'block';
    msg.textContent = '⏳ Этот экран пока доступен только в Telegram.' +
      (body.deeplink ? ' → ' + body.deeplink : '');
    return;
  }
  if (!res.ok) {
    msg.style.display = 'block';
    msg.textContent = (body && body.text) || '❌ Действие отклонено';
    return;
  }
  if (body.text) { msg.style.display = 'block'; msg.textContent = body.text; }
  if (body.screen) renderBotScreen(body.screen);
}

// Init
_tabLoaded.overview = true;
loadOverview();

// Авто-обновление: панель сама подхватывает изменения проекта.
// Обзор — каждые 30 с; активная вкладка Кодера — каждые 60 с.
setInterval(() => {
  loadOverview();
  if (document.getElementById('tab-coder').classList.contains('active')) {
    loadCoder();
  }
}, 30000);
</script>
</body>
</html>
"""


def register_http_endpoints(app: web.Application) -> None:
    """Attach VISION routes to the bot's aiohttp app."""
    app.router.add_get("/vision/{token}/ping", _ping)
    app.router.add_get("/vision/{token}/", _root)
    app.router.add_get("/vision/{token}", _root)
    logger.info("vision endpoints registered on aiohttp app")
