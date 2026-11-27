"""Telegram UI + capture for «🔌 Каналы-источники» (srcch:* callbacks).

UI (private chat with the owner):
  • список источников, добавить / удалить;
  • 🟢/🔴 авто-проверка ВКЛ/ВЫКЛ;
  • ⏰ расписание: список времён, добавить / убрать время;
  • 🔍 проверить сейчас;
  • просмотр по категориям (✅ проверить / 🖼 медиа / 💻 код / 💳 деньги).

Capture (a separate router included FIRST in the dispatcher): every post
in a registered source chat is appended to the buffer and *not* answered
as a normal chat message.
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...access import can_admin
from . import processor, store

logger = logging.getLogger(__name__)


class SourceStates(StatesGroup):
    awaiting_source = State()
    awaiting_time = State()


def _esc(s: str) -> str:
    return html.escape(s or "")


# ── keyboards ──────────────────────────────────────────────────────────

def _kb_home() -> InlineKeyboardMarkup:
    auto = store.is_auto_enabled()
    auto_label = "🟢 Авто-проверка: ВКЛ" if auto else "🔴 Авто-проверка: ВЫКЛ"
    n_src = len(store.get_sources())
    n_pending = store.pending_count()
    rows = [
        [InlineKeyboardButton(text=f"📡 Источники ({n_src})", callback_data="srcch:sources")],
        [InlineKeyboardButton(text=auto_label, callback_data="srcch:auto")],
        [InlineKeyboardButton(text="⏰ Расписание проверок", callback_data="srcch:sched")],
        [InlineKeyboardButton(text=f"🔍 Проверить сейчас ({n_pending})", callback_data="srcch:check")],
    ]
    c = store.category_counts()
    rows.append([
        InlineKeyboardButton(text=f"✅ {c[store.CAT_CHECK]}", callback_data=f"srcch:cat:{store.CAT_CHECK}"),
        InlineKeyboardButton(text=f"🖼 {c[store.CAT_MEDIA]}", callback_data=f"srcch:cat:{store.CAT_MEDIA}"),
        InlineKeyboardButton(text=f"💻 {c[store.CAT_CODE]}", callback_data=f"srcch:cat:{store.CAT_CODE}"),
        InlineKeyboardButton(text=f"💳 {c[store.CAT_MONEY]}", callback_data=f"srcch:cat:{store.CAT_MONEY}"),
    ])
    rows.append([InlineKeyboardButton(text="↩️ В настройки", callback_data="main:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_sources() -> InlineKeyboardMarkup:
    rows = []
    for s in store.get_sources():
        emoji = {"channel": "📢", "group": "👥", "private": "👤", "site": "🌐"}.get(s.get("type"), "📡")
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {s.get('title', s.get('chat_id'))}",
            callback_data=f"srcch:src:info:{s['id']}",
        )])
        rows.append([InlineKeyboardButton(
            text="🗑 Удалить", callback_data=f"srcch:src:del:{s['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить источник", callback_data="srcch:add")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="srcch:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_sched() -> InlineKeyboardMarkup:
    rows = []
    for t in store.get_times():
        rows.append([
            InlineKeyboardButton(text=f"⏰ {t}", callback_data="srcch:noop"),
            InlineKeyboardButton(text="🗑", callback_data=f"srcch:time:del:{t}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить время", callback_data="srcch:time:add")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="srcch:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── renderers ──────────────────────────────────────────────────────────

def _render_home() -> str:
    n_src = len(store.get_sources())
    n_pending = store.pending_count()
    auto = "включена 🟢" if store.is_auto_enabled() else "выключена 🔴"
    times = store.get_times()
    return (
        "🔌 <b>Подключение к каналу источников</b>\n\n"
        "Добавь бота в Telegram-канал/чат, зарегистрируй его как источник — "
        "и кидай туда что угодно: API-ключи картинок, банковские карты, код, "
        "фото/видео или инструкции.\n\n"
        "ИИ сам разложит каждый пост по полкам: "
        "<b>✅ проверить</b> · <b>🖼 видео и фото</b> · <b>💻 код</b> · "
        "<b>💳 карта (деньги)</b>. Карты и ключи шифруются и кладутся в 🔒 Сейф, "
        "а инструкции — исполняются.\n\n"
        f"📡 Источников: <b>{n_src}</b>\n"
        f"📥 Ждут проверки: <b>{n_pending}</b>\n"
        f"⚙️ Авто-проверка: <b>{auto}</b>\n"
        f"⏰ Раз в день: <b>{len(times)}</b> ({', '.join(times) if times else '—'})"
    )


def _render_sched() -> str:
    times = store.get_times()
    return (
        "⏰ <b>Расписание авто-проверок</b>\n\n"
        f"Таймзона: <b>{_esc(store.get_tz())}</b>\n"
        f"Проверок в день: <b>{len(times)}</b>\n\n"
        "Бот будет проверять источники в эти моменты (если авто-проверка ВКЛ). "
        "Добавляй и убирай времена кнопками ниже."
    )


# ════════════════════════ UI ROUTER ════════════════════════════════════

def build_source_channels_router() -> Router:
    router = Router(name="source_channels_ui")

    async def _guard(query: CallbackQuery) -> bool:
        if not can_admin(query.from_user.id):
            await query.answer("Только владелец.", show_alert=True)
            return False
        # Remember where to send scheduled reports.
        store.set_owner_chat_id(query.message.chat.id)
        return True

    @router.callback_query(F.data.in_({"main:sources", "srcch:home"}))
    async def cb_home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _guard(query):
            return
        await state.clear()
        await query.message.edit_text(_render_home(), reply_markup=_kb_home())
        await query.answer()

    @router.callback_query(F.data == "srcch:noop")
    async def cb_noop(query: CallbackQuery) -> None:
        # The time chip is a label, not an action — tell the user how to edit.
        await query.answer("Это время проверки. Удалить — кнопкой 🗑 рядом.",
                           show_alert=False)

    # ── sources ────────────────────────────────────────────────────────
    @router.callback_query(F.data == "srcch:sources")
    async def cb_sources(query: CallbackQuery) -> None:
        if not await _guard(query):
            return
        srcs = store.get_sources()
        text = "📡 <b>Источники</b>\n\n" + (
            "\n".join(
                f"• {_esc(s.get('title', str(s.get('chat_id'))))} "
                f"(<code>{s.get('chat_id')}</code>)"
                for s in srcs
            ) if srcs else "<i>Пока нет источников. Добавь первый.</i>"
        )
        await query.message.edit_text(text, reply_markup=_kb_sources())
        await query.answer()

    @router.callback_query(F.data.startswith("srcch:src:del:"))
    async def cb_src_del(query: CallbackQuery) -> None:
        if not await _guard(query):
            return
        sid = query.data.split(":")[-1]
        ok = store.remove_source(sid)
        await query.answer("Удалено" if ok else "Не найдено")
        srcs = store.get_sources()
        text = "📡 <b>Источники</b>\n\n" + (
            "\n".join(
                f"• {_esc(s.get('title', str(s.get('chat_id'))))} "
                f"(<code>{s.get('chat_id')}</code>)" for s in srcs
            ) if srcs else "<i>Пока нет источников. Добавь первый.</i>"
        )
        await query.message.edit_text(text, reply_markup=_kb_sources())

    @router.callback_query(F.data.startswith("srcch:src:info:"))
    async def cb_src_info(query: CallbackQuery) -> None:
        if not await _guard(query):
            return
        sid = query.data.split(":")[-1]
        s = next((x for x in store.get_sources() if x["id"] == sid), None)
        if not s:
            await query.answer("Не найдено", show_alert=True)
            return
        await query.answer(
            f"{s.get('title')}\nID: {s.get('chat_id')}\nтип: {s.get('type')}",
            show_alert=True,
        )

    @router.callback_query(F.data == "srcch:add")
    async def cb_add(query: CallbackQuery, state: FSMContext) -> None:
        if not await _guard(query):
            return
        await state.set_state(SourceStates.awaiting_source)
        await query.message.edit_text(
            "➕ <b>Добавить источник</b>\n\n"
            "Источником может быть <b>сайт</b> или <b>Telegram-канал/чат</b>:\n"
            "🌐 пришли <b>ссылку на сайт</b> (<code>https://…</code>) — бот берёт оттуда контент при каждой проверке;\n"
            "1️⃣ <b>Перешли пост</b> из нужного канала/чата — я возьму его ID;\n"
            "2️⃣ или пришли <code>@username</code> канала;\n"
            "3️⃣ или числовой ID (например <code>-1001234567890</code>).\n\n"
            "⚠️ Для ТГ-канала бот должен быть его участником/админом.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="srcch:sources")]]),
        )
        await query.answer()

    @router.message(StateFilter(SourceStates.awaiting_source))
    async def msg_add_source(message: Message, state: FSMContext) -> None:
        await state.clear()
        bot = message.bot
        chat_id = None
        title = ""
        ctype = "channel"

        # 0) website source — a pasted URL
        raw_text = (message.text or "").strip()
        if raw_text.lower().startswith(("http://", "https://")):
            url = raw_text.split()[0]
            s = store.add_site_source(url)
            await message.answer(
                f"✅ Сайт-источник добавлен: <b>{_esc(s['title'])}</b>\n\n"
                "Бот будет брать оттуда контент при каждой проверке.",
                reply_markup=_kb_home())
            return

        # 1) forwarded post → forward_from_chat / forward_origin
        fwd = getattr(message, "forward_from_chat", None)
        if fwd is None:
            origin = getattr(message, "forward_origin", None)
            fwd = getattr(origin, "chat", None) if origin else None
        if fwd is not None:
            chat_id = fwd.id
            title = fwd.title or fwd.full_name or str(fwd.id)
            ctype = fwd.type if isinstance(fwd.type, str) else str(fwd.type)
        else:
            ref = (message.text or "").strip()
            if ref:
                try:
                    chat = await bot.get_chat(ref if ref.startswith("@") else int(ref))
                    chat_id = chat.id
                    title = chat.title or getattr(chat, "full_name", "") or str(chat.id)
                    ctype = chat.type if isinstance(chat.type, str) else str(chat.type)
                except Exception as exc:  # noqa: BLE001
                    await message.answer(
                        f"❌ Не вышло открыть «{_esc(ref)}»: {_esc(str(exc))}\n"
                        "Убедись, что бот добавлен в канал, и попробуй переслать пост.",
                        reply_markup=_kb_sources())
                    return

        if chat_id is None:
            await message.answer(
                "❌ Не понял. Перешли пост из канала или пришли @username / ID.",
                reply_markup=_kb_sources())
            return

        ctype = {"supergroup": "group", "group": "group",
                 "channel": "channel", "private": "private"}.get(ctype, "channel")
        s = store.add_source(chat_id, title, ctype)
        await message.answer(
            f"✅ Источник добавлен: <b>{_esc(s['title'])}</b> "
            f"(<code>{s['chat_id']}</code>)\n\n"
            "Теперь всё, что туда попадёт, будет ловиться и сортироваться.",
            reply_markup=_kb_home())

    # ── auto toggle ──────────────────────────────────────────────────────
    @router.callback_query(F.data == "srcch:auto")
    async def cb_auto(query: CallbackQuery) -> None:
        if not await _guard(query):
            return
        store.set_auto(not store.is_auto_enabled())
        await query.message.edit_text(_render_home(), reply_markup=_kb_home())
        await query.answer("Готово")

    # ── schedule ─────────────────────────────────────────────────────────
    @router.callback_query(F.data == "srcch:sched")
    async def cb_sched(query: CallbackQuery, state: FSMContext) -> None:
        if not await _guard(query):
            return
        await state.clear()
        await query.message.edit_text(_render_sched(), reply_markup=_kb_sched())
        await query.answer()

    @router.callback_query(F.data.startswith("srcch:time:del:"))
    async def cb_time_del(query: CallbackQuery) -> None:
        if not await _guard(query):
            return
        t = query.data.split("srcch:time:del:")[-1]
        ok = store.remove_time(t)
        await query.answer("Убрано" if ok else "Не найдено")
        await query.message.edit_text(_render_sched(), reply_markup=_kb_sched())

    @router.callback_query(F.data == "srcch:time:add")
    async def cb_time_add(query: CallbackQuery, state: FSMContext) -> None:
        if not await _guard(query):
            return
        await state.set_state(SourceStates.awaiting_time)
        await query.message.edit_text(
            "➕ <b>Добавить время</b>\n\n"
            "Пришли время в формате <code>ЧЧ:ММ</code> (например <code>09:30</code>).\n"
            f"Таймзона: <b>{_esc(store.get_tz())}</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="srcch:sched")]]),
        )
        await query.answer()

    @router.message(StateFilter(SourceStates.awaiting_time))
    async def msg_add_time(message: Message, state: FSMContext) -> None:
        await state.clear()
        norm = store.add_time((message.text or "").strip())
        if norm is None:
            await message.answer(
                "❌ Не похоже на время. Формат: <code>ЧЧ:ММ</code>, например 21:00.",
                reply_markup=_kb_sched())
            return
        await message.answer(
            f"✅ Время добавлено: <b>{norm}</b>. Теперь проверок в день: "
            f"<b>{len(store.get_times())}</b>.",
            reply_markup=_kb_sched())

    # ── manual check ─────────────────────────────────────────────────────
    @router.callback_query(F.data == "srcch:check")
    async def cb_check(query: CallbackQuery) -> None:
        if not await _guard(query):
            return
        await query.answer("Проверяю…")
        result = await processor.process_pending()
        await query.message.edit_text(result["report"], reply_markup=_kb_home())

    # ── category view ────────────────────────────────────────────────────
    @router.callback_query(F.data.startswith("srcch:cat:"))
    async def cb_cat(query: CallbackQuery) -> None:
        if not await _guard(query):
            return
        cat = query.data.split(":")[-1]
        items = store.items_in_category(cat, limit=15)
        label = store.CATEGORY_LABEL.get(cat, cat)
        if not items:
            text = f"{label}\n\n<i>Пока пусто.</i>"
        else:
            lines = [f"{label} — последние {len(items)}:\n"]
            for it in reversed(items):
                src = _esc(it.get("source_title", "?"))
                summ = _esc((it.get("summary") or it.get("text") or "")[:90])
                extra = " 🔒" if it.get("vaulted") else ""
                lines.append(f"• <b>{src}</b>: {summ}{extra}")
                if it.get("instruction_result"):
                    lines.append(f"  📜 → {_esc(it['instruction_result'][:150])}")
            text = "\n".join(lines)
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Назад", callback_data="srcch:home")]]),
        )
        await query.answer()

    return router


# ════════════════════════ CAPTURE ROUTER ═══════════════════════════════
#
# Registered FIRST (before the Groq catch-all) so posts in source chats
# are captured and not answered as normal chat messages.

def _kind_of(message: Message) -> tuple[str, str | None]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.document:
        return "document", message.document.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.audio:
        return "audio", message.audio.file_id
    txt = message.text or message.caption or ""
    if txt.strip().startswith("http"):
        return "url", None
    return "text", None


def _capture(message: Message) -> None:
    kind, file_id = _kind_of(message)
    src = store.find_source(message.chat.id)
    store.add_to_buffer({
        "source_id": src.get("id") if src else None,
        "source_title": (src.get("title") if src else None) or message.chat.title or str(message.chat.id),
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "kind": kind,
        "file_id": file_id,
        "text": message.text or "",
        "caption": message.caption or "",
    })
    # Auto-process immediately when auto-mode is on AND no schedule is set,
    # otherwise just buffer for the next scheduled / manual check.


def build_source_capture_router() -> Router:
    router = Router(name="source_channels_capture")

    def _is_source_chat(event) -> bool:
        try:
            return int(event.chat.id) in store.get_source_ids()
        except Exception:  # noqa: BLE001
            return False

    @router.channel_post(_is_source_chat)
    async def on_channel_post(message: Message) -> None:
        _capture(message)
        await _maybe_auto(message.bot)

    @router.edited_channel_post(_is_source_chat)
    async def on_edited_channel_post(message: Message) -> None:
        _capture(message)

    @router.message(_is_source_chat)
    async def on_group_message(message: Message) -> None:
        _capture(message)
        await _maybe_auto(message.bot)

    return router


async def _maybe_auto(bot: Bot) -> None:
    """If auto is on and the owner set NO times, process right away.

    When the owner has a daily schedule, capture only buffers and the
    scheduler does the work at the chosen times.
    """
    if not store.is_auto_enabled():
        return
    if store.get_times():
        return
    try:
        result = await processor.process_pending()
        chat_id = store.get_owner_chat_id()
        if chat_id is not None and result["processed"]:
            await bot.send_message(chat_id, result["report"])
    except Exception:  # noqa: BLE001
        logger.exception("source_channels: immediate auto-process failed")
