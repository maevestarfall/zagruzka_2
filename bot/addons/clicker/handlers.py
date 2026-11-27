"""Telegram UI for «🤖 Макрос действий (Кликер)» (clk:* callbacks)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...access import can_use
from . import describe_step, parse_macro
from .image_parser import detect_colored_boxes, steps_to_macro_text
from .recorder import INSTRUCTIONS, RECORDER_FILENAME, RECORDER_USERSCRIPT

logger = logging.getLogger(__name__)

_MAX_SHOTS = 5


class ClickerStates(StatesGroup):
    awaiting_macro = State()
    awaiting_screenshots = State()
    awaiting_url = State()
    awaiting_macro_real = State()
    awaiting_real_url = State()
    awaiting_file = State()


def _esc(s: str) -> str:
    return html.escape(s or "")


def _kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить макрос", callback_data="clk:run")],
        [InlineKeyboardButton(text="🌐 Запустить в браузере (по-настоящему)", callback_data="clk:runreal")],
        [InlineKeyboardButton(text="📎 Прислать файл для макроса", callback_data="clk:file")],
        [InlineKeyboardButton(text="⏺ Записать действия", callback_data="clk:rec")],
        [InlineKeyboardButton(text="📸 Визуальная запись (по скриншотам)", callback_data="clk:visual")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="misc:banya")],
    ])


def _kb_visual() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово (создать макрос)", callback_data="clk:visual:done")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="clk:home")],
    ])


def _kb_run_choice() -> InlineKeyboardMarkup:
    """After a macro is built: pick how to run it. CloakBrowser is the default real engine."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Симуляция (в чате)", callback_data="clk:vrun:sim")],
        [InlineKeyboardButton(text="🌐 Реальный кликер: CloakBrowser 🟢", callback_data="clk:vrun:cloak")],
        [InlineKeyboardButton(text="🆕 Реальный кликер: browser-use", callback_data="clk:vrun:bu")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="clk:home")],
    ])


def _kb_rec() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Через сайт", callback_data="clk:rec:site")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="clk:home")],
    ])


_HOME_TEXT = (
    "🤖 <b>Макрос действий (Кликер)</b>\n\n"
    "Задаёшь последовательность действий, бот пошагово их проигрывает в чате "
    "(⏳ → ✅), выдерживая паузы.\n\n"
    "<b>Формат (каждая команда с новой строки):</b>\n"
    "<code>Клик: Запустить проверку\n"
    "Пауза: 3\n"
    "Скопировать: Номер карты\n"
    "Клик: Очистить</code>\n\n"
    "• <b>▶️ Запустить макрос</b> — пришли список команд.\n"
    "• <b>⏺ Записать действия</b> — записать клики прямо на сайте и получить "
    "готовый макрос.\n"
    "• <b>📸 Визуальная запись</b> — пришли скриншоты с 🟥 (клик) и 🟩 (копировать) "
    "рамками, бот сам соберёт макрос (OpenCV + OCR)."
)


def build_clicker_router() -> Router:
    router = Router(name="clicker")

    def _ok(q: CallbackQuery) -> bool:
        return can_use(q.from_user.id if q.from_user else None)

    @router.callback_query(F.data == "clk:home")
    async def cb_home(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_text(_HOME_TEXT, reply_markup=_kb_home())
        await q.answer()

    @router.callback_query(F.data == "clk:rec")
    async def cb_rec(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        with contextlib.suppress(Exception):
            await q.message.edit_text(
                "⏺ <b>Записать действия</b>\n\n"
                "Выбери способ записи. Сейчас доступна запись на реальном сайте "
                "через юзерскрипт (Tampermonkey): запись по F12/F9, копирование "
                "по Ctrl+C, воспроизведение и экспорт макроса для бота.",
                reply_markup=_kb_rec())
        await q.answer()

    @router.callback_query(F.data == "clk:rec:site")
    async def cb_rec_site(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await q.answer("Готовлю скрипт…")
        with contextlib.suppress(Exception):
            await q.message.answer(INSTRUCTIONS, reply_markup=_kb_rec())
        # Hand over the userscript as a downloadable file.
        with contextlib.suppress(Exception):
            doc = BufferedInputFile(
                RECORDER_USERSCRIPT.encode("utf-8"), filename=RECORDER_FILENAME)
            await q.message.answer_document(
                doc, caption=f"📜 {RECORDER_FILENAME} — установи в Tampermonkey")

    @router.callback_query(F.data == "clk:run")
    async def cb_run(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(ClickerStates.awaiting_macro)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "Пришли макрос (каждая команда с новой строки):\n\n"
                "<code>Клик: Запустить проверку\nПауза: 3\n"
                "Скопировать: Номер карты\nКлик: Очистить</code>")
        await q.answer()

    @router.message(StateFilter(ClickerStates.awaiting_macro))
    async def msg_macro(m: Message, state: FSMContext) -> None:
        await state.clear()
        steps = parse_macro(m.text or "")
        if not steps:
            await m.answer("Пустой макрос. Открой ещё раз через 🧖 баня → "
                           "🤖 Макрос действий (Кликер).")
            return
        await _run_macro(m, steps)

    # ───────────────────── visual recorder (screenshots) ─────────────────────
    @router.callback_query(F.data == "clk:visual")
    async def cb_visual(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(ClickerStates.awaiting_screenshots)
        await state.update_data(vsteps=[], vshots=0)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "📸 <b>Визуальная запись (по скриншотам)</b>\n\n"
                "1. Сделай скриншот экрана/сайта на телефоне.\n"
                "2. В редакторе картинок обведи:\n"
                "   • <b>🟥 красной</b> рамкой — кнопку, на которую нужно "
                "<b>нажать</b> (шаг «Клик»).\n"
                "   • <b>🟩 зелёной</b> рамкой — текст/область, которую нужно "
                "<b>скопировать</b> (шаг «Скопировать»).\n"
                f"3. Пришли размеченные скриншоты (до {_MAX_SHOTS} штук) — "
                "по очереди.\n"
                "4. Нажми «✅ Готово (создать макрос)».\n\n"
                "<i>Бот распознает рамки (OpenCV) и текст внутри (OCR rus+eng) и "
                "соберёт макрос.</i>",
                parse_mode="HTML", reply_markup=_kb_visual())
        await q.answer()

    @router.message(StateFilter(ClickerStates.awaiting_screenshots), F.photo)
    async def msg_visual_shot(m: Message, state: FSMContext) -> None:
        data = await state.get_data()
        shots = int(data.get("vshots", 0))
        vsteps: list = list(data.get("vsteps", []))
        if shots >= _MAX_SHOTS:
            await m.answer(f"Уже принято {_MAX_SHOTS} скриншотов — жми «✅ Готово».",
                           reply_markup=_kb_visual())
            return
        try:
            from ...config import DATA_DIR
            sess = os.path.join(str(DATA_DIR), "session")
            os.makedirs(sess, exist_ok=True)
            uid = m.from_user.id if m.from_user else 0
            path = os.path.join(sess, f"visual_{uid}_{shots}.png")
            await m.bot.download(m.photo[-1], destination=path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("visual: download failed: %s", exc)
            await m.answer(f"⚠️ Не смог скачать скриншот: {_esc(str(exc))}")
            return

        try:
            from .. import state as addon_state
            smart = bool(addon_state.get("clicker", "smart_vision", True))
        except Exception:  # noqa: BLE001
            smart = True
        try:
            found = detect_colored_boxes(path, smart=smart)
        except Exception as exc:  # noqa: BLE001
            logger.exception("visual: detect failed")
            await m.answer(f"⚠️ Ошибка распознавания: {_esc(str(exc))}")
            return

        vsteps.extend([list(s) for s in found])
        shots += 1
        await state.update_data(vsteps=vsteps, vshots=shots)

        if found:
            preview = "\n".join(
                f"  {'👆 Клик' if k == 'click' else '📋 Скопировать'}: {t}" for k, t in found)
        else:
            preview = "  (рамок не найдено — проверь, что они ярко-красные/зелёные)"
        await m.answer(
            f"✅ Скриншот {shots}/{_MAX_SHOTS} обработан. Найдено шагов: {len(found)}\n"
            f"{preview}\n\nПришли ещё или нажми «✅ Готово».",
            reply_markup=_kb_visual())

    @router.callback_query(F.data == "clk:visual:done")
    async def cb_visual_done(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        data = await state.get_data()
        vsteps = list(data.get("vsteps", []))
        if not vsteps:
            await q.answer("Пока нет ни одного шага — пришли размеченные скриншоты.",
                           show_alert=True)
            return
        macro_text = steps_to_macro_text([(k, t) for k, t in vsteps])
        # Keep the built steps for the run stage.
        await state.set_state(None)
        await state.update_data(vsteps=vsteps)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "🧩 <b>Макрос собран</b> · шагов: "
                f"{len(vsteps)}\n\n<code>{_esc(macro_text)}</code>\n\n"
                "Как запустить?",
                parse_mode="HTML", reply_markup=_kb_run_choice())
        await q.answer()

    @router.callback_query(F.data == "clk:vrun:sim")
    async def cb_vrun_sim(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        data = await state.get_data()
        vsteps = list(data.get("vsteps", []))
        await state.clear()
        if not vsteps:
            await q.answer("Макрос пуст.", show_alert=True)
            return
        await q.answer("Запускаю симуляцию…")
        await _run_macro(q.message, _vsteps_to_sim(vsteps))

    @router.callback_query(F.data.in_({"clk:vrun:cloak", "clk:vrun:bu"}))
    async def cb_vrun_real(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        data = await state.get_data()
        vsteps = list(data.get("vsteps", []))
        if not vsteps:
            await q.answer("Макрос пуст.", show_alert=True)
            return
        engine = "cloak" if q.data.endswith("cloak") else "bu"
        await state.set_state(ClickerStates.awaiting_url)
        await state.update_data(vsteps=vsteps, vengine=engine)
        name = "CloakBrowser" if engine == "cloak" else "browser-use"
        with contextlib.suppress(Exception):
            await q.message.answer(
                f"🌐 <b>Реальный кликер · {name}</b>\n\n"
                "Пришли <b>ссылку на сайт</b> (URL), где нужно выполнить эти шаги.\n"
                "<i>Например: https://example.com</i>\n\n(/cancel — отмена)",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖️ Отмена", callback_data="clk:vcancel")],
                ]))
        await q.answer()

    @router.callback_query(F.data == "clk:vcancel")
    async def cb_vcancel(q: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.answer("Отменено. Открой заново через 🧖 баня → 🤖 Макрос действий.")
        await q.answer("Отменено")

    @router.message(StateFilter(ClickerStates.awaiting_url), Command("cancel"))
    async def msg_visual_url_cancel(m: Message, state: FSMContext) -> None:
        await state.clear()
        await m.answer("Отменено.")

    @router.message(StateFilter(ClickerStates.awaiting_url), F.text & ~F.text.startswith("/"))
    async def msg_visual_url(m: Message, state: FSMContext) -> None:
        data = await state.get_data()
        vsteps = list(data.get("vsteps", []))
        engine = data.get("vengine", "cloak")
        await state.clear()
        url = (m.text or "").strip()
        if not url.startswith("http"):
            await m.answer("Нужна ссылка вида https://… Открой визуальную запись заново.")
            return
        if not vsteps:
            await m.answer("Макрос пуст.")
            return
        if engine == "bu":
            await _run_browser_use(m, _vsteps_to_task(vsteps, url))
        else:
            await _run_cloak(m, _vsteps_to_tuples(vsteps, url))

    # ───────────────── текстовый макрос: реальный запуск в браузере ─────────────────
    @router.callback_query(F.data == "clk:runreal")
    async def cb_runreal(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(ClickerStates.awaiting_macro_real)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "🌐 <b>Запустить макрос в браузере</b>\n\n"
                "Пришли макрос. Поддерживаются команды:\n"
                "<code>Перейти: https://site.com\n"
                "Клик: Войти\n"
                "Ввести: любой текст в Поле\n"
                "Логин: #username\n"
                "Пароль: #password\n"
                "Файл: аватар.png\n"
                "Скопировать: Результат\n"
                "Пауза: 2</code>\n\n"
                "«Логин»/«Пароль» подставляют сохранённые учётки домена (задать — "
                "в настройках домена). Файлы пришли через «📎 Прислать файл». "
                "Если в макросе нет «Перейти:», я спрошу ссылку отдельно.",
                parse_mode="HTML")
        await q.answer()

    @router.message(StateFilter(ClickerStates.awaiting_macro_real))
    async def msg_macro_real(m: Message, state: FSMContext) -> None:
        steps = parse_macro(m.text or "")
        if not steps:
            await state.clear()
            await m.answer("Пустой макрос.")
            return
        await state.update_data(rsteps=steps)
        await state.set_state(None)
        with contextlib.suppress(Exception):
            preview = "\n".join(f"{i}. {describe_step(s)}" for i, s in enumerate(steps, 1))
            await m.answer(
                f"🧩 <b>Макрос ({len(steps)} шаг.)</b>\n\n{_esc(preview)}\n\nЧем выполнить?",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🌐 CloakBrowser 🟢", callback_data="clk:rrun:cloak")],
                    [InlineKeyboardButton(text="🆕 browser-use", callback_data="clk:rrun:bu")],
                    [InlineKeyboardButton(text="✖️ Отмена", callback_data="clk:vcancel")],
                ]))

    @router.callback_query(F.data.in_({"clk:rrun:cloak", "clk:rrun:bu"}))
    async def cb_rrun(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        data = await state.get_data()
        rsteps = list(data.get("rsteps", []))
        if not rsteps:
            await q.answer("Макрос пуст.", show_alert=True)
            return
        engine = "cloak" if q.data.endswith("cloak") else "bu"
        has_goto = any(s.get("op") == "goto" for s in rsteps)
        if has_goto:
            await state.clear()
            await q.answer("Запускаю…")
            if engine == "bu":
                await _run_browser_use(q.message, _steps_to_task(rsteps, ""))
            else:
                await _run_cloak(q.message, _steps_to_tuples(rsteps, ""))
            return
        # No goto in macro → ask for the URL.
        await state.set_state(ClickerStates.awaiting_real_url)
        await state.update_data(rsteps=rsteps, rengine=engine)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "Пришли <b>ссылку на сайт</b> (URL), где выполнить макрос.\n"
                "<i>Например: https://example.com</i>", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖️ Отмена", callback_data="clk:vcancel")]]))
        await q.answer()

    @router.message(StateFilter(ClickerStates.awaiting_real_url), F.text & ~F.text.startswith("/"))
    async def msg_real_url(m: Message, state: FSMContext) -> None:
        data = await state.get_data()
        rsteps = list(data.get("rsteps", []))
        engine = data.get("rengine", "cloak")
        await state.clear()
        url = (m.text or "").strip()
        if not url.startswith("http"):
            await m.answer("Нужна ссылка вида https://… Открой запуск заново.")
            return
        if not rsteps:
            await m.answer("Макрос пуст.")
            return
        if engine == "bu":
            await _run_browser_use(m, _steps_to_task(rsteps, url))
        else:
            await _run_cloak(m, _steps_to_tuples(rsteps, url))

    # ───────────────── приём файла для загрузки на сайт ─────────────────
    @router.callback_query(F.data == "clk:file")
    async def cb_file(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(ClickerStates.awaiting_file)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "📎 <b>Прислать файл для макроса</b>\n\n"
                "Пришли документ или фото. Я сохраню его, а ты потом сошлёшься на него "
                "в макросе командой <code>Файл: имя_файла</code>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖️ Отмена", callback_data="clk:vcancel")]]))
        await q.answer()

    @router.message(StateFilter(ClickerStates.awaiting_file), F.document | F.photo)
    async def msg_file(m: Message, state: FSMContext) -> None:
        await state.clear()
        try:
            from ...config import DATA_DIR
            att = os.path.join(str(DATA_DIR), "attachments")
            os.makedirs(att, exist_ok=True)
            if m.document:
                fname = m.document.file_name or f"file_{m.document.file_unique_id}"
                file_obj = m.document
            else:
                fname = f"photo_{m.photo[-1].file_unique_id}.jpg"
                file_obj = m.photo[-1]
            fname = os.path.basename(fname)
            path = os.path.join(att, fname)
            await m.bot.download(file_obj, destination=path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("clicker file save failed: %s", exc)
            await m.answer(f"⚠️ Не смог сохранить файл: {_esc(str(exc))}")
            return
        await m.answer(
            f"✅ Файл сохранён: <code>{_esc(fname)}</code>\n\n"
            f"В макросе используй: <code>Файл: {_esc(fname)}</code>",
            parse_mode="HTML", reply_markup=_kb_home())

    return router


# «Живая карта проекта»: @traced — no-op при LIVEMAP_ENABLED=0 (ТЗ 5.3).
from ..projectmap.tracer import traced as _lm_traced


@_lm_traced("clicker.run_macro")
async def _run_macro(m: Message, steps: list[dict]) -> None:
    """Walk the steps as a visual simulation, updating one message."""
    total = len(steps)
    header = f"🤖 <b>Выполняю макрос</b> · {total} шаг(ов)\n\n"
    lines = [f"⏳ {i}. {describe_step(s)}" for i, s in enumerate(steps, 1)]
    msg = await m.answer(header + "\n".join(lines))

    async def _render() -> None:
        with contextlib.suppress(Exception):
            await msg.edit_text(header + "\n".join(lines))

    for i, s in enumerate(steps):
        # mark current step as running
        lines[i] = f"▶️ {i + 1}. {describe_step(s)}"
        await _render()
        if s["op"] == "pause":
            await asyncio.sleep(min(float(s.get("seconds", 1)), 120.0))
        else:
            await asyncio.sleep(0.7)  # visual beat
        lines[i] = f"✅ {i + 1}. {describe_step(s)}"
        await _render()

    with contextlib.suppress(Exception):
        await msg.edit_text(header + "\n".join(lines) + "\n\n<b>Готово ✅</b>")


# ─────────────────── visual-macro helpers & real executors ───────────────────
def _vsteps_to_sim(vsteps: list) -> list[dict]:
    """[(kind, text), ...] → simulation step dicts."""
    out: list[dict] = []
    for kind, text in vsteps:
        if kind == "copy":
            out.append({"op": "copy", "label": text})
        else:
            out.append({"op": "click", "label": text})
    return out


def _vsteps_to_tuples(vsteps: list, url: str) -> list[tuple]:
    """[(kind, text), ...] + url → run_browser_macro tuples (goto first)."""
    steps: list[tuple] = [("goto", url)]
    for kind, text in vsteps:
        if kind == "copy":
            steps.append(("copy", text))
        else:
            steps.append(("click", text))
    return steps


def _vsteps_to_task(vsteps: list, url: str) -> str:
    """[(kind, text), ...] + url → free-text task for the browser-use agent."""
    lines = [f"Open {url}."]
    for kind, text in vsteps:
        if kind == "copy":
            lines.append(f"Copy/read the text of '{text}'.")
        else:
            lines.append(f"Click the button labelled '{text}'.")
    lines.append("Then report what you copied.")
    return " ".join(lines)


def _steps_to_tuples(steps: list[dict], url: str = "") -> list[tuple]:
    """parse_macro step dicts → run_browser_macro tuples.

    Prepends a ``goto`` from *url* only if the macro has no goto of its own.
    """
    out: list[tuple] = []
    has_goto = any(s.get("op") == "goto" for s in steps)
    if url and not has_goto:
        out.append(("goto", url))
    for s in steps:
        op = s.get("op")
        if op == "goto":
            out.append(("goto", str(s.get("url", ""))))
        elif op == "click":
            out.append(("click", str(s.get("label", ""))))
        elif op == "copy":
            out.append(("copy", str(s.get("label", ""))))
        elif op == "type":
            out.append(("type", (str(s.get("text", "")), str(s.get("field", "")))))
        elif op == "type_login":
            out.append(("type_login", str(s.get("field", ""))))
        elif op == "type_password":
            out.append(("type_password", str(s.get("field", ""))))
        elif op == "upload":
            out.append(("upload", str(s.get("file", ""))))
        elif op == "pause":
            out.append(("pause", float(s.get("seconds", 1))))
    return out


def _steps_to_task(steps: list[dict], url: str = "") -> str:
    """parse_macro step dicts → free-text task for the browser-use agent."""
    lines: list[str] = []
    goto = next((s.get("url") for s in steps if s.get("op") == "goto"), "") or url
    if goto:
        lines.append(f"Open {goto}.")
    for s in steps:
        op = s.get("op")
        if op == "click":
            lines.append(f"Click '{s.get('label', '')}'.")
        elif op == "type":
            lines.append(f"Type '{s.get('text', '')}' into the field '{s.get('field', '') or 'input'}'.")
        elif op == "type_login":
            lines.append(f"Type the saved login into the field '{s.get('field', '') or 'login'}'.")
        elif op == "type_password":
            lines.append(f"Type the saved password into the field '{s.get('field', '') or 'password'}'.")
        elif op == "upload":
            lines.append(f"Upload the file '{s.get('file', '')}' into the file input.")
        elif op == "copy":
            lines.append(f"Copy/read the text of '{s.get('label', '')}'.")
        elif op == "pause":
            lines.append(f"Wait {s.get('seconds', 1):g} seconds.")
    lines.append("Then report what you copied.")
    return " ".join(lines)


async def _run_cloak(m: Message, steps: list) -> None:
    """Run pre-built run_browser_macro tuples via the CloakBrowser engine."""
    url = ""
    for op, val in steps:
        if op == "goto":
            url = str(val)
            break
    prog = await m.answer(f"🌐 CloakBrowser: выполняю {len(steps)} шаг(ов)"
                          + (f" на {_esc(url)}" if url else "") + "…")
    try:
        from ...config import DATA_DIR
        shot = os.path.join(str(DATA_DIR), "session", "clicker_visual.png")
        from ...workers.browser_clicker import run_browser_macro
        result = await run_browser_macro(steps, screenshot_path=shot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("clicker cloak run failed")
        with contextlib.suppress(Exception):
            await prog.edit_text(f"❌ Не удалось выполнить: {_esc(str(exc))}")
        return

    note = [f"Движок: {result.get('browser_type') or '—'}"]
    if result.get("error"):
        note.append(f"⚠️ {result['error']}")
    copied = result.get("copied_texts") or []
    if copied:
        pretty = []
        for c in copied:
            if isinstance(c, (tuple, list)) and len(c) == 2:
                pretty.append(f"• {c[0]}: {c[1]}")
            else:
                pretty.append(f"• {c}")
        note.append("\n📋 Со страницы:\n" + "\n".join(pretty)[:1500])
    caption = "✅ Готово.\n" + "\n".join(note)

    shot_path = result.get("screenshot")
    if shot_path and os.path.exists(shot_path):
        with contextlib.suppress(Exception):
            await m.answer_photo(FSInputFile(shot_path), caption=caption[:1024])
            return
    with contextlib.suppress(Exception):
        await prog.edit_text(caption[:4000])


async def _run_browser_use(m: Message, task: str) -> None:
    """Run a pre-built free-text task via the browser-use agent."""
    prog = await m.answer(
        "🆕 Запускаю <b>browser-use</b>…\n"
        "<i>Первый запуск может ставить пакет и Chromium — это займёт время.</i>",
        parse_mode="HTML")
    try:
        from ...config import DATA_DIR
        shot = os.path.join(str(DATA_DIR), "session", "clicker_visual_bu.png")
        from ...workers.browser_use_agent import run_browser_use_task
        result = await run_browser_use_task(task, screenshot_path=shot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("clicker visual browser-use run failed")
        with contextlib.suppress(Exception):
            await prog.edit_text(f"❌ browser-use не смог выполнить: {_esc(str(exc))}")
        return

    if not result.get("ok"):
        with contextlib.suppress(Exception):
            await prog.edit_text(f"❌ {_esc(str(result.get('error') or 'неизвестная ошибка'))}")
        return

    final = str(result.get("final") or "—")[:3000]
    with contextlib.suppress(Exception):
        await prog.edit_text(f"✅ <b>browser-use готово.</b>\n\n{_esc(final)}"[:4000],
                             parse_mode="HTML")
    shot_path = result.get("screenshot")
    if shot_path and os.path.exists(shot_path):
        with contextlib.suppress(Exception):
            await m.answer_photo(FSInputFile(shot_path), caption="📸 Финальная страница")
