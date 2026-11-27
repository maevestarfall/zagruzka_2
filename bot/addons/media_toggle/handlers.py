# Media menu: photo/video choice screens, reactions toggle, and prompt sources.
# Co-authored with CoCo
"""Master ON/OFF switch for the bot's photo and video reactions.

Why this exists: lilush's ``media_ui.py`` and the Helpzavr addon both
react to every incoming photo. When the user just wants to send a
picture into the chat without the bot pestering them with "Что делаем
с фото?" / running OCR, this toggle silences both.

Default is **ON** so a fresh deploy keeps the original lilush behavior —
the user flips it off explicitly when they get tired of the prompt.

State key: ``_settings.addons.media_toggle.by_chat.<chat_id>.enabled``.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .. import state as addon_state

logger = logging.getLogger(__name__)


# ---- state helper --------------------------------------------------------


def is_media_enabled(chat_id: int) -> bool:
    """Return True when the bot should react to photos/videos in ``chat_id``.

    Default is True so existing chats don't suddenly lose lilush's media
    flow on first deploy. The user opts out explicitly via the toggle.
    """
    return bool(
        addon_state.chat_get("media_toggle", chat_id, "enabled", True)
    )


def _set_media_enabled(chat_id: int, value: bool) -> None:
    addon_state.chat_set("media_toggle", chat_id, "enabled", value)


# ---- screen --------------------------------------------------------------


def _kb_screen(enabled: bool) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton(
            text="⏸ Выключить реакции", callback_data="media_toggle:off"
        )
        if enabled
        else InlineKeyboardButton(
            text="🔘 Включить реакции", callback_data="media_toggle:on"
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📷 Фото", callback_data="media_toggle:photo"),
                InlineKeyboardButton(text="🎬 Видео", callback_data="media_toggle:video"),
            ],
            [InlineKeyboardButton(text="🧩 Алгоритм", callback_data="aps:home")],
            [toggle],
            [
                InlineKeyboardButton(
                    text="📚 Источники промптов",
                    callback_data="main:prompt_sources",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад", callback_data="media_toggle:back"
                )
            ],
        ]
    )


def _kb_photo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Helpzavr — распознать текст", callback_data="main:helpzavr")],
        [InlineKeyboardButton(text="📚 Источники фото", callback_data="psrc:list:photo")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="media_toggle:home")],
    ])


def _kb_video() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧬 Получить промт по видео", callback_data="viddyn:open")],
        [InlineKeyboardButton(text="⚡ Получить промт из видео fast", callback_data="viddyn:fast")],
        [InlineKeyboardButton(text="📚 Источники видео", callback_data="psrc:list:video")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="media_toggle:home")],
    ])


SCREEN_TEXT = (
    "<b>🎬 Генерация фото и видео</b>\n\n"
    "Этот тоггл — общий тихий-режим. Не влияет на то, что предлагает "
    "выбор «фото → Helpzavr / Генерация» (этим занимается отдельный "
    "аддон <b>photo_chooser</b>).\n\n"
    "Когда <b>ВКЛ</b> — бот предлагает выбор куда отправить фото:\n"
    "• <b>Helpzavr</b> — анализ скриншота, OCR, подсказки.\n"
    "• <b>Генерация</b> — img2img / убрать фон / анимация в ролик.\n\n"
    "Когда <b>ВЫКЛ</b> — бот <i>молча игнорирует</i> присланные фото и "
    "видео. Удобно если ты просто хочешь скинуть картинку без "
    "вопросов «что делать?».\n\n"
    "На команды и текст это не влияет.\n\n"
    "Состояние: <b>{state}</b>"
)


async def show_screen(message_or_query) -> None:
    """Render the toggle screen via either Message or CallbackQuery."""
    if isinstance(message_or_query, CallbackQuery):
        if message_or_query.message is None:
            await message_or_query.answer()
            return
        chat_id = message_or_query.message.chat.id
    else:
        chat_id = message_or_query.chat.id
    enabled = is_media_enabled(chat_id)
    text = SCREEN_TEXT.format(state="ВКЛ ✅" if enabled else "ВЫКЛ ⛔")
    kb = _kb_screen(enabled)
    if isinstance(message_or_query, CallbackQuery):
        try:
            await message_or_query.message.edit_text(text, reply_markup=kb)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            await message_or_query.message.answer(text, reply_markup=kb)  # type: ignore[union-attr]
    else:
        await message_or_query.answer(text, reply_markup=kb)


# ---- router --------------------------------------------------------------


def build_media_toggle_router() -> Router:
    router = Router(name="media_toggle_addon")

    @router.callback_query(F.data == "media_toggle:on")
    async def on_enable(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        _set_media_enabled(query.message.chat.id, True)
        await query.answer("Реакции включены")
        await show_screen(query)

    @router.callback_query(F.data == "media_toggle:off")
    async def on_disable(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        _set_media_enabled(query.message.chat.id, False)
        await query.answer("Реакции выключены")
        await show_screen(query)

    @router.callback_query(F.data == "media_toggle:home")
    async def on_home(query: CallbackQuery) -> None:
        await query.answer()
        await show_screen(query)

    @router.callback_query(F.data == "media_toggle:photo")
    async def on_photo(query: CallbackQuery) -> None:
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "<b>📷 Фото</b>\n\n"
                    "• <b>Helpzavr</b> — распознать текст на картинке (OCR), анализ.\n"
                    "• <b>Источники фото</b> — библиотеки готовых промптов.\n\n"
                    "Просто пришли фото в чат — бот предложит обработать его.",
                    reply_markup=_kb_photo(), parse_mode="HTML")

    @router.callback_query(F.data == "media_toggle:video")
    async def on_video(query: CallbackQuery) -> None:
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "<b>🎬 Видео</b>\n\n"
                    "• <b>Источники видео</b> — библиотеки готовых видео-промптов.\n\n"
                    "Пришли видео в чат — бот предложит обработать.",
                    reply_markup=_kb_video(), parse_mode="HTML")

    @router.callback_query(F.data == "media_toggle:back")
    async def on_back(query: CallbackQuery) -> None:
        try:
            from ...wizard import _kb_main_after_claim  # type: ignore
            uid = query.from_user.id if query.from_user else None
            kb = _kb_main_after_claim(uid)
            if query.message is not None:
                await query.message.edit_text(
                    "Главное меню. Выбери что сделать:", reply_markup=kb
                )
        except Exception:  # noqa: BLE001
            logger.debug("media_toggle: back failed", exc_info=True)
        await query.answer()

    return router
