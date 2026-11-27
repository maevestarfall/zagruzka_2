"""AI CHECK screens — рендерим внутри Lilush ровно тот UX, что у
standalone Telegram-бота из ``services/ai-tool-inspector/`` (bot.mjs).

Главное меню (6 inline-кнопок в 2 колонки):

    📊 Статистика     🌐 Сервисы
    🤖 Модели         🔗 Ссылки для ИИ
    🔑 Токены         ❓ Помощь
    ➕ Добавить сервер
    « Назад

* Сервера хранятся под ``addons.ai_check.servers`` как список
  ``{id, url, name, addedAt}``.
* Старое поле ``addons.ai_check.server_url`` подхватывается лениво и
  превращается в первую запись (``id = 'default'``).
* Endpoints от inspector берём те же, что и Node-бот:
  ``/api/health``, ``/api/models``, ``/api/tokens``,
  ``/api/tokens/:modelId``, ``/api/summary``,
  ``/api/models/:modelId/tools``,
  ``/api/models/:modelId/tools/:toolId/verify``.

Все экраны admin/owner-only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from aiogram import F, Router
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

API_TIMEOUT = 8.0


# ─── State storage ────────────────────────────────────────────────────────────


class AiCheckStates(StatesGroup):
    awaiting_add_url = State()
    awaiting_add_name = State()


def _migrate_legacy() -> None:
    """Перетащить старый ``server_url`` (одиночная строка) в список."""
    legacy = (addon_state.get("ai_check", "server_url", "") or "").strip().rstrip("/")
    servers = addon_state.get("ai_check", "servers", None)
    if servers is None and legacy:
        addon_state.set_(
            "ai_check",
            "servers",
            [
                {
                    "id": "default",
                    "url": legacy,
                    "name": "Default",
                    "addedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ],
        )
        addon_state.delete("ai_check", "server_url")
    elif servers is None:
        addon_state.set_("ai_check", "servers", [])


def _servers() -> list[dict[str, Any]]:
    _migrate_legacy()
    raw = addon_state.get("ai_check", "servers", []) or []
    # Defensive normalisation.
    out: list[dict[str, Any]] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip().rstrip("/")
        if not url:
            continue
        out.append(
            {
                "id": str(s.get("id") or f"svc_{int(time.time() * 1000)}"),
                "url": url,
                "name": s.get("name") or url,
                "addedAt": s.get("addedAt") or "",
            }
        )
    return out


def _save_servers(servers: list[dict[str, Any]]) -> None:
    addon_state.set_("ai_check", "servers", servers)


def _add_server(url: str, name: str) -> tuple[bool, dict[str, Any]]:
    url = url.strip().rstrip("/")
    servers = _servers()
    existing = next((s for s in servers if s["url"] == url), None)
    if existing:
        return False, existing
    svc = {
        "id": f"svc_{int(time.time() * 1000)}",
        "url": url,
        "name": name.strip() or url,
        "addedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    servers.append(svc)
    _save_servers(servers)
    return True, svc


def _remove_server(svc_id: str) -> bool:
    servers = _servers()
    new = [s for s in servers if s["id"] != svc_id]
    if len(new) == len(servers):
        return False
    _save_servers(new)
    return True


# ─── HTML helpers ─────────────────────────────────────────────────────────────


def _html(text: Any) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M")
    except Exception:  # noqa: BLE001
        return iso or "—"


def _bar(percent: int, width: int = 10) -> str:
    filled = max(0, min(width, percent // (100 // width)))
    return "█" * filled + "░" * (width - filled)


# ─── Owner gate ───────────────────────────────────────────────────────────────


async def _is_owner_query(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]
    return _is_owner(query.from_user.id if query.from_user else 0)


async def _is_owner_msg(message: Message) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]
    return _is_owner(message.from_user.id if message.from_user else 0)


# ─── HTTP ─────────────────────────────────────────────────────────────────────


async def _fetch(svc_url: str, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
    url = svc_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.request(method.upper(), url, **kwargs)
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            return resp.status_code, body
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


async def _check_health(svc: dict[str, Any]) -> dict[str, Any]:
    status, body = await _fetch(svc["url"], "GET", "/api/health")
    if status == 200 and isinstance(body, dict):
        return {"online": True, "health": body}
    return {"online": False, "health": None, "error": str(body) if status == 0 else f"HTTP {status}"}


async def _all_with_status() -> list[dict[str, Any]]:
    servers = _servers()
    results = await asyncio.gather(*(_check_health(s) for s in servers), return_exceptions=True)
    out: list[dict[str, Any]] = []
    for svc, res in zip(servers, results):
        if isinstance(res, Exception):
            out.append({**svc, "online": False, "health": None, "error": str(res)})
        else:
            out.append({**svc, **res})
    return out


async def _first_online() -> dict[str, Any] | None:
    for s in await _all_with_status():
        if s.get("online"):
            return s
    return None


# ─── Keyboards ────────────────────────────────────────────────────────────────


def _kb_home(has_servers: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_servers:
        rows.extend(
            [
                [
                    InlineKeyboardButton(text="📊 Статистика", callback_data="aicheck:stats"),
                    InlineKeyboardButton(text="🌐 Сервисы", callback_data="aicheck:services"),
                ],
                [
                    InlineKeyboardButton(text="🤖 Модели", callback_data="aicheck:models"),
                    InlineKeyboardButton(text="🔗 Ссылки для ИИ", callback_data="aicheck:links"),
                ],
                [
                    InlineKeyboardButton(text="🔑 Токены", callback_data="aicheck:tokens"),
                    InlineKeyboardButton(text="❓ Помощь", callback_data="aicheck:help"),
                ],
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить сервер", callback_data="aicheck:add")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="main:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_back_home(extra: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="aicheck:refresh"),
            InlineKeyboardButton(text="« Назад", callback_data="aicheck:open"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── Renderers ────────────────────────────────────────────────────────────────


def _render_home(servers: list[dict[str, Any]]) -> str:
    if not servers:
        return (
            "<b>🛡 AI CHECK</b>\n\n"
            "Связка с <i>ai-tool-inspector</i>: проверка моделей и инструментов, "
            "TrustScore, токены для прокидывания в ИИ.\n\n"
            "<b>Серверы пока не подключены.</b> Подними inspector "
            "(<code>services/ai-tool-inspector/</code>, <code>node server.mjs</code>) и нажми "
            "<b>➕ Добавить сервер</b>."
        )
    return (
        "<b>🛡 AI CHECK</b>\n\n"
        "Управляю системой верификации инструментов ИИ-агентов.\n\n"
        "<b>Что умею:</b>\n"
        "📊 Статистика по всем серверам\n"
        "🌐 Подключённые сервера\n"
        "🤖 Модели + TrustScore\n"
        "🔗 Персональные ссылки/токены для каждой ИИ\n"
        "🔑 Активные токены\n\n"
        f"Подключено серверов: <b>{len(servers)}</b>\n"
        "Выбери действие ↓"
    )


def _render_stats(services: list[dict[str, Any]]) -> str:
    if not services:
        return "<b>📊 Статистика</b>\n\nНет подключённых серверов. Добавь через <b>➕ Добавить сервер</b>."

    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    lines = ["<b>📊 Статистика AI Tool Inspector</b>", f"🕐 {now}", ""]
    total_v = total_f = total_t = online = 0
    for svc in services:
        if not svc.get("online"):
            lines.append(f"❌ <b>{_html(svc['name'])}</b>\n<i>Недоступен</i>\n")
            continue
        online += 1
        h = svc["health"] or {}
        lines.append(f"✅ <b>{_html(svc['name'])}</b>")
        lines.append(f"🔗 <code>{_html(svc['url'])}</code>")
        lines.append(f"⏱ Uptime: {int((h.get('uptime') or 0) // 60)} мин")
        lines.append(f"📦 Моделей: {h.get('models', '?')} | Инструментов: {h.get('totalTools', '?')}")
        for m in (h.get("stats") or [])[:10]:
            v = m.get("verified", 0)
            f_ = m.get("failed", 0)
            t = m.get("total", 0)
            if v + f_ > 0:
                bar = "✅" * min(v, 5) + ("❌" * min(f_, 3) if f_ else "")
            else:
                bar = "⬜"
            lines.append(f"  {bar} {_html(m.get('name', '?'))}: {v}✅ {f_}❌ / {t}")
            total_v += v
            total_f += f_
            total_t += t
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append(f"🌐 Серверов онлайн: {online}/{len(services)}")
    lines.append(f"📊 Итого: {total_v}✅ {total_f}❌ из {total_t} инструментов")
    if total_v + total_f > 0:
        score = round(total_v / (total_v + total_f) * 100)
        lines.append(f"📈 Достоверность: [{_bar(score)}] {score}%")
    return "\n".join(lines)


def _render_services(services: list[dict[str, Any]]) -> tuple[str, list[list[InlineKeyboardButton]]]:
    lines = ["<b>🌐 Подключённые серверы</b>", ""]
    rm_buttons: list[list[InlineKeyboardButton]] = []
    if not services:
        lines.append("Пусто. Нажми <b>➕ Добавить сервер</b>.")
    for svc in services:
        dot = "🟢" if svc.get("online") else "🔴"
        lines.append(f"{dot} <b>{_html(svc['name'])}</b>")
        lines.append(f"<code>{_html(svc['url'])}</code>")
        lines.append(f"ID: <code>{_html(svc['id'])}</code>")
        if svc.get("online") and isinstance(svc.get("health"), dict):
            h = svc["health"]
            lines.append(
                f"✅ Онлайн | {h.get('models', '?')} моделей | uptime {int((h.get('uptime') or 0) // 60)} мин"
            )
        else:
            err = svc.get("error") or "Недоступен"
            lines.append(f"❌ {_html(err)}")
        if svc.get("addedAt"):
            lines.append(f"Добавлен: {_fmt_dt(svc['addedAt'])}")
        lines.append("")
        rm_buttons.append(
            [InlineKeyboardButton(text=f"🗑 Удалить {svc['name']}", callback_data=f"aicheck:rm:{svc['id']}")]
        )
    lines.append("━━━━━━━━━━━━━━━━━")
    online = sum(1 for s in services if s.get("online"))
    lines.append(f"Всего: {len(services)} серверов | Онлайн: {online}")
    return "\n".join(lines), rm_buttons


def _render_models(svc: dict[str, Any], payload: Any) -> tuple[str, list[list[InlineKeyboardButton]]]:
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or not models:
        return f"<b>🤖 ИИ-модели</b> ({_html(svc['name'])})\n\nПусто.", []
    lines = [f"<b>🤖 ИИ-модели</b> ({_html(svc['name'])})", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for m in models:
        st = m.get("stats") or {}
        v, f_, u = st.get("verified", 0), st.get("failed", 0), st.get("unchecked", 0)
        score = (
            f"{round(v / (v + f_) * 100)}%" if (v + f_) > 0 else "—"
        )
        lines.append(f"<b>{_html(m.get('name', '?'))}</b> <i>({_html(m.get('provider', '?'))})</i>")
        lines.append(f"  ✅ {v}  ❌ {f_}  ⬜ {u}  |  {score} достоверность")
        lines.append(f"  ID: <code>{_html(m.get('id', '?'))}</code>")
        lines.append("")
        mid = m.get("id")
        if mid:
            buttons.append(
                [
                    InlineKeyboardButton(text=f"🔗 Ссылка {m.get('name', mid)}", callback_data=f"aicheck:link:{mid}"),
                    InlineKeyboardButton(text="✅ Проверить все", callback_data=f"aicheck:vall:{mid}"),
                ]
            )
    return "\n".join(lines), buttons


def _render_token(svc: dict[str, Any], payload: dict[str, Any]) -> str:
    name = payload.get("modelName") or payload.get("modelId") or "?"
    token = payload.get("token", "?")
    probe = payload.get("probeUrl", "?")
    verify = payload.get("verifyUrl", "?")
    return (
        f"🔗 <b>Персональная ссылка для {_html(name)}</b>\n"
        f"<i>{_html(svc['name'])}</i>\n\n"
        f"<b>Токен:</b> <code>{_html(token)}</code>\n\n"
        f"<b>📥 Получить задания:</b>\n<code>GET {_html(probe)}</code>\n\n"
        f"<b>📤 Отправить результат:</b>\n<code>POST {_html(verify)}</code>\n\n"
        f"<b>Тело запроса:</b>\n"
        f"<code>{{\n  \"toolId\": \"bash\",\n  \"result\": \"verified\",\n  \"message\": \"Протестировано\"\n}}</code>\n\n"
        f"<i>Скопируй это в промпт — ИИ сам дёрнет API.</i>"
    )


def _render_tokens(svc: dict[str, Any], payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"<b>🔑 Токены</b>\n\n<code>{_html(payload)}</code>"
    tokens = payload.get("tokens") or []
    if not tokens:
        return (
            f"<b>🔑 Активные токены</b> ({_html(svc['name'])})\n\n"
            "Токенов нет. Сгенерируй через 🔗 «Ссылки для ИИ»."
        )
    lines = [f"<b>🔑 Активные токены</b> ({_html(svc['name'])})", ""]
    for t in tokens[:15]:
        lines.append(f"<b>{_html(t.get('modelId', '?'))}</b>")
        lines.append(f"Токен: <code>{_html(t.get('token', '?'))}</code>")
        created = t.get("createdAt", "")
        if created:
            lines.append(f"Создан: {_fmt_dt(created)}")
        lines.append(f"Probe URL: <code>{_html(t.get('probeUrl', '?'))}</code>")
        lines.append("")
    if len(tokens) > 15:
        lines.append(f"… ещё {len(tokens) - 15}")
    return "\n".join(lines)


_HELP_TEXT = (
    "<b>📖 Справка — AI CHECK</b>\n\n"
    "<b>Что это:</b> панель управления связкой с <i>ai-tool-inspector</i>. "
    "Один inspector — один Telegram-бот сам по себе; здесь же он встроен "
    "прямо в Lilush, и можно подключать несколько серверов сразу.\n\n"
    "<b>Кнопки:</b>\n"
    "📊 <b>Статистика</b> — сводка по всем серверам с прогресс-баром "
    "достоверности (TrustScore).\n"
    "🌐 <b>Сервисы</b> — список подключённых серверов, статус, кнопки "
    "удаления.\n"
    "🤖 <b>Модели</b> — список ИИ-моделей у первого онлайн-сервера: "
    "TrustScore, токен, массовая проверка.\n"
    "🔗 <b>Ссылки для ИИ</b> — выбрать модель → получить персональный "
    "<code>probeUrl</code> + <code>verifyUrl</code>.\n"
    "🔑 <b>Токены</b> — все активные токены первого онлайн-сервера.\n"
    "➕ <b>Добавить сервер</b> — подключить inspector по URL "
    "(например, <code>https://my-app.onrender.com</code>).\n\n"
    "<b>Запуск inspector:</b>\n"
    "<code>cd services/ai-tool-inspector\n"
    "cp env.example .env  # TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID\n"
    "pnpm install\n"
    "node server.mjs      # :3001</code>\n\n"
    "Потом сюда → <b>➕ Добавить сервер</b> → URL → Готово."
)


# ─── Screen helpers ───────────────────────────────────────────────────────────


async def _send_or_edit(query: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    if query.message is None:
        await query.answer()
        return
    try:
        await query.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 — длинный текст / not modified
        await query.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await query.answer()


async def show_screen(message_or_query) -> None:
    """Главный экран AI CHECK."""
    servers = _servers()
    body = _render_home(servers)
    kb = _kb_home(bool(servers))
    if isinstance(message_or_query, CallbackQuery):
        await _send_or_edit(message_or_query, body, kb)
    else:
        await message_or_query.answer(body, reply_markup=kb, disable_web_page_preview=True)


# ─── Router ───────────────────────────────────────────────────────────────────


def build_ai_check_router() -> Router:
    router = Router(name="ai_check")

    # Entry points -------------------------------------------------------------

    @router.callback_query(F.data == "main:ai_check")
    async def from_main_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await show_screen(query)

    @router.callback_query(F.data == "aicheck:open")
    async def open_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await show_screen(query)

    @router.callback_query(F.data == "aicheck:refresh")
    async def refresh_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await show_screen(query)

    # Help ---------------------------------------------------------------------

    @router.callback_query(F.data == "aicheck:help")
    async def help_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await _send_or_edit(query, _HELP_TEXT, _kb_back_home())

    # Stats --------------------------------------------------------------------

    @router.callback_query(F.data == "aicheck:stats")
    async def stats_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        if query.message is not None:
            try:
                await query.message.edit_text(
                    "⏳ Собираю статистику со всех серверов…",
                    reply_markup=_kb_back_home(),
                )
            except Exception:  # noqa: BLE001
                pass
        services = await _all_with_status()
        await _send_or_edit(query, _render_stats(services), _kb_back_home())

    # Services -----------------------------------------------------------------

    @router.callback_query(F.data == "aicheck:services")
    async def services_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        services = await _all_with_status()
        body, rm = _render_services(services)
        extra = list(rm)
        extra.append(
            [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="aicheck:add")]
        )
        await _send_or_edit(query, body, _kb_back_home(extra=extra))

    @router.callback_query(F.data.startswith("aicheck:rm:"))
    async def rm_svc_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        svc_id = query.data.split(":", 2)[2]
        removed = _remove_server(svc_id)
        await query.answer("✅ Удалено" if removed else "❌ Не найдено")
        services = await _all_with_status()
        body, rm = _render_services(services)
        extra = list(rm)
        extra.append(
            [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="aicheck:add")]
        )
        if query.message is not None:
            try:
                await query.message.edit_text(body, reply_markup=_kb_back_home(extra=extra), disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                await query.message.answer(body, reply_markup=_kb_back_home(extra=extra), disable_web_page_preview=True)

    # Add server ---------------------------------------------------------------

    @router.callback_query(F.data == "aicheck:add")
    async def add_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(AiCheckStates.awaiting_add_url)
        await state.update_data(_add_name=None)
        if query.message is not None:
            await query.message.answer(
                "Пришли URL inspector одним сообщением.\n"
                "Пример: <code>https://my-app.onrender.com</code> или <code>http://127.0.0.1:3001</code>\n\n"
                "Можно сразу с названием: <code>https://my-app.onrender.com Render</code>",
                disable_web_page_preview=True,
            )
        await query.answer()

    @router.message(AiCheckStates.awaiting_add_url)
    async def add_url_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пустой URL — попробуй ещё раз.")
            return
        parts = text.split(maxsplit=1)
        url = parts[0]
        name = parts[1] if len(parts) > 1 else url
        if not (url.startswith("http://") or url.startswith("https://")):
            await message.answer("❌ URL должен начинаться с http:// или https://")
            return
        probe = await message.answer(f"⏳ Проверяю сервер <code>{_html(url)}</code>…")
        status, body = await _fetch(url, "GET", "/api/health")
        added, svc = _add_server(url, name)
        if status == 200 and isinstance(body, dict):
            reply = (
                f"{'✅ Сервер добавлен' if added else '⚠️ Сервер уже существует'}\n\n"
                f"<b>{_html(svc['name'])}</b>\n"
                f"🔗 <code>{_html(svc['url'])}</code>\n"
                f"📦 Моделей: {body.get('models', '?')} | Инструментов: {body.get('totalTools', '?')}\n"
                f"⏱ Uptime: {int((body.get('uptime') or 0) // 60)} мин\n"
                f"ID: <code>{_html(svc['id'])}</code>"
            )
        else:
            reply = (
                f"⚠️ Сервер недоступен, но добавлен в список (возможно спит на Render).\n"
                f"<code>{_html(url)}</code>\nID: <code>{_html(svc['id'])}</code>"
            )
        try:
            await probe.edit_text(reply, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await message.answer(reply, disable_web_page_preview=True)
        await state.clear()
        await show_screen(message)

    # Models -------------------------------------------------------------------

    @router.callback_query(F.data == "aicheck:models")
    async def models_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        svc = await _first_online()
        if not svc:
            await _send_or_edit(
                query,
                "<b>🤖 Модели</b>\n\nНет доступных серверов. Добавь через ➕.",
                _kb_back_home(),
            )
            return
        status, payload = await _fetch(svc["url"], "GET", "/api/models")
        if status != 200:
            body = f"<b>🤖 Модели</b>\n\n❌ Ошибка: <code>{_html(payload)}</code>"
            await _send_or_edit(query, body, _kb_back_home())
            return
        body, btns = _render_models(svc, payload)
        await _send_or_edit(query, body, _kb_back_home(extra=btns))

    # Links menu ---------------------------------------------------------------

    @router.callback_query(F.data == "aicheck:links")
    async def links_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        svc = await _first_online()
        if not svc:
            await _send_or_edit(
                query,
                "<b>🔗 Ссылки для ИИ</b>\n\nНет доступных серверов. Добавь через ➕.",
                _kb_back_home(),
            )
            return
        status, payload = await _fetch(svc["url"], "GET", "/api/models")
        models = payload.get("models") if isinstance(payload, dict) else None
        if status != 200 or not isinstance(models, list) or not models:
            await _send_or_edit(
                query,
                f"<b>🔗 Ссылки для ИИ</b>\n\n❌ Не удалось получить модели: <code>{_html(payload)}</code>",
                _kb_back_home(),
            )
            return
        btns: list[list[InlineKeyboardButton]] = []
        for m in models:
            mid = m.get("id")
            if not mid:
                continue
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"{m.get('name', mid)} ({m.get('provider', '?')})",
                        callback_data=f"aicheck:link:{mid}",
                    )
                ]
            )
        body = (
            f"<b>🔗 Выбери ИИ для генерации ссылки</b>\n"
            f"<i>Сервер: {_html(svc['name'])}</i>\n"
            f"<i>Каждый получит уникальный токен доступа.</i>"
        )
        await _send_or_edit(query, body, _kb_back_home(extra=btns))

    @router.callback_query(F.data.startswith("aicheck:link:"))
    async def link_one_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        model_id = query.data.split(":", 2)[2]
        svc = await _first_online()
        if not svc:
            await _send_or_edit(query, "❌ Нет доступных серверов.", _kb_back_home())
            return
        if query.message is not None:
            try:
                await query.message.edit_text("⏳ Генерирую токен…", reply_markup=_kb_back_home())
            except Exception:  # noqa: BLE001
                pass
        status, payload = await _fetch(svc["url"], "POST", f"/api/tokens/{model_id}")
        if status != 200 or not isinstance(payload, dict):
            body = f"<b>🔗 Ссылка</b>\n\n❌ Ошибка: <code>{_html(payload)}</code>"
            await _send_or_edit(query, body, _kb_back_home())
            return
        body = _render_token(svc, payload)
        extra = [
            [InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"aicheck:link:{model_id}")],
        ]
        await _send_or_edit(query, body, _kb_back_home(extra=extra))

    # Tokens list --------------------------------------------------------------

    @router.callback_query(F.data == "aicheck:tokens")
    async def tokens_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        svc = await _first_online()
        if not svc:
            await _send_or_edit(query, "❌ Нет доступных серверов.", _kb_back_home())
            return
        status, payload = await _fetch(svc["url"], "GET", "/api/tokens")
        if status != 200:
            body = f"<b>🔑 Токены</b>\n\n❌ Ошибка: <code>{_html(payload)}</code>"
            await _send_or_edit(query, body, _kb_back_home())
            return
        await _send_or_edit(query, _render_tokens(svc, payload), _kb_back_home())

    # Verify all ---------------------------------------------------------------

    @router.callback_query(F.data.startswith("aicheck:vall:"))
    async def verify_all_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        model_id = query.data.split(":", 2)[2]
        svc = await _first_online()
        if not svc:
            await _send_or_edit(query, "❌ Нет доступных серверов.", _kb_back_home())
            return
        await query.answer("⏳ Запускаю проверку…")
        if query.message is not None:
            try:
                await query.message.edit_text(
                    f"⏳ Проверяю инструменты для <b>{_html(model_id)}</b>…",
                    reply_markup=_kb_back_home(),
                )
            except Exception:  # noqa: BLE001
                pass
        status, payload = await _fetch(svc["url"], "GET", f"/api/models/{model_id}/tools")
        if status != 200 or not isinstance(payload, dict):
            body = f"<b>✅ Verify all</b>\n\n❌ Не получил список инструментов: <code>{_html(payload)}</code>"
            await _send_or_edit(query, body, _kb_back_home())
            return
        tools = payload.get("tools") or []
        verified = failed = 0
        for tool in tools:
            if tool.get("verificationStatus") != "unchecked":
                verified += 1
                continue
            result = "failed" if tool.get("reallyExists") is False else "verified"
            st, _ = await _fetch(
                svc["url"],
                "POST",
                f"/api/models/{model_id}/tools/{tool.get('id')}/verify",
                json={
                    "result": result,
                    "message": "Запущено из Lilush AI CHECK",
                    "source": "lilush-ai-check",
                },
            )
            if st == 200 and result == "verified":
                verified += 1
            else:
                failed += 1
            await asyncio.sleep(0.3)
        body = (
            f"✅ <b>Проверка завершена</b> для <b>{_html(model_id)}</b>\n\n"
            f"✅ Подтверждено: {verified}\n"
            f"❌ Не существует: {failed}\n"
            f"Всего: {len(tools)}"
        )
        await _send_or_edit(query, body, _kb_back_home())

    return router
