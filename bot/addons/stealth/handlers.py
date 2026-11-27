"""Telegram UI for «🕵️ Антидетект» (stl:* callbacks)."""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ...access import can_admin
from . import is_enabled, set_enabled

logger = logging.getLogger(__name__)


def _esc(s: str) -> str:
    return html.escape(s or "")


def _kb() -> InlineKeyboardMarkup:
    tog = "🟢 Антидетект: ВКЛ" if is_enabled() else "🔴 Антидетект: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить live-check", callback_data="stl:run")],
        [InlineKeyboardButton(text=tog, callback_data="stl:toggle")],
        [InlineKeyboardButton(text="↩️ Вернуться", callback_data="main:settings")],
    ])


def _render_home() -> str:
    state = "включён 🟢" if is_enabled() else "выключен 🔴"
    return (
        "🕵️ <b>Антидетект (stealth-отпечаток)</b>\n\n"
        f"Сейчас: <b>{state}</b>\n\n"
        "Подмена Canvas / WebGL / WebRTC / timezone / screen / languages "
        "при автоматизации браузера.\n\n"
        "• <b>Запустить live-check</b> — проверка спуфов в настоящем Chromium "
        "(реальные пробы + живой WebRTC).\n"
        "• <b>ВКЛ/ВЫКЛ</b> — включить или выключить антидетект для браузерных "
        "задач.\n"
        "• <b>Вернуться</b> — назад в настройки."
    )


def build_stealth_router() -> Router:
    router = Router(name="stealth")

    async def _guard(q: CallbackQuery) -> bool:
        if not can_admin(q.from_user.id):
            await q.answer("Только владелец.", show_alert=True)
            return False
        return True

    @router.callback_query(F.data.in_({"main:stealth", "stl:home"}))
    async def cb_home(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        await q.message.edit_text(_render_home(), reply_markup=_kb())
        await q.answer()

    @router.callback_query(F.data == "stl:toggle")
    async def cb_toggle(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        set_enabled(not is_enabled())
        await q.message.edit_text(_render_home(), reply_markup=_kb())
        await q.answer("Готово")

    @router.callback_query(F.data == "stl:run")
    async def cb_run(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        await q.answer("Запускаю live-check…")
        try:
            await q.message.edit_text(
                "🕵️ <b>Live-check</b>\nЗапускаю реальный Chromium и проверяю спуфы…")
        except Exception:  # noqa: BLE001
            pass
        try:
            from .. import stealth_fp_check
            result = await stealth_fp_check.live_check()
            report = stealth_fp_check.format_report(result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("stealth live-check failed")
            report = f"❌ Live-check не выполнен: {exc}"
        # Telegram-safe: escape and wrap in <pre>.
        body = f"<pre>{_esc(report)}</pre>"
        if len(body) > 3800:
            body = body[:3800] + "…</pre>"
        await q.message.edit_text(body, reply_markup=_kb())

    return router
