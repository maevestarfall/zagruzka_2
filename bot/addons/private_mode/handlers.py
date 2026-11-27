"""Private-mode control panel UI addon.

Adds Settings → ⛔ Доступ → «🔒 Приват режим»: the owner picks exactly which
features guests may **use** («Функции») and **see** («Что они видят») with
dynamic green-✅ toggle buttons, configures payment («Оплата» — card / Telegram
Stars / crypto, each with its own on/off, plus pay-on-join and the $-per-slot
price) and the guest-facing texts («Оповещения» — welcome + limit-reached).

All persistence goes through the thin ``storage.pm_*`` wrappers (logic in
:mod:`bot.private_mode`). Only the real owner may change anything here.
"""

from __future__ import annotations

import contextlib
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from ...access import is_owner
from ...private_mode import (
    FEATURES,
    PAYMENT_METHODS,
)
from ...storage import storage

logger = logging.getLogger(__name__)


async def _crypto_create_invoice(token: str, usd: float, user_id=None):
    """Create a fiat-USD Crypto Pay invoice via aiocryptopay (CryptoBot).

    Returns the invoice object (has ``.invoice_id`` and ``.bot_invoice_url``).
    ``user_id`` is stored in the invoice payload so the webhook can credit
    the right guest on payment. Kept tiny and self-contained so the rest of
    the addon never imports the optional dependency at module load time.
    """
    from aiocryptopay import AioCryptoPay, Networks
    crypto = AioCryptoPay(token=token, network=Networks.MAIN_NET)
    try:
        kwargs = dict(amount=round(float(usd), 2), fiat="USD", currency_type="fiat")
        if user_id is not None:
            kwargs["payload"] = str(user_id)
        return await crypto.create_invoice(**kwargs)
    finally:
        await crypto.close()


async def _crypto_invoice_paid(token: str, invoice_id) -> bool:
    """Return True when the given Crypto Pay invoice is fully paid."""
    from aiocryptopay import AioCryptoPay, Networks
    crypto = AioCryptoPay(token=token, network=Networks.MAIN_NET)
    try:
        res = await crypto.get_invoices(invoice_ids=invoice_id)
        inv = res[0] if isinstance(res, list) else res
        return bool(inv) and getattr(inv, "status", "") == "paid"
    finally:
        await crypto.close()


class PrivateModeStates(StatesGroup):
    awaiting_card = State()
    awaiting_card_token = State()
    awaiting_crypto = State()
    awaiting_crypto_token = State()
    awaiting_crypto_pub_url = State()
    awaiting_price = State()
    awaiting_tokens = State()
    awaiting_welcome = State()
    awaiting_limit = State()


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _deny(query: CallbackQuery) -> bool:
    return not is_owner(query.from_user.id if query.from_user else 0)


def _check(on: bool) -> str:
    """Green check when ON, hollow circle when OFF — the dynamic ✅ marker."""
    return "✅" if on else "⚪"


# --------------------------------------------------------------------------
# keyboards
# --------------------------------------------------------------------------
def _kb_main() -> InlineKeyboardMarkup:
    on = storage.pm_is_enabled()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{_check(on)} Приват режим: {'ВКЛ' if on else 'ВЫКЛ'}",
            callback_data="pm:toggle")],
        [InlineKeyboardButton(text="🧩 Функции (что можно)", callback_data="pm:funcs")],
        [InlineKeyboardButton(text="👁 Что они видят", callback_data="pm:vis")],
        [InlineKeyboardButton(text="💰 Оплата", callback_data="pm:pay")],
        [InlineKeyboardButton(text="📣 Оповещения", callback_data="pm:notif")],
        [InlineKeyboardButton(text="↩️ К доступу", callback_data="access:menu")],
    ])


def _kb_flags(kind: str) -> InlineKeyboardMarkup:
    """One toggle button per feature, with a live green-✅ marker."""
    snap = storage.pm_flags_snapshot(kind)
    rows: list[list[InlineKeyboardButton]] = []
    for key, label in FEATURES:
        on = snap.get(key, False)
        rows.append([InlineKeyboardButton(
            text=f"{_check(on)} {label}",
            callback_data=f"pm:t:{kind}:{key}",
        )])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="pm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_pay() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for method, label in PAYMENT_METHODS:
        on = storage.pm_is_method_enabled(method)
        rows.append([InlineKeyboardButton(
            text=f"{_check(on)} {label}",
            callback_data=f"pm:mtog:{method}",
        )])
        if method in ("card", "crypto"):
            rows.append([InlineKeyboardButton(
                text=f"   ✏️ Реквизиты ({'карта' if method == 'card' else 'крипта'})",
                callback_data=f"pm:medit:{method}",
            )])
        if method == "card":
            has_token = bool(storage.pm_get_card_token())
            rows.append([InlineKeyboardButton(
                text=("   🤖 Авто-оплата картой: ВКЛ (токен задан)" if has_token
                      else "   🔌 Подключить авто-оплату (вставить токен)"),
                callback_data="pm:ctoken",
            )])
        if method == "crypto":
            has_ctoken = bool(storage.pm_get_crypto_token())
            rows.append([InlineKeyboardButton(
                text=("   🤖 Авто-крипта: ВКЛ (CryptoBot токен задан)" if has_ctoken
                      else "   🔌 Подключить CryptoBot (вставить токен)"),
                callback_data="pm:cryptotoken",
            )])
            wh_on = storage.pm_get_crypto_webhook_enabled()
            rows.append([InlineKeyboardButton(
                text=f"   🌐 Вебхук CryptoBot: {'ВКЛ 🟢' if wh_on else 'ВЫКЛ'}",
                callback_data="pm:cwh",
            )])
    join = storage.pm_get_require_on_join()
    rows.append([InlineKeyboardButton(
        text=f"{_check(join)} Требовать оплату при входе",
        callback_data="pm:join")])
    rows.append([InlineKeyboardButton(
        text=f"💵 Цена: ${storage.pm_get_usd_per_slot():g} = 1 слот",
        callback_data="pm:price")])
    rows.append([InlineKeyboardButton(
        text=f"🎟 Токенов на 1 слот: {storage.pm_get_tokens_per_slot():,}",
        callback_data="pm:tokens")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="pm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_notif() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Текст при входе (с оплатой)", callback_data="pm:nedit:welcome")],
        [InlineKeyboardButton(text="✏️ Текст когда лимит кончился", callback_data="pm:nedit:limit")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="pm:menu")],
    ])


# --------------------------------------------------------------------------
# bodies
# --------------------------------------------------------------------------
def _body_main() -> str:
    on = storage.pm_is_enabled()
    return (
        "<b>🔒 Приват режим</b>\n\n"
        f"Статус: <b>{'ВКЛ' if on else 'ВЫКЛ'}</b>\n\n"
        "Когда ВКЛ — владелец видит и может всё, а гостям доступно только то, "
        "что ты отметил зелёной ✅ ниже. По умолчанию всё ВЫКЛ.\n\n"
        "• <b>Функции</b> — что гость может <i>использовать</i>.\n"
        "• <b>Что они видят</b> — что гость <i>видит</i> в меню.\n"
        "• <b>Оплата</b> — карта / звёзды / крипта; $X = 1 слот.\n"
        "• <b>Оповещения</b> — текст при входе и когда лимит кончился."
    )


def _body_flags(kind: str) -> str:
    title = "🧩 Функции — что можно" if kind == "features" else "👁 Что они видят"
    snap = storage.pm_flags_snapshot(kind)
    n_on = sum(1 for v in snap.values() if v)
    return (
        f"<b>{title}</b>\n\n"
        f"Включено: <b>{n_on}</b> / {len(snap)}.\n"
        "Тапни пункт — ✅ значит включено для гостей, ⚪ — выключено.\n"
        "По умолчанию всё выключено."
    )


def _body_pay() -> str:
    pay = storage.pm_get_payment()
    lines = ["<b>💰 Оплата</b>\n"]
    card = pay["card"]
    crypto = pay["crypto"]
    has_token = bool(storage.pm_get_card_token())
    cur = storage.pm_get_card_currency()
    lines.append(
        f"💳 Карта: {'ВКЛ' if card['enabled'] else 'ВЫКЛ'} · "
        f"реквизиты: <code>{_esc(card.get('details')) or '(пусто)'}</code>"
    )
    lines.append(
        f"   {'🤖 авто-оплата ВКЛ' if has_token else '✋ ручной режим (токен не задан)'}"
        + (f' · валюта {cur}' if has_token else '')
    )
    lines.append(f"⭐ Звёзды: {'ВКЛ' if pay['stars']['enabled'] else 'ВЫКЛ'}")
    has_ctoken = bool(storage.pm_get_crypto_token())
    lines.append(
        f"🪙 Крипта: {'ВКЛ' if crypto['enabled'] else 'ВЫКЛ'} · "
        f"реквизиты: <code>{_esc(crypto.get('details')) or '(пусто)'}</code>"
    )
    lines.append(
        f"   {'🤖 авто-крипта (CryptoBot) ВКЛ' if has_ctoken else '✋ ручной режим (адрес кошелька)'}"
    )
    lines.append("")
    lines.append(
        f"💵 <b>${pay['usd_per_slot']:g} = 1 API-слот ≈ "
        f"{storage.pm_get_tokens_per_slot():,} токенов.</b> На сколько оплатил — "
        "на столько и пользуется: баланс списывается по мере расхода токенов."
    )
    lines.append(
        f"При входе оплата {'требуется' if pay['require_on_join'] else 'не требуется'}."
    )
    lines.append("")
    lines.append(
        "<i>Выключенные способы гостю даже не предлагаются. Для карты впиши, "
        "что и куда переводить (номер РФ-карты / Mastercard); для крипты — "
        "адрес кошелька и сеть.</i>"
    )
    return "\n".join(lines)


def _body_notif() -> str:
    welcome = storage.pm_get_notification("welcome")
    limit = storage.pm_get_notification("limit_reached")
    return (
        "<b>📣 Оповещения</b>\n\n"
        "<b>При входе</b> (показывается новому гостю, под текстом — кнопки оплаты "
        "теми способами, что включены):\n"
        f"<code>{_esc(welcome) or '(не задан)'}</code>\n\n"
        "<b>Когда лимит кончился</b> (показывается, когда баланс гостя дошёл до 0, "
        "снова с кнопками оплаты):\n"
        f"<code>{_esc(limit) or '(не задан)'}</code>"
    )


async def _edit(query: CallbackQuery, body: str, kb: InlineKeyboardMarkup) -> None:
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(body, reply_markup=kb, parse_mode="HTML")


# --------------------------------------------------------------------------
# guest-facing payment offer (shown on join / when the limit runs out)
# --------------------------------------------------------------------------
_METHOD_BTN = {
    "card": ("💳 Оплатить картой", "pm:buy:card"),
    "stars": ("⭐ Оплатить звёздами", "pm:buy:stars"),
    "crypto": ("🪙 Оплатить криптой", "pm:buy:crypto"),
}


def guest_offer(kind: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the guest-facing (text, keyboard) for ``kind`` ∈ {welcome, limit_reached}.

    The keyboard offers ONLY the payment methods the owner switched ON — a
    disabled method is never shown. Returns ``(text, None)`` when no method is
    enabled (nothing to offer).
    """
    text = storage.pm_get_notification(kind) or (
        "Этот бот работает в приватном режиме. Оплати доступ, чтобы пользоваться."
        if kind == "welcome"
        else "Лимит доступа закончился. Продли оплату, чтобы продолжить."
    )
    methods = storage.pm_enabled_methods()
    if not methods:
        return text, None
    price = storage.pm_get_usd_per_slot()
    rows = [[InlineKeyboardButton(text=_METHOD_BTN[m][0], callback_data=_METHOD_BTN[m][1])]
            for m in methods if m in _METHOD_BTN]
    text += f"\n\n💵 Цена: ${price:g} = 1 слот доступа."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_private_mode_router() -> Router:
    router = Router(name="private_mode")

    @router.callback_query(F.data == "pm:menu")
    async def cb_menu(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await _edit(query, _body_main(), _kb_main())
        await query.answer()

    @router.callback_query(F.data == "pm:toggle")
    async def cb_toggle(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        on = storage.pm_toggle_enabled()
        await _edit(query, _body_main(), _kb_main())
        await query.answer(f"Приват режим: {'ВКЛ' if on else 'ВЫКЛ'}")

    @router.callback_query(F.data == "pm:funcs")
    async def cb_funcs(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await _edit(query, _body_flags("features"), _kb_flags("features"))
        await query.answer()

    @router.callback_query(F.data == "pm:vis")
    async def cb_vis(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await _edit(query, _body_flags("visibility"), _kb_flags("visibility"))
        await query.answer()

    @router.callback_query(F.data.startswith("pm:t:"))
    async def cb_toggle_flag(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        _, _, kind, key = query.data.split(":", 3)
        try:
            now_on = storage.pm_toggle_flag(kind, key)
        except ValueError:
            await query.answer("Неизвестный пункт.", show_alert=True)
            return
        await _edit(query, _body_flags(kind), _kb_flags(kind))
        await query.answer("✅ включено" if now_on else "⚪ выключено")

    # ---- payment ----
    @router.callback_query(F.data == "pm:pay")
    async def cb_pay(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await _edit(query, _body_pay(), _kb_pay())
        await query.answer()

    @router.callback_query(F.data.startswith("pm:mtog:"))
    async def cb_method_toggle(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        method = query.data.split(":")[2]
        try:
            on = storage.pm_toggle_method(method)
        except ValueError:
            await query.answer("Неизвестный способ.", show_alert=True)
            return
        await _edit(query, _body_pay(), _kb_pay())
        await query.answer(f"{method}: {'ВКЛ' if on else 'ВЫКЛ'}")

    @router.callback_query(F.data.startswith("pm:medit:"))
    async def cb_method_edit(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        method = query.data.split(":")[2]
        if method == "card":
            await state.set_state(PrivateModeStates.awaiting_card)
            prompt = (
                "💳 Пришли реквизиты карты для оплаты одним сообщением.\n\n"
                "Что обычно вписывают: номер РФ-карты (МИР) или Mastercard, "
                "имя получателя, и — если платёж через Telegram-инвойс — "
                "<b>provider token</b> от @BotFather (Payments). Можно просто "
                "номер карты + инструкцию «переведи и пришли скрин»."
            )
        elif method == "crypto":
            await state.set_state(PrivateModeStates.awaiting_crypto)
            prompt = (
                "🪙 Пришли крипто-реквизиты одним сообщением: адрес кошелька, "
                "сеть (например USDT TRC-20) и, если нужно, memo/tag."
            )
        else:
            await query.answer("У этого способа нет реквизитов.", show_alert=True)
            return
        if query.message is not None:
            await query.message.answer(prompt, parse_mode="HTML")
        await query.answer()

    @router.callback_query(F.data == "pm:ctoken")
    async def cb_card_token(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(PrivateModeStates.awaiting_card_token)
        cur = storage.pm_get_card_currency()
        has = bool(storage.pm_get_card_token())
        if query.message is not None:
            await query.message.answer(
                "🔌 <b>Подключение авто-оплаты картой</b>\n\n"
                "Пришли <b>provider token</b> из @BotFather → твой бот → "
                "Payments → ЮKassa/Stripe. Выглядит как "
                "<code>381764678:TEST:abc123</code>.\n\n"
                "Можно вместе с валютой через пробел, например:\n"
                "<code>381764678:LIVE:xxxxx RUB</code>\n\n"
                f"Сейчас: {'токен задан, валюта ' + cur if has else 'токен НЕ задан (ручной режим)'}.\n"
                "Чтобы <b>отключить</b> авто-оплату и вернуть ручной режим — пришли <code>-</code>.",
                parse_mode="HTML",
            )
        await query.answer()

    @router.callback_query(F.data == "pm:cryptotoken")
    async def cb_crypto_token(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(PrivateModeStates.awaiting_crypto_token)
        has = bool(storage.pm_get_crypto_token())
        if query.message is not None:
            await query.message.answer(
                "🔌 <b>Подключение CryptoBot (авто-крипта)</b>\n\n"
                "Открой @CryptoBot → <b>Crypto Pay</b> → <b>My Apps</b> → "
                "<b>Create App</b> и пришли сюда <b>API Token</b>. "
                "Выглядит как <code>12345:AAbbCC...</code>.\n\n"
                f"Сейчас: {'токен задан — крипта автоматическая' if has else 'токен НЕ задан (ручной режим, просто адрес кошелька)'}.\n"
                "Чтобы <b>отключить</b> и вернуть ручной режим — пришли <code>-</code>.",
                parse_mode="HTML",
            )
        await query.answer()

    def _crypto_wh_screen() -> tuple[str, InlineKeyboardMarkup]:
        from .crypto_webhook import webhook_url
        wh_on = storage.pm_get_crypto_webhook_enabled()
        override = storage.pm_get_crypto_public_url()
        base = override
        if not base:
            try:
                from ...config import resolve_public_url
                base = resolve_public_url()
            except Exception:  # noqa: BLE001
                base = ""
        url = webhook_url(base) if base else "(публичный URL не определён)"
        has_token = bool(storage.pm_get_crypto_token())
        body = (
            "<b>🌐 Вебхук CryptoBot</b>\n\n"
            "Мгновенное начисление: как только гость оплатил, CryptoBot сам "
            "сообщает боту — кнопку «Я оплатил» жать не нужно.\n\n"
            f"Статус: <b>{'ВКЛ 🟢' if wh_on else 'ВЫКЛ'}</b>\n"
            f"CryptoBot токен: <b>{'задан' if has_token else 'НЕ задан'}</b>\n\n"
            "<b>Куда вставить URL</b> (в @CryptoBot → Crypto Pay → My Apps → "
            "твоё приложение → <b>Webhooks</b> → Enable, и вставь сюда адрес):\n"
            f"<code>{_esc(url)}</code>\n\n"
            "<i>Если URL выше пустой или неверный — нажми «Вставить публичный "
            "URL» и пришли адрес своего сервера (например https://mybot.onrender.com).</i>"
        )
        rows = [
            [InlineKeyboardButton(
                text=f"{'🟢 Вебхук ВКЛ' if wh_on else '⚪ Вебхук ВЫКЛ'} — переключить",
                callback_data="pm:cwh:toggle")],
            [InlineKeyboardButton(text="✏️ Вставить публичный URL", callback_data="pm:cwh:url")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="pm:pay")],
        ]
        return body, InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data == "pm:cwh")
    async def cb_crypto_webhook(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        body, kb = _crypto_wh_screen()
        await _edit(query, body, kb)
        await query.answer()

    @router.callback_query(F.data == "pm:cwh:toggle")
    async def cb_crypto_webhook_toggle(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        new = not storage.pm_get_crypto_webhook_enabled()
        if new and not storage.pm_get_crypto_token():
            await query.answer("Сначала задай CryptoBot токен.", show_alert=True)
            return
        storage.pm_set_crypto_webhook_enabled(new)
        body, kb = _crypto_wh_screen()
        await _edit(query, body, kb)
        await query.answer(
            ("Вебхук ВКЛ — не забудь перезапустить бота и вставить URL в CryptoBot."
             if new else "Вебхук ВЫКЛ."),
            show_alert=new,
        )

    @router.callback_query(F.data == "pm:cwh:url")
    async def cb_crypto_webhook_url(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(PrivateModeStates.awaiting_crypto_pub_url)
        if query.message is not None:
            await query.message.answer(
                "✏️ Пришли публичный HTTPS-адрес твоего сервера "
                "(например <code>https://mybot.onrender.com</code>). "
                "Чтобы очистить и снова автоопределять — пришли <code>-</code>.",
                parse_mode="HTML",
            )
        await query.answer()

    @router.callback_query(F.data == "pm:join")
    async def cb_join(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        on = storage.pm_toggle_require_on_join()
        await _edit(query, _body_pay(), _kb_pay())
        await query.answer(f"Оплата при входе: {'ВКЛ' if on else 'ВЫКЛ'}")

    @router.callback_query(F.data == "pm:price")
    async def cb_price(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(PrivateModeStates.awaiting_price)
        if query.message is not None:
            await query.message.answer(
                "💵 Пришли цену в долларах за <b>1 API-слот</b> доступа "
                "(число, например <code>10</code>).",
                parse_mode="HTML",
            )
        await query.answer()

    @router.callback_query(F.data == "pm:tokens")
    async def cb_tokens(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(PrivateModeStates.awaiting_tokens)
        if query.message is not None:
            await query.message.answer(
                "🎟 Сколько <b>токенов</b> даёт 1 оплаченный слот? "
                "Число, например <code>1000000</code>. Чем больше — тем дешевле "
                "обходится гостю каждый токен.",
                parse_mode="HTML",
            )
        await query.answer()

    # ---- notifications ----
    @router.callback_query(F.data == "pm:notif")
    async def cb_notif(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await _edit(query, _body_notif(), _kb_notif())
        await query.answer()

    @router.callback_query(F.data.startswith("pm:nedit:"))
    async def cb_notif_edit(query: CallbackQuery, state: FSMContext) -> None:
        if _deny(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        which = query.data.split(":")[2]
        if which == "welcome":
            await state.set_state(PrivateModeStates.awaiting_welcome)
            prompt = ("✏️ Пришли текст, который увидит новый гость при входе. "
                      "Под ним появятся кнопки оплаты (теми способами, что включены).")
        else:
            await state.set_state(PrivateModeStates.awaiting_limit)
            prompt = ("✏️ Пришли текст, который увидит гость, когда его лимит "
                      "кончился. Под ним тоже будут кнопки оплаты.")
        if query.message is not None:
            await query.message.answer(prompt)
        await query.answer()

    # ---- text capture for all awaiting states ----
    @router.message(PrivateModeStates.awaiting_card, F.text & ~F.text.startswith("/"))
    async def cap_card(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        storage.pm_set_method_details("card", (message.text or "").strip())
        await state.clear()
        await message.answer("💳 Реквизиты карты сохранены.", reply_markup=_kb_pay())
        await message.answer(_body_pay(), reply_markup=_kb_pay(), parse_mode="HTML")

    @router.message(PrivateModeStates.awaiting_card_token, F.text & ~F.text.startswith("/"))
    async def cap_card_token(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        raw = (message.text or "").strip()
        await state.clear()
        if raw == "-":
            storage.pm_set_card_token("")
            await message.answer("✋ Авто-оплата отключена — карта снова в ручном режиме.",
                                 reply_markup=_kb_pay())
        else:
            parts = raw.split()
            token = parts[0]
            currency = parts[1].upper() if len(parts) > 1 else storage.pm_get_card_currency()
            storage.pm_set_card_token(token)
            storage.pm_set_card_currency(currency)
            await message.answer(
                f"🤖 Токен сохранён. Валюта: {currency}. Теперь кнопка "
                "«Оплатить картой» выставляет настоящий счёт Visa/Mastercard.",
                reply_markup=_kb_pay(),
            )
        await message.answer(_body_pay(), reply_markup=_kb_pay(), parse_mode="HTML")

    @router.message(PrivateModeStates.awaiting_crypto_token, F.text & ~F.text.startswith("/"))
    async def cap_crypto_token(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        raw = (message.text or "").strip()
        await state.clear()
        if raw == "-":
            storage.pm_set_crypto_token("")
            await message.answer("✋ Авто-крипта отключена — снова ручной режим (адрес кошелька).",
                                 reply_markup=_kb_pay())
        else:
            storage.pm_set_crypto_token(raw)
            await message.answer(
                "🤖 CryptoBot токен сохранён. Теперь кнопка «Оплатить криптой» "
                "создаёт счёт в CryptoBot и доступ начисляется после оплаты.",
                reply_markup=_kb_pay(),
            )
        await message.answer(_body_pay(), reply_markup=_kb_pay(), parse_mode="HTML")

    @router.message(PrivateModeStates.awaiting_crypto_pub_url, F.text & ~F.text.startswith("/"))
    async def cap_crypto_pub_url(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        raw = (message.text or "").strip()
        await state.clear()
        if raw == "-":
            storage.pm_set_crypto_public_url("")
            await message.answer("✅ Публичный URL очищен — снова автоопределение.")
        else:
            storage.pm_set_crypto_public_url(raw)
            await message.answer("✅ Публичный URL сохранён.")
        from .crypto_webhook import webhook_url
        base = storage.pm_get_crypto_public_url()
        if not base:
            try:
                from ...config import resolve_public_url
                base = resolve_public_url()
            except Exception:  # noqa: BLE001
                base = ""
        url = webhook_url(base) if base else "(публичный URL не определён)"
        await message.answer(
            "Вставь этот адрес в @CryptoBot → Crypto Pay → My Apps → Webhooks:\n"
            f"<code>{_esc(url)}</code>",
            parse_mode="HTML",
        )

    @router.message(PrivateModeStates.awaiting_crypto, F.text & ~F.text.startswith("/"))
    async def cap_crypto(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        storage.pm_set_method_details("crypto", (message.text or "").strip())
        await state.clear()
        await message.answer("🪙 Крипто-реквизиты сохранены.", reply_markup=_kb_pay())
        await message.answer(_body_pay(), reply_markup=_kb_pay(), parse_mode="HTML")

    @router.message(PrivateModeStates.awaiting_price, F.text & ~F.text.startswith("/"))
    async def cap_price(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        raw = (message.text or "").strip().replace(",", ".").lstrip("$").strip()
        try:
            usd = float(raw)
        except ValueError:
            await message.answer("Это не число. Пришли, например, 10.")
            return
        storage.pm_set_usd_per_slot(usd)
        await state.clear()
        await message.answer(f"💵 Цена: ${usd:g} = 1 слот.", reply_markup=_kb_pay())
        await message.answer(_body_pay(), reply_markup=_kb_pay(), parse_mode="HTML")

    @router.message(PrivateModeStates.awaiting_tokens, F.text & ~F.text.startswith("/"))
    async def cap_tokens(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        raw = (message.text or "").strip().replace(",", "").replace(" ", "").replace("_", "")
        try:
            tokens = int(float(raw))
        except ValueError:
            await message.answer("Это не число. Пришли, например, 1000000.")
            return
        storage.pm_set_tokens_per_slot(tokens)
        await state.clear()
        await message.answer(
            f"🎟 1 слот = {storage.pm_get_tokens_per_slot():,} токенов.",
            reply_markup=_kb_pay(),
        )
        await message.answer(_body_pay(), reply_markup=_kb_pay(), parse_mode="HTML")

    @router.message(PrivateModeStates.awaiting_welcome, F.text & ~F.text.startswith("/"))
    async def cap_welcome(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        storage.pm_set_notification("welcome", (message.text or "").strip())
        await state.clear()
        await message.answer("📣 Текст при входе сохранён.", reply_markup=_kb_notif())
        await message.answer(_body_notif(), reply_markup=_kb_notif(), parse_mode="HTML")

    @router.message(PrivateModeStates.awaiting_limit, F.text & ~F.text.startswith("/"))
    async def cap_limit(message: Message, state: FSMContext) -> None:
        if not is_owner(message.from_user.id):
            return
        storage.pm_set_notification("limit_reached", (message.text or "").strip())
        await state.clear()
        await message.answer("📣 Текст «лимит кончился» сохранён.", reply_markup=_kb_notif())
        await message.answer(_body_notif(), reply_markup=_kb_notif(), parse_mode="HTML")

    # ---- guest-facing payment (only enabled methods are reachable) ----
    @router.callback_query(F.data == "pm:buy:card")
    async def cb_buy_card(query: CallbackQuery, state: FSMContext) -> None:
        if not storage.pm_is_method_enabled("card"):
            await query.answer("Оплата картой выключена.", show_alert=True)
            return
        price = storage.pm_get_usd_per_slot()
        token = storage.pm_get_card_token()
        if query.message is None:
            await query.answer()
            return
        # Auto mode: a provider token is configured → send a real in-Telegram
        # Visa/Mastercard invoice (native Telegram Payments). The amount is in
        # the smallest currency unit (kopecks/cents), so price * 100.
        if token:
            currency = storage.pm_get_card_currency()
            amount = max(1, int(round(price * 100)))
            try:
                await query.message.answer_invoice(
                    title="Доступ к боту",
                    description=f"1 слот доступа (${price:g}).",
                    payload=f"pm_slot:{query.from_user.id}",
                    currency=currency,
                    prices=[LabeledPrice(label="1 слот", amount=amount)],
                    provider_token=token,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("card invoice failed: %s", exc)
                await query.answer(f"Не получилось выставить счёт: {exc}", show_alert=True)
                return
            await query.answer()
            return
        # Manual fallback: no token yet → show the owner's requisites text.
        details = storage.pm_get_method_details("card") or "(реквизиты не заданы)"
        await query.message.answer(
            f"💳 <b>Оплата картой</b>\n\nСумма: ${price:g} за 1 слот.\n\n"
            f"Реквизиты:\n<code>{_esc(details)}</code>\n\n"
            "После перевода пришли владельцу подтверждение — он начислит доступ.",
            parse_mode="HTML",
        )
        await query.answer()

    @router.callback_query(F.data == "pm:buy:crypto")
    async def cb_buy_crypto(query: CallbackQuery, state: FSMContext) -> None:
        if not storage.pm_is_method_enabled("crypto"):
            await query.answer("Оплата криптой выключена.", show_alert=True)
            return
        price = storage.pm_get_usd_per_slot()
        token = storage.pm_get_crypto_token()
        if query.message is None:
            await query.answer()
            return
        # Auto mode: a Crypto Pay token is set → create a real CryptoBot invoice
        # and give the guest a pay button + a "I paid / check" button.
        if token:
            try:
                invoice = await _crypto_create_invoice(token, price, query.from_user.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("crypto invoice failed: %s", exc)
                await query.answer(f"Не получилось создать счёт: {exc}", show_alert=True)
                return
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🪙 Оплатить в CryptoBot", url=invoice.bot_invoice_url)],
                [InlineKeyboardButton(text="✅ Я оплатил — проверить",
                                      callback_data=f"pm:cchk:{invoice.invoice_id}")],
            ])
            await query.message.answer(
                f"🪙 <b>Оплата криптой</b>\n\nСумма: ${price:g} за 1 слот.\n\n"
                "Нажми «Оплатить в CryptoBot», выбери монету и оплати. "
                "Потом вернись и нажми «Я оплатил — проверить».",
                reply_markup=kb, parse_mode="HTML",
            )
            await query.answer()
            return
        # Manual fallback: no token → show the wallet requisites text.
        details = storage.pm_get_method_details("crypto") or "(реквизиты не заданы)"
        await query.message.answer(
            f"🪙 <b>Оплата криптой</b>\n\nСумма: ${price:g} за 1 слот.\n\n"
            f"Реквизиты:\n<code>{_esc(details)}</code>\n\n"
            "После перевода пришли владельцу хэш транзакции — он начислит доступ.",
            parse_mode="HTML",
        )
        await query.answer()

    @router.callback_query(F.data.startswith("pm:cchk:"))
    async def cb_crypto_check(query: CallbackQuery, state: FSMContext) -> None:
        invoice_id = query.data.split(":", 2)[2]
        token = storage.pm_get_crypto_token()
        if not token:
            await query.answer("Крипто-оплата сейчас в ручном режиме.", show_alert=True)
            return
        if storage.pm_crypto_was_credited(invoice_id):
            await query.answer("Этот счёт уже зачтён ранее.", show_alert=True)
            return
        try:
            paid = await _crypto_invoice_paid(token, invoice_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("crypto check failed: %s", exc)
            await query.answer(f"Не удалось проверить: {exc}", show_alert=True)
            return
        if not paid:
            await query.answer("Оплата пока не пришла. Если уже оплатил — подожди минуту и нажми ещё раз.",
                               show_alert=True)
            return
        usd = storage.pm_get_usd_per_slot()
        storage.pm_crypto_mark_credited(invoice_id, query.from_user.id)
        new_bal = storage.pm_add_balance(query.from_user.id, usd)
        if query.message is not None:
            await query.message.answer(
                f"✅ Оплата криптой получена. Начислено ${usd:g}. Баланс: ${new_bal:g}.\n"
                "Доступ открыт — пользуйся!"
            )
        await query.answer("Готово, доступ начислен ✅")

    @router.callback_query(F.data == "pm:buy:stars")
    async def cb_buy_stars(query: CallbackQuery, state: FSMContext) -> None:
        if not storage.pm_is_method_enabled("stars"):
            await query.answer("Оплата звёздами выключена.", show_alert=True)
            return
        price = storage.pm_get_usd_per_slot()
        # Telegram Stars are charged in whole XTR units. We bill the price
        # number as XTR (owner tunes it via the price button). usd_per_slot is
        # credited to the guest on a successful payment.
        stars = max(1, int(round(price)))
        if query.message is None:
            await query.answer()
            return
        try:
            await query.message.answer_invoice(
                title="Доступ к боту",
                description=f"1 слот доступа (${price:g}).",
                payload=f"pm_slot:{query.from_user.id}",
                currency="XTR",
                prices=[LabeledPrice(label="1 слот", amount=stars)],
                provider_token="",  # Stars need no provider token
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("stars invoice failed: %s", exc)
            await query.answer(f"Не получилось выставить счёт: {exc}", show_alert=True)
            return
        await query.answer()

    @router.pre_checkout_query()
    async def on_pre_checkout(pcq: PreCheckoutQuery) -> None:
        payload = getattr(pcq, "invoice_payload", "") or ""
        if not payload.startswith("pm_slot:"):
            with contextlib.suppress(Exception):
                await pcq.answer(ok=False, error_message="Неизвестный счёт.")
            return
        with contextlib.suppress(Exception):
            await pcq.answer(ok=True)

    @router.message(F.successful_payment)
    async def on_paid(message: Message, state: FSMContext) -> None:
        sp = message.successful_payment
        payload = getattr(sp, "invoice_payload", "") or ""
        if not payload.startswith("pm_slot:"):
            return  # not ours
        # Record the Stars charge id so the owner can refund it later
        # (matches the official aiogram example + bohd4nx/stars-payment).
        charge_id = getattr(sp, "telegram_payment_charge_id", "") or ""
        # Anti-double-credit: skip if this charge was already processed.
        if charge_id and storage.pm_charge_exists(message.from_user.id, charge_id):
            await message.answer("ℹ️ Эта оплата уже была зачтена ранее.")
            return
        if charge_id:
            storage.pm_record_charge(message.from_user.id, charge_id)
        usd = storage.pm_get_usd_per_slot()
        # Verify the paid amount matches the expected price (guards against
        # stale invoices or price changes between invoice creation and payment).
        paid_amount = getattr(sp, "total_amount", 0) or 0
        currency = getattr(sp, "currency", "") or ""
        if currency == "XTR":
            expected = max(1, int(round(usd)))
        else:
            expected = max(1, int(round(usd * 100)))
        if paid_amount and paid_amount < expected:
            logger.warning(
                "payment amount mismatch: paid=%s %s, expected=%s (user %s)",
                paid_amount, currency, expected, message.from_user.id,
            )
            await message.answer(
                "⚠️ Сумма оплаты меньше текущей цены слота. "
                "Свяжись с владельцем бота для ручного зачисления."
            )
            return
        new_bal = storage.pm_add_balance(message.from_user.id, usd)
        await message.answer(
            f"✅ Оплата получена. Начислено ${usd:g}. Баланс: ${new_bal:g}.\n"
            "Доступ открыт — пользуйся!"
        )

    @router.message(Command("pm_refund"))
    async def cmd_refund(message: Message, bot: Bot) -> None:
        """Owner-only: refund a guest's last Stars payment.

        Usage: ``/pm_refund <user_id>``. Telegram requires merchants to be able
        to refund Stars — this mirrors bohd4nx/stars-payment's /refund.
        """
        if not is_owner(message.from_user.id if message.from_user else 0):
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("Использование: <code>/pm_refund &lt;user_id&gt;</code>", parse_mode="HTML")
            return
        uid = int(parts[1])
        charge_id = storage.pm_get_last_charge(uid)
        if not charge_id:
            await message.answer(f"У {uid} нет известных оплат звёздами для возврата.")
            return
        try:
            await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Не получилось вернуть: {exc}")
            return
        storage.pm_clear_charge(uid, charge_id)
        # Roll back the credited slot so the refunded balance isn't usable.
        storage.pm_deduct_balance(uid, storage.pm_get_usd_per_slot())
        await message.answer(f"↩️ Возврат звёзд для {uid} выполнен.")

    return router


# --------------------------------------------------------------------------
# payment-gate outer middleware — makes paid access ACTUALLY enforced
# --------------------------------------------------------------------------
def _extract_user_and_cb(event):
    """Pull (user_id, callback_data, target_obj) out of an aiogram Update."""
    msg = getattr(event, "message", None)
    cq = getattr(event, "callback_query", None)
    if msg is not None and getattr(msg, "from_user", None):
        return msg.from_user.id, None, msg, bool(getattr(msg, "successful_payment", None))
    if cq is not None and getattr(cq, "from_user", None):
        return cq.from_user.id, (cq.data or ""), cq, False
    return None, None, None, False


def build_payment_gate_middleware():
    """Outer middleware that blocks guests without paid access and shows the
    welcome / limit-reached offer with pay buttons.

    Owners/co-owners always pass. Payment-related updates (the pay buttons,
    pre-checkout, the successful-payment message) always pass so a guest can
    actually complete a purchase. Everything is wrapped defensively so a bug
    here can never take the whole bot down.
    """
    async def _gate(handler, event, data):  # noqa: ANN001
        try:
            uid, cb_data, target, is_payment_msg = _extract_user_and_cb(event)
            if uid is None:
                return await handler(event, data)
            # let pre_checkout and the successful-payment message through
            if getattr(event, "pre_checkout_query", None) is not None or is_payment_msg:
                return await handler(event, data)
            # owner / co-owner always pass
            if storage.is_owner_or_co_owner(uid):
                return await handler(event, data)
            # let the payment UI itself through (pay buttons, offer navigation)
            if cb_data and cb_data.startswith(("pm:buy", "pm:menu", "pm:cchk")):
                return await handler(event, data)
            decision = storage.pm_guest_gate_decision(uid)
            if decision == "allow":
                return await handler(event, data)
            # blocked → present the offer, drop the update
            text, kb = guest_offer("welcome" if decision == "welcome" else "limit_reached")
            with contextlib.suppress(Exception):
                if target is not None:
                    await target.answer(text, reply_markup=kb, parse_mode="HTML")
            return None
        except Exception:  # noqa: BLE001 — never let the gate break the bot
            logger.exception("payment gate middleware error")
            return await handler(event, data)

    return _gate
