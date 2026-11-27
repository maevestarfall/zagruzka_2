# «➕ Улучшить проект из Важного» — UI/handlers (Регенерация + пульт Гермеса).
# Co-authored with CoCo
"""Кнопки и поток: предложение Гермеса → «Улучшить» (Кодер) → авто/тайминг."""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import store
from . import engine

logger = logging.getLogger(__name__)


class ImpjStates(StatesGroup):
    awaiting_time = State()
    awaiting_refine = State()


async def _owner(obj) -> bool:
    try:
        from ...wizard import _is_owner
        return _is_owner(obj.from_user.id if obj.from_user else 0)
    except Exception:  # noqa: BLE001
        return False


def _home_kb() -> InlineKeyboardMarkup:
    auto = "🔁 Авто: ВКЛ" if store.get_auto() else "🔁 Авто: ВЫКЛ"
    rows = [[InlineKeyboardButton(text="🔎 Предложить улучшения (Гермес)", callback_data="impj:propose")]]
    if store.get_last_proposal():
        rows.append([InlineKeyboardButton(text="✅ Улучшить (отдать Кодеру)", callback_data="impj:apply")])
    rows.append([InlineKeyboardButton(text=auto, callback_data="impj:auto")])
    rows.append([InlineKeyboardButton(text="⏱ Тайминг (в управлении Гермесом)", callback_data="impj:timing")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="main:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _solution_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Исправляй с этим", callback_data="impj:apply")],
        [InlineKeyboardButton(text="✍️ Дополнить (что не надо / только часть)", callback_data="impj:refine")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="impj:home")],
    ])


def _home_text() -> str:
    return (
        "<b>➕ Улучшить проект из «❗Важно»</b>\n\n"
        "Гермес читает «❗Важно» + карту проекта, исследует по инструкции и "
        "предлагает: «могу сделать X · было так → станет так · плюсы».\n"
        "Потом жмёшь <b>«✅ Улучшить»</b> — он берёт проект и правит через Кодер.\n\n"
        f"Авто-применение без вопросов: <b>{'ВКЛ' if store.get_auto() else 'ВЫКЛ'}</b>."
    )


def _timing_kb() -> InlineKeyboardMarkup:
    s = store.get_schedule()
    what = "ошибки" if s["what"] == "errors" else "инструкция"
    freq = {"off": "выкл", "daily": "каждый день", "weekly": "каждую неделю"}[s["freq"]]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎯 Что улучшать: {what}", callback_data="impj:tim:what")],
        [InlineKeyboardButton(text=f"📅 Частота: {freq}", callback_data="impj:tim:freq")],
        [InlineKeyboardButton(text=f"⏰ Время: {s['time']}", callback_data="impj:tim:time")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="impj:home")],
    ])


def _timing_text() -> str:
    return (
        "<b>⏱ Тайминг авто-улучшений</b>\n\n"
        "Выбери, ЧТО улучшать (искать ошибки или улучшать инструкцию), КАК часто "
        "(каждый день / каждую неделю / выкл) и ВО СКОЛЬКО. В назначенное время "
        "Гермес сам запустит задачу (если «Авто» ВКЛ — применит; иначе пришлёт "
        "предложение)."
    )


async def _render_proposal(message: Message, text: str) -> None:
    from ...handlers import _send_long
    with contextlib.suppress(Exception):
        await _send_long(message, "🔎 <b>Предложения Гермеса:</b>\n\n" + text, code=False)
    with contextlib.suppress(Exception):
        await message.answer("Что дальше?", reply_markup=_home_kb())


def build_improveproj_router() -> Router:
    router = Router(name="improveproj")

    @router.callback_query(F.data == "impj:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        if query.message is not None:
            await query.message.answer(_home_text(), parse_mode="HTML", reply_markup=_home_kb())

    @router.callback_query(F.data == "impj:propose")
    async def _propose(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer("🔎 Гермес анализирует…")
        msg = query.message
        if msg is None:
            return
        # 1) Гермес видит проблему / формулирует улучшение.
        text = await engine.propose()
        store.set_last_proposal(text)
        store.set_refinement("")  # сброс прошлого «дополнения»
        with contextlib.suppress(Exception):
            from ...handlers import _send_long
            await _send_long(msg, "🔎 <b>Гермес нашёл, что можно улучшить:</b>\n\n" + text, code=False)
        # 2) Отдаёт проблему Анализатору ПРО и ждёт решение.
        await msg.answer("🛰 Отправляю запрос Анализатору ПРО и жду решение…")
        status, task_id, solution = await engine.research_via_analyzer(text)
        if status == "completed" and solution.strip():
            store.set_last_solution(solution)
            with contextlib.suppress(Exception):
                from ...handlers import _send_long
                await _send_long(msg, "🛰 <b>Анализатор ПРО нашёл решение:</b>\n\n" + solution, code=False)
            head = "Гермес: нашёл решение. Как чинить?"
        elif status == "pending":
            store.set_last_solution("")
            head = (f"🛰 Анализатор ПРО ещё думает (задача {task_id}). Решение придёт в "
                    "🔬 Анализатор ПРО → Задачи. Можно чинить и без него.")
        elif status == "no_server":
            store.set_last_solution("")
            head = (f"🛰 Анализатор ПРО не подключён ({solution}). Чиню по знаниям кодера "
                    "и «❗Важно».")
        else:
            store.set_last_solution("")
            head = f"🛰 Анализатор ПРО не дал решение ({solution or status}). Можно чинить и так."
        # 3) Авто → сразу к Кодеру; иначе кнопки.
        if store.get_auto():
            await msg.answer("🔁 Авто ВКЛ — сразу чиню через Кодера…")
            await engine.apply_real(text, msg.bot, msg.chat.id)
            return
        await msg.answer(head, reply_markup=_solution_kb())

    @router.callback_query(F.data == "impj:apply")
    async def _apply(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        prop = store.get_last_proposal()
        if not prop:
            await query.answer("Сначала «Предложить улучшения».", show_alert=True)
            return
        await query.answer("✅ Отдаю Кодеру…")
        msg = query.message
        if msg is not None:
            await engine.apply_real(prop, msg.bot, msg.chat.id)

    @router.callback_query(F.data == "impj:auto")
    async def _auto(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        store.set_auto(not store.get_auto())
        await query.answer(f"Авто: {'ВКЛ' if store.get_auto() else 'ВЫКЛ'}")
        with contextlib.suppress(Exception):
            await query.message.edit_reply_markup(reply_markup=_home_kb())

    # ── «Дополнить» (scope: что НЕ делать / только часть) ────────────
    @router.callback_query(F.data == "impj:refine")
    async def _refine(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(ImpjStates.awaiting_refine)
        await query.answer()
        await query.message.answer(
            "✍️ Напиши, что <b>НЕ надо</b> делать или <b>только какую часть</b> "
            "решения применить. Учту это при починке.", parse_mode="HTML")

    @router.message(ImpjStates.awaiting_refine, F.text)
    async def _refine_set(message: Message, state: FSMContext) -> None:
        if not await _owner(message):
            return
        await state.clear()
        store.set_refinement((message.text or "").strip())
        await message.answer(
            "✍️ Дополнение сохранено — учту при починке.", reply_markup=_solution_kb())

    # ── Тайминг ──────────────────────────────────────────────────────
    @router.callback_query(F.data == "impj:timing")
    async def _timing(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_timing_text(), parse_mode="HTML", reply_markup=_timing_kb())

    @router.callback_query(F.data == "impj:tim:what")
    async def _tim_what(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        cur = store.get_schedule()["what"]
        store.set_schedule(what="instruction" if cur == "errors" else "errors")
        await query.answer("Обновлено")
        with contextlib.suppress(Exception):
            await query.message.edit_reply_markup(reply_markup=_timing_kb())

    @router.callback_query(F.data == "impj:tim:freq")
    async def _tim_freq(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        order = {"off": "daily", "daily": "weekly", "weekly": "off"}
        store.set_schedule(freq=order[store.get_schedule()["freq"]])
        await query.answer("Обновлено")
        with contextlib.suppress(Exception):
            await query.message.edit_reply_markup(reply_markup=_timing_kb())

    @router.callback_query(F.data == "impj:tim:time")
    async def _tim_time(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(ImpjStates.awaiting_time)
        await query.answer()
        await query.message.answer("⏰ Пришли время в формате ЧЧ:ММ (например 09:30).")

    @router.message(ImpjStates.awaiting_time, F.text)
    async def _tim_time_set(message: Message, state: FSMContext) -> None:
        if not await _owner(message):
            return
        import re
        t = (message.text or "").strip()
        await state.clear()
        if not re.match(r"^\d{1,2}:\d{2}$", t):
            await message.answer("Формат ЧЧ:ММ, например 09:30. Попробуй ещё раз через кнопку.")
            return
        store.set_schedule(time=t)
        await message.answer(f"⏰ Время установлено: {t}", reply_markup=_timing_kb())

    return router


async def run_scheduled(bot, chat_id: int) -> None:
    """Called by heartbeat when the schedule is due. Propose (+ auto-apply)."""
    store.mark_ran()
    with contextlib.suppress(Exception):
        text = await engine.propose()
        store.set_last_proposal(text)
        await bot.send_message(chat_id, "⏱ Плановое улучшение — предложения Гермеса:")
        await bot.send_message(chat_id, text[:3500])
        if store.get_auto():
            await engine.apply_real(text, bot, chat_id)
