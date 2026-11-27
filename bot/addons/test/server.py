# Test preview surface: hosts token-gated payloads including regen fix previews.
# Co-authored with CoCo
"""HTTP endpoints for the «🧪 Тест» live-preview surface.

Exposes a tiny REST + HTML surface under ``/test/<token>/...`` that
an external code-gen bot can POST preview payloads into. Chloe opens
the URL in her browser to *see* whatever the coder-bot produced
(plain HTML / iframe / JSON) — lilush itself does **not** generate
the content; it only hosts and gates access.

Available endpoints (every one of them needs the path token to match
``addons.test.token`` AND ``addons.test.enabled is True``):

* ``GET  /test/<token>/``         — splash HTML (or last payload).
* ``GET  /test/<token>/ping``     — liveness JSON.
* ``GET  /test/<token>/info``     — owner / payload metadata.
* ``POST /test/<token>/payload``  — stash a preview payload (placeholder).

Bad / disabled token → ``403 {"error": "forbidden"}`` (neutral, so
the URL doesn't leak whether it once existed).

This is deliberately a **caркас (skeleton)** per Chloe's spec:
no device frames, no FSM mode picker, no mock-Telegram UI. The
coder-bot project will fill `/payload` later; for now the splash
page just confirms the route is alive.
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from typing import Any, Optional

from aiohttp import web

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "test"


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


def get_payload() -> dict[str, Any]:
    raw = addon_state.get(ADDON, "payload", None)
    return raw if isinstance(raw, dict) else {}


# --- targets (Render / Oracle / Custom) -------------------------------
#
# Chloe's deploy flow: she always commits the project to GitHub and lets
# Render or Oracle (or another host) auto-deploy. The bot does NOT
# deploy anything; it just stores the URL of whichever host is "live"
# right now and offers a one-tap «🔗 Открыть превью» button so she
# (and demo viewers) can poke at the deployed project.
#
# State layout:
#
#   addons.test.targets = {
#       "render": {"url": "https://my-bot.onrender.com"},
#       "oracle": {"url": "https://bot.example.com"},
#       "custom": [{"url": "https://foo.dev", "name": "foo.dev"}, ...],
#   }
#   addons.test.active_target_id = "render" | "oracle" | "custom:0" | "lilush"
#
# "lilush" is a synthetic always-available target that points at the
# bot's own /test/<token>/ skeleton — handy for proving the surface
# itself works when no external host is configured yet.

_TARGET_LABELS = {
    "lilush": "🤖 Lilush surface",
    "render": "🚀 Render",
    "oracle": "🐙 Oracle",
}


def _targets() -> dict[str, Any]:
    raw = addon_state.get(ADDON, "targets", None)
    if not isinstance(raw, dict):
        return {}
    return raw


def get_target_url(target_id: str) -> str:
    """Read URL for a single named target ('render', 'oracle', 'custom:N')."""
    if target_id == "lilush":
        return get_link()
    t = _targets()
    if target_id.startswith("custom:"):
        try:
            idx = int(target_id.split(":", 1)[1])
        except Exception:  # noqa: BLE001
            return ""
        custom = t.get("custom", []) or []
        if 0 <= idx < len(custom):
            return str(custom[idx].get("url", "") or "")
        return ""
    bucket = t.get(target_id) or {}
    return str(bucket.get("url", "") or "")


def get_target_label(target_id: str) -> str:
    base = _TARGET_LABELS.get(target_id, "")
    if base:
        return base
    if target_id.startswith("custom:"):
        try:
            idx = int(target_id.split(":", 1)[1])
        except Exception:  # noqa: BLE001
            return "⚙️ Custom"
        t = _targets()
        custom = t.get("custom", []) or []
        if 0 <= idx < len(custom):
            name = str(custom[idx].get("name", "") or "")
            return f"⚙️ {name}" if name else "⚙️ Custom"
    return target_id


def set_target_url(target_id: str, url: str) -> None:
    """Set the URL for the 'render' or 'oracle' preset slot."""
    if target_id not in {"render", "oracle"}:
        raise ValueError(f"set_target_url only handles preset slots, got {target_id!r}")
    t = dict(_targets())
    bucket = dict(t.get(target_id) or {})
    bucket["url"] = url.strip()
    t[target_id] = bucket
    addon_state.set_(ADDON, "targets", t)


def add_custom_target(url: str, name: str = "") -> int:
    """Append a custom URL; auto-name from hostname if blank. Returns idx."""
    url = url.strip()
    if not name:
        # Strip scheme + path → just the host. Falls back to the raw URL
        # if parsing fails so we never silently drop the entry.
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            name = parsed.netloc or url
        except Exception:  # noqa: BLE001
            name = url
    t = dict(_targets())
    custom = list(t.get("custom", []) or [])
    custom.append({"url": url, "name": name})
    t["custom"] = custom
    addon_state.set_(ADDON, "targets", t)
    return len(custom) - 1


def remove_custom_target(idx: int) -> bool:
    t = dict(_targets())
    custom = list(t.get("custom", []) or [])
    if 0 <= idx < len(custom):
        custom.pop(idx)
        t["custom"] = custom
        addon_state.set_(ADDON, "targets", t)
        # If we just deleted the active custom slot, fall back to lilush.
        if get_active_target_id() == f"custom:{idx}":
            set_active_target("lilush")
        return True
    return False


def get_custom_targets() -> list[dict[str, Any]]:
    return list(_targets().get("custom", []) or [])


def get_active_target_id() -> str:
    raw = addon_state.get(ADDON, "active_target_id", None)
    if isinstance(raw, str) and raw:
        return raw
    return "lilush"


def set_active_target(target_id: str) -> None:
    addon_state.set_(ADDON, "active_target_id", target_id)


def get_active_target() -> dict[str, str]:
    """Return ``{"id", "name", "url"}`` for the currently active target.

    Falls back to the synthetic 'lilush' target (bot's own /test/<token>/)
    when nothing else is configured. URL may still be empty if the bot
    has no public URL detected yet.
    """
    tid = get_active_target_id()
    return {
        "id": tid,
        "name": get_target_label(tid),
        "url": get_target_url(tid),
    }


def _resolve_public_base() -> str:
    """Thin wrapper around :func:`bot.config.resolve_public_url`.

    Kept addon-local so tests can monkey-patch this without leaking
    into other addons. The actual fallback chain
    (PUBLIC_URL → Render → Railway → Fly → Oracle) lives in
    :mod:`bot.config` — single source of truth across the bot.
    """
    try:
        from ...config import resolve_public_url
        base = resolve_public_url()
    except Exception:  # noqa: BLE001
        base = ""
    if base:
        return base
    # Local dev fallback: a *relative* link (``/test/<token>/``) is
    # rejected by Telegram (BUTTON_URL_INVALID) when used in a
    # url-button, which silently kills the whole «🧪 Тест» screen.
    # Always return an absolute URL.
    try:
        from ...config import PORT as _port
    except Exception:  # noqa: BLE001
        _port = 8080
    logger.warning(
        "test addon: PUBLIC_URL is not configured — falling back to "
        "http://127.0.0.1:%s. The link will only work on this machine; "
        "set PUBLIC_URL for production.", _port,
    )
    return f"http://127.0.0.1:{_port}"


def get_link(base_url: str | None = None) -> str:
    token = get_token()
    if not token:
        return ""
    base = (base_url if base_url is not None else _resolve_public_base()).rstrip("/")
    return f"{base}/test/{token}/" if base else f"/test/{token}/"


def generate_link(base_url: str | None = None) -> tuple[str, str]:
    """Rotate token, flip ``enabled`` on, return ``(token, public_link)``."""
    token = secrets.token_urlsafe(24)
    addon_state.set_(ADDON, "token", token)
    addon_state.set_(ADDON, "enabled", True)
    addon_state.set_(ADDON, "rotated_at", int(time.time()))
    return token, get_link(base_url)


def disable_link() -> bool:
    """Flip ``enabled → False``. Returns the previous state."""
    was = is_enabled()
    addon_state.set_(ADDON, "enabled", False)
    return was


def publish_regen_preview(fix: Any, verify: dict, errors: list) -> str:
    """Render a regen fix summary as HTML, stash it as the preview payload,
    enable the surface if needed, and return the public link.

    Shows: problem, root cause, before→after scan, changed files and the
    diff — so the user can review the fix in a browser before deploying.
    """
    # Ensure the surface is live (rotate a token if none yet).
    if not get_token() or not is_enabled():
        generate_link()

    def esc(x: Any) -> str:
        return html.escape(str(x or ""))

    problem = getattr(fix, "problem", "") if fix else ""
    root_cause = getattr(fix, "root_cause", "") if fix else ""
    instructions = getattr(fix, "instructions", "") if fix else ""
    files = getattr(fix, "files", {}) if fix else {}
    diff = getattr(fix, "diff", "") if fix else ""
    confidence = getattr(fix, "confidence", "") if fix else ""

    b, a = verify.get("before"), verify.get("after")
    if a is not None and b is not None:
        mark = "✅" if a < b else ("➖" if a == b else "⚠️")
        verdict = f"{mark} ошибок: было {b} → стало {a}"
    elif a is not None:
        verdict = f"ошибок сканера после фикса: {a}"
    else:
        verdict = "повторный скан не выполнен"

    files_html = "".join(
        f"<details><summary><code>{esc(path)}</code></summary>"
        f"<pre>{esc(content)[:20000]}</pre></details>"
        for path, content in (files or {}).items()
    ) or "<em>кодер не дал полных файлов — см. инструкцию</em>"

    err_html = ""
    if errors:
        err_html = (
            "<div class='card' style='border-color:#5c3434'>"
            "<strong>⚠️ Ошибки компиляции:</strong><pre>"
            + esc("\n".join(errors[:30])) + "</pre></div>"
        )

    content = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>🧪 Regen — превью фикса</title>
<style>
 body{{margin:0;background:#0b0d12;color:#e6e9ef;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif}}
 main{{max-width:860px;margin:0 auto;padding:40px 20px}}
 h1{{font-size:24px}} .card{{background:#131720;border:1px solid #232a39;border-radius:12px;padding:16px 18px;margin:14px 0}}
 pre{{white-space:pre-wrap;word-break:break-word;background:#1c2230;padding:12px;border-radius:8px;font-size:12px;overflow:auto}}
 code{{background:#1c2230;padding:2px 6px;border-radius:6px}} summary{{cursor:pointer;margin:6px 0}}
 .verdict{{font-size:18px;font-weight:700}}
</style></head><body><main>
 <h1>🧪 Превью фикса (Регенерация)</h1>
 <div class="card"><div class="verdict">{esc(verdict)}</div>
   <div>Уверенность кодера: <b>{esc(confidence)}</b></div></div>
 <div class="card"><strong>Проблема</strong><pre>{esc(problem)}</pre></div>
 <div class="card"><strong>Причина</strong><pre>{esc(root_cause) or '—'}</pre></div>
 <div class="card"><strong>Инструкция</strong><pre>{esc(instructions) or '—'}</pre></div>
 {err_html}
 <div class="card"><strong>Изменённые файлы</strong>{files_html}</div>
 {"<div class='card'><strong>Diff</strong><pre>" + esc(diff) + "</pre></div>" if diff else ""}
</main></body></html>"""

    record = {"kind": "html", "content": content[:_MAX_PAYLOAD_BYTES], "ts": int(time.time())}
    addon_state.set_(ADDON, "payload", record)
    return get_link()


# --- HTTP handlers ----------------------------------------------------

def _check(token: str) -> Optional[web.Response]:
    stored = get_token()
    if not stored or not secrets.compare_digest(stored, token):
        return web.json_response({"error": "forbidden"}, status=403)
    if not is_enabled():
        return web.json_response({"error": "forbidden"}, status=403)
    return None


_SPLASH_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="robots" content="noindex,nofollow" />
<title>🧪 Тест — Lilush preview slot</title>
<style>
  :root {{
    color-scheme: dark light;
  }}
  html, body {{
    margin: 0;
    padding: 0;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    background: #0b0d12;
    color: #e6e9ef;
  }}
  main {{
    max-width: 720px;
    margin: 0 auto;
    padding: 64px 24px;
  }}
  h1 {{
    font-size: 28px;
    margin: 0 0 12px;
  }}
  p.lede {{
    font-size: 16px;
    color: #a8b0bf;
    margin: 0 0 32px;
  }}
  .card {{
    background: #131720;
    border: 1px solid #232a39;
    border-radius: 12px;
    padding: 20px 22px;
    margin: 16px 0;
  }}
  code {{
    background: #1c2230;
    padding: 2px 6px;
    border-radius: 6px;
    font-size: 13px;
  }}
  .pill {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    background: #1f3a2a;
    color: #6ee7a8;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }}
  footer {{
    margin-top: 48px;
    color: #5d6577;
    font-size: 12px;
  }}
  a {{ color: #8ab4ff; }}
</style>
</head>
<body>
<main>
  <span class="pill">🧪 ТЕСТ-СЛОТ</span>
  <h1>Превью-слот готов к работе</h1>
  <p class="lede">
    Это пустая площадка для предпросмотра того, что сгенерирует
    отдельный кодер-бот. Сюда позже будут приходить ссылки и контент
    через <code>POST /test/{token_short}…/payload</code>. Пока что
    маршрут активен и token-gate работает.
  </p>

  <div class="card">
    <strong>Статус:</strong> 🟢 surface активен<br />
    <strong>Поднят:</strong> {rotated_human}<br />
    <strong>Последний payload:</strong> {payload_summary}
  </div>

  <div class="card">
    <strong>Что сюда складывать?</strong><br />
    Кодер-бот (отдельный проект) шлёт сгенерированный сайт / HTML / URL
    через <code>POST /test/&lt;token&gt;/payload</code> с JSON-телом
    <code>{{"kind": "html|url|json", "content": "..."}}</code>.
    Бот сохраняет последний payload в <code>state.json</code> и
    рендерит его на этой странице.
  </div>

  <footer>
    Lilush · <code>addons.test</code> · token <code>{token_short}…</code>
  </footer>
</main>
</body>
</html>
"""


def _human_ts(epoch: int) -> str:
    if not epoch:
        return "ещё не поднимали"
    try:
        import datetime as _dt
        return (
            _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC")
        )
    except Exception:  # noqa: BLE001
        return str(epoch)


def _render_splash(token: str) -> str:
    payload = get_payload()
    if payload:
        kind = str(payload.get("kind", "?"))
        size = len(str(payload.get("content", "")))
        ts = payload.get("ts")
        payload_summary = html.escape(
            f"{kind}, {size} симв., обновлён {_human_ts(int(ts) if ts else 0)}"
        )
    else:
        payload_summary = "<em>пока пусто — ждём первый POST от кодер-бота</em>"
    return _SPLASH_HTML.format(
        token_short=html.escape(token[:6]),
        rotated_human=html.escape(_human_ts(get_rotated_at())),
        payload_summary=payload_summary,
    )


async def _root(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    if (resp := _check(token)) is not None:
        return resp
    payload = get_payload()
    # If the coder-bot has posted an HTML payload, serve it raw so the
    # user sees their generated site instead of the splash. URL / JSON
    # payloads fall back to the splash with a summary card (the splash
    # already shows the metadata).
    if payload.get("kind") == "html" and isinstance(payload.get("content"), str):
        return web.Response(text=payload["content"], content_type="text/html")
    return web.Response(text=_render_splash(token), content_type="text/html")


async def _ping(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    return web.json_response(
        {
            "ok": True,
            "rotated_at": get_rotated_at(),
            "has_payload": bool(get_payload()),
            "version": "test/1",
        }
    )


async def _info(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    payload = get_payload()
    return web.json_response(
        {
            "rotated_at": get_rotated_at(),
            "payload_kind": payload.get("kind") if payload else None,
            "payload_size": (
                len(str(payload.get("content", ""))) if payload else 0
            ),
            "payload_ts": payload.get("ts") if payload else None,
        }
    )


# Maximum body size accepted by /payload. The whole point of the slot
# is small preview snippets — anything bigger is almost certainly a
# misuse and we don't want to fill state.json with megabytes.
_MAX_PAYLOAD_BYTES = 200_000


async def _payload_post(request: web.Request) -> web.Response:
    if (resp := _check(request.match_info["token"])) is not None:
        return resp
    if request.content_length is not None and request.content_length > _MAX_PAYLOAD_BYTES:
        return web.json_response({"error": "payload too large"}, status=413)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "expected object"}, status=400)
    kind = str(payload.get("kind", "")).strip().lower()
    content = payload.get("content", "")
    if kind not in {"html", "url", "json"}:
        return web.json_response(
            {"error": "kind must be one of html|url|json"}, status=400
        )
    if not isinstance(content, str):
        return web.json_response({"error": "content must be a string"}, status=400)
    if len(content) > _MAX_PAYLOAD_BYTES:
        return web.json_response({"error": "content too large"}, status=413)
    record = {"kind": kind, "content": content, "ts": int(time.time())}
    addon_state.set_(ADDON, "payload", record)
    return web.json_response({"ok": True, "stored": kind, "bytes": len(content)})


def register_http_endpoints(app: web.Application) -> None:
    """Attach Test routes to the bot's aiohttp app.

    Call once at startup in both polling and webhook modes — mirrors
    :func:`bot.addons.fable5.server.register_http_endpoints`. Order
    matters only inside the addon (root last, so ``/ping`` doesn't
    get shadowed by a greedy prefix match).
    """
    app.router.add_get("/test/{token}/ping", _ping)
    app.router.add_get("/test/{token}/info", _info)
    app.router.add_post("/test/{token}/payload", _payload_post)
    app.router.add_get("/test/{token}/", _root)
    app.router.add_get("/test/{token}", _root)
    logger.info("test endpoints registered on aiohttp app")
