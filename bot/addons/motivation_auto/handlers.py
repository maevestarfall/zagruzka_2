"""Settings UI for bot-wide auto-motivation.

Screens:
  🎯 Мотивация авто (main)
    ├── 🟢/⚪ Авто / Вручную  (toggle mode)
    ├── 🔢 Интервал            (set custom interval)
    ├── 📄 Документ            (shared doc management)
    │   ├── Загрузить / 👁 Посмотреть
    │   ├── 🗑 Удалить
    │   └── « Назад
    └── ↩️ Назад → settings menu
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import engine

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_IKB = InlineKeyboardButton


class MotAutoStates(StatesGroup):
    awaiting_interval = State()
    awaiting_doc_upload = State()


# ─── Keyboards ────────────────────────────────────────────────────────────────


def _kb_main() -> InlineKeyboardMarkup:
    mode = engine.get_mode()
    mode_btn = _IKB(
        text="⚪ Переключить на Вручную" if mode == "auto"
        else "🟢 Переключить на Авто",
        callback_data="motauto:toggle_mode",
    )
    rows = [
        [mode_btn],
        [_IKB(text="🔢 Интервал", callback_data="motauto:interval")],
        [_IKB(text="📄 Документ", callback_data="motauto:doc")],
        [_IKB(text="↩️ Назад", callback_data="motauto:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_doc() -> InlineKeyboardMarkup:
    try:
        from ..debates import motivation as _mot
        doc = _mot.get_doc()
    except ImportError:
        doc = None

    rows: list[list[_IKB]] = []
    if doc:
        rows.append([_IKB(text="👁 Посмотреть", callback_data="motauto:doc_view")])
        rows.append([_IKB(text="🗑 Удалить", callback_data="motauto:doc_delete")])
    else:
        rows.append([_IKB(text="📥 Загрузить", callback_data="motauto:doc_upload")])
    rows.append([_IKB(text="↩️ Назад", callback_data="motauto:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_doc_view() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_IKB(text="🗑 Удалить", callback_data="motauto:doc_delete")],
        [_IKB(text="↩️ Назад", callback_data="motauto:doc")],
    ])


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _edit_safe(query: CallbackQuery, text: str,
                     kb: InlineKeyboardMarkup) -> None:
    """Edit message, ignoring 'not modified' errors."""
    try:
        if query.message:
            await query.message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass
    await query.answer()


def _doc_summary_text() -> str:
    try:
        from ..debates import motivation as _mot
        doc = _mot.get_doc()
        if not doc:
            return "📄 <b>Документ контекстных инъекций</b>\n\n❌ Не загружен."
        cats = doc.get("categories", {})
        total = doc.get("total_phrases", 0)
        name = doc.get("name", "—")[:60]
        cat_lines = []
        _emoji = {"A": "🔴", "B": "🟢", "C": "⚡", "D": "🔵", "E": "🧠"}
        _names = {
            "A": "Ответственность", "B": "Уверенность",
            "C": "Провокация", "D": "Честность",
            "E": "Karpathy контекст",
        }
        for k in ("A", "B", "C", "D", "E"):
            items = cats.get(k, [])
            if items:
                cat_lines.append(
                    f"  {_emoji.get(k, '•')} {_names.get(k, k)}: "
                    f"{len(items)} фраз")
        return (
            f"📄 <b>Документ контекстных инъекций</b>\n\n"
            f"Название: {name}\n"
            f"Фраз: {total}\n"
            + ("\n".join(cat_lines) if cat_lines else "")
            + "\n\n<i>Этот документ общий для дебатов и авто-мотивации.</i>"
        )
    except ImportError:
        return "📄 <b>Документ</b>\n\n⚠ Модуль debates не найден."


# ─── Router ───────────────────────────────────────────────────────────────────


def build_motivation_auto_router() -> Router:
    router = Router(name="motivation_auto_addon")

    # ── Main screen ──

    @router.callback_query(F.data == "motauto:home")
    async def on_home(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        text = engine.get_status_text()
        await _edit_safe(query, text, _kb_main())

    # ── Toggle mode ──

    @router.callback_query(F.data == "motauto:toggle_mode")
    async def on_toggle_mode(query: CallbackQuery) -> None:
        current = engine.get_mode()
        new = "manual" if current == "auto" else "auto"
        engine.set_mode(new)
        label = "🟢 Авто" if new == "auto" else "⚪ Вручную"
        await query.answer(f"Режим: {label}", show_alert=False)
        text = engine.get_status_text()
        await _edit_safe(query, text, _kb_main())

    # ── Interval ──

    @router.callback_query(F.data == "motauto:interval")
    async def on_interval(query: CallbackQuery, state: FSMContext) -> None:
        current = engine.get_interval()
        eff = engine._effective_interval()
        mem = engine.storage.get_history_limit()

        text = (
            f"<b>🔢 Интервал авто-мотивации</b>\n\n"
            f"Текущий: <b>{eff}</b> сообщений"
        )
        if current > 0:
            text += " (задано вручную)"
        elif mem > 100:
            text += " (авто: память > 100 → 40)"
        else:
            text += f" (авто: память {mem} + 1)"
        text += (
            "\n\nВведи число (1–1000) для ручной настройки, "
            "или <b>0</b> для авто-расчёта."
        )
        await state.set_state(MotAutoStates.awaiting_interval)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_IKB(text="↩️ Отмена", callback_data="motauto:home")],
        ])
        await _edit_safe(query, text, kb)

    @router.callback_query(
        F.data == "motauto:home",
        MotAutoStates.awaiting_interval,
    )
    async def on_cancel_interval(
        query: CallbackQuery, state: FSMContext
    ) -> None:
        await state.clear()
        text = engine.get_status_text()
        await _edit_safe(query, text, _kb_main())

    @router.message(MotAutoStates.awaiting_interval)
    async def capture_interval(
        message: Message, state: FSMContext
    ) -> None:
        raw = (message.text or "").strip()
        try:
            n = int(raw)
            if n < 0 or n > 1000:
                raise ValueError
        except ValueError:
            await message.answer(
                "Нужно число от 0 до 1000. 0 = авто-расчёт.")
            return
        engine.set_interval(n)
        await state.clear()
        label = "авто-расчёт" if n == 0 else f"{n} сообщений"
        await message.answer(
            f"✅ Интервал установлен: <b>{label}</b>")
        # Show main screen
        await message.answer(
            engine.get_status_text(),
            reply_markup=_kb_main(),
        )

    # ── Document management ──

    @router.callback_query(F.data == "motauto:doc")
    async def on_doc(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        text = _doc_summary_text()
        await _edit_safe(query, text, _kb_doc())

    @router.callback_query(F.data == "motauto:doc_view")
    async def on_doc_view(query: CallbackQuery) -> None:
        try:
            from ..debates import motivation as _mot
            doc = _mot.get_doc()
        except ImportError:
            doc = None

        if not doc:
            await query.answer("Документ не загружен", show_alert=True)
            return

        raw = doc.get("raw_content", "")
        # Telegram message limit ~4096 chars — send as file if too long
        if query.message:
            if len(raw) > 3500:
                # Send first 3000 chars + note
                preview = raw[:3000] + "\n\n… (обрезано, документ слишком длинный)"
                preview = (
                    preview.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                await query.message.edit_text(
                    f"<pre>{preview}</pre>",
                    reply_markup=_kb_doc_view(),
                )
            else:
                escaped = (
                    raw.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                await query.message.edit_text(
                    f"<pre>{escaped}</pre>",
                    reply_markup=_kb_doc_view(),
                )
        await query.answer()

    @router.callback_query(F.data == "motauto:doc_upload")
    async def on_doc_upload(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(MotAutoStates.awaiting_doc_upload)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_IKB(text="↩️ Отмена", callback_data="motauto:doc")],
        ])
        text = (
            "<b>📥 Загрузка документа</b>\n\n"
            "Отправь файл (.md или .txt) с документом контекстных "
            "инъекций.\n\n"
            "Поддерживаемые форматы:\n"
            "• CONTEXT_INJECTION_SYSTEM v3 (5 категорий A-E)\n"
            "• MOTIVATIONAL_INJECTION_SYSTEM v2 (4 категории A-D)\n"
            "• Простой формат (Интервал + фразы)"
        )
        await _edit_safe(query, text, kb)

    @router.message(MotAutoStates.awaiting_doc_upload)
    async def capture_doc_upload(
        message: Message, state: FSMContext
    ) -> None:
        text_content: str | None = None

        # Check for file attachment
        doc = message.document
        if doc:
            from aiogram import Bot
            bot: Bot = message.bot  # type: ignore[assignment]
            try:
                file = await bot.get_file(doc.file_id)
                from io import BytesIO
                buf = BytesIO()
                await bot.download_file(file.file_path, buf)  # type: ignore[arg-type]
                text_content = buf.getvalue().decode("utf-8", errors="replace")
            except Exception as exc:
                logger.warning("doc download failed: %s", exc)
                await message.answer("⚠ Не удалось скачать файл. Попробуй ещё раз.")
                return
        elif message.text and len(message.text) > 50:
            text_content = message.text
        else:
            await message.answer(
                "Отправь файл (.md/.txt) или вставь текст документа "
                "(минимум 50 символов).")
            return

        # Parse
        try:
            from ..debates import motivation as _mot
        except ImportError:
            await message.answer("⚠ Модуль debates не найден.")
            await state.clear()
            return

        categories, timing, total = _mot.parse_motivation_doc(text_content)
        if total == 0:
            await message.answer(
                "⚠ Не удалось распарсить фразы из документа. "
                "Проверь формат (A01: \"...\", B01: \"...\" и т.д.).")
            return

        name = _mot._extract_title(text_content, "Загруженный документ")
        _mot.set_doc(name, text_content, categories, timing, total)
        await state.clear()

        cat_parts = []
        _emoji = {"A": "🔴", "B": "🟢", "C": "⚡", "D": "🔵", "E": "🧠"}
        for k in ("A", "B", "C", "D", "E"):
            items = categories.get(k, [])
            if items:
                cat_parts.append(f"{_emoji.get(k, '•')} {k}: {len(items)}")

        await message.answer(
            f"✅ Документ загружен!\n\n"
            f"📄 {name}\n"
            f"Фраз: {total}\n"
            + "\n".join(cat_parts))
        await message.answer(
            engine.get_status_text(),
            reply_markup=_kb_main(),
        )

    @router.callback_query(F.data == "motauto:doc_delete")
    async def on_doc_delete(query: CallbackQuery) -> None:
        try:
            from ..debates import motivation as _mot
            deleted = _mot.delete_doc()
        except ImportError:
            deleted = False

        if deleted:
            await query.answer("Документ удалён", show_alert=False)
        else:
            await query.answer("Документ не найден", show_alert=True)

        text = _doc_summary_text()
        await _edit_safe(query, text, _kb_doc())

    # ── Back to settings ──

    @router.callback_query(F.data == "motauto:back")
    async def on_back(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        # Rebuild the main settings screen — same as memory:back etc.
        try:
            from ...wizard import _kb_settings_menu
            uid = query.from_user.id if query.from_user else None
            kb = _kb_settings_menu(uid)
            if query.message:
                await query.message.edit_text(
                    "<b>⚙️ Настройки</b>", reply_markup=kb)
        except Exception:  # noqa: BLE001
            logger.debug("motauto: back to settings failed", exc_info=True)
        await query.answer()

    return router
