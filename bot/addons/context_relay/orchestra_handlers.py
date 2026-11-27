# «🎼 Оркестр» — UI: 20 slots, per-slot config + relay-role assignment.
# Co-authored with CoCo
"""Screens for the orchestra: a 20-slot list, per-slot API/model config
(entered like «Другое»), and a role picker mapping the slot's model onto
TEMPORAL RELAY roles.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import orchestra as orch

logger = logging.getLogger(__name__)


class OrchStates(StatesGroup):
    awaiting_cfg = State()


async def _owner(obj) -> bool:
    try:
        from ...wizard import _is_owner
        return _is_owner(obj.from_user.id if obj.from_user else 0)
    except Exception:  # noqa: BLE001
        return False


def _kb_home() -> InlineKeyboardMarkup:
    rows = []
    for i in range(orch.MAX_SLOTS):
        mark = "🟢" if orch.is_configured(i) else "⚪"
        rows.append([InlineKeyboardButton(text=f"{mark} {orch.slot_label(i)}",
                                          callback_data=f"orch:slot:{i}")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="main:brain")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _home_text() -> str:
    return (
        "<b>🎼 Оркестр</b>\n\n"
        f"Слотов: {orch.MAX_SLOTS}, настроено: <b>{orch.configured_count()}</b>.\n\n"
        "Каждый слот — своя модель (API/endpoint/model, как «Другое») + роли в "
        "конвейере «🧭 Контекст». Несколько ролей на слот = обслуживает их по "
        "очереди (по фазам)."
    )


def _kb_slot(i: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Задать API / модель", callback_data=f"orch:cfg:{i}")],
        [InlineKeyboardButton(text="🎭 Роли в Контексте", callback_data=f"orch:roles:{i}")],
        [InlineKeyboardButton(text="🗑 Очистить слот", callback_data=f"orch:clear:{i}")],
        [InlineKeyboardButton(text="↩️ К списку", callback_data="orch:home")],
    ])


def _slot_text(i: int) -> str:
    s = orch.get_slot(i)
    roles = orch.get_roles(i)
    rlbl = ", ".join(orch.ROLE_LABEL[r] for r in roles) if roles else "—"
    key = s.get("api_key", "")
    key_disp = (key[:6] + "…") if key else "не задан"
    return (
        f"<b>🎼 Слот {i + 1}</b>\n\n"
        f"API-ключ: <code>{key_disp}</code>\n"
        f"Endpoint: <code>{s.get('base_url','') or 'по умолчанию'}</code>\n"
        f"Модель: <code>{s.get('model','') or 'не задана'}</code>\n"
        f"Роли: <b>{rlbl}</b>"
    )


def _kb_roles(i: int) -> InlineKeyboardMarkup:
    active = set(orch.get_roles(i))
    rows = []
    for key, label in orch.ROLES:
        mark = "✅" if key in active else "⬜"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}",
                                          callback_data=f"orch:role:{i}:{key}")])
    rows.append([InlineKeyboardButton(text="↩️ К слоту", callback_data=f"orch:slot:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_orchestra_router() -> Router:
    router = Router(name="orchestra")

    @router.callback_query(F.data == "orch:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML", reply_markup=_kb_home())

    @router.callback_query(F.data.startswith("orch:slot:"))
    async def _slot(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.clear()
        i = int(query.data.split(":")[-1])
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_slot_text(i), parse_mode="HTML", reply_markup=_kb_slot(i))

    @router.callback_query(F.data.startswith("orch:cfg:"))
    async def _cfg(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        i = int(query.data.split(":")[-1])
        await state.set_state(OrchStates.awaiting_cfg)
        await state.update_data(orch_idx=i)
        await query.answer()
        await query.message.answer(
            f"✏️ Слот {i + 1}. Пришли конфиг одним сообщением в формате:\n"
            "<code>API_KEY | BASE_URL | MODEL</code>\n\n"
            "BASE_URL можно оставить пустым (тогда OpenRouter по умолчанию):\n"
            "<code>sk-xxx | | gpt-4o-mini</code>",
            parse_mode="HTML")

    @router.message(OrchStates.awaiting_cfg, F.text)
    async def _cfg_got(message: Message, state: FSMContext) -> None:
        if not await _owner(message):
            return
        data = await state.get_data()
        i = int(data.get("orch_idx", 0))
        await state.clear()
        parts = [p.strip() for p in (message.text or "").split("|")]
        api_key = parts[0] if len(parts) > 0 else ""
        base_url = parts[1] if len(parts) > 1 else ""
        model = parts[2] if len(parts) > 2 else ""
        if not api_key or not model:
            await message.answer("Нужно минимум API_KEY и MODEL. Формат: KEY | URL | MODEL")
            return
        orch.set_config(i, api_key, base_url, model)
        with contextlib.suppress(Exception):
            await message.delete()  # hide the key from chat
        await message.answer(
            f"✅ Слот {i + 1} настроен: <code>{model}</code>. Теперь назначь роли.",
            parse_mode="HTML", reply_markup=_kb_slot(i))

    @router.callback_query(F.data.startswith("orch:roles:"))
    async def _roles(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        i = int(query.data.split(":")[-1])
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _slot_text(i) + "\n\nОтметь роли этого слота:",
                parse_mode="HTML", reply_markup=_kb_roles(i))

    @router.callback_query(F.data.startswith("orch:role:"))
    async def _role_toggle(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        _, _, i_s, role = query.data.split(":", 3)
        i = int(i_s)
        orch.toggle_role(i, role)
        await query.answer("Обновлено")
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _slot_text(i) + "\n\nОтметь роли этого слота:",
                parse_mode="HTML", reply_markup=_kb_roles(i))

    @router.callback_query(F.data.startswith("orch:clear:"))
    async def _clear(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        i = int(query.data.split(":")[-1])
        orch.clear_slot(i)
        await query.answer("Слот очищен")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_slot_text(i), parse_mode="HTML", reply_markup=_kb_slot(i))

    return router
