"""Telegram UI for «📚 Источники промптов» (psrc:* callbacks).

Surfaces:
* Manage per-kind (photo/video) source lists — add / remove / reset.
* «🤖 Помощь ИИ» — type an idea, get 4 prompt options (3 from sources +
  1 hybrid) as copy-ready text. The same engine is wired into the real
  photo/video generation flow in ``bot.media_ui``.
"""

from __future__ import annotations

import html
import logging

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

from ...access import can_use
from . import store

logger = logging.getLogger(__name__)

_KIND_LABEL = {"photo": "📷 фото", "video": "🎬 видео"}


class PromptSourceStates(StatesGroup):
    awaiting_url_photo = State()
    awaiting_url_video = State()
    awaiting_help_photo = State()
    awaiting_help_video = State()


def _esc(s: str) -> str:
    return html.escape(s or "")


# ---- keyboards -----------------------------------------------------------


def _kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Помощь ИИ — собрать промпт", callback_data="psrc:help")],
            [InlineKeyboardButton(text="📷 Источники для фото", callback_data="psrc:list:photo")],
            [InlineKeyboardButton(text="🎬 Источники для видео", callback_data="psrc:list:video")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="main:media_toggle")],
        ]
    )


def _kb_list(kind: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, url in enumerate(store.get_sources(kind)):
        short = url.split("//", 1)[-1]
        if len(short) > 34:
            short = short[:33] + "…"
        rows.append(
            [InlineKeyboardButton(text=f"❌ {short}", callback_data=f"psrc:del:{kind}:{i}")]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить сайт", callback_data=f"psrc:add:{kind}")])
    rows.append([InlineKeyboardButton(text="♻️ Сбросить по умолчанию", callback_data=f"psrc:reset:{kind}")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="psrc:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_help_pick() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📷 Промпт для фото", callback_data="psrc:help:photo")],
            [InlineKeyboardButton(text="🎬 Промпт для видео", callback_data="psrc:help:video")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="psrc:home")],
        ]
    )


# ---- screens -------------------------------------------------------------

_HOME_TEXT = (
    "<b>📚 Источники промптов</b>\n\n"
    "Здесь живут сайты с готовыми промптами — отдельно для <b>фото</b> и "
    "<b>видео</b>. Когда ты просишь «помощь ИИ» по идее, бот лезет в эти "
    "источники, находит подходящие популярные промпты и предлагает "
    "<b>4 варианта</b>: 3 — почти один-в-один с сайтов, 1 — гибрид под "
    "твою ситуацию.\n\n"
    "Можно добавлять и убирать сайты для каждого типа."
)


async def show_screen(query: CallbackQuery) -> None:
    if query.message is None:
        return
    try:
        await query.message.edit_text(_HOME_TEXT, reply_markup=_kb_home())
    except Exception:  # noqa: BLE001
        await query.message.answer(_HOME_TEXT, reply_markup=_kb_home())


def _list_text(kind: str) -> str:
    urls = store.get_sources(kind)
    label = _KIND_LABEL.get(kind, kind)
    if urls:
        body = "\n".join(f"• {_esc(u)}" for u in urls)
    else:
        body = "<i>пусто — добавь хотя бы один сайт</i>"
    return (
        f"<b>Источники для {label}</b>\n\n{body}\n\n"
        "Жми ❌ чтобы убрать, ➕ чтобы добавить."
    )


# ---- handlers ------------------------------------------------------------


async def _gate(query: CallbackQuery) -> bool:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return False
    return True


async def _open_home(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    await state.clear()
    await query.answer()
    await show_screen(query)


async def _open_list(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    kind = (query.data or "").split(":")[-1]
    if kind not in store.KINDS:
        await query.answer("?", show_alert=True)
        return
    await state.clear()
    await query.answer()
    if query.message is not None:
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                _list_text(kind), reply_markup=_kb_list(kind),
                disable_web_page_preview=True,
            )


async def _del_source(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    parts = (query.data or "").split(":")
    kind = parts[2]
    try:
        idx = int(parts[3])
    except (ValueError, IndexError):
        await query.answer("?", show_alert=True)
        return
    ok = store.remove_source(kind, idx)
    await query.answer("Убрал" if ok else "Не нашёл")
    if query.message is not None:
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                _list_text(kind), reply_markup=_kb_list(kind),
                disable_web_page_preview=True,
            )


async def _reset_sources(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    kind = (query.data or "").split(":")[-1]
    store.reset_sources(kind)
    await query.answer("Сброшено к стандартным")
    if query.message is not None:
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                _list_text(kind), reply_markup=_kb_list(kind),
                disable_web_page_preview=True,
            )


async def _add_start(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    kind = (query.data or "").split(":")[-1]
    target = (
        PromptSourceStates.awaiting_url_photo
        if kind == "photo"
        else PromptSourceStates.awaiting_url_video
    )
    await state.set_state(target)
    await query.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"psrc:list:{kind}")]
        ]
    )
    if query.message is not None:
        await query.message.answer(
            f"Пришли ссылку на сайт с промптами для {_KIND_LABEL.get(kind, kind)} "
            "(например <code>https://lexica.art</code>):",
            reply_markup=kb,
        )


async def _add_capture(message: Message, state: FSMContext, *, kind: str) -> None:
    uid = message.from_user.id if message.from_user else None
    if not can_use(uid):
        return
    await state.clear()
    ok, msg = store.add_source(kind, message.text or "")
    if ok:
        await message.answer(
            f"✅ Добавил: <code>{_esc(msg)}</code>",
            reply_markup=_kb_list(kind),
        )
    else:
        await message.answer(f"⚠️ {_esc(msg)}", reply_markup=_kb_list(kind))


async def _help_pick(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    await state.clear()
    await query.answer()
    if query.message is not None:
        with __import__("contextlib").suppress(Exception):
            await query.message.edit_text(
                "<b>🤖 Помощь ИИ</b>\n\nДля чего собрать промпт?",
                reply_markup=_kb_help_pick(),
            )


async def _help_start(query: CallbackQuery, state: FSMContext) -> None:
    if not await _gate(query):
        return
    kind = (query.data or "").split(":")[-1]
    target = (
        PromptSourceStates.awaiting_help_photo
        if kind == "photo"
        else PromptSourceStates.awaiting_help_video
    )
    await state.set_state(target)
    await query.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="psrc:help")]
        ]
    )
    if query.message is not None:
        await query.message.answer(
            "Опиши обычным текстом, что хочешь. Например: "
            "<code>хочу быть диснеевским персонажем</code>.\n\n"
            "Я найду подходящие промпты в источниках и дам 4 варианта:",
            reply_markup=kb,
        )


def _render_variants(keywords: str, result: dict) -> str:
    variants = result.get("variants", [])
    kinds = result.get("kinds", [])
    note = result.get("note", "")
    lines = [f"<b>✨ Варианты промпта</b> для «{_esc(keywords)[:160]}»\n"]
    for i, v in enumerate(variants):
        tag = "🧬 гибрид" if (i < len(kinds) and kinds[i] == "hybrid") else "📌 из источника"
        lines.append(f"<b>{i + 1}. {tag}</b>\n<code>{_esc(v)}</code>")
    if note:
        lines.append(f"\n<i>{_esc(note)}</i>")
    return "\n\n".join(lines)


async def _help_capture(message: Message, state: FSMContext, *, kind: str) -> None:
    uid = message.from_user.id if message.from_user else None
    if not can_use(uid):
        return
    keywords = (message.text or "").strip()
    if not keywords:
        await message.answer("Опиши текстом, что хочешь.")
        return
    await state.clear()
    progress = await message.answer("🔎 Ищу промпты в источниках…")
    from .ai import prompt_help
    from .._llm import NoBrainError

    try:
        result = await prompt_help(keywords, kind=kind)
    except NoBrainError as exc:
        await progress.edit_text(f"⚠️ {_esc(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("prompt_help failed")
        await progress.edit_text(f"Ошибка: {_esc(str(exc))}")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Ещё раз", callback_data=f"psrc:help:{kind}")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="psrc:home")],
        ]
    )
    with __import__("contextlib").suppress(Exception):
        await progress.delete()
    await message.answer(
        _render_variants(keywords, result),
        reply_markup=kb,
        disable_web_page_preview=True,
    )


# ---- router --------------------------------------------------------------


def build_prompt_sources_router() -> Router:
    router = Router(name="prompt_sources_addon")
    router.callback_query.register(_open_home, F.data == "main:prompt_sources")
    router.callback_query.register(_open_home, F.data == "psrc:home")
    router.callback_query.register(_open_list, F.data.startswith("psrc:list:"))
    router.callback_query.register(_del_source, F.data.startswith("psrc:del:"))
    router.callback_query.register(_reset_sources, F.data.startswith("psrc:reset:"))
    router.callback_query.register(_add_start, F.data.startswith("psrc:add:"))
    router.callback_query.register(_help_start, F.data.startswith("psrc:help:"))
    router.callback_query.register(_help_pick, F.data == "psrc:help")

    @router.message(StateFilter(PromptSourceStates.awaiting_url_photo))
    async def _u_photo(message: Message, state: FSMContext) -> None:
        await _add_capture(message, state, kind="photo")

    @router.message(StateFilter(PromptSourceStates.awaiting_url_video))
    async def _u_video(message: Message, state: FSMContext) -> None:
        await _add_capture(message, state, kind="video")

    @router.message(StateFilter(PromptSourceStates.awaiting_help_photo))
    async def _h_photo(message: Message, state: FSMContext) -> None:
        await _help_capture(message, state, kind="photo")

    @router.message(StateFilter(PromptSourceStates.awaiting_help_video))
    async def _h_video(message: Message, state: FSMContext) -> None:
        await _help_capture(message, state, kind="video")

    return router
