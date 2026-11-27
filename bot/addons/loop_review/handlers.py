# Loop-review UI: main-menu screen, run flow, and the auto-after-relay toggle.
# Co-authored with CoCo
"""«🔁 Ревью→фикс→проверка→повтор (GitHub)» — screen + run + toggle."""

from __future__ import annotations

import contextlib
import logging

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

from . import store

logger = logging.getLogger(__name__)


class LrvStates(StatesGroup):
    awaiting_input = State()


async def _owner(obj) -> bool:
    try:
        from ...wizard import _is_owner
        return _is_owner(obj.from_user.id if obj.from_user else 0)
    except Exception:  # noqa: BLE001
        return False


def _kb_home() -> InlineKeyboardMarkup:
    auto = "🔁 Авто-ревью после сборки: ВКЛ" if store.get_enabled() else "🔁 Авто-ревью после сборки: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶ Запустить (код или задача)", callback_data="lrv:run")],
        [InlineKeyboardButton(text=auto, callback_data="lrv:auto")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main:menu")],
    ])


def _home_text() -> str:
    return (
        "<b>🔁 Ревью → фикс → проверка → повтор</b>\n"
        "<i>(идея из github.com/di-sukharev/loop-review)</i>\n\n"
        "Цикл: свежий независимый ревьюер ставит балл 1–10 и находит проблемы → "
        "фиксер правит → берётся новый ревьюер → повтор, пока балл "
        f"≥ <b>{store.get_threshold()}</b> или замечаний нет "
        f"(до {store.get_max_passes()} проходов).\n\n"
        f"Авто-ревью после сборки «🧭 Контекст»: "
        f"<b>{'ВКЛ' if store.get_enabled() else 'ВЫКЛ'}</b>\n\n"
        "«▶ Запустить» — пришли код или задачу, верну улучшенный код + балл."
    )


def build_loop_review_router() -> Router:
    router = Router(name="loop_review")

    @router.callback_query(F.data == "lrv:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML", reply_markup=_kb_home())

    @router.callback_query(F.data == "lrv:auto")
    async def _auto(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        store.set_enabled(not store.get_enabled())
        await query.answer("Авто-ревью обновлено")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML", reply_markup=_kb_home())

    @router.callback_query(F.data == "lrv:run")
    async def _run(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(LrvStates.awaiting_input)
        await query.answer()
        await query.message.answer(
            "▶ Пришли <b>код</b> или <b>задачу</b> одним сообщением — "
            "запущу цикл ревью→фикс→повтор.", parse_mode="HTML")

    @router.message(LrvStates.awaiting_input, F.text)
    async def _got(message: Message, state: FSMContext) -> None:
        if not await _owner(message):
            return
        target = (message.text or "").strip()
        await state.clear()
        if len(target) < 5:
            await message.answer("Слишком коротко. Пришли код или развёрнутую задачу.")
            return
        import asyncio
        asyncio.create_task(_run_and_report(message, target))
        await message.answer("🚀 Запускаю цикл ревью…")

    return router


async def _run_and_report(message: Message, target: str) -> None:
    from .engine import run_loop

    async def progress(t: str) -> None:
        with contextlib.suppress(Exception):
            await message.answer(t, parse_mode="HTML")

    try:
        res = await run_loop(target, progress=progress)
    except Exception as e:  # noqa: BLE001
        logger.exception("loop-review failed")
        with contextlib.suppress(Exception):
            await message.answer(f"❌ Цикл упал: {e}")
        return
    left = res.get("findings_left") or []
    caption = (
        "🔁 <b>Готово</b>\n"
        f"Итоговый балл: <b>{res.get('score', 0):.1f}/10</b> за {res.get('passes', 0)} проход(ов)\n"
        + ("Незакрытых замечаний: " + str(len(left)) if left else "Замечаний не осталось.")
    )
    with contextlib.suppress(Exception):
        doc = BufferedInputFile((res.get("code", "") or "").encode("utf-8"),
                                filename="loop_review_result.txt")
        await message.answer_document(doc, caption=caption, parse_mode="HTML")
