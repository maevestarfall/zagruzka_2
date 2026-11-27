# Instructions addon: master instruction screen with optimize/import/export/mode.
# Co-authored with CoCo
"""«📜 Инструкции» — manage the bot's master instruction.

* View / reset the single master instruction (default seeded).
* 🛠 Оптимизировать — analyse all bot functions (reuses regen optimizer),
  show the improvements, and offer to merge them into the master.
* 📥 Импорт / 📤 Экспорт the instruction as a file.
* 🤖 Режим: instruction+internet / only instruction / off.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import store
from . import important

logger = logging.getLogger(__name__)


class InstrStates(StatesGroup):
    awaiting_import = State()


def _esc(s) -> str:
    import html
    return html.escape(str(s or ""))


async def _owner(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner
    return _is_owner(query.from_user.id if query.from_user else 0)


def _kb_home() -> InlineKeyboardMarkup:
    mode = store.get_mode()
    mode_label = {"off": "ВЫКЛ", "only": "только инструкция",
                  "internet": "инструкция + интернет"}.get(mode, mode)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠 Оптимизировать", callback_data="instr:opt")],
        [InlineKeyboardButton(text="🧪 Тест контекста модели", callback_data="ctx:test")],
        [InlineKeyboardButton(text=f"🤖 Режим: {mode_label}", callback_data="instr:mode")],
        [
            InlineKeyboardButton(text="📥 Импорт", callback_data="instr:import"),
            InlineKeyboardButton(text="📤 Экспорт", callback_data="instr:export"),
        ],
        [InlineKeyboardButton(text="♻️ Сброс к дефолту", callback_data="instr:reset")],
        [InlineKeyboardButton(text="🗺 graphify (карта проекта)", callback_data="pmg:settings")],
        [InlineKeyboardButton(text="❗Важно (мои выводы)", callback_data="vazhno:home")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main:settings")],
    ])


def _kb_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Инструкция + интернет", callback_data="instr:mode:internet")],
        [InlineKeyboardButton(text="📕 Только инструкция", callback_data="instr:mode:only")],
        [InlineKeyboardButton(text="⛔ Выключить", callback_data="instr:mode:off")],
        [InlineKeyboardButton(text="↩️ Вернуться", callback_data="instr:home")],
    ])


def _home_text() -> str:
    m = store.get_master()
    preview = m[:900] + ("…" if len(m) > 900 else "")
    mode = store.get_mode()
    mode_label = {"off": "ВЫКЛ", "only": "только инструкция",
                  "internet": "инструкция + интернет"}.get(mode, mode)
    return (
        "<b>📜 Инструкции</b>\n\n"
        f"Режим использования: <b>{mode_label}</b>\n\n"
        f"<b>Текущая инструкция:</b>\n<pre>{_esc(preview)}</pre>"
    )


def build_instructions_router() -> Router:
    router = Router(name="instructions")

    @router.callback_query(F.data == "instr:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML",
                                          reply_markup=_kb_home())

    @router.callback_query(F.data == "instr:mode")
    async def _mode(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🤖 Как ИИ использует инструкцию?</b>", parse_mode="HTML",
                reply_markup=_kb_mode())

    # ─── «❗Важно» — выводы владельца по архитектуре ──────────────────
    def _vazhno_home_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 От автора", callback_data="vazhno:list:author")],
            [InlineKeyboardButton(text="🤖 От ИИ", callback_data="vazhno:list:ai")],
            [InlineKeyboardButton(text="📓 Все указания (журнал)", callback_data="vazhno:log")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="instr:home")],
        ])

    def _vazhno_list_kb(source: str) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text=f"{n['status_label']} {n['title']}",
                                      callback_data=f"vazhno:item:{n['id']}")]
                for n in important.get_notes(source)]
        if not rows:
            rows = [[InlineKeyboardButton(text="— пусто —", callback_data="vazhno:home")]]
        rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="vazhno:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data == "vazhno:home")
    async def _vazhno_home(query: CallbackQuery) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>❗Важно</b>\n\n"
                "Это <b>то, что надо доработать</b> (не сама инструкция — "
                "последовательность действий живёт в 📜 Инструкции/Поступенчатая "
                "и должна улучшаться).\n\n"
                "Два раздела:\n"
                "• <b>👤 От автора</b> — твои умозаключения о механизмах.\n"
                "• <b>🤖 От ИИ</b> — важные выводы, которые добавляю я.\n"
                "• <b>📓 Все указания</b> — журнал: твоя цитата → кратко → полный "
                "текст, что сделано.\n\n"
                "💡 = идея, ✅ = работает. Гермес заглядывает сюда и предлагает "
                "связки перед правками.",
                parse_mode="HTML", reply_markup=_vazhno_home_kb())

    @router.callback_query(F.data.startswith("vazhno:list:"))
    async def _vazhno_list(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        source = query.data.split(":", 2)[2]
        title = "👤 От автора" if source == "author" else "🤖 От ИИ"
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"<b>{title}</b>\n\nТапни пункт — описание словами + переключатель статуса.",
                parse_mode="HTML", reply_markup=_vazhno_list_kb(source))

    @router.callback_query(F.data == "vazhno:log")
    async def _vazhno_log(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer("📓 Достаю журнал…")
        import os
        from pathlib import Path
        candidates = [
            Path(__file__).resolve().parents[3] / "AI_CHANGELOG.md",  # repo root
            Path("/app/AI_CHANGELOG.md"),
            Path("AI_CHANGELOG.md"),
        ]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            with contextlib.suppress(Exception):
                await query.message.answer(
                    "Журнал (AI_CHANGELOG.md) не найден в образе. Он ведётся в "
                    "репозитории — открой файл AI_CHANGELOG.md там.")
            return
        with contextlib.suppress(Exception):
            from aiogram.types import FSInputFile
            await query.message.answer_document(
                FSInputFile(str(path), filename="AI_CHANGELOG.md"),
                caption="📓 Все указания: твои цитаты → кратко → полный текст.")

    @router.callback_query(F.data.startswith("vazhno:item:"))
    async def _vazhno_item(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        nid = query.data.split(":", 2)[2]
        n = important.get_note(nid)
        await query.answer()
        if not n:
            with contextlib.suppress(Exception):
                await query.message.edit_text("Пункт не найден.", reply_markup=_vazhno_home_kb())
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Переключить статус (идея↔работает)",
                                  callback_data=f"vazhno:toggle:{nid}")],
            [InlineKeyboardButton(text="↩️ К списку", callback_data="vazhno:home")],
        ])
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"<b>{_esc(n['title'])}</b>\n\nСтатус: <b>{n['status_label']}</b>\n\n"
                f"{_esc(n['text'])}",
                parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith("vazhno:toggle:"))
    async def _vazhno_toggle(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        nid = query.data.split(":", 2)[2]
        important.toggle_status(nid)
        await query.answer("Статус обновлён")
        n = important.get_note(nid)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Переключить статус (идея↔работает)",
                                  callback_data=f"vazhno:toggle:{nid}")],
            [InlineKeyboardButton(text="↩️ К списку", callback_data="vazhno:home")],
        ])
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"<b>{_esc(n['title'])}</b>\n\nСтатус: <b>{n['status_label']}</b>\n\n"
                f"{_esc(n['text'])}",
                parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data.startswith("instr:mode:"))
    async def _mode_set(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        mode = query.data.split(":")[-1]
        store.set_mode(mode)
        await query.answer("Режим обновлён")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML",
                                          reply_markup=_kb_home())

    @router.callback_query(F.data == "instr:reset")
    async def _reset(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        store.reset_master()
        await query.answer("Сброшено к дефолту")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML",
                                          reply_markup=_kb_home())

    @router.callback_query(F.data == "instr:export")
    async def _export(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.answer_document(
                BufferedInputFile(store.get_master().encode("utf-8"),
                                  filename="instruction.md"),
                caption="📤 Текущая инструкция")

    @router.callback_query(F.data == "instr:import")
    async def _import(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(InstrStates.awaiting_import)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>📥 Импорт инструкции</b>\n\nПришли новый текст инструкции "
                "сообщением или <code>.md/.txt</code> файлом. Он заменит текущую.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data="instr:home")]]))

    @router.message(InstrStates.awaiting_import)
    async def _import_capture(message: Message, state: FSMContext) -> None:
        from ...wizard import _is_owner
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return
        text = ""
        if message.document:
            with contextlib.suppress(Exception):
                import io
                buf = io.BytesIO()
                await message.bot.download(message.document.file_id, destination=buf)
                text = buf.getvalue().decode("utf-8", "ignore")
        elif message.text:
            text = message.text
        if not text.strip():
            await message.answer("Пусто — пришли текст или .md/.txt файл.")
            return
        store.set_master(text)
        await state.clear()
        await message.answer("✅ Инструкция обновлена.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📜 Инструкции", callback_data="instr:home")]]))

    # ── Optimize (moved from Регенерация) ────────────────────────────────────
    @router.callback_query(F.data == "instr:opt")
    async def _opt(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer("🛠 Анализирую функции…")
        msg = query.message
        note = await msg.answer("🛠 Читаю карту проекта и собираю улучшения…")
        try:
            from ..regen.project_map import build_project_map
            from ..regen.optimizer import build_optimization, NoBrainError
            pmap = build_project_map()
            improved = await build_optimization(pmap)
        except NoBrainError as exc:
            with contextlib.suppress(Exception):
                await note.edit_text(f"🧠 {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("instr optimize failed")
            with contextlib.suppress(Exception):
                await note.edit_text(f"Не удалось: {type(exc).__name__}: {exc}")
            return
        # stash for the merge decision
        addon_state_set(improved)
        with contextlib.suppress(Exception):
            await note.delete()
        from ...handlers import _send_long
        await _send_long(msg, "🛠 <b>Что улучшил:</b>\n\n" + improved, code=False)
        await msg.answer(
            "Добавить улучшения в главную инструкцию?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить в главную", callback_data="instr:opt:add")],
                [InlineKeyboardButton(text="🚫 Не добавлять", callback_data="instr:home")],
            ]))

    @router.callback_query(F.data == "instr:opt:add")
    async def _opt_add(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        improved = addon_state_get()
        if not improved:
            await query.answer("Нечего добавлять — запусти оптимизацию заново.", show_alert=True)
            return
        store.append_master(improved)
        await query.answer("✅ Добавлено в инструкцию")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML",
                                          reply_markup=_kb_home())

    return router


# tiny helpers to stash the last optimization between two callbacks
def addon_state_set(text: str) -> None:
    store.addon_state.set_(store.ADDON, "last_opt", text or "")


def addon_state_get() -> str:
    return str(store.addon_state.get(store.ADDON, "last_opt", "") or "")
