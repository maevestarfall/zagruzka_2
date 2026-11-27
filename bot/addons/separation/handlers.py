# Separation addon: pick functions and let the coder extract a working sub-project.
# Co-authored with CoCo
"""«📦 Отделение» — build a spec of selected functions and let the coder
extract a working sub-project containing only them.

The bot does not cut files itself — it composes a ТЗ from the user's
checkbox selection and hands it to the coder (reusing the regen brain +
project map + zip builder). The coder decides what to keep/rewrite and
returns a working project; if a clean split isn't possible it says so.
"""

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

ADDON = "separation"

# Selectable components: (id, label). Order = display order.
COMPONENTS: list[tuple[str, str]] = [
    ("codespace", "🧰 Codespace (среда)"),
    ("llm", "🧠 LLM (мозг)"),
    ("helpzavr", "🤖 Helpzavr"),
    ("pretty_text", "✨ Красивый текст"),
    ("mailbox", "📬 Почта"),
    ("media", "🎬 Генерация фото/видео"),
    ("algo_slots", "🧩 Алгоритм-слоты"),
    ("prompt_sources", "📚 Источники промптов"),
    ("osint", "🕵️ OSINT"),
    ("dump", "🗑 Скинуть мусор"),
    ("ai_check", "🛡 AI CHECK"),
    ("debates", "🎤 Дебаты"),
    ("analyzer", "🔬 Анализатор PRO"),
    ("issuance", "💳 Выдача"),
    ("markitdown", "📝 Markitdown"),
    ("github", "🐙 GitHub проекты"),
    ("coder", "💻 Кодер (Coder Firm)"),
    ("regen", "🪄 Регенерация"),
    ("vibe", "✨ Vibe Coding"),
    ("terminal", "🖥 Терминал"),
    ("vision", "🖥 VISION"),
    ("masterroom", "🛠 Master Room"),
    ("memory", "💾 Память"),
    ("vault", "🔒 Сейф"),
    ("instructions", "📜 Инструкции"),
    ("thinking_style", "🧠 Соображалка"),
    ("priority", "🎯 Приоритет"),
    ("motivation_auto", "🎯 Мотивация авто"),
    ("editor_agent", "🎞 Монтажёр"),
    ("unstructured", "🔌 Unstructured"),
    ("ram_guard", "💻 RAM"),
    ("tts", "🔊 Голосовой ответчик"),
]


def _selected() -> list[str]:
    raw = addon_state.get(ADDON, "selected", []) or []
    return [str(x) for x in raw]


def _set_selected(ids: list[str]) -> None:
    addon_state.set_(ADDON, "selected", ids)


def _toggle(cid: str) -> None:
    cur = _selected()
    if cid in cur:
        cur = [x for x in cur if x != cid]
    else:
        cur.append(cid)
    _set_selected(cur)


async def _owner(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner
    return _is_owner(query.from_user.id if query.from_user else 0)


def _kb_home() -> InlineKeyboardMarkup:
    sel = set(_selected())
    rows: list[list[InlineKeyboardButton]] = []
    for cid, label in COMPONENTS:
        mark = "✅" if cid in sel else "➖"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"sep:t:{cid}")])
    rows.append([InlineKeyboardButton(text="🧹 Сбросить", callback_data="sep:clear")])
    rows.append([InlineKeyboardButton(text="📦 Отделить", callback_data="sep:go")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="main:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _home_text() -> str:
    n = len(_selected())
    return (
        "<b>📦 Отделение</b>\n\n"
        f"Выбрано функций: <b>{n}</b>\n\n"
        "Отметь функции, которые должны остаться в отдельном проекте, и нажми "
        "<b>📦 Отделить</b>. Кодер соберёт рабочий бот только с ними "
        "(или скажет, если чисто отделить нельзя)."
    )


def build_separation_router() -> Router:
    router = Router(name="separation")

    @router.callback_query(F.data == "sep:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML",
                                          reply_markup=_kb_home())

    @router.callback_query(F.data.startswith("sep:t:"))
    async def _tog(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        _toggle(query.data.split(":")[-1])
        await query.answer("Обновлено")
        with contextlib.suppress(Exception):
            await query.message.edit_reply_markup(reply_markup=_kb_home())

    @router.callback_query(F.data == "sep:clear")
    async def _clear(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        _set_selected([])
        await query.answer("Сброшено")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML",
                                          reply_markup=_kb_home())

    @router.callback_query(F.data == "sep:go")
    async def _go(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        sel = _selected()
        if not sel:
            await query.answer("Сначала отметь функции.", show_alert=True)
            return
        await query.answer("📦 Передаю ТЗ кодеру…")
        msg = query.message
        labels = {cid: lab for cid, lab in COMPONENTS}
        chosen = "\n".join(f"- {labels.get(c, c)} ({c})" for c in sel)
        problem = (
            "ЗАДАЧА: отдели из этого бота РАБОЧИЙ под-проект, в котором "
            "останутся ТОЛЬКО следующие функции:\n" + chosen + "\n\n"
            "Требования:\n"
            "1. Перепиши bot/addons/__init__.py (build_addon_routers) — "
            "регистрируй только нужные аддоны.\n"
            "2. Почисти главное меню (wizard) от кнопок лишних функций.\n"
            "3. Сохрани рабочее ядро (main, config, storage, agent, persona, "
            "media — то, от чего зависят выбранные функции).\n"
            "4. Верни ПОЛНЫЕ изменённые файлы. Если чисто отделить нельзя — "
            "напиши это в начале root_cause и оставь files пустым."
        )
        note = await msg.answer("🛠 Кодер изучает проект и собирает отделение…")
        try:
            from ..regen.project_map import build_project_map
            from ..coder.regen_brain import generate_fix, NoBrainError
            pmap = build_project_map()
            fix = await generate_fix(problem, project_map=pmap, max_tokens=4000)
        except NoBrainError as exc:
            with contextlib.suppress(Exception):
                await note.edit_text(f"🧠 {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("separation: generate_fix failed")
            with contextlib.suppress(Exception):
                await note.edit_text(f"Кодер не смог: {type(exc).__name__}: {exc}")
            return
        from ...handlers import _send_long
        head = [f"🛠 Кодер принял ТЗ. Причина/вывод: {fix.root_cause[:500] or '—'}"]
        if fix.notes:
            head.append(f"Заметки: {fix.notes[:300]}")
        if not fix.files:
            head.append("\n⚠️ Кодер не дал полные файлы — см. инструкцию ниже.")
            head.append(fix.instructions[:1500] if fix.instructions else "")
        with contextlib.suppress(Exception):
            await note.delete()
        await _send_long(msg, "\n".join(head), code=False)
        # Build the zip with applied changes (if any).
        prog = await msg.answer("📦 Собираю отделённый проект…")
        try:
            import asyncio
            from ..regen import outputs as routs, scanner as rscan
            zip_path, errors, verify = await asyncio.to_thread(
                routs.build_fix_zip, fix, rscan.repo_root(),
                version_tag="separation",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("separation: zip failed")
            with contextlib.suppress(Exception):
                await prog.edit_text(f"Не смог собрать zip: {type(exc).__name__}: {exc}")
            return
        import os
        from aiogram.types import FSInputFile
        cap = "📦 Отделённый проект."
        if errors:
            cap = "⚠️ Собран, но есть ошибки компиляции — см. REGEN_FIX_NOTES.md."
        elif not fix.files:
            cap = "📦 Бот + инструкция (кодер дал рецепт, без полных файлов)."
        with contextlib.suppress(Exception):
            await prog.delete()
        with contextlib.suppress(Exception):
            await msg.answer_document(
                FSInputFile(zip_path, filename=os.path.basename(zip_path)),
                caption=cap,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📦 Отделение", callback_data="sep:home")]]))

    return router
