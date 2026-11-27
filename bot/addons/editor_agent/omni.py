"""«🎬 Омни от Гугл» — Omni montage section for the Editor Agent.

Self-contained sub-screens wired onto the editor_agent router:

* Способ входа — API или браузер (single-select).
* Аккаунт Омни — логин+пароль ИЛИ API-ключ.
* Доп. настройки входа — железо (fingerprint preset) + прокси/VPN,
  переиспользуются из аддона ``issuance`` чтобы не дублировать пресеты.
* Промты — до 10 динамических промтов (добавить / убрать).
* Омни монтаж — берёт видео, выбранное из «💾 Хранилища», прогоняет
  его через Omni-конвейер с настроенными промтами.

State лежит в ``_settings.addons.omni.*`` и переживает рестарт.
"""

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

from .. import state as addon_state

logger = logging.getLogger(__name__)

_NS = "omni"
MAX_PROMPTS = 10
_LOGIN_METHODS = ("api", "browser")
_METHOD_LABEL = {"api": "🔌 API", "browser": "🌐 Браузер"}


def _esc(s: object) -> str:
    return html.escape(str(s or ""))


# ---- FSM ----------------------------------------------------------------


class OmniStates(StatesGroup):
    awaiting_login = State()
    awaiting_password = State()
    awaiting_api = State()
    awaiting_proxy = State()
    awaiting_prompt = State()


# ---- store helpers ------------------------------------------------------


def get_login_method() -> str:
    v = str(addon_state.get(_NS, "login_method", "api") or "api")
    return v if v in _LOGIN_METHODS else "api"


def set_login_method(v: str) -> None:
    addon_state.set_(_NS, "login_method", v if v in _LOGIN_METHODS else "api")


def get_login() -> str:
    return str(addon_state.get(_NS, "login", "") or "")


def set_login(v: str) -> None:
    addon_state.set_(_NS, "login", v or "")


def get_password() -> str:
    return str(addon_state.get(_NS, "password", "") or "")


def set_password(v: str) -> None:
    addon_state.set_(_NS, "password", v or "")


def get_api_key() -> str:
    return str(addon_state.get(_NS, "api_key", "") or "")


def set_api_key(v: str) -> None:
    addon_state.set_(_NS, "api_key", v or "")


def get_hardware() -> str:
    return str(addon_state.get(_NS, "hardware", "") or "")


def set_hardware(v: str) -> None:
    addon_state.set_(_NS, "hardware", v or "")


def get_prompts() -> list[str]:
    v = addon_state.get(_NS, "prompts", [])
    return [str(x) for x in v][:MAX_PROMPTS] if isinstance(v, list) else []


def set_prompts(lst: list[str]) -> None:
    addon_state.set_(_NS, "prompts", [str(x) for x in lst][:MAX_PROMPTS])


def add_prompt(text: str) -> bool:
    lst = get_prompts()
    if text and len(lst) < MAX_PROMPTS:
        lst.append(text.strip())
        set_prompts(lst)
        return True
    return False


def remove_prompt(idx: int) -> None:
    lst = get_prompts()
    if 0 <= idx < len(lst):
        lst.pop(idx)
        set_prompts(lst)


def get_selected_video() -> str:
    return str(addon_state.get(_NS, "video", "") or "")


def set_selected_video(path: str) -> None:
    addon_state.set_(_NS, "video", path or "")


# ---- proxy / hardware bridges to the issuance addon ---------------------


def _get_proxy() -> str:
    with contextlib.suppress(Exception):
        from ..issuance.core import get_fingerprint_proxy
        return str(get_fingerprint_proxy() or "")
    return ""


def _set_proxy(value: str) -> None:
    with contextlib.suppress(Exception):
        from ..issuance.core import set_fingerprint_proxy
        set_fingerprint_proxy(value or "")


def _hw_presets() -> dict:
    with contextlib.suppress(Exception):
        from ..issuance.core import PROFILE_PRESETS
        return dict(PROFILE_PRESETS)
    return {}


def _hw_label(key: str) -> str:
    presets = _hw_presets()
    if key in presets:
        return str(presets[key].get("name", key))
    return key or "—"


# ---- screen rendering ---------------------------------------------------


def _render_home() -> str:
    method = get_login_method()
    if method == "api":
        acc = "API-ключ задан ✅" if get_api_key() else "API-ключ не задан ❌"
    else:
        acc = ("логин+пароль заданы ✅" if get_login() and get_password()
               else "логин/пароль не заданы ❌")
    try:
        from .. import hwproxy
        hwline = hwproxy.summary_line()
    except Exception:  # noqa: BLE001
        hwline = "—"
    vid = get_selected_video()
    vid_name = vid.rsplit("/", 1)[-1] if vid else "не выбрано"
    return (
        "<b>🎬 Омни от Гугл</b>\n\n"
        "Вставляешь видео — Омни монтирует его по твоим промтам.\n\n"
        f"• Способ входа: <b>{_METHOD_LABEL.get(method, method)}</b>\n"
        f"• Аккаунт: {acc}\n"
        f"• 🖥 Железо/прокси: <code>{_esc(hwline)}</code>\n"
        f"• 📝 Промтов: {len(get_prompts())}/{MAX_PROMPTS}\n"
        f"• 🎞 Видео: <code>{_esc(vid_name)}</code>\n\n"
        "<i>Видео берётся из «💾 Хранилище» — тыкни видео → «🌀 Омни монтаж».\n"
        "Туда же попадают сгенерированные ролики.</i>"
    )


def _kb_home() -> InlineKeyboardMarkup:
    method = get_login_method()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔑 Способ входа: {_METHOD_LABEL.get(method, method)}",
            callback_data="omni:method")],
        [InlineKeyboardButton(text="👤 Аккаунт Омни", callback_data="omni:account")],
        [InlineKeyboardButton(text="⚙️ Доп. настройки входа (железо/прокси)",
                              callback_data="omni:extra")],
        [InlineKeyboardButton(text=f"📝 Промты ({len(get_prompts())}/{MAX_PROMPTS})",
                              callback_data="omni:prompts")],
        [InlineKeyboardButton(text="🌀 Смонтировать через Омни", callback_data="omni:run")],
        [InlineKeyboardButton(text="↩️ Назад в Монтажёр", callback_data="editor_agent:back")],
    ])


def _kb_method() -> InlineKeyboardMarkup:
    method = get_login_method()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("▶ " if method == "api" else "") + "🔌 Через API",
            callback_data="omni:method:api")],
        [InlineKeyboardButton(
            text=("▶ " if method == "browser" else "") + "🌐 Через браузер",
            callback_data="omni:method:browser")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="omni:home")],
    ])


def _kb_account() -> InlineKeyboardMarkup:
    method = get_login_method()
    rows = []
    if method == "api":
        rows.append([InlineKeyboardButton(text="🔌 Ввести API-ключ", callback_data="omni:acc:api")])
    else:
        rows.append([InlineKeyboardButton(text="👤 Ввести логин", callback_data="omni:acc:login")])
        rows.append([InlineKeyboardButton(text="🔒 Ввести пароль", callback_data="omni:acc:pass")])
    rows.append([InlineKeyboardButton(text="🗑 Очистить данные", callback_data="omni:acc:clear")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="omni:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_extra() -> InlineKeyboardMarkup:
    rows = []
    try:
        from .. import hwproxy
        active = hwproxy.get_active_id()
        for p in hwproxy.list_profiles():
            mark = "🟢 " if p.get("id") == active else "⚪ "
            rows.append([InlineKeyboardButton(
                text=f"{mark}🖥 {p.get('name', p.get('id'))}",
                callback_data=f"omni:hw:{p.get('id')}")])
    except Exception:  # noqa: BLE001
        pass
    rows.append([InlineKeyboardButton(
        text="🛠 Управлять профилями (Прочие → Железо и прокси)",
        callback_data="hwx:home")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="omni:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_prompts() -> InlineKeyboardMarkup:
    rows = []
    for i, _p in enumerate(get_prompts()):
        rows.append([InlineKeyboardButton(text=f"🗑 Убрать промт {i + 1}",
                                          callback_data=f"omni:pr:del:{i}")])
    if len(get_prompts()) < MAX_PROMPTS:
        rows.append([InlineKeyboardButton(text="➕ Добавить промт", callback_data="omni:pr:add")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="omni:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_prompts() -> str:
    prompts = get_prompts()
    if not prompts:
        return ("<b>📝 Промты Омни</b>\n\n"
                f"Пусто. Можно добавить до {MAX_PROMPTS} промтов — "
                "они применяются при монтаже по очереди.")
    lines = ["<b>📝 Промты Омни</b>", ""]
    for i, p in enumerate(prompts):
        short = p if len(p) <= 80 else p[:77] + "…"
        lines.append(f"{i + 1}. {_esc(short)}")
    return "\n".join(lines)


async def _edit(query: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# ---- router registration ------------------------------------------------


def register_omni(router: Router) -> None:
    """Attach all Omni handlers onto the editor_agent router."""

    @router.callback_query(F.data == "omni:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await query.answer()
        await _edit(query, _render_home(), _kb_home())

    # --- login method ---
    @router.callback_query(F.data == "omni:method")
    async def _method(query: CallbackQuery) -> None:
        await query.answer()
        await _edit(query, "<b>🔑 Способ входа в Омни</b>\n\n"
                    "Выбери, как заходить: через API-ключ или через браузер "
                    "(логин+пароль).", _kb_method())

    @router.callback_query(F.data.startswith("omni:method:"))
    async def _method_set(query: CallbackQuery) -> None:
        m = query.data.split(":")[-1]
        set_login_method(m)
        await query.answer(f"Способ входа: {_METHOD_LABEL.get(m, m)}")
        await _edit(query, _render_home(), _kb_home())

    # --- account ---
    @router.callback_query(F.data == "omni:account")
    async def _account(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await query.answer()
        method = get_login_method()
        body = ("<b>👤 Аккаунт Омни</b>\n\n"
                f"Текущий способ входа: <b>{_METHOD_LABEL.get(method, method)}</b>.\n"
                + ("Введи API-ключ Омни." if method == "api"
                   else "Введи логин и пароль от аккаунта Омни."))
        await _edit(query, body, _kb_account())

    @router.callback_query(F.data == "omni:acc:login")
    async def _acc_login(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(OmniStates.awaiting_login)
        await query.answer()
        await _edit(query, "<b>👤 Логин Омни</b>\n\nПришли логин или email "
                    "одним сообщением.", InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отмена", callback_data="omni:account")]]))

    @router.callback_query(F.data == "omni:acc:pass")
    async def _acc_pass(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(OmniStates.awaiting_password)
        await query.answer()
        await _edit(query, "<b>🔒 Пароль Омни</b>\n\nПришли пароль — я сразу "
                    "удалю сообщение из чата.", InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отмена", callback_data="omni:account")]]))

    @router.callback_query(F.data == "omni:acc:api")
    async def _acc_api(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(OmniStates.awaiting_api)
        await query.answer()
        await _edit(query, "<b>🔌 API-ключ Омни</b>\n\nПришли API-ключ — я сразу "
                    "удалю сообщение из чата.", InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отмена", callback_data="omni:account")]]))

    @router.callback_query(F.data == "omni:acc:clear")
    async def _acc_clear(query: CallbackQuery) -> None:
        set_login("")
        set_password("")
        set_api_key("")
        await query.answer("Данные аккаунта очищены")
        await _edit(query, _render_home(), _kb_home())

    @router.message(StateFilter(OmniStates.awaiting_login), F.text & ~F.text.startswith("/"))
    async def _msg_login(message: Message, state: FSMContext) -> None:
        set_login((message.text or "").strip())
        await state.clear()
        await message.answer("✅ Логин сохранён.", reply_markup=_kb_account())

    @router.message(StateFilter(OmniStates.awaiting_password), F.text & ~F.text.startswith("/"))
    async def _msg_pass(message: Message, state: FSMContext) -> None:
        set_password((message.text or "").strip())
        with contextlib.suppress(Exception):
            await message.delete()
        await state.clear()
        await message.answer("✅ Пароль сохранён (сообщение удалено).",
                             reply_markup=_kb_account())

    @router.message(StateFilter(OmniStates.awaiting_api), F.text & ~F.text.startswith("/"))
    async def _msg_api(message: Message, state: FSMContext) -> None:
        set_api_key((message.text or "").strip())
        with contextlib.suppress(Exception):
            await message.delete()
        await state.clear()
        await message.answer("✅ API-ключ сохранён (сообщение удалено).",
                             reply_markup=_kb_account())

    # --- extra (hardware + proxy) ---
    @router.callback_query(F.data == "omni:extra")
    async def _extra(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await query.answer()
        try:
            from .. import hwproxy
            hwline = hwproxy.summary_line()
        except Exception:  # noqa: BLE001
            hwline = "—"
        body = ("<b>⚙️ Доп. настройки входа</b>\n\n"
                "Под каким <b>железом</b> и через какой <b>прокси</b> заходить, "
                "чтобы выглядеть как живой пользователь. Это общие профили из "
                "«🧩 Прочие → 🖥 Железо и прокси» — выбери активный:\n\n"
                f"Сейчас: <code>{_esc(hwline)}</code>")
        await _edit(query, body, _kb_extra())

    @router.callback_query(F.data.startswith("omni:hw:"))
    async def _hw_set(query: CallbackQuery) -> None:
        pid = query.data.split(":", 2)[-1]
        name = pid
        with contextlib.suppress(Exception):
            from .. import hwproxy
            hwproxy.set_active_id(pid)
            prof = hwproxy.get_profile(pid)
            name = prof.get("name", pid) if prof else pid
        await query.answer(f"Профиль: {name}")
        try:
            from .. import hwproxy
            hwline = hwproxy.summary_line()
        except Exception:  # noqa: BLE001
            hwline = "—"
        body = ("<b>⚙️ Доп. настройки входа</b>\n\n"
                f"Активный профиль: <code>{_esc(hwline)}</code>")
        await _edit(query, body, _kb_extra())

    @router.callback_query(F.data == "omni:proxy")
    async def _proxy(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(OmniStates.awaiting_proxy)
        await query.answer()
        cur = _get_proxy()
        hint = f"\n\nСейчас: <code>{_esc(cur)}</code>" if cur else ""
        await _edit(query, "<b>🛡 Прокси/VPN</b>\n\nПришли строку прокси "
                    "(<code>http://user:pass@host:port</code> или "
                    "<code>socks5://host:port</code>). Пусто/«-» — очистить." + hint,
                    InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отмена", callback_data="omni:extra")]]))

    @router.message(StateFilter(OmniStates.awaiting_proxy), F.text & ~F.text.startswith("/"))
    async def _msg_proxy(message: Message, state: FSMContext) -> None:
        val = (message.text or "").strip()
        if val in {"-", "—", "clear", "очистить"}:
            val = ""
        _set_proxy(val)
        await state.clear()
        await message.answer("✅ Прокси " + ("сохранён." if val else "очищен."))

    # --- prompts ---
    @router.callback_query(F.data == "omni:prompts")
    async def _prompts(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await query.answer()
        await _edit(query, _render_prompts(), _kb_prompts())

    @router.callback_query(F.data == "omni:pr:add")
    async def _pr_add(query: CallbackQuery, state: FSMContext) -> None:
        if len(get_prompts()) >= MAX_PROMPTS:
            await query.answer(f"Максимум {MAX_PROMPTS} промтов.", show_alert=True)
            return
        await state.set_state(OmniStates.awaiting_prompt)
        await query.answer()
        await _edit(query, "<b>➕ Новый промт</b>\n\nПришли текст промта одним "
                    "сообщением.", InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отмена", callback_data="omni:prompts")]]))

    @router.message(StateFilter(OmniStates.awaiting_prompt), F.text & ~F.text.startswith("/"))
    async def _msg_prompt(message: Message, state: FSMContext) -> None:
        ok = add_prompt((message.text or "").strip())
        await state.clear()
        note = "✅ Промт добавлен." if ok else "❌ Не добавлен (пусто или лимит)."
        await message.answer(note + f"\n\n{_render_prompts()}",
                             parse_mode="HTML", reply_markup=_kb_prompts())

    @router.callback_query(F.data.startswith("omni:pr:del:"))
    async def _pr_del(query: CallbackQuery) -> None:
        with contextlib.suppress(Exception):
            remove_prompt(int(query.data.split(":")[-1]))
        await query.answer("Промт убран")
        await _edit(query, _render_prompts(), _kb_prompts())

    # --- run montage ---
    @router.callback_query(F.data == "omni:run")
    async def _run(query: CallbackQuery) -> None:
        await query.answer()
        # Чек-лист уяснения перед запуском.
        missing = []
        vid = get_selected_video()
        if not vid:
            missing.append("🎞 видео (выбери в «💾 Хранилище» → «🌀 Омни монтаж»)")
        method = get_login_method()
        if method == "api" and not get_api_key():
            missing.append("🔌 API-ключ (Аккаунт Омни)")
        if method == "browser" and not (get_login() and get_password()):
            missing.append("👤 логин и пароль (Аккаунт Омни)")
        if not get_prompts():
            missing.append("📝 хотя бы один промт")
        if missing:
            body = ("<b>🚦 Нужны данные для Омни монтажа</b>\n\nНе хватает:\n"
                    + "\n".join(f"• {m}" for m in missing))
            await _edit(query, body, _kb_home())
            return
        # Enqueue the same analyze→edit pipeline the «🎞 Монтажёр» uses,
        # tagging the job with Omni settings so the worker can apply them.
        hw_id = ""
        with contextlib.suppress(Exception):
            from .. import hwproxy
            hwproxy.apply_active()  # push active fingerprint+proxy to the engine
            hw_id = hwproxy.get_active_id()
        enq_ok = False
        with contextlib.suppress(Exception):
            from ...jobs import get_default_queue
            queue = get_default_queue()
            chat_id = query.message.chat.id if query.message else None
            await queue.enqueue(
                "analyze",
                {
                    "source_path": vid,
                    "duration_s": 0.0,
                    "language": None,
                    "engine": "omni",
                    "omni": {
                        "login_method": method,
                        "hw_profile": hw_id,
                        "prompts": get_prompts(),
                    },
                },
                chat_id=chat_id,
            )
            enq_ok = True
        vid_name = vid.rsplit("/", 1)[-1]
        if enq_ok:
            body = ("<b>🌀 Омни монтаж запущен</b>\n\n"
                    f"Видео: <code>{_esc(vid_name)}</code>\n"
                    f"Промтов: {len(get_prompts())} · вход: "
                    f"{_METHOD_LABEL.get(method, method)}\n\n"
                    "Готовый ролик придёт в «💾 Хранилище». Прогресс — /jobs.")
        else:
            body = ("<b>🌀 Омни монтаж</b>\n\n"
                    f"Видео <code>{_esc(vid_name)}</code> и настройки приняты, "
                    "но очередь задач недоступна. Проверь воркеры.")
        await _edit(query, body, _kb_home())
