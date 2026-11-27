# Debates control panel + start_problem_debate hook for regen integration.
# Co-authored with CoCo
"""Debates screens — пульт сервера ``debate-ii`` прямо внутри Lilush.

Главное меню (inline):

    🚪 Двери          🌐 Сервисы
    🆕 Создать дверь  ❓ Помощь
    ➕ Добавить сервер
    « Назад

* Сервера хранятся под ``addons.debates.servers`` как список
  ``{id, url, name, token, addedAt}``.
* Эндпоинты совпадают с ``debate-ii`` (см. ``services/debate-ii/app.py``,
  ``debate.py``): ``/healthz``, ``/doors``, ``/door/{id}/state``,
  ``/door/{id}/events``, ``/door/{id}/session/start``,
  ``/door/{id}/session/end`` и т.д.
* Каждый запрос идёт с ``?token=<DEBATE_TOKEN>`` сервера.

Все экраны — owner-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import state as addon_state

logger = logging.getLogger(__name__)

API_TIMEOUT = 10.0
MAX_TZ_BYTES = 200 * 1024
EVENTS_TAIL = 30


# ─── State storage ────────────────────────────────────────────────────────────


class DebatesStates(StatesGroup):
    awaiting_add_url = State()
    awaiting_add_token = State()
    awaiting_session = State()  # waiting for /begin payload (text or doc)


def _servers() -> list[dict[str, Any]]:
    raw = addon_state.get("debates", "servers", []) or []
    out: list[dict[str, Any]] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip().rstrip("/")
        if not url:
            continue
        out.append(
            {
                "id": str(s.get("id") or f"dbt_{int(time.time() * 1000)}"),
                "url": url,
                "name": s.get("name") or url,
                "token": (s.get("token") or "").strip(),
                "addedAt": s.get("addedAt") or "",
            }
        )
    return out


def _save_servers(servers: list[dict[str, Any]]) -> None:
    addon_state.set_("debates", "servers", servers)


def _add_server(url: str, name: str, token: str) -> tuple[bool, dict[str, Any]]:
    url = url.strip().rstrip("/")
    servers = _servers()
    existing = next((s for s in servers if s["url"] == url), None)
    if existing:
        # keep existing id, but refresh token/name when re-added
        existing["name"] = name.strip() or existing["name"]
        existing["token"] = token.strip() or existing.get("token", "")
        _save_servers(servers)
        return False, existing
    svc = {
        "id": f"dbt_{int(time.time() * 1000)}",
        "url": url,
        "name": name.strip() or url,
        "token": token.strip(),
        "addedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    servers.append(svc)
    _save_servers(servers)
    return True, svc


def _remove_server(svc_id: str) -> bool:
    servers = _servers()
    new = [s for s in servers if s["id"] != svc_id]
    if len(new) == len(servers):
        return False
    _save_servers(new)
    return True


def _get_server(svc_id: str) -> dict[str, Any] | None:
    return next((s for s in _servers() if s["id"] == svc_id), None)


# ─── HTML helpers ─────────────────────────────────────────────────────────────


def _html(text: Any) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M")
    except Exception:  # noqa: BLE001
        return iso or "—"


# ─── Owner gate ───────────────────────────────────────────────────────────────


async def _is_owner_query(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]
    return _is_owner(query.from_user.id if query.from_user else 0)


async def _is_owner_msg(message: Message) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]
    return _is_owner(message.from_user.id if message.from_user else 0)


# ─── HTTP ─────────────────────────────────────────────────────────────────────


async def _fetch(svc: dict[str, Any], method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
    url = svc["url"].rstrip("/") + (path if path.startswith("/") else "/" + path)
    params = dict(kwargs.pop("params", {}) or {})
    if svc.get("token"):
        params.setdefault("token", svc["token"])
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.request(method.upper(), url, params=params, **kwargs)
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            return resp.status_code, body
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


async def _check_health(svc: dict[str, Any]) -> dict[str, Any]:
    status, body = await _fetch(svc, "GET", "/healthz")
    if status == 200:
        return {"online": True, "health": body if isinstance(body, dict) else {"ok": True}}
    return {"online": False, "health": None, "error": str(body) if status == 0 else f"HTTP {status}"}


async def _all_with_status() -> list[dict[str, Any]]:
    servers = _servers()
    results = await asyncio.gather(*(_check_health(s) for s in servers), return_exceptions=True)
    out: list[dict[str, Any]] = []
    for svc, res in zip(servers, results):
        if isinstance(res, Exception):
            out.append({**svc, "online": False, "health": None, "error": str(res)})
        else:
            out.append({**svc, **res})
    return out


async def _first_online() -> dict[str, Any] | None:
    for s in await _all_with_status():
        if s.get("online"):
            return s
    return None


async def _fetch_doors(svc: dict[str, Any]) -> dict[str, Any] | None:
    status, body = await _fetch(svc, "GET", "/doors")
    if status == 200 and isinstance(body, dict):
        return body
    return None


# ─── Keyboards ────────────────────────────────────────────────────────────────


def _kb_home(has_servers: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_servers:
        rows.extend(
            [
                [
                    InlineKeyboardButton(text="🚪 Двери", callback_data="debates:doors"),
                    InlineKeyboardButton(text="🌐 Сервисы", callback_data="debates:services"),
                ],
                [
                    InlineKeyboardButton(text="🆕 Создать дверь", callback_data="debates:newdoor"),
                    InlineKeyboardButton(text="❓ Помощь", callback_data="debates:help"),
                ],
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="❓ Помощь", callback_data="debates:help")])
    # Viktor style дебат (v4) — отдельный пульт
    rows.append([InlineKeyboardButton(text="🎯 Дебаты (Viktor style)",
                                       callback_data="vsd:open")])
    # TZ management
    rows.append([
        InlineKeyboardButton(text="📄 ТЗ лоу", callback_data="tz:upload:low"),
        InlineKeyboardButton(text="📄 Супер ТЗ", callback_data="tz:upload:super"),
    ])
    rows.append([InlineKeyboardButton(text="📂 Хранилище ТЗ",
                                       callback_data="tz:storage")])
    rows.append([InlineKeyboardButton(text="🚪 Двери дебат v3", callback_data="dbd:home")])
    rows.append([InlineKeyboardButton(text="➕ Добавить сервер", callback_data="debates:add")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="main:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_back_home(extra: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="debates:refresh"),
            InlineKeyboardButton(text="« Назад", callback_data="debates:open"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_doors_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="debates:doors"),
                InlineKeyboardButton(text="« Меню", callback_data="debates:open"),
            ]
        ]
    )


# ─── Renderers ────────────────────────────────────────────────────────────────


def _render_home(servers: list[dict[str, Any]]) -> str:
    if not servers:
        return (
            "<b>🎤 Дебаты</b>\n\n"
            "Пульт сервера <i>debate-ii</i>: многодверные ИИ-дебаты с эстафетой "
            "и лентой событий.\n\n"
            "<b>Серверы пока не подключены.</b> Подними debate-ii "
            "(<code>services/debate-ii/</code>, <code>python run.py</code>) и "
            "нажми <b>➕ Добавить сервер</b>."
        )
    return (
        "<b>🎤 Дебаты</b>\n\n"
        "Пульт сервера <i>debate-ii</i>. Сам бот в дебатах не участвует — он "
        "только заводит двери, раздаёт ссылки ИИ-дебатёрам и показывает живые "
        "события.\n\n"
        "<b>Что умею:</b>\n"
        "🚪 Список дверей со всех серверов\n"
        "🆕 Создание новой двери (➕ → ссылка)\n"
        "🎙 Запуск дебата (число дебатёров + ТЗ)\n"
        "🔔 Лента событий двери\n"
        "🌐 Управление серверами\n\n"
        f"Подключено серверов: <b>{len(servers)}</b>\n"
        "Выбери действие ↓"
    )


def _door_short(svc: dict[str, Any], d: dict[str, Any]) -> str:
    did = d.get("door_id")
    title = d.get("title") or f"Door {did}"
    conn = d.get("connections", 0)
    mx = d.get("max_slots", 5)
    active = "🟢" if d.get("session_active") else "💤"
    return f"{active} #{did} {title} · {conn}/{mx}"


async def _render_doors_all() -> tuple[str, list[list[InlineKeyboardButton]]]:
    services = await _all_with_status()
    online = [s for s in services if s.get("online")]
    if not services:
        return (
            "<b>🚪 Двери</b>\n\nНет подключённых серверов. Нажми <b>➕ Добавить сервер</b>.",
            [],
        )
    if not online:
        return (
            "<b>🚪 Двери</b>\n\nВсе серверы недоступны. Проверь, что debate-ii поднят.",
            [],
        )
    lines = ["<b>🚪 Двери</b>", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for svc in online:
        data = await _fetch_doors(svc)
        if not data:
            lines.append(f"⚠️ <b>{_html(svc['name'])}</b> — не отвечает")
            continue
        doors = data.get("doors") or []
        total = data.get("total_doors", len(doors))
        cap = data.get("max_doors", 20)
        lines.append(f"<b>{_html(svc['name'])}</b> — {total}/{cap}")
        if not doors:
            lines.append("  <i>пусто</i>")
        for d in doors:
            did = d.get("door_id")
            lines.append(f"  {_door_short(svc, d)}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=_door_short(svc, d),
                        callback_data=f"debates:door:view:{svc['id']}:{did}",
                    )
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🆕 Создать в {svc['name']}",
                    callback_data=f"debates:door:new:{svc['id']}",
                )
            ]
        )
        lines.append("")
    return "\n".join(lines).rstrip(), buttons


def _render_door_view(svc: dict[str, Any], did: int, st: dict[str, Any] | None) -> str:
    if not st:
        return f"<b>🚪 #{did}</b>\n\n❌ Дверь недоступна на <i>{_html(svc['name'])}</i>."
    title = st.get("door_title") or f"Door {did}"
    link = f"{svc['url']}/door/{did}"
    lines = [
        f"<b>🚪 #{did} · {_html(title)}</b>",
        f"<i>{_html(svc['name'])}</i>",
        f"🔗 <code>{_html(link)}</code>",
    ]
    if not st.get("session_active"):
        lines.append(
            "\n💤 Сессии нет. Нажми <b>🎙 Запустить сессию</b> и пришли тему/ТЗ."
        )
    else:
        req = st.get("required_debaters")
        slots = st.get("slots") or []
        occ = [s for s in slots if s.get("occupied")]
        started = st.get("started")
        gate = "🟢 идут дебаты" if started else f"⏳ ждём набора {len(occ)}/{req}"
        lines.append(f"\n🎙 <b>{_html(st.get('title') or '')}</b> · {gate}")
        lines.append(f"\n👥 <b>Участники ({len(occ)}/{req}):</b>")
        for s in occ:
            em = {
                "waiting": "⏳",
                "thinking": "🤔",
                "spoke": "💬",
                "offline": "🔴",
            }.get(s.get("status"), "•")
            lines.append(f"  {em} <b>{_html(s.get('name'))}</b> — {_html(s.get('status'))}")
        agenda = st.get("agenda") or []
        cur = st.get("current", 0)
        lines.append(f"\n📋 <b>Пункты ({cur}/{len(agenda)}):</b>")
        for i, p in enumerate(agenda):
            mark = "✅" if p.get("status") == "decided" else ("👉" if i == cur else "•")
            who = ""
            if p.get("status") != "decided":
                if p.get("turn"):
                    who = f" · ход: {_html(p['turn'])}"
                elif p.get("assignee"):
                    who = f" · ⚡{_html(p['assignee'])}"
            lines.append(f"  {mark} {i + 1}. {_html((p.get('text') or '')[:60])}{who}")
            if p.get("decision"):
                lines.append(f"      → <i>{_html(p['decision'][:140])}</i>")
    return "\n".join(lines)


def _kb_door_view(svc_id: str, did: int, active: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🔔 События", callback_data=f"debates:door:events:{svc_id}:{did}"),
            InlineKeyboardButton(text="🔄", callback_data=f"debates:door:view:{svc_id}:{did}"),
        ],
    ]
    if active:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🏁 Завершить сессию",
                    callback_data=f"debates:door:end:{svc_id}:{did}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎙 Запустить сессию",
                    callback_data=f"debates:door:start:{svc_id}:{did}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✕ Удалить дверь",
                callback_data=f"debates:door:del:{svc_id}:{did}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="« Двери", callback_data="debates:doors")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_services(services: list[dict[str, Any]]) -> tuple[str, list[list[InlineKeyboardButton]]]:
    lines = ["<b>🌐 Подключённые серверы</b>", ""]
    rm_buttons: list[list[InlineKeyboardButton]] = []
    if not services:
        lines.append("Пусто. Нажми <b>➕ Добавить сервер</b>.")
    for svc in services:
        dot = "🟢" if svc.get("online") else "🔴"
        lines.append(f"{dot} <b>{_html(svc['name'])}</b>")
        lines.append(f"<code>{_html(svc['url'])}</code>")
        lines.append(f"ID: <code>{_html(svc['id'])}</code>")
        if svc.get("token"):
            mask = svc["token"][:4] + "…" + svc["token"][-3:] if len(svc["token"]) > 10 else "***"
            lines.append(f"Token: <code>{_html(mask)}</code>")
        else:
            lines.append("⚠️ Token не задан — API закрыт")
        if svc.get("online"):
            h = svc.get("health") or {}
            if isinstance(h, dict) and h.get("doors") is not None:
                lines.append(f"✅ Онлайн | дверей {h.get('doors')}")
            else:
                lines.append("✅ Онлайн")
        else:
            err = svc.get("error") or "Недоступен"
            lines.append(f"❌ {_html(err)}")
        if svc.get("addedAt"):
            lines.append(f"Добавлен: {_fmt_dt(svc['addedAt'])}")
        lines.append("")
        rm_buttons.append(
            [InlineKeyboardButton(text=f"🗑 Удалить {svc['name']}", callback_data=f"debates:rm:{svc['id']}")]
        )
    lines.append("━━━━━━━━━━━━━━━━━")
    online = sum(1 for s in services if s.get("online"))
    lines.append(f"Всего: {len(services)} серверов | Онлайн: {online}")
    return "\n".join(lines), rm_buttons


_HELP_TEXT = (
    "<b>📖 Справка — Дебаты</b>\n\n"
    "<b>Что это:</b> пульт сервера <i>debate-ii</i>. Сервер держит до 20 "
    "независимых дверей (комнат), в каждой — до 5 ИИ-дебатёров. Бот не "
    "участвует в дебатах: он создаёт двери, раздаёт ссылки и подсматривает "
    "за лентой событий.\n\n"
    "<b>Кнопки:</b>\n"
    "🚪 <b>Двери</b> — список со всех онлайн-серверов. Тык в дверь → "
    "детали + лента событий.\n"
    "🌐 <b>Сервисы</b> — управление подключёнными debate-ii.\n"
    "🆕 <b>Создать дверь</b> — на выбранном сервере.\n"
    "➕ <b>Добавить сервер</b> — URL + DEBATE_TOKEN.\n\n"
    "<b>Запуск дебата:</b>\n"
    "1. Открой дверь → <b>🎙 Запустить сессию</b>.\n"
    "2. Пришли ТЗ — либо текстом, либо <code>.txt</code>/<code>.md</code> "
    "файлом, в первой строке: <code>&lt;кол-во&gt; &lt;тема&gt;</code>, "
    "далее — содержимое ТЗ.\n"
    "3. Раздай <code>{server}/door/{id}</code> ИИ-дебатёрам. Когда "
    "наберётся нужное число — дебаты автостартуют.\n\n"
    "<b>Эстафета:</b> ИИ ходят по очереди по пункту повестки, свободные "
    "берут следующий пункт параллельно. Все события (➡️ передал ход, "
    "⚡ взял пункт параллельно, ✅ пункт решён) видны в <b>🔔 Событиях</b>.\n\n"
    "<b>Поднять сервер:</b>\n"
    "<code>cd services/debate-ii\n"
    "pip install -r requirements.txt\n"
    "export DEBATE_TOKEN=secret\n"
    "python run.py     # :8080</code>\n\n"
    "Потом сюда → <b>➕ Добавить сервер</b> → URL → DEBATE_TOKEN."
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
    """Главный экран дебатов."""
    servers = _servers()
    body = _render_home(servers)
    kb = _kb_home(bool(servers))
    if isinstance(message_or_query, CallbackQuery):
        await _send_or_edit(message_or_query, body, kb)
    else:
        await message_or_query.answer(body, reply_markup=kb, disable_web_page_preview=True)


# ─── Router ───────────────────────────────────────────────────────────────────


def build_debates_router() -> Router:
    router = Router(name="debates")

    # Entry / nav -------------------------------------------------------------

    @router.callback_query(F.data == "main:debates")
    async def from_main_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await show_screen(query)

    @router.callback_query(F.data == "debates:open")
    async def open_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await show_screen(query)

    @router.callback_query(F.data == "debates:refresh")
    async def refresh_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await show_screen(query)

    # Help --------------------------------------------------------------------

    @router.callback_query(F.data == "debates:help")
    async def help_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await _send_or_edit(query, _HELP_TEXT, _kb_back_home())

    # Doors list --------------------------------------------------------------

    @router.callback_query(F.data == "debates:doors")
    async def doors_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        if query.message is not None:
            try:
                await query.message.edit_text(
                    "⏳ Собираю двери со всех серверов…",
                    reply_markup=_kb_doors_back(),
                )
            except Exception:  # noqa: BLE001
                pass
        body, btns = await _render_doors_all()
        await _send_or_edit(query, body, _kb_back_home(extra=btns))

    # Create door (global) -----------------------------------------------------

    @router.callback_query(F.data == "debates:newdoor")
    async def newdoor_pick_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        services = await _all_with_status()
        online = [s for s in services if s.get("online")]
        if not online:
            await _send_or_edit(
                query,
                "<b>🆕 Создать дверь</b>\n\nНет онлайн-серверов. Добавь через ➕.",
                _kb_back_home(),
            )
            return
        btns = [
            [
                InlineKeyboardButton(
                    text=f"🚪 в {svc['name']}",
                    callback_data=f"debates:door:new:{svc['id']}",
                )
            ]
            for svc in online
        ]
        await _send_or_edit(
            query,
            "<b>🆕 Создать дверь</b>\n\nВыбери сервер:",
            _kb_back_home(extra=btns),
        )

    @router.callback_query(F.data.startswith("debates:door:new:"))
    async def newdoor_do_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        svc_id = query.data.split(":", 3)[3]
        svc = _get_server(svc_id)
        if not svc:
            await _send_or_edit(query, "❌ Сервер не найден.", _kb_back_home())
            return
        status, body = await _fetch(svc, "POST", "/doors", json={"title": ""})
        if status != 200 or not isinstance(body, dict):
            await _send_or_edit(
                query,
                f"<b>🆕 Создать дверь</b>\n\n❌ Ошибка: <code>{_html(body)}</code>",
                _kb_back_home(),
            )
            return
        did = body.get("door_id")
        link = f"{svc['url']}/door/{did}"
        text = (
            f"✅ <b>Дверь #{did}</b> создана на <i>{_html(svc['name'])}</i>.\n\n"
            f"🔗 Ссылка для ИИ-дебатёров:\n<code>{_html(link)}</code>\n\n"
            "Открой дверь и нажми <b>🎙 Запустить сессию</b>, чтобы задать тему."
        )
        kb = _kb_back_home(
            extra=[
                [
                    InlineKeyboardButton(
                        text=f"🚪 Открыть #{did}",
                        callback_data=f"debates:door:view:{svc['id']}:{did}",
                    )
                ],
                [InlineKeyboardButton(text="« Двери", callback_data="debates:doors")],
            ]
        )
        await _send_or_edit(query, text, kb)

    # Door view ---------------------------------------------------------------

    @router.callback_query(F.data.startswith("debates:door:view:"))
    async def door_view_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        _, _, _, svc_id, did_s = query.data.split(":", 4)
        svc = _get_server(svc_id)
        if not svc:
            await _send_or_edit(query, "❌ Сервер не найден.", _kb_back_home())
            return
        did = int(did_s)
        status, st = await _fetch(svc, "GET", f"/door/{did}/state")
        body = _render_door_view(svc, did, st if status == 200 and isinstance(st, dict) else None)
        active = bool(isinstance(st, dict) and st.get("session_active"))
        await _send_or_edit(query, body, _kb_door_view(svc["id"], did, active))

    @router.callback_query(F.data.startswith("debates:door:del:"))
    async def door_del_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        _, _, _, svc_id, did_s = query.data.split(":", 4)
        svc = _get_server(svc_id)
        if not svc:
            await _send_or_edit(query, "❌ Сервер не найден.", _kb_back_home())
            return
        did = int(did_s)
        status, body = await _fetch(svc, "DELETE", f"/doors/{did}")
        if status == 200:
            await query.answer(f"🗑 #{did} удалена")
        else:
            await query.answer(f"❌ {body}", show_alert=True)
        body_, btns = await _render_doors_all()
        await _send_or_edit(query, body_, _kb_back_home(extra=btns))

    @router.callback_query(F.data.startswith("debates:door:end:"))
    async def door_end_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        _, _, _, svc_id, did_s = query.data.split(":", 4)
        svc = _get_server(svc_id)
        if not svc:
            await _send_or_edit(query, "❌ Сервер не найден.", _kb_back_home())
            return
        did = int(did_s)
        status, body = await _fetch(svc, "POST", f"/door/{did}/session/end")
        if status == 200 and isinstance(body, dict):
            archived = body.get("archived_session_id") or "—"
            await query.answer("🏁 Сессия завершена")
            msg = (
                f"🏁 Сессия двери #{did} (<i>{_html(svc['name'])}</i>) завершена.\n"
                f"Архив: <code>{_html(archived)}</code>"
            )
        else:
            await query.answer("❌ Не удалось", show_alert=True)
            msg = f"<b>🏁 Завершение</b>\n\n❌ <code>{_html(body)}</code>"
        # Refresh door view
        status, st = await _fetch(svc, "GET", f"/door/{did}/state")
        full = msg + "\n\n" + _render_door_view(svc, did, st if status == 200 and isinstance(st, dict) else None)
        active = bool(isinstance(st, dict) and st.get("session_active"))
        await _send_or_edit(query, full, _kb_door_view(svc["id"], did, active))

    # Events tail -------------------------------------------------------------

    @router.callback_query(F.data.startswith("debates:door:events:"))
    async def door_events_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        _, _, _, svc_id, did_s = query.data.split(":", 4)
        svc = _get_server(svc_id)
        if not svc:
            await _send_or_edit(query, "❌ Сервер не найден.", _kb_back_home())
            return
        did = int(did_s)
        status, ev = await _fetch(svc, "GET", f"/door/{did}/events", params={"since": 0})
        lines = [f"<b>🔔 События двери #{did}</b>", f"<i>{_html(svc['name'])}</i>", ""]
        if status != 200 or not isinstance(ev, dict):
            lines.append(f"❌ <code>{_html(ev)}</code>")
        else:
            events = ev.get("events") or []
            if not events:
                lines.append("<i>Пока ничего не происходило.</i>")
            else:
                for e in events[-EVENTS_TAIL:]:
                    lines.append(f"• {_html(e.get('text'))}")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Обновить",
                        callback_data=f"debates:door:events:{svc['id']}:{did}",
                    ),
                    InlineKeyboardButton(
                        text="« Дверь",
                        callback_data=f"debates:door:view:{svc['id']}:{did}",
                    ),
                ]
            ]
        )
        await _send_or_edit(query, "\n".join(lines), kb)

    # Start session FSM -------------------------------------------------------

    @router.callback_query(F.data.startswith("debates:door:start:"))
    async def door_start_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        _, _, _, svc_id, did_s = query.data.split(":", 4)
        svc = _get_server(svc_id)
        if not svc:
            await _send_or_edit(query, "❌ Сервер не найден.", _kb_back_home())
            return
        did = int(did_s)
        await state.set_state(DebatesStates.awaiting_session)
        await state.update_data(_dbt_svc=svc_id, _dbt_did=did)
        if query.message is not None:
            await query.message.answer(
                f"🎙 <b>Запуск сессии в двери #{did}</b>\n"
                f"<i>{_html(svc['name'])}</i>\n\n"
                "Пришли одним сообщением:\n"
                "  • первая строка: <code>&lt;кол-во&gt; &lt;тема&gt;</code>\n"
                "  • дальше — текст ТЗ (или ничего, тогда ТЗ пустой)\n\n"
                "Или прикрепи <code>.txt</code>/<code>.md</code> с тем же шаблоном в подписи.\n\n"
                "Пример первой строки: <code>3 Улучшаем архитектуру relay</code>",
                disable_web_page_preview=True,
            )
        await query.answer()

    @router.message(DebatesStates.awaiting_session, F.document)
    async def session_doc_(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        data = await state.get_data()
        svc = _get_server(data.get("_dbt_svc"))
        did = data.get("_dbt_did")
        if not svc or did is None:
            await state.clear()
            await message.answer("Контекст утерян, попробуй снова из меню двери.")
            return
        caption = (message.caption or "").strip()
        if not caption:
            await message.answer(
                "Нужна подпись к файлу: <code>&lt;кол-во&gt; &lt;тема&gt;</code>"
            )
            return
        parts = caption.split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            await message.answer(
                "Неверный формат. Подпись: <code>&lt;кол-во&gt; &lt;тема&gt;</code>"
            )
            return
        required, title = int(parts[0]), parts[1]
        tz_text = ""
        doc = message.document
        if doc and (doc.file_size or 0) <= MAX_TZ_BYTES:
            try:
                bot = message.bot
                file = await bot.get_file(doc.file_id)
                buf = await bot.download_file(file.file_path)
                if buf is not None:
                    raw = buf.read() if hasattr(buf, "read") else bytes(buf)
                    tz_text = raw[:MAX_TZ_BYTES].decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                logger.warning("debates tz download failed: %s", exc)
        await _do_start_session(message, svc, did, required, title, tz_text)
        await state.clear()

    @router.message(DebatesStates.awaiting_session, F.text)
    async def session_text_(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        data = await state.get_data()
        svc = _get_server(data.get("_dbt_svc"))
        did = data.get("_dbt_did")
        if not svc or did is None:
            await state.clear()
            await message.answer("Контекст утерян, попробуй снова из меню двери.")
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пустое сообщение, жду ТЗ.")
            return
        first, _, rest = text.partition("\n")
        parts = first.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            await message.answer(
                "Первая строка должна быть: <code>&lt;кол-во&gt; &lt;тема&gt;</code>"
            )
            return
        required, title = int(parts[0]), parts[1]
        tz_text = rest.strip()
        await _do_start_session(message, svc, did, required, title, tz_text)
        await state.clear()

    # Services ----------------------------------------------------------------

    @router.callback_query(F.data == "debates:services")
    async def services_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        services = await _all_with_status()
        body, rm = _render_services(services)
        extra = list(rm)
        extra.append(
            [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="debates:add")]
        )
        await _send_or_edit(query, body, _kb_back_home(extra=extra))

    @router.callback_query(F.data.startswith("debates:rm:"))
    async def rm_svc_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        svc_id = query.data.split(":", 2)[2]
        removed = _remove_server(svc_id)
        await query.answer("✅ Удалено" if removed else "❌ Не найдено")
        services = await _all_with_status()
        body, rm = _render_services(services)
        extra = list(rm)
        extra.append(
            [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="debates:add")]
        )
        if query.message is not None:
            try:
                await query.message.edit_text(
                    body, reply_markup=_kb_back_home(extra=extra), disable_web_page_preview=True
                )
            except Exception:  # noqa: BLE001
                await query.message.answer(
                    body, reply_markup=_kb_back_home(extra=extra), disable_web_page_preview=True
                )

    # Add server FSM ----------------------------------------------------------

    @router.callback_query(F.data == "debates:add")
    async def add_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(DebatesStates.awaiting_add_url)
        if query.message is not None:
            await query.message.answer(
                "Пришли URL debate-ii одним сообщением.\n"
                "Пример: <code>https://debate-ii.onrender.com</code> или "
                "<code>http://127.0.0.1:8080</code>\n\n"
                "Можно сразу с названием: "
                "<code>https://debate-ii.onrender.com Render</code>",
                disable_web_page_preview=True,
            )
        await query.answer()

    @router.message(DebatesStates.awaiting_add_url)
    async def add_url_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пустой URL — попробуй ещё раз.")
            return
        parts = text.split(maxsplit=1)
        url = parts[0]
        name = parts[1] if len(parts) > 1 else url
        if not (url.startswith("http://") or url.startswith("https://")):
            await message.answer("❌ URL должен начинаться с http:// или https://")
            return
        await state.update_data(_dbt_add_url=url, _dbt_add_name=name)
        await state.set_state(DebatesStates.awaiting_add_token)
        await message.answer(
            "Теперь пришли <code>DEBATE_TOKEN</code> сервера (общий секрет API).\n"
            "Пришли <code>-</code>, если токен не нужен (сервер открыт).",
            disable_web_page_preview=True,
        )

    @router.message(DebatesStates.awaiting_add_token)
    async def add_token_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        token_raw = (message.text or "").strip()
        token = "" if token_raw in ("-", "—", "") else token_raw
        data = await state.get_data()
        url = data.get("_dbt_add_url", "")
        name = data.get("_dbt_add_name") or url
        probe = await message.answer(f"⏳ Проверяю сервер <code>{_html(url)}</code>…")
        # Try health first (no token needed for /healthz)
        status, body = await _fetch({"url": url, "token": token}, "GET", "/healthz")
        added, svc = _add_server(url, name, token)
        if status == 200:
            # Try to fetch doors count (needs token)
            dstatus, dbody = await _fetch(svc, "GET", "/doors")
            total = dbody.get("total_doors") if isinstance(dbody, dict) else "?"
            cap = dbody.get("max_doors") if isinstance(dbody, dict) else "?"
            reply = (
                f"{'✅ Сервер добавлен' if added else '⚠️ Сервер обновлён'}\n\n"
                f"<b>{_html(svc['name'])}</b>\n"
                f"🔗 <code>{_html(svc['url'])}</code>\n"
                f"🚪 Дверей: {total}/{cap}\n"
                f"ID: <code>{_html(svc['id'])}</code>"
            )
            if dstatus not in (200, 0):
                reply += f"\n⚠️ /doors вернул {dstatus} — проверь DEBATE_TOKEN."
        else:
            reply = (
                f"⚠️ Сервер недоступен, но сохранён (возможно спит на Render).\n"
                f"<code>{_html(url)}</code>\nID: <code>{_html(svc['id'])}</code>"
            )
        try:
            await probe.edit_text(reply, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await message.answer(reply, disable_web_page_preview=True)
        await state.clear()
        await show_screen(message)

    # ── TZ management ─────────────────────────────────────────────────────────

    from . import tz_manager as _tz
    from aiogram.fsm.state import State as _TzState, StatesGroup as _TzSG

    class TzStates(_TzSG):
        awaiting_tz_upload = _TzState()

    # ── TZ auto-clean settings (stored in addon state) ──

    def _tz_auto_cfg() -> dict[str, Any]:
        """Get auto-clean config: {enabled, brain_slot}."""
        raw = addon_state.get("debates", "tz_auto_clean", {}) or {}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "brain_slot": str(raw.get("brain_slot", "1")),
        }

    def _tz_auto_cfg_set(cfg: dict[str, Any]) -> None:
        addon_state.set_("debates", "tz_auto_clean", cfg)

    def _brain_labels() -> list[tuple[str, str]]:
        """Return [(slot_id, label), ...] for available brains."""
        from ...agent import _slot_cfg, _slot_is_configured
        from ...config import MODEL as DEFAULT_MODEL
        out: list[tuple[str, str]] = []
        for slot in ("1", "2"):
            if _slot_is_configured(slot):
                provider, _, _, model = _slot_cfg(slot)
                short = (model or provider or f"Слот {slot}")
                # Truncate model name
                if "/" in short:
                    short = short.split("/")[-1]
                if len(short) > 30:
                    short = short[:27] + "…"
                out.append((slot, f"🧠 Слот {slot}: {short}"))
        # Default model as "slot 0"
        if DEFAULT_MODEL:
            short = DEFAULT_MODEL
            if "/" in short:
                short = short.split("/")[-1]
            if len(short) > 30:
                short = short[:27] + "…"
            out.append(("0", f"🤖 По умолч.: {short}"))
        return out

    async def _do_auto_clean(tz_id: str, brain_slot: str,
                              msg: Message) -> None:
        """Run AI cleanup on a TZ entry using the specified brain."""
        entry = _tz.get(tz_id)
        if not entry:
            await msg.reply("❌ ТЗ не найдено")
            return
        content = entry.get("content", "")
        if len(content) < 50:
            await msg.reply("❌ Слишком короткий текст")
            return

        # Build prompt for cleanup
        system_prompt = (
            "Ты — фильтр ТЗ для дебатов. Тебе дан текст технического задания. "
            "Убери ВСЕ пункты, которые не относятся к логике/функциональности приложения: "
            "деплой, серверы, docker, CI/CD, хостинг, инфраструктура, мониторинг, "
            "настройка окружения, GitHub Actions, Render, облачные провайдеры и т.д. "
            "Оставь ТОЛЬКО пункты про функции, модули, API, логику, данные, UX. "
            "Верни очищенный текст без комментариев, без вступления."
        )

        try:
            from ...agent import _slot_cfg
            from ...config import MODEL as DEFAULT_MODEL

            if brain_slot == "0":
                # Use default model via openrouter
                import httpx
                provider = "openrouter"
                api_key = ""
                base_url = ""
                model = DEFAULT_MODEL
                # Get the key for openrouter
                from ... import storage as _storage
                api_key = _storage.get_provider_key("openrouter") or ""
                base_url = _storage.get_base_url() or ""
                if not base_url:
                    base_url = "https://openrouter.ai/api/v1"
            else:
                provider, api_key, base_url, model = _slot_cfg(brain_slot)
                if not base_url:
                    if provider == "openrouter":
                        base_url = "https://openrouter.ai/api/v1"
                    else:
                        base_url = "https://openrouter.ai/api/v1"

            if not api_key:
                await msg.reply(
                    f"❌ Мозг (слот {brain_slot}) не настроен — нет API ключа.\n"
                    "Настрой через /setup → 🧠 Мозг.")
                return

            import httpx
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                "max_tokens": 4096,
            }
            async with httpx.AsyncClient(timeout=60.0) as cli:
                resp = await cli.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code != 200:
                    await msg.reply(
                        f"❌ API вернул {resp.status_code}:\n"
                        f"<code>{resp.text[:300]}</code>")
                    return
                data = resp.json()
                cleaned = (data.get("choices", [{}])[0]
                           .get("message", {}).get("content", ""))

            if not cleaned or len(cleaned) < 30:
                await msg.reply("❌ ИИ вернул пустой или слишком короткий ответ.")
                return

            # Update the TZ entry with cleaned content
            _tz.update_content(tz_id, cleaned)

            # Recalc summary
            first_lines = [ln.strip() for ln in cleaned.split("\n")
                           if ln.strip() and not ln.startswith("#")][:2]
            new_summary = " ".join(first_lines)[:200] if first_lines else cleaned[:200]
            _tz.update_summary(tz_id, new_summary)

            short_model = model
            if "/" in short_model:
                short_model = short_model.split("/")[-1]

            await msg.reply(
                f"✅ <b>ТЗ очищено!</b>\n\n"
                f"Мозг: <code>{short_model}</code>\n"
                f"Было: {len(content)} символов → Стало: {len(cleaned)} символов\n"
                f"Убрано: {len(content) - len(cleaned)} символов")

        except Exception as e:
            logger.exception("TZ auto-clean failed")
            await msg.reply(f"❌ Ошибка очистки: {str(e)[:200]}")

    @router.callback_query(F.data.startswith("tz:upload:"))
    async def tz_upload(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        tz_type = query.data.split(":")[-1]  # "low" or "super"
        label = "ТЗ лоу" if tz_type == "low" else "Супер ТЗ"
        await state.set_state(TzStates.awaiting_tz_upload)
        await state.update_data(tz_type=tz_type)
        if query.message is not None:
            await query.message.edit_text(
                f"<b>📄 Загрузка {label}</b>\n\n"
                "Пришли файл (<code>.md</code>, <code>.txt</code>) "
                "или текст сообщением.\n\n"
                "ТЗ сохранится как есть. Потом можно нажать "
                "<b>🧹 Авто убрать лишнее</b> чтобы ИИ почистил.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="« Назад",
                                          callback_data="debates:open")]]),
            )
        await query.answer()

    async def _save_tz_raw(text: str, tz_type: str, msg: Message) -> None:
        """Save TZ text as-is (no cleanup)."""
        text = text.strip()
        if len(text) < 50:
            await msg.reply("❌ Текст слишком короткий (<50 символов).")
            return

        # Generate summary
        first_lines = [ln.strip() for ln in text.split("\n")
                       if ln.strip() and not ln.startswith("#")][:2]
        summary = " ".join(first_lines)[:200] if first_lines else text[:200]

        # Generate name from first heading or first line
        name = "Без названия"
        for ln in text.split("\n"):
            ln = ln.strip()
            if ln.startswith("#"):
                name = ln.lstrip("#").strip()[:80]
                break
            if len(ln) > 10:
                name = ln[:80]
                break

        entry = _tz.add(tz_type, name, summary, text)
        label = "ТЗ лоу" if tz_type == "low" else "Супер ТЗ"

        # Check if auto-clean is enabled
        acfg = _tz_auto_cfg()
        auto_note = ""
        if acfg["enabled"]:
            auto_note = "\n\n⏳ <i>Авто-очистка включена — запускаю…</i>"

        await msg.reply(
            f"✅ <b>{label} сохранено</b>\n\n"
            f"📄 <b>{entry['name']}</b>\n"
            f"<i>{entry['summary'][:150]}</i>\n\n"
            f"ID: <code>{entry['id']}</code>\n"
            f"Размер: {len(text)} символов{auto_note}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« В меню дебатов",
                                      callback_data="debates:open")],
                [InlineKeyboardButton(text="📂 Хранилище ТЗ",
                                      callback_data="tz:storage")],
            ]),
        )

        # If auto-clean is on, run it
        if acfg["enabled"]:
            await _do_auto_clean(entry["id"], acfg["brain_slot"], msg)

    @router.message(TzStates.awaiting_tz_upload, F.document)
    async def tz_upload_doc(msg: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        doc = msg.document
        if doc.file_size and doc.file_size > 500_000:
            await msg.reply("❌ Файл слишком большой (>500KB)")
            return
        file = await msg.bot.download(doc)
        text = file.read().decode("utf-8", errors="replace")
        await _save_tz_raw(text, data.get("tz_type", "low"), msg)

    @router.message(TzStates.awaiting_tz_upload, F.text)
    async def tz_upload_text(msg: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        text = msg.text or ""
        if len(text) < 50:
            await msg.reply("❌ Текст слишком короткий (<50 символов)")
            return
        await _save_tz_raw(text, data.get("tz_type", "low"), msg)

    @router.callback_query(F.data == "tz:storage")
    async def tz_storage(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        low = _tz.list_by_type("low")
        sup = _tz.list_by_type("super")
        rows: list[list[InlineKeyboardButton]] = []
        if low:
            rows.append([InlineKeyboardButton(
                text=f"── 📁 Лоу ТЗ ({len(low)}) ──",
                callback_data="tz:noop")])
            for e in low[-10:]:
                rows.append([InlineKeyboardButton(
                    text=f"📄 {e['name'][:35]}",
                    callback_data=f"tz:view:{e['id']}")])
        if sup:
            rows.append([InlineKeyboardButton(
                text=f"── 📁 Супер ТЗ ({len(sup)}) ──",
                callback_data="tz:noop")])
            for e in sup[-10:]:
                rows.append([InlineKeyboardButton(
                    text=f"📄✨ {e['name'][:35]}",
                    callback_data=f"tz:view:{e['id']}")])
        if not low and not sup:
            rows.append([InlineKeyboardButton(
                text="(хранилище пусто)",
                callback_data="tz:noop")])
        rows.append([InlineKeyboardButton(
            text="« Назад", callback_data="debates:open")])
        if query.message is not None:
            await query.message.edit_text(
                "<b>📂 Хранилище ТЗ</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await query.answer()

    @router.callback_query(F.data == "tz:noop")
    async def tz_noop(query: CallbackQuery) -> None:
        await query.answer()

    @router.callback_query(F.data.startswith("tz:view:"))
    async def tz_view(query: CallbackQuery) -> None:
        tz_id = query.data.split(":")[-1]
        entry = _tz.get(tz_id)
        if not entry:
            await query.answer("ТЗ не найдено", show_alert=True)
            return
        label = "Лоу ТЗ" if entry["type"] == "low" else "Супер ТЗ"
        acfg = _tz_auto_cfg()
        auto_label = "✅ вкл" if acfg["enabled"] else "❌ выкл"
        text = (
            f"<b>📄 {label}: {entry['name'][:60]}</b>\n\n"
            f"<i>{entry['summary'][:200]}</i>\n\n"
            f"Создано: {entry.get('createdAt', '—')}\n"
            f"Размер: {len(entry.get('content', ''))} символов\n"
            f"Авто-очистка: {auto_label}"
        )
        rows = [
            [InlineKeyboardButton(
                text="🧹 Авто убрать лишнее",
                callback_data=f"tz:clean:{tz_id}")],
            [InlineKeyboardButton(text="🗑 Удалить",
                                  callback_data=f"tz:del:{tz_id}")],
            [InlineKeyboardButton(text="« Хранилище",
                                  callback_data="tz:storage")],
        ]
        if query.message is not None:
            await query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await query.answer()

    # ── Auto-clean: brain picker ──

    @router.callback_query(F.data.startswith("tz:clean:"))
    async def tz_clean_pick(query: CallbackQuery) -> None:
        tz_id = query.data.split(":")[-1]
        entry = _tz.get(tz_id)
        if not entry:
            await query.answer("ТЗ не найдено", show_alert=True)
            return
        brains = _brain_labels()
        acfg = _tz_auto_cfg()
        rows: list[list[InlineKeyboardButton]] = []
        for slot_id, label in brains:
            mark = " ✓" if slot_id == acfg["brain_slot"] else ""
            rows.append([InlineKeyboardButton(
                text=f"{label}{mark}",
                callback_data=f"tz:doclean:{tz_id}:{slot_id}")])
        # Auto on/off toggle
        if acfg["enabled"]:
            rows.append([InlineKeyboardButton(
                text="🔴 Авто: ВКЛ → выключить",
                callback_data=f"tz:autotgl:{tz_id}:off")])
        else:
            rows.append([InlineKeyboardButton(
                text="🟢 Авто: ВЫКЛ → включить",
                callback_data=f"tz:autotgl:{tz_id}:on")])
        rows.append([InlineKeyboardButton(
            text="« Назад к ТЗ",
            callback_data=f"tz:view:{tz_id}")])
        if query.message is not None:
            await query.message.edit_text(
                f"<b>🧹 Выбери мозг для очистки</b>\n\n"
                f"ТЗ: <b>{entry['name'][:50]}</b>\n"
                f"Размер: {len(entry.get('content', ''))} символов\n\n"
                f"Нажми на мозг — он уберёт лишние пункты.\n"
                f"Или включи <b>Авто</b> — каждое новое ТЗ будет чиститься автоматически.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await query.answer()

    @router.callback_query(F.data.startswith("tz:doclean:"))
    async def tz_do_clean(query: CallbackQuery) -> None:
        parts = query.data.split(":")
        tz_id = parts[2]
        brain_slot = parts[3]
        # Save chosen brain as default
        acfg = _tz_auto_cfg()
        acfg["brain_slot"] = brain_slot
        _tz_auto_cfg_set(acfg)
        await query.answer("⏳ Запускаю очистку…")
        if query.message is not None:
            await _do_auto_clean(tz_id, brain_slot, query.message)

    @router.callback_query(F.data.startswith("tz:autotgl:"))
    async def tz_auto_toggle(query: CallbackQuery) -> None:
        parts = query.data.split(":")
        tz_id = parts[2]
        action = parts[3]  # "on" or "off"
        acfg = _tz_auto_cfg()
        acfg["enabled"] = (action == "on")
        _tz_auto_cfg_set(acfg)
        label = "✅ Авто-очистка ВКЛЮЧЕНА" if acfg["enabled"] else "❌ Авто-очистка ВЫКЛЮЧЕНА"
        await query.answer(label)
        # Refresh brain picker
        query.data = f"tz:clean:{tz_id}"
        await tz_clean_pick(query)

    @router.callback_query(F.data.startswith("tz:del:"))
    async def tz_del(query: CallbackQuery) -> None:
        tz_id = query.data.split(":")[-1]
        ok = _tz.delete(tz_id)
        if ok:
            await query.answer("🗑 Удалено")
        else:
            await query.answer("Не найдено", show_alert=True)
        # Refresh storage view
        query.data = "tz:storage"
        await tz_storage(query)


    # Mount embedded debate-door v3 management screens.
    try:
        from .debate_doors import register_debate_doors
        register_debate_doors(router)
    except Exception:  # noqa: BLE001
        logger.exception("failed to register debate-doors callbacks on Debates router")

    return router


# ─── Session start helper ─────────────────────────────────────────────────────


async def _do_start_session(
    message: Message,
    svc: dict[str, Any],
    did: int,
    required: int,
    title: str,
    tz_text: str,
) -> None:
    probe = await message.answer(f"⏳ Запускаю сессию в #{did}…")
    status, body = await _fetch(
        svc,
        "POST",
        f"/door/{did}/session/start",
        json={
            "title": title,
            "required_debaters": required,
            "tz_text": tz_text,
        },
    )
    if status == 200 and isinstance(body, dict):
        link = f"{svc['url']}/door/{did}"
        note = f"ТЗ {len(tz_text)} симв." if tz_text else "без файла ТЗ"
        text = (
            f"🎙 Дебат запущен в #{did}: <b>{_html(title)}</b>\n"
            f"Нужно дебатёров: <b>{required}</b> · {note}\n"
            f"🔗 <code>{_html(link)}</code>\n\n"
            f"Сервер: <i>{_html(svc['name'])}</i>\n"
            f"Подпишись на ленту через 🔔 События."
        )
    else:
        text = f"❌ Не удалось запустить: <code>{_html(body)}</code>"
    try:
        await probe.edit_text(text, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await message.answer(text, disable_web_page_preview=True)


async def start_problem_debate(
    message: Message, tz_text: str, title: str, *, required: int = 3,
) -> bool:
    """Public hook: launch a problem-finding debate on the active server/door.

    Used by Регенерация to hand the project ТЗ to the existing debate
    engine. Returns True if a session was started, False if no online
    server/door is available (caller should then fall back to a file).
    """
    svc = await _first_online()
    if not svc:
        return False
    did = None
    with contextlib.suppress(Exception):
        doors = await _fetch_doors(svc)
        if isinstance(doors, dict):
            raw = doors.get("doors") or doors.get("items") or doors
            if isinstance(raw, list) and raw:
                first = raw[0]
                did = first.get("id") if isinstance(first, dict) else first
            elif isinstance(raw, dict) and raw:
                did = next(iter(raw.keys()))
    if did is None:
        did = 1  # generic door default
    with contextlib.suppress(Exception):
        did = int(did)
    await _do_start_session(message, svc, did, required, title, tz_text)
    return True

