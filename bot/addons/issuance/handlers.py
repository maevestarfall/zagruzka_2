"""Telegram handlers and FSM states for the Issuance and Card Verification addon."""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...wizard import can_use
from .core import simulate_verification

logger = logging.getLogger(__name__)

ADDON = "issuance"


class CardCheckerStates(StatesGroup):
    waiting_for_card = State()


def build_issuance_router() -> Router:
    """Build and return the router for the Issuance addon."""
    router = Router(name="issuance_addon")
    
    # Main menu entry
    router.callback_query.register(cmd_issuance_menu, F.data == "main:issuance")
    
    # Start card check
    router.callback_query.register(cmd_card_start, F.data == "issuance:card")
    
    # Cancel card check
    router.callback_query.register(cmd_card_cancel, F.data == "issuance:cancel")
    
    # Card input handler
    router.message.register(process_card_input, StateFilter(CardCheckerStates.waiting_for_card))
    
    return router


def _get_issuance_keyboard() -> InlineKeyboardMarkup:
    """Generate the keyboard for the Issuance main screen."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 CARD (Массовый чекер)", callback_data="issuance:card")],
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="settings:back")],
        ]
    )


async def show_screen(query: CallbackQuery) -> None:
    """Render the main Issuance & CARD menu."""
    text = (
        "<b>💳 Выдача и проверка карт (CARD)</b>\n\n"
        "Добро пожаловать в модуль выдачи и проверки платежных карт!\n\n"
        "Здесь вы можете протестировать работу платежных шлюзов, "
        "проверить карты по алгоритму Луна, определить банк-эмитент, "
        "страну и тип карты в режиме реального времени.\n\n"
        "<i>Поддерживается как одиночный, так и массовый (bulk) ввод карт.</i>"
    )
    
    if query.message is not None:
        await query.message.edit_text(
            text=text,
            reply_markup=_get_issuance_keyboard(),
            parse_mode="HTML"
        )


async def cmd_issuance_menu(query: CallbackQuery, state: FSMContext) -> None:
    """Handle click on the 'Выдача' button in the main menu."""
    user = query.from_user
    uid = user.id if user else None
    if not can_use(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return
        
    await state.clear()
    await query.answer()
    await show_screen(query)


async def cmd_card_start(query: CallbackQuery, state: FSMContext) -> None:
    """Start the CARD checking FSM."""
    user = query.from_user
    uid = user.id if user else None
    if not can_use(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return
        
    await query.answer()
    await state.set_state(CardCheckerStates.waiting_for_card)
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="issuance:cancel")]
        ]
    )
    
    text = (
        "<b>💳 Массовый чекер карт (CARD)</b>\n\n"
        "Отправьте список карт для проверки в формате:\n"
        "<code>НОМЕР|ММ|ГГ|CVV</code>\n"
        "или просто список номеров карт (каждая карта с новой строки).\n\n"
        "<b>Пример ввода:</b>\n"
        "<code>4242424242424242|12|28|123</code> (Stripe Live)\n"
        "<code>5353161234567890|09|27|999</code>\n\n"
        "<i>Отправьте сообщение с картами:</i>"
    )
    
    if query.message is not None:
        await query.message.edit_text(
            text=text,
            reply_markup=kb,
            parse_mode="HTML"
        )


async def cmd_card_cancel(query: CallbackQuery, state: FSMContext) -> None:
    """Cancel the FSM and return to the main issuance screen."""
    await state.clear()
    await query.answer("Проверка отменена")
    await show_screen(query)


async def process_card_input(message: Message, state: FSMContext) -> None:
    """Process single or bulk card inputs from the user."""
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовый список карт.")
        return
        
    lines = [line.strip() for line in message.text.split("\n") if line.strip()]
    if not lines:
        await message.answer("Пустой список карт. Попробуйте ещё раз.")
        return
        
    status_msg = await message.answer("🔍 <b>Анализирую карты...</b>\n<i>Запускаю симуляцию платежного шлюза...</i>", parse_mode="HTML")
    await state.clear()
    
    results = []
    
    for i, line in enumerate(lines):
        # Parse card details (NUMBER|MM|YY|CVV or space/comma separated)
        parts = re.split(r"[|;,:\s]+", line)
        card_num = parts[0] if parts else ""
        expiry = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else ""
        cvv = parts[3] if len(parts) >= 4 else ""
        
        # Clean card number
        clean_num = re.sub(r"\D", "", card_num)
        if not clean_num:
            continue
            
        # Update progress for long lists
        if len(lines) > 3 and i % 3 == 0:
            await status_msg.edit_text(
                f"🔍 <b>Проверяю карты... ({i}/{len(lines)})</b>\n<i>Обрабатываю платежные шлюзы...</i>",
                parse_mode="HTML"
            )
            
        # Run simulation
        res = await simulate_verification(clean_num, expiry, cvv)
        results.append((line, res))
        
        # Artificial delay to simulate real network requests
        await asyncio.sleep(0.4)
        
    # Format and send results
    live_cards = [r for r in results if r[1]["ok"]]
    die_cards = [r for r in results if not r[1]["ok"]]
    
    summary = (
        f"<b>📋 РЕЗУЛЬТАТЫ ПРОВЕРКИ (CARD)</b>\n\n"
        f"✅ <b>LIVE:</b> {len(live_cards)}\n"
        f"❌ <b>DIE:</b> {len(die_cards)}\n"
        f"───────────────\n\n"
    )
    
    details = []
    for raw_line, res in results:
        status_emoji = "🟢" if res["ok"] else "🔴"
        bin_info = res["bin_info"]
        country_emoji = bin_info.get("country", {}).get("emoji", "🏳️")
        country_name = bin_info.get("country", {}).get("name", "Unknown")
        bank_name = bin_info.get("bank", {}).get("name", "Unknown Bank")
        scheme = str(bin_info.get("scheme", "unknown")).upper()
        card_type = str(bin_info.get("type", "credit")).upper()
        
        # Mask card number for display
        raw_num = re.sub(r"\D", "", raw_line.split("|")[0])
        masked_num = raw_num[:6] + "******" + raw_num[-4:] if len(raw_num) >= 10 else raw_num
        
        details.append(
            f"{status_emoji} <b>{masked_num}</b> | {scheme} {card_type}\n"
            f"🌍 Страна: {country_emoji} {country_name}\n"
            f"🏦 Банк: <code>{bank_name}</code>\n"
            f"💬 Статус: <b>{res['status']}</b> - {res['reason']}\n"
        )
        
    full_text = summary + "\n".join(details)
    
    # Split if text is too long (Telegram limit is 4096 chars)
    if len(full_text) > 4000:
        parts = [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]
        for p in parts:
            await message.answer(p, parse_mode="HTML")
    else:
        await message.answer(full_text, parse_mode="HTML")
        
    # Clean up status message
    try:
        await status_msg.delete()
    except Exception:
        pass
        
    # Show main menu again
    await message.answer(
        "Что вы хотите сделать дальше?",
        reply_markup=_get_issuance_keyboard()
    )
