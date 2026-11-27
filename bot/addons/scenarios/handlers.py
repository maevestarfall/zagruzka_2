"""Telegram UI for «🧭 Сценарий» — routes between two sites (ТЗ rev.7.1 §4).

Callbacks (namespace ``scn:``):
    scn:menu           — меню сценариев (список + «Новый маршрут»)
    scn:new            — начать создание (LLM-планировщик, §5)
    scn:help           — инструкция «Как описать действия» (§16.1)
    scn:run:<id>       — запустить маршрут (§7)
    scn:demo:<id>      — демонстрация по стадиям (§9)
    scn:export:<id>    — выгрузить JSON + .md (§10)
    scn:del:<id>       — удалить
    scn:cancel:<id>    — отменить исполнение (§15.6)
    scn:confirm / scn:discard — подтверждение плана после LLM
    scn:import         — импорт ранее выгруженного JSON (§10)
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import time

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...access import can_use
from . import planner, runner, store
from .models import Route, validate_route
from .presets import V0_PRESET_ID, build_v0_route

logger = logging.getLogger(__name__)

# Active runs per chat: chat_id -> cancel token (§15.6).
_active_runs: dict[int, object] = {}


class ScenarioStates(StatesGroup):
    awaiting_description = State()
    awaiting_import = State()


# §16.1 — инструкция-шаблон (также преамбула экспорта .md, §10).
HELP_TEXT = (
    "<b>Опишите маршрут по шагам, как объясняли бы человеку. "
    "На каждый шаг — одно действие:</b>\n\n"
    "1. <b>Куда зайти</b> — дайте ссылку: «зайди на https://…».\n"
    "2. <b>Что нажать</b> — назовите кнопку <b>точно тем текстом, который "
    "на ней написан</b>: «нажми кнопку \"Sign Up\"». Регистр и язык важны — "
    "пишите как на сайте.\n"
    "3. <b>Куда ввести</b> — назовите поле по подписи или подсказке внутри "
    "него: «в поле \"Email Address\" введи …», «в поле с подсказкой "
    "\"name@work-email.com\" введи …».\n"
    "4. <b>Что скопировать</b> — скажите, что это: «скопируй почту», "
    "«скопируй код из 6 цифр» — бот сам вытащит значение по шаблону.\n"
    "5. <b>Откуда взять и куда вставить</b> — связывайте шаги: «вставь "
    "почту, которую скопировал на первом сайте».\n"
    "6. <b>Ожидание</b> — если сайту нужно время: «подожди, пока появится "
    "поле для кода».\n\n"
    "<b>Не нужно:</b> знать HTML/селекторы, писать код, указывать "
    "координаты. <b>Нужно:</b> точные тексты кнопок и подписей полей — "
    "откройте сайт и перепишите их как есть."
)

HELP_TEXT_MD = (
    "**Опишите маршрут по шагам, как объясняли бы человеку. "
    "На каждый шаг — одно действие:**\n\n"
    "1. **Куда зайти** — дайте ссылку: «зайди на https://…».\n"
    "2. **Что нажать** — назовите кнопку **точно тем текстом, который на ней "
    "написан**: «нажми кнопку \"Sign Up\"». Регистр и язык важны — пишите как "
    "на сайте.\n"
    "3. **Куда ввести** — назовите поле по подписи или подсказке внутри него: "
    "«в поле \"Email Address\" введи …», «в поле с подсказкой "
    "\"name@work-email.com\" введи …».\n"
    "4. **Что скопировать** — скажите, что это: «скопируй почту», «скопируй "
    "код из 6 цифр» — бот сам вытащит значение по шаблону.\n"
    "5. **Откуда взять и куда вставить** — связывайте шаги: «вставь почту, "
    "которую скопировал на первом сайте».\n"
    "6. **Ожидание** — если сайту нужно время: «подожди, пока появится поле "
    "для кода».\n\n"
    "**Не нужно:** знать HTML/селекторы, писать код, указывать координаты. "
    "**Нужно:** точные тексты кнопок и подписей полей — откройте сайт и "
    "перепишите их как есть."
)


# ---- keyboards ----

def _kb_menu(chat_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for route in store.load_all(chat_id):
        title = route.title[:40]
        rows.append([InlineKeyboardButton(
            text=f"▶️ {title}", callback_data=f"scn:card:{route.id}")])
    rows.append([InlineKeyboardButton(
        text="🧪 Пример: v0.app", callback_data="scn:preset:v0")])
    rows.append([InlineKeyboardButton(
        text="➕ Новый маршрут", callback_data="scn:new")])
    rows.append([InlineKeyboardButton(
        text="📥 Импорт JSON", callback_data="scn:import")])
    rows.append([InlineKeyboardButton(
        text="❓ Как описать действия", callback_data="scn:help")])
    rows.append([InlineKeyboardButton(
        text="↩️ Назад", callback_data="brws:hub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_card(route_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить", callback_data=f"scn:run:{route_id}")],
        [InlineKeyboardButton(text="🎬 Демонстрация", callback_data=f"scn:demo:{route_id}")],
        [InlineKeyboardButton(text="📤 Экспорт", callback_data=f"scn:export:{route_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"scn:del:{route_id}")],
        [InlineKeyboardButton(text="↩️ К списку", callback_data="scn:menu")],
    ])


def _kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="scn:confirm"),
         InlineKeyboardButton(text="❌ Отбросить", callback_data="scn:discard")],
    ])


def _kb_cancel(route_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Отмена", callback_data=f"scn:cancel:{route_id}")],
    ])


# ---- rendering ----

def _render_route(route: Route) -> str:
    lines = [f"🧭 <b>{html.escape(route.title)}</b>\n"]
    lines.append(f"Сайт А: {html.escape(route.site_a or '—')}")
    if route.site_b:
        lines.append(f"Сайт Б: {html.escape(route.site_b)}")
    lines.append("")
    for stage in route.stages:
        lines.append(f"<b>{stage.index}) {html.escape(stage.title)}</b>")
        for step in stage.steps:
            note = step.note or f"{step.op} {step.target or ''}"
            lines.append(f"   • [{step.page}] {html.escape(str(note))}")
    text = "\n".join(lines)
    return text[:3900]


def _render_stage_start(stage, total: int) -> str:
    # §9: «сначала → потом» — явный порядок движений.
    lines = [f"🎬 <b>Стадия {stage.index}/{total}: "
             f"{html.escape(stage.title)}</b>\n"]
    order_words = ["сначала", "затем", "потом", "после этого", "далее", "в конце"]
    for i, step in enumerate(stage.steps):
        word = order_words[min(i, len(order_words) - 1)]
        note = step.note or f"{step.op} {step.target or ''}"
        lines.append(f"{i + 1}. {word} — [{step.page}] "
                     f"{html.escape(str(note))}")
    return "\n".join(lines)[:3900]


# ---- router ----

def build_scenarios_router() -> Router:
    router = Router(name="scenarios")

    def _ok(q: CallbackQuery) -> bool:
        return can_use(q.from_user.id if q.from_user else None)

    # ── menu ──
    @router.callback_query(F.data == "scn:menu")
    async def cb_menu(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_text(
                "🧭 <b>Сценарий: маршрут между двумя сайтами</b>\n\n"
                "Опишите словами цепочку действий (почта с сайта А → форма на "
                "сайте Б → код обратно с А), или запустите сохранённый маршрут.",
                parse_mode="HTML",
                reply_markup=_kb_menu(q.message.chat.id),
                disable_web_page_preview=True,
            )
        await q.answer()

    # ── help (§16.1) ──
    @router.callback_query(F.data == "scn:help")
    async def cb_help(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await q.message.answer(HELP_TEXT, parse_mode="HTML",
                               disable_web_page_preview=True)
        await q.answer()

    # ── preset (§16.3) ──
    @router.callback_query(F.data == "scn:preset:v0")
    async def cb_preset(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        chat_id = q.message.chat.id
        route = build_v0_route()
        store.save(chat_id, route)
        await q.message.answer(
            _render_route(route) +
            "\n\n⚠️ Перед запуском замените {mail_url} на ссылку вашей почты "
            "(экспортируйте JSON, поправьте, импортируйте обратно — или "
            "опишите маршрут словами через «Новый маршрут»).",
            parse_mode="HTML",
            reply_markup=_kb_card(route.id),
            disable_web_page_preview=True,
        )
        await q.answer("Пример добавлен")

    # ── card ──
    @router.callback_query(F.data.startswith("scn:card:"))
    async def cb_card(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        route_id = q.data.split(":", 2)[2]
        route = store.load(q.message.chat.id, route_id)
        if not route:
            await q.answer("Маршрут не найден.", show_alert=True)
            return
        with contextlib.suppress(Exception):
            await q.message.edit_text(_render_route(route), parse_mode="HTML",
                                      reply_markup=_kb_card(route.id),
                                      disable_web_page_preview=True)
        await q.answer()

    # ── new route via LLM (§5) ──
    @router.callback_query(F.data == "scn:new")
    async def cb_new(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(ScenarioStates.awaiting_description)
        await q.message.answer(
            "📝 Опишите маршрут словами (см. «Как описать действия»).\n\n"
            "Например: «Зайди на сайт А (моя почта по ссылке https://…), "
            "скопируй адрес почты. Открой v0.app во второй вкладке, нажми "
            "\"Sign Up\", в поле \"Email Address\" вставь почту, нажми "
            "\"Continue with Email\". Вернись на почту, дождись письма с кодом "
            "из 6 цифр, скопируй код. Вернись на v0.app, вставь код и "
            "подтверди.»",
            disable_web_page_preview=True,
        )
        await q.answer()

    @router.message(StateFilter(ScenarioStates.awaiting_description), F.text)
    async def msg_description(message: Message, state: FSMContext) -> None:
        if not can_use(message.from_user.id if message.from_user else None):
            return
        await state.clear()
        prog = await message.answer("🧠 Строю маршрут…")
        try:
            route, problems, raw = await planner.plan_route(message.text)
        except Exception as e:  # noqa: BLE001
            logger.exception("scenarios: planner failed")
            await prog.edit_text(f"❌ Планировщик упал: {html.escape(str(e))}",
                                 parse_mode="HTML")
            return
        if route is None:
            # §5.2 — показать сырой план на ручную правку.
            await prog.edit_text(
                "❌ Модель не вернула валидный JSON. Сырой ответ ниже — "
                "поправьте его и пришлите через «Импорт JSON».")
            await message.answer_document(
                BufferedInputFile((raw or "").encode("utf-8"),
                                  filename="raw_plan.txt"))
            return
        text = _render_route(route)
        if problems:
            text += ("\n\n⚠️ <b>Валидатор нашёл проблемы:</b>\n" +
                     "\n".join(f"• {html.escape(p)}" for p in problems))[:3900]
        await state.update_data(pending_route=route.to_dict())
        with contextlib.suppress(Exception):
            await prog.delete()
        await message.answer(text, parse_mode="HTML",
                             reply_markup=_kb_confirm(),
                             disable_web_page_preview=True)

    @router.callback_query(F.data == "scn:confirm")
    async def cb_confirm(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        data = await state.get_data()
        pending = data.get("pending_route")
        if not pending:
            await q.answer("Нет плана на подтверждение.", show_alert=True)
            return
        route = Route.from_dict(pending)
        store.sanitize_route_secrets(route)   # §11 — секреты в vault
        store.save(q.message.chat.id, route)
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_reply_markup(reply_markup=_kb_card(route.id))
        await q.answer("Сохранено")

    @router.callback_query(F.data == "scn:discard")
    async def cb_discard(q: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_reply_markup(reply_markup=None)
        await q.answer("Отброшено")

    # ── import (§10) ──
    @router.callback_query(F.data == "scn:import")
    async def cb_import(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(ScenarioStates.awaiting_import)
        await q.message.answer(
            "📥 Пришлите файл JSON (ранее выгруженный экспортом) "
            "или сам JSON текстом.")
        await q.answer()

    @router.message(StateFilter(ScenarioStates.awaiting_import))
    async def msg_import(message: Message, state: FSMContext) -> None:
        if not can_use(message.from_user.id if message.from_user else None):
            return
        raw = None
        if message.document:
            try:
                buf = await message.bot.download(message.document)
                raw = buf.read().decode("utf-8")
            except Exception as e:  # noqa: BLE001
                await message.answer(f"❌ Не удалось скачать файл: {e}")
                return
        elif message.text:
            raw = message.text
        if not raw:
            await message.answer("Пришлите JSON файлом или текстом.")
            return
        try:
            route = Route.from_json(raw)
        except Exception as e:  # noqa: BLE001
            await message.answer(f"❌ Невалидный JSON: {e}")
            return
        await state.clear()
        problems = validate_route(route)
        store.save(message.chat.id, route)
        text = _render_route(route)
        if problems:
            text += ("\n\n⚠️ <b>Замечания валидатора:</b>\n" +
                     "\n".join(f"• {html.escape(p)}" for p in problems))[:3900]
        await message.answer(text, parse_mode="HTML",
                             reply_markup=_kb_card(route.id),
                             disable_web_page_preview=True)

    # ── export (§10) ──
    @router.callback_query(F.data.startswith("scn:export:"))
    async def cb_export(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        route_id = q.data.split(":", 2)[2]
        route = store.load(q.message.chat.id, route_id)
        if not route:
            await q.answer("Маршрут не найден.", show_alert=True)
            return
        base = f"route_{route.id}"
        await q.message.answer_document(
            BufferedInputFile(route.to_json().encode("utf-8"),
                              filename=f"{base}.json"),
            caption="JSON маршрута — полностью воспроизводим, можно "
                    "импортировать обратно.")
        md = route.to_markdown(preamble=HELP_TEXT_MD)   # §16.1 как преамбула
        await q.message.answer_document(
            BufferedInputFile(md.encode("utf-8"), filename=f"{base}.md"),
            caption="Человекочитаемое описание стадий.")
        await q.answer()

    # ── delete ──
    @router.callback_query(F.data.startswith("scn:del:"))
    async def cb_del(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        route_id = q.data.split(":", 2)[2]
        store.delete(q.message.chat.id, route_id)
        with contextlib.suppress(Exception):
            await q.message.edit_text(
                "🗑 Маршрут удалён.", reply_markup=_kb_menu(q.message.chat.id))
        await q.answer("Удалено")

    # ── cancel (§15.6) ──
    @router.callback_query(F.data.startswith("scn:cancel:"))
    async def cb_cancel(q: CallbackQuery) -> None:
        token = _active_runs.get(q.message.chat.id)
        if token is None:
            await q.answer("Нет активного маршрута.", show_alert=True)
            return
        token.cancel()
        await q.answer("Отменяю…")

    # ── run (§7) ──
    @router.callback_query(F.data.startswith("scn:run:"))
    async def cb_run(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        route_id = q.data.split(":", 2)[2]
        await q.answer()
        await _execute(q.message, route_id, demo=False)

    # ── demo (§9) ──
    @router.callback_query(F.data.startswith("scn:demo:"))
    async def cb_demo(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        route_id = q.data.split(":", 2)[2]
        await q.answer()
        await _execute(q.message, route_id, demo=True)

    async def _execute(message: Message, route_id: str, *, demo: bool) -> None:
        chat_id = message.chat.id
        if chat_id in _active_runs:
            await message.answer("⚠️ Уже идёт маршрут — сначала отмените его.")
            return
        route = store.load(chat_id, route_id)
        if not route:
            await message.answer("Маршрут не найден.")
            return
        if "{" in (route.site_a or ""):
            await message.answer(
                "⚠️ В маршруте остались плейсхолдеры (например {mail_url}). "
                "Заполните их через экспорт/импорт JSON или пересоздайте "
                "маршрут с реальными ссылками.")
            return
        problems = validate_route(route)
        if problems:
            await message.answer(
                "❌ Валидатор не пропустил маршрут:\n" +
                "\n".join(f"• {p}" for p in problems))
            return

        route.vars = {}
        token = runner.make_cancel_token()
        _active_runs[chat_id] = token
        total = len(route.stages)
        status = await message.answer(
            f"🚀 Запускаю «{route.title}» ({total} стадий)…",
            reply_markup=_kb_cancel(route.id))

        async def on_stage(phase: str, stage, screenshot: str | None = None) -> None:
            if phase == "start":
                if demo:
                    await message.answer(_render_stage_start(stage, total),
                                         parse_mode="HTML")
            elif phase == "done":
                if demo and screenshot and os.path.exists(screenshot):
                    with contextlib.suppress(Exception):
                        await message.answer_photo(
                            FSInputFile(screenshot),
                            caption=f"✅ Стадия {stage.index}/{total} выполнена: "
                                    f"{stage.title}"[:1024])
                        return
                if demo:
                    await message.answer(
                        f"✅ Стадия {stage.index}/{total} выполнена: "
                        f"{stage.title}")

        try:
            result = await runner.run_route(
                route,
                on_stage=on_stage if demo else None,
                cancel=token,
            )
        finally:
            _active_runs.pop(chat_id, None)

        if result.get("ok"):
            note = ""
            if not result.get("session_persist", True):
                # §15.7 — честное предупреждение для nodriver.
                note = ("\n\n⚠️ Движок nodriver: сессия (cookies/логин) "
                        "НЕ сохраняется между запусками.")
            vars_ = result.get("vars") or {}
            vars_str = "\n".join(
                f"• {k} = {str(v)[:80]}" for k, v in vars_.items())
            await status.edit_text(
                f"✅ Маршрут «{route.title}» выполнен "
                f"({len(result.get('stages_done') or [])}/{total} стадий)."
                + (f"\n\nПеременные:\n{vars_str}" if vars_str else "")
                + note)
        elif result.get("cancelled"):
            await status.edit_text("⛔ Маршрут отменён.")
        else:
            await status.edit_text(
                f"❌ Маршрут остановлен: {result.get('error')}"[:4000])

    return router
