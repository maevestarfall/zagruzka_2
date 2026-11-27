"""aiogram router for the prompt-library finder.

Commands / triggers:
* ``/prompt <wish>``           — text wish (works as a photo caption too).
* photo + caption ``/prompt …`` — same, with the image attached for context.

Menu (prefix ``pl:``) configures the priority-ordered source sites:
* ``pl:menu``      — list sites (priority 1..N) + add/remove
* ``pl:add``       — FSM: paste a new site URL (appended at the end)
* ``pl:up:<i>``    — raise a site's priority
* ``pl:del:<i>``   — remove a site
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...access import is_owner
from ..state import get as _state_get
from ..state import set_ as _state_set
from . import engine

logger = logging.getLogger(__name__)

_ADDON = "promptlab"
_DEFAULT_SITES = ["https://youmind.com/nano-banana-pro-prompts"]


class PromptLabStates(StatesGroup):
    awaiting_site = State()


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def get_sites() -> list[str]:
    sites = _state_get(_ADDON, "sites", None)
    if not sites:
        return list(_DEFAULT_SITES)
    return [s for s in sites if s]


def set_sites(sites: list[str]) -> None:
    _state_set(_ADDON, "sites", [s.strip() for s in sites if s.strip()])


def _fetch(url: str) -> str:
    import requests
    return requests.get(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; LilushBot/1.0)"},
        timeout=25,
    ).text


# --------------------------------------------------------------------------
# config menu
# --------------------------------------------------------------------------
def _menu_body() -> str:
    sites = get_sites()
    lines = ["<b>📚 Справочник промтов</b>", "",
             "Источники по приоритету (ищу сверху вниз):"]
    if sites:
        for i, s in enumerate(sites, 1):
            lines.append(f"  {i}. <code>{_esc(s)}</code>")
    else:
        lines.append("  (пусто)")
    lines += ["",
              "Как пользоваться: пришли фото с подписью "
              "<code>/prompt твоё пожелание</code> (или просто "
              "<code>/prompt хочу летать в небе</code>).",
              "Я разберу ключевые слова, поищу на сайтах по приоритету и "
              "верну готовые промты + один гибрид."]
    return "\n".join(lines)


def _menu_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, _s in enumerate(get_sites()):
        row = [InlineKeyboardButton(text=f"🗑 #{i+1}", callback_data=f"pl:del:{i}")]
        if i > 0:
            row.append(InlineKeyboardButton(text=f"⬆️ #{i+1}", callback_data=f"pl:up:{i}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="➕ Добавить сайт", callback_data="pl:add")])
    rows.append([InlineKeyboardButton(text="← К 20 слотам", callback_data="b20:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------
# the actual search
# --------------------------------------------------------------------------
async def _run_finder(message: Message, wish: str) -> None:
    sites = get_sites()
    if not sites:
        await message.answer("Нет источников. Открой 📚 Справочник промтов → ➕ Добавить сайт.")
        return
    status = await message.answer("🔎 Ищу промты по приоритетным сайтам…")

    # 1) translate the wish into English search keywords (libraries are EN).
    keywords: list[str] = []
    with contextlib.suppress(Exception):
        from ...agent import oneshot_summary
        raw = await oneshot_summary(
            "Translate this image wish into 3-6 short English search keywords, "
            f"comma-separated, no other text: {wish}",
            purpose="promptlab",
        )
        keywords = [k.strip().lower() for k in raw.replace("\n", ",").split(",") if k.strip()][:6]
    if not keywords:
        keywords = engine.extract_keywords(wish)

    # 2) blocking fetch/parse off the event loop
    res = await asyncio.to_thread(
        engine.find_ready_prompts, wish, sites, _fetch, per_site=3, keywords=keywords,
    )
    ready = res["ready"]

    # 3) hybrid via the bot's brain (falls back to empty on failure)
    hybrid = ""
    if ready:
        try:
            from ...agent import oneshot_summary
            refs = "\n".join(
                f"- {r['title']}: {r['text'][:300]}" for r in ready[:4] if r.get("text")
            )
            hybrid = await oneshot_summary(
                "Ты генерируешь промты для image-моделей. Слей пожелание и "
                "референсы в ОДИН цельный английский промт. Верни только промт.\n\n"
                f"Пожелание: {wish}\n\nРеференсы:\n{refs}",
                purpose="promptlab",
            )
        except Exception:  # noqa: BLE001
            hybrid = ""

    # 4) render
    kw = ", ".join(res["keywords"]) or "—"
    parts = [f"<b>📚 Промты по запросу:</b> {_esc(wish)}",
             f"<i>ключевые слова:</i> {_esc(kw)}", ""]
    if ready:
        parts.append("<b>Готовые промты (по приоритету источников):</b>")
        for i, r in enumerate(ready[:6], 1):
            t = r.get("text") or "(текст не извлёкся — открой ссылку)"
            head = f"{i}. [сайт #{r['priority']}] <b>{_esc(r['title'])}</b>"
            if r.get("url"):
                head += f" — <a href=\"{_esc(r['url'])}\">открыть</a>"
            parts.append(head)
            parts.append(f"<code>{_esc(t[:600])}</code>")
    else:
        parts.append("⚠️ На источниках ничего не нашёл по этим словам.")
    if hybrid:
        parts += ["", "<b>🧬 Гибрид:</b>", f"<code>{_esc(hybrid[:900])}</code>"]
    if res["errors"]:
        parts += ["", "Ошибки источников:"] + [f"• {_esc(e)}" for e in res["errors"]]

    with contextlib.suppress(Exception):
        await status.delete()
    await message.answer("\n".join(parts), parse_mode="HTML", disable_web_page_preview=True)


def _wish_from(message: Message) -> str:
    """Extract the wish after the /prompt command from text or caption."""
    raw = (message.text or message.caption or "").strip()
    # strip the leading command token (/prompt or /промт, with optional @bot)
    parts = raw.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------
def build_promptlab_router() -> Router:
    router = Router(name="promptlab")

    @router.message(Command("prompt", "promt", "промт", "промпт"))
    async def cmd_prompt(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id if message.from_user else 0):
            return
        wish = _wish_from(message)
        if not wish:
            await message.answer(
                "Напиши пожелание после команды, напр.:\n"
                "<code>/prompt хочу летать в небе</code>\n"
                "Можно прикрепить фото с этой подписью.",
                parse_mode="HTML",
            )
            return
        await _run_finder(message, wish)

    # photo whose caption is a /prompt command (Telegram puts it in caption)
    @router.message(F.photo, F.caption.regexp(r"^/(prompt|promt|промт|промпт)\b"))
    async def photo_prompt(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id if message.from_user else 0):
            return
        wish = _wish_from(message)
        if wish:
            await _run_finder(message, wish)

    @router.callback_query(F.data == "pl:menu")
    async def cb_menu(query: CallbackQuery, state: FSMContext) -> None:
        if not is_owner(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(_menu_body(), reply_markup=_menu_kb(),
                                              parse_mode="HTML", disable_web_page_preview=True)
        await query.answer()

    @router.callback_query(F.data == "pl:add")
    async def cb_add(query: CallbackQuery, state: FSMContext) -> None:
        if not is_owner(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(PromptLabStates.awaiting_site)
        if query.message is not None:
            await query.message.answer("🌐 Пришли URL сайта-источника промтов (добавлю в конец списка по приоритету):")
        await query.answer()

    @router.callback_query(F.data.startswith("pl:del:"))
    async def cb_del(query: CallbackQuery, state: FSMContext) -> None:
        if not is_owner(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        i = int(query.data.split(":")[2])
        sites = get_sites()
        if 0 <= i < len(sites):
            sites.pop(i)
            set_sites(sites)
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(_menu_body(), reply_markup=_menu_kb(),
                                              parse_mode="HTML", disable_web_page_preview=True)
        await query.answer("Удалено.")

    @router.callback_query(F.data.startswith("pl:up:"))
    async def cb_up(query: CallbackQuery, state: FSMContext) -> None:
        if not is_owner(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return
        i = int(query.data.split(":")[2])
        sites = get_sites()
        if 0 < i < len(sites):
            sites[i - 1], sites[i] = sites[i], sites[i - 1]
            set_sites(sites)
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(_menu_body(), reply_markup=_menu_kb(),
                                              parse_mode="HTML", disable_web_page_preview=True)
        await query.answer("Приоритет повышен.")

    @router.message(PromptLabStates.awaiting_site, F.text & ~F.text.startswith("/"))
    async def capture_site(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id if message.from_user else 0):
            return
        url = (message.text or "").strip()
        await state.clear()
        if not url.startswith("http"):
            await message.answer("Это не похоже на URL. Нужен http(s)://…")
            return
        sites = get_sites()
        sites.append(url)
        set_sites(sites)
        await message.answer("✅ Добавлено.\n\n" + _menu_body(),
                             reply_markup=_menu_kb(), parse_mode="HTML",
                             disable_web_page_preview=True)

    return router
