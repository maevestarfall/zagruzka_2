# План карта Telegram UI: link create/toggle, export & import via Master Room storage.
# Co-authored with CoCo
"""Telegram UI for «🗺 План карта» (pm:* callbacks).

Main-menu button opens a screen with exactly four actions:

1. «🔗 Создать ссылку» — mints a random token (Fable5 principle) and
   prints the editor URL; while enabled an extra «🔴 Выключить ссылку»
   row kills the link instantly.
2. «📤 Экспорт плана» — sends ``plan_map.json`` from the Master Room
   storage (``/data/storage``) as a Telegram document.
3. «📥 Импорт плана» — accepts a ``plan_map.json`` document, validates
   it and puts it back into the Master Room storage to continue work.
4. «↩️ Вернуться в главное меню».
"""

from __future__ import annotations

import contextlib
import json
import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import server

logger = logging.getLogger(__name__)

_MAX_IMPORT_BYTES = 2 * 1024 * 1024  # keep in sync with server._MAX_PLAN_BYTES


class PlanMapStates(StatesGroup):
    awaiting_import = State()


def _html_escape(s: object) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def _is_owner_query(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner  # local import to avoid cycle
    return _is_owner(query.from_user.id if query.from_user else 0)


async def _is_owner_msg(message: Message) -> bool:
    from ...wizard import _is_owner
    return _is_owner(message.from_user.id if message.from_user else 0)


async def _deny(query: CallbackQuery) -> None:
    await query.answer("Только владелец.", show_alert=True)


async def _send_or_edit(
    query: CallbackQuery, body: str, kb: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await query.message.edit_text(
            body, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception:  # noqa: BLE001
        try:
            await query.message.answer(
                body, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("plan_map: failed to send screen")


# ─── Home screen ──────────────────────────────────────────────────────────────


def _render_home() -> tuple[str, InlineKeyboardMarkup]:
    plan = server.load_plan()
    n_nodes = len(plan.get("nodes", []))
    n_edges = len(plan.get("edges", []))
    enabled = server.is_enabled()
    link = server.get_link() if enabled else ""

    lines = ["<b>🗺 План карта</b>", ""]
    lines.append(
        "Цепочка действий: квадратики с текстом, названия над ними "
        "и стрелки для очерёдности."
    )
    lines.append("")
    lines.append(f"📦 Квадратов: <b>{n_nodes}</b> · ➡️ Стрелок: <b>{n_edges}</b>")
    if enabled and link:
        lines.append(f"🟢 Ссылка активна:\n<code>{_html_escape(link)}</code>")
    else:
        lines.append("⚪ Ссылка выключена — сгенерируй, чтобы открыть редактор.")
    lines.append("")
    lines.append(
        "План хранится в хранилище Master Room "
        "(<code>/data/storage/plan_map.json</code>)."
    )

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🔗 Создать ссылку", callback_data="pm:link")],
    ]
    if enabled:
        rows.append(
            [InlineKeyboardButton(text="🔴 Выключить ссылку", callback_data="pm:link:off")]
        )
    rows.append(
        [InlineKeyboardButton(text="📤 Экспорт плана", callback_data="pm:export")]
    )
    rows.append(
        [InlineKeyboardButton(text="📥 Импорт плана", callback_data="pm:import")]
    )
    rows.append(
        [InlineKeyboardButton(text="↩️ Вернуться в главное меню", callback_data="pm:back")]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def build_plan_map_router() -> Router:
    router = Router(name="plan_map")

    # --- Entry / nav ------------------------------------------------------

    @router.callback_query(F.data.in_({"main:planmap", "pm:home"}))
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        body, kb = _render_home()
        await _send_or_edit(query, body, kb)

    @router.callback_query(F.data == "pm:back")
    async def _back(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        uid = query.from_user.id if query.from_user else 0
        try:
            from ...wizard import _kb_main_after_claim
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await query.message.edit_text(
                        "Главное меню. Выбери что сделать:",
                        reply_markup=_kb_main_after_claim(uid),
                    )
        except Exception:  # noqa: BLE001
            if query.message is not None:
                await query.message.answer("Главное меню — нажми /start")

    # --- 1) Создать / выключить ссылку (принцип Fable5) --------------------

    @router.callback_query(F.data == "pm:link")
    async def _link_on(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        _token, link = server.generate_link()
        await query.answer("Ссылка сгенерирована.")
        if not link.startswith("http"):
            body = (
                "<b>🔗 Ссылка создана</b>\n\n"
                "⚠️ Публичный URL сервера не определён (PUBLIC_URL). "
                f"Путь редактора:\n<code>{_html_escape(link)}</code>"
            )
        else:
            body = (
                "<b>🔗 Ссылка создана</b>\n\n"
                f"<code>{_html_escape(link)}</code>\n\n"
                "Открой её в браузере — там редактор плана: добавляй "
                "квадраты, пиши название сверху и текст внутри, соединяй "
                "стрелками.\n\n"
                "«Создать ссылку» всегда меняет токен — старая ссылка "
                "умирает. «Выключить» гасит доступ мгновенно."
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Выключить ссылку", callback_data="pm:link:off")],
            [InlineKeyboardButton(text="« Назад", callback_data="pm:home")],
        ])
        await _send_or_edit(query, body, kb)

    @router.callback_query(F.data == "pm:link:off")
    async def _link_off(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        server.disable_link()
        await query.answer("Ссылка выключена.")
        body, kb = _render_home()
        await _send_or_edit(query, body, kb)

    # --- 2) Экспорт плана (из хранилища Master Room) ------------------------

    @router.callback_query(F.data == "pm:export")
    async def _export(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        plan = server.load_plan()
        if not plan.get("nodes"):
            await query.answer("План пуст — нечего экспортировать.", show_alert=True)
            return
        await query.answer()
        data = json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            await query.message.answer_document(
                BufferedInputFile(data, filename=server.PLAN_FILE_NAME),
                caption=(
                    "📤 Экспорт плана из хранилища Master Room.\n"
                    "Сохрани файл — потом импортируешь и продолжишь."
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("plan_map: export failed")
            await query.message.answer(f"❌ Не удалось отправить файл: {e}")

    # --- 3) Импорт плана (в хранилище Master Room) ---------------------------

    @router.callback_query(F.data == "pm:import")
    async def _import_prompt(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(PlanMapStates.awaiting_import)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>📥 Импорт плана</b>\n\n"
            "Пришли файл <code>plan_map.json</code> как документ "
            "(📎 → файл) — тот, что получил через «Экспорт плана».\n\n"
            "⚠️ Текущий план в хранилище будет заменён.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="pm:home")],
            ]),
        )

    @router.message(PlanMapStates.awaiting_import)
    async def _import_receive(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        if message.text and message.text.startswith("/"):
            # Let global command handlers (/cancel, /start, ...) run.
            raise SkipHandler
        doc = message.document
        if not doc:
            await message.answer("Нужен файл как документ (📎 → файл).")
            return
        if doc.file_size and doc.file_size > _MAX_IMPORT_BYTES:
            await message.answer("❌ Файл больше 2 МБ — это не похоже на план.")
            return
        status = await message.answer("⏳ Импортирую...")
        try:
            from aiogram import Bot
            bot: Bot = message.bot  # type: ignore[assignment]
            file = await bot.get_file(doc.file_id)
            buf = await bot.download_file(file.file_path)
            raw = json.loads(buf.read().decode("utf-8"))
        except json.JSONDecodeError:
            await status.edit_text("❌ Это не JSON. Нужен файл из «Экспорт плана».")
            return
        except Exception as e:  # noqa: BLE001
            await status.edit_text(f"❌ Не удалось скачать файл: {e}")
            return
        plan = server.normalize_plan(raw)
        if plan is None:
            await status.edit_text(
                "❌ Неверный формат плана. Нужен файл из «Экспорт плана» "
                "(поля nodes / edges)."
            )
            return
        await state.clear()
        try:
            server.save_plan(plan)
        except Exception as e:  # noqa: BLE001
            logger.exception("plan_map: import save failed")
            await status.edit_text(f"❌ Не удалось сохранить план: {e}")
            return
        with contextlib.suppress(Exception):
            await status.delete()
        body, kb = _render_home()
        await message.answer(
            f"✅ План импортирован: квадратов — {len(plan['nodes'])}, "
            f"стрелок — {len(plan['edges'])}.\n\n{body}",
            parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True,
        )

    return router
