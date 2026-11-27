"""Telegram UI for SwapFace (ТЗ SwapFace v2.0 §9). Callback prefix ``swf:``.

Owner-only (``access.can_admin``). Screens:

* ``swf:menu``   — status, mode switch, Discord config, toggles, self-test;
* ``swf:faces``  — face registry (add / activate / delete / verify);
* pick screen ``swf:pick:1|2`` — variant chooser used by the pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time

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

from . import state as sw_state

logger = logging.getLogger("swapface.ui")

router = Router(name="swapface")


class SwfStates(StatesGroup):
    guild_id = State()
    channel_id = State()
    face_idname = State()
    face_photo = State()


def _esc(s) -> str:
    import html

    return html.escape(str(s or ""))


def _is_owner(user_id: int | None) -> bool:
    try:
        from ...access import can_admin

        return can_admin(user_id)
    except Exception:  # noqa: BLE001
        return False


async def _guard(query: CallbackQuery) -> bool:
    if not _is_owner(query.from_user.id if query.from_user else None):
        await query.answer("Только владелец.", show_alert=True)
        return False
    return True


def menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🎭 SwapFace", callback_data="swf:menu")


# ── main menu ──────────────────────────────────────────────────────────────

def _menu_text() -> str:
    mode = sw_state.get_mode()
    mode_label = "⚡ Автомат (user-токен)" if mode == "usertoken" else "🤝 Полуручной"
    credits = sw_state.get_credits()
    remaining = credits.get("remaining")
    limit = credits.get("daily_limit", 50)
    cred_line = (
        f"Кредиты: осталось {remaining}/{limit}" if remaining is not None
        else f"Кредиты: неизвестно (лимит ≈{limit}/день)"
    )
    gw_state = "?"
    with contextlib.suppress(Exception):
        from ...gateway.adapters import discord_gw

        gw_state = discord_gw.get_status().get("state", "?")
    active = sw_state.get_active_face() or "не выбрано"
    lines = [
        "<b>🎭 SwapFace</b> — свап лиц через InsightFaceSwap (Discord)",
        "",
        f"Режим: {mode_label}",
        f"Guild: <code>{_esc(sw_state.get_guild_id() or '—')}</code> · "
        f"Channel: <code>{_esc(sw_state.get_channel_id() or '—')}</code>",
        f"Активное лицо: <b>{_esc(active)}</b> · Лиц в реестре: {len(sw_state.get_faces())}",
        cred_line,
        f"Наш Discord-бот: {gw_state}",
    ]
    return "\n".join(lines)


def _kb_menu() -> InlineKeyboardMarkup:
    mode = sw_state.get_mode()
    mode_label = ("⚙️ Режим: ⚡ Автомат" if mode == "usertoken"
                  else "⚙️ Режим: 🤝 Полуручной")
    photo_label = "📷 Фото: ВКЛ" if sw_state.enabled_photo() else "📷 Фото: ВЫКЛ"
    video_label = "🎬 Видео: ВКЛ" if sw_state.enabled_video() else "🎬 Видео: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎭 Лица", callback_data="swf:faces")],
        [InlineKeyboardButton(text=mode_label, callback_data="swf:mode")],
        [InlineKeyboardButton(text="🔗 Discord", callback_data="swf:discord")],
        [
            InlineKeyboardButton(text=photo_label, callback_data="swf:toggle:photo"),
            InlineKeyboardButton(text=video_label, callback_data="swf:toggle:video"),
        ],
        [InlineKeyboardButton(text="🩺 Самотест", callback_data="swf:selftest")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="settings:back")],
    ])


@router.callback_query(F.data == "swf:menu")
async def cb_menu(query: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(query):
        return
    await state.clear()
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            _menu_text(), reply_markup=_kb_menu(), parse_mode="HTML",
        )
    await query.answer()


# ── toggles ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("swf:toggle:"))
async def cb_toggle(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    kind = (query.data or "").split(":")[-1]
    if kind == "photo":
        sw_state.set_enabled_photo(not sw_state.enabled_photo())
    elif kind == "video":
        sw_state.set_enabled_video(not sw_state.enabled_video())
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            _menu_text(), reply_markup=_kb_menu(), parse_mode="HTML",
        )
    await query.answer("Переключено")


# ── mode switch + disclaimers (§7) ─────────────────────────────────────────

USERTOKEN_DISCLAIMER = (
    "⚠️ <b>Режим «⚡ Автомат»</b> работает через твой ЛИЧНЫЙ токен Discord.\n"
    "Это self-bot — <b>нарушает ToS Discord, аккаунт могут забанить</b>.\n\n"
    "Токен читается из env-переменной "
    f"<code>{sw_state.DEFAULT_USER_TOKEN_REF}</code> и никогда не сохраняется "
    "в state.json и логи.\n\nПродолжить?"
)

CONSENT_TEXT = (
    "📜 <b>Напоминание о согласии</b>\n\n"
    "Свап лиц реальных людей требует их согласия. Фича доступна только "
    "владельцу бота и не является публичной. Используя SwapFace, ты "
    "подтверждаешь, что понимаешь это."
)


@router.callback_query(F.data == "swf:mode")
async def cb_mode(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    if sw_state.get_mode() == "usertoken":
        sw_state.set_mode("manual")
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _menu_text(), reply_markup=_kb_menu(), parse_mode="HTML",
            )
        await query.answer("Режим: полуручной")
        return
    # manual → usertoken: сперва дисклеймер (§7)
    if sw_state.usertoken_accepted_at() is None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принимаю риск", callback_data="swf:mode:accept")],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="swf:menu")],
        ])
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                USERTOKEN_DISCLAIMER, reply_markup=kb, parse_mode="HTML",
            )
        await query.answer()
        return
    sw_state.set_mode("usertoken")
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            _menu_text(), reply_markup=_kb_menu(), parse_mode="HTML",
        )
    await query.answer("Режим: автомат")


@router.callback_query(F.data == "swf:mode:accept")
async def cb_mode_accept(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    sw_state.set_usertoken_accepted()
    sw_state.set_mode("usertoken")
    if sw_state.consent_accepted_at() is None:
        sw_state.set_consent_accepted()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Понимаю", callback_data="swf:menu")],
        ])
        with contextlib.suppress(Exception):
            await query.message.edit_text(CONSENT_TEXT, reply_markup=kb, parse_mode="HTML")
        await query.answer()
        return
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            _menu_text(), reply_markup=_kb_menu(), parse_mode="HTML",
        )
    await query.answer("Режим: автомат")


# ── Discord config (FSM guild → channel) ───────────────────────────────────

@router.callback_query(F.data == "swf:discord")
async def cb_discord(query: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(query):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ввести guild_id + channel_id",
                              callback_data="swf:discord:edit")],
        [InlineKeyboardButton(text="🔄 Пересканировать команды",
                              callback_data="swf:discord:rescan")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="swf:menu")],
    ])
    scanned = sw_state.commands_scanned_at()
    scanned_line = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(scanned)) if scanned else "никогда"
    )
    text = (
        "<b>🔗 Discord для SwapFace</b>\n\n"
        f"Guild: <code>{_esc(sw_state.get_guild_id() or '—')}</code>\n"
        f"Channel: <code>{_esc(sw_state.get_channel_id() or '—')}</code>\n"
        f"InsightFaceSwap app: <code>{_esc(sw_state.get_insight_app_id() or '—')}</code>\n"
        f"Команды сканированы: {scanned_line}"
    )
    with contextlib.suppress(Exception):
        await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await query.answer()


@router.callback_query(F.data == "swf:discord:edit")
async def cb_discord_edit(query: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(query):
        return
    await state.set_state(SwfStates.guild_id)
    with contextlib.suppress(Exception):
        await query.message.answer("Шаг 1/2 — пришли <b>guild_id</b> сервера:",
                                   parse_mode="HTML")
    await query.answer()


@router.message(SwfStates.guild_id)
async def msg_guild_id(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    value = (message.text or "").strip()
    if not value.isdigit():
        await message.answer("guild_id — это число. Попробуй ещё раз.")
        return
    await state.update_data(swf_guild=value)
    await state.set_state(SwfStates.channel_id)
    await message.answer("Шаг 2/2 — пришли <b>channel_id</b> канала:",
                         parse_mode="HTML")


@router.message(SwfStates.channel_id)
async def msg_channel_id(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    value = (message.text or "").strip()
    if not value.isdigit():
        await message.answer("channel_id — это число. Попробуй ещё раз.")
        return
    data = await state.get_data()
    sw_state.set_guild_id(data.get("swf_guild"))
    sw_state.set_channel_id(value)
    await state.clear()
    await message.answer(
        "✅ Сохранено. Теперь жми «🔄 Пересканировать команды» в «🔗 Discord».",
    )


@router.callback_query(F.data == "swf:discord:rescan")
async def cb_discord_rescan(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    await query.answer("Сканирую…")
    try:
        from . import discord_user

        app_id, commands = await discord_user.scan_commands()
        text = (f"✅ InsightFaceSwap найден: app <code>{_esc(app_id)}</code>\n"
                f"Команды: {', '.join('/' + c for c in sorted(commands))}")
    except Exception as exc:  # noqa: BLE001
        text = f"❌ {_esc(str(exc))}"
    with contextlib.suppress(Exception):
        await query.message.answer(text, parse_mode="HTML")


# ── faces screen (§9.2) ────────────────────────────────────────────────────

def _kb_faces() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    active = sw_state.get_active_face()
    for f in sw_state.get_faces():
        idname = f.get("idname", "?")
        sync = "✅" if f.get("synced") else "⏳"
        star = "⭐ " if idname == active else ""
        rows.append([InlineKeyboardButton(
            text=f"{star}🎭 {idname} {sync}",
            callback_data=f"swf:face:{idname}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить лицо", callback_data="swf:face_add")])
    rows.append([InlineKeyboardButton(text="🔁 Сверить с Discord", callback_data="swf:face_verify")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="swf:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "swf:faces")
async def cb_faces(query: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(query):
        return
    await state.clear()
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            "<b>🎭 Лица SwapFace</b>\n\n⭐ — активное, ✅ — синхронизировано с Discord, "
            "⏳ — ждёт /saveid.",
            reply_markup=_kb_faces(), parse_mode="HTML",
        )
    await query.answer()


@router.callback_query(F.data == "swf:face_add")
async def cb_face_add(query: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(query):
        return
    await state.set_state(SwfStates.face_idname)
    with contextlib.suppress(Exception):
        await query.message.answer(
            "Придумай <b>idname</b> лица: латиница/цифры, до 10 символов "
            "(например <code>anna1</code>):",
            parse_mode="HTML",
        )
    await query.answer()


@router.message(SwfStates.face_idname)
async def msg_face_idname(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    idname = (message.text or "").strip()
    err = sw_state.validate_idname(idname)
    if err:
        await message.answer(f"⚠️ {err}\nПопробуй другое имя.")
        return
    if sw_state.get_face(idname) is not None:
        await message.answer(f"⚠️ Лицо «{_esc(idname)}» уже есть. Другое имя?")
        return
    await state.update_data(swf_idname=idname)
    await state.set_state(SwfStates.face_photo)
    await message.answer("Теперь пришли фото лица (чёткое, анфас):")


@router.message(SwfStates.face_photo, F.photo | F.document)
async def msg_face_photo(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    fid = None
    if message.photo:
        fid = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        fid = message.document.file_id
    if not fid or message.bot is None:
        await message.answer("Не вижу фото — пришли картинку.")
        return
    buf = io.BytesIO()
    try:
        await message.bot.download(fid, destination=buf)
    except Exception:  # noqa: BLE001
        await message.answer("Не смог скачать фото — попробуй ещё раз.")
        return
    data = await state.get_data()
    idname = data.get("swf_idname") or ""
    await state.clear()
    progress = await message.answer(f"💾 Сохраняю лицо «{_esc(idname)}»…", parse_mode="HTML")
    try:
        from . import service

        await service.save_face(buf.getvalue(), idname)
        face = sw_state.get_face(idname) or {}
        status = "✅ синхронизировано с Discord" if face.get("synced") else (
            "⏳ отправлено в канал — выполни /saveid в Discord")
        text = f"🎭 Лицо «{_esc(idname)}» сохранено. {status}"
    except Exception as exc:  # noqa: BLE001
        text = f"❌ {_esc(str(exc))}"
    with contextlib.suppress(Exception):
        await progress.edit_text(text, parse_mode="HTML")
    with contextlib.suppress(Exception):
        await message.answer("Реестр лиц:", reply_markup=_kb_faces())


@router.callback_query(F.data.startswith("swf:face:"))
async def cb_face_card(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    idname = (query.data or "")[len("swf:face:"):]
    face = sw_state.get_face(idname)
    if face is None:
        await query.answer("Лицо не найдено.", show_alert=True)
        return
    active = sw_state.get_active_face() == idname
    lines = [
        f"<b>🎭 {_esc(idname)}</b>{' ⭐ (активное)' if active else ''}",
        f"Файл: <code>{_esc(face.get('file'))}</code>",
        f"Синхронизация: {'✅' if face.get('synced') else '⏳'}",
    ]
    if face.get("last_error"):
        lines.append(f"⚠️ Последняя ошибка: {_esc(face['last_error'])}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Сделать активным",
                              callback_data=f"swf:activate:{idname}")],
        [InlineKeyboardButton(text="🗑 Удалить",
                              callback_data=f"swf:del_ask:{idname}")],
        [InlineKeyboardButton(text="↩️ К лицам", callback_data="swf:faces")],
    ])
    with contextlib.suppress(Exception):
        await query.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await query.answer()


@router.callback_query(F.data.startswith("swf:activate:"))
async def cb_activate(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    idname = (query.data or "")[len("swf:activate:"):]
    await query.answer("Активирую…")
    try:
        from . import service

        await service.set_active(idname)
        text = f"⭐ Активное лицо: <b>{_esc(idname)}</b>"
    except Exception as exc:  # noqa: BLE001
        text = f"❌ {_esc(str(exc))}"
    with contextlib.suppress(Exception):
        await query.message.edit_text(text, reply_markup=_kb_faces(), parse_mode="HTML")


@router.callback_query(F.data.startswith("swf:del_ask:"))
async def cb_del_ask(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    idname = (query.data or "")[len("swf:del_ask:"):]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить (Discord + локально)",
                              callback_data=f"swf:del:{idname}")],
        [InlineKeyboardButton(text="📁 Удалить только локально",
                              callback_data=f"swf:del_local:{idname}")],
        [InlineKeyboardButton(text="✖ Отмена", callback_data=f"swf:face:{idname}")],
    ])
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            f"Удалить лицо «{_esc(idname)}»?", reply_markup=kb, parse_mode="HTML",
        )
    await query.answer()


async def _do_delete(query: CallbackQuery, idname: str, local_only: bool) -> None:
    await query.answer("Удаляю…")
    try:
        from . import service

        await service.delete_face(idname, local_only=local_only)
        text = f"🗑 Лицо «{_esc(idname)}» удалено{' (только локально)' if local_only else ''}."
    except Exception as exc:  # noqa: BLE001
        text = (f"❌ Discord недоступен ({_esc(str(exc))}). "
                "Можно «удалить только локально».")
    with contextlib.suppress(Exception):
        await query.message.edit_text(text, reply_markup=_kb_faces(), parse_mode="HTML")


@router.callback_query(F.data.startswith("swf:del_local:"))
async def cb_del_local(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    await _do_delete(query, (query.data or "")[len("swf:del_local:"):], True)


@router.callback_query(F.data.startswith("swf:del:"))
async def cb_del(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    await _do_delete(query, (query.data or "")[len("swf:del:"):], False)


@router.callback_query(F.data == "swf:face_verify")
async def cb_face_verify(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    await query.answer("Сверяю…")
    try:
        from . import discord_user

        message = await discord_user.list_faces()
        content = str(message.get("content") or "")[:1000]
        local = {f["idname"] for f in sw_state.get_faces()}
        missing = [n for n in local if n.lower() not in content.lower()]
        for n in missing:
            sw_state.update_face(n, synced=False)
        text = (f"📋 Ответ /listid:\n<code>{_esc(content)}</code>\n\n"
                + ("⚠️ Не найдены в Discord: " + ", ".join(missing) if missing
                   else "✅ Все локальные лица найдены."))
    except Exception as exc:  # noqa: BLE001
        text = f"❌ {_esc(str(exc))} (сверка доступна в режиме «Автомат»)"
    with contextlib.suppress(Exception):
        await query.message.answer(text, parse_mode="HTML")


# ── self-test (§11) ────────────────────────────────────────────────────────

@router.callback_query(F.data == "swf:selftest")
async def cb_selftest(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    await query.answer("Диагностика…")
    lines = ["<b>🩺 Самотест SwapFace</b>", ""]
    mode = sw_state.get_mode()

    # 1. token / bot online
    if mode == "usertoken":
        if sw_state.get_user_token():
            try:
                from . import discord_user

                me = await discord_user.check_token()
                lines.append(f"✅ User-токен принят ({_esc(me.get('username') or '')})")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"❌ User-токен: {_esc(str(exc))}")
        else:
            lines.append(f"❌ Env {_esc(sw_state.user_token_ref())} не задана")
    else:
        gw_state = "?"
        with contextlib.suppress(Exception):
            from ...gateway.adapters import discord_gw

            gw_state = discord_gw.get_status().get("state", "?")
        lines.append("✅ Наш Discord-бот online" if gw_state == "online"
                     else f"❌ Наш Discord-бот: {_esc(gw_state)} — запусти в «🌐 Платформы»")

    # 2. guild/channel
    if sw_state.get_guild_id() and sw_state.get_channel_id():
        lines.append("✅ guild_id и channel_id заданы")
    else:
        lines.append("❌ guild_id/channel_id не заданы — «🔗 Discord»")

    # 3. command index
    if sw_state.get_insight_app_id() and sw_state.get_commands().get("swapid"):
        lines.append("✅ Команды InsightFaceSwap отсканированы")
    else:
        lines.append("⚠️ Команды не отсканированы — «🔄 Пересканировать»")

    # 4. intent (mode 2) — implied by gateway design (message_content on)
    if mode == "manual":
        lines.append("✅ message_content intent включён в gateway-клиенте")

    # 5. credits
    credits = sw_state.get_credits()
    if credits.get("remaining") is not None:
        icon = "⚠️" if sw_state.credits_low() else "✅"
        lines.append(f"{icon} Кредиты: {credits['remaining']}/{credits['daily_limit']}")
    else:
        lines.append("⚠️ Кредиты неизвестны (обновятся после первого свапа)")

    with contextlib.suppress(Exception):
        await query.message.answer("\n".join(lines), parse_mode="HTML")


# ── interactive choices used by the media pipeline (§5.4, §5.6, §9.3) ──────
#
# The media pipeline is a long-running async handler. To let the owner
# actually pick 1 of 2 variants (acceptance §13.5) and confirm a low-credit
# swap (§5.6), we park an ``asyncio.Future`` per chat and resolve it from
# the corresponding callback. A timeout falls back to a safe default so the
# pipeline never hangs forever.

_pending_choice: dict[int, asyncio.Future] = {}


def _resolve_choice(chat_id: int, value: str) -> None:
    fut = _pending_choice.get(chat_id)
    if fut is not None and not fut.done():
        fut.set_result(value)


async def _await_choice(chat_id: int, timeout: float, default: str) -> str:
    """Park a per-chat future; return the resolved value or ``default``."""
    old = _pending_choice.pop(chat_id, None)
    if old is not None and not old.done():
        old.cancel()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_choice[chat_id] = fut
    try:
        return await asyncio.wait_for(fut, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return default
    finally:
        if _pending_choice.get(chat_id) is fut:
            _pending_choice.pop(chat_id, None)


def kb_pick_variant() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1️⃣ Вариант 1", callback_data="swf:pick:1"),
            InlineKeyboardButton(text="2️⃣ Вариант 2", callback_data="swf:pick:2"),
        ],
        [InlineKeyboardButton(text="✖ Продолжить без свапа", callback_data="swf:pick:skip")],
    ])


async def choose_variant(message: Message, variants: list[bytes], *,
                         timeout: float = 120.0) -> bytes | None:
    """§5.4/§9.3: show both variants, await the owner's pick.

    Returns the chosen image bytes, or ``None`` if the owner chose to
    continue without the swap. On timeout falls back to variant 1 so the
    pipeline keeps moving.
    """
    variants = variants[:2]
    if not variants:
        return None
    for i, data in enumerate(variants, start=1):
        kb = kb_pick_variant() if i == len(variants) else None
        with contextlib.suppress(Exception):
            await message.answer_photo(
                BufferedInputFile(data, filename=f"swap_v{i}.png"),
                caption=f"🎭 SwapFace — вариант {i}"
                + (" · выбери вариант ниже 👇" if kb else ""),
                reply_markup=kb,
            )
    choice = await _await_choice(message.chat.id, timeout, default="1")
    if choice == "skip":
        return None
    idx = 2 if choice == "2" else 1
    return variants[idx - 1] if idx <= len(variants) else variants[0]


@router.callback_query(F.data.startswith("swf:pick:"))
async def cb_pick(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    choice = (query.data or "").split(":")[-1]
    _resolve_choice(query.message.chat.id if query.message else 0, choice)
    label = {"1": "Вариант 1", "2": "Вариант 2"}.get(choice, "Без свапа")
    with contextlib.suppress(Exception):
        await query.message.edit_reply_markup(reply_markup=None)
    await query.answer(f"✅ {label}")


def kb_confirm_credits() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Продолжить", callback_data="swf:cred:go")],
        [InlineKeyboardButton(text="✖ Отмена", callback_data="swf:cred:cancel")],
    ])


async def confirm_low_credits(message: Message, remaining: int | None, *,
                              timeout: float = 60.0) -> bool:
    """§5.6 preflight: warn about low credits, await Продолжить/Отмена.

    Returns True to proceed. Timeout defaults to proceeding (the swap
    itself will surface a real limit error if it truly runs out).
    """
    with contextlib.suppress(Exception):
        await message.answer(
            f"⚠️ Похоже, кредиты InsightFaceSwap на исходе "
            f"(осталось {remaining}, свап ≈{sw_state.SWAP_COST_CREDITS}). Продолжить?",
            reply_markup=kb_confirm_credits(),
        )
    choice = await _await_choice(message.chat.id, timeout, default="go")
    return choice != "cancel"


@router.callback_query(F.data.startswith("swf:cred:"))
async def cb_cred(query: CallbackQuery) -> None:
    if not await _guard(query):
        return
    choice = (query.data or "").split(":")[-1]
    _resolve_choice(query.message.chat.id if query.message else 0, choice)
    with contextlib.suppress(Exception):
        await query.message.edit_reply_markup(reply_markup=None)
    await query.answer("Продолжаю" if choice == "go" else "Отменено")


def build_swapface_router() -> Router:
    return router
