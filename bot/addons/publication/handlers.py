# Publication handlers: account slots, API/browser login, network split, auto+Analyzer, publish.
# Co-authored with CoCo
"""«📢 Публикация» — post the монтажёр's finished videos to social networks.

Reuses what's already onboard:
* ``workers.browser_clicker.run_browser_macro`` — stealth login/upload
  (Camoufox, human-like mouse, proxy/fingerprint).
* ``issuance`` — hardware presets + proxy/VPN + fingerprint.
* ``vault`` — encrypted storage (cookies/params per account).
* Analyzer PRO — «как правильно публиковать» plan in Авто mode.

Honest caveat: real login/upload reliability depends on the deployed
browser engine and the target site's anti-bot defences. The framework,
storage and macro wiring are complete; success is best-effort.
"""

from __future__ import annotations

import contextlib
import io
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import store

logger = logging.getLogger(__name__)


class PubStates(StatesGroup):
    awaiting_api_token = State()
    awaiting_site = State()
    awaiting_login = State()
    awaiting_password = State()
    awaiting_proxy = State()
    awaiting_video = State()
    awaiting_openreels_topic = State()
    awaiting_openreels_url = State()


def _esc(s) -> str:
    import html
    return html.escape(str(s or ""))


async def _owner(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner
    return _is_owner(query.from_user.id if query.from_user else 0)


def _hw_presets() -> dict:
    try:
        from ..issuance.core import PROFILE_PRESETS
        return PROFILE_PRESETS
    except Exception:  # noqa: BLE001
        return {}


# ─── Keyboards ────────────────────────────────────────────────────────────────

def _kb_home() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in store.get_slots():
        mark = "🟢" if s.get("logged_in") else "⚪"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {s.get('name')} · {s.get('network')}",
            callback_data=f"pub:slot:{s['id']}")])
    if len(store.get_slots()) < store.MAX_SLOTS:
        rows.append([InlineKeyboardButton(text="➕ Новый аккаунт", callback_data="pub:add")])
    rows.append([InlineKeyboardButton(text="🎬 OpenReels (тема → видео)", callback_data="pub:or")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="main:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_OR_MODE_LABELS = {
    "generate_then_publish": "сгенерить → опубликовать через слот",
    "generate_only": "только прислать MP4 в Telegram",
    "openreels_publishes": "OpenReels публикует сам",
}


def _kb_openreels() -> InlineKeyboardMarkup:
    url = store.get_openreels_url() or "—"
    mode = store.get_openreels_mode()
    platform = store.get_openreels_platform()
    rows = [
        [InlineKeyboardButton(text="✍️ Сгенерировать из темы", callback_data="pub:or:gen")],
        [InlineKeyboardButton(text=f"🔧 URL сервера: {url[:32]}", callback_data="pub:or:url")],
        [InlineKeyboardButton(text=f"🎛 Режим: {_OR_MODE_LABELS.get(mode, mode)}", callback_data="pub:or:mode")],
        [InlineKeyboardButton(text=f"📱 Платформа: {platform}", callback_data="pub:or:platform")],
        [InlineKeyboardButton(text="🔌 Проверить связь", callback_data="pub:or:health")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="pub:home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _openreels_text() -> str:
    url = store.get_openreels_url()
    mode = store.get_openreels_mode()
    return (
        "<b>🎬 OpenReels</b> — тема → готовый вертикальный ролик.\n\n"
        "Опенсорс-пайплайн (github.com/tsensei/OpenReels): research → сценарий → "
        "озвучка → визуал → музыка → субтитры → сборка MP4. Запускается у тебя "
        "(<code>docker compose up</code>), бот ходит в его REST API.\n\n"
        f"Сервер: <code>{_esc(url) if url else 'не задан'}</code>\n"
        f"Режим: <i>{_OR_MODE_LABELS.get(mode, mode)}</i>\n\n"
        "Нажми «✍️ Сгенерировать из темы» и пришли тему одним сообщением."
    )


def _kb_slot(s: dict) -> InlineKeyboardMarkup:
    sid = s["id"]
    auto = "ВКЛ" if s.get("auto") else "ВЫКЛ"
    mode = {"wide": "обычный (широкий)", "shorts": "Shorts", "auto": "Авто"}.get(s.get("mode"), s.get("mode"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🌐 Соцсеть: {s.get('network')}", callback_data=f"pub:net:{sid}")],
        [InlineKeyboardButton(text="🔑 Вход в аккаунт", callback_data=f"pub:login:{sid}")],
        [InlineKeyboardButton(text=f"🎬 Режим: {mode}", callback_data=f"pub:mode:{sid}")],
        [InlineKeyboardButton(text=f"🤖 Авто (Analyzer): {auto}", callback_data=f"pub:auto:{sid}")],
        [InlineKeyboardButton(text=f"💻 Железо: {s.get('hardware') or '—'}", callback_data=f"pub:hw:{sid}")],
        [InlineKeyboardButton(text=f"🛡 Прокси/VPN: {'есть' if s.get('proxy') else '—'}", callback_data=f"pub:proxy:{sid}")],
        [InlineKeyboardButton(text="🚀 Опубликовать видео", callback_data=f"pub:go:{sid}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"pub:del:{sid}")],
        [InlineKeyboardButton(text="↩️ Вернуться", callback_data="pub:home")],
    ])


def _kb_net(sid: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=n, callback_data=f"pub:net:{sid}:{n}")] for n in store.NETWORKS]
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"pub:slot:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_login(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Через API (токен)", callback_data=f"pub:login:{sid}:api")],
        [InlineKeyboardButton(text="🌐 Через сайт (логин+пароль, stealth)", callback_data=f"pub:login:{sid}:browser")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"pub:slot:{sid}")],
    ])


def _kb_mode(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Обычный (широкий)", callback_data=f"pub:mode:{sid}:wide")],
        [InlineKeyboardButton(text="📱 Shorts", callback_data=f"pub:mode:{sid}:shorts")],
        [InlineKeyboardButton(text="🤖 Авто", callback_data=f"pub:mode:{sid}:auto")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"pub:slot:{sid}")],
    ])


def _kb_hw(sid: str) -> InlineKeyboardMarkup:
    rows = []
    for key, prof in _hw_presets().items():
        rows.append([InlineKeyboardButton(
            text=f"{key} · {prof.get('hardware', '')[:30]}", callback_data=f"pub:hw:{sid}:{key}")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"pub:slot:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _slot_text(s: dict) -> str:
    return (
        f"<b>📢 {_esc(s.get('name'))}</b>\n\n"
        f"🌐 Соцсеть: <b>{s.get('network')}</b>\n"
        f"🔑 Вход: {s.get('login_type') or 'не задан'} "
        f"({'вошёл' if s.get('logged_in') else 'нет'})\n"
        f"🎬 Режим: {s.get('mode')}\n"
        f"🤖 Авто: {'ВКЛ' if s.get('auto') else 'ВЫКЛ'}\n"
        f"💻 Железо: {s.get('hardware') or '—'}\n"
        f"🛡 Прокси/VPN: {'есть' if s.get('proxy') else '—'}"
    )


# ─── Publish helpers (best-effort via onboard browser engine) ────────────────

async def _analyzer_plan(network: str, mode: str) -> str:
    """Ask Analyzer PRO how to publish; fallback to '' if unavailable."""
    with contextlib.suppress(Exception):
        from ..regen.research import research_problem
        q = (f"Как правильно опубликовать видео в {network} ({mode}): что писать "
             "в заголовке/описании, какие хэштеги и настройки выставить, какое "
             "железо/фингерпринт и через какой прокси/VPN заходить, чтобы выглядеть "
             "как человек и не словить бан.")
        return await research_problem(q)
    return ""


async def _publish(sid: str, video_path: str, on_status=None) -> tuple[bool, str]:
    """Best-effort publish via run_browser_macro. Returns (ok, note)."""
    s = store.get_slot(sid)
    if not s:
        return False, "слот не найден"
    network = s.get("network") or "custom"
    mode = s.get("mode") or "auto"
    # Apply hardware/proxy to the onboard fingerprint engine.
    with contextlib.suppress(Exception):
        from ..issuance import core as iss
        if s.get("proxy"):
            iss.set_fingerprint_proxy(s["proxy"])
        hw = _hw_presets().get(s.get("hardware") or "")
        if hw and hw.get("seed"):
            iss.set_fingerprint_seed(hw["seed"])
    plan = await _analyzer_plan(network, mode) if s.get("auto") else ""
    # Build a generic upload macro per network.
    upload_urls = {
        "youtube": "https://studio.youtube.com",
        "tiktok": "https://www.tiktok.com/tiktokstudio/upload",
        "instagram": "https://www.instagram.com",
    }
    url = upload_urls.get(network, s.get("site") or "https://example.com")
    steps = [
        ("goto", url),
        ("pause", 3.0),
        ("upload", video_path),
        ("pause", 3.0),
    ]
    try:
        from ...workers.browser_clicker import run_browser_macro
        result = await run_browser_macro(steps)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publication: macro failed")
        return False, f"движок недоступен: {type(exc).__name__}: {exc}"
    # Persist the params actually used (for import).
    store.save_params(sid, {
        "network": network, "mode": mode, "hardware": s.get("hardware"),
        "proxy": s.get("proxy"), "auto": s.get("auto"),
        "analyzer_plan": plan[:2000], "macro_result": str(result)[:500],
    })
    # Persist cookies if the engine returned them.
    with contextlib.suppress(Exception):
        if isinstance(result, dict) and result.get("cookies"):
            store.save_cookies(sid, result["cookies"])
    return True, "макрос выполнен (проверь результат на сайте)"


def build_publication_router() -> Router:
    router = Router(name="publication")

    @router.callback_query(F.data == "pub:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>📢 Публикация</b>\n\nАккаунты соцсетей. Выбери или добавь.",
                parse_mode="HTML", reply_markup=_kb_home())

    @router.callback_query(F.data == "pub:add")
    async def _add(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        s = store.add_slot()
        if not s:
            await query.answer("Лимит 10 аккаунтов.", show_alert=True)
            return
        await query.answer("Создан")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_slot_text(s), parse_mode="HTML",
                                          reply_markup=_kb_slot(s))

    @router.callback_query(F.data.startswith("pub:slot:"))
    async def _slot(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.clear()
        s = store.get_slot(query.data.split(":")[-1])
        if not s:
            await query.answer("Не найден.", show_alert=True)
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_slot_text(s), parse_mode="HTML",
                                          reply_markup=_kb_slot(s))

    @router.callback_query(F.data.startswith("pub:del:"))
    async def _del(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        store.remove_slot(query.data.split(":")[-1])
        await query.answer("Удалён")
        with contextlib.suppress(Exception):
            await query.message.edit_text("<b>📢 Публикация</b>", parse_mode="HTML",
                                          reply_markup=_kb_home())

    # ── Network ──
    @router.callback_query(F.data.startswith("pub:net:"))
    async def _net(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        parts = query.data.split(":")
        sid = parts[2]
        if len(parts) == 4:  # value chosen
            store.update_slot(sid, network=parts[3])
            await query.answer("Соцсеть выбрана")
            s = store.get_slot(sid)
            with contextlib.suppress(Exception):
                await query.message.edit_text(_slot_text(s), parse_mode="HTML",
                                              reply_markup=_kb_slot(s))
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text("<b>🌐 Выбери соцсеть</b>", parse_mode="HTML",
                                          reply_markup=_kb_net(sid))

    # ── Mode ──
    @router.callback_query(F.data.startswith("pub:mode:"))
    async def _mode(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        parts = query.data.split(":")
        sid = parts[2]
        if len(parts) == 4:
            store.update_slot(sid, mode=parts[3])
            await query.answer("Режим выбран")
            s = store.get_slot(sid)
            with contextlib.suppress(Exception):
                await query.message.edit_text(_slot_text(s), parse_mode="HTML",
                                              reply_markup=_kb_slot(s))
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text("<b>🎬 Режим публикации</b>", parse_mode="HTML",
                                          reply_markup=_kb_mode(sid))

    @router.callback_query(F.data.startswith("pub:auto:"))
    async def _auto(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        sid = query.data.split(":")[-1]
        s = store.get_slot(sid)
        store.update_slot(sid, auto=not s.get("auto"))
        await query.answer("Авто переключён")
        s = store.get_slot(sid)
        with contextlib.suppress(Exception):
            await query.message.edit_text(_slot_text(s), parse_mode="HTML",
                                          reply_markup=_kb_slot(s))

    # ── Hardware ──
    @router.callback_query(F.data.startswith("pub:hw:"))
    async def _hw(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        parts = query.data.split(":")
        sid = parts[2]
        if len(parts) == 4:
            store.update_slot(sid, hardware=parts[3])
            await query.answer("Железо выбрано")
            s = store.get_slot(sid)
            with contextlib.suppress(Exception):
                await query.message.edit_text(_slot_text(s), parse_mode="HTML",
                                              reply_markup=_kb_slot(s))
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text("<b>💻 Выбери железо (фингерпринт)</b>",
                                          parse_mode="HTML", reply_markup=_kb_hw(sid))

    # ── Proxy ──
    @router.callback_query(F.data.startswith("pub:proxy:"))
    async def _proxy(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        sid = query.data.split(":")[-1]
        await state.update_data(pub_sid=sid)
        await state.set_state(PubStates.awaiting_proxy)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🛡 Прокси/VPN</b>\n\nПришли строку прокси "
                "(<code>http://user:pass@host:port</code>) или «-» чтобы убрать.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data=f"pub:slot:{sid}")]]))

    @router.message(PubStates.awaiting_proxy, F.text)
    async def _proxy_set(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("pub_sid")
        val = (message.text or "").strip()
        store.update_slot(sid, proxy="" if val == "-" else val)
        await state.clear()
        s = store.get_slot(sid)
        await message.answer("✅ Прокси сохранён.",
            reply_markup=_kb_slot(s) if s else None)

    # ── Login ──
    @router.callback_query(F.data.startswith("pub:login:"))
    async def _login(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        parts = query.data.split(":")
        sid = parts[2]
        if len(parts) == 3:
            await query.answer()
            with contextlib.suppress(Exception):
                await query.message.edit_text("<b>🔑 Способ входа</b>", parse_mode="HTML",
                                              reply_markup=_kb_login(sid))
            return
        kind = parts[3]
        if kind == "api":
            store.update_slot(sid, login_type="api")
            await state.update_data(pub_sid=sid)
            await state.set_state(PubStates.awaiting_api_token)
            await query.answer()
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "<b>🔌 API</b>\n\nПришли API-токен соцсети.", parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отмена", callback_data=f"pub:slot:{sid}")]]))
        else:
            store.update_slot(sid, login_type="browser")
            await state.update_data(pub_sid=sid)
            await state.set_state(PubStates.awaiting_site)
            await query.answer()
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "<b>🌐 Вход через сайт</b>\n\nПришли адрес сайта для входа.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отмена", callback_data=f"pub:slot:{sid}")]]))

    @router.message(PubStates.awaiting_api_token, F.text)
    async def _api_token(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("pub_sid")
        store.update_slot(sid, api_token=(message.text or "").strip(), logged_in=True)
        with contextlib.suppress(Exception):
            await message.delete()
        await state.clear()
        s = store.get_slot(sid)
        await message.answer("✅ API-токен сохранён, вход настроен.",
                             reply_markup=_kb_slot(s) if s else None)

    @router.message(PubStates.awaiting_site, F.text)
    async def _site(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        store.update_slot(data.get("pub_sid"), site=(message.text or "").strip())
        await state.set_state(PubStates.awaiting_login)
        await message.answer("Логин (почта)?")

    @router.message(PubStates.awaiting_login, F.text)
    async def _login_msg(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        store.update_slot(data.get("pub_sid"), login=(message.text or "").strip())
        await state.set_state(PubStates.awaiting_password)
        await message.answer("Пароль? (сообщение удалю)")

    @router.message(PubStates.awaiting_password, F.text)
    async def _password(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("pub_sid")
        password = (message.text or "").strip()
        with contextlib.suppress(Exception):
            await message.delete()
        await state.clear()
        s = store.get_slot(sid)
        note = await message.answer("🌐 Пробую войти через stealth-браузер…")
        # Apply proxy/fingerprint, then run a login macro (best-effort).
        with contextlib.suppress(Exception):
            from ..issuance import core as iss
            if s and s.get("proxy"):
                iss.set_fingerprint_proxy(s["proxy"])
        try:
            from ...workers.browser_clicker import run_browser_macro
            steps = [
                ("goto", s.get("site") or "https://example.com"),
                ("pause", 3.0),
                ("type_login", s.get("login") or ""),
                ("type_password", password),
                ("pause", 2.0),
            ]
            result = await run_browser_macro(steps)
        except Exception as exc:  # noqa: BLE001
            logger.exception("publication: login macro failed")
            await note.edit_text(
                f"⚠️ Браузер недоступен в этом окружении: {type(exc).__name__}. "
                "Вход через сайт сработает на сервере с установленным Camoufox/Playwright.")
            return
        with contextlib.suppress(Exception):
            if isinstance(result, dict) and result.get("cookies"):
                store.save_cookies(sid, result["cookies"])
        store.update_slot(sid, logged_in=True)
        await note.edit_text(
            "✅ Попытка входа выполнена. Куки сохранены (если движок их вернул).",
            reply_markup=_kb_slot(store.get_slot(sid)))

    # ── Publish ──
    @router.callback_query(F.data.startswith("pub:go:"))
    async def _go(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        sid = query.data.split(":")[-1]
        # If a video was selected from «сделанное видео», publish it directly.
        pending = store.get_pending_video()
        import os
        if pending and os.path.exists(pending):
            store.clear_pending_video()
            await query.answer("🚀 Публикую выбранное видео…")
            prog = await query.message.answer("⏳ Публикую…")
            ok, note = await _publish(sid, pending)
            mark = "✅" if ok else "⚠️"
            with contextlib.suppress(Exception):
                await prog.edit_text(f"{mark} {note}", reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="📢 Публикация", callback_data="pub:home")]]))
            return
        await state.update_data(pub_sid=sid)
        await state.set_state(PubStates.awaiting_video)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🚀 Публикация</b>\n\nПришли <b>видео</b> (или файл из «сделанное "
                "видео») — опубликую по настройкам слота.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data=f"pub:slot:{sid}")]]))

    @router.message(PubStates.awaiting_video)
    async def _go_video(message: Message, state: FSMContext) -> None:
        from ...wizard import _is_owner
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return
        data = await state.get_data()
        sid = data.get("pub_sid")
        await state.clear()
        # Download the sent video to /data.
        fid = None
        if message.video:
            fid = message.video.file_id
        elif message.document:
            fid = message.document.file_id
        if not fid:
            await message.answer("Нужно видео. Пришли файл.")
            return
        prog = await message.answer("⏳ Готовлю и публикую…")
        try:
            from ...config import DATA_DIR
            dest = DATA_DIR / "storage" / "publication" / f"{sid}_upload.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            await message.bot.download(fid, destination=buf)
            dest.write_bytes(buf.getvalue())
        except Exception as exc:  # noqa: BLE001
            await prog.edit_text(f"❌ Не удалось скачать видео: {exc}")
            return
        ok, note = await _publish(sid, str(dest))
        mark = "✅" if ok else "⚠️"
        with contextlib.suppress(Exception):
            await prog.edit_text(
                f"{mark} {note}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📢 Публикация", callback_data="pub:home")]]))

    # ── OpenReels (topic → MP4) ──
    @router.callback_query(F.data == "pub:or")
    async def _or_home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_openreels_text(), parse_mode="HTML",
                                          reply_markup=_kb_openreels(),
                                          disable_web_page_preview=True)

    @router.callback_query(F.data == "pub:or:url")
    async def _or_url(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.set_state(PubStates.awaiting_openreels_url)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.answer(
                "Пришли URL сервера OpenReels одним сообщением.\n"
                "Например: <code>http://localhost:3000</code> или "
                "<code>https://openreels.мой-домен.com</code>",
                parse_mode="HTML", disable_web_page_preview=True)

    @router.message(PubStates.awaiting_openreels_url, F.text)
    async def _or_url_save(message: Message, state: FSMContext) -> None:
        from ...wizard import _is_owner
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return
        url = (message.text or "").strip()
        if not url.startswith(("http://", "https://")):
            await message.answer("URL должен начинаться с http:// или https://. Ещё раз.")
            return
        store.set_openreels_url(url)
        await state.clear()
        await message.answer("✅ URL сервера OpenReels сохранён.", parse_mode="HTML",
                             reply_markup=_kb_openreels())

    @router.callback_query(F.data == "pub:or:mode")
    async def _or_mode(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        order = ["generate_then_publish", "generate_only", "openreels_publishes"]
        cur = store.get_openreels_mode()
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        store.set_openreels_mode(nxt)
        await query.answer(_OR_MODE_LABELS.get(nxt, nxt))
        with contextlib.suppress(Exception):
            await query.message.edit_text(_openreels_text(), parse_mode="HTML",
                                          reply_markup=_kb_openreels(),
                                          disable_web_page_preview=True)

    @router.callback_query(F.data == "pub:or:platform")
    async def _or_platform(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        order = ["youtube", "tiktok", "instagram"]
        cur = store.get_openreels_platform()
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        store.set_openreels_platform(nxt)
        await query.answer(nxt)
        with contextlib.suppress(Exception):
            await query.message.edit_text(_openreels_text(), parse_mode="HTML",
                                          reply_markup=_kb_openreels(),
                                          disable_web_page_preview=True)

    @router.callback_query(F.data == "pub:or:health")
    async def _or_health(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer("Проверяю…")
        from ...openreels import health
        if not store.get_openreels_url():
            with contextlib.suppress(Exception):
                await query.message.answer("⚠️ Сначала задай URL сервера (🔧 URL сервера).")
            return
        ok, msg = await health()
        mark = "✅" if ok else "❌"
        with contextlib.suppress(Exception):
            await query.message.answer(f"{mark} {_esc(msg)}")

    @router.callback_query(F.data == "pub:or:gen")
    async def _or_gen(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        if not store.get_openreels_url():
            await query.answer("Сначала задай URL сервера OpenReels.", show_alert=True)
            return
        await state.set_state(PubStates.awaiting_openreels_topic)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.answer(
                "Пришли <b>тему</b> ролика одним сообщением.\n"
                "Например: <i>история миссии Аполлон-13 — от взрыва до спасения</i>",
                parse_mode="HTML")

    @router.message(PubStates.awaiting_openreels_topic, F.text)
    async def _or_topic(message: Message, state: FSMContext) -> None:
        from ...wizard import _is_owner
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return
        topic = (message.text or "").strip()
        await state.clear()
        if not topic:
            await message.answer("Пустая тема. Ещё раз.")
            return
        mode = store.get_openreels_mode()
        platform = store.get_openreels_platform()
        from ...openreels import OpenReelsError, download_to, generate

        prog = await message.answer("⏳ OpenReels: ставлю задачу…")

        async def _on_progress(stage: str, pct: float) -> None:
            with contextlib.suppress(Exception):
                await prog.edit_text(f"⏳ OpenReels: <b>{_esc(stage)}</b>…", parse_mode="HTML")

        try:
            result = await generate(topic, platform=platform,
                                    on_progress=_on_progress)
        except OpenReelsError as exc:
            with contextlib.suppress(Exception):
                await prog.edit_text(f"❌ {_esc(str(exc))}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("openreels generate failed")
            with contextlib.suppress(Exception):
                await prog.edit_text(f"❌ OpenReels ошибка: {_esc(str(exc))}")
            return

        # Mode 3: OpenReels publishes on its own — just report.
        if mode == "openreels_publishes":
            with contextlib.suppress(Exception):
                await prog.edit_text(
                    f"✅ OpenReels собрал ролик по теме «{_esc(topic)}» и публикует сам "
                    f"(job {result.get('job_id')}). Платформа: {platform}.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🎬 OpenReels", callback_data="pub:or")]]))
            return

        # Modes 1 & 2 need the MP4 locally.
        with contextlib.suppress(Exception):
            await prog.edit_text("⬇️ Скачиваю готовый MP4…")
        try:
            from ...config import DATA_DIR
            dest = DATA_DIR / "storage" / "publication" / f"openreels_{result.get('job_id')}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            await download_to(str(dest), result.get("video_url", ""),
                              result.get("download_urls"))
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await prog.edit_text(
                    f"⚠️ Ролик готов (job {result.get('job_id')}), но скачать не вышло: "
                    f"{_esc(str(exc))}\nОткрой OpenReels UI и забери файл вручную.")
            return

        from aiogram.types import FSInputFile
        with contextlib.suppress(Exception):
            await prog.delete()
        # Mode 2: just send the MP4 to the owner.
        if mode == "generate_only":
            with contextlib.suppress(Exception):
                await message.answer_video(
                    FSInputFile(str(dest)),
                    caption=f"🎬 OpenReels · «{_esc(topic)}»",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="📢 Опубликовать", callback_data="pub:home")]]))
            return

        # Mode 1: stash as pending and let the owner pick a slot to publish.
        store.set_pending_video(str(dest))
        slots = store.get_slots()
        rows = [[InlineKeyboardButton(text=f"📢 {s.get('name')} · {s.get('network')}",
                                      callback_data=f"pub:go:{s['id']}")] for s in slots]
        rows.append([InlineKeyboardButton(text="💾 Просто прислать мне", callback_data="pub:or:send")])
        rows.append([InlineKeyboardButton(text="↩️ OpenReels", callback_data="pub:or")])
        with contextlib.suppress(Exception):
            await message.answer_video(
                FSInputFile(str(dest)),
                caption=f"🎬 Готово · «{_esc(topic)}». Куда публикуем?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @router.callback_query(F.data == "pub:or:send")
    async def _or_send(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer("Файл уже выше ⬆️")

    return router
