"""Telegram UI for «🌐 Браузерная» — chooser + two engine panels.

Callbacks:
    main:browser / brws:hub      → chooser (две кнопки)
    brws:old:home/auto/prompt    → classic engine panel
    brws:new:home/auto/prompt    → browser-use panel
    brws:back                    → main menu

Classic path: NL → JSON steps (``_BROWSER_AI_SYSTEM``) → run_browser_macro
(honours 🕵️ Антидетект). New path: raw task → browser-use Agent.
"""

from __future__ import annotations

import contextlib
import html
import logging
import os
import re

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

from ...access import can_use
from . import is_auto_old, set_auto_old, is_auto_new, set_auto_new
from ..stealth import is_enabled as stealth_enabled

logger = logging.getLogger(__name__)


class BrowserPanelStates(StatesGroup):
    awaiting_prompt_old = State()
    awaiting_prompt_new = State()
    awaiting_cred_domain = State()
    awaiting_cred_login = State()
    awaiting_cred_password = State()
    awaiting_cf_url = State()


def _curl_old() -> bool:
    from .. import state as _st
    return bool(_st.get("browser", "curl_old", False))


def _curl_new() -> bool:
    from .. import state as _st
    return bool(_st.get("browser", "curl_new", False))


def _esc(s: str) -> str:
    return html.escape(s or "")


# ──────────────────────── classic NL → steps planner ────────────────────────
_BROWSER_AI_SYSTEM = (
    "You convert a user's free-form task (any language) into a JSON array of "
    "browser-automation steps for a stealth browser. Output ONLY a JSON array, "
    "no prose, no markdown fences.\n"
    "Allowed step objects:\n"
    '  {"op":"goto","url":"https://..."}      navigate (MUST be the first step)\n'
    '  {"op":"click","target":"visible text or CSS selector"}\n'
    '  {"op":"type","text":"...","field":"placeholder/name/id or visible label"}\n'
    '  {"op":"upload","file":"filename"}       file from session/attachments\n'
    '  {"op":"copy","selector":"CSS or text"}  read text from the page\n'
    '  {"op":"pause","seconds":2}\n'
    "Rules: first step is always goto with a full https URL. Keep it under 20 "
    "steps. Prefer visible button/link text for click. Use copy to extract any "
    "result the user asked for. Return strictly valid JSON."
)


def _parse_macro_from_llm(text: str) -> list[tuple]:
    """Parse the LLM's JSON step list into run_browser_macro tuples."""
    import json as _json

    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    if not raw.startswith("["):
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            raw = m.group(0)
    data = _json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("ожидался JSON-массив шагов")
    steps: list[tuple] = []
    for item in data[:20]:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "").lower()
        if op == "goto":
            url = str(item.get("url") or "").strip()
            if url:
                steps.append(("goto", url))
        elif op == "click":
            tgt = str(item.get("target") or item.get("selector") or "").strip()
            if tgt:
                steps.append(("click", tgt))
        elif op == "type":
            steps.append(("type", (str(item.get("text") or ""), str(item.get("field") or ""))))
        elif op == "upload":
            fn = str(item.get("file") or item.get("filename") or "").strip()
            if fn:
                steps.append(("upload", fn))
        elif op == "copy":
            sel = str(item.get("selector") or item.get("target") or "body").strip()
            steps.append(("copy", sel))
        elif op in ("pause", "wait", "sleep"):
            try:
                steps.append(("pause", float(item.get("seconds") or item.get("value") or 1)))
            except (TypeError, ValueError):
                steps.append(("pause", 1.0))
    if not steps or steps[0][0] != "goto":
        raise ValueError("первый шаг должен быть переходом на сайт (goto). Уточни URL в задаче.")
    return steps


# ──────────────────────────── keyboards / texts ─────────────────────────────
def _kb_hub() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Браузерная", callback_data="brws:old:home")],
        [InlineKeyboardButton(text="🆕 Браузерная новая", callback_data="brws:new:home")],
        [InlineKeyboardButton(text="🎯 Сценарии", callback_data="scn:menu")],
        [InlineKeyboardButton(text="🕵️ Антидетект", callback_data="main:stealth")],
        [InlineKeyboardButton(text="🖥 Железо и прокси", callback_data="hwx:home")],
        [InlineKeyboardButton(text="🔑 Логин/пароль для домена", callback_data="brws:creds")],
        [InlineKeyboardButton(text="🧠 Умное зрение", callback_data="main:smartvision")],
        [InlineKeyboardButton(text="⚡ curl_cffi Fetch (как Chrome)", callback_data="brws:cf")],
        [InlineKeyboardButton(text=("⚡ curl_cffi → Браузерная: ВКЛ 🟢" if _curl_old() else "⚡ curl_cffi → Браузерная: ВЫКЛ 🔴"), callback_data="brws:cf:told")],
        [InlineKeyboardButton(text=("⚡ curl_cffi → Браузерная новая: ВКЛ 🟢" if _curl_new() else "⚡ curl_cffi → Браузерная новая: ВЫКЛ 🔴"), callback_data="brws:cf:tnew")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="brws:back")],
    ])


_HUB_TEXT = (
    "🌐 <b>Браузерная</b>\n\n"
    "Выбери движок управления браузером:\n\n"
    "• <b>🌐 Браузерная</b> — твой классический движок (Camoufox → CloakBrowser "
    "→ nodriver → Playwright). Комбинируется с 🕵️ Антидетектом.\n"
    "• <b>🆕 Браузерная новая</b> — настоящий <code>browser-use</code> с GitHub "
    "(100k★): автономный агент сам решает, куда кликать.\n\n"
    "У обеих внутри: 🤖 Авто / 📝 Промт / ↩️ Назад.\n\n"
    "<b>Ярлыки stealth-настроек</b> (действуют на браузер):\n"
    "• <b>🕵️ Антидетект</b> — вкл/выкл отпечатка + live-check.\n"
    "• <b>🖥 Железо и прокси</b> — свой User-Agent / WebGL / прокси.\n"
    "• <b>🧠 Умное зрение</b> — точный детект рамок на скриншотах (approxPolyDP)."
)


def _kb_old() -> InlineKeyboardMarkup:
    auto = "🤖 Авто: ВКЛ 🟢" if is_auto_old() else "🤖 Авто: ВЫКЛ 🔴"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=auto, callback_data="brws:old:auto")],
        [InlineKeyboardButton(text="📝 Промт", callback_data="brws:old:prompt")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="brws:hub")],
    ])


def _kb_new() -> InlineKeyboardMarkup:
    auto = "🤖 Авто: ВКЛ 🟢" if is_auto_new() else "🤖 Авто: ВЫКЛ 🔴"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=auto, callback_data="brws:new:auto")],
        [InlineKeyboardButton(text="📝 Промт", callback_data="brws:new:prompt")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="brws:hub")],
    ])


def _render_old() -> str:
    auto = "включён 🟢" if is_auto_old() else "выключен 🔴"
    stealth = "ВКЛ 🟢" if stealth_enabled() else "ВЫКЛ 🔴"
    return (
        "🌐 <b>Браузерная (классическая)</b>\n\n"
        f"🤖 Авто-режим: <b>{auto}</b>\n"
        f"🕵️ Антидетект (стелс): <b>{stealth}</b>\n\n"
        "Движок проекта: NL → шаги → реальный браузер с человекоподобной мышью "
        "и фингерпринтом.\n\n"
        "• <b>🤖 Авто</b> — когда ВКЛ, любое сообщение в чат сразу исполняется "
        "этим движком.\n"
        "• <b>📝 Промт</b> — одна задача словами.\n"
        "• <b>↩️ Назад</b> — к выбору движка.\n\n"
        "<i>Комбинируется со стелсом: 🕵️ Антидетект ВКЛ → Dolphin-Anty-"
        "фингерпринт на каждом запуске.</i>"
    )


def _render_new() -> str:
    auto = "включён 🟢" if is_auto_new() else "выключен 🔴"
    return (
        "🆕 <b>Браузерная новая (browser-use)</b>\n\n"
        f"🤖 Авто-режим: <b>{auto}</b>\n\n"
        "Настоящий <code>browser-use</code> с GitHub (100k★): автономный агент "
        "сам читает страницу и решает, куда кликать. Использует твой LLM-мозг "
        "(/setup).\n\n"
        "• <b>🤖 Авто</b> — когда ВКЛ, любое сообщение в чат уходит агенту "
        "browser-use.\n"
        "• <b>📝 Промт</b> — одна задача словами.\n"
        "• <b>↩️ Назад</b> — к выбору движка.\n\n"
        "<i>Первый запуск может занять время — browser-use и Chromium ставятся "
        "автоматически. Стелс-тумблер действует на классический движок; "
        "browser-use использует свою маскировку.</i>"
    )


# ──────────────────────────────── runners ───────────────────────────────────
async def _curl_prefetch(message: Message, text: str) -> None:
    """Guarded curl_cffi (Chrome) pre-fetch preview of the first URL in text.
    Runs when the curl_cffi toggle is ON; never breaks the main run."""
    import re
    m = re.search(r"https?://\S+", text or "")
    if not m:
        return
    url = m.group(0).rstrip(").,")
    with contextlib.suppress(Exception):
        proxy = ""
        with contextlib.suppress(Exception):
            from .. import hwproxy
            proxy = hwproxy.effective_proxy() or ""
        from ..netfetch import chrome_fetch
        res = await chrome_fetch(url, proxy=proxy)
        if res.get("status"):
            await message.answer(
                f"⚡ curl_cffi предпросмотр ({res['engine']}): {_esc(url)} → "
                f"статус {res['status']}, {(_esc(res['title']) or '—')}"[:800],
                parse_mode="HTML")


# «Живая карта проекта»: @traced — no-op при LIVEMAP_ENABLED=0 (ТЗ 5.3).
from ..projectmap.tracer import traced as _lm_traced


@_lm_traced("browser_panel.run_classic")
async def _run_old(message: Message, instruction: str) -> None:
    """Classic engine: NL → steps → run_browser_macro (stealth-aware)."""
    instruction = (instruction or "").strip()
    if not instruction:
        await message.answer("Пустая инструкция.")
        return
    if _curl_old():
        await _curl_prefetch(message, instruction)

    prog = await message.answer("🧠 Составляю сценарий…")
    try:
        from .._llm import chat_once
        reply = await chat_once(_BROWSER_AI_SYSTEM, instruction, max_tokens=700, temperature=0.2)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await prog.edit_text(f"⚠️ ИИ-мозг недоступен: {_esc(str(exc))}")
        return
    try:
        steps = _parse_macro_from_llm(reply)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await prog.edit_text(
                f"⚠️ Не смог собрать сценарий: {_esc(str(exc))}\n\n"
                f"<i>Ответ ИИ:</i> <code>{_esc(reply[:300])}</code>", parse_mode="HTML")
        return

    pretty = "\n".join(f"{i+1}. {op} {('' if v is None else v)}" for i, (op, v) in enumerate(steps))
    stealth = "🟢 стелс ВКЛ" if stealth_enabled() else "🔴 стелс ВЫКЛ"
    with contextlib.suppress(Exception):
        await prog.edit_text(
            f"▶️ Выполняю {len(steps)} шаг(ов) в браузере ({stealth}):\n"
            f"<code>{_esc(pretty)}</code>", parse_mode="HTML")

    try:
        from ...config import DATA_DIR
        shot = str(DATA_DIR / "session" / "browser_panel.png")
        from ...workers.browser_clicker import run_browser_macro
        result = await run_browser_macro(steps, screenshot_path=shot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser_panel classic run failed")
        await message.answer(f"❌ Браузер не смог выполнить сценарий: {_esc(str(exc))}")
        return

    note = [f"Движок: {result.get('browser_type') or '—'}"]
    if result.get("error"):
        note.append(f"⚠️ {result['error']}")
    copied = result.get("copied_texts") or []
    if copied:
        note.append("\n📋 Со страницы:\n" + "\n".join(str(c) for c in copied)[:1500])
    caption = "✅ Готово.\n" + "\n".join(note)

    from aiogram.types import FSInputFile
    shot_path = result.get("screenshot")
    if shot_path and os.path.exists(shot_path):
        with contextlib.suppress(Exception):
            await message.answer_photo(FSInputFile(shot_path), caption=caption[:1024])
            return
    await message.answer(caption[:4000])


@_lm_traced("browser_panel.run_agent")
async def _run_new(message: Message, task: str) -> None:
    """browser-use engine: raw task → autonomous Agent."""
    task = (task or "").strip()
    if not task:
        await message.answer("Пустая задача.")
        return
    if _curl_new():
        await _curl_prefetch(message, task)

    prog = await message.answer(
        "🆕 Запускаю <b>browser-use</b>…\n"
        "<i>Если это первый запуск — ставлю пакет и Chromium, это может занять "
        "пару минут.</i>", parse_mode="HTML")
    try:
        from ...config import DATA_DIR
        shot = str(DATA_DIR / "session" / "browser_use.png")
        from ...workers.browser_use_agent import run_browser_use_task
        result = await run_browser_use_task(task, screenshot_path=shot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser-use run failed")
        with contextlib.suppress(Exception):
            await prog.edit_text(f"❌ browser-use не смог выполнить задачу: {_esc(str(exc))}")
        return

    if not result.get("ok"):
        with contextlib.suppress(Exception):
            await prog.edit_text(f"❌ {_esc(str(result.get('error') or 'неизвестная ошибка'))}")
        return

    final = str(result.get("final") or "—")[:3000]
    caption = f"✅ <b>browser-use готово.</b>\n\n{_esc(final)}"
    with contextlib.suppress(Exception):
        await prog.edit_text(caption[:4000], parse_mode="HTML")

    from aiogram.types import FSInputFile
    shot_path = result.get("screenshot")
    if shot_path and os.path.exists(shot_path):
        with contextlib.suppress(Exception):
            await message.answer_photo(FSInputFile(shot_path), caption="📸 Финальная страница")


async def _auto_active(message: Message) -> bool:
    """Filter: intercept plain chat only when SOME auto toggle is ON."""
    uid = message.from_user.id if message.from_user else None
    if not can_use(uid):
        return False
    return is_auto_new() or is_auto_old()


# ──────────────────────────────── router ────────────────────────────────────
def build_browser_panel_router() -> Router:
    router = Router(name="browser_panel")

    def _ok(q: CallbackQuery) -> bool:
        return can_use(q.from_user.id if q.from_user else None)

    @router.callback_query(F.data.in_({"main:browser", "brws:hub"}))
    async def cb_hub(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_text(_HUB_TEXT, parse_mode="HTML",
                                      reply_markup=_kb_hub(),
                                      disable_web_page_preview=True)
        await q.answer()

    # ── classic panel ──
    @router.callback_query(F.data == "brws:old:home")
    async def cb_old_home(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_text(_render_old(), parse_mode="HTML",
                                      reply_markup=_kb_old(),
                                      disable_web_page_preview=True)
        await q.answer()

    @router.callback_query(F.data == "brws:old:auto")
    async def cb_old_auto(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        set_auto_old(not is_auto_old())
        with contextlib.suppress(Exception):
            await q.message.edit_text(_render_old(), parse_mode="HTML",
                                      reply_markup=_kb_old(),
                                      disable_web_page_preview=True)
        await q.answer("Авто ВКЛ" if is_auto_old() else "Авто ВЫКЛ")

    @router.callback_query(F.data == "brws:old:prompt")
    async def cb_old_prompt(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(BrowserPanelStates.awaiting_prompt_old)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "📝 <b>Промт (классический движок)</b>\n\n"
                "Опиши задачу словами — переведу в сценарий и выполню в "
                "stealth-браузере.\n\n"
                "<i>Пример: зайди на example.com, введи в поиск «погода Москва» "
                "и забери результат.</i>\n\n(/cancel — отмена)",
                parse_mode="HTML", disable_web_page_preview=True)
        await q.answer()

    # ── browser-use panel ──
    @router.callback_query(F.data == "brws:new:home")
    async def cb_new_home(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.clear()
        with contextlib.suppress(Exception):
            await q.message.edit_text(_render_new(), parse_mode="HTML",
                                      reply_markup=_kb_new(),
                                      disable_web_page_preview=True)
        await q.answer()

    @router.callback_query(F.data == "brws:new:auto")
    async def cb_new_auto(q: CallbackQuery) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        set_auto_new(not is_auto_new())
        with contextlib.suppress(Exception):
            await q.message.edit_text(_render_new(), parse_mode="HTML",
                                      reply_markup=_kb_new(),
                                      disable_web_page_preview=True)
        await q.answer("Авто ВКЛ" if is_auto_new() else "Авто ВЫКЛ")

    @router.callback_query(F.data == "brws:new:prompt")
    async def cb_new_prompt(q: CallbackQuery, state: FSMContext) -> None:
        if not _ok(q):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(BrowserPanelStates.awaiting_prompt_new)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "📝 <b>Промт (browser-use)</b>\n\n"
                "Опиши задачу словами — автономный агент browser-use выполнит её "
                "сам.\n\n"
                "<i>Пример: найди на github репозиторий browser use и скажи "
                "сколько у него звёзд.</i>\n\n(/cancel — отмена)",
                parse_mode="HTML", disable_web_page_preview=True)
        await q.answer()

    @router.callback_query(F.data == "brws:back")
    async def cb_back(q: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        uid = q.from_user.id if q.from_user else None
        with contextlib.suppress(Exception):
            from ...wizard import _kb_main_after_claim
            await q.message.edit_text("Главное меню:", reply_markup=_kb_main_after_claim(uid))
        await q.answer()

    # ── prompt message handlers (explicit) ──
    @router.message(StateFilter(BrowserPanelStates.awaiting_prompt_old),
                    F.text & ~F.text.startswith("/"))
    async def msg_old(message: Message, state: FSMContext) -> None:
        if not can_use(message.from_user.id if message.from_user else None):
            return
        await state.clear()
        await _run_old(message, message.text or "")

    @router.message(StateFilter(BrowserPanelStates.awaiting_prompt_new),
                    F.text & ~F.text.startswith("/"))
    async def msg_new(message: Message, state: FSMContext) -> None:
        if not can_use(message.from_user.id if message.from_user else None):
            return
        await state.clear()
        await _run_new(message, message.text or "")

    # ── credentials per domain (login/password autofill) ──
    @router.callback_query(F.data == "brws:creds")
    async def cb_creds(q: CallbackQuery, state: FSMContext) -> None:
        if not can_use(q.from_user.id if q.from_user else None):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(BrowserPanelStates.awaiting_cred_domain)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "🔑 <b>Логин/пароль для домена</b>\n\n"
                "Учётки хранятся в профиле домена и подставляются командами "
                "<code>Логин:</code> / <code>Пароль:</code> в кликере (вводятся "
                "по-человечески, в теле макроса пароль не светится).\n\n"
                "Пришли <b>домен</b> (например <code>example.com</code>):",
                parse_mode="HTML")
        await q.answer()

    @router.message(StateFilter(BrowserPanelStates.awaiting_cred_domain),
                    F.text & ~F.text.startswith("/"))
    async def msg_cred_domain(message: Message, state: FSMContext) -> None:
        if not can_use(message.from_user.id if message.from_user else None):
            return
        dom = (message.text or "").strip().lower()
        # normalise: strip scheme/path, keep host
        from urllib.parse import urlparse
        if "//" in dom:
            dom = urlparse(dom).netloc or dom
        dom = dom.split("/")[0].split(":")[0]
        if not dom or "." not in dom:
            await message.answer("Нужен домен вида example.com. Попробуй ещё раз.")
            return
        await state.update_data(cred_domain=dom)
        await state.set_state(BrowserPanelStates.awaiting_cred_login)
        await message.answer(f"Домен: <code>{_esc(dom)}</code>\nТеперь пришли <b>логин</b>:",
                             parse_mode="HTML")

    @router.message(StateFilter(BrowserPanelStates.awaiting_cred_login),
                    F.text & ~F.text.startswith("/"))
    async def msg_cred_login(message: Message, state: FSMContext) -> None:
        if not can_use(message.from_user.id if message.from_user else None):
            return
        await state.update_data(cred_login=(message.text or "").strip())
        await state.set_state(BrowserPanelStates.awaiting_cred_password)
        await message.answer("Теперь пришли <b>пароль</b> (можно «-» чтобы не задавать):",
                             parse_mode="HTML")

    @router.message(StateFilter(BrowserPanelStates.awaiting_cred_password),
                    F.text & ~F.text.startswith("/"))
    async def msg_cred_password(message: Message, state: FSMContext) -> None:
        if not can_use(message.from_user.id if message.from_user else None):
            return
        data = await state.get_data()
        await state.clear()
        dom = data.get("cred_domain", "")
        login = data.get("cred_login", "")
        pwd = (message.text or "").strip()
        if pwd in ("-", "—"):
            pwd = ""
        _set_domain_credentials(dom, login, pwd)
        masked = ("•" * len(pwd)) if pwd else "(пусто)"
        await message.answer(
            f"✅ Сохранено для <code>{_esc(dom)}</code>\n"
            f"Логин: <code>{_esc(login)}</code>\nПароль: {masked}\n\n"
            "В макросе используй: <code>Логин: #поле</code> / <code>Пароль: #поле</code>.",
            parse_mode="HTML")

    # ── curl_cffi (Chrome-отпечаток, обход Cloudflare) — отдельно от карт ──
    @router.callback_query(F.data == "brws:cf:told")
    async def cb_cf_told(q: CallbackQuery) -> None:
        if not can_use(q.from_user.id if q.from_user else None):
            await q.answer("Нет доступа.", show_alert=True)
            return
        from .. import state as _st
        new = not _curl_old()
        _st.set_("browser", "curl_old", new)
        with contextlib.suppress(Exception):
            await q.message.edit_reply_markup(reply_markup=_kb_hub())
        await q.answer("curl_cffi → Браузерная: " + ("ВКЛ" if new else "ВЫКЛ"))

    @router.callback_query(F.data == "brws:cf:tnew")
    async def cb_cf_tnew(q: CallbackQuery) -> None:
        if not can_use(q.from_user.id if q.from_user else None):
            await q.answer("Нет доступа.", show_alert=True)
            return
        from .. import state as _st
        new = not _curl_new()
        _st.set_("browser", "curl_new", new)
        with contextlib.suppress(Exception):
            await q.message.edit_reply_markup(reply_markup=_kb_hub())
        await q.answer("curl_cffi → Браузерная новая: " + ("ВКЛ" if new else "ВЫКЛ"))

    @router.callback_query(F.data == "brws:cf")
    async def cb_cf(q: CallbackQuery, state: FSMContext) -> None:
        if not can_use(q.from_user.id if q.from_user else None):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await state.set_state(BrowserPanelStates.awaiting_cf_url)
        with contextlib.suppress(Exception):
            await q.message.answer(
                "⚡ Пришли <b>URL</b> — загружу через <code>curl_cffi</code> с отпечатком Chrome "
                "(проходит Cloudflare как настоящий браузер, без запуска браузера):",
                parse_mode="HTML")
        await q.answer()

    @router.message(StateFilter(BrowserPanelStates.awaiting_cf_url),
                    F.text & ~F.text.startswith("/"))
    async def msg_cf_url(message: Message, state: FSMContext) -> None:
        if not can_use(message.from_user.id if message.from_user else None):
            return
        await state.clear()
        url = (message.text or "").strip()
        prog = await message.answer("⚡ Загружаю через curl_cffi…")
        proxy = ""
        with contextlib.suppress(Exception):
            from .. import hwproxy
            proxy = hwproxy.effective_proxy() or ""
        from ..netfetch import chrome_fetch
        res = await chrome_fetch(url, proxy=proxy)
        if res.get("status"):
            txt = (f"⚡ <b>curl_cffi</b> · {res['engine']}\n"
                   f"URL: {_esc(url)}\nСтатус: <b>{res['status']}</b>\n"
                   f"Title: {_esc(res['title']) or '—'}\n\n{_esc(res['snippet']) or '—'}")
        else:
            txt = f"❌ Не удалось загрузить: {_esc(res.get('error', ''))}"
        with contextlib.suppress(Exception):
            await prog.edit_text(txt[:3900], parse_mode="HTML")

    # ── auto path: plain chat (no FSM state) when some auto is ON ──
    @router.message(StateFilter(None), F.text & ~F.text.startswith("/"), _auto_active)
    async def msg_auto(message: Message, state: FSMContext) -> None:
        if is_auto_new():
            await _run_new(message, message.text or "")
        elif is_auto_old():
            await _run_old(message, message.text or "")

    return router


def _set_domain_credentials(domain: str, login: str, password: str) -> None:
    """Store login/password into browser.domain_profiles[domain], preserving the
    existing fingerprint. If the domain has no profile yet, generate a full
    stealth fingerprint (same shape browser_clicker uses) so we never leave the
    domain with an empty/weak fingerprint."""
    import random
    import secrets
    from .. import state as addon_state
    profiles = addon_state.get("browser", "domain_profiles", {}) or {}
    if not isinstance(profiles, dict):
        profiles = {}
    prof = profiles.get(domain)
    if not isinstance(prof, dict):
        webgl_vendors = ["Google Inc. (Intel)", "Google Inc. (NVIDIA)", "Apple Inc."]
        webgl_renderers = [
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0)",
            "Apple GPU",
        ]
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        idx = random.randint(0, len(webgl_vendors) - 1)
        prof = {
            "seed": f"seed_{domain}_{secrets.token_hex(6)}",
            "user_agent": random.choice(user_agents),
            "proxy": "",
            "webgl_vendor": webgl_vendors[idx],
            "webgl_renderer": webgl_renderers[idx],
            "login": "",
            "password": "",
        }
    prof["login"] = login
    prof["password"] = password
    profiles[domain] = prof
    addon_state.set_("browser", "domain_profiles", profiles)
