"""
telegram_bot.py — owner control panel for the multi-door debate server.

What it does
- Inline-button menu: [🧠 Мозг] [🚪 Двери].
- Doors: live list (x/y joined · status), [➕] to generate a door + its link,
  tap a door to see who's debating, subscribe to live events.
- Owner-claim: the FIRST person to /start becomes the owner (stored on disk);
  everyone else is silently ignored. OWNER_TG_ID is an optional bootstrap.
- 🧠 Мозг: optional in-bot LLM chat (OpenRouter).
- Live push: while subscribed to a door, new debate events are pushed to you.
- Self-ping: pings the server's /healthz so Render Free doesn't fall asleep.

The bot never participates in debates itself.

Env vars
  TELEGRAM_BOT_TOKEN  — from BotFather
  OWNER_TG_ID         — optional: pre-set owner (else first /start claims it)
  DEBATE_TOKEN        — shared secret of the debate server
  DEBATE_BASE_URL     — internal server URL (default http://127.0.0.1:$PORT)
  PUBLIC_BASE_URL     — public URL put into debater links
  STATE_DIR           — where owner_id is persisted (same disk as the server)
  SELF_PING           — "1" (default) to keep Render awake, "0" to disable
  PING_INTERVAL       — seconds between pings (default 600)
  BRAIN_API_KEY / OPENROUTER_API_KEY — key for the 🧠 Мозг LLM chat
  BRAIN_MODEL         — model for 🧠 Мозг (default openai/gpt-4o-mini)
"""

import html
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("debate-tg-bot")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_ENV_OWNER = int(os.environ.get("OWNER_TG_ID", "0") or "0")
DEBATE_TOKEN = os.environ.get("DEBATE_TOKEN", "")
_PORT = os.environ.get("PORT", "8080")
DEBATE_BASE_URL = os.environ.get("DEBATE_BASE_URL", f"http://127.0.0.1:{_PORT}").rstrip("/")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or DEBATE_BASE_URL
STATE_DIR = Path(os.environ.get("STATE_DIR", "./data"))
OWNER_FILE = STATE_DIR / "bot_owner.json"

SELF_PING = os.environ.get("SELF_PING", "1") != "0"
PING_INTERVAL = int(os.environ.get("PING_INTERVAL", "600"))

BRAIN_API_KEY = os.environ.get("BRAIN_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "openai/gpt-4o-mini")
BRAIN_BASE = os.environ.get("BRAIN_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")

MAX_TZ_BYTES = 200 * 1024

# ─── runtime state (in-memory) ──────────────────────────────────────────────
# door_id -> last pushed event id (presence = subscribed)
_subs: dict[int, int] = {}
_owner_chat_id: int | None = None
_brain_on: dict[int, bool] = {}
_brain_hist: dict[int, list] = {}


# ─── owner-claim storage ────────────────────────────────────────────────────

def _read_owner() -> int | None:
    if _ENV_OWNER:
        return _ENV_OWNER
    try:
        return int(json.loads(OWNER_FILE.read_text(encoding="utf-8"))["owner_id"])
    except Exception:
        return None


def _claim_owner(user_id: int) -> bool:
    """Claim ownership if unclaimed. Returns True if THIS user is now owner."""
    cur = _read_owner()
    if cur is not None:
        return cur == int(user_id)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        OWNER_FILE.write_text(json.dumps({"owner_id": int(user_id),
                                          "claimed_at": time.time()}), encoding="utf-8")
    except Exception as e:
        log.warning("owner claim write failed: %s", e)
        return False
    log.info("owner claimed by %s", user_id)
    return True


def _is_owner(update: Update) -> bool:
    owner = _read_owner()
    u = update.effective_user
    return bool(u) and owner is not None and u.id == owner


# ─── helpers ────────────────────────────────────────────────────────────────

def _esc(x: Any) -> str:
    if x is None:
        return "—"
    return html.escape(str(x), quote=False)


def _door_link(door_id: int) -> str:
    return f"{PUBLIC_BASE_URL}/door/{door_id}"


async def _fetch(path: str, params: dict | None = None) -> dict | None:
    if not DEBATE_TOKEN:
        return None
    p = {"token": DEBATE_TOKEN, **(params or {})}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{DEBATE_BASE_URL}{path}", params=p)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.warning("fetch %s failed: %s", path, e)
        return None


async def _send(method: str, path: str, json_body: dict | None = None) -> tuple[bool, Any]:
    if not DEBATE_TOKEN:
        return False, "DEBATE_TOKEN не настроен"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.request(method, f"{DEBATE_BASE_URL}{path}",
                                params={"token": DEBATE_TOKEN}, json=json_body)
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:
                    pass
                return False, f"{r.status_code}: {detail}"
            return True, (r.json() if r.content else {})
    except Exception as e:
        log.warning("%s %s failed: %s", method, path, e)
        return False, str(e)


# ─── keyboards ──────────────────────────────────────────────────────────────

def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Мозг", callback_data="menu:brain"),
         InlineKeyboardButton("🚪 Двери", callback_data="menu:doors")],
    ])


def _kb_claim() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Запустить и стать владельцем", callback_data="claim")],
    ])


def _render_doors_kb(data: dict | None) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    if not data:
        text = "❌ Сервер недоступен или DEBATE_TOKEN не настроен."
        rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="menu:doors")])
        rows.append([InlineKeyboardButton("← Меню", callback_data="menu:main")])
        return text, InlineKeyboardMarkup(rows)
    doors = data.get("doors") or []
    total = data.get("total_doors", len(doors))
    cap = data.get("max_doors", 20)
    lines = [f"🚪 <b>Двери: {total}/{cap}</b>"]
    if not doors:
        lines.append("\nПока пусто. Нажми ➕ — создам дверь и выдам ссылку.")
    for d in doors:
        did = d.get("door_id")
        title = _esc(d.get("title") or f"Door {did}")
        conn = d.get("connections", 0)
        mx = d.get("max_slots", 5)
        active = "🟢" if d.get("session_active") else "💤"
        sub = "🔔" if did in _subs else ""
        lines.append(f"{active} <b>#{did}</b> {title} · {conn}/{mx} {sub}")
        rows.append([InlineKeyboardButton(f"{active} #{did} {d.get('title') or did} · {conn}/{mx}",
                                          callback_data=f"door:view:{did}")])
    rows.append([InlineKeyboardButton("➕ Создать дверь", callback_data="door:new"),
                 InlineKeyboardButton("🔄 Обновить", callback_data="menu:doors")])
    rows.append([InlineKeyboardButton("← Меню", callback_data="menu:main")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _render_door_view(did: int, st: dict | None) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    if not st:
        rows.append([InlineKeyboardButton("← Двери", callback_data="menu:doors")])
        return f"❌ Дверь #{did} недоступна.", InlineKeyboardMarkup(rows)
    title = st.get("door_title") or f"Door {did}"
    lines = [f"🚪 <b>#{did} · {_esc(title)}</b>",
             f"🔗 <code>{_esc(_door_link(did))}</code>"]
    if not st.get("session_active"):
        lines.append("\n💤 Сессии нет. Запусти дебат: пришли файл-тему с подписью\n"
                     f"<code>/begin {did} &lt;кол-во&gt; &lt;тема&gt;</code>")
    else:
        req = st.get("required_debaters")
        slots = st.get("slots") or []
        occ = [s for s in slots if s.get("occupied")]
        started = st.get("started")
        gate = "🟢 идут дебаты" if started else f"⏳ ждём набора {len(occ)}/{req}"
        lines.append(f"🎙 <b>{_esc(st.get('title'))}</b> · {gate}")
        lines.append(f"\n👥 <b>Участники ({len(occ)}/{req}):</b>")
        for s in occ:
            em = {"waiting": "⏳", "thinking": "🤔", "spoke": "💬",
                  "offline": "🔴"}.get(s.get("status"), "•")
            lines.append(f"  {em} <b>{_esc(s.get('name'))}</b> — {_esc(s.get('status'))}")
        agenda = st.get("agenda") or []
        cur = st.get("current", 0)
        lines.append(f"\n📋 <b>Пункты ({cur}/{len(agenda)}):</b>")
        for i, p in enumerate(agenda):
            mark = "✅" if p.get("status") == "decided" else ("👉" if i == cur else "•")
            who = ""
            if p.get("status") != "decided":
                if p.get("turn"):
                    who = f" · ход: {_esc(p['turn'])}"
                elif p.get("assignee"):
                    who = f" · ⚡{_esc(p['assignee'])}"
            lines.append(f"  {mark} {i+1}. {_esc((p.get('text') or '')[:60])}{who}")
            if p.get("decision"):
                lines.append(f"      → <i>{_esc(p['decision'][:140])}</i>")
    subbed = did in _subs
    rows.append([
        InlineKeyboardButton("🔕 Отписаться" if subbed else "🔔 Живые события",
                             callback_data=f"door:{'unsub' if subbed else 'sub'}:{did}"),
        InlineKeyboardButton("🔄", callback_data=f"door:view:{did}"),
    ])
    rows.append([InlineKeyboardButton("✕ Удалить дверь", callback_data=f"door:del:{did}")])
    rows.append([InlineKeyboardButton("← Двери", callback_data="menu:doors")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _safe_edit(update: Update, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    q = update.callback_query
    try:
        if q:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
    except Exception as e:
        log.debug("edit failed: %s", e)
    if update.effective_chat:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ─── commands ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _owner_chat_id
    u = update.effective_user
    if not u:
        return
    owner = _read_owner()
    if owner is None:
        await update.effective_chat.send_message(
            "👋 <b>Debate-II</b>\n\nЭтот бот ещё не присвоен. "
            "Первый, кто нажмёт кнопку ниже, станет владельцем — остальных бот не пустит.",
            parse_mode=ParseMode.HTML, reply_markup=_kb_claim())
        return
    if u.id != owner:
        return  # not the owner — silently ignore
    _owner_chat_id = update.effective_chat.id
    await update.effective_chat.send_message(
        "👋 <b>Debate-II — пульт</b>\nВыбери раздел:",
        parse_mode=ParseMode.HTML, reply_markup=_kb_main())


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await update.effective_chat.send_message("Меню:", reply_markup=_kb_main())


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await _safe_edit(
        update,
        "<b>Debate-II — справка</b>\n\n"
        "Меню: 🧠 Мозг (ЛЛМ-чат) · 🚪 Двери.\n\n"
        "<b>Двери:</b> ➕ создаёт дверь и ссылку. Тык в дверь — кто зашёл, чьи "
        "ходы, можно подписаться 🔔 на живые события дебата.\n\n"
        "<b>Запуск дебата:</b> пришли файл-тему (.txt/.md) с подписью\n"
        "<code>/begin &lt;id&gt; &lt;кол-во&gt; &lt;тема&gt;</code>. Дебатёры не начнут, "
        "пока не наберётся указанное число.\n\n"
        "<b>Формат:</b> эстафета — ИИ ходят по очереди по пункту и передают ход; "
        "свободный ИИ берёт следующий пункт параллельно. Один дебатёр = одна дверь, "
        "двери не пересекаются, у каждого свой контейнер с логом и напоминалками.\n\n"
        "Команды: /menu /doors /door /newdoor /deldoor /begin /end /url /help",
    )


async def cmd_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await update.effective_chat.send_message(
        f"🔗 Публичный: <code>{_esc(PUBLIC_BASE_URL)}</code>\n"
        f"UI: <code>{_esc(PUBLIC_BASE_URL)}/</code>\n"
        f"Health: <code>{_esc(DEBATE_BASE_URL)}/healthz</code>",
        parse_mode=ParseMode.HTML)


async def cmd_doors(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    data = await _fetch("/doors")
    text, kb = _render_doors_kb(data)
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_door(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.effective_chat.send_message("Использование: /door <id>")
        return
    did = int(args[0])
    st = await _fetch(f"/door/{did}/state")
    text, kb = _render_door_view(did, st)
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_newdoor(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    title = " ".join(ctx.args).strip() if ctx.args else ""
    ok, res = await _send("POST", "/doors", {"title": title})
    if not ok:
        await update.effective_chat.send_message(f"❌ {res}")
        return
    did = res.get("door_id")
    await update.effective_chat.send_message(
        f"✅ Дверь #{did} создана.\n🔗 <code>{_esc(_door_link(did))}</code>",
        parse_mode=ParseMode.HTML)


async def cmd_deldoor(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.effective_chat.send_message("Использование: /deldoor <id>")
        return
    did = int(args[0])
    ok, res = await _send("DELETE", f"/doors/{did}")
    _subs.pop(did, None)
    await update.effective_chat.send_message(
        f"🗑 Дверь #{did} удалена." if ok else f"❌ {res}")


async def cmd_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.effective_chat.send_message("Использование: /end <id>")
        return
    did = int(args[0])
    ok, res = await _send("POST", f"/door/{did}/session/end")
    if not ok:
        await update.effective_chat.send_message(f"❌ {res}")
        return
    await update.effective_chat.send_message(
        f"🏁 Сессия двери #{did} завершена. Архив: <code>{_esc(res.get('archived_session_id'))}</code>",
        parse_mode=ParseMode.HTML)


# ─── /begin (start a debate with an attached ТЗ file) ────────────────────────

async def _read_attached_tz(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    msg = update.effective_message
    if not msg:
        return ""
    doc = msg.document or (msg.reply_to_message.document if msg.reply_to_message else None)
    if doc:
        if doc.file_size and doc.file_size > MAX_TZ_BYTES:
            return ""
        try:
            f = await ctx.bot.get_file(doc.file_id)
            ba = await f.download_as_bytearray()
            return bytes(ba)[:MAX_TZ_BYTES].decode("utf-8", errors="replace")
        except Exception as e:
            log.warning("tz download failed: %s", e)
            return ""
    if msg.reply_to_message and msg.reply_to_message.text:
        return msg.reply_to_message.text
    return ""


async def _do_begin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tokens: list[str]) -> None:
    if len(tokens) < 3 or not tokens[0].isdigit() or not tokens[1].isdigit():
        await update.effective_chat.send_message(
            "Использование: <code>/begin &lt;id&gt; &lt;кол-во&gt; &lt;тема&gt;</code>\n"
            "Прикрепи .txt/.md как ТЗ. Пример: <code>/begin 1 3 Улучшаем архитектуру</code>",
            parse_mode=ParseMode.HTML)
        return
    did, required = int(tokens[0]), int(tokens[1])
    title = " ".join(tokens[2:]).strip()
    tz_text = await _read_attached_tz(update, ctx)
    ok, res = await _send("POST", f"/door/{did}/session/start",
                          {"title": title, "required_debaters": required, "tz_text": tz_text})
    if not ok:
        await update.effective_chat.send_message(f"❌ {res}")
        return
    note = f"ТЗ {len(tz_text)} симв." if tz_text else "без файла ТЗ"
    await update.effective_chat.send_message(
        f"🎙 Дебат запущен в #{did}: <b>{_esc(title)}</b>\n"
        f"Нужно дебатёров: <b>{required}</b> · {note}\n"
        f"🔗 <code>{_esc(_door_link(did))}</code>\n"
        f"Подпишись на живые события: /door {did}",
        parse_mode=ParseMode.HTML)


async def cmd_begin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await _do_begin(update, ctx, list(ctx.args) if ctx.args else [])


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    cap = (update.effective_message.caption or "").strip()
    if not cap.startswith("/begin"):
        return
    await _do_begin(update, ctx, cap.split()[1:])


# ─── 🧠 Мозг (LLM chat) ──────────────────────────────────────────────────────

async def _brain_reply(chat_id: int, text: str) -> str:
    if not BRAIN_API_KEY:
        return ("🧠 ЛЛМ не настроен. Задай переменную окружения "
                "<code>BRAIN_API_KEY</code> (или <code>OPENROUTER_API_KEY</code>).")
    hist = _brain_hist.setdefault(chat_id, [])
    hist.append({"role": "user", "content": text})
    hist[:] = hist[-12:]
    payload = {"model": BRAIN_MODEL,
               "messages": [{"role": "system", "content":
                             "Ты — встроенный ассистент бота дебатов. Отвечай кратко и по делу, по-русски."}]
               + hist, "max_tokens": 700}
    headers = {"Authorization": f"Bearer {BRAIN_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{BRAIN_BASE}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            ans = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"🧠 Ошибка ЛЛМ: {_esc(e)}"
    hist.append({"role": "assistant", "content": ans})
    return _esc(ans)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    chat_id = update.effective_chat.id
    if not _brain_on.get(chat_id):
        await update.effective_chat.send_message("Меню:", reply_markup=_kb_main())
        return
    ans = await _brain_reply(chat_id, update.effective_message.text or "")
    await update.effective_chat.send_message(ans, parse_mode=ParseMode.HTML)


# ─── callbacks ──────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _owner_chat_id
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    u = update.effective_user

    if data == "claim":
        if _claim_owner(u.id):
            _owner_chat_id = update.effective_chat.id
            await q.answer("Готово — ты владелец")
            await _safe_edit(update, "✅ Ты теперь владелец.\nВыбери раздел:", _kb_main())
        else:
            await q.answer("Бот уже присвоен другому", show_alert=True)
        return

    if not _is_owner(update):
        await q.answer()
        return
    _owner_chat_id = update.effective_chat.id
    await q.answer()

    if data == "menu:main":
        await _safe_edit(update, "👋 <b>Debate-II — пульт</b>\nВыбери раздел:", _kb_main())
    elif data == "menu:brain":
        chat_id = update.effective_chat.id
        _brain_on[chat_id] = True
        cfg = "включён" if BRAIN_API_KEY else "НЕ настроен (нужен BRAIN_API_KEY)"
        await _safe_edit(
            update,
            f"🧠 <b>Мозг</b> ({cfg}).\nПиши сообщения — отвечаю как ЛЛМ. "
            "Кнопка ниже выключает режим.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Выключить мозг", callback_data="brain:off")],
                                  [InlineKeyboardButton("← Меню", callback_data="menu:main")]]))
    elif data == "brain:off":
        _brain_on[update.effective_chat.id] = False
        await _safe_edit(update, "🧠 Мозг выключен.", _kb_main())
    elif data == "menu:doors":
        text, kb = _render_doors_kb(await _fetch("/doors"))
        await _safe_edit(update, text, kb)
    elif data == "door:new":
        ok, res = await _send("POST", "/doors", {"title": ""})
        if not ok:
            await _safe_edit(update, f"❌ {res}",
                             InlineKeyboardMarkup([[InlineKeyboardButton("← Двери", callback_data="menu:doors")]]))
            return
        did = res.get("door_id")
        await _safe_edit(
            update,
            f"✅ <b>Дверь #{did}</b> создана.\n\n🔗 Ссылка для ИИ-дебатёров:\n"
            f"<code>{_esc(_door_link(did))}</code>\n\n"
            f"Запусти дебат: пришли файл-тему с подписью\n<code>/begin {did} &lt;кол-во&gt; &lt;тема&gt;</code>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🚪 Открыть #{did}", callback_data=f"door:view:{did}")],
                [InlineKeyboardButton("← Двери", callback_data="menu:doors")]]))
    elif data.startswith("door:view:"):
        did = int(data.split(":")[2])
        text, kb = _render_door_view(did, await _fetch(f"/door/{did}/state"))
        await _safe_edit(update, text, kb)
    elif data.startswith("door:del:"):
        did = int(data.split(":")[2])
        ok, res = await _send("DELETE", f"/doors/{did}")
        _subs.pop(did, None)
        text, kb = _render_doors_kb(await _fetch("/doors"))
        await _safe_edit(update, (f"🗑 #{did} удалена.\n\n" if ok else f"❌ {res}\n\n") + text, kb)
    elif data.startswith("door:sub:"):
        did = int(data.split(":")[2])
        ev = await _fetch(f"/door/{did}/events", {"since": 10**12})
        _subs[did] = ev.get("last_id", 0) if ev else 0
        text, kb = _render_door_view(did, await _fetch(f"/door/{did}/state"))
        await _safe_edit(update, f"🔔 Подписка на #{did} включена. События пойдут сюда.\n\n" + text, kb)
    elif data.startswith("door:unsub:"):
        did = int(data.split(":")[2])
        _subs.pop(did, None)
        text, kb = _render_door_view(did, await _fetch(f"/door/{did}/state"))
        await _safe_edit(update, f"🔕 Отписка от #{did}.\n\n" + text, kb)


# ─── background jobs (push + self-ping) ──────────────────────────────────────

async def job_push(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _owner_chat_id is None or not _subs:
        return
    for did in list(_subs.keys()):
        ev = await _fetch(f"/door/{did}/events", {"since": _subs[did]})
        if not ev:
            continue
        events = ev.get("events", [])
        for e in events:
            try:
                await ctx.bot.send_message(
                    _owner_chat_id, f"🚪#{did} {_esc(e.get('text'))}", parse_mode=ParseMode.HTML)
            except Exception as ex:
                log.warning("push send failed: %s", ex)
        if events:
            _subs[did] = events[-1]["id"]
        elif "last_id" in ev:
            _subs[did] = max(_subs[did], ev["last_id"])


async def job_ping(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.get(f"{DEBATE_BASE_URL}/healthz")
    except Exception as e:
        log.debug("self-ping failed: %s", e)


# ─── wiring ─────────────────────────────────────────────────────────────────

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("telegram handler error: %s", ctx.error)


def build_app() -> Application | None:
    if not TG_TOKEN:
        log.info("TELEGRAM_BOT_TOKEN not set — telegram bot disabled")
        return None
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("doors", cmd_doors))
    app.add_handler(CommandHandler("door", cmd_door))
    app.add_handler(CommandHandler("newdoor", cmd_newdoor))
    app.add_handler(CommandHandler("deldoor", cmd_deldoor))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(CommandHandler("url", cmd_url))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    if app.job_queue is not None:
        app.job_queue.run_repeating(job_push, interval=4, first=4)
        if SELF_PING:
            app.job_queue.run_repeating(job_ping, interval=PING_INTERVAL, first=30)
            log.info("self-ping every %ss enabled", PING_INTERVAL)
    else:
        log.warning("job_queue unavailable — push/self-ping disabled "
                    "(install python-telegram-bot[job-queue])")
    return app
