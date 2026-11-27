"""Telegram UI for the OSINT addon.

Two surfaces, all under the ``osint:`` callback prefix:

1. A browsable catalog of AI-OSINT tools grouped into person-research
   categories (face / username / contacts / geoint / dorks / …). Each
   category renders its tools as tappable URL buttons.
2. ``🤖 Помощь ИИ`` — the user describes a target and the LLM returns a
   structured OSINT plan: which catalog tools to use, in what order, and
   ready-to-paste search queries / Google dorks.

State lives in lilush's storage via the shared addon-state helper; this
addon stores nothing of its own beyond the FSM target text.
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
from .catalog import CATALOG, CATEGORY_ORDER, SOURCE_URL

logger = logging.getLogger(__name__)

_MAX_TOOLS_PER_SCREEN = 16


class OsintStates(StatesGroup):
    awaiting_target = State()


def _esc(s: str) -> str:
    return html.escape(s or "")


# ---- keyboards -----------------------------------------------------------


def _kb_home() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🤖 Помощь ИИ — план поиска", callback_data="osint:ai")]
    ]
    # Two categories per row to keep the screen compact.
    pair: list[InlineKeyboardButton] = []
    for key in CATEGORY_ORDER:
        cat = CATALOG.get(key)
        if not cat:
            continue
        label = f"{cat['emoji']} {cat['label']}"
        pair.append(InlineKeyboardButton(text=label, callback_data=f"osint:cat:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="🎞 Анализ динамики видео", callback_data="viddyn:open")])
    rows.append([InlineKeyboardButton(text="🧰 GitHub-инструменты анализа", callback_data="ghtools:open")])
    rows.append([InlineKeyboardButton(text="🔗 Открыть каталог-источник", url=SOURCE_URL)])
    rows.append([InlineKeyboardButton(text="↩️ В главное меню", callback_data="osint:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_category(key: str) -> InlineKeyboardMarkup:
    cat = CATALOG.get(key, {})
    items = cat.get("items", [])[:_MAX_TOOLS_PER_SCREEN]
    rows: list[list[InlineKeyboardButton]] = []
    for name, url in items:
        label = name if len(name) <= 42 else name[:41] + "…"
        rows.append([InlineKeyboardButton(text=f"🔗 {label}", url=url)])
    rows.append([InlineKeyboardButton(text="↩️ К категориям", callback_data="osint:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---- screens -------------------------------------------------------------


_HOME_TEXT = (
    "<b>🕵️ Исследование и поиск человека</b>\n\n"
    "Подборка AI-OSINT инструментов (на базе репозитория "
    "<i>Awesome-AI-OSINT</i>) для поиска людей: по лицу, нику, контактам, "
    "геолокации фото, доркам Google и т.д.\n\n"
    "• Нажми <b>🤖 Помощь ИИ</b> — опиши, кого/что ищешь, и я составлю "
    "пошаговый план: какие инструменты и какие готовые запросы "
    "использовать.\n"
    "• Или выбери категорию ниже — открою список рабочих сервисов "
    "ссылками.\n\n"
    "<i>⚠️ Используй только законно: для собственной безопасности, "
    "проверки контрагентов, расследований с согласия. Не для "
    "преследования.</i>"
)


async def show_screen(query: CallbackQuery) -> None:
    """Render the OSINT home screen (edit in place when possible)."""
    if query.message is None:
        return
    try:
        await query.message.edit_text(
            _HOME_TEXT, reply_markup=_kb_home(), disable_web_page_preview=True
        )
    except Exception:  # noqa: BLE001
        await query.message.answer(
            _HOME_TEXT, reply_markup=_kb_home(), disable_web_page_preview=True
        )


def _category_text(key: str) -> str:
    cat = CATALOG.get(key, {})
    items = cat.get("items", [])
    shown = min(len(items), _MAX_TOOLS_PER_SCREEN)
    extra = (
        f"\n\n<i>Показаны {shown} из {len(items)}. "
        "Полный список — в каталоге-источнике.</i>"
        if len(items) > shown
        else ""
    )
    return (
        f"<b>{cat.get('emoji', '')} {_esc(cat.get('label', key))}</b>\n\n"
        "Тапни по инструменту, чтобы открыть его в браузере." + extra
    )


# ---- handlers ------------------------------------------------------------


async def _open_home(query: CallbackQuery, state: FSMContext) -> None:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await query.answer()
    await show_screen(query)


async def _open_category(query: CallbackQuery, state: FSMContext) -> None:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return
    key = (query.data or "").split(":", 2)[-1]
    if key not in CATALOG:
        await query.answer("Категория не найдена.", show_alert=True)
        return
    await state.clear()
    await query.answer()
    if query.message is not None:
        try:
            await query.message.edit_text(
                _category_text(key),
                reply_markup=_kb_category(key),
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            await query.message.answer(
                _category_text(key),
                reply_markup=_kb_category(key),
                disable_web_page_preview=True,
            )


async def _ai_start(query: CallbackQuery, state: FSMContext) -> None:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(OsintStates.awaiting_target)
    await query.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="osint:home")]
        ]
    )
    if query.message is not None:
        await query.message.answer(
            "<b>🤖 Помощь ИИ — план поиска</b>\n\n"
            "Опиши цель одним сообщением. Чем больше зацепок — тем точнее "
            "план. Например:\n"
            "• <code>Иван Петров, Москва, инстаграм @ivanp, работал в X</code>\n"
            "• <code>email john.doe@gmail.com</code>\n"
            "• <code>ник darkfox_99 на форумах</code>\n"
            "• <code>фото человека, нужна геолокация</code>\n\n"
            "Пришли описание цели:",
            reply_markup=kb,
        )


def _build_plan_prompt() -> str:
    # Compact tool inventory the model can reference by name.
    lines = []
    for key in CATEGORY_ORDER:
        cat = CATALOG.get(key, {})
        names = ", ".join(name for name, _ in cat.get("items", [])[:8])
        lines.append(f"- {cat.get('label', key)}: {names}")
    inventory = "\n".join(lines)
    return (
        "Ты — опытный OSINT-аналитик. По описанию цели составь чёткий "
        "пошаговый план поиска информации о человеке, используя AI-OSINT "
        "инструменты. Отвечай на русском.\n\n"
        "Доступные категории инструментов (можешь упоминать конкретные "
        "названия):\n" + inventory + "\n\n"
        "Структура ответа:\n"
        "1) Что у нас есть (зацепки) и что хотим узнать.\n"
        "2) Пошаговый план: на каждом шаге — какой инструмент/категория "
        "и зачем.\n"
        "3) Готовые поисковые запросы и Google-дорки (в блоках кода), "
        "которые можно скопировать.\n"
        "4) Короткое предупреждение о законности и OPSEC.\n"
        "Будь конкретным и практичным, без воды."
    )


async def _ai_capture(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else None
    if not can_use(uid):
        return
    target = (message.text or "").strip()
    if not target:
        await message.answer("Пришли текстовое описание цели.")
        return
    await state.clear()
    progress = await message.answer("🧠 Составляю план поиска…")
    from .._llm import NoBrainError, chat_once

    try:
        plan = await chat_once(
            _build_plan_prompt(),
            f"Цель поиска: {target}",
            max_tokens=1100,
        )
    except NoBrainError as exc:
        await progress.edit_text(f"⚠️ {_esc(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("osint plan failed")
        await progress.edit_text(f"Ошибка ИИ: {_esc(str(exc))}")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Новый поиск", callback_data="osint:ai")],
            [InlineKeyboardButton(text="↩️ К категориям", callback_data="osint:home")],
        ]
    )
    try:
        from ..hermes_experience import record as _hx

        _hx(
            "osint",
            f"Построен план поиска по цели «{target[:80]}» ({len(plan)} символов)",
            outcome="ok" if plan else "empty",
        )
    except Exception:  # noqa: BLE001
        pass
    text = plan if plan else "Пустой ответ модели. Попробуй переформулировать."
    # Telegram hard limit ~4096; trim defensively.
    if len(text) > 3800:
        text = text[:3800] + "\n…"
    with __import__("contextlib").suppress(Exception):
        await progress.delete()
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def _back_to_main(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    try:
        from ...wizard import _kb_main_after_claim  # type: ignore

        uid = query.from_user.id if query.from_user else None
        kb = _kb_main_after_claim(uid)
        if query.message is not None:
            await query.message.edit_text(
                "Главное меню. Выбери что сделать:", reply_markup=kb
            )
    except Exception:  # noqa: BLE001
        logger.debug("osint: back to main failed", exc_info=True)


# ---- router --------------------------------------------------------------


def build_osint_router() -> Router:
    router = Router(name="osint_addon")
    router.callback_query.register(_open_home, F.data == "main:osint")
    router.callback_query.register(_open_home, F.data == "osint:home")
    router.callback_query.register(_open_category, F.data.startswith("osint:cat:"))
    router.callback_query.register(_ai_start, F.data == "osint:ai")
    router.callback_query.register(_back_to_main, F.data == "osint:back")
    router.message.register(_ai_capture, StateFilter(OsintStates.awaiting_target))
    return router
