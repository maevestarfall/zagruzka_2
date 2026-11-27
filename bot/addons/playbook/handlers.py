"""Telegram UI for «📚 Поступенчатая инструкция» (pb:* callbacks)."""

from __future__ import annotations

import contextlib
import html
import logging

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

from ...access import can_admin
from . import executor, hermes, store

logger = logging.getLogger(__name__)


class PlaybookStates(StatesGroup):
    awaiting_node_title = State()
    awaiting_step_name = State()
    awaiting_step_tools = State()
    awaiting_step_prompt = State()
    awaiting_step_timing = State()


def _esc(s: str) -> str:
    return html.escape(s or "")


# ── keyboards ──────────────────────────────────────────────────────────

def _kb_home() -> InlineKeyboardMarkup:
    tf = "🟢 ВКЛ" if store.instruction_first() else "🔴 ВЫКЛ"
    th = "🟢 ВКЛ" if store.hermes_auto() else "🔴 ВЫКЛ"
    rows = [
        [InlineKeyboardButton(text=f"👁 Сначала смотри инструкцию: {tf}", callback_data="pb:tf")],
        [InlineKeyboardButton(text=f"🧠 Гермес дорабатывает: {th}", callback_data="pb:th")],
        [InlineKeyboardButton(text="🧠 Гермес: проработать сейчас", callback_data="pb:hermes")],
    ]
    for n in store.list_children(None):
        rows.append([InlineKeyboardButton(text=f"📂 {n['title']}", callback_data=f"pb:node:{n['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Раздел верхнего уровня", callback_data="pb:add:root")])
    rows.append([InlineKeyboardButton(text="↩️ В настройки", callback_data="main:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_node(node_id: str) -> InlineKeyboardMarkup:
    rows = []
    for ch in store.list_children(node_id):
        mark = "📂" if not store.is_leaf(ch["id"]) else "📄"
        rows.append([InlineKeyboardButton(text=f"{mark} {ch['title']}", callback_data=f"pb:node:{ch['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Подпункт", callback_data=f"pb:add:{node_id}")])
    rows.append([InlineKeyboardButton(text="➕ Шаг (инструмент/промт/тайминг)", callback_data=f"pb:sa:{node_id}")])
    steps_here = store.get_steps(node_id)
    if steps_here:
        rows.append([InlineKeyboardButton(
            text="▶️ Выполнить план", callback_data=f"pb:run:{node_id}")])
    for s in steps_here:
        rows.append([InlineKeyboardButton(
            text=f"🗑 шаг: {s['name'][:30]}",
            callback_data=f"pb:sd:{node_id}:{s['id']}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить раздел", callback_data=f"pb:del:{node_id}")])
    parent = (store.get_node(node_id) or {}).get("parent")
    back = f"pb:node:{parent}" if parent else "pb:home"
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── renderers ──────────────────────────────────────────────────────────

def _render_home() -> str:
    return (
        "📚 <b>Поступенчатая инструкция</b>\n\n"
        "Дерево действий от общего к частному "
        "(Общее → Контент → Фото и видео → Видео → …). На нужном листе "
        "лежит план: <b>инструменты + промты + тайминги</b> по шагам.\n\n"
        "• <b>Сначала смотри инструкцию</b> — бот сперва ищет в дереве "
        "лучший путь и идёт по шагам.\n"
        "• <b>Гермес дорабатывает</b> — фоном смотрит, что сработало, а что "
        "нет, и улучшает шаги (старое не стирает).\n\n"
        "Выбери раздел или добавь новый:"
    )


def _render_node(node_id: str) -> str:
    node = store.get_node(node_id)
    if not node:
        return "Раздел не найден."
    lines = [f"📚 <b>{_esc(store.path_titles(node_id))}</b>\n"]
    steps = store.get_steps(node_id)
    if steps:
        lines.append("<b>План шагов:</b>")
        for i, s in enumerate(steps, 1):
            tools = ", ".join(s.get("tools", [])) or "—"
            stat = f" (✅{s.get('ok',0)}/❌{s.get('fail',0)})" if (s.get('ok') or s.get('fail')) else ""
            lines.append(
                f"\n<b>{i}. {_esc(s['name'])}</b>{stat}\n"
                f"🛠 {_esc(tools)}\n"
                f"💬 {_esc(s.get('prompt','') or '—')}\n"
                f"⏱ {_esc(s.get('timing','') or '—')}"
            )
            if s.get("notes"):
                lines.append(f"📝 {_esc(s['notes'][:300])}")
    else:
        kids = store.list_children(node_id)
        if kids:
            lines.append("<i>Это раздел. Спустись глубже или добавь подпункт.</i>")
        else:
            lines.append("<i>Пока нет шагов. Добавь первый шаг плана.</i>")
    return "\n".join(lines)


# ── router ─────────────────────────────────────────────────────────────

def build_playbook_router() -> Router:
    store.seed_default()  # idempotent — seeds the example tree on first run
    router = Router(name="playbook")

    async def _guard(q: CallbackQuery) -> bool:
        if not can_admin(q.from_user.id):
            await q.answer("Только владелец.", show_alert=True)
            return False
        return True

    @router.callback_query(F.data.in_({"main:playbook", "pb:home"}))
    async def cb_home(q: CallbackQuery, state: FSMContext) -> None:
        if not await _guard(q):
            return
        await state.clear()
        await q.message.edit_text(_render_home(), reply_markup=_kb_home())
        await q.answer()

    @router.callback_query(F.data.startswith("pb:node:"))
    async def cb_node(q: CallbackQuery, state: FSMContext) -> None:
        if not await _guard(q):
            return
        await state.clear()
        nid = q.data.split("pb:node:")[-1]
        await q.message.edit_text(_render_node(nid), reply_markup=_kb_node(nid))
        await q.answer()

    @router.callback_query(F.data == "pb:tf")
    async def cb_tf(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        store.set_instruction_first(not store.instruction_first())
        await q.message.edit_text(_render_home(), reply_markup=_kb_home())
        await q.answer("Готово")

    @router.callback_query(F.data == "pb:th")
    async def cb_th(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        store.set_hermes_auto(not store.hermes_auto())
        await q.message.edit_text(_render_home(), reply_markup=_kb_home())
        await q.answer("Готово")

    @router.callback_query(F.data == "pb:hermes")
    async def cb_hermes(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        await q.answer("Гермес работает…")
        summary = await hermes.refine_once()
        await q.message.edit_text(hermes._build_report(summary), reply_markup=_kb_home())

    # ── run a plan (real execution → records ok/fail for Hermes) ──────────
    @router.callback_query(F.data.startswith("pb:run:"))
    async def cb_run(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        nid = q.data.split("pb:run:")[-1]
        steps = store.get_steps(nid)
        if not steps:
            await q.answer("В этом разделе нет шагов для выполнения.", show_alert=True)
            return
        await q.answer("Выполняю план…")
        path = store.path_titles(nid)
        total = len(steps)
        with contextlib.suppress(Exception):
            await q.message.edit_text(
                f"▶️ <b>Выполняю план</b>\n{_esc(path)}\n\n0/{total} — старт…")

        async def _on_step(i: int, tot: int, step: dict, ok: bool) -> None:
            mark = "✅" if ok else "❌"
            with contextlib.suppress(Exception):
                await q.message.edit_text(
                    f"▶️ <b>Выполняю план</b>\n{_esc(path)}\n\n"
                    f"{mark} {i}/{tot}: {_esc(step.get('name', ''))}")

        try:
            summary = await executor.run_plan(nid, q.from_user.id, on_step=_on_step)
        except Exception as exc:  # noqa: BLE001
            logger.exception("playbook run_plan failed")
            await q.message.edit_text(
                f"⚠️ <b>Не удалось выполнить план</b>\n{_esc(path)}\n\n{_esc(str(exc))}",
                reply_markup=_kb_node(nid))
            return

        report = (
            "✅ <b>План выполнен</b>\n"
            f"{_esc(path)}\n\n"
            f"Шагов: {summary['ran']} · ✅ {summary['succeeded']} · "
            f"❌ {summary['failed']}\n"
            "Результаты записаны в журнал — Гермес учтёт их при доработке."
        )
        await q.message.edit_text(report, reply_markup=_kb_node(nid))

    # ── add node ────────────────────────────────────────────────────────
    @router.callback_query(F.data.startswith("pb:add:"))
    async def cb_add(q: CallbackQuery, state: FSMContext) -> None:
        if not await _guard(q):
            return
        parent = q.data.split("pb:add:")[-1]
        parent = None if parent == "root" else parent
        await state.set_state(PlaybookStates.awaiting_node_title)
        await state.update_data(parent=parent)
        where = store.path_titles(parent) if parent else "верхний уровень"
        await q.message.edit_text(
            f"➕ <b>Новый раздел</b> в: {_esc(where)}\n\nПришли название:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена",
                                      callback_data=f"pb:node:{parent}" if parent else "pb:home")]]))
        await q.answer()

    @router.message(StateFilter(PlaybookStates.awaiting_node_title))
    async def msg_node_title(m: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        node = store.add_node(data.get("parent"), (m.text or "").strip())
        await m.answer(f"✅ Раздел добавлен: <b>{_esc(node['title'])}</b>",
                       reply_markup=_kb_node(node["id"]))

    # ── add step (4-field FSM) ──────────────────────────────────────────
    @router.callback_query(F.data.startswith("pb:sa:"))
    async def cb_step_add(q: CallbackQuery, state: FSMContext) -> None:
        if not await _guard(q):
            return
        nid = q.data.split("pb:sa:")[-1]
        await state.set_state(PlaybookStates.awaiting_step_name)
        await state.update_data(node_id=nid)
        await q.message.edit_text(
            f"➕ <b>Новый шаг</b> в: {_esc(store.path_titles(nid))}\n\n"
            "<b>1/4</b> Название шага:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data=f"pb:node:{nid}")]]))
        await q.answer()

    @router.message(StateFilter(PlaybookStates.awaiting_step_name))
    async def msg_step_name(m: Message, state: FSMContext) -> None:
        await state.update_data(name=(m.text or "").strip())
        await state.set_state(PlaybookStates.awaiting_step_tools)
        await m.answer("<b>2/4</b> Инструменты через запятую "
                       "(напр. <code>media:photo, img2video</code>) или «-»:")

    @router.message(StateFilter(PlaybookStates.awaiting_step_tools))
    async def msg_step_tools(m: Message, state: FSMContext) -> None:
        raw = (m.text or "").strip()
        tools = [] if raw in ("-", "—", "") else [t.strip() for t in raw.split(",")]
        await state.update_data(tools=tools)
        await state.set_state(PlaybookStates.awaiting_step_prompt)
        await m.answer("<b>3/4</b> Промт/что делать на этом шаге:")

    @router.message(StateFilter(PlaybookStates.awaiting_step_prompt))
    async def msg_step_prompt(m: Message, state: FSMContext) -> None:
        await state.update_data(prompt=(m.text or "").strip())
        await state.set_state(PlaybookStates.awaiting_step_timing)
        await m.answer("<b>4/4</b> Тайминг (напр. <code>после кадра, пауза 5–10с</code>) или «-»:")

    @router.message(StateFilter(PlaybookStates.awaiting_step_timing))
    async def msg_step_timing(m: Message, state: FSMContext) -> None:
        raw = (m.text or "").strip()
        timing = "" if raw in ("-", "—") else raw
        data = await state.get_data()
        await state.clear()
        nid = data["node_id"]
        store.add_step(nid, name=data.get("name", "Шаг"),
                       tools=data.get("tools", []),
                       prompt=data.get("prompt", ""), timing=timing)
        await m.answer("✅ Шаг добавлен.", reply_markup=_kb_node(nid))

    # ── delete step / node ──────────────────────────────────────────────
    @router.callback_query(F.data.startswith("pb:sd:"))
    async def cb_step_del(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        _, _, nid, sid = q.data.split(":", 3)
        store.delete_step(nid, sid)
        await q.answer("Шаг удалён")
        await q.message.edit_text(_render_node(nid), reply_markup=_kb_node(nid))

    @router.callback_query(F.data.startswith("pb:del:"))
    async def cb_node_del(q: CallbackQuery) -> None:
        if not await _guard(q):
            return
        nid = q.data.split("pb:del:")[-1]
        parent = (store.get_node(nid) or {}).get("parent")
        n = store.delete_node(nid)
        await q.answer(f"Удалено разделов: {n}")
        if parent:
            await q.message.edit_text(_render_node(parent), reply_markup=_kb_node(parent))
        else:
            await q.message.edit_text(_render_home(), reply_markup=_kb_home())

    return router
