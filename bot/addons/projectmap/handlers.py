# Project-map addon: graphify settings screen (use / auto) + base import.
# Co-authored with CoCo
"""«🗺 graphify» settings, reachable from «📜 Инструкции».

Buttons the owner asked for:
* «Использовать» — toggle feeding the map TEXT into the AI prompt.
* «Авто» ВКЛ/ВЫКЛ — toggle automatic rebuild + Hermes analysis.
Plus import of an exported base (JSON) so the curated «связки» can be
carried into another identical project. Export/rebuild live on the main
«🗺 Карта проекта» screen (wizard).
"""

from __future__ import annotations

import contextlib
import json
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import store

logger = logging.getLogger(__name__)


class PmgStates(StatesGroup):
    awaiting_import = State()


async def _owner(query: CallbackQuery) -> bool:
    try:
        from ...wizard import _is_owner
        return _is_owner(query.from_user.id if query.from_user else 0)
    except Exception:  # noqa: BLE001
        return False


def _kb_settings() -> InlineKeyboardMarkup:
    use_lbl = "✅ Использовать карту: ВКЛ" if store.get_use() else "⛔ Использовать карту: ВЫКЛ"
    auto_lbl = "🔁 Авто: ВКЛ" if store.get_auto() else "🔁 Авто: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=use_lbl, callback_data="pmg:use:toggle")],
        [InlineKeyboardButton(text=auto_lbl, callback_data="pmg:auto:toggle")],
        [
            InlineKeyboardButton(text="📥 Импорт базы", callback_data="pmg:import"),
            InlineKeyboardButton(text="📤 Экспорт базы", callback_data="pmg:export"),
        ],
        [InlineKeyboardButton(text="🗺 Открыть карту", callback_data="main:projmap")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="instr:home")],
    ])


def _settings_text() -> str:
    use = "ВКЛ" if store.get_use() else "ВЫКЛ"
    auto = "ВКЛ" if store.get_auto() else "ВЫКЛ"
    n_edges = len(store.get_edges())
    return (
        "<b>🗺 graphify — карта проекта</b>\n\n"
        f"• Использовать карту в ИИ: <b>{use}</b>\n"
        f"  <i>текст связок подмешивается в промпт</i>\n"
        f"• Авто-обновление (+ анализ Гермеса): <b>{auto}</b>\n"
        f"• Курируемых связок в базе: <b>{n_edges}</b>\n\n"
        "«📥 Импорт базы» — загрузить связки из другого такого же проекта.\n"
        "«📤 Экспорт базы» — выгрузить, чтобы вставить в другой проект."
    )


def build_projectmap_router() -> Router:
    router = Router(name="projectmap")

    @router.callback_query(F.data == "pmg:settings")
    async def _settings(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_settings_text(), parse_mode="HTML",
                                          reply_markup=_kb_settings())

    @router.callback_query(F.data == "pmg:use:toggle")
    async def _use_toggle(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        store.set_use(not store.get_use())
        await query.answer("Использование карты обновлено")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_settings_text(), parse_mode="HTML",
                                          reply_markup=_kb_settings())

    @router.callback_query(F.data == "pmg:auto:toggle")
    async def _auto_toggle(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        store.set_auto(not store.get_auto())
        await query.answer("Авто-режим обновлён")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_settings_text(), parse_mode="HTML",
                                          reply_markup=_kb_settings())

    @router.callback_query(F.data == "pmg:export")
    async def _export(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer("📤 Выгружаю базу…")
        from aiogram.types import BufferedInputFile
        payload = json.dumps(store.export_base(), ensure_ascii=False, indent=2)
        doc = BufferedInputFile(payload.encode("utf-8"), filename="projectmap_base.json")
        with contextlib.suppress(Exception):
            await query.message.answer_document(
                doc, caption="🗺 База связок. Вставь её в другом проекте через «📥 Импорт базы».")

    @router.callback_query(F.data == "pmg:import")
    async def _import(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(PmgStates.awaiting_import)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.answer(
                "📥 Пришли файл базы (projectmap_base.json) одним сообщением.\n"
                "Связки будут добавлены к текущим (дубликаты пропускаются).")

    @router.message(PmgStates.awaiting_import, F.document)
    async def _import_file(message: Message, state: FSMContext) -> None:
        await state.clear()
        try:
            from ...wizard import _is_owner
            if not _is_owner(message.from_user.id if message.from_user else 0):
                return
        except Exception:  # noqa: BLE001
            return
        try:
            from aiogram import Bot
            bot: Bot = message.bot
            buf = await bot.download(message.document)
            data = json.loads(buf.read().decode("utf-8"))
            res = store.import_base(data, merge=True)
            await message.answer(
                f"✅ Импорт готов: добавлено {res['added']}, всего связок {res['total']}.")
        except Exception as e:  # noqa: BLE001
            await message.answer(f"⚠️ Не удалось импортировать базу: {e}")

    return router
