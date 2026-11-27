from __future__ import annotations

import contextlib
import logging
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .. import state as addon_state

logger = logging.getLogger(__name__)
unstructured_router = Router()

ADDON = "unstructured"
DEFAULT_MODE = "auto"

_MODE_LABELS = {
    "on": "🟢 Включено",
    "off": "🔴 Выключено",
    "auto": "🔵 Авто",
}

def get_unstructured_mode() -> str:
    mode = addon_state.get(ADDON, "mode", DEFAULT_MODE)
    if mode not in _MODE_LABELS:
        return DEFAULT_MODE
    return mode

def _set_unstructured_mode(value: str) -> None:
    if value in _MODE_LABELS:
        addon_state.set_(ADDON, "mode", value)

def _kb_unstructured_screen() -> InlineKeyboardMarkup:
    current = get_unstructured_mode()
    
    # Mark current mode
    btn_on_text = "🟢 Включить" + (" (активно)" if current == "on" else "")
    btn_off_text = "🔴 Выключить" + (" (активно)" if current == "off" else "")
    btn_auto_text = "🔵 Авто" + (" (активно)" if current == "auto" else "")
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=btn_on_text, callback_data="unstructured:set:on")],
            [InlineKeyboardButton(text=btn_off_text, callback_data="unstructured:set:off")],
            [InlineKeyboardButton(text=btn_auto_text, callback_data="unstructured:set:auto")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="unstructured:back")],
        ]
    )

def _screen_text() -> str:
    current = get_unstructured_mode()
    current_label = _MODE_LABELS.get(current, current)
    
    return (
        "<b>🔌 Интеграция с Unstructured</b>\n\n"
        "Unstructured — это профессиональная платформа для обработки и структурирования "
        "любых типов неструктурированных данных (PDF, HTML, DOCX, презентации) в чистый "
        "текст и markdown для последующего анализа моделями ИИ.\n\n"
        "<b>Режимы работы:</b>\n"
        "• <b>🟢 Включено</b> — бот будет принудительно использовать Unstructured API для всех входящих документов.\n"
        "• <b>🔴 Выключено</b> — обработка через Unstructured полностью отключена (используются локальные парсеры).\n"
        "• <b>🔵 Авто</b> — бот сам решает, когда использовать Unstructured (например, для сложных PDF-файлов или сканов), "
        "а простые файлы обрабатывает локально.\n\n"
        f"Текущий статус: <b>{current_label}</b>"
    )

@unstructured_router.callback_query(F.data == "main:unstructured")
@unstructured_router.callback_query(F.data == "unstructured:menu")
async def cb_unstructured_menu(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _screen_text(),
                reply_markup=_kb_unstructured_screen()
            )

@unstructured_router.callback_query(F.data.startswith("unstructured:set:"))
async def cb_unstructured_set(query: CallbackQuery) -> None:
    if query.data is None:
        return
    
    mode = query.data.split(":")[-1]
    _set_unstructured_mode(mode)
    
    label = _MODE_LABELS.get(mode, mode)
    await query.answer(f"Режим Unstructured изменен на: {label}")
    
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _screen_text(),
                reply_markup=_kb_unstructured_screen()
            )

@unstructured_router.callback_query(F.data == "unstructured:back")
async def cb_unstructured_back(query: CallbackQuery) -> None:
    await query.answer()
    from ...wizard import _kb_settings_menu
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "Настройки бота. Выбери раздел для изменения:",
                reply_markup=_kb_settings_menu(query.from_user.id),
            )

def build_unstructured_router() -> Router:
    return unstructured_router
