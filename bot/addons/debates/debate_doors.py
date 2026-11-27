# Debate-v3 door management screens: admin link, door views, and server interaction.
# Co-authored with CoCo
"""Debate-v3 door management screens — mounted inside Дебаты.

Provides inline-keyboard screens for:
- 🚪 Door list (all doors with status)
- 🆕 Create new door (Part A/B/C → auto-create with join link)
- 📋 Door detail (state, phase, events, join link)
- ⚡ Owner directives (pause / resume / terminate)

Engine calls are direct Python (no HTTP round-trip — the engine is embedded).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .. import state as addon_state


logger = logging.getLogger(__name__)


# ─── Engine helpers (lazy import to avoid boot-time issues) ──────────────────

def _engine():
    """Lazy import of engine modules."""
    from .engine import db, doors
    from .engine.canonical import list_part_ids, load_part_config
    return db, doors, list_part_ids, load_part_config


def _configs_dir() -> Path:
    return Path(__file__).parent / "engine" / "part_configs"


# ─── Owner check (reuse from AI Check) ──────────────────────────────────────

async def _is_owner_query(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]
    return _is_owner(query.from_user.id if query.from_user else 0)



# ─── Formatting helpers ─────────────────────────────────────────────────────

_PHASE_EMOJI = {
    "voice_manifest": "🗣",
    "atoms_propose": "💡",
    "atoms_critique": "🔍",
    "atoms_revision": "🔄",
    "atoms_adversarial": "⚔️",
    "atoms_approve": "✅",
    "phase_gate_a": "🚧",
    "clusters_propose": "📦",
    "clusters_critique": "🔍",
    "clusters_revision": "🔄",
    "clusters_approve": "✅",
    "phase_gate_b": "🚧",
    "final_propose": "📝",
    "final_critique": "🔍",
    "final_revision": "🔄",
    "final_approve": "✅",
    "phase_gate_c": "🚧",
    "done": "🏁",
    "terminated": "🛑",
}

_STATUS_EMOJI = {"open": "🟢", "paused": "⏸", "closed": "⚫"}


def _fmt_phase(phase: str) -> str:
    em = _PHASE_EMOJI.get(phase, "❓")
    return f"{em} {phase.replace('_', ' ').title()}"


def _fmt_ts(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def _public_url() -> str:
    from ...config import resolve_public_url
    return resolve_public_url()


# ─── Keyboards ───────────────────────────────────────────────────────────────

def _kb_doors_home(doors_list: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for d in doors_list[:12]:
        em = _STATUS_EMOJI.get(d["status"], "❓")
        label = f'{em} {d["title"][:28]} — {d["part_id"]}'
        rows.append([InlineKeyboardButton(
            text=label, callback_data=f"dbd:view:{d['door_id']}")])
    rows.append([InlineKeyboardButton(
        text="🆕 Создать дверь", callback_data="dbd:new")])
    rows.append([InlineKeyboardButton(
        text="🔄 Обновить", callback_data="dbd:home")])
    rows.append([InlineKeyboardButton(
        text="« Дебаты", callback_data="debates:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_door_detail(door_id: str, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="🌐 Ссылка для дебатёров", callback_data=f"dbd:url:{door_id}")],
        [InlineKeyboardButton(
            text="📜 Последние события", callback_data=f"dbd:evts:{door_id}")],
    ]
    if status == "open":
        rows.append([
            InlineKeyboardButton(
                text="⏸ Пауза", callback_data=f"dbd:pause:{door_id}"),
            InlineKeyboardButton(
                text="🛑 Завершить", callback_data=f"dbd:term:{door_id}"),
        ])
    elif status == "paused":
        rows.append([
            InlineKeyboardButton(
                text="▶️ Возобновить", callback_data=f"dbd:resume:{door_id}"),
            InlineKeyboardButton(
                text="🛑 Завершить", callback_data=f"dbd:term:{door_id}"),
        ])
    rows.append([InlineKeyboardButton(
        text="💬 Чат с админом", callback_data=f"dbd:achat:{door_id}")])
    rows.append([InlineKeyboardButton(
        text="🔗 Ссылка для админа", callback_data=f"dbd:admlink:{door_id}")])
    rows.append([InlineKeyboardButton(
        text="🔄 Обновить", callback_data=f"dbd:view:{door_id}")])
    rows.append([InlineKeyboardButton(
        text="« Двери", callback_data="dbd:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_part_select() -> InlineKeyboardMarkup:
    _, _, list_ids, _ = _engine()
    parts = list_ids(_configs_dir())
    rows = [[InlineKeyboardButton(
        text=f"Part {p}", callback_data=f"dbd:part:{p}")] for p in parts]
    rows.append([InlineKeyboardButton(text="« Отмена", callback_data="dbd:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_back_doors() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Двери", callback_data="dbd:home")],
    ])


# ─── Screen content builders ────────────────────────────────────────────────

def _render_doors_home(doors_list: list[dict]) -> str:
    if not doors_list:
        return (
            "<b>🚪 Двери дебатов v3</b>\n\n"
            "Дверей пока нет. Нажми <b>🆕 Создать дверь</b> для начала.\n\n"
            "<i>Двери — это сессии STRICT-дебата по PRAVILO_DEBAT_v3.</i>"
        )
    lines = ["<b>🚪 Двери дебатов v3</b>", ""]
    for d in doors_list[:12]:
        em = _STATUS_EMOJI.get(d["status"], "❓")
        phase = _fmt_phase(d.get("current_phase", "?"))
        ts = _fmt_ts(d["created_at"]) if d.get("created_at") else "?"
        lines.append(
            f'{em} <b>{d["title"][:32]}</b> — {d["part_id"]}\n'
            f'   Фаза: {phase} | {ts}')
    lines.append(f"\nВсего: {len(doors_list)}")
    return "\n".join(lines)


def _render_door_detail(state: dict) -> str:
    d = state
    phase = _fmt_phase(d.get("current_phase", "?"))
    em = _STATUS_EMOJI.get(d.get("status", "?"), "❓")
    counters = d.get("counters", {})
    msgs_total = counters.get("messages_total", 0)
    rej_total = counters.get("rejections_total", 0)
    per_deb = counters.get("per_debater", {})

    lines = [
        f'{em} <b>{d.get("title", "?")}</b>',
        f'Part: <b>{d.get("part", "?")}</b>',
        f'Фаза: {phase}',
        f'Статус: {d.get("status", "?")}',
        "",
        f'📩 Сообщений: {msgs_total} (❌ отклонено: {rej_total})',
    ]
    for deb, stats in per_deb.items():
        msg_cnt = stats.get("messages", 0)
        atoms = stats.get("atoms_proposed", 0)
        lines.append(f'  • {deb}: {msg_cnt} msg, {atoms} atoms')

    # atoms summary
    atoms_by_cat = d.get("atoms", {})
    locked = sum(1 for c in atoms_by_cat.values()
                 if c.get("locked_version") is not None)
    total_cats = len(d.get("canonical_categories", []))
    lines.append(f'\n🔒 Атомы: {locked}/{total_cats} залочены')

    # phase gates
    gates = d.get("phase_gates", {})
    if gates:
        lines.append("\n📋 Phase gates:")
        for name, g in gates.items():
            st = "✅" if g["status"] == "passed" else "❌"
            lines.append(f'  {st} {name}')

    fvm = d.get("forced_validation_mode")
    if fvm:
        lines.append("\n⚠️ Forced Validation Mode ON")

    lines.append(f'\n<code>{d.get("door_id", "?")}</code>')
    return "\n".join(lines)


# ─── Inline message send/edit helper ────────────────────────────────────────

async def _send_or_edit(query: CallbackQuery, text: str,
                        kb: InlineKeyboardMarkup) -> None:
    try:
        await query.message.edit_text(
            text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True)
    except Exception:
        await query.message.answer(
            text, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True)
    await query.answer()


# ─── Register callbacks onto the AI Check router ─────────────────────────────

def register_debate_doors(router: Router) -> None:
    """Called from ``build_debates_router()`` to mount debate-door
    callbacks onto the existing Debates router."""

    # -- Home screen --
    @router.callback_query(F.data == "dbd:home")
    async def doors_home_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        db_mod, doors_mod, _, _ = _engine()
        try:
            with db_mod.connect() as con:
                cur = con.execute(
                    "SELECT door_id, title, part_id, style, status, "
                    "current_phase, created_at FROM doors "
                    "ORDER BY created_at DESC")
                all_doors = [dict(r) for r in cur.fetchall()]
        except Exception:
            all_doors = []
        text = _render_doors_home(all_doors)
        kb = _kb_doors_home(all_doors)
        await _send_or_edit(query, text, kb)

    # -- View door --
    @router.callback_query(F.data.startswith("dbd:view:"))
    async def view_door_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        door_id = query.data.removeprefix("dbd:view:")
        _, doors_mod, _, _ = _engine()
        s = doors_mod.get_state_snapshot(door_id)
        if not s.get("ok"):
            await query.answer("Дверь не найдена.", show_alert=True)
            return
        text = _render_door_detail(s)
        kb = _kb_door_detail(door_id, s.get("status", "closed"))
        await _send_or_edit(query, text, kb)

    # -- Show tokens --
    @router.callback_query(F.data.startswith("dbd:tok:"))
    async def tokens_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        door_id = query.data.removeprefix("dbd:tok:")
        db_mod, _, _, _ = _engine()
        with db_mod.connect() as con:
            cur = con.execute(
                "SELECT debater_id, token FROM door_tokens WHERE door_id = ?",
                (door_id,))
            rows = cur.fetchall()
        if not rows:
            await query.answer("Токены не найдены.", show_alert=True)
            return
        lines = [f"<b>🔑 Токены для двери</b>\n<code>{door_id}</code>\n"]
        for r in rows:
            lines.append(f'<b>{r["debater_id"]}</b>:\n<code>{r["token"]}</code>\n')
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data=f"dbd:view:{door_id}")],
        ])
        await _send_or_edit(query, "\n".join(lines), kb)

    # -- Show debater URL (join link) --
    @router.callback_query(F.data.startswith("dbd:url:"))
    async def url_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        door_id = query.data.removeprefix("dbd:url:")
        base = _public_url()
        db_mod, _, _, _ = _engine()
        with db_mod.connect() as con:
            row = con.execute(
                "SELECT join_secret FROM doors WHERE door_id = ?",
                (door_id,)).fetchone()
        if not base:
            base = "https://&lt;BOT_URL&gt;"
        join_secret = row["join_secret"] if row and row["join_secret"] else None
        if join_secret:
            join_url = f"{base}/debate/join/{join_secret}"
            lines = [
                "<b>🌐 Ссылка для дебатёров</b>\n",
                f"<code>{join_url}</code>\n",
                "Отправь эту ссылку каждому дебатёру.",
                "Первый кто перейдёт — слот A, второй — слот B.\n",
                f"<b>Лобби:</b>\n<code>{base}/debate/door/{door_id}/lobby</code>",
            ]
        else:
            # Legacy door without join_secret — show endpoints
            lines = [
                f"<b>🌐 Эндпоинты</b>\n<code>{door_id}</code>\n",
                f"  POST <code>{base}/debate/door/{door_id}/msg</code>",
                f"  GET  <code>{base}/debate/door/{door_id}/state</code>",
                f"  GET  <code>{base}/debate/door/{door_id}/events</code>",
            ]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data=f"dbd:view:{door_id}")],
        ])
        await _send_or_edit(query, "\n".join(lines), kb)

    # -- Events --
    @router.callback_query(F.data.startswith("dbd:evts:"))
    async def events_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        door_id = query.data.removeprefix("dbd:evts:")
        _, doors_mod, _, _ = _engine()
        r = doors_mod.list_events(door_id, since_msg_id=0, limit=15)
        if not r.get("ok"):
            await query.answer("Не удалось загрузить.", show_alert=True)
            return
        lines = [f"<b>📜 Последние события</b>\n<code>{door_id}</code>\n"]
        for ev in r.get("events", [])[-15:]:
            rej = "❌ " if ev.get("rejected") else ""
            preview = (ev.get("preview") or "")[:60]
            ts_str = _fmt_ts(ev["ts"]) if ev.get("ts") else ""
            lines.append(
                f'{rej}<b>#{ev["msg_id"]}</b> <i>{ts_str}</i> '
                f'[{ev["kind"]}] '
                f'{ev["debater_id"]}: {preview}')
        if not r.get("events"):
            lines.append("<i>Событий пока нет.</i>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить",
                                  callback_data=f"dbd:evts:{door_id}")],
            [InlineKeyboardButton(text="« Назад",
                                  callback_data=f"dbd:view:{door_id}")],
        ])
        await _send_or_edit(query, "\n".join(lines), kb)

    # -- Create door (step 1: select Part) --
    @router.callback_query(F.data == "dbd:new")
    async def new_step1_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        text = (
            "<b>🆕 Создать дверь</b>\n\n"
            "Выбери Part (часть спецификации):"
        )
        kb = _kb_part_select()
        await _send_or_edit(query, text, kb)

    # -- Create door (step 2: pick Part → auto-create) --
    @router.callback_query(F.data.startswith("dbd:part:"))
    async def new_step2_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        part_id = query.data.removeprefix("dbd:part:")
        await state.clear()

        # Auto-generate title: "Part {id} #{count}"
        db_mod, doors_mod, _, _ = _engine()
        try:
            with db_mod.connect() as con:
                cnt = con.execute(
                    "SELECT COUNT(*) FROM doors WHERE part_id = ?",
                    (part_id,)).fetchone()[0]
        except Exception:
            cnt = 0
        title = f"Part {part_id} #{cnt + 1}"

        # Create door with join link (no debater IDs / tokens needed)
        try:
            result = doors_mod.create_door_simple(title, part_id)
        except Exception as e:
            await _send_or_edit(
                query, f"❌ Ошибка: {e}", _kb_back_doors())
            return

        base = _public_url() or "https://&lt;BOT_URL&gt;"
        door_id = result["door_id"]
        join_secret = result.get("join_secret", "")
        join_url = f"{base}/debate/join/{join_secret}"

        lines = [
            "✅ <b>Дверь создана!</b>\n",
            f"<b>Part:</b> {part_id}",
            f"<b>Название:</b> {title}\n",
            "<b>🌐 Ссылка для дебатёров:</b>",
            f"<code>{join_url}</code>\n",
            "Отправь эту ссылку каждому дебатёру.",
            "Первый кто перейдёт — получит слот A, второй — слот B.",
        ]

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Открыть дверь",
                                  callback_data=f"dbd:view:{door_id}")],
            [InlineKeyboardButton(text="« Двери", callback_data="dbd:home")],
        ])
        await _send_or_edit(query, "\n".join(lines), kb)

    # -- Owner directives --
    @router.callback_query(F.data.startswith("dbd:pause:"))
    async def pause_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return
        door_id = query.data.removeprefix("dbd:pause:")
        db_mod, _, _, _ = _engine()
        with db_mod.connect() as con:
            con.execute("UPDATE doors SET status = 'paused' WHERE door_id = ?",
                        (door_id,))
            con.execute(
                "INSERT INTO owner_directives (door_id, kind, payload_json, ran_at) "
                "VALUES (?, 'pause', '{}', ?)", (door_id, time.time()))
        await query.answer("⏸ Поставлено на паузу", show_alert=True)
        # refresh view
        _, doors_mod, _, _ = _engine()
        s = doors_mod.get_state_snapshot(door_id)
        if s.get("ok"):
            await _send_or_edit(query, _render_door_detail(s),
                                _kb_door_detail(door_id, s["status"]))

    @router.callback_query(F.data.startswith("dbd:resume:"))
    async def resume_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return
        door_id = query.data.removeprefix("dbd:resume:")
        db_mod, _, _, _ = _engine()
        with db_mod.connect() as con:
            con.execute("UPDATE doors SET status = 'open' WHERE door_id = ?",
                        (door_id,))
            con.execute(
                "INSERT INTO owner_directives (door_id, kind, payload_json, ran_at) "
                "VALUES (?, 'resume', '{}', ?)", (door_id, time.time()))
        await query.answer("▶️ Возобновлено", show_alert=True)
        _, doors_mod, _, _ = _engine()
        s = doors_mod.get_state_snapshot(door_id)
        if s.get("ok"):
            await _send_or_edit(query, _render_door_detail(s),
                                _kb_door_detail(door_id, s["status"]))

    @router.callback_query(F.data.startswith("dbd:term:"))
    async def terminate_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return
        door_id = query.data.removeprefix("dbd:term:")
        db_mod, _, _, _ = _engine()
        with db_mod.connect() as con:
            con.execute(
                "UPDATE doors SET status = 'closed', "
                "current_phase = 'terminated', "
                "terminated_reason = 'owner' WHERE door_id = ?",
                (door_id,))
            con.execute(
                "INSERT INTO owner_directives (door_id, kind, payload_json, ran_at) "
                "VALUES (?, 'terminate', '{}', ?)", (door_id, time.time()))
        await query.answer("🛑 Дверь завершена", show_alert=True)
        _, doors_mod, _, _ = _engine()
        s = doors_mod.get_state_snapshot(door_id)
        if s.get("ok"):
            await _send_or_edit(query, _render_door_detail(s),
                                _kb_door_detail(door_id, s["status"]))

    # -- Admin Chat view --
    @router.callback_query(F.data.startswith("dbd:achat:"))
    async def admin_chat_view_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        door_id = query.data.removeprefix("dbd:achat:")
        db_mod, _, _, _ = _engine()
        with db_mod.connect() as con:
            rows = con.execute(
                "SELECT debater_id, role, content, created_at "
                "FROM admin_chat WHERE door_id = ? "
                "ORDER BY created_at DESC LIMIT 20",
                (door_id,)).fetchall()
        if not rows:
            text = (
                f"<b>💬 Чат с админом</b>\n"
                f"<code>{door_id}</code>\n\n"
                "<i>Сообщений пока нет.\n\n"
                "Дебатёры пишут сюда через API:\n"
                "POST /debate/door/{door_id}/admin_chat\n"
                '{"message": "описание проблемы"}</i>'
            )
        else:
            lines = [
                f"<b>💬 Чат с админом</b>\n"
                f"<code>{door_id}</code>\n"
            ]
            for r in reversed(rows):
                ts = _fmt_ts(r["created_at"])
                tag = f"🗣 {r['debater_id']}" if r["role"] == "debater" else "🤖 Админ"
                content = (r["content"] or "")[:200]
                lines.append(f"<b>{tag}</b> <i>{ts}</i>\n{content}\n")
            text = "\n".join(lines)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Обновить", callback_data=f"dbd:achat:{door_id}")],
            [InlineKeyboardButton(
                text="« Назад", callback_data=f"dbd:view:{door_id}")],
        ])
        await _send_or_edit(query, text[:4000], kb)

    # -- Admin link --
    @router.callback_query(F.data.startswith("dbd:admlink:"))
    async def admin_link_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        door_id = query.data.removeprefix("dbd:admlink:")
        from .debate_server import _get_admin_secret, _PREFIX
        secret = _get_admin_secret()
        v3 = addon_state.get("debates", "viktor_v3", {}) or {}
        base = (v3.get("server_base_url") or "").rstrip("/")
        if not base:
            base = "https://YOUR_SERVER"
        link = f"{base}{_PREFIX}/admin_connect/{secret}"
        text = (
            "<b>🔗 Ссылка для админа</b>\n\n"
            f"<code>{link}</code>\n\n"
            "<b>Права:</b> чтение состояния дверей, событий, бюджета, "
            "чтение/запись админ-чата.\n"
            "<b>Ограничения:</b> нельзя менять правила, фазы, "
            "создавать двери, кикать дебатёров."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="« Назад", callback_data=f"dbd:view:{door_id}")],
        ])
        await _send_or_edit(query, text, kb)
