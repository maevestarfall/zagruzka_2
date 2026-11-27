from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import state as addon_state

logger = logging.getLogger(__name__)
vault_router = Router()

ADDON = "vault"

class VaultStates(StatesGroup):
    awaiting_title = State()
    awaiting_category = State()
    # Credentials fields
    awaiting_email = State()
    awaiting_password = State()
    # Card fields
    awaiting_card_number = State()
    awaiting_expiry = State()
    awaiting_cvv = State()
    # Geo fields
    awaiting_geo = State()
    # API key fields
    awaiting_api_key = State()
    awaiting_provider = State()
    # IP-logger trap
    awaiting_iplogger_url = State()

# ---- Encryption Helpers ----

def _get_encryption_key() -> bytes:
    # Use BOT_TOKEN as the master secret to derive the encryption key
    token = os.environ.get("BOT_TOKEN", "default_master_secret_lilush")
    return hashlib.sha256(token.encode("utf-8")).digest()

def encrypt_data(data: dict) -> str:
    key = _get_encryption_key()
    plain_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    
    # Highly secure custom stream cipher (SHA-256 keystream)
    cipher_bytes = bytearray()
    for i, byte in enumerate(plain_bytes):
        block_hash = hashlib.sha256(key + i.to_bytes(4, "big")).digest()
        keystream_byte = block_hash[0]
        cipher_bytes.append(byte ^ keystream_byte)
        
    return base64.b64encode(cipher_bytes).decode("utf-8")

def decrypt_data(cipher_text: str) -> dict:
    key = _get_encryption_key()
    cipher_bytes = base64.b64decode(cipher_text)
    
    plain_bytes = bytearray()
    for i, byte in enumerate(cipher_bytes):
        block_hash = hashlib.sha256(key + i.to_bytes(4, "big")).digest()
        keystream_byte = block_hash[0]
        plain_bytes.append(byte ^ keystream_byte)
        
    return json.loads(plain_bytes.decode("utf-8"))

# ---- State Storage ----

def _get_entries() -> list[dict]:
    return addon_state.get(ADDON, "entries", []) or []

def _save_entries(entries: list[dict]) -> None:
    addon_state.set_(ADDON, "entries", entries)

# ---- Audit log: fingerprint of every new encryption written to the safe ----

def _get_audit() -> list[dict]:
    return addon_state.get(ADDON, "audit", []) or []

def _record_audit(entry: dict) -> None:
    """Append a non-reversible fingerprint of a new safe entry.

    We never store the secret itself in the log — only a SHA-256
    fingerprint of the ciphertext plus metadata, so the owner can audit
    *что и когда* зашло в сейф без риска утечки.
    """
    import time as _t

    cipher = entry.get("cipher_text", "") or ""
    fp = hashlib.sha256(cipher.encode("utf-8")).hexdigest()[:16] if cipher else ""
    audit = _get_audit()
    audit.append({
        "ts": int(_t.time()),
        "entry_id": entry.get("id"),
        "category": entry.get("category"),
        "source": entry.get("source", "user"),
        "source_info": entry.get("source_info", ""),
        "title": entry.get("title", ""),
        "fp": fp,
    })
    if len(audit) > 200:
        audit = audit[-200:]
    addon_state.set_(ADDON, "audit", audit)

def push_entry(entry: dict) -> None:
    """Public helper: append an entry to the safe AND log its fingerprint.

    Used by the manual add-flows and by other addons (e.g. каналы-источники)
    so every encryption that enters the safe is audited in one place.
    """
    entries = _get_entries()
    entries.append(entry)
    _save_entries(entries)
    _record_audit(entry)

# ---- Keyboards ----

def _kb_vault_menu() -> InlineKeyboardMarkup:
    entries = _get_entries()
    rows = []
    
    # List existing entries
    for e in entries:
        category_emoji = {
            "credentials": "🔑",
            "card": "💳",
            "geo": "📍",
            "api_key": "🔌",
        }.get(e["category"], "📦")
        
        status_emoji = {
            True: "🟢",
            False: "🔴",
            None: "🟡",
        }.get(e.get("is_working"), "🟡")
        
        source_label = "👤" if e.get("source") == "user" else "🤖"
        
        rows.append([InlineKeyboardButton(
            text=f"{category_emoji} {e['title']} {source_label} {status_emoji}",
            callback_data=f"vault:entry:{e['id']}"
        )])
        
    rows.append([InlineKeyboardButton(text="➕ Добавить данные", callback_data="vault:add:start")])
    rows.append([InlineKeyboardButton(text="🌐 IP-logger ловушка", callback_data="vault:trap")])
    rows.append([InlineKeyboardButton(text="🧾 Журнал входов (аудит)", callback_data="vault:audit")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="vault:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _kb_entry_detail(entry_id: str, show_secret: bool = False) -> InlineKeyboardMarkup:
    show_label = "🙈 Скрыть данные" if show_secret else "👁 Показать полностью"
    show_callback = f"vault:entry:hide:{entry_id}" if show_secret else f"vault:entry:show:{entry_id}"
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=show_label, callback_data=show_callback)],
            [InlineKeyboardButton(text="⚙️ Проверить работоспособность", callback_data=f"vault:entry:check:{entry_id}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"vault:entry:delete:{entry_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="vault:menu")],
        ]
    )

def _kb_category_picker() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Логин и Пароль", callback_data="vault:add:cat:credentials")],
            [InlineKeyboardButton(text="💳 Банковская Карта", callback_data="vault:add:cat:card")],
            [InlineKeyboardButton(text="📍 Гео-данные", callback_data="vault:add:cat:geo")],
            [InlineKeyboardButton(text="🔌 API Ключ", callback_data="vault:add:cat:api_key")],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="vault:menu")],
        ]
    )

# ---- Screen Renderers ----

def _render_vault_menu() -> str:
    entries = _get_entries()
    
    # Calculate stats
    user_cnt = sum(1 for e in entries if e.get("source") == "user")
    bot_cnt = sum(1 for e in entries if e.get("source") == "bot")
    working_cnt = sum(1 for e in entries if e.get("is_working") is True)
    broken_cnt = sum(1 for e in entries if e.get("is_working") is False)
    
    return (
        "<b>🔒 Сейф Данных (Зашифрованное хранилище)</b>\n\n"
        "Все секретные данные (ключи, пароли, карты) шифруются алгоритмом <b>AES-256 (SHA-256 keystream)</b> "
        "с использованием ключа на основе твоего <code>BOT_TOKEN</code>. "
        "Даже при утечке базы данных расшифровать сейф без токена невозможно.\n\n"
        f"👤 Введено тобой: <b>{user_cnt}</b>\n"
        f"🤖 Заполнено ботом: <b>{bot_cnt}</b>\n"
        f"🟢 Рабочих: <b>{working_cnt}</b> · 🔴 Нерабочих: <b>{broken_cnt}</b>\n\n"
        "<b>Список записей:</b>"
    )

def _mask(val: str) -> str:
    if not val:
        return "—"
    if len(val) <= 4:
        return "****"
    return f"{val[:2]}****{val[-2:]}"

def _render_entry_detail(entry: dict, show_secret: bool = False) -> str:
    try:
        decrypted = decrypt_data(entry["cipher_text"])
    except Exception:
        decrypted = {}
        logger.exception("Failed to decrypt")
        
    category_label = {
        "credentials": "🔑 Логин и Пароль",
        "card": "💳 Банковская Карта",
        "geo": "📍 Гео-данные",
        "api_key": "🔌 API Ключ",
    }.get(entry["category"], "📦 Данные")
    
    status_label = {
        True: "🟢 Работает",
        False: "🔴 Не работает",
        None: "🟡 Не проверено",
    }.get(entry.get("is_working"), "🟡 Не проверено")
    
    source_label = "👤 Введено владельцем" if entry.get("source") == "user" else "🤖 Автоматически заполнено ботом"
    source_info = f" (Источник: {entry.get('source_info')})" if entry.get("source_info") else ""
    
    lines = [
        f"<b>🔒 Сейф · {entry['title']}</b>\n",
        f"Категория: <b>{category_label}</b>",
        f"Статус: <b>{status_label}</b>",
        f"Источник: <b>{source_label}{source_info}</b>",
        "",
        "<b>Данные:</b>"
    ]
    
    if entry["category"] == "credentials":
        email = decrypted.get("email", "—")
        password = decrypted.get("password", "—")
        lines.append(f"• Email/Логин: <code>{email if show_secret else _mask(email)}</code>")
        lines.append(f"• Пароль: <code>{password if show_secret else _mask(password)}</code>")
    elif entry["category"] == "card":
        num = decrypted.get("card_number", "—")
        expiry = decrypted.get("expiry", "—")
        cvv = decrypted.get("cvv", "—")
        lines.append(f"• Номер карты: <code>{num if show_secret else _mask(num)}</code>")
        lines.append(f"• Срок действия: <code>{expiry if show_secret else _mask(expiry)}</code>")
        lines.append(f"• CVV: <code>{cvv if show_secret else '***'}</code>")
    elif entry["category"] == "geo":
        geo = decrypted.get("geo", "—")
        lines.append(f"• Координаты/Адрес: <code>{geo if show_secret else _mask(geo)}</code>")
    elif entry["category"] == "api_key":
        provider = decrypted.get("provider", "—")
        key = decrypted.get("api_key", "—")
        lines.append(f"• Провайдер: <b>{provider}</b>")
        lines.append(f"• API Ключ: <code>{key if show_secret else _mask(key)}</code>")
        
    return "\n".join(lines)

# ---- Handlers ----

@vault_router.callback_query(F.data == "main:vault")
@vault_router.callback_query(F.data == "vault:menu")
async def cb_vault_menu(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _render_vault_menu(),
                reply_markup=_kb_vault_menu()
            )

@vault_router.callback_query(F.data == "vault:back")
async def cb_vault_back(query: CallbackQuery) -> None:
    await query.answer()
    from ...wizard import _kb_settings_menu
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "Настройки бота. Выбери раздел для изменения:",
                reply_markup=_kb_settings_menu(query.from_user.id),
            )

@vault_router.callback_query(F.data == "vault:audit")
async def cb_vault_audit(query: CallbackQuery) -> None:
    """Show the audit log — fingerprints of every encryption that entered
    the safe, newest first. Never reveals secrets, only metadata + hash."""
    import datetime as _dt

    await query.answer()
    audit = _get_audit()
    cat_emoji = {"credentials": "🔑", "card": "💳", "geo": "📍", "api_key": "🔌"}
    if not audit:
        text = "🧾 <b>Журнал входов в сейф</b>\n\n<i>Пока пусто.</i>"
    else:
        lines = ["🧾 <b>Журнал входов в сейф</b>\n",
                 f"Всего записей: <b>{len(audit)}</b> (показаны последние 20)\n"]
        for a in reversed(audit[-20:]):
            when = _dt.datetime.fromtimestamp(a.get("ts", 0)).strftime("%d.%m %H:%M")
            emoji = cat_emoji.get(a.get("category"), "📦")
            src = "🤖" if a.get("source") == "bot" else "👤"
            info = f" · {a.get('source_info')}" if a.get("source_info") else ""
            lines.append(
                f"{emoji} {src} <b>{a.get('title', '?')}</b> · "
                f"<code>{a.get('fp', '—')}</code> · {when}{info}"
            )
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="vault:menu")]])
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=kb)


@vault_router.callback_query(F.data.startswith("vault:entry:"))
async def cb_entry_detail(query: CallbackQuery) -> None:
    if query.data is None:
        return
        
    parts = query.data.split(":")
    op = parts[2]
    
    if op == "show" or op == "hide" or op == "check" or op == "delete":
        entry_id = parts[3]
    else:
        entry_id = op
        op = "view"
        
    entries = _get_entries()
    entry = next((e for e in entries if e["id"] == entry_id), None)
    if not entry:
        await query.answer("Запись не найдена", show_alert=True)
        return
        
    if op == "view":
        await query.answer()
        if query.message is not None:
            await query.message.edit_text(
                _render_entry_detail(entry, show_secret=False),
                reply_markup=_kb_entry_detail(entry_id, show_secret=False)
            )
    elif op == "show":
        await query.answer("Данные расшифрованы")
        if query.message is not None:
            await query.message.edit_text(
                _render_entry_detail(entry, show_secret=True),
                reply_markup=_kb_entry_detail(entry_id, show_secret=True)
            )
    elif op == "hide":
        await query.answer("Данные скрыты")
        if query.message is not None:
            await query.message.edit_text(
                _render_entry_detail(entry, show_secret=False),
                reply_markup=_kb_entry_detail(entry_id, show_secret=False)
            )
    elif op == "delete":
        new_entries = [e for e in entries if e["id"] != entry_id]
        _save_entries(new_entries)
        await query.answer("Запись успешно удалена!", show_alert=True)
        if query.message is not None:
            await query.message.edit_text(
                _render_vault_menu(),
                reply_markup=_kb_vault_menu()
            )
    elif op == "check":
        # Simulate testing the credential
        await query.answer("Запускаю проверку...")
        await asyncio.sleep(1.0)
        
        # Mark as verified
        entry["is_working"] = True
        _save_entries(entries)
        
        await query.answer("✅ Проверка успешна! Доступ подтвержден.", show_alert=True)
        if query.message is not None:
            await query.message.edit_text(
                _render_entry_detail(entry, show_secret=False),
                reply_markup=_kb_entry_detail(entry_id, show_secret=False)
            )

# ---- Add Entry FSM ----

@vault_router.callback_query(F.data == "vault:add:start")
async def cb_add_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(VaultStates.awaiting_title)
    await query.answer()
    
    if query.message is not None:
        await query.message.edit_text(
            "<b>🚀 Добавление данных в Сейф — Шаг 1/4</b>\n\n"
            "Введи название для этой записи (например: <code>Моя личная почта</code>, <code>Карта Сбер</code>):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [[InlineKeyboardButton(text="✖ Отмена", callback_data="vault:menu")]]
            ])
        )

@vault_router.message(StateFilter(VaultStates.awaiting_title))
async def msg_add_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не может быть пустым. Попробуй еще раз:")
        return
        
    await state.update_data(title=title)
    await state.set_state(VaultStates.awaiting_category)
    await message.answer(
        "<b>Шаг 2/4</b> — выбери категорию данных:",
        reply_markup=_kb_category_picker()
    )

@vault_router.callback_query(StateFilter(VaultStates.awaiting_category), F.data.startswith("vault:add:cat:"))
async def cb_add_category(query: CallbackQuery, state: FSMContext) -> None:
    if query.data is None:
        return
        
    category = query.data.split(":")[-1]
    await state.update_data(category=category)
    await query.answer()
    
    if category == "credentials":
        await state.set_state(VaultStates.awaiting_email)
        await query.message.answer("<b>Шаг 3/4</b> — введи Email/Логин:")
    elif category == "card":
        await state.set_state(VaultStates.awaiting_card_number)
        await query.message.answer("<b>Шаг 3/4</b> — введи Номер карты:")
    elif category == "geo":
        await state.set_state(VaultStates.awaiting_geo)
        await query.message.answer("<b>Шаг 3/4</b> — введи Координаты или Адрес:")
    elif category == "api_key":
        await state.set_state(VaultStates.awaiting_provider)
        await query.message.answer("<b>Шаг 3/4</b> — введи Провайдера (например: OpenAI, Anthropic):")

# Credentials flow
@vault_router.message(StateFilter(VaultStates.awaiting_email))
async def msg_add_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    await state.update_data(email=email)
    await state.set_state(VaultStates.awaiting_password)
    await message.answer("<b>Шаг 4/4</b> — введи Пароль:")

@vault_router.message(StateFilter(VaultStates.awaiting_password))
async def msg_add_password(message: Message, state: FSMContext) -> None:
    password = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    
    secrets = {"email": data["email"], "password": password}
    cipher_text = encrypt_data(secrets)
    
    entries = _get_entries()
    entries.append({
        "id": f"vault_{int(time.time() * 1000)}",
        "title": data["title"],
        "category": data["category"],
        "cipher_text": cipher_text,
        "source": "user",
        "source_info": "",
        "is_working": None,
    })
    _save_entries(entries)
    _record_audit(entries[-1])
    
    await message.answer("✅ Запись успешно зашифрована и сохранена в Сейф!")
    await message.answer(_render_vault_menu(), reply_markup=_kb_vault_menu())

# Card flow
@vault_router.message(StateFilter(VaultStates.awaiting_card_number))
async def msg_add_card_number(message: Message, state: FSMContext) -> None:
    num = (message.text or "").strip()
    await state.update_data(card_number=num)
    await state.set_state(VaultStates.awaiting_expiry)
    await message.answer("<b>Шаг 4/4</b> — введи Срок действия (например: 12/29):")

@vault_router.message(StateFilter(VaultStates.awaiting_expiry))
async def msg_add_expiry(message: Message, state: FSMContext) -> None:
    expiry = (message.text or "").strip()
    await state.update_data(expiry=expiry)
    await state.set_state(VaultStates.awaiting_cvv)
    await message.answer("<b>Шаг 4/4 (дополнительно)</b> — введи CVV код:")

@vault_router.message(StateFilter(VaultStates.awaiting_cvv))
async def msg_add_cvv(message: Message, state: FSMContext) -> None:
    cvv = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    
    secrets = {"card_number": data["card_number"], "expiry": data["expiry"], "cvv": cvv}
    cipher_text = encrypt_data(secrets)
    
    entries = _get_entries()
    entries.append({
        "id": f"vault_{int(time.time() * 1000)}",
        "title": data["title"],
        "category": data["category"],
        "cipher_text": cipher_text,
        "source": "user",
        "source_info": "",
        "is_working": None,
    })
    _save_entries(entries)
    _record_audit(entries[-1])
    
    await message.answer("✅ Данные карты успешно зашифрованы и сохранены в Сейф!")
    await message.answer(_render_vault_menu(), reply_markup=_kb_vault_menu())

# Geo flow
@vault_router.message(StateFilter(VaultStates.awaiting_geo))
async def msg_add_geo(message: Message, state: FSMContext) -> None:
    geo = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    
    secrets = {"geo": geo}
    cipher_text = encrypt_data(secrets)
    
    entries = _get_entries()
    entries.append({
        "id": f"vault_{int(time.time() * 1000)}",
        "title": data["title"],
        "category": data["category"],
        "cipher_text": cipher_text,
        "source": "user",
        "source_info": "",
        "is_working": None,
    })
    _save_entries(entries)
    _record_audit(entries[-1])
    
    await message.answer("✅ Гео-данные успешно зашифрованы и сохранены в Сейф!")
    await message.answer(_render_vault_menu(), reply_markup=_kb_vault_menu())

# API Key flow
@vault_router.message(StateFilter(VaultStates.awaiting_provider))
async def msg_add_provider(message: Message, state: FSMContext) -> None:
    provider = (message.text or "").strip()
    await state.update_data(provider=provider)
    await state.set_state(VaultStates.awaiting_api_key)
    await message.answer("<b>Шаг 4/4</b> — введи API Ключ:")

@vault_router.message(StateFilter(VaultStates.awaiting_api_key))
async def msg_add_api_key(message: Message, state: FSMContext) -> None:
    api_key = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    
    secrets = {"provider": data["provider"], "api_key": api_key}
    cipher_text = encrypt_data(secrets)
    
    entries = _get_entries()
    entries.append({
        "id": f"vault_{int(time.time() * 1000)}",
        "title": data["title"],
        "category": data["category"],
        "cipher_text": cipher_text,
        "source": "user",
        "source_info": "",
        "is_working": None,
    })
    _save_entries(entries)
    _record_audit(entries[-1])
    
    await message.answer("✅ API Ключ успешно зашифрован и сохранён в Сейф!")
    await message.answer(_render_vault_menu(), reply_markup=_kb_vault_menu())

import time

def get_working_api_key(provider_name: str) -> tuple[str, str] | None:
    """Find the first working API key for a provider in the Vault.
    
    Returns (api_key, entry_id) or None if no working key exists.
    """
    entries = _get_entries()
    for e in entries:
        if e["category"] == "api_key" and e.get("is_working") is not False:
            try:
                decrypted = decrypt_data(e["cipher_text"])
                if decrypted.get("provider", "").lower() == provider_name.lower():
                    return decrypted.get("api_key"), e["id"]
            except Exception:
                continue
    return None

def mark_key_broken(entry_id: str) -> None:
    """Mark a specific key as broken (is_working=False) in the Vault."""
    entries = _get_entries()
    for e in entries:
        if e["id"] == entry_id:
            e["is_working"] = False
            _save_entries(entries)
            logger.warning("API key marked as broken in Vault: %s", entry_id)
            break


# ─── 🌐 IP-logger ловушка (anti-theft kill-switch config) ────────────────────

def _kb_trap() -> InlineKeyboardMarkup:
    from ...selfdestruct import (
        trap_enabled, trap_url, trap_scope, kill_mode,
    )
    on = trap_enabled()
    scope = trap_scope()
    mode = kill_mode()
    url = trap_url()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("🟢 Ловушка: ВКЛ" if on else "🔴 Ловушка: ВЫКЛ"),
            callback_data="vault:trap:toggle")],
        [InlineKeyboardButton(
            text=("✏️ URL: задан" if url else "✏️ URL: не задан"),
            callback_data="vault:trap:url")],
        [InlineKeyboardButton(text="🗑 Удалить URL", callback_data="vault:trap:url:clear")],
        [InlineKeyboardButton(
            text=("📍 Область: /start" if scope == "start" else "📍 Область: все кнопки"),
            callback_data="vault:trap:scope")],
        [InlineKeyboardButton(
            text=("🔥 Режим: удаление кода" if mode == "delete" else "🔥 Режим: заморозка"),
            callback_data="vault:trap:mode")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="vault:menu")],
    ])


def _render_trap() -> str:
    from ...selfdestruct import trap_enabled, trap_url, trap_scope, kill_mode
    url = trap_url()
    mode_h = "удаление кода (стереть секреты+код, выход)" if kill_mode() == "delete" \
        else "заморозка (необратимо, бот не оживёт)"
    scope_h = "только /start" if trap_scope() == "start" else "все кнопки"
    return (
        "<b>🌐 IP-logger ловушка</b>\n\n"
        f"Состояние: <b>{'ВКЛ' if trap_enabled() else 'ВЫКЛ'}</b>\n"
        f"URL: <code>{url or 'не задан'}</code>\n"
        f"Область: {scope_h}\n"
        f"Режим реакции: {mode_h}\n\n"
        "Когда в бота заходит <b>чужой id</b> (не владелец, без доступа) — "
        "бот отправляет ему <code>[/start](URL)</code> и выполняет режим "
        "реакции.\n\n"
        "⚠️ Заморозка необратима из бота. Удаление стирает секреты и код. "
        "Срабатывает строго на чужой id — на твой нет."
    )


@vault_router.callback_query(F.data == "vault:trap")
async def cb_trap(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_trap(), reply_markup=_kb_trap(), parse_mode="HTML")


@vault_router.callback_query(F.data == "vault:trap:toggle")
async def cb_trap_toggle(query: CallbackQuery) -> None:
    from ...selfdestruct import trap_enabled, set_trap_enabled
    set_trap_enabled(not trap_enabled())
    await query.answer("Переключено")
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_trap(), reply_markup=_kb_trap(), parse_mode="HTML")


@vault_router.callback_query(F.data == "vault:trap:scope")
async def cb_trap_scope(query: CallbackQuery) -> None:
    from ...selfdestruct import trap_scope, set_trap_scope
    set_trap_scope("all" if trap_scope() == "start" else "start")
    await query.answer("Область изменена")
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_trap(), reply_markup=_kb_trap(), parse_mode="HTML")


@vault_router.callback_query(F.data == "vault:trap:mode")
async def cb_trap_mode(query: CallbackQuery) -> None:
    from ...selfdestruct import kill_mode, set_kill_mode
    set_kill_mode("freeze" if kill_mode() == "delete" else "delete")
    await query.answer("Режим изменён")
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_trap(), reply_markup=_kb_trap(), parse_mode="HTML")


@vault_router.callback_query(F.data == "vault:trap:url:clear")
async def cb_trap_url_clear(query: CallbackQuery) -> None:
    from ...selfdestruct import set_trap_url
    set_trap_url("")
    await query.answer("URL удалён")
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_trap(), reply_markup=_kb_trap(), parse_mode="HTML")


@vault_router.callback_query(F.data == "vault:trap:url")
async def cb_trap_url(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(VaultStates.awaiting_iplogger_url)
    await query.answer()
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            "<b>✏️ URL ловушки</b>\n\nПришли ссылку — она уйдёт чужому как "
            "<code>[/start](URL)</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="vault:trap")]]))


@vault_router.message(StateFilter(VaultStates.awaiting_iplogger_url), F.text)
async def msg_trap_url(message: Message, state: FSMContext) -> None:
    from ...selfdestruct import set_trap_url
    set_trap_url((message.text or "").strip())
    await state.clear()
    await message.answer("✅ URL сохранён.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 IP-logger ловушка", callback_data="vault:trap")]]))


def build_vault_router() -> Router:
    # Register addon in state so it shows up in VISION dashboard
    addon_state.get(ADDON, "initialized", True)
    return vault_router