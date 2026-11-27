"""Telegram UI for «🎯 Приоритет» (prio:* callbacks)."""

from __future__ import annotations

import contextlib
import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ...access import can_use
from . import store

logger = logging.getLogger(__name__)


def _kb() -> InlineKeyboardMarkup:
    enabled = store.is_enabled()
    mode = store.get_mode()
    toggle = (
        InlineKeyboardButton(text="🟢 Приоритет: ВКЛ", callback_data="prio:off")
        if enabled
        else InlineKeyboardButton(text="🔴 Приоритет: ВЫКЛ", callback_data="prio:on")
    )
    rows: list[list[InlineKeyboardButton]] = [[toggle]]
    for m in store.MODES:
        mark = "✅ " if (enabled and mode == m) else "▫️ "
        rows.append(
            [InlineKeyboardButton(text=mark + store.MODE_LABELS[m], callback_data=f"prio:mode:{m}")]
        )
    rows.append([InlineKeyboardButton(text="📖 Показать инструкцию (дерево)", callback_data="prio:tree")])
    rows.append([InlineKeyboardButton(text="↩️ В настройки", callback_data="main:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _text() -> str:
    enabled = store.is_enabled()
    mode = store.get_mode()
    status = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    cur = store.MODE_LABELS.get(mode, mode) if enabled else "—"
    return (
        "<b>🎯 Приоритет</b>\n\n"
        "Управляет тем, откуда бот берёт решение задачи.\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Режим: <b>{html.escape(cur)}</b>\n\n"
        "• <b>Только возможности бота</b> — бот не лезет в интернет, "
        "работает встроенными функциями.\n"
        "• <b>Сначала бот, потом интернет</b> — пробует свои функции, "
        "затем веб как запас.\n"
        "• <b>Инструкция</b> — идёт строго по дереву возможностей "
        "(жми «Показать инструкцию»).\n\n"
        "<i>Когда ВКЛ — правило передаётся живому ИИ-мозгу бота.</i>"
    )


async def show_screen(query: CallbackQuery) -> None:
    if query.message is None:
        return
    with contextlib.suppress(Exception):
        await query.message.edit_text(_text(), reply_markup=_kb())


async def _gate(query: CallbackQuery) -> bool:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return False
    return True


async def _open(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    await state.clear()
    await query.answer()
    await show_screen(query)


async def _on(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    store.set_enabled(True)
    await query.answer("Приоритет включён")
    await show_screen(query)


async def _off(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    store.set_enabled(False)
    await query.answer("Приоритет выключен")
    await show_screen(query)


async def _set_mode(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    mode = (query.data or "").split(":")[-1]
    store.set_mode(mode)
    if not store.is_enabled():
        store.set_enabled(True)
    await query.answer(f"Режим: {store.MODE_LABELS.get(mode, mode)}")
    await show_screen(query)


async def _tree(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    await query.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="prio:home")]]
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>📖 Инструкция — дерево возможностей</b>\n\n"
                f"<pre>{html.escape(store.CAPABILITY_TREE)}</pre>",
                reply_markup=kb,
            )


def build_priority_router() -> Router:
    router = Router(name="priority_addon")
    router.callback_query.register(_open, F.data == "main:priority")
    router.callback_query.register(_open, F.data == "prio:home")
    router.callback_query.register(_on, F.data == "prio:on")
    router.callback_query.register(_off, F.data == "prio:off")
    router.callback_query.register(_set_mode, F.data.startswith("prio:mode:"))
    router.callback_query.register(_tree, F.data == "prio:tree")
    return router
