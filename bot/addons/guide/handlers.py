# «🧭 Гид» — Telegram screen: mint the link + open buttons (VISION-style).
# Co-authored with CoCo
"""Owner opens «🧭 Гид» → gets a token link to the step-by-step web wizard."""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from . import store

logger = logging.getLogger(__name__)


async def _owner(query: CallbackQuery) -> bool:
    try:
        from ...wizard import _is_owner
        return _is_owner(query.from_user.id if query.from_user else 0)
    except Exception:  # noqa: BLE001
        return False


def _kb() -> InlineKeyboardMarkup:
    if store.is_enabled() and store.get_link():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть гид", url=store.get_link())],
            [InlineKeyboardButton(text="🔄 Новая ссылка", callback_data="guide:rotate")],
            [InlineKeyboardButton(text="🔴 Выключить", callback_data="guide:off")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="misc:home")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Включить гид", callback_data="guide:on")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="misc:home")],
    ])


def _text() -> str:
    return (
        "<b>🧭 Гид по настройке</b>\n\n"
        "Открой ссылку в браузере — там пошаговый мастер: жмёшь «Дальше» и "
        "идёшь по шагам (GitHub → деплой → панель → мозг → проверка) с "
        "прогресс-баром."
    )


def build_guide_router() -> Router:
    router = Router(name="guide")

    async def _show(query: CallbackQuery) -> None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(_text(), parse_mode="HTML", reply_markup=_kb())

    @router.callback_query(F.data == "guide:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        if query.message is not None:
            await query.message.answer(_text(), parse_mode="HTML", reply_markup=_kb())

    @router.callback_query(F.data.in_({"guide:on", "guide:rotate"}))
    async def _mint(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        try:
            store.generate_link()
            await query.answer("🔗 Ссылка готова")
        except Exception:  # noqa: BLE001
            await query.answer("Ошибка генерации ссылки", show_alert=True)
            return
        await _show(query)

    @router.callback_query(F.data == "guide:off")
    async def _off(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        store.disable_link()
        await query.answer("🔴 Гид выключен")
        await _show(query)

    return router
