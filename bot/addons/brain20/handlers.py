"""aiogram router for the 20-slot brain pool.

Self-contained: its own ``Router`` and FSM state group, included by the
dispatcher via :func:`bot.addons.build_addon_routers`. All persistence goes
through the thin ``storage.*brain_slot*`` wrappers (logic in
:mod:`bot.brain_slots`).

Callback map (prefix ``b20:``)::

    b20:menu              pool overview (model selector + 20-slot grid)
    b20:slot:<id>         per-slot config screen
    b20:f:<id>:<field>    prompt for a slot field (api/url/m1/m2/m3/budget)
    b20:clr:<id>          clear a slot
    b20:tok:<id>          reset a slot's token counter
    b20:bulk              «во все 20 слотов» template screen
    b20:bf:<field>        prompt for a bulk field (url/m1/m2/m3)
    b20:bapply            push template into all 20 slots
    b20:am:<idx>          set the active model (1/2/3)
"""

from __future__ import annotations

import contextlib
import html
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...access import is_owner
from ...brain_slots import MODEL_FIELDS, SLOT_IDS
from ...storage import storage

logger = logging.getLogger(__name__)


class Brain20States(StatesGroup):
    awaiting_value = State()
    awaiting_import = State()


# --- field metadata: callback token -> (storage field, prompt) -------------
_SLOT_FIELDS = {
    "api": ("api_key", "🔑 Пришли <b>API-ключ</b> для слота {sid}:"),
    "url": ("base_url", "🌐 Пришли <b>URL</b> (OpenAI-совместимый, напр. https://api.x.com/v1) для слота {sid}:"),
    "m1": ("model1", "🤖 Пришли имя <b>модели 1</b> для слота {sid}:"),
    "m2": ("model2", "🤖 Пришли имя <b>модели 2</b> для слота {sid}:"),
    "m3": ("model3", "🤖 Пришли имя <b>модели 3</b> для слота {sid}:"),
    "budget": ("token_budget", "🎟 Пришли <b>бюджет токенов</b> для слота {sid} (число; 0 = без лимита):"),
    "img": ("image_url", "📷 Пришли <b>сайт-источник фото</b> (вторичный, для картинок) для слота {sid}:"),
    "bal": ("balance_url",
            "💰 Пришли <b>URL для проверки остатка</b> у провайдера для слота {sid} "
            "(напр. https://api.x/v1/credits или дашборд iyh.app).\n\n"
            "Можно оставить пусто — тогда я сам попробую стандартные адреса "
            "(/auth/key, /credits, /balance) от твоего URL слота."),
    "curl": ("curl",
             "📝 Пришли <b>cURL</b> (или код) для слота {sid} одним сообщением.\n\n"
             "Я сохраню его и попробую авто-извлечь URL, ключ и модель.\n\n"
             "<code>curl https://api.example.com/v1/chat/completions \\\n"
             "  -H \"Authorization: Bearer sk-...\" \\\n"
             "  -d '{{\"model\": \"gpt-4\", \"messages\": [...]}}'</code>"),
}
_BULK_FIELDS = {
    "url": ("base_url", "🌐 Пришли <b>URL</b> для всех 20 слотов:"),
    "img": ("image_url", "📷 Пришли <b>сайт-источник фото</b> (вторичный) для всех слотов:"),
    "m1": ("model1", "🤖 Пришли <b>модель 1</b> для всех слотов:"),
    "m2": ("model2", "🤖 Пришли <b>модель 2</b> для всех слотов:"),
    "m3": ("model3", "🤖 Пришли <b>модель 3</b> для всех слотов:"),
}


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def _parse_curl(text: str) -> dict:
    """Best-effort extraction of base_url / api_key / model from a pasted
    cURL command (or similar). Everything is optional — only keys that are
    confidently found are returned. The raw text is always stored separately.
    """
    out: dict[str, str] = {}
    # API key: "Authorization: Bearer <key>" or "x-api-key: <key>"
    m = re.search(r"(?:Authorization:\s*Bearer\s+|x-api-key:\s*)([A-Za-z0-9._\-]{8,})", text, re.I)
    if m:
        out["api_key"] = m.group(1).strip()
    # Endpoint URL: first http(s) URL; normalise to the OpenAI base (strip the
    # /chat/completions suffix so it works as base_url).
    u = re.search(r"https?://[^\s\"'\\]+", text)
    if u:
        url = u.group(0).rstrip("/")
        url = re.sub(r"/chat/completions/?$", "", url)
        out["base_url"] = url
    # Model name from a JSON body: "model": "..."
    mm = re.search(r"[\"']model[\"']\s*:\s*[\"']([^\"']+)[\"']", text)
    if mm:
        out["model1"] = mm.group(1).strip()
    return out


def _deny(query: CallbackQuery) -> bool:
    return not is_owner(query.from_user.id)


# --------------------------------------------------------------------------
# keyboards
# --------------------------------------------------------------------------
def _kb_pool() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # active-model selector — one button per filled model index
    bulk = storage.get_brain_bulk()
    active = storage.get_active_model_index()
    sel_row: list[InlineKeyboardButton] = []
    for idx in storage.available_model_indexes():
        name = bulk.get(f"model{idx}") or f"Модель {idx}"
        mark = "▶ " if idx == active else ""
        label = name if len(name) <= 16 else name[:15] + "…"
        sel_row.append(
            InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"b20:am:{idx}")
        )
    if sel_row:
        rows.append(sel_row)

    rows.append([InlineKeyboardButton(text="🧩 Во все 20 слотов", callback_data="b20:bulk")])
    rows.append([InlineKeyboardButton(text="📥 Массовый импорт (все 20 разом)", callback_data="b20:import")])
    rows.append([InlineKeyboardButton(text="📚 Справочник промтов", callback_data="pl:menu")])

    # 20-slot grid, 4 per row, with status glyphs
    grid_row: list[InlineKeyboardButton] = []
    for sid in SLOT_IDS:
        slot = storage.get_brain_slot(sid)
        if not slot.get("api_key"):
            glyph = "⚪"
        elif storage.brain_slot_budget_exhausted(sid):
            glyph = "⛔"
        else:
            glyph = "✅"
        grid_row.append(
            InlineKeyboardButton(text=f"{glyph}{sid}", callback_data=f"b20:slot:{sid}")
        )
        if len(grid_row) == 4:
            rows.append(grid_row)
            grid_row = []
    if grid_row:
        rows.append(grid_row)

    rows.append([InlineKeyboardButton(text="← В меню мозга", callback_data="auto_brain:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_slot(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 API ключ", callback_data=f"b20:f:{sid}:api"),
             InlineKeyboardButton(text="🌐 URL", callback_data=f"b20:f:{sid}:url")],
            [InlineKeyboardButton(text="🤖 Модель 1", callback_data=f"b20:f:{sid}:m1"),
             InlineKeyboardButton(text="🤖 Модель 2", callback_data=f"b20:f:{sid}:m2"),
             InlineKeyboardButton(text="🤖 Модель 3", callback_data=f"b20:f:{sid}:m3")],
            [InlineKeyboardButton(text="🎟 Бюджет токенов", callback_data=f"b20:f:{sid}:budget"),
             InlineKeyboardButton(text="♻️ Сброс счётчика", callback_data=f"b20:tok:{sid}")],
            [InlineKeyboardButton(text="💰 URL остатка", callback_data=f"b20:f:{sid}:bal"),
             InlineKeyboardButton(text="🔄 Запросить остаток", callback_data=f"b20:bal:{sid}")],
            [InlineKeyboardButton(text="📷 Сайт-фото (вторичный)", callback_data=f"b20:f:{sid}:img")],
            [InlineKeyboardButton(text="📝 cURL / код (авто-разбор)", callback_data=f"b20:f:{sid}:curl")],
            [InlineKeyboardButton(text="🗑 Очистить слот", callback_data=f"b20:clr:{sid}")],
            [InlineKeyboardButton(text="← К списку слотов", callback_data="b20:menu")],
        ]
    )


def _kb_bulk() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 URL", callback_data="b20:bf:url")],
            [InlineKeyboardButton(text="📷 Сайт-фото (вторичный)", callback_data="b20:bf:img")],
            [InlineKeyboardButton(text="🤖 Модель 1", callback_data="b20:bf:m1"),
             InlineKeyboardButton(text="🤖 Модель 2", callback_data="b20:bf:m2"),
             InlineKeyboardButton(text="🤖 Модель 3", callback_data="b20:bf:m3")],
            [InlineKeyboardButton(text="📥 Залить во все 20 слотов", callback_data="b20:bapply")],
            [InlineKeyboardButton(text="← К списку слотов", callback_data="b20:menu")],
        ]
    )


# --------------------------------------------------------------------------
# screen renderers
# --------------------------------------------------------------------------
def _pool_body() -> str:
    configured = storage.configured_brain_slot_ids()
    active = storage.get_active_model_index()
    bulk = storage.get_brain_bulk()
    active_name = bulk.get(f"model{active}") or f"Модель {active}"
    chain = " → ".join(configured) if configured else "(пусто)"
    return (
        "<b>🧩 20 API-слотов (мозг с авто-переключением)</b>\n\n"
        f"Активная модель: <code>{_esc(active_name)}</code>\n"
        f"Настроено слотов: <b>{len(configured)}</b> / 20\n"
        f"Цепочка переключения: <code>{_esc(chain)}</code>\n\n"
        "✅ — ключ задан · ⚪ — пусто · ⛔ — бюджет исчерпан\n"
        "Когда у активного слота кончается ключ/бюджет — чат сам "
        "переходит на следующий настроенный слот, сохранив историю.\n\n"
        "Выбери слот для настройки или «🧩 Во все 20 слотов» для массовой заливки."
    )


def _slot_body(sid: str) -> str:
    slot = storage.get_brain_slot(sid)
    used = storage.get_brain_slot_tokens_used(sid)
    rem = storage.brain_slot_remaining_tokens(sid)
    rem_line = f"{rem} осталось" if rem is not None else "без лимита"
    models = " / ".join(
        slot.get(f, "") or "—" for f in MODEL_FIELDS
    )
    bal = storage.get_brain_slot_balance(sid)
    if bal["remaining"] is not None:
        import datetime as _dt
        when = (
            _dt.datetime.fromtimestamp(bal["checked_ts"]).strftime("%d.%m %H:%M")
            if bal["checked_ts"] else "?"
        )
        bal_line = (
            f"💰 Остаток (с провайдера): <b>{bal['remaining']:g} {_esc(bal['currency'])}</b> "
            f"<i>(проверено {when})</i>"
        )
    else:
        bal_line = "💰 Остаток (с провайдера): <i>не запрошен</i>"
    return (
        f"<b>🧠 Слот {sid}</b>\n\n"
        f"URL: <code>{_esc(slot.get('base_url')) or '(по умолчанию)'}</code>\n"
        f"📷 Сайт-фото: <code>{_esc(slot.get('image_url')) or '(нет)'}</code>\n"
        f"Модели (1/2/3): <code>{_esc(models)}</code>\n"
        f"API ключ: {'✅ задан' if slot.get('api_key') else '⚠️ пусто'}\n"
        f"Токены (ручной бюджет): использовано <b>{used}</b> · {rem_line}\n"
        f"{bal_line}\n\n"
        "Каждый слот — отдельный ключ. Это и даёт авто-переключение."
    )


def _bulk_body() -> str:
    b = storage.get_brain_bulk()
    return (
        "<b>🧩 Во все 20 слотов</b>\n\n"
        f"URL: <code>{_esc(b.get('base_url')) or '(пусто)'}</code>\n"
        f"📷 Сайт-фото: <code>{_esc(b.get('image_url')) or '(пусто)'}</code>\n"
        f"Модель 1: <code>{_esc(b.get('model1')) or '(пусто)'}</code>\n"
        f"Модель 2: <code>{_esc(b.get('model2')) or '(пусто)'}</code>\n"
        f"Модель 3: <code>{_esc(b.get('model3')) or '(пусто)'}</code>\n\n"
        "«📥 Залить» запишет URL и 3 модели во <b>все 20 слотов</b>. "
        "API-ключи не трогаются — их вводи в каждом слоте отдельно "
        "(иначе переключение по ключам не имеет смысла).\n\n"
        "Сколько моделей заполнишь — столько кнопок выбора будет сверху меню."
    )


async def _edit(query: CallbackQuery, body: str, kb: InlineKeyboardMarkup) -> None:
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(body, reply_markup=kb, parse_mode="HTML")


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------
def build_brain20_router() -> Router:
    router = Router(name="brain20")

    @router.callback_query(F.data == "b20:menu")
    async def cb_menu(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await _edit(query, _pool_body(), _kb_pool())
        await query.answer()

    @router.callback_query(F.data.startswith("b20:slot:"))
    async def cb_slot(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        sid = query.data.split(":")[2]
        if sid not in SLOT_IDS:
            await query.answer("Неизвестный слот.", show_alert=True)
            return
        await state.clear()
        await _edit(query, _slot_body(sid), _kb_slot(sid))
        await query.answer()

    @router.callback_query(F.data.startswith("b20:f:"))
    async def cb_field(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        _, _, sid, token = query.data.split(":")
        if sid not in SLOT_IDS or token not in _SLOT_FIELDS:
            await query.answer("Неизвестное поле.", show_alert=True)
            return
        field, prompt = _SLOT_FIELDS[token]
        await state.update_data(b20_scope="slot", b20_sid=sid, b20_field=field)
        await state.set_state(Brain20States.awaiting_value)
        if query.message is not None:
            await query.message.answer(prompt.format(sid=sid), parse_mode="HTML")
        await query.answer()

    @router.callback_query(F.data.startswith("b20:clr:"))
    async def cb_clear(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        sid = query.data.split(":")[2]
        storage.clear_brain_slot(sid)
        await state.clear()
        await _edit(query, _slot_body(sid), _kb_slot(sid))
        await query.answer("Слот очищен.")

    @router.callback_query(F.data.startswith("b20:tok:"))
    async def cb_reset_tok(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        sid = query.data.split(":")[2]
        storage.reset_brain_slot_tokens(sid)
        await _edit(query, _slot_body(sid), _kb_slot(sid))
        await query.answer("Счётчик токенов сброшен.")

    @router.callback_query(F.data.startswith("b20:bal:"))
    async def cb_fetch_balance(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        sid = query.data.split(":")[2]
        if sid not in SLOT_IDS:
            await query.answer("Неизвестный слот.", show_alert=True)
            return
        slot = storage.get_brain_slot(sid)
        if not slot.get("api_key"):
            await query.answer("Сначала задай API-ключ слота.", show_alert=True)
            return
        await query.answer("Запрашиваю остаток у провайдера…")
        import asyncio
        try:
            # Network call off the event loop so the bot stays responsive.
            result = await asyncio.to_thread(storage.fetch_brain_slot_balance, sid)
        except Exception as exc:  # noqa: BLE001
            logger.exception("balance fetch failed for slot %s", sid)
            await _edit(query, _slot_body(sid), _kb_slot(sid))
            await query.answer(f"Ошибка запроса: {exc}", show_alert=True)
            return
        await _edit(query, _slot_body(sid), _kb_slot(sid))
        if result.get("ok"):
            await query.answer(
                f"Остаток: {result['remaining']:g} {result.get('currency', '')}".strip(),
                show_alert=True,
            )
        else:
            hint = (result.get("errors") or ["не удалось"])[0]
            await query.answer(
                f"Не получил остаток. {hint}\n"
                "Подсказка: укажи точный «URL остатка» в слоте.",
                show_alert=True,
            )

    @router.callback_query(F.data.startswith("b20:am:"))
    async def cb_active_model(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        idx = query.data.split(":")[2]
        try:
            storage.set_active_model_index(idx)
        except ValueError:
            await query.answer("Неизвестная модель.", show_alert=True)
            return
        await _edit(query, _pool_body(), _kb_pool())
        await query.answer(f"Активная модель: {idx}")

    @router.callback_query(F.data == "b20:bulk")
    async def cb_bulk(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await _edit(query, _bulk_body(), _kb_bulk())
        await query.answer()

    @router.callback_query(F.data.startswith("b20:bf:"))
    async def cb_bulk_field(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        token = query.data.split(":")[2]
        if token not in _BULK_FIELDS:
            await query.answer("Неизвестное поле.", show_alert=True)
            return
        field, prompt = _BULK_FIELDS[token]
        await state.update_data(b20_scope="bulk", b20_field=field)
        await state.set_state(Brain20States.awaiting_value)
        if query.message is not None:
            await query.message.answer(prompt, parse_mode="HTML")
        await query.answer()

    @router.callback_query(F.data == "b20:bapply")
    async def cb_bulk_apply(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        n = storage.apply_brain_bulk_to_all()
        await _edit(query, _bulk_body(), _kb_bulk())
        await query.answer(f"URL и модели залиты в {n} слотов. Ключи введи в каждом слоте.", show_alert=True)

    @router.callback_query(F.data == "b20:import")
    async def cb_import(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(Brain20States.awaiting_import)
        if query.message is not None:
            await query.message.answer(
                "<b>📥 Массовый импорт всех 20 слотов</b>\n\n"
                "Пришли одним сообщением. Поддерживаю 3 формата:\n\n"
                "<b>1) JSON</b>\n"
                "<code>{\n"
                '  "active_model": "1",\n'
                '  "bulk": {"base_url":"https://api.x/v1","model1":"claude opus 4.8","model2":"gpt 5.5","model3":"gemini 3.5 flash"},\n'
                '  "slots": {\n'
                '    "1": {"api_key":"sk-...","url":"https://api.x/v1","model1":"gpt 5.5","budget":100000},\n'
                '    "2": {"api_key":"sk-...","url":"https://api.y/v1"}\n'
                "  }\n"
                "}</code>\n\n"
                "<b>2) JSON-массив</b> (позиция = номер слота):\n"
                '<code>[{"api_key":"sk-1","model1":"gpt 5.5"},{"api_key":"sk-2"}]</code>\n\n'
                "<b>3) По строкам</b> (1 слот = 1 строка):\n"
                "<code>1 | key=sk-...; url=https://api.x/v1; model1=gpt 5.5; budget=100000\n"
                "2 | key=sk-...; url=https://api.y/v1</code>\n\n"
                "Ключи (key/api), url, model1/2/3, budget, curl. Сообщение удалю после импорта.",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await query.answer()

    @router.message(Brain20States.awaiting_import, F.text & ~F.text.startswith("/"))
    async def capture_import(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        blob = message.text or ""
        await state.clear()
        with contextlib.suppress(Exception):
            await message.delete()  # payload contains API keys
        report = storage.import_brain_slots(blob)
        lines = ["<b>📥 Массовый импорт — результат</b>", ""]
        if report.get("applied"):
            lines.append(f"✅ Применено ({len(report['applied'])}):")
            lines += [f"• {a}" for a in report["applied"][:25]]
            if len(report["applied"]) > 25:
                lines.append(f"…и ещё {len(report['applied']) - 25}")
        else:
            lines.append("⚠️ Ничего не применено.")
        if report.get("errors"):
            lines.append("")
            lines.append("Ошибки:")
            lines += [f"• {e}" for e in report["errors"][:15]]
        await message.answer("\n".join(lines), reply_markup=_kb_pool(), parse_mode="HTML")
        await message.answer(_pool_body(), reply_markup=_kb_pool(), parse_mode="HTML")

    @router.message(Brain20States.awaiting_value, F.text & ~F.text.startswith("/"))
    async def capture_value(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        data = await state.get_data()
        scope = data.get("b20_scope")
        field = data.get("b20_field")
        value = (message.text or "").strip()
        await state.clear()

        if scope == "bulk":
            storage.set_brain_bulk_field(field, value)
            await message.answer("Сохранено в шаблон.", reply_markup=_kb_bulk())
            await message.answer(_bulk_body(), reply_markup=_kb_bulk(), parse_mode="HTML")
            return

        sid = data.get("b20_sid")
        if sid not in SLOT_IDS:
            await message.answer("Слот потерян, открой /setup → 🧠 → 🧩 заново.")
            return
        if field == "curl":
            # store raw cURL (as the old menu did) and auto-extract url/key/model
            with contextlib.suppress(Exception):
                await message.delete()  # may contain a key
            storage.set_brain_slot_field(sid, "curl", value)
            found = _parse_curl(value)
            for f, v in found.items():
                storage.set_brain_slot_field(sid, f, v)
            storage.set_brain_slot_field(sid, "provider", "custom")
            extracted = ", ".join(
                {"api_key": "ключ", "base_url": "URL", "model1": "модель"}[k]
                for k in found
            ) or "ничего не распознал — заполни поля вручную"
            await message.answer(
                f"📝 cURL сохранён. Авто-разбор: {extracted}.",
                reply_markup=_kb_slot(sid),
            )
            await message.answer(_slot_body(sid), reply_markup=_kb_slot(sid), parse_mode="HTML")
            return
        if field == "api_key":
            # delete the message so the key isn't left in chat history
            with contextlib.suppress(Exception):
                await message.delete()
        storage.set_brain_slot_field(sid, field, value)
        # provider for pool slots is always a custom OpenAI-compatible endpoint
        with contextlib.suppress(Exception):
            storage.set_brain_slot_field(sid, "provider", "custom")
        await message.answer(_slot_body(sid), reply_markup=_kb_slot(sid), parse_mode="HTML")

    return router
