# «🧭 Контекст» (TEMPORAL RELAY) — screen, calibration, run.
# Co-authored with CoCo
"""UI for the TEMPORAL RELAY pipeline.

Button «🧭 Контекст» in the main menu opens this screen:
* 🧪 Калибровать модель — Phase 0 (measures peak → reshapes the run).
* ▶ Запустить — ask for the ТЗ, then run the full pipeline with live
  progress and deliver the assembled code as a file.
* 📊 Последний результат — priorities / weak spots from the last run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import store

logger = logging.getLogger(__name__)

# --- Fix (buttons 31-32, problem 2): crelay:calib must not block the dispatcher ---
CALIBRATION_TIMEOUT = 300  # hard ceiling for the whole calibration phase, seconds
_CALIB_TASKS: set[asyncio.Task] = set()  # strong refs so GC won't kill bg tasks
# Guard: a double-tap on «Калибровать» must not spawn two parallel
# calibrations (double LLM spend + races over the stored calibration).
_CALIB_ACTIVE = False


def _spawn_bg(coro) -> asyncio.Task:
    """Run *coro* in the background, keeping a strong reference to the task.

    Bare ``asyncio.create_task(...)`` results may be garbage-collected
    mid-flight (documented asyncio pitfall) — every fire-and-forget task in
    this module must go through here.
    """
    task = asyncio.create_task(coro)
    _CALIB_TASKS.add(task)
    task.add_done_callback(_CALIB_TASKS.discard)
    return task


class RelayStates(StatesGroup):
    awaiting_tz = State()


async def _owner(obj) -> bool:
    try:
        from ...wizard import _is_owner
        uid = obj.from_user.id if obj.from_user else 0
        return _is_owner(uid)
    except Exception:  # noqa: BLE001
        return False


def _kb_home() -> InlineKeyboardMarkup:
    cal = store.get_calibration()
    cal_lbl = (
        f"🧪 Перекалибровать (пик≈{int(cal['peak_ratio']*100)}%)"
        if cal else "🧪 Калибровать модель"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=cal_lbl, callback_data="crelay:calib")],
        [InlineKeyboardButton(text="▶ Запустить конвейер", callback_data="crelay:run")],
        [InlineKeyboardButton(text="📊 Последний результат", callback_data="crelay:last")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main:menu")],
    ])


def _home_text() -> str:
    cal = store.get_calibration()
    if cal:
        lay = cal.get("layout", {})
        cal_line = (
            f"Калибровка: пик≈<b>{int(cal['peak_ratio']*100)}%</b>, "
            f"балл {int(cal.get('peak_score',0))} → частей {lay.get('parts','?')}, "
            f"кодеров {lay.get('coders','?')}."
        )
    else:
        cal_line = "Калибровка: <b>не сделана</b> (будет пик 20% по умолчанию)."
    return (
        "<b>🧭 Контекст — TEMPORAL RELAY</b>\n\n"
        "Многоагентный конвейер: собирает код из «пиков» модели и ротирует "
        "свежих агентов, чтобы контекст не засорялся.\n\n"
        f"{cal_line}\n\n"
        "Сначала — <b>калибровка</b> (тест модели). Она задаёт форму всего "
        "прогона. Потом — <b>запуск</b>: пришлёшь ТЗ, получишь собранный код."
    )


def build_context_relay_router() -> Router:
    router = Router(name="context_relay")

    @router.callback_query(F.data == "crelay:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_home_text(), parse_mode="HTML",
                                          reply_markup=_kb_home())

    @router.callback_query(F.data == "crelay:calib")
    async def _calib(query: CallbackQuery) -> None:
        global _CALIB_ACTIVE
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        if _CALIB_ACTIVE:
            await query.answer(
                "⏳ Калибровка уже идёт — дождись результата.", show_alert=True
            )
            return
        if query.message is None:
            await query.answer("Сообщение недоступно, открой меню заново.",
                               show_alert=True)
            return
        await query.answer("🧪 Калибрую…")
        msg = await query.message.answer("🧪 <b>Калибровка</b> — тестирую модель…",
                                         parse_mode="HTML")
        # Fix (buttons 31-32, problem 2): run_calibration() makes two LLM
        # calls with no timeout; awaiting it inline froze this handler for
        # minutes when the provider was slow/unreachable. Now it runs in a
        # background task under a hard timeout.
        _CALIB_ACTIVE = True
        _spawn_bg(_run_calibration_bg(msg))

    @router.callback_query(F.data == "crelay:run")
    async def _run(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.set_state(RelayStates.awaiting_tz)
        await query.answer()
        await query.message.answer(
            "▶ Пришли <b>ТЗ</b> одним сообщением — запущу конвейер "
            "(декомпозиция → кодеры → выбор золота → патчи → сборка).",
            parse_mode="HTML")

    @router.callback_query(F.data == "crelay:last")
    async def _last(query: CallbackQuery) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await query.answer()
        r = store.get_last_result()
        if not r:
            await query.message.answer("Пока нет прогонов.")
            return
        prio = ", ".join(r.get("priorities", []) or []) or "—"
        await query.message.answer(
            "<b>📊 Последний прогон</b>\n\n"
            f"Части: {', '.join(map(str, r.get('parts', []))) or '—'}\n"
            f"Приоритеты для следующего прогона: {prio}\n"
            f"Слабые места: {r.get('weak','') or '—'}\n"
            f"Размер кода: {r.get('code_len',0)} симв.",
            parse_mode="HTML")

    @router.message(RelayStates.awaiting_tz, F.text)
    async def _got_tz(message: Message, state: FSMContext) -> None:
        if not await _owner(message):
            return
        tz = (message.text or "").strip()
        await state.clear()
        if len(tz) < 10:
            await message.answer("ТЗ слишком короткое. Пришли развёрнутое ТЗ.")
            return
        # Run the pipeline in the background, streaming progress. Keep a
        # strong reference — a bare create_task() may be GC'd mid-run.
        _spawn_bg(_run_pipeline(message, tz))
        await message.answer("🚀 Запускаю конвейер, буду отчитываться по фазам…")

    return router


async def _run_calibration_bg(msg: Message) -> None:
    """Run calibration in the background with a hard timeout (fix, buttons 31-32)."""
    global _CALIB_ACTIVE
    try:
        from .calibration import run_calibration
        res = await asyncio.wait_for(run_calibration(), timeout=CALIBRATION_TIMEOUT)
        lay = res.get("layout", {})
        await msg.edit_text(
            "✅ <b>Калибровка готова</b>\n\n"
            f"Пик≈<b>{int(res['peak_ratio']*100)}%</b>, балл {int(res.get('peak_score',0))}\n"
            f"→ частей: {lay.get('parts')}, кодеров/часть: {lay.get('coders')}\n"
            f"<i>{res.get('note','')}</i>",
            parse_mode="HTML", reply_markup=_kb_home())
    except asyncio.TimeoutError:
        logger.warning("relay calibration timed out after %ss", CALIBRATION_TIMEOUT)
        with contextlib.suppress(Exception):
            await msg.edit_text(
                "❌ Калибровка не удалась: модель не ответила за "
                f"{CALIBRATION_TIMEOUT // 60} мин. "
                "Проверь ключ/сеть и попробуй ещё раз.",
                reply_markup=_kb_home())
    except Exception as e:  # noqa: BLE001
        logger.exception("relay calibration failed")
        with contextlib.suppress(Exception):
            await msg.edit_text(f"❌ Калибровка не удалась: {e}", reply_markup=_kb_home())
    finally:
        _CALIB_ACTIVE = False


async def _run_pipeline(message: Message, tz: str) -> None:
    from .pipeline import run_relay

    async def progress(text: str) -> None:
        with contextlib.suppress(Exception):
            await message.answer(text, parse_mode="HTML")

    try:
        result = await run_relay(tz, progress=progress)
    except Exception as e:  # noqa: BLE001
        logger.exception("relay pipeline failed")
        with contextlib.suppress(Exception):
            await message.answer(f"❌ Конвейер упал: {e}")
        return
    code = result.get("code", "")
    prio = ", ".join(result.get("priorities", []) or []) or "—"
    with contextlib.suppress(Exception):
        doc = BufferedInputFile(code.encode("utf-8"), filename="relay_result.txt")
        await message.answer_document(
            doc,
            caption=(
                "🧭 <b>Готовый код (собран из золота)</b>\n"
                f"Части: {', '.join(map(str, result.get('parts', [])))}\n"
                f"Приоритеты на след. прогон: {prio}"
            ),
            parse_mode="HTML",
        )
    # Artifacts: all 25 segments + gold + final code (for review).
    art = result.get("artifacts_zip", "")
    if art:
        import os
        with contextlib.suppress(Exception):
            if os.path.isfile(art):
                from aiogram.types import FSInputFile
                await message.answer_document(
                    FSInputFile(art, filename=os.path.basename(art)),
                    caption="📦 Артефакты прогона: все сегменты + золото + отчёт.",
                )
