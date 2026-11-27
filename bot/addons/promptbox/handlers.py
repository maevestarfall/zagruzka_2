# Prompt storage addon: composer (functions + prompt slots + executor + run).
# Co-authored with CoCo
"""Telegram UI for «🗄 Хранилище промтов» (pb:* callbacks).

Composer hub: pick FUNCTIONS (checkboxes), pick a PROMPT slot, pick an
EXECUTOR (brain), then RUN — the selected function context + prompt is sent
to the chosen brain as a plain instruction (like typing to the LLM in chat).

State (addon namespace ``promptbox``):
  slots      — list[{"desc","prompt"}] (20)
  sel_funcs  — list[int] selected function indices
  sel_prompt — int index of chosen slot (-1 = none)
  executor   — "auto" | "brain1" | "brain2"
"""

from __future__ import annotations

import contextlib
import html
import io
import json
import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import state as addon_state

logger = logging.getLogger(__name__)

ADDON = "promptbox"
_MAX_SLOTS = 20

# Curated list of project functions (F) and sub-functions (S).
# Card checker («Выдача») intentionally NOT listed — I don't route tasks there.
FUNCTIONS = [
    ("F", "🤖 Helpzavr — разбор скриншотов", "главное меню"),
    ("F", "✨ Красивый текст", "главное меню"),
    ("F", "📬 Проверка почты", "главное меню"),
    ("F", "🎬 Генерация фото и видео", "главное меню"),
    ("F", "🕵️ OSINT — поиск человека", "главное меню"),
    ("F", "🛡 AI CHECK", "главное меню"),
    ("F", "🎤 Дебаты", "главное меню"),
    ("S", "🎯 Viktor v3 дебат", "Дебаты"),
    ("F", "🔬 Анализатор PRO", "главное меню"),
    ("F", "📝 Markitdown", "главное меню"),
    ("F", "🐙 GitHub проекты", "главное меню"),
    ("F", "💻 Кодер (Coder Firm)", "главное меню"),
    ("F", "🪄 Регенерация", "главное меню"),
    ("S", "🎤 Найти проблемы через дебаты", "Регенерация"),
    ("F", "✨ Vibe (Vibe Coding)", "главное меню"),
    ("F", "🖥 Терминал", "главное меню"),
    ("F", "🌐 Браузерная", "главное меню"),
    ("S", "🌐 Классический движок (CloakBrowser)", "Браузерная"),
    ("S", "🆕 browser-use", "Браузерная"),
    ("S", "🌐 curl_cffi (Chrome-TLS запросы, обход Cloudflare)", "Браузерная"),
    ("S", "🕵️ Антидетект", "Браузерная / Настройки"),
    ("S", "🖥 Железо и прокси", "Браузерная / Прочие"),
    ("S", "🔑 Логин/пароль для домена", "Браузерная"),
    ("F", "🧠 Умное зрение (OpenCV+OCR)", "главное меню / кликер"),
    ("F", "🤖 Макрос действий (кликер)", "🧖 баня"),
    ("S", "📸 Визуальная запись по скриншотам", "кликер"),
    ("F", "🗄 Хранилище промтов", "главное меню"),
    ("F", "🖥 VISION — веб-панель", "главное меню"),
    ("F", "🗺 Карта проекта", "главное меню"),
    ("F", "🛠 Master Room", "главное меню"),
    ("S", "💾 Хранилище (файлы)", "Master Room"),
    ("S", "📂 Проекты", "Master Room"),
    ("F", "📥 Скачать видео", "главное меню"),
    ("F", "🎞 Монтажёр", "главное меню"),
    ("F", "🧠 Соображалка", "главное меню"),
    ("F", "🎯 Приоритет", "главное меню"),
    ("F", "📚 Поступенчатая инструкция", "главное меню"),
    ("F", "🔌 Unstructured", "Настройки"),
    ("F", "🧠 Контекст", "Настройки"),
    ("S", "📦 Сравнить zip с версией", "Контекст"),
    ("S", "📦 Сравнить с zip из хранилища", "Контекст"),
    ("S", "🧪 Тест контекста модели", "Контекст"),
    ("F", "💾 Память бота", "Настройки"),
    ("F", "💻 RAM", "Настройки"),
    ("F", "🔒 Сейф данных", "Настройки"),
    ("F", "🔌 Каналы-источники", "Настройки"),
]

_EXEC_LABELS = {"auto": "🧠 Авто мозг", "brain1": "Мозг 1", "brain2": "Мозг 2"}


def _esc(s) -> str:
    return html.escape(str(s or ""))


class PromptBoxStates(StatesGroup):
    awaiting_desc = State()
    awaiting_prompt = State()
    awaiting_import = State()
    awaiting_addon = State()


# ─── state helpers ────────────────────────────────────────────────────────────
def _get_slots() -> list:
    raw = addon_state.get(ADDON, "slots", None)
    if not isinstance(raw, list):
        raw = []
    out = []
    for i in range(_MAX_SLOTS):
        item = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        out.append({"desc": str(item.get("desc", "")), "prompt": str(item.get("prompt", ""))})
    return out


def _save_slots(slots: list) -> None:
    addon_state.set_(ADDON, "slots", slots)


def _sel_funcs() -> list:
    v = addon_state.get(ADDON, "sel_funcs", [])
    return [int(i) for i in v if isinstance(i, int) or str(i).isdigit()] if isinstance(v, list) else []


def _sel_prompt() -> int:
    v = addon_state.get(ADDON, "sel_prompt", -1)
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _executor() -> str:
    v = addon_state.get(ADDON, "executor", "auto")
    return v if v in _EXEC_LABELS else "auto"


def _analyze_first() -> bool:
    return bool(addon_state.get(ADDON, "analyze_first", False))


def _pb_extra() -> str:
    return str(addon_state.get(ADDON, "pb_extra", "") or "")


_PLAN_N = 10
_ORD = ["первый", "второй", "третий", "четвёртый", "пятый",
        "шестой", "седьмой", "восьмой", "девятый", "десятый"]


def _smart_plan() -> bool:
    return bool(addon_state.get(ADDON, "smart_plan", False))


def _plan_slots() -> list:
    raw = addon_state.get(ADDON, "plan_slots", None)
    out = []
    for i in range(_PLAN_N):
        v = raw[i] if isinstance(raw, list) and i < len(raw) else -1
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            out.append(-1)
    return out


def _save_plan(slots: list) -> None:
    addon_state.set_(ADDON, "plan_slots", slots)


def _plan_filled() -> list:
    return [f for f in _plan_slots() if 0 <= f < len(FUNCTIONS)]


# ─── execution helpers ────────────────────────────────────────────────────────
def _compose_user() -> tuple:
    """Build (ok, user_text) from current selections. ok=False if no prompt."""
    slots = _get_slots()
    sp = _sel_prompt()
    if not (0 <= sp < len(slots)) or not (slots[sp]["prompt"] or slots[sp]["desc"]):
        return False, ""
    funcs = _sel_funcs()
    fdesc = "\n".join(f"- {FUNCTIONS[i][1]} ({FUNCTIONS[i][2]})"
                      for i in funcs if i < len(FUNCTIONS)) or "(функции не выбраны)"
    prompt = slots[sp]["prompt"] or slots[sp]["desc"]
    user = f"ЗАТРАГИВАЕМЫЕ ФУНКЦИИ:\n{fdesc}\n\nИНСТРУКЦИЯ (промт):\n{prompt}"
    extra = _pb_extra()
    if extra:
        user += f"\n\nДОПОЛНЕНИЕ ОТ ЗАКАЗЧИКА:\n{extra}"
    return True, user


def _kb_back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Хранилище", callback_data="pb:home")]])


async def _do_execute(msg) -> None:
    ok, user = _compose_user()
    if not ok:
        await msg.answer("Промт не выбран.")
        return
    execu = _executor()
    prog = await msg.answer(f"▶ Исполняю (исполнитель: {_EXEC_LABELS[execu]})…")
    try:
        from ...agent import oneshot_chat
        ans = await oneshot_chat(
            "Ты — исполнитель задач бота Lilush. Тебе дают список затрагиваемых функций проекта "
            "и промт-инструкцию (плюс возможное дополнение заказчика). Выполни по делу. По-русски.",
            user, purpose="promptbox-run", max_tokens=1200)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await prog.edit_text(f"⚠️ Исполнитель недоступен: {_esc(str(exc))}")
        return
    with contextlib.suppress(Exception):
        await prog.delete()
    addon_state.set_(ADDON, "pb_extra", "")  # reset accumulated additions after a run
    await msg.answer("▶ <b>Результат:</b>\n\n" + _esc(ans)[:3800],
                     parse_mode="HTML", reply_markup=_kb_back_home())


async def _do_preview(msg) -> None:
    ok, user = _compose_user()
    if not ok:
        await msg.answer("Промт не выбран.")
        return
    prog = await msg.answer("🔎 Готовлю план…")
    try:
        from ...agent import oneshot_chat
        plan = await oneshot_chat(
            "Ты планировщик. НЕ выполняя задачу, кратко и по делу (по-русски) опиши ЧТО именно "
            "будешь делать по этой инструкции — план шагов. Не выполняй, только план.",
            user, purpose="promptbox-plan", max_tokens=600)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await prog.edit_text(f"⚠️ Недоступно: {_esc(str(exc))}")
        return
    with contextlib.suppress(Exception):
        await prog.delete()
    await msg.answer(
        "🔎 <b>Что будет сделано:</b>\n\n" + _esc(plan)[:3400], parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Исполнить", callback_data="pb:run:go")],
            [InlineKeyboardButton(text="✏️ Дополнить", callback_data="pb:run:add")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="pb:home")],
        ]))


async def _do_pipeline(msg) -> None:
    """Умное планирование: run filled queue slots as sequential agent stages."""
    ok, base = _compose_user()
    if not ok:
        await msg.answer("Промт не выбран.")
        return
    filled = _plan_filled()
    if not filled:
        await _do_execute(msg)
        return
    from ...agent import oneshot_chat
    await msg.answer(f"🧠 Умное планирование: {len(filled)} стади(й) по очереди…")
    prev = ""
    for n, f in enumerate(filled, 1):
        name, where = FUNCTIONS[f][1], FUNCTIONS[f][2]
        u = base + f"\n\n=== СТАДИЯ {n}: применяй подход «{name}» ({where}) ==="
        if prev:
            u += f"\nРезультат предыдущей стадии (учитывай):\n{prev[:1500]}"
        prog = await msg.answer(f"⏳ Стадия {n}: {name}…")
        try:
            ans = await oneshot_chat(
                "Ты — исполнитель-конвейер бота Lilush. Выполни ТЕКУЩУЮ стадию, опираясь на подход "
                "указанной функции и результат прошлой стадии. По-русски, по делу.",
                u, purpose="promptbox-pipe", max_tokens=900)
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await prog.edit_text(f"⚠️ Стадия {n} не удалась: {_esc(str(exc))}")
            return
        prev = ans
        with contextlib.suppress(Exception):
            await prog.edit_text(f"✅ Стадия {n} ({_esc(name)}):\n\n{_esc(ans)[:1400]}", parse_mode="HTML")
    addon_state.set_(ADDON, "pb_extra", "")
    await msg.answer("🧠 <b>Итог конвейера:</b>\n\n" + _esc(prev)[:3400],
                     parse_mode="HTML", reply_markup=_kb_back_home())


async def _execute_route(msg) -> None:
    """Pick pipeline (Умное планирование) or single execution."""
    if _smart_plan() and _plan_filled():
        await _do_pipeline(msg)
    else:
        await _do_execute(msg)


# ─── keyboards ────────────────────────────────────────────────────────────────
def _kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Функции", callback_data="pb:funcs")],
        [InlineKeyboardButton(text="💬 Промпт (слоты)", callback_data="pb:slots")],
        [InlineKeyboardButton(text="🧠 Выбрать исполнителя", callback_data="pb:exec")],
        [InlineKeyboardButton(text=("🔎 Сначала анализ: ВКЛ 🟢" if _analyze_first() else "🔎 Сначала анализ: ВЫКЛ 🔴"), callback_data="pb:af")],
        [InlineKeyboardButton(text=("🧠 Умное планирование: ВКЛ 🟢" if _smart_plan() else "🧠 Умное планирование: ВЫКЛ 🔴"), callback_data="pb:plan")],
        [InlineKeyboardButton(text="▶ Исполнить", callback_data="pb:run")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main:settings")],
    ])


def _render_home() -> str:
    funcs = _sel_funcs()
    fnames = ", ".join(FUNCTIONS[i][1] for i in funcs if i < len(FUNCTIONS))[:400] or "—"
    slots = _get_slots()
    sp = _sel_prompt()
    pname = (slots[sp]["desc"] or slots[sp]["prompt"][:30]) if 0 <= sp < len(slots) and (slots[sp]["desc"] or slots[sp]["prompt"]) else "не выбран"
    return (
        "🗄 <b>Хранилище промтов</b>\n\n"
        f"📋 <b>Функции</b> ({len(funcs)}): {_esc(fnames)}\n"
        f"💬 <b>Промпт</b>: {_esc(pname)}\n"
        f"🧠 <b>Исполнитель</b>: {_EXEC_LABELS[_executor()]}\n\n"
        "Отметь функции, выбери промт и исполнителя → «▶ Исполнить»."
    )


def _kb_slots() -> InlineKeyboardMarkup:
    slots = _get_slots()
    sp = _sel_prompt()
    rows = []
    for i, s in enumerate(slots):
        mark = "✅ " if i == sp else ""
        if s["desc"] or s["prompt"]:
            label = f"{mark}📝 {i + 1}. {s['desc'] or s['prompt'][:24]}"
        else:
            label = f"🆓 Слот {i + 1} (пусто)"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"pb:slot:{i}")])
    rows.append([
        InlineKeyboardButton(text="📤 Экспорт", callback_data="pb:export"),
        InlineKeyboardButton(text="📥 Импорт", callback_data="pb:import"),
    ])
    rows.append([InlineKeyboardButton(text="↩️ Хранилище", callback_data="pb:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_slot(i: int, filled: bool) -> InlineKeyboardMarkup:
    rows = []
    if filled:
        rows.append([InlineKeyboardButton(text="✅ Выбрать для исполнения", callback_data=f"pb:pick:{i}")])
    rows.append([InlineKeyboardButton(text="✏️ Задать/изменить", callback_data=f"pb:edit:{i}")])
    if filled:
        rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"pb:del:{i}")])
    rows.append([InlineKeyboardButton(text="↩️ К слотам", callback_data="pb:slots")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_funcs() -> InlineKeyboardMarkup:
    sel = set(_sel_funcs())
    rows = []
    for i, (kind, name, _where) in enumerate(FUNCTIONS):
        box = "✅" if i in sel else "⬜"
        tag = "" if kind == "F" else "  ↳ "
        rows.append([InlineKeyboardButton(text=f"{box} {tag}{name}"[:60], callback_data=f"pb:fn:{i}")])
    rows.append([InlineKeyboardButton(text="🧹 Сбросить", callback_data="pb:fnclear")])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="pb:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_exec() -> InlineKeyboardMarkup:
    cur = _executor()
    rows = [[InlineKeyboardButton(text=("▶ " if k == cur else "") + v, callback_data=f"pb:ex:{k}")]
            for k, v in _EXEC_LABELS.items()]
    rows.append([InlineKeyboardButton(text="↩️ Хранилище", callback_data="pb:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_plan() -> InlineKeyboardMarkup:
    ps = _plan_slots()
    on = _smart_plan()
    rows = [[InlineKeyboardButton(
        text=("🧠 Умное планирование: ВКЛ 🟢" if on else "🧠 Умное планирование: ВЫКЛ 🔴"),
        callback_data="pb:plan:tog")]]
    for i in range(_PLAN_N):
        f = ps[i]
        val = FUNCTIONS[f][1] if 0 <= f < len(FUNCTIONS) else "(пусто)"
        rows.append([InlineKeyboardButton(text=f"{i + 1}. {_ORD[i]} в очереди: {val}"[:60],
                                          callback_data=f"pb:pslot:{i}")])
    rows.append([InlineKeyboardButton(text="↩️ Хранилище", callback_data="pb:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_plan_pick(slot: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🧹 Очистить слот", callback_data=f"pb:pclr:{slot}")]]
    for i, (kind, name, _w) in enumerate(FUNCTIONS):
        tag = "" if kind == "F" else "  ↳ "
        rows.append([InlineKeyboardButton(text=f"{tag}{name}"[:60], callback_data=f"pb:pset:{slot}:{i}")])
    rows.append([InlineKeyboardButton(text="↩️ К очереди", callback_data="pb:plan")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_PLAN_TEXT = (
    "🧠 <b>Умное планирование</b>\n\n"
    "10 слотов очереди. В каждый ставишь функцию — бот выполнит их <b>по порядку</b> "
    "как стадии (напр. 1: Контекст → 2: Анализ ПРО → 3: Кодер), передавая результат "
    "дальше. Тумблер ВКЛ — «▶ Исполнить» пойдёт по очереди."
)


_SLOTS_TEXT = (
    "💬 <b>Промпт-слоты</b> (20)\n\n"
    "Описание видно на кнопке, полный промт — внутри. Тапни слот, чтобы задать/"
    "изменить/удалить или выбрать для исполнения (✅). 📤/📥 — обмен файлом."
)


# ─── router ───────────────────────────────────────────────────────────────────
def build_promptbox_router() -> Router:
    router = Router(name="promptbox")

    async def _ok(q: CallbackQuery) -> bool:
        from ...wizard import _is_owner
        return _is_owner(q.from_user.id if q.from_user else 0)

    @router.callback_query(F.data.in_({"main:promptbox", "pb:home"}))
    async def cb_home(q: CallbackQuery, state: FSMContext) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_text(_render_home(), parse_mode="HTML", reply_markup=_kb_home())
        await q.answer()

    # ── функции ──
    @router.callback_query(F.data == "pb:funcs")
    async def cb_funcs(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        with contextlib.suppress(Exception):
            await q.message.edit_text(
                "📋 <b>Функции</b> — отметь нужные (✅). ↳ = подфункция.",
                parse_mode="HTML", reply_markup=_kb_funcs())
        await q.answer()

    @router.callback_query(F.data.startswith("pb:fn:"))
    async def cb_fn(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        i = int(q.data.rsplit(":", 1)[-1])
        sel = _sel_funcs()
        if i in sel:
            sel.remove(i)
        elif i < len(FUNCTIONS):
            sel.append(i)
        addon_state.set_(ADDON, "sel_funcs", sel)
        with contextlib.suppress(Exception):
            await q.message.edit_reply_markup(reply_markup=_kb_funcs())
        await q.answer()

    @router.callback_query(F.data == "pb:fnclear")
    async def cb_fnclear(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        addon_state.set_(ADDON, "sel_funcs", [])
        with contextlib.suppress(Exception):
            await q.message.edit_reply_markup(reply_markup=_kb_funcs())
        await q.answer("Сброшено")

    # ── исполнитель ──
    @router.callback_query(F.data == "pb:exec")
    async def cb_exec(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        with contextlib.suppress(Exception):
            await q.message.edit_text(
                "🧠 <b>Исполнитель</b>\nКто выполняет задачу. Исполнение идёт через "
                "мозговую цепочку бота.", parse_mode="HTML", reply_markup=_kb_exec())
        await q.answer()

    @router.callback_query(F.data.startswith("pb:ex:"))
    async def cb_ex(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        v = q.data.rsplit(":", 1)[-1]
        if v in _EXEC_LABELS:
            addon_state.set_(ADDON, "executor", v)
        with contextlib.suppress(Exception):
            await q.message.edit_reply_markup(reply_markup=_kb_exec())
        await q.answer(_EXEC_LABELS.get(v, ""))

    # ── исполнить ──
    @router.callback_query(F.data == "pb:af")
    async def cb_af(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        new = not _analyze_first()
        addon_state.set_(ADDON, "analyze_first", new)
        with contextlib.suppress(Exception):
            await q.message.edit_text(_render_home(), parse_mode="HTML", reply_markup=_kb_home())
        await q.answer("Сначала анализ: " + ("ВКЛ" if new else "ВЫКЛ"))

    @router.callback_query(F.data == "pb:run")
    async def cb_run(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        ok, _u = _compose_user()
        if not ok:
            await q.answer("Сначала выбери промт (💬 Промпт → слот → «Выбрать для исполнения»).", show_alert=True)
            return
        await q.answer()
        if _analyze_first():
            await _do_preview(q.message)
        else:
            await _execute_route(q.message)

    @router.callback_query(F.data == "pb:run:go")
    async def cb_run_go(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await q.answer("Исполняю…")
        await _execute_route(q.message)

    @router.callback_query(F.data == "pb:run:add")
    async def cb_run_add(q: CallbackQuery, state: FSMContext) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(PromptBoxStates.awaiting_addon)
        with contextlib.suppress(Exception):
            await q.message.answer("✏️ Пришли, что <b>изменить/добавить</b> к задаче (я покажу обновлённый план):",
                                   parse_mode="HTML")
        await q.answer()

    @router.message(StateFilter(PromptBoxStates.awaiting_addon), F.text)
    async def msg_addon(m: Message, state: FSMContext) -> None:
        await state.clear()
        prev = _pb_extra()
        addon_state.set_(ADDON, "pb_extra", (prev + "\n" + (m.text or "")).strip())
        await _do_preview(m)

    # ── умное планирование (очередь) ──
    @router.callback_query(F.data == "pb:plan")
    async def cb_plan(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        with contextlib.suppress(Exception):
            await q.message.edit_text(_PLAN_TEXT, parse_mode="HTML", reply_markup=_kb_plan())
        await q.answer()

    @router.callback_query(F.data == "pb:plan:tog")
    async def cb_plan_tog(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        new = not _smart_plan()
        addon_state.set_(ADDON, "smart_plan", new)
        with contextlib.suppress(Exception):
            await q.message.edit_reply_markup(reply_markup=_kb_plan())
        await q.answer("Умное планирование: " + ("ВКЛ" if new else "ВЫКЛ"))

    @router.callback_query(F.data.startswith("pb:pslot:"))
    async def cb_pslot(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        i = int(q.data.rsplit(":", 1)[-1])
        with contextlib.suppress(Exception):
            await q.message.edit_text(
                f"🧠 Слот {i + 1} ({_ORD[i]} в очереди) — выбери функцию:",
                reply_markup=_kb_plan_pick(i))
        await q.answer()

    @router.callback_query(F.data.startswith("pb:pset:"))
    async def cb_pset(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        parts = q.data.split(":")  # pb:pset:<slot>:<func>
        slot, func = int(parts[2]), int(parts[3])
        ps = _plan_slots()
        if 0 <= slot < _PLAN_N and 0 <= func < len(FUNCTIONS):
            ps[slot] = func
            _save_plan(ps)
        with contextlib.suppress(Exception):
            await q.message.edit_text(_PLAN_TEXT, parse_mode="HTML", reply_markup=_kb_plan())
        await q.answer("Задано")

    @router.callback_query(F.data.startswith("pb:pclr:"))
    async def cb_pclr(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        i = int(q.data.rsplit(":", 1)[-1])
        ps = _plan_slots()
        if 0 <= i < _PLAN_N:
            ps[i] = -1
            _save_plan(ps)
        with contextlib.suppress(Exception):
            await q.message.edit_text(_PLAN_TEXT, parse_mode="HTML", reply_markup=_kb_plan())
        await q.answer("Очищено")

    # ── слоты (промпт) ──
    @router.callback_query(F.data == "pb:slots")
    async def cb_slots(q: CallbackQuery, state: FSMContext) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_text(_SLOTS_TEXT, parse_mode="HTML", reply_markup=_kb_slots())
        await q.answer()

    @router.callback_query(F.data.startswith("pb:slot:"))
    async def cb_slot(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        i = int(q.data.rsplit(":", 1)[-1])
        slots = _get_slots()
        if i >= len(slots):
            await q.answer("Нет такого слота.", show_alert=True)
            return
        s = slots[i]
        filled = bool(s["desc"] or s["prompt"])
        if filled:
            body = (f"📝 <b>Слот {i + 1}</b>\n\n"
                    f"<b>Описание:</b> {_esc(s['desc']) or '—'}\n\n"
                    f"<b>Промт:</b>\n<code>{_esc(s['prompt']) or '—'}</code>")
        else:
            body = f"🆓 <b>Слот {i + 1}</b> — пусто.\nНажми «Задать», чтобы вписать промт."
        with contextlib.suppress(Exception):
            await q.message.edit_text(body[:3900], parse_mode="HTML", reply_markup=_kb_slot(i, filled))
        await q.answer()

    @router.callback_query(F.data.startswith("pb:pick:"))
    async def cb_pick(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        i = int(q.data.rsplit(":", 1)[-1])
        addon_state.set_(ADDON, "sel_prompt", i)
        await q.answer(f"Промт слота {i + 1} выбран для исполнения ✅", show_alert=True)

    @router.callback_query(F.data.startswith("pb:edit:"))
    async def cb_edit(q: CallbackQuery, state: FSMContext) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        i = int(q.data.rsplit(":", 1)[-1])
        await state.set_state(PromptBoxStates.awaiting_desc)
        await state.update_data(pb_i=i)
        with contextlib.suppress(Exception):
            await q.message.answer(
                f"✏️ Слот {i + 1}. Пришли <b>короткое описание</b> (одна строка):",
                parse_mode="HTML")
        await q.answer()

    @router.message(StateFilter(PromptBoxStates.awaiting_desc), F.text)
    async def msg_desc(m: Message, state: FSMContext) -> None:
        await state.update_data(pb_desc=(m.text or "").strip())
        await state.set_state(PromptBoxStates.awaiting_prompt)
        await m.answer("Теперь пришли <b>полный текст промта</b> для исполнения:",
                       parse_mode="HTML")

    @router.message(StateFilter(PromptBoxStates.awaiting_prompt), F.text)
    async def msg_prompt(m: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        i = int(data.get("pb_i", 0))
        slots = _get_slots()
        if 0 <= i < len(slots):
            slots[i] = {"desc": data.get("pb_desc", ""), "prompt": (m.text or "").strip()}
            _save_slots(slots)
        await m.answer(
            f"✅ Слот {i + 1} сохранён.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ К слотам", callback_data="pb:slots")]]))

    @router.callback_query(F.data.startswith("pb:del:"))
    async def cb_del(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        i = int(q.data.rsplit(":", 1)[-1])
        slots = _get_slots()
        if 0 <= i < len(slots):
            slots[i] = {"desc": "", "prompt": ""}
            _save_slots(slots)
            if _sel_prompt() == i:
                addon_state.set_(ADDON, "sel_prompt", -1)
        with contextlib.suppress(Exception):
            await q.message.edit_text(_SLOTS_TEXT, parse_mode="HTML", reply_markup=_kb_slots())
        await q.answer("Слот очищен")

    @router.callback_query(F.data == "pb:export")
    async def cb_export(q: CallbackQuery) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        data = json.dumps({"promptbox_slots": _get_slots()}, ensure_ascii=False, indent=2)
        doc = BufferedInputFile(data.encode("utf-8"), filename="promptbox_slots.json")
        with contextlib.suppress(Exception):
            await q.message.answer_document(doc, caption="📤 20 слотов промтов (JSON). Загрузи другому боту через 📥 Импорт.")
        await q.answer()

    @router.callback_query(F.data == "pb:import")
    async def cb_import(q: CallbackQuery, state: FSMContext) -> None:
        if not await _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(PromptBoxStates.awaiting_import)
        with contextlib.suppress(Exception):
            await q.message.answer("📥 Пришли JSON-файл (из «Экспорт»). Он заменит слоты.")
        await q.answer()

    @router.message(StateFilter(PromptBoxStates.awaiting_import), F.document)
    async def msg_import(m: Message, state: FSMContext) -> None:
        await state.clear()
        try:
            buf = io.BytesIO()
            await m.bot.download(m.document.file_id, destination=buf)
            payload = json.loads(buf.getvalue().decode("utf-8"))
            raw = payload.get("promptbox_slots") if isinstance(payload, dict) else payload
            if not isinstance(raw, list):
                raise ValueError("нет списка promptbox_slots")
            slots = []
            for i in range(_MAX_SLOTS):
                it = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
                slots.append({"desc": str(it.get("desc", "")), "prompt": str(it.get("prompt", ""))})
            _save_slots(slots)
        except Exception as exc:  # noqa: BLE001
            await m.answer(f"❌ Не смог импортировать: {_esc(str(exc))}")
            return
        await m.answer(
            "✅ Импортировано.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ К слотам", callback_data="pb:slots")]]))

    return router
