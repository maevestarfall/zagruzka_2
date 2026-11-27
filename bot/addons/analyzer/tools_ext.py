# Analyzer PRO tools extension: tool picker, decision-map, auto-pick (isolated add-on layer).
# Co-authored with CoCo
"""Separate, removable extension for «🔬 Анализатор PRO».

Design goal (Mason): DO NOT touch the existing analyzer model — just hook
into it. Everything here lives under its own state keys (``tx_*``) and its
own callback prefix (``anx:``). Two hook lines plug it in:

* ``tools_ext.register(router)``           — inside ``build_analyzer_router``.
* a menu button ``anx:menu`` in ``_kb_home``.

Features:
* Real tool catalog pulled from the server's ``GET /tools/available``
  (cached to ``/data/storage/analyzer_pro/`` + state, so it survives
  restarts and shows offline).
* 🧰 Tool picker — dynamic toggle buttons with ✅ + priority number.
* 🗺 Auto decision-map — editable routing rules (default provided,
  replace / delete / reset), on/off.
* 🤖 Auto-pick — "read all tools and decide yourself", on/off.
* 📂 Data section inside the existing 💾 Storage.

If нет подключённого сервера — экраны всё равно работают (показывают кэш
и заметку «работает локально без сервера»).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "analyzer"  # share the analyzer namespace, separate keys

DEFAULT_MAP = (
    "# Авто-путеводитель: какие инструменты под какую задачу.\n"
    "# Формат: одна строка = одно правило «ключевые слова → инструменты».\n"
    "новости, события, тренды → веб-поиск, глубокий ресёрч\n"
    "сайт, страница, url → парсинг сайта, веб-поиск\n"
    "документ, pdf, файл, таблица → файл→JSON, реферат\n"
    "код, репозиторий, ошибка, баг → анализ кода, веб-поиск\n"
    "сводка, итог, кратко → реферат, сводка\n"
)


# ─── State (all under tx_* keys, isolated) ───────────────────────────────────

def get_catalog() -> list[dict]:
    raw = addon_state.get(ADDON, "tx_catalog", []) or []
    return [t for t in raw if isinstance(t, dict)]


def set_catalog(tools: list[dict]) -> None:
    addon_state.set_(ADDON, "tx_catalog", tools)
    _persist_data()


def get_selected() -> list[str]:
    """Ordered list of selected tool ids (order = priority)."""
    raw = addon_state.get(ADDON, "tx_selected", []) or []
    return [str(x) for x in raw]


def set_selected(ids: list[str]) -> None:
    addon_state.set_(ADDON, "tx_selected", ids)
    _persist_data()


def toggle_selected(tool_id: str) -> None:
    cur = get_selected()
    if tool_id in cur:
        cur = [x for x in cur if x != tool_id]
    else:
        cur.append(tool_id)
    set_selected(cur)


def get_map() -> str:
    val = addon_state.get(ADDON, "tx_map", None)
    return val if isinstance(val, str) else DEFAULT_MAP


def set_map(text: str) -> None:
    addon_state.set_(ADDON, "tx_map", text)
    _persist_data()


def reset_map() -> None:
    addon_state.set_(ADDON, "tx_map", DEFAULT_MAP)
    _persist_data()


def map_enabled() -> bool:
    return bool(addon_state.get(ADDON, "tx_map_enabled", True))


def set_map_enabled(v: bool) -> None:
    addon_state.set_(ADDON, "tx_map_enabled", bool(v))


def auto_pick() -> bool:
    return bool(addon_state.get(ADDON, "tx_auto_pick", False))


def set_auto_pick(v: bool) -> None:
    addon_state.set_(ADDON, "tx_auto_pick", bool(v))


# ─── Storage section (/data/storage/analyzer_pro/) ───────────────────────────

def _data_dir():
    from ...config import DATA_DIR
    d = DATA_DIR / "storage" / "analyzer_pro"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _persist_data() -> None:
    """Mirror catalog/selection/map into the storage section as JSON."""
    try:
        payload = {
            "catalog": get_catalog(),
            "selected": get_selected(),
            "map": get_map(),
            "map_enabled": map_enabled(),
            "auto_pick": auto_pick(),
            "saved_at": int(time.time()),
        }
        (_data_dir() / "analyzer_pro.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ─── Catalog fetch from the server (real tools) ──────────────────────────────

def _parse_tools(body: Any) -> list[dict]:
    """Normalise /tools/available into [{id, name, desc}]."""
    items = body
    if isinstance(body, dict):
        items = body.get("tools") or body.get("available") or body.get("data") or []
    out: list[dict] = []
    if isinstance(items, dict):
        items = [{"name": k, "description": v} for k, v in items.items()]
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, str):
            out.append({"id": it, "name": it, "desc": ""})
        elif isinstance(it, dict):
            name = str(it.get("name") or it.get("id") or it.get("tool") or "?")
            out.append({
                "id": str(it.get("id") or name),
                "name": name,
                "desc": str(it.get("description") or it.get("desc") or ""),
            })
    return out


async def fetch_catalog() -> tuple[list[dict], str]:
    """Pull real tools from the active Analyzer-Boss. Returns (tools, note)."""
    try:
        from .handlers import _active_server, _fetch
    except Exception:  # noqa: BLE001
        return get_catalog(), "локальный режим (нет доступа к серверу)"
    svc = _active_server()
    if not svc:
        cached = get_catalog()
        note = ("сервер не подключён — показываю кэш" if cached
                else "сервер не подключён — каталог пуст, подключи Analyzer-Boss")
        return cached, note
    try:
        code, body = await _fetch(svc, "GET", "/tools/available", timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("anx: tools fetch failed: %s", exc)
        return get_catalog(), f"сервер недоступен ({exc}) — показываю кэш"
    if code != 200:
        return get_catalog(), f"сервер вернул {code} — показываю кэш"
    tools = _parse_tools(body)
    if tools:
        set_catalog(tools)
        return tools, f"каталог обновлён с сервера ({len(tools)} шт.)"
    return get_catalog(), "сервер не отдал инструменты — показываю кэш"


# ─── Payload injection (read by the user's Analyzer-Boss; harmless if not) ───

def payload_extra() -> dict:
    """Extra fields merged into POST /user/analysis-request."""
    extra: dict[str, Any] = {}
    sel = get_selected()
    if sel:
        extra["tools"] = sel
    if map_enabled():
        extra["routing"] = get_map()
    if auto_pick():
        extra["auto_pick"] = True
    return extra


# ─── UI ───────────────────────────────────────────────────────────────────────

def menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🧰 Инструменты", callback_data="anx:menu")


def _esc(s: Any) -> str:
    import html
    return html.escape(str(s or ""))


def _kb_menu() -> InlineKeyboardMarkup:
    me = "ВКЛ" if map_enabled() else "ВЫКЛ"
    ap = "ВКЛ" if auto_pick() else "ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧰 Выбрать инструменты", callback_data="anx:tools")],
        [InlineKeyboardButton(text="ℹ️ Что умеет каждый", callback_data="anx:desc")],
        [InlineKeyboardButton(text=f"🗺 Авто-путеводитель: {me}", callback_data="anx:map")],
        [InlineKeyboardButton(text=f"🤖 Авто-выбор инструментов: {ap}", callback_data="anx:auto")],
        [InlineKeyboardButton(text="📂 Данные Анализатора PRO", callback_data="anx:data")],
        [InlineKeyboardButton(text="🔄 Обновить каталог с сервера", callback_data="anx:refresh")],
        [InlineKeyboardButton(text="« Назад", callback_data="analyzer:menu")],
    ])


def _menu_text(note: str = "") -> str:
    cat = get_catalog()
    sel = get_selected()
    lines = ["<b>🧰 Инструменты Анализатора PRO</b>", ""]
    lines.append(f"Каталог: <b>{len(cat)}</b> · выбрано: <b>{len(sel)}</b>")
    lines.append(f"Путеводитель: {'ВКЛ' if map_enabled() else 'ВЫКЛ'} · "
                 f"Авто-выбор: {'ВКЛ' if auto_pick() else 'ВЫКЛ'}")
    if note:
        lines += ["", f"<i>{_esc(note)}</i>"]
    lines += ["", "ℹ️ Пока сервер не подключён — работает локально (выбор и "
              "путеводитель сохраняются; анализ идёт через локальный движок)."]
    return "\n".join(lines)


def _kb_tools() -> InlineKeyboardMarkup:
    cat = get_catalog()
    sel = get_selected()
    rows: list[list[InlineKeyboardButton]] = []
    if not cat:
        rows.append([InlineKeyboardButton(
            text="📭 Каталог пуст — обнови с сервера", callback_data="anx:refresh")])
    for t in cat[:40]:
        tid = t.get("id") or t.get("name")
        name = t.get("name") or tid
        if tid in sel:
            prio = sel.index(tid) + 1
            label = f"✅{prio} {name}"
        else:
            label = f"➖ {name}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"anx:t:{tid}"[:64])])
    rows.append([InlineKeyboardButton(text="🧹 Сбросить выбор", callback_data="anx:tclear")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="anx:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_map() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'🟢 ВКЛ' if map_enabled() else '🔴 ВЫКЛ'} — переключить",
                              callback_data="anx:map:toggle")],
        [InlineKeyboardButton(text="✏️ Заменить (прислать свой)", callback_data="anx:map:edit")],
        [InlineKeyboardButton(text="🔁 Сброс к моему дефолту", callback_data="anx:map:reset")],
        [InlineKeyboardButton(text="🗑 Удалить (пусто)", callback_data="anx:map:clear")],
        [InlineKeyboardButton(text="« Назад", callback_data="anx:menu")],
    ])


# ─── FSM for editing the map ───────────────────────────────────────────────────

from aiogram.fsm.state import State, StatesGroup  # noqa: E402


class ToolsExtStates(StatesGroup):
    awaiting_map = State()


def register(router: Router) -> None:
    """Attach all anx: handlers to the analyzer router (one hook line)."""

    async def _owner(query: CallbackQuery) -> bool:
        from ...wizard import _is_owner
        return _is_owner(query.from_user.id if query.from_user else 0)

    @router.callback_query(F.data == "anx:menu")
    async def _menu(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(_menu_text(), reply_markup=_kb_menu(),
                                          parse_mode="HTML")

    @router.callback_query(F.data == "anx:refresh")
    async def _refresh(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer("🔄 Тяну каталог…")
        _, note = await fetch_catalog()
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(_menu_text(note), reply_markup=_kb_menu(),
                                          parse_mode="HTML")

    @router.callback_query(F.data == "anx:tools")
    async def _tools(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                "<b>🧰 Выбор инструментов</b>\n\nТап = ✅ + номер приоритета "
                "(по порядку). Повторный тап снимает.",
                reply_markup=_kb_tools(), parse_mode="HTML")

    @router.callback_query(F.data.startswith("anx:t:"))
    async def _toggle(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        tid = query.data[len("anx:t:"):]
        toggle_selected(tid)
        await query.answer("Обновлено")
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_reply_markup(reply_markup=_kb_tools())

    @router.callback_query(F.data == "anx:tclear")
    async def _tclear(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        set_selected([])
        await query.answer("Выбор сброшен")
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_reply_markup(reply_markup=_kb_tools())

    @router.callback_query(F.data == "anx:desc")
    async def _desc(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        cat = get_catalog()
        if not cat:
            text = "Каталог пуст — нажми «🔄 Обновить каталог с сервера»."
        else:
            lines = ["<b>ℹ️ Инструменты — что умеют</b>", ""]
            for t in cat[:40]:
                lines.append(f"• <b>{_esc(t.get('name'))}</b> — "
                             f"{_esc(t.get('desc')) or 'описание не задано'}")
            text = "\n".join(lines)
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="« Назад", callback_data="anx:menu")]]))

    @router.callback_query(F.data == "anx:map")
    async def _map(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                "<b>🗺 Авто-путеводитель</b>\n\n<pre>" + _esc(get_map()[:1500]) + "</pre>",
                parse_mode="HTML", reply_markup=_kb_map())

    @router.callback_query(F.data == "anx:map:toggle")
    async def _map_toggle(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        set_map_enabled(not map_enabled())
        await query.answer("Переключено")
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_reply_markup(reply_markup=_kb_map())

    @router.callback_query(F.data == "anx:map:reset")
    async def _map_reset(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        reset_map()
        await query.answer("Сброшено к дефолту")
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                "<b>🗺 Авто-путеводитель</b>\n\n<pre>" + _esc(get_map()[:1500]) + "</pre>",
                parse_mode="HTML", reply_markup=_kb_map())

    @router.callback_query(F.data == "anx:map:clear")
    async def _map_clear(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        set_map("")
        await query.answer("Путеводитель очищен")
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                "<b>🗺 Авто-путеводитель</b>\n\n<i>(пусто)</i>",
                parse_mode="HTML", reply_markup=_kb_map())

    @router.callback_query(F.data == "anx:map:edit")
    async def _map_edit(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(ToolsExtStates.awaiting_map)
        await query.answer()
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                "<b>✏️ Новый путеводитель</b>\n\nПришли текст правил одним "
                "сообщением. Он заменит текущий.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data="anx:map")]]))

    @router.message(ToolsExtStates.awaiting_map, F.text)
    async def _map_capture(message, state: FSMContext) -> None:
        from ...wizard import _is_owner
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return
        set_map((message.text or "").strip())
        await state.clear()
        await message.answer("✅ Путеводитель обновлён.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧰 Инструменты", callback_data="anx:menu")]]))

    @router.callback_query(F.data == "anx:auto")
    async def _auto(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        set_auto_pick(not auto_pick())
        await query.answer("Переключено")
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(_menu_text(), reply_markup=_kb_menu(),
                                          parse_mode="HTML")

    @router.callback_query(F.data == "anx:data")
    async def _data(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        _persist_data()
        d = _data_dir()
        files = sorted(p.name for p in d.iterdir() if p.is_file())
        body = ("<b>📂 Данные Анализатора PRO</b>\n\n"
                f"Папка: <code>/data/storage/analyzer_pro/</code>\n"
                f"Файлов: {len(files)}\n")
        if files:
            body += "\n" + "\n".join(f"• <code>{_esc(f)}</code>" for f in files[:20])
        body += ("\n\nЗдесь хранятся: кэш каталога, путеводитель, выбор "
                 "инструментов. Переживает перезапуск.")
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(body, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="« Назад", callback_data="anx:menu")]]))
