"""Debate-v3 door management screens — mounted inside AI CHECK.

Provides inline-keyboard screens for:
- 🚪 Door list (all doors with status)
- 🆕 Create new door (Part A/B/C, debater IDs)
- 📋 Door detail (state, phase, events, tokens, link)
- ⚡ Owner directives (pause / resume / terminate)

Engine calls are direct Python (no HTTP round-trip — the engine is embedded).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


logger = logging.getLogger(__name__)


# ─── FSM states ──────────────────────────────────────────────────────────────

class DebateDoorStates(StatesGroup):
    awaiting_title = State()
    awaiting_debaters = State()


# ─── Engine helpers (lazy import to avoid boot-time issues) ──────────────────

def _engine():
    """Lazy import of engine modules."""
    from ..debates.engine import db, doors
    from ..debates.engine.canonical import list_part_ids, load_part_config
    return db, doors, list_part_ids, load_part_config


def _configs_dir() -> Path:
    return Path(__file__).parent.parent / "debates" / "engine" / "part_configs"


# ─── Owner check (reuse from AI Check) ──────────────────────────────────────

async def _is_owner_query(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]
    return _is_owner(query.from_user.id if query.from_user else 0)


def _is_owner_sync(user_id: int) -> bool:
    from ...wizard import _is_owner  # type: ignore[attr-defined]
    return _is_owner(user_id)


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
        text="« AI Check", callback_data="aicheck:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_door_detail(door_id: str, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="🔑 Показать токены", callback_data=f"dbd:tok:{door_id}")],
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
    """Called from ``build_ai_check_router()`` to mount debate-door
    callbacks onto the existing AI Check router."""

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

    # -- Show debater URL --
    @router.callback_query(F.data.startswith("dbd:url:"))
    async def url_(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        door_id = query.data.removeprefix("dbd:url:")
        base = _public_url()
        db_mod, _, _, _ = _engine()
        with db_mod.connect() as con:
            cur = con.execute(
                "SELECT debater_id, token FROM door_tokens WHERE door_id = ?",
                (door_id,))
            rows = cur.fetchall()
        if not base:
            base = "https://&lt;BOT_URL&gt;"
        lines = [
            f"<b>🌐 Ссылки для дебатёров</b>\n<code>{door_id}</code>\n",
            f"<b>Base URL:</b>\n<code>{base}/debate</code>\n",
            "<b>Эндпоинты:</b>",
            f"  POST <code>{base}/debate/door/{door_id}/msg</code>",
            f"  POST <code>{base}/debate/door/{door_id}/validate</code>",
            f"  GET  <code>{base}/debate/door/{door_id}/state</code>",
            f"  GET  <code>{base}/debate/door/{door_id}/events</code>",
            "",
        ]
        for r in rows:
            lines.append(
                f'<b>{r["debater_id"]}</b>: '
                f'<code>X-Debate-Token: {r["token"]}</code>')
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
            lines.append(
                f'{rej}<b>#{ev["msg_id"]}</b> [{ev["kind"]}] '
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

    # -- Create door (step 2: enter title) --
    @router.callback_query(F.data.startswith("dbd:part:"))
    async def new_step2_(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        part_id = query.data.removeprefix("dbd:part:")
        await state.update_data(new_part=part_id)
        await state.set_state(DebateDoorStates.awaiting_title)
        text = (
            f"<b>Part: {part_id}</b>\n\n"
            "Напиши название двери (title), например:\n"
            "<code>Coder-bot spec Part C</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="dbd:home")],
        ])
        await _send_or_edit(query, text, kb)

    # -- Create door (step 3: enter debater IDs) --
    @router.message(DebateDoorStates.awaiting_title)
    async def new_step3_(message: Message, state: FSMContext) -> None:
        if not _is_owner_sync(message.from_user.id if message.from_user else 0):
            return
        title = (message.text or "").strip()
        if len(title) < 3:
            await message.answer("Название слишком короткое (мин. 3 символа).")
            return
        await state.update_data(new_title=title)
        await state.set_state(DebateDoorStates.awaiting_debaters)
        await message.answer(
            f"<b>Название:</b> {title}\n\n"
            "Теперь напиши два debater_id через пробел, например:\n"
            "<code>d6 viktor</code>",
            parse_mode="HTML",
        )

    # -- Create door (step 4: actually create) --
    @router.message(DebateDoorStates.awaiting_debaters)
    async def new_step4_(message: Message, state: FSMContext) -> None:
        if not _is_owner_sync(message.from_user.id if message.from_user else 0):
            return
        text = (message.text or "").strip()
        parts = text.split()
        if len(parts) != 2 or parts[0] == parts[1]:
            await message.answer(
                "Нужно ровно 2 разных debater_id через пробел.\n"
                "Пример: <code>d6 viktor</code>",
                parse_mode="HTML",
            )
            return
        data = await state.get_data()
        part_id = data.get("new_part", "C")
        title = data.get("new_title", "Untitled")
        await state.clear()

        _, doors_mod, _, _ = _engine()
        try:
            result = doors_mod.create_door(title, part_id, "viktor_v3", parts)
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
            return

        base = _public_url() or "https://&lt;BOT_URL&gt;"
        door_id = result["door_id"]
        tokens = result.get("tokens", {})

        lines = [
            "✅ <b>Дверь создана!</b>\n",
            f"<b>ID:</b> <code>{door_id}</code>",
            f"<b>Part:</b> {part_id}",
            f"<b>Title:</b> {title}\n",
            "<b>🔑 Токены:</b>",
        ]
        for deb, tok in tokens.items():
            lines.append(f"  <b>{deb}:</b> <code>{tok}</code>")
        lines.extend([
            "",
            "<b>🌐 Ссылка для дебатёров:</b>",
            f"<code>{base}/debate/door/{door_id}/msg</code>",
            "",
            "Дай каждому дебатёру его токен.",
            "Оба используют одну и ту же ссылку.",
        ])

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Открыть дверь",
                                  callback_data=f"dbd:view:{door_id}")],
            [InlineKeyboardButton(text="« Двери", callback_data="dbd:home")],
        ])
        await message.answer("\n".join(lines), parse_mode="HTML",
                             reply_markup=kb)

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
