"""Telegram surface of the gateway — the ``gateway:*`` screens (ТЗ-1 §4).

One aiogram router handles every ``gateway:*`` callback by funnelling it
through :func:`bot.gateway.dispatch.dispatch_action` — the exact same
path web (and later Discord/VK) actions take. That means the TG screens
double as a living integration test of the whole pipeline.

Фикс 1.1 (ТЗ §4.4, R6): токены Discord/VK вводятся прямо в Telegram
через FSM — кнопка «Подключить (ввести Bot Token)» на live-экране
переводит владельца в состояние ожидания токена; присланное сообщение
с токеном НЕМЕДЛЕННО удаляется (до валидации), токен никогда не
логируется и не цитируется в ответах.

Registered «мягко» from :func:`bot.addons.build_addon_routers` (try/
except), consistent with every other addon (§2 п.4).
"""

from __future__ import annotations

import contextlib
import html
import logging
from dataclasses import replace

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .dispatch import GatewayContext, dispatch_action
from .menu import MenuButton, MenuScreen, build_screen, screen_to_tg

logger = logging.getLogger("gateway.wizard_ui")


# --------------------------------------------------------------------------
# FSM: ввод токена платформы сообщением (фикс 1.1)
# --------------------------------------------------------------------------

class GatewayTokenInput(StatesGroup):
    """Owner is about to paste a platform bot token as a message."""

    waiting_token = State()


#: callback action → платформа, для которой запрашивается токен.
ENTER_TOKEN_ACTIONS: dict[str, str] = {
    "gateway:discord:enter_token": "discord",
    "gateway:vk:enter_token": "vk",
}

#: callback action «Отмена» → платформа (возврат на live-экран).
CANCEL_TOKEN_ACTIONS: dict[str, str] = {
    "gateway:discord:cancel_token": "discord",
    "gateway:vk:cancel_token": "vk",
}

CONNECT_BUTTON_TEXT = "🔑 Подключить (ввести Bot Token)"

TOKEN_PROMPT = (
    "Пришли Bot Token сообщением. "
    "Сообщение будет немедленно удалено."
)

_STATE_PLATFORM_KEY = "gateway_token_platform"


def _can_admin(user_id: int | None) -> bool:
    """Owner/co-owner check — fails closed (та же логика, что dispatch)."""
    if user_id is None:
        return False
    try:
        from .. import access
        return bool(access.can_admin(int(user_id)))
    except Exception:  # noqa: BLE001
        logger.exception("wizard_ui admin check failed closed")
        return False


def _cancel_kb(platform: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data=f"gateway:{platform}:cancel_token",
                )
            ]
        ]
    )


async def _validate_vk_token(token: str) -> tuple[bool, str | None, str | None]:
    """Проверить VK community token ДО сохранения (симметрия фикса 1.2).

    Использует существующую валидацию адаптера: ``groups.getById``
    через :func:`vk_gw._resolve_group` (файл vk_gw не модифицируется —
    он принадлежит части 2; если интегратор добавит туда
    ``validate_token``, используем его).
    Возвращает ``(ok, group_name, error)``. Токен не логируется.
    """
    token = (token or "").strip()
    if not token:
        return False, None, "Пустой токен."
    from .adapters import vk_gw
    if hasattr(vk_gw, "validate_token"):
        return await vk_gw.validate_token(token)  # part-2 wrapper, если есть
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=10.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            _group_id, group_name = await vk_gw._resolve_group(session, token)  # noqa: SLF001
        return True, group_name, None
    except Exception as exc:  # noqa: BLE001 — сеть/невалидный токен
        logger.warning("vk token validation failed: %s", type(exc).__name__)
        return False, None, (
            "Токен не принят VK. Проверь, что это токен сообщества "
            "с правом «messages», и попробуй ещё раз."
        )


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def _augment_screen(screen: MenuScreen) -> MenuScreen:
    """Добавить кнопку «Подключить» на live-экраны без токена (фикс 1.1).

    Кнопка вставляется на стороне TG-рендера, чтобы не трогать файлы
    адаптеров (принадлежат другим частям handoff'а).
    """
    try:
        if screen.screen_id == "gateway:discord:live":
            from .adapters import discord_gw
            if not discord_gw.get_token():
                connect = [MenuButton(text=CONNECT_BUTTON_TEXT,
                                      action="gateway:discord:enter_token")]
                return replace(screen, rows=[connect, *screen.rows])
        elif screen.screen_id == "gateway:vk:live":
            from .adapters import vk_gw
            if not vk_gw.get_token():
                connect = [MenuButton(text=CONNECT_BUTTON_TEXT,
                                      action="gateway:vk:enter_token")]
                return replace(screen, rows=[connect, *screen.rows])
    except Exception:  # noqa: BLE001 — косметика не должна ломать рендер
        logger.exception("live screen augmentation failed")
    return screen


def _screen_text(screen: MenuScreen) -> str:
    """Title + body as safe HTML for a TG message."""
    text = f"<b>{html.escape(screen.title)}</b>"
    if screen.body:
        text += f"\n\n{html.escape(screen.body)}"
    return text


async def _render_screen(query: CallbackQuery, screen: MenuScreen) -> None:
    screen = _augment_screen(screen)
    kb = screen_to_tg(screen, query.from_user.id if query.from_user else None)
    text = _screen_text(screen)
    if query.message is None:
        return
    # Prefer edit-in-place (like the rest of the wizard); fall back to a
    # fresh message when the original can't be edited (e.g. too old).
    try:
        await query.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await query.message.answer(
                text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )


async def _send_live_screen(message: Message, platform: str,
                            user_id: int | None) -> None:
    """Прислать live-экран платформы новым сообщением (после ввода токена)."""
    screen = build_screen(f"gateway:{platform}:live", user_id)
    if screen is None:
        with contextlib.suppress(Exception):
            await message.answer("⚠️ Экран сейчас недоступен. Открой меню заново.")
        return
    screen = _augment_screen(screen)
    kb = screen_to_tg(screen, user_id)
    with contextlib.suppress(Exception):
        await message.answer(
            _screen_text(screen),
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# --------------------------------------------------------------------------
# handlers (module-level — тестируются без запуска Dispatcher)
# --------------------------------------------------------------------------

async def start_token_input(query: CallbackQuery, state: FSMContext,
                            platform: str) -> None:
    """Кнопка «Подключить»: перевести владельца в режим ожидания токена."""
    uid = query.from_user.id if query.from_user else None
    if not _can_admin(uid):
        await query.answer("⛔ Только владелец может подключать платформы.",
                           show_alert=True)
        return
    await state.set_state(GatewayTokenInput.waiting_token)
    await state.update_data(**{_STATE_PLATFORM_KEY: platform})
    await query.answer()
    if query.message is None:
        return
    kb = _cancel_kb(platform)
    try:
        await query.message.edit_text(TOKEN_PROMPT, reply_markup=kb)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await query.message.answer(TOKEN_PROMPT, reply_markup=kb)


async def on_gateway_callback(query: CallbackQuery, state: FSMContext) -> None:
    """Единый обработчик всех ``gateway:*`` callback'ов."""
    from .screens import is_gateway_enabled

    uid = query.from_user.id if query.from_user else None
    action = query.data or ""

    if not is_gateway_enabled():
        await query.answer("🌐 Платформы выключены.", show_alert=True)
        return

    # Lazy-регистрация Bot для уведомлений владельцу (фикс 1.3) — тот же
    # паттерн, что кэширование bot_username ниже.
    with contextlib.suppress(Exception):
        from . import identity
        identity.set_bot(query.bot)

    # Фикс 1.1: ввод токена через FSM — обрабатываем ДО dispatch_action.
    platform = ENTER_TOKEN_ACTIONS.get(action)
    if platform is not None:
        await start_token_input(query, state, platform)
        return
    platform = CANCEL_TOKEN_ACTIONS.get(action)
    if platform is not None:
        with contextlib.suppress(Exception):
            await state.clear()
        # дальше обычный dispatch отрисует live-экран платформы
        action = f"gateway:{platform}:live"

    # Remember the bot username for tg_only deep-links on other
    # platforms (best-effort, cached in state.json).
    with contextlib.suppress(Exception):
        from ..addons import state as addon_state
        if not addon_state.get("gateway", "bot_username", ""):
            me = await query.bot.get_me()
            if me.username:
                addon_state.set_("gateway", "bot_username", me.username)

    ctx = GatewayContext(
        platform="telegram",
        external_id=str(uid) if uid is not None else "",
        user_id=uid,
    )
    try:
        result = await dispatch_action(action, ctx)
    except Exception:  # noqa: BLE001
        logger.exception("gateway dispatch failed: %s", action)
        await query.answer("⚠️ Ошибка. Попробуй ещё раз.", show_alert=True)
        return

    if result.error == "forbidden":
        await query.answer(result.text or "⛔ Недоступно.", show_alert=True)
        return

    if result.screen is not None:
        await query.answer(result.text or "")
        await _render_screen(query, result.screen)
        return

    if result.text:
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.answer(result.text)
        return

    await query.answer("⚠️ Действие не выполнено.", show_alert=True)


async def on_token_message(message: Message, state: FSMContext) -> None:
    """Сообщение с токеном (FSM-состояние ``waiting_token``).

    Порядок жёсткий (ТЗ §4.4): sensitive-сообщение удаляется ПЕРВЫМ
    делом — до любой валидации, даже если токен кривой. Токен никогда
    не логируется и не попадает в тексты ответов.
    """
    # 1. Немедленно удалить сообщение с токеном (best-effort).
    with contextlib.suppress(Exception):
        await message.delete()

    data = await state.get_data()
    platform = str(data.get(_STATE_PLATFORM_KEY) or "")
    uid = message.from_user.id if message.from_user else None

    with contextlib.suppress(Exception):
        from . import identity
        identity.set_bot(message.bot)

    # 2. Защита от гонок: состояние могло достаться не-владельцу.
    if not _can_admin(uid) or platform not in ("discord", "vk"):
        with contextlib.suppress(Exception):
            await state.clear()
        return

    token = (message.text or "").strip()

    # 3. Валидация ДО сохранения (фикс 1.2 — discord_gw.validate_token).
    if platform == "discord":
        from .adapters import discord_gw
        ok, bot_tag, err = await discord_gw.validate_token(token)
        if ok:
            discord_gw.set_token(token)
            with contextlib.suppress(Exception):
                discord_gw._set_bot_tag(bot_tag)  # noqa: SLF001
    else:  # vk
        ok, _group_name, err = await _validate_vk_token(token)
        if ok:
            from .adapters import vk_gw
            vk_gw.set_token(token)

    if not ok:
        # Остаёмся в состоянии — владелец может прислать токен заново.
        with contextlib.suppress(Exception):
            await message.answer(
                f"❌ {err or 'Токен не принят.'}\n\n"
                "Пришли токен ещё раз или нажми «Отмена».",
                reply_markup=_cancel_kb(platform),
            )
        return

    await state.clear()
    await _send_live_screen(message, platform, uid)


def build_gateway_router() -> Router:
    """Router for every ``gateway:*`` callback + FSM-ввод токена."""
    # Importing screens registers wave-1 builders + gateway actions.
    from . import screens  # noqa: F401

    router = Router(name="gateway")
    router.callback_query(F.data.startswith("gateway:"))(on_gateway_callback)
    router.message(GatewayTokenInput.waiting_token)(on_token_message)
    return router


#: Module-level instance imported by :mod:`bot.main`.
gateway_router = build_gateway_router()
