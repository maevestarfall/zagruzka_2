"""Analyzer PRO screens — пульт Analyzer-Boss прямо внутри Lilush.

Главное меню (inline):

    📊 Статус задач      🌐 Серверы
    🚀 Новый анализ      🔗 История
    🔑 Настройки         ❓ Помощь
    ➕ Добавить сервер
    « Назад

Спецификация — см. ``LILUSH_ADDON_INTEGRATION.md`` в репо ``ana``.

* Сервера хранятся под ``addons.analyzer.servers`` как список
  ``{id, url, name, token, addedAt}``.
* Активный сервер — ``addons.analyzer.active_server_id``.
* Задачи — ``addons.analyzer.tasks`` как dict ``{task_id: {...}}``.
* Дефолты (бюджет, приоритет) — ``addons.analyzer.defaults``.

* Эндпоинты Analyzer-Boss (см. PART_1_runtime_routing_lilush_addon.md
  §FastAPI и LILUSH_ADDON_INTEGRATION.md §HTTP-клиент):
    GET  /health
    POST /user/analysis-request
    GET  /user/status?task_id=...
    GET  /sessions/{task_id}/result
    GET  /tools/available

Все запросы идут с ``Authorization: Bearer <token>``.

Все экраны — owner-only (тот же ``_is_owner`` из ``bot/wizard.py``,
что и в debates/ai_check addon'ах).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import state as addon_state

logger = logging.getLogger(__name__)

API_TIMEOUT = 15.0
MAX_FILE_BYTES = 200 * 1024  # 200 KB intake cap per spec
POLL_INTERVAL_SEC = 5.0
POLL_THROTTLE_MS = 300  # parallel /health throttle for «Проверить все»

BUDGET_CHOICES = (100_000, 200_000, 500_000)
PRIORITY_CHOICES = ("critical", "high", "normal", "low", "deferred")
PRIORITY_LABEL = {
    "critical": "🔥 Critical",
    "high": "⬆️ High",
    "normal": "▫️ Normal",
    "low": "⬇️ Low",
    "deferred": "💤 Deferred",
}

# active polling workers keyed by (chat_id, task_id) so we never spawn
# two of them for the same task even if the user reopens detail view.
_POLLERS: dict[tuple[int, str], asyncio.Task] = {}


# ─── State storage ────────────────────────────────────────────────────────────


class AnalyzerStates(StatesGroup):
    awaiting_add_url = State()
    awaiting_add_token = State()
    awaiting_task_text = State()
    awaiting_optional_file = State()
    awaiting_budget = State()
    awaiting_priority = State()
    confirming = State()


def _defaults() -> dict[str, Any]:
    raw = addon_state.get("analyzer", "defaults", None) or {}
    return {
        "budget_tokens": int(raw.get("budget_tokens") or 100_000),
        "priority": raw.get("priority") or "normal",
    }


def _save_defaults(d: dict[str, Any]) -> None:
    addon_state.set_("analyzer", "defaults", d)


def _servers() -> list[dict[str, Any]]:
    raw = addon_state.get("analyzer", "servers", []) or []
    out: list[dict[str, Any]] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip().rstrip("/")
        if not url:
            continue
        out.append(
            {
                "id": str(s.get("id") or f"ana_{int(time.time() * 1000)}"),
                "url": url,
                "name": s.get("name") or url,
                "token": (s.get("token") or "").strip(),
                "addedAt": s.get("addedAt") or "",
            }
        )
    return out


def _save_servers(servers: list[dict[str, Any]]) -> None:
    addon_state.set_("analyzer", "servers", servers)


def _active_id() -> str | None:
    val = addon_state.get("analyzer", "active_server_id", None)
    return str(val) if val else None


def _set_active(server_id: str | None) -> None:
    addon_state.set_("analyzer", "active_server_id", server_id)


def _active_server() -> dict[str, Any] | None:
    aid = _active_id()
    if not aid:
        return None
    return next((s for s in _servers() if s["id"] == aid), None)


def _add_server(url: str, name: str, token: str) -> tuple[bool, dict[str, Any]]:
    url = url.strip().rstrip("/")
    servers = _servers()
    existing = next((s for s in servers if s["url"] == url), None)
    if existing:
        existing["name"] = name.strip() or existing["name"]
        existing["token"] = token.strip() or existing.get("token", "")
        _save_servers(servers)
        if not _active_id():
            _set_active(existing["id"])
        return False, existing
    svc = {
        "id": f"ana_{int(time.time() * 1000)}",
        "url": url,
        "name": name.strip() or url,
        "token": token.strip(),
        "addedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    servers.append(svc)
    _save_servers(servers)
    if not _active_id():
        _set_active(svc["id"])
    return True, svc


def _remove_server(svc_id: str) -> bool:
    servers = _servers()
    new = [s for s in servers if s["id"] != svc_id]
    if len(new) == len(servers):
        return False
    _save_servers(new)
    if _active_id() == svc_id:
        _set_active(new[0]["id"] if new else None)
    return True


def _get_server(svc_id: str) -> dict[str, Any] | None:
    return next((s for s in _servers() if s["id"] == svc_id), None)


def _tasks() -> dict[str, dict[str, Any]]:
    raw = addon_state.get("analyzer", "tasks", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _save_tasks(tasks: dict[str, dict[str, Any]]) -> None:
    addon_state.set_("analyzer", "tasks", tasks)


def _save_task(task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    tasks = _tasks()
    cur = dict(tasks.get(task_id, {}))
    cur.update(patch)
    tasks[task_id] = cur
    _save_tasks(tasks)
    return cur


def _delete_task(task_id: str) -> None:
    tasks = _tasks()
    if task_id in tasks:
        tasks.pop(task_id)
        _save_tasks(tasks)


# ─── HTML helpers ─────────────────────────────────────────────────────────────


def _html(s: Any) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M")
    except Exception:  # noqa: BLE001
        return iso or "—"


def _fmt_budget(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def _trustbar(pct: float) -> str:
    """5-cell coloured bar, мимикрирующий aicheck TrustScore."""
    pct = max(0.0, min(1.0, float(pct)))
    full = int(round(pct * 5))
    return "▰" * full + "▱" * (5 - full)


# ─── Owner gate ───────────────────────────────────────────────────────────────


async def _is_owner_query(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]

    return _is_owner(query.from_user.id if query.from_user else 0)


async def _is_owner_msg(message: Message) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]

    return _is_owner(message.from_user.id if message.from_user else 0)


# ─── HTTP ─────────────────────────────────────────────────────────────────────


async def _fetch(
    svc: dict[str, Any],
    method: str,
    path: str,
    *,
    json_body: Any = None,
    files: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> tuple[int, Any]:
    """One-shot HTTP call against the chosen Analyzer-Boss instance."""
    url = svc["url"].rstrip("/") + (path if path.startswith("/") else "/" + path)
    headers: dict[str, str] = {}
    if svc.get("token"):
        headers["Authorization"] = f"Bearer {svc['token']}"
    try:
        async with httpx.AsyncClient(timeout=timeout or API_TIMEOUT) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers=headers,
                json=json_body if json_body is not None and not files else None,
                files=files,
                data=data,
                params=params,
            )
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            return resp.status_code, body
    except Exception as e:  # noqa: BLE001
        logger.warning("analyzer addon: HTTP %s %s failed: %s", method, url, e)
        return 0, str(e)


async def _fetch_with_retry(
    svc: dict[str, Any],
    method: str,
    path: str,
    *,
    retries: int = 3,
    backoff: float = 0.5,
    **kwargs: Any,
) -> tuple[int, Any]:
    last: tuple[int, Any] = (0, "")
    for attempt in range(retries):
        last = await _fetch(svc, method, path, **kwargs)
        code = last[0]
        if 200 <= code < 500 and code != 0:
            return last
        await asyncio.sleep(backoff * (2**attempt))
    return last


# ─── Public API for other addons (Гермес → Анализатор ПРО) ──────────────────

async def submit_query(text: str, *, budget: int = 4000, priority: str = "normal") -> tuple[bool, str, str]:
    """Submit a research problem to the active Analyzer-Boss.
    Returns ``(ok, task_id, error)``. Lets Hermes hand a problem to Analyzer PRO."""
    svc = _active_server()
    if not svc:
        return (False, "", "нет активного сервера Анализатора ПРО (добавь URL в 🔬 Анализатор ПРО)")
    code, body = await _fetch_with_retry(
        svc, "POST", "/user/analysis-request",
        json_body={"text": text, "budget_tokens": budget, "priority": priority},
    )
    if code != 200 or not isinstance(body, dict) or not body.get("task_id"):
        err = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)[:200]
        return (False, "", f"HTTP {code}: {err}")
    task_id = str(body["task_id"])
    _save_task(task_id, {
        "server_id": svc["id"], "created_at": int(time.time()),
        "status": (body.get("status") or "queued").lower(),
        "budget_tokens": budget, "priority": priority,
        "text_preview": (text or "")[:400], "title": f"Гермес: {text[:40]}",
    })
    return (True, task_id, "")


async def poll_result(task_id: str, *, timeout: float = 90.0, interval: float = 6.0) -> tuple[str, str]:
    """Poll a task until done or timeout. Returns ``(status, text)`` where
    status ∈ {completed, failed, pending}."""
    t = _tasks().get(task_id) or {}
    svc = _get_server(t.get("server_id") or "") or _active_server()
    if not svc:
        return ("failed", "сервер Анализатора недоступен")
    waited = 0.0
    while waited < timeout:
        code, body = await _fetch(svc, "GET", "/user/status", params={"task_id": task_id})
        status = ((body.get("status") if isinstance(body, dict) else "") or "").lower()
        if status in ("completed", "done", "success"):
            rc, rb = await _fetch_with_retry(svc, "GET", f"/sessions/{task_id}/result")
            if rc == 200 and isinstance(rb, dict):
                return ("completed", (rb.get("report_md") or rb.get("report") or rb.get("summary") or "")[:6000])
            return ("completed", "(результат получен, но отчёт пуст)")
        if status in ("failed", "error", "cancelled"):
            return ("failed", (body.get("error") if isinstance(body, dict) else "") or "задача завершилась ошибкой")
        await asyncio.sleep(interval)
        waited += interval
    return ("pending", "")


# ─── Screens ──────────────────────────────────────────────────────────────────


def _render_home(servers: list[dict[str, Any]], active: dict[str, Any] | None) -> str:
    if not servers:
        return (
            "<b>🔬 Анализатор PRO</b>\n\n"
            "Пульт Analyzer Bot Family (ana).\n\n"
            "Серверов пока нет. Добавь Analyzer-Boss через "
            "<b>➕ Добавить сервер</b> — URL + Bearer-токен."
        )
    cnt = len(servers)
    act = (
        f"\n🟢 Активный: <b>{_html(active['name'])}</b>\n"
        f"🔗 <code>{_html(active['url'])}</code>"
        if active
        else "\n⚪ Активный сервер не выбран. Открой <b>🌐 Серверы</b>."
    )
    tasks = _tasks()
    running = sum(1 for t in tasks.values() if t.get("status") in ("running", "queued", "pending"))
    done = sum(1 for t in tasks.values() if t.get("status") == "completed")
    return (
        "<b>🔬 Анализатор PRO</b>\n\n"
        f"Серверов зарегистрировано: <b>{cnt}</b>{act}\n\n"
        f"📊 Задач в работе: <b>{running}</b>\n"
        f"🏁 Завершено: <b>{done}</b>"
    )


def _kb_home(have_servers: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if have_servers:
        rows.append(
            [
                InlineKeyboardButton(text="📊 Статус задач", callback_data="analyzer:tasks"),
                InlineKeyboardButton(text="🌐 Серверы", callback_data="analyzer:servers"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="🚀 Новый анализ", callback_data="analyzer:new_task"),
                InlineKeyboardButton(text="🔗 История", callback_data="analyzer:history"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="🔑 Настройки", callback_data="analyzer:settings"),
                InlineKeyboardButton(text="❓ Помощь", callback_data="analyzer:help"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="❓ Помощь", callback_data="analyzer:help")])
    # tools_ext hook — always visible (works offline too).
    from .tools_ext import menu_button as _tx_btn
    rows.append([_tx_btn()])
    rows.append([InlineKeyboardButton(text="➕ Добавить сервер", callback_data="analyzer:servers:add")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="main:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


HELP_TEXT = (
    "<b>🔬 Анализатор PRO — справка</b>\n\n"
    "Пульт Analyzer Bot Family (ana) — мульти-агентного бэкенда для "
    "глубокого анализа задач. Лилуш-бот выступает фронтом: ставит "
    "задачу в Analyzer-Boss FastAPI, пингует прогресс и приносит "
    "финальный отчёт.\n\n"
    "<b>Кнопки:</b>\n"
    "📊 <b>Статус задач</b> — текущие активные задачи по серверам.\n"
    "🌐 <b>Серверы</b> — список инстансов Analyzer-Boss; выбор активного, "
    "массовый health-check (<b>🔍 Проверить все</b>).\n"
    "🚀 <b>Новый анализ</b> — FSM в 5 шагов:\n"
    "  1. текст задачи (или /skip и загрузить файл);\n"
    "  2. опциональный <code>.txt</code>/<code>.md</code>/<code>.pdf</code> "
    "до 200 KB;\n"
    "  3. бюджет токенов (100K / 200K / 500K);\n"
    "  4. приоритет (critical / high / normal / low / deferred);\n"
    "  5. подтверждение → старт.\n"
    "🔗 <b>История</b> — завершённые задачи, повторное скачивание отчёта.\n"
    "🔑 <b>Настройки</b> — дефолтный бюджет и приоритет.\n\n"
    "<b>Поднять Analyzer-Boss:</b>\n"
    "<code>git clone https://github.com/benjamingriffit/ana\n"
    "cd ana &amp;&amp; pip install -e .\n"
    "export ANALYZER_BOSS_TOKEN=secret\n"
    "uvicorn abf.user_api:app --port 8090</code>\n\n"
    "Потом сюда → <b>➕ Добавить сервер</b> → URL → Bearer-токен.\n\n"
    "Прогресс задач обновляется каждые "
    f"<b>{int(POLL_INTERVAL_SEC)}</b> секунд. Когда задача завершается, "
    "бот присылает <code>report.md</code> + короткое summary."
)


# ─── Screen helpers ───────────────────────────────────────────────────────────


async def _send_or_edit(query: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    if query.message is None:
        await query.answer()
        return
    try:
        await query.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await query.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await query.answer()


async def show_screen(message_or_query) -> None:
    """Главный экран Анализатора PRO."""
    servers = _servers()
    active = _active_server()
    body = _render_home(servers, active)
    kb = _kb_home(bool(servers))
    if isinstance(message_or_query, CallbackQuery):
        await _send_or_edit(message_or_query, body, kb)
    else:
        await message_or_query.answer(body, reply_markup=kb, disable_web_page_preview=True)


# ─── Servers screen ───────────────────────────────────────────────────────────


def _render_servers(servers: list[dict[str, Any]], active_id: str | None, health: dict[str, dict[str, Any]] | None = None) -> str:
    if not servers:
        return (
            "<b>🌐 Серверы Analyzer-Boss</b>\n\n"
            "Список пуст. Добавь первый через <b>➕ Добавить сервер</b>."
        )
    lines = ["<b>🌐 Серверы Analyzer-Boss</b>", ""]
    for s in servers:
        marker = "🟢" if s["id"] == active_id else "⚪"
        h = (health or {}).get(s["id"])
        hline = ""
        if h:
            if h.get("ok"):
                hline = (
                    f"   <i>✅ online {h.get('latency_ms', '?')} ms · "
                    f"v{_html(h.get('version', '?'))}</i>"
                )
            else:
                hline = f"   <i>❌ offline: {_html(h.get('err', '?'))}</i>"
        lines.append(f"{marker} <b>{_html(s['name'])}</b>")
        lines.append(f"   🔗 <code>{_html(s['url'])}</code>")
        if hline:
            lines.append(hline)
        lines.append(f"   <i>добавлен: {_fmt_dt(s.get('addedAt', ''))}</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def _kb_servers(servers: list[dict[str, Any]], active_id: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in servers:
        active = s["id"] == active_id
        rows.append(
            [
                InlineKeyboardButton(
                    text=("🟢 " if active else "⚪ ") + s["name"],
                    callback_data=f"analyzer:servers:select:{s['id']}",
                ),
                InlineKeyboardButton(text="🗑", callback_data=f"analyzer:servers:rm:{s['id']}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🔍 Проверить все", callback_data="analyzer:servers:verify_all"),
            InlineKeyboardButton(text="➕ Добавить", callback_data="analyzer:servers:add"),
        ]
    )
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="analyzer:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _health_check_all(servers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Parallel /health probe with a 300 ms throttle between launches."""
    out: dict[str, dict[str, Any]] = {}

    async def one(s: dict[str, Any]) -> None:
        t0 = time.perf_counter()
        code, body = await _fetch(s, "GET", "/health", timeout=5.0)
        latency = int((time.perf_counter() - t0) * 1000)
        if code == 200 and isinstance(body, dict):
            out[s["id"]] = {
                "ok": True,
                "latency_ms": latency,
                "version": body.get("version") or body.get("abf_version") or "?",
            }
        elif code == 0:
            out[s["id"]] = {"ok": False, "err": str(body)[:80]}
        else:
            out[s["id"]] = {"ok": False, "err": f"HTTP {code}"}

    tasks: list[asyncio.Task] = []
    for s in servers:
        tasks.append(asyncio.create_task(one(s)))
        await asyncio.sleep(POLL_THROTTLE_MS / 1000)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return out


# ─── Tasks screen ─────────────────────────────────────────────────────────────


def _render_tasks(tasks: dict[str, dict[str, Any]], *, active_only: bool) -> str:
    rows: list[tuple[str, dict[str, Any]]] = []
    for tid, t in tasks.items():
        st = (t.get("status") or "unknown").lower()
        if active_only and st in ("completed", "failed", "cancelled"):
            continue
        if not active_only and st not in ("completed", "failed", "cancelled"):
            continue
        rows.append((tid, t))
    rows.sort(key=lambda kv: kv[1].get("created_at") or 0, reverse=True)
    header = "<b>📊 Активные задачи</b>" if active_only else "<b>🔗 История</b>"
    if not rows:
        return f"{header}\n\nПусто."
    lines = [header, ""]
    for tid, t in rows[:20]:
        progress = t.get("progress")
        prog_s = f" · {int(progress)}%" if isinstance(progress, (int, float)) else ""
        budget = t.get("budget_tokens") or 0
        spent = t.get("tokens_spent") or 0
        budget_s = f" · {spent:,}/{budget:,} tok" if budget else ""
        title = t.get("title") or (t.get("text_preview") or tid)[:48]
        lines.append(
            f"• <b>{_html(title)}</b>\n"
            f"  <code>{_html(tid)}</code> · {_html(t.get('status', '?'))}{prog_s}{budget_s}"
        )
    if len(rows) > 20:
        lines.append("")
        lines.append(f"<i>+{len(rows) - 20} ещё, показаны последние 20.</i>")
    return "\n".join(lines)


def _kb_tasks(tasks: dict[str, dict[str, Any]], *, active_only: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    items = [
        (tid, t)
        for tid, t in tasks.items()
        if ((t.get("status") or "").lower() not in ("completed", "failed", "cancelled")) == active_only
    ]
    items.sort(key=lambda kv: kv[1].get("created_at") or 0, reverse=True)
    for tid, t in items[:10]:
        title = t.get("title") or (t.get("text_preview") or tid)[:24]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {title[:32]}",
                    callback_data=f"analyzer:tasks:detail:{tid}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="analyzer:tasks" if active_only else "analyzer:history"),
            InlineKeyboardButton(text="« Назад", callback_data="analyzer:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_task_detail(tid: str, t: dict[str, Any]) -> str:
    lines = [f"<b>📄 Задача</b> <code>{_html(tid)}</code>", ""]
    lines.append(f"📌 Статус: <b>{_html(t.get('status', '?'))}</b>")
    if "progress" in t:
        lines.append(f"📈 Прогресс: <b>{int(t.get('progress') or 0)}%</b>")
    if t.get("current_step"):
        lines.append(f"🛠 Этап: <i>{_html(t['current_step'])}</i>")
    budget = t.get("budget_tokens") or 0
    spent = t.get("tokens_spent") or 0
    if budget:
        lines.append(f"💰 Бюджет: <b>{spent:,}/{budget:,}</b> tok ({_fmt_budget(budget)})")
    if t.get("priority"):
        lines.append(f"⚡ Приоритет: {PRIORITY_LABEL.get(t['priority'], t['priority'])}")
    if t.get("created_at"):
        try:
            iso = datetime.fromtimestamp(int(t["created_at"])).strftime("%d.%m.%Y %H:%M")
            lines.append(f"🕐 Создана: {iso}")
        except Exception:  # noqa: BLE001
            pass
    if t.get("text_preview"):
        lines.append("")
        lines.append("<b>ТЗ:</b>")
        lines.append(f"<code>{_html(t['text_preview'][:400])}</code>")
    if t.get("last_error"):
        lines.append("")
        lines.append(f"❌ Ошибка: <code>{_html(t['last_error'])}</code>")
    return "\n".join(lines)


def _kb_task_detail(tid: str, t: dict[str, Any]) -> InlineKeyboardMarkup:
    status = (t.get("status") or "").lower()
    rows: list[list[InlineKeyboardButton]] = []
    if status in ("completed",):
        rows.append([InlineKeyboardButton(text="📥 Скачать отчёт", callback_data=f"analyzer:tasks:report:{tid}")])
    if status not in ("completed", "failed", "cancelled"):
        rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"analyzer:tasks:detail:{tid}")])
    rows.append(
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"analyzer:tasks:forget:{tid}"),
            InlineKeyboardButton(text="« Назад", callback_data="analyzer:tasks"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── Settings screen ──────────────────────────────────────────────────────────


def _render_settings(d: dict[str, Any]) -> str:
    return (
        "<b>🔑 Настройки Анализатора PRO</b>\n\n"
        f"💰 Дефолтный бюджет: <b>{_fmt_budget(int(d['budget_tokens']))}</b> "
        f"({int(d['budget_tokens']):,} tok)\n"
        f"⚡ Дефолтный приоритет: {PRIORITY_LABEL.get(d['priority'], d['priority'])}\n\n"
        "Эти значения подставляются в FSM «🚀 Новый анализ» по умолчанию — "
        "можно менять для каждой задачи отдельно."
    )


def _kb_settings(d: dict[str, Any]) -> InlineKeyboardMarkup:
    budget_row = [
        InlineKeyboardButton(
            text=("🟢 " if d["budget_tokens"] == b else "") + _fmt_budget(b),
            callback_data=f"analyzer:settings:budget:{b}",
        )
        for b in BUDGET_CHOICES
    ]
    prio_rows: list[list[InlineKeyboardButton]] = []
    cur = list(PRIORITY_CHOICES)
    for i in range(0, len(cur), 3):
        prio_rows.append(
            [
                InlineKeyboardButton(
                    text=("🟢 " if p == d["priority"] else "") + PRIORITY_LABEL[p],
                    callback_data=f"analyzer:settings:prio:{p}",
                )
                for p in cur[i : i + 3]
            ]
        )
    rows = [budget_row, *prio_rows, [InlineKeyboardButton(text="« Назад", callback_data="analyzer:menu")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── New-task FSM screens ─────────────────────────────────────────────────────


async def _fsm_set_data(state: FSMContext, **patch: Any) -> dict[str, Any]:
    data = await state.get_data()
    data.update(patch)
    await state.set_data(data)
    return data


def _kb_budget(selected: int | None) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=("🟢 " if selected == b else "") + _fmt_budget(b),
            callback_data=f"analyzer:fsm:budget:{b}",
        )
        for b in BUDGET_CHOICES
    ]
    rows = [row, [InlineKeyboardButton(text="✖ Отмена", callback_data="analyzer:fsm:cancel")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_priority(selected: str | None) -> InlineKeyboardMarkup:
    cur = list(PRIORITY_CHOICES)
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(cur), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    text=("🟢 " if p == selected else "") + PRIORITY_LABEL[p],
                    callback_data=f"analyzer:fsm:prio:{p}",
                )
                for p in cur[i : i + 3]
            ]
        )
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="analyzer:fsm:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Запустить", callback_data="analyzer:fsm:start"),
                InlineKeyboardButton(text="✖ Отмена", callback_data="analyzer:fsm:cancel"),
            ]
        ]
    )


def _render_fsm_summary(data: dict[str, Any]) -> str:
    prio = data.get("priority", "normal")
    budget = int(data.get("budget_tokens") or 100_000)
    text_len = len(data.get("text") or "")
    file_name = data.get("file_name")
    file_size = data.get("file_size")
    lines = ["<b>🚀 Новый анализ — подтверждение</b>", ""]
    if data.get("text"):
        preview = (data["text"] or "").strip().splitlines()[0][:120]
        lines.append(f"📝 Текст: <i>{_html(preview)}</i> ({text_len} chars)")
    if file_name:
        lines.append(f"📎 Файл: <code>{_html(file_name)}</code> ({file_size} bytes)")
    lines.append(f"💰 Бюджет: <b>{_fmt_budget(budget)}</b>")
    lines.append(f"⚡ Приоритет: {PRIORITY_LABEL.get(prio, prio)}")
    svc = _active_server()
    if svc:
        lines.append(f"🌐 Сервер: <b>{_html(svc['name'])}</b>")
    return "\n".join(lines)


# ─── Polling worker ───────────────────────────────────────────────────────────


def _stop_poller(chat_id: int, task_id: str) -> None:
    key = (chat_id, task_id)
    task = _POLLERS.pop(key, None)
    if task and not task.done():
        task.cancel()


def _start_poller(bot, chat_id: int, task_id: str, status_msg_id: int) -> None:
    key = (chat_id, task_id)
    if key in _POLLERS and not _POLLERS[key].done():
        return
    _POLLERS[key] = asyncio.create_task(_poll_loop(bot, chat_id, task_id, status_msg_id))


async def _poll_loop(bot, chat_id: int, task_id: str, status_msg_id: int) -> None:
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            t = _tasks().get(task_id)
            if not t:
                return
            svc = _get_server(t.get("server_id") or "")
            if not svc:
                return
            code, body = await _fetch(svc, "GET", "/user/status", params={"task_id": task_id})
            if code != 200 or not isinstance(body, dict):
                continue
            status = (body.get("status") or "").lower() or t.get("status", "?")
            patch = {
                "status": status,
                "progress": body.get("progress"),
                "current_step": body.get("current_step") or body.get("stage"),
                "tokens_spent": body.get("tokens_spent") or body.get("spent_tokens"),
                "last_error": body.get("error"),
            }
            t = _save_task(task_id, patch)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=_render_task_detail(task_id, t),
                    reply_markup=_kb_task_detail(task_id, t),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:  # noqa: BLE001
                pass
            if status in ("completed", "failed", "cancelled"):
                if status == "completed":
                    await _deliver_report(bot, chat_id, svc, task_id, t)
                return
    except asyncio.CancelledError:
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("analyzer poller crashed for %s: %s", task_id, e)


async def _deliver_report(bot, chat_id: int, svc: dict[str, Any], task_id: str, t: dict[str, Any]) -> None:
    code, body = await _fetch_with_retry(svc, "GET", f"/sessions/{task_id}/result")
    if code != 200:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ Задача <code>{_html(task_id)}</code> завершилась, "
                f"но скачать отчёт не вышло (HTTP {code}). Можно повторить "
                f"через <b>📊 Статус задач → 📥 Скачать отчёт</b>."
            ),
            parse_mode="HTML",
        )
        return
    if isinstance(body, dict):
        report_md = body.get("report_md") or body.get("report") or ""
        manifest = body.get("manifest") or {}
        summary = body.get("summary") or ""
    else:
        report_md = str(body)
        manifest = {}
        summary = ""
    if not report_md:
        report_md = json.dumps(body, ensure_ascii=False, indent=2)
    md_bytes = report_md.encode("utf-8")
    file = BufferedInputFile(md_bytes, filename=f"analyzer_{task_id}.md")
    caption = (
        f"✅ <b>Задача завершена</b>\n<code>{_html(task_id)}</code>\n\n"
        + (f"<i>{_html(summary[:600])}</i>\n\n" if summary else "")
        + f"📊 Tokens spent: <b>{int(t.get('tokens_spent') or manifest.get('tokens_spent') or 0):,}</b>"
    )
    await bot.send_document(chat_id=chat_id, document=file, caption=caption, parse_mode="HTML")
    if manifest:
        mf = BufferedInputFile(
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            filename=f"analyzer_{task_id}_manifest.json",
        )
        await bot.send_document(chat_id=chat_id, document=mf)


# ─── Router ───────────────────────────────────────────────────────────────────


def build_analyzer_router() -> Router:
    router = Router(name="analyzer")

    # Entry / nav -------------------------------------------------------------

    @router.callback_query(F.data == "main:analyzer")
    async def from_main_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await show_screen(query)

    @router.callback_query(F.data == "analyzer:menu")
    async def menu_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await show_screen(query)

    @router.callback_query(F.data == "analyzer:help")
    async def help_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await _send_or_edit(
            query,
            HELP_TEXT,
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="analyzer:menu")]]
            ),
        )

    # Servers ----------------------------------------------------------------

    @router.callback_query(F.data == "analyzer:servers")
    async def servers_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        servers = _servers()
        await _send_or_edit(query, _render_servers(servers, _active_id()), _kb_servers(servers, _active_id()))

    @router.callback_query(F.data.startswith("analyzer:servers:select:"))
    async def server_select_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        svc_id = query.data.rsplit(":", 1)[-1]
        if not _get_server(svc_id):
            await query.answer("Сервер не найден.", show_alert=True)
            return
        _set_active(svc_id)
        await query.answer("Активен ✓")
        servers = _servers()
        await _send_or_edit(query, _render_servers(servers, _active_id()), _kb_servers(servers, _active_id()))

    @router.callback_query(F.data.startswith("analyzer:servers:rm:"))
    async def server_rm_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        svc_id = query.data.rsplit(":", 1)[-1]
        ok = _remove_server(svc_id)
        await query.answer("Удалено ✓" if ok else "Не найдено")
        servers = _servers()
        await _send_or_edit(query, _render_servers(servers, _active_id()), _kb_servers(servers, _active_id()))

    @router.callback_query(F.data == "analyzer:servers:verify_all")
    async def server_verify_all_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        servers = _servers()
        if not servers:
            await query.answer("Нет серверов", show_alert=True)
            return
        await query.answer("Проверяю…")
        health = await _health_check_all(servers)
        ok = sum(1 for h in health.values() if h.get("ok"))
        bar = _trustbar(ok / max(1, len(servers)))
        text = _render_servers(servers, _active_id(), health) + f"\n\n<b>TrustScore: {bar}</b> ({ok}/{len(servers)} online)"
        await _send_or_edit(query, text, _kb_servers(servers, _active_id()))

    @router.callback_query(F.data == "analyzer:servers:add")
    async def server_add_start_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(AnalyzerStates.awaiting_add_url)
        await _send_or_edit(
            query,
            (
                "<b>➕ Добавить сервер Analyzer-Boss</b>\n\n"
                "Пришли URL инстанса (например <code>https://analyzer-boss-prod.onrender.com</code>)."
            ),
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="analyzer:servers")]]
            ),
        )

    @router.message(AnalyzerStates.awaiting_add_url)
    async def server_add_url_(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        url = (message.text or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await message.answer("URL должен начинаться с http(s)://. Попробуй ещё раз.")
            return
        await _fsm_set_data(state, pending_url=url.rstrip("/"))
        await state.set_state(AnalyzerStates.awaiting_add_token)
        await message.answer(
            "Теперь пришли <b>Bearer-токен</b> Analyzer-Boss (значение "
            "<code>ANALYZER_BOSS_TOKEN</code> на сервере).",
            parse_mode="HTML",
        )

    @router.message(AnalyzerStates.awaiting_add_token)
    async def server_add_token_(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        token = (message.text or "").strip()
        if not token:
            await message.answer("Пустой токен. Пришли ещё раз.")
            return
        data = await state.get_data()
        url = data.get("pending_url") or ""
        from urllib.parse import urlparse

        name = urlparse(url).netloc or url
        added, svc = _add_server(url, name, token)
        await state.clear()
        await message.answer(
            f"{'✅ Добавлен' if added else '✅ Обновлён'} сервер "
            f"<b>{_html(svc['name'])}</b>.",
            parse_mode="HTML",
        )
        await show_screen(message)

    # Tasks / History --------------------------------------------------------

    @router.callback_query(F.data == "analyzer:tasks")
    async def tasks_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        tasks = _tasks()
        await _send_or_edit(query, _render_tasks(tasks, active_only=True), _kb_tasks(tasks, active_only=True))

    @router.callback_query(F.data == "analyzer:history")
    async def history_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        tasks = _tasks()
        await _send_or_edit(query, _render_tasks(tasks, active_only=False), _kb_tasks(tasks, active_only=False))

    @router.callback_query(F.data.startswith("analyzer:tasks:detail:"))
    async def task_detail_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        tid = query.data.rsplit(":", 1)[-1]
        t = _tasks().get(tid)
        if not t:
            await query.answer("Задача не найдена", show_alert=True)
            return
        # Re-sync once when user opens the detail screen.
        svc = _get_server(t.get("server_id") or "")
        if svc and (t.get("status") or "").lower() not in ("completed", "failed", "cancelled"):
            code, body = await _fetch(svc, "GET", "/user/status", params={"task_id": tid})
            if code == 200 and isinstance(body, dict):
                t = _save_task(
                    tid,
                    {
                        "status": (body.get("status") or t.get("status") or "?").lower(),
                        "progress": body.get("progress"),
                        "current_step": body.get("current_step") or body.get("stage"),
                        "tokens_spent": body.get("tokens_spent") or body.get("spent_tokens"),
                        "last_error": body.get("error"),
                    },
                )
        await _send_or_edit(query, _render_task_detail(tid, t), _kb_task_detail(tid, t))

    @router.callback_query(F.data.startswith("analyzer:tasks:report:"))
    async def task_report_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        tid = query.data.rsplit(":", 1)[-1]
        t = _tasks().get(tid)
        if not t:
            await query.answer("Задача не найдена", show_alert=True)
            return
        svc = _get_server(t.get("server_id") or "")
        if not svc:
            await query.answer("Сервер удалён", show_alert=True)
            return
        await query.answer("Скачиваю…")
        await _deliver_report(query.bot, query.message.chat.id, svc, tid, t)

    @router.callback_query(F.data.startswith("analyzer:tasks:forget:"))
    async def task_forget_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        tid = query.data.rsplit(":", 1)[-1]
        _stop_poller(query.message.chat.id, tid)
        _delete_task(tid)
        await query.answer("Удалено ✓")
        tasks = _tasks()
        await _send_or_edit(query, _render_tasks(tasks, active_only=True), _kb_tasks(tasks, active_only=True))

    # Settings ---------------------------------------------------------------

    @router.callback_query(F.data == "analyzer:settings")
    async def settings_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        d = _defaults()
        await _send_or_edit(query, _render_settings(d), _kb_settings(d))

    @router.callback_query(F.data.startswith("analyzer:settings:budget:"))
    async def settings_budget_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        try:
            val = int(query.data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad value", show_alert=True)
            return
        d = _defaults()
        d["budget_tokens"] = val
        _save_defaults(d)
        await query.answer("Сохранено ✓")
        await _send_or_edit(query, _render_settings(d), _kb_settings(d))

    @router.callback_query(F.data.startswith("analyzer:settings:prio:"))
    async def settings_prio_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        p = query.data.rsplit(":", 1)[-1]
        if p not in PRIORITY_CHOICES:
            await query.answer("Bad value", show_alert=True)
            return
        d = _defaults()
        d["priority"] = p
        _save_defaults(d)
        await query.answer("Сохранено ✓")
        await _send_or_edit(query, _render_settings(d), _kb_settings(d))

    # New-task FSM -----------------------------------------------------------

    @router.callback_query(F.data == "analyzer:new_task")
    async def new_task_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        if not _active_server():
            await _send_or_edit(
                query,
                (
                    "<b>🚀 Новый анализ</b>\n\n"
                    "Сначала добавь хотя бы один Analyzer-Boss "
                    "и выбери активный сервер."
                ),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="analyzer:servers:add")],
                        [InlineKeyboardButton(text="« Назад", callback_data="analyzer:menu")],
                    ]
                ),
            )
            return
        d = _defaults()
        await state.set_state(AnalyzerStates.awaiting_task_text)
        await state.set_data(
            {
                "budget_tokens": d["budget_tokens"],
                "priority": d["priority"],
            }
        )
        await _send_or_edit(
            query,
            (
                "<b>🚀 Новый анализ — шаг 1/5</b>\n\n"
                "Пришли <b>текст ТЗ</b> для анализатора.\n\n"
                "Если ТЗ лежит в файле — пришли <code>/skip</code> и на "
                "следующем шаге приложи <code>.txt</code> / <code>.md</code> / "
                "<code>.pdf</code> до 200 KB."
            ),
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="analyzer:fsm:cancel")]]
            ),
        )

    @router.message(AnalyzerStates.awaiting_task_text)
    async def fsm_text_(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        text = (message.text or "").strip()
        if text == "/skip":
            await _fsm_set_data(state, text="")
        else:
            if not text:
                await message.answer("Пусто. Пришли текст ТЗ или /skip.")
                return
            await _fsm_set_data(state, text=text, title=text.splitlines()[0][:64])
        await state.set_state(AnalyzerStates.awaiting_optional_file)
        await message.answer(
            (
                "<b>Шаг 2/5</b> — файл (опционально).\n\n"
                "Пришли <code>.txt</code> / <code>.md</code> / <code>.pdf</code> "
                "до 200 KB, или <code>/skip</code> чтобы пропустить."
            ),
            parse_mode="HTML",
        )

    @router.message(AnalyzerStates.awaiting_optional_file)
    async def fsm_file_(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        if message.text and message.text.strip() == "/skip":
            await _fsm_set_data(state, file_blob=None, file_name=None, file_size=0)
        elif message.document:
            doc = message.document
            if doc.file_size and doc.file_size > MAX_FILE_BYTES:
                await message.answer(
                    f"Файл больше 200 KB ({doc.file_size} bytes). "
                    "Сократи или приложи .md."
                )
                return
            buf = io.BytesIO()
            await message.bot.download(doc, destination=buf)
            blob = buf.getvalue()
            if len(blob) > MAX_FILE_BYTES:
                await message.answer("Файл больше 200 KB. Сократи или /skip.")
                return
            await _fsm_set_data(
                state,
                file_blob=blob,
                file_name=doc.file_name or "input.bin",
                file_size=len(blob),
            )
        else:
            await message.answer("Пришли файл или /skip.")
            return
        data = await state.get_data()
        if not data.get("text") and not data.get("file_blob"):
            await message.answer("Нужен либо текст ТЗ, либо файл. Пришли файл или вернись /cancel.")
            return
        await state.set_state(AnalyzerStates.awaiting_budget)
        await message.answer(
            "<b>Шаг 3/5</b> — бюджет токенов.",
            parse_mode="HTML",
            reply_markup=_kb_budget(data.get("budget_tokens")),
        )

    @router.callback_query(F.data.startswith("analyzer:fsm:budget:"))
    async def fsm_budget_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        try:
            val = int(query.data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad value", show_alert=True)
            return
        data = await _fsm_set_data(state, budget_tokens=val)
        await state.set_state(AnalyzerStates.awaiting_priority)
        await _send_or_edit(
            query,
            f"<b>Шаг 4/5</b> — приоритет.\n\nВыбранный бюджет: <b>{_fmt_budget(val)}</b>",
            _kb_priority(data.get("priority")),
        )

    @router.callback_query(F.data.startswith("analyzer:fsm:prio:"))
    async def fsm_prio_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        p = query.data.rsplit(":", 1)[-1]
        if p not in PRIORITY_CHOICES:
            await query.answer("Bad value", show_alert=True)
            return
        data = await _fsm_set_data(state, priority=p)
        await state.set_state(AnalyzerStates.confirming)
        await _send_or_edit(query, _render_fsm_summary(data), _kb_confirm())

    @router.callback_query(F.data == "analyzer:fsm:cancel")
    async def fsm_cancel_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer("Отменено")
        await show_screen(query)

    @router.callback_query(F.data == "analyzer:fsm:start")
    async def fsm_start_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        data = await state.get_data()
        svc = _active_server()
        if not svc:
            await query.answer("Активный сервер не выбран", show_alert=True)
            return
        await query.answer("Отправляю в Analyzer-Boss…")
        # POST /user/analysis-request — multipart if file, иначе JSON.
        budget = int(data.get("budget_tokens") or 100_000)
        priority = data.get("priority") or "normal"
        text = data.get("text") or ""
        file_blob = data.get("file_blob")
        file_name = data.get("file_name")
        # tools_ext: merge selected tools / routing-map / auto-pick (server
        # reads them if supported; harmless extra fields otherwise).
        from .tools_ext import payload_extra as _tx_extra
        _extra = _tx_extra()
        if file_blob:
            files = {"file": (file_name or "input.bin", file_blob, "application/octet-stream")}
            form = {
                "text": text,
                "budget_tokens": str(budget),
                "priority": priority,
            }
            if _extra.get("tools"):
                form["tools"] = json.dumps(_extra["tools"])
            if _extra.get("routing"):
                form["routing"] = _extra["routing"]
            if _extra.get("auto_pick"):
                form["auto_pick"] = "1"
            code, body = await _fetch_with_retry(
                svc, "POST", "/user/analysis-request", files=files, data=form
            )
        else:
            code, body = await _fetch_with_retry(
                svc,
                "POST",
                "/user/analysis-request",
                json_body={"text": text, "budget_tokens": budget, "priority": priority,
                           **_extra},
            )
        if code != 200 or not isinstance(body, dict) or not body.get("task_id"):
            err = body if isinstance(body, str) else json.dumps(body)[:200]
            await _send_or_edit(
                query,
                f"❌ Не удалось создать задачу.\nHTTP {code}\n<code>{_html(err)}</code>",
                InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="analyzer:menu")]]
                ),
            )
            await state.clear()
            return
        task_id = str(body["task_id"])
        _save_task(
            task_id,
            {
                "server_id": svc["id"],
                "created_at": int(time.time()),
                "status": (body.get("status") or "queued").lower(),
                "budget_tokens": budget,
                "priority": priority,
                "text_preview": (text or "")[:400],
                "title": data.get("title") or f"task {task_id[:8]}",
            },
        )
        await state.clear()
        # send a fresh status message — keep it editable by poller
        sent = await query.message.answer(
            _render_task_detail(task_id, _tasks()[task_id]),
            reply_markup=_kb_task_detail(task_id, _tasks()[task_id]),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        _start_poller(query.bot, query.message.chat.id, task_id, sent.message_id)
        await show_screen(query)

    # tools_ext hook — register the isolated tool-picker/decision-map layer.
    from .tools_ext import register as _tx_register
    _tx_register(router)

    return router
