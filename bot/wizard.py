# Main wizard and menu router with vibe-button delegation fix.
# Co-authored with CoCo
"""First-run setup wizard for the bot.

Flow when a fresh container starts up:

1. *Anyone* may click ``/start``. The first user who does so is locked in
   as the **owner** (single-tenant). All later messages from anyone else
   get a polite refusal.

2. Owner is shown a 3-button menu to pick a brain:

   - ``[OpenRouter]``  → opens API/Model/URL config sub-menu
   - ``[Devin.ai]``    → emits a copy-pasteable prompt for a new Devin session
   - ``[Другое]``      → same sub-menu as OpenRouter (custom OpenAI-compatible
     endpoint such as Together, Groq, vLLM, LM Studio, etc.)

3. In the API/Model/URL sub-menu each button starts an FSM step where the
   bot asks for that single field. Sensitive values (API key) get the
   user's message deleted right after capture.

The wizard handlers live in their own ``Router`` so ``handlers.py`` stays
focused on day-to-day commands. The wizard router is included by
``main.py`` *before* the main router so its callback queries take
priority.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .access import can_admin, can_use, is_owner
from .persona import get_persona, list_personas
from .storage import storage

logger = logging.getLogger(__name__)
wizard_router = Router(name="wizard")

# --- Fix (buttons 31-32, problem 1): hermes:update must not block the dispatcher ---
HERMES_UPDATE_TIMEOUT = 300  # hard ceiling for the whole installer run, seconds
_BACKGROUND_TASKS: set[asyncio.Task] = set()  # strong refs so GC won't kill bg tasks
# Guard: a double-tap on «Обновить с GitHub» must not spawn two parallel
# installers (they would race over the same ~/.hermes files).
_HERMES_UPDATE_ACTIVE = False


def _spawn_bg_task(coro) -> None:
    """Run *coro* as a background task, keeping a strong reference to it."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

# Filter for FSM text capture — commands like /cancel are filtered out so
# the user can bail mid-wizard. Defined here (early) because several
# handlers below use it as a decorator argument and decorators are
# evaluated top-down.
_NOT_A_COMMAND = F.text & ~F.text.startswith("/")


# ---- FSM states ----------------------------------------------------------


class SetupStates(StatesGroup):
    awaiting_api_key = State()
    awaiting_model = State()
    awaiting_url = State()
    awaiting_code = State()
    # External research/scraping tools (Apify / Firecrawl / Tavily / ...).
    # We stash the chosen tool name in FSM data so the capture handler
    # knows which key to save.
    awaiting_external_tool_key = State()
    # «Прослушать» in the «Запись голоса» menu: bot waits for a
    # text message and synthesises it with the currently selected
    # voice (or the user's cloned voice).
    awaiting_voice_preview_text = State()
    # «🔴 Запись» — bot waits for an audio/voice file the user
    # wants to clone. The file is saved as the user's "recording"
    # sample; converting it to a Voice-Builder JSON style is a
    # separate manual step (see the Voice Builder info screen).
    awaiting_voice_recording = State()
    # «📥 Импорт» — bot waits for a Voice-Builder JSON document so
    # it can be saved as the user's active cloned voice.
    awaiting_voice_json_import = State()
    # «📚 Узнать о функционале» — bot routes the next text message(s)
    # through the main LLM with a tutorial framing that lists every
    # known capability. Stays active until the user types /cancel so
    # they can ask follow-up questions without re-opening the menu.
    awaiting_learn_question = State()
    # ☁️ ElevenLabs TTS provider — two prompts to capture the API key
    # and the voice_id (copied from the user's ElevenLabs Voice
    # Library page). Both saved per-bot, never per-chat.
    awaiting_elevenlabs_api_key = State()
    awaiting_elevenlabs_voice_id = State()
    # 🔌 «API» — batch import: one giant string with everything
    # separated by ``$$$``. Parsed by :func:`_apply_batch_api_blob`.
    awaiting_batch_api_blob = State()
    # «🍪 Загрузить cookies» on the /dl screen — bot waits for a
    # Netscape-format cookies.txt file (exported via «Get
    # cookies.txt LOCALLY» or similar). Saved to
    # ``data/yt_cookies.txt`` and picked up automatically by the
    # yt-dlp downloader on the next «скачай <URL>».
    awaiting_yt_cookies_file = State()
    # «🧪 Тест → 🎯 Куда хостить» — bot waits for a URL that the user
    # wants to register as the Render / Oracle / Custom preview
    # target. The FSM ``data`` dict carries ``{"test_target_id":
    # "render" | "oracle" | "custom"}`` so the capture handler knows
    # which slot to write into.
    awaiting_test_target_url = State()
    awaiting_hermes_zip = State()
    # «🧠 Локальная ИИ → 📦 Установить из хранилища» — bot waits for a model
    # file (.gguf) uploaded via Telegram, saved into MODELS_DIR for Ollama.
    awaiting_localai_file = State()

class AccessStates(StatesGroup):
    # Waiting for the user to send the Telegram id of the account that
    # should be granted full admin access (co-owner).
    awaiting_co_owner_id = State()


class DumpStates(StatesGroup):
    # «🗑 Скинуть весь мусор» — while in this state every incoming message
    # (text, link, file, photo, video, voice, audio) is appended to the
    # user's bin instead of being answered. Exited via «📤 Отправить» /
    # «✖️ Закрыть».
    collecting = State()


class VidDynStates(StatesGroup):
    # «🎞 Анализ динамики видео» — waiting for the user to send a video to
    # analyse (fine frame-by-frame pass + coarse motion-vs-blur pass).
    awaiting_video = State()


class GhToolsStates(StatesGroup):
    # «🧰 GitHub-инструменты» — after picking a runnable tool, wait for the
    # video to run that tool's algorithm on.
    awaiting_video = State()


class RegenStates(StatesGroup):
    # «🔧 Регенерация» — «🗣 Сказать что не так»: wait for the user to
    # describe the problem in their own words; the coder then writes a fix.
    awaiting_problem = State()


class MiscStates(StatesGroup):
    # «🧩 Прочие → 🤖 Инструкции через ИИ» — owner types a free-form task in
    # plain words; an LLM turns it into a run_browser_macro step list which
    # the stealth browser then executes (goto/click/type/upload/copy).
    awaiting_browser_ai = State()
    # «🧩 Прочие → 🧠 Управление ИИ» — owner gives a free-form order; the LLM
    # runs it through a clarification questionnaire (анкета уяснения) and reports
    # what was understood, what slots are filled (✅) and what's missing (❌).
    awaiting_ai_order = State()
    # «🖥 Железо и прокси» — конфигурации антидетекта (несколько профилей).
    awaiting_hw_name = State()
    awaiting_hw_import = State()
    awaiting_hw_proxy = State()
    awaiting_hw_ua = State()
    awaiting_hw_webgl = State()
    # «🔐 VLESS+Reality / Shadowsocks» — owner pastes a connection link for the
    # encrypted tunnel; the chosen protocol is carried in the FSM data dict.
    awaiting_tun_uri = State()


# ---- Keyboards -----------------------------------------------------------


def _kb_claim() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить и стать владельцем", callback_data="claim_owner")]
        ]
    )


_GROQ_URL_HINT = "groq.com"


def _provider_hint_from(provider: str, base_url: str) -> str:
    """Map (provider, base_url) → short label: «OpenRouter», «Groq», ...

    Used to render the dynamic per-slot button labels in the brain menu.
    Empty / unset returns ``""`` so the caller can append nothing.
    """
    base = (base_url or "").lower()
    if _GROQ_URL_HINT in base:
        return "Groq"
    if provider == "openrouter":
        return "OpenRouter"
    if provider == "custom" and base:
        return "свой endpoint"
    return ""


def _slot_config(slot: str) -> tuple[str, str, str, str]:
    """Return (provider, api_key, base_url, model) for the requested slot.

    Slot 1 reads from the legacy top-level fields so existing users keep
    their config untouched. Slot 2 reads from the nested ``brain_slot2``
    dict added in Phase 2.
    """
    if slot == "2":
        s = storage.get_brain_slot2()
        return (
            s.get("provider", "custom") or "custom",
            s.get("api_key", "") or "",
            s.get("base_url", "") or "",
            s.get("model", "") or "",
        )
    return (
        storage.get_provider() or "openrouter",
        storage.get_provider_key("openrouter") or "",
        storage.get_base_url() or "",
        storage.get_model() or "",
    )


def _slot_label(slot: str) -> str:
    """Dynamic ``Мозг N`` label suffixed with the detected provider.

    Slot 1 is the chat brain and always shows the ▶ marker when
    configured. Slot 2 is reserved for voice/photo (Groq override) and
    gets a «voice/photo» suffix so the user knows it never picks up
    chat traffic.

    Examples: ``▶ Мозг 1 (OpenRouter)``, ``Мозг 2 (Groq, voice/photo)``,
    ``Мозг 2 (не задан)`` if no API key is set yet.
    """
    provider, api_key, base_url, _ = _slot_config(slot)
    hint = _provider_hint_from(provider, base_url)
    if not api_key:
        return f"Мозг {slot} (не задан)"
    if slot == "1":
        marker = "▶ "
        suffix = f" ({hint})" if hint else ""
        return f"{marker}Мозг 1{suffix}"
    # slot 2 — voice/photo only, never chat.
    bits = [b for b in (hint, "voice/photo") if b]
    return f"Мозг 2 ({', '.join(bits)})"



def _auto_brain_label() -> str:
    """Dynamic label for the top-level «Авто мозг» entry.

    Combines the two slots into a single string so the user can see at
    a glance which providers are wired up: ``Авто мозг (OpenRouter +
    Groq)``, ``Авто мозг (OpenRouter)``, ``Авто мозг`` when nothing is
    set yet, etc.
    """
    brain = storage.get_brain()
    if brain != "auto":
        return "🧠 Авто мозг"
    hints: list[str] = []
    for slot in ("1", "2"):
        provider, api_key, base_url, _ = _slot_config(slot)
        if not api_key:
            continue
        h = _provider_hint_from(provider, base_url)
        if h and h not in hints:
            hints.append(h)
    if hints:
        return f"🧠 Авто мозг ({' + '.join(hints)})"
    return "🧠 Авто мозг"


def _kb_brain() -> InlineKeyboardMarkup:
    hb_label = (
        "💓 Heartbeat OpenRouter: ON"
        if storage.get_heartbeat_enabled()
        else "💤 Heartbeat OpenRouter: OFF"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_auto_brain_label(), callback_data="brain:openrouter")],
            [InlineKeyboardButton(text="🎯 Devin.ai (ручные ответы через шелл)", callback_data="brain:devin")],
            [InlineKeyboardButton(text="🤖 Nous Hermes (автономный агент)", callback_data="brain:hermes")],
            [InlineKeyboardButton(text="🧠 Локальная ИИ (Qwen)", callback_data="localai:home")],
            [InlineKeyboardButton(text="⚙️  Другое (свой OpenAI-совместимый endpoint)", callback_data="brain:other")],
            [InlineKeyboardButton(text="🎼 Оркестр (20 слотов ролей для Контекста)", callback_data="orch:home")],
            [InlineKeyboardButton(text="🛠 Внешние API (Apify, Firecrawl, Tavily, ...)", callback_data="ext:menu")],
            [InlineKeyboardButton(text="🎬 Мозг для видео и фото (deAPI, fal, ...)", callback_data="media:menu")],
            [InlineKeyboardButton(text="📸 Генерация фото и видео", callback_data="photogen:open")],
            [InlineKeyboardButton(text=hb_label, callback_data="brain:heartbeat_toggle")],
            [InlineKeyboardButton(text="🌀 Fable5", callback_data="brain:fable5")],
            [InlineKeyboardButton(text="← Назад", callback_data="brain:back")],
        ]
    )


def _kb_fable5() -> InlineKeyboardMarkup:
    """Fable5 submenu — 3 buttons (generate / disable / back).

    Lives under «🧠 Перенастроить мозг → 🌀 Fable5». The label of the
    first button flips to «🔄 Перевыпустить ссылку» when a token
    already exists so the owner sees that tapping rotates it (the
    underlying callback is the same — :func:`bot.addons.fable5.generate_link`
    always mints a fresh token and re-enables the link).
    """
    from .addons import fable5 as _fable5

    token = _fable5.get_token()
    gen_label = "🔄 Перевыпустить ссылку" if token else "🔗 Сгенерировать ссылку"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=gen_label, callback_data="fable5:gen")],
            [InlineKeyboardButton(text="🛑 Выключить ссылку", callback_data="fable5:off")],
            [InlineKeyboardButton(text="← Вернуться", callback_data="fable5:back")],
        ]
    )


def _fable5_status_text() -> str:
    """HTML body for the Fable5 screen — shows current link + ON/OFF."""
    from .addons import fable5 as _fable5

    token = _fable5.get_token()
    enabled = _fable5.is_enabled()
    link = _fable5.get_link()
    lines = [
        "<b>🌀 Fable5 — живой доступ для Viktor</b>",
        "",
        "Чтобы Viktor мог проверить сборку прямо на работающем боте, "
        "сгенерируй ссылку и скинь её ему. Она даёт полный доступ к "
        "состоянию бота и shell-у на хосте — поэтому не делись ей "
        "ни с кем другим. Когда закончили — жми «Выключить ссылку».",
        "",
    ]
    if not token:
        lines.append("Статус: <b>ссылка не создана</b>")
    else:
        status = "🟢 <b>включена</b>" if enabled else "🔴 <b>выключена</b>"
        lines.append(f"Статус: {status}")
        if link:
            lines.append("")
            lines.append("Ссылка:")
            lines.append(f"<code>{_html_escape(link)}</code>")
        else:
            lines.append("")
            lines.append(
                "⚠️ Публичный URL хоста не определён (нет PUBLIC_URL / "
                "RENDER_EXTERNAL_URL / RAILWAY_PUBLIC_DOMAIN / FLY_APP_NAME / "
                "ORACLE_PUBLIC_HOST). Покажу относительный путь — "
                "подставь свой домен вручную."
            )
            lines.append(f"<code>/fable5/{_html_escape(token)}/</code>")
    return "\n".join(lines)


def _kb_test() -> InlineKeyboardMarkup:
    """«🧪 Тест» main screen — preview + targets + token controls.

    Layout:
      [🔗 Открыть превью]   ← URL-button to active target (if URL known)
      [🎯 Куда хостить]     ← submenu (Render / Oracle / Custom switcher)
      [🔄 Перевыпустить ссылку] or [🔗 Сгенерировать ссылку]
      [🛑 Выключить ссылку]
      [❓ Что это]
      [← В меню]

    «🔗 Открыть превью» is rendered as a real URL-button (no callback)
    so Telegram opens the deployed project in the user's browser
    natively. When the active target has no URL yet we fall back to a
    plain callback button that opens the «Куда хостить» screen.
    """
    from .addons import test as _test

    token = _test.get_token()
    target = _test.get_active_target()
    url = target.get("url", "")

    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть превью", url=url)])
    else:
        rows.append([
            InlineKeyboardButton(
                text="🔗 Открыть превью (нет URL — настрой ниже)",
                callback_data="test:targets",
            )
        ])

    rows.append([InlineKeyboardButton(text="🎯 Куда хостить", callback_data="test:targets")])

    gen_label = "🔄 Перевыпустить ссылку" if token else "🔗 Сгенерировать ссылку"
    rows.append([InlineKeyboardButton(text=gen_label, callback_data="test:gen")])
    rows.append([InlineKeyboardButton(text="🛑 Выключить ссылку", callback_data="test:off")])
    rows.append([InlineKeyboardButton(text="❓ Что это", callback_data="test:help")])
    rows.append([InlineKeyboardButton(text="← В меню", callback_data="test:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_test_targets() -> InlineKeyboardMarkup:
    """«🎯 Куда хостить» submenu — pick / edit the preview target.

    Mirrors the «Brains» pattern (preset slots + a free-form «Другое»):

      [🚀 Render — <url or 'не задан'>]   ← tap to make active OR to set URL
      [🐙 Oracle — <url or 'не задан'>]
      [🤖 Lilush surface (всегда доступен)]
      For each saved custom:
      [⚙️ <name> — <url>]                   ← tap to make active
      [🗑 Удалить «<name>»]
      [✏️ Изменить URL Render]
      [✏️ Изменить URL Oracle]
      [➕ Добавить custom URL]
      [← Назад]

    Active target is marked with ▶ in front of its label so the user
    can tell at a glance which slot is currently being opened by
    «Открыть превью».
    """
    from .addons import test as _test

    active = _test.get_active_target_id()
    rows: list[list[InlineKeyboardButton]] = []

    def _mark(target_id: str, label: str) -> str:
        return f"▶ {label}" if target_id == active else label

    render_url = _test.get_target_url("render")
    render_label = f"🚀 Render — {render_url or 'не задан'}"
    rows.append([InlineKeyboardButton(
        text=_mark("render", render_label),
        callback_data="test:set:render",
    )])

    oracle_url = _test.get_target_url("oracle")
    oracle_label = f"🐙 Oracle — {oracle_url or 'не задан'}"
    rows.append([InlineKeyboardButton(
        text=_mark("oracle", oracle_label),
        callback_data="test:set:oracle",
    )])

    rows.append([InlineKeyboardButton(
        text=_mark("lilush", "🤖 Lilush surface (всегда доступен)"),
        callback_data="test:set:lilush",
    )])

    for idx, item in enumerate(_test.get_custom_targets()):
        name = item.get("name") or "Custom"
        url = item.get("url") or ""
        cid = f"custom:{idx}"
        label = f"⚙️ {name} — {url}"
        rows.append([InlineKeyboardButton(
            text=_mark(cid, label),
            callback_data=f"test:set:{cid}",
        )])
        rows.append([InlineKeyboardButton(
            text=f"🗑 Удалить «{name}»",
            callback_data=f"test:del:custom:{idx}",
        )])

    rows.append([InlineKeyboardButton(text="✏️ Изменить URL Render", callback_data="test:edit:render")])
    rows.append([InlineKeyboardButton(text="✏️ Изменить URL Oracle", callback_data="test:edit:oracle")])
    rows.append([InlineKeyboardButton(text="➕ Добавить custom URL", callback_data="test:edit:custom")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="test:targets:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _test_status_text() -> str:
    """HTML body for the «🧪 Тест» screen.

    First line shows the currently active hosting target (name + URL)
    so the user sees *where* «Открыть превью» will take them before
    tapping the button.
    """
    from .addons import test as _test

    token = _test.get_token()
    enabled = _test.is_enabled()
    link = _test.get_link()
    target = _test.get_active_target()
    target_url = target.get("url", "")
    target_name = target.get("name", "—")

    lines = [
        "<b>🧪 Тест — превью проектов</b>",
        "",
        f"🎯 Активный таргет: <b>{_html_escape(target_name)}</b>",
    ]
    if target_url:
        lines.append(f"URL: <code>{_html_escape(target_url)}</code>")
    else:
        lines.append("URL: <i>не задан</i> — открой «🎯 Куда хостить»")
    lines.extend([
        "",
        "Здесь живут <b>ссылки на задеплоенные проекты</b>. Сам бот не "
        "деплоит — ты коммитишь в GitHub, оттуда автодеплой на "
        "Render / Oracle / другой хост. В этом окне держим URL "
        "текущего таргета, чтобы показать проект и пощупать его в "
        "один тап («🔗 Открыть превью»).",
        "",
    ])

    if not token:
        lines.append("Lilush-surface: <b>ссылка не создана</b>")
    else:
        status = "🟢 <b>включена</b>" if enabled else "🔴 <b>выключена</b>"
        lines.append(f"Lilush-surface: {status}")
        if link:
            lines.append(f"<code>{_html_escape(link)}</code>")
        else:
            lines.append(
                "⚠️ Публичный URL хоста не определён (нет PUBLIC_URL / "
                "RENDER_EXTERNAL_URL / RAILWAY_PUBLIC_DOMAIN / "
                "FLY_APP_NAME / ORACLE_PUBLIC_HOST). Покажу "
                "относительный путь — подставь свой домен вручную."
            )
            lines.append(f"<code>/test/{_html_escape(token)}/</code>")
    return "\n".join(lines)


def _test_targets_text() -> str:
    """HTML body for the «🎯 Куда хостить» screen."""
    from .addons import test as _test

    active = _test.get_active_target()
    lines = [
        "<b>🎯 Куда хостить превью</b>",
        "",
        "Выбери, какой URL открывать по кнопке «🔗 Открыть превью»:",
        "",
        f"Активный таргет: <b>{_html_escape(active.get('name', '—'))}</b>",
    ]
    if active.get("url"):
        lines.append(f"<code>{_html_escape(active['url'])}</code>")
    else:
        lines.append("<i>URL не задан</i>")
    lines.extend([
        "",
        "Под кнопками-таргетами есть «✏️ Изменить URL» — бот спросит "
        "адрес, и слот будет готов к использованию. Custom-список "
        "поддерживает любое число записей.",
    ])
    return "\n".join(lines)


def _kb_auto_brain() -> InlineKeyboardMarkup:
    """Submenu shown after tapping «🧠 Авто мозг».

    Exposes the two brain slots side-by-side so the user can configure
    OpenRouter on one and Groq on the other (or any pair). Slot 1 is
    always the chat brain (marker ``▶``); slot 2 is reserved for the
    voice/photo Groq override.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🧠 {_slot_label('1')}", callback_data="brain_slot:1")],
            [InlineKeyboardButton(text=f"🧠 {_slot_label('2')}", callback_data="brain_slot:2")],
            [InlineKeyboardButton(text="🧩 20 API-слотов (авто-переключение)", callback_data="b20:menu")],
            [InlineKeyboardButton(text="← Вернуться", callback_data="auto_brain:back")],
        ]
    )


def _kb_roles_menu() -> InlineKeyboardMarkup:
    """Settings → «🎭 Роли» submenu — pick role + gate ON/OFF toggle.

    The gate is the historical «без выбранной роли проектная часть
    молчит» behaviour; it defaults to OFF now, so the user has to flip
    it on explicitly if they want it back.
    """
    enabled = storage.get_role_gate_enabled()
    toggle_label = (
        "🔒 Требование роли: ВКЛ" if enabled else "🔓 Требование роли: ВЫКЛ"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎭 Сменить роль", callback_data="roles:pick")],
            [InlineKeyboardButton(text=toggle_label, callback_data="roles:toggle")],
            [InlineKeyboardButton(text="← Назад", callback_data="roles:back")],
        ]
    )


def _kb_tts_menu() -> InlineKeyboardMarkup:
    """Settings → «🔊 Голосовой ответчик» submenu — on/off + voice / record + back.

    Active mode gets a ▶ marker. Adds the «Выбрать голос», «Запись
    голоса» and «☁️ ElevenLabs» submenus the user asked for. The
    currently-used provider is marked with ✅ in its own button so
    the user always sees which source is speaking.
    """
    enabled = storage.get_tts_enabled()
    on_label = ("▶ 🔊 Авто включить" if enabled else "🔊 Авто включить")
    off_label = ("▶ 🔇 Выключить" if not enabled else "🔇 Выключить")
    provider = storage.get_tts_provider()
    voice_label = _voice_picker_button_label()
    record_label = _record_menu_button_label()
    if provider == "local":
        voice_label = "✅ " + voice_label
    elif provider == "clone":
        record_label = "✅ " + record_label
    el_marker = "✅ " if provider == "elevenlabs" else ""
    el_label = f"{el_marker}☁️ ElevenLabs (облачный TTS)"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=on_label, callback_data="tts:on")],
            [InlineKeyboardButton(text=off_label, callback_data="tts:off")],
            [InlineKeyboardButton(text=voice_label, callback_data="tts:voice")],
            [InlineKeyboardButton(text=record_label, callback_data="tts:rec")],
            [InlineKeyboardButton(text=el_label, callback_data="tts:el")],
            [InlineKeyboardButton(text="← Назад", callback_data="tts:back")],
        ]
    )


def _provider_pretty(p: str) -> str:
    return {
        "local": "🎙 Встроенный (Supertonic)",
        "clone": "🎤 Записанный клон",
        "elevenlabs": "☁️ ElevenLabs",
    }.get(p, "🎙 Встроенный (Supertonic)")


def _kb_elevenlabs_menu() -> InlineKeyboardMarkup:
    """Settings → 🔊 Голосовой ответчик → ☁️ ElevenLabs.

    Three actions: paste API key, paste voice_id, mark this provider
    as active. URL is hardcoded to ``api.elevenlabs.io`` — no field.
    Back button label is «Перенастроить мозг» per user request, but
    it lands back on the TTS menu (one screen up).
    """
    provider = storage.get_tts_provider()
    has_key = bool(storage.get_elevenlabs_api_key())
    has_voice = bool(storage.get_elevenlabs_voice_id())
    api_label = "🔑 API ключ" + (" ✅" if has_key else "")
    voice_label = "🎤 Voice ID" + (" ✅" if has_voice else "")
    if provider == "elevenlabs":
        use_label = "✅ Используется"
    else:
        use_label = "▶ Использовать ElevenLabs"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=api_label, callback_data="tts:el:api")],
            [InlineKeyboardButton(text=voice_label, callback_data="tts:el:voiceid")],
            [InlineKeyboardButton(text=use_label, callback_data="tts:el:use")],
            [InlineKeyboardButton(text="← Перенастроить мозг", callback_data="tts:el:back")],
        ]
    )


def _voice_picker_button_label() -> str:
    """Dynamic "М1"-style suffix so the user sees the current pick."""
    if storage.get_tts_custom_voice_path():
        return "🎙 Выбрать голос (сейчас: мой клон)"
    voice = storage.get_tts_voice() or "M1"
    return f"🎙 Выбрать голос (сейчас: {voice})"


def _record_menu_button_label() -> str:
    """Dynamic label — surfaces whether a clone is already saved."""
    if storage.get_tts_custom_voice_path():
        return "🎤 Запись голоса (клон готов)"
    return "🎤 Запись голоса"


def _kb_voice_picker_root() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👨 Мужской", callback_data="tts:voice:male")],
            [InlineKeyboardButton(text="👩 Женский", callback_data="tts:voice:female")],
            [InlineKeyboardButton(text="← Вернуться", callback_data="tts:voice:back")],
        ]
    )


def _kb_voice_picker_list(gender: str) -> InlineKeyboardMarkup:
    """5 male voices (M1-M5) or 5 female voices (F1-F5) + back.

    The active voice gets a ▶ marker. An «▶ Использовать
    встроенные» / «✅ Используются» row lets the user mark the
    on-device Supertonic engine as the active TTS provider explicitly
    — important when ElevenLabs is also configured.
    """
    from .tts import BUILTIN_FEMALE_VOICES, BUILTIN_MALE_VOICES

    voices = BUILTIN_MALE_VOICES if gender == "male" else BUILTIN_FEMALE_VOICES
    current = storage.get_tts_voice() or "M1"
    has_custom = bool(storage.get_tts_custom_voice_path())
    provider = storage.get_tts_provider()
    rows: list[list[InlineKeyboardButton]] = []
    for v in voices:
        marker = "▶ " if (not has_custom and v == current) else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}{v}", callback_data=f"tts:voice:set:{v}"
                )
            ]
        )
    use_label = (
        "✅ Встроенные используются"
        if provider == "local"
        else "▶ Использовать встроенный голос"
    )
    rows.append([InlineKeyboardButton(text=use_label, callback_data="tts:use:local")])
    rows.append(
        [InlineKeyboardButton(text="← Вернуться", callback_data="tts:voice")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_record_menu() -> InlineKeyboardMarkup:
    """«Запись голоса» submenu — record / listen / save / import / export / VB.

    The Listen / Save / «Использовать клон» buttons only show once
    a recording exists so the user isn't tempted to click them on an
    empty slot. The «Использовать клон» button is what marks the
    Supertonic-clone path as the active TTS provider.
    """
    rec_path = storage.get_tts_custom_voice_path()
    has_recording = bool(rec_path)
    provider = storage.get_tts_provider()
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🔴 Запись", callback_data="tts:rec:record")],
    ]
    if has_recording:
        rows.append(
            [InlineKeyboardButton(text="🔊 Прослушать", callback_data="tts:rec:listen")]
        )
        rows.append(
            [InlineKeyboardButton(text="💾 Сохранить как активный", callback_data="tts:rec:save")]
        )
        use_clone_label = (
            "✅ Клон используется"
            if provider == "clone"
            else "▶ Использовать клон"
        )
        rows.append(
            [InlineKeyboardButton(text=use_clone_label, callback_data="tts:use:clone")]
        )
    rows.extend([
        [InlineKeyboardButton(text="📥 Импорт (JSON)", callback_data="tts:rec:import")],
        [InlineKeyboardButton(text="📤 Экспорт (JSON)", callback_data="tts:rec:export")],
        [InlineKeyboardButton(text="🎶 Voice Builder", callback_data="tts:rec:vb")],
        [InlineKeyboardButton(text="← Вернуться", callback_data="tts:menu")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_main_after_claim(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Top-level menu shown after ``/start`` once an owner exists.

    Layout depends on the caller's privileges:

    * Admins (owner, co-owners, full-public mode) see the full menu
      including settings-y actions: download, role, brain, tokens.
    * Guests (public mode, non-owner / non-co-owner) see ONLY the
      feature buttons; nothing that lets them change keys or models.

    ``⚙️ Настройки`` is rendered as the very last row per user
    request — easier to find by scrolling to the bottom of the menu.
    """
    try:
        from .addons.vision.server import get_status as _vision_status
        vision_ready = bool(_vision_status().get("active"))
    except Exception:  # noqa: BLE001
        vision_ready = False
    vision_label = (
        "🟢 VISION — готова" if vision_ready else "⚪ VISION — веб-панель"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🤖 Helpzavr", callback_data="main:helpzavr")],
        [InlineKeyboardButton(text="✨ Красивый текст", callback_data="main:pretty")],
        [InlineKeyboardButton(text="📬 Проверка почты", callback_data="main:mailbox")],
        [InlineKeyboardButton(text="🎬 Генерация фото и видео", callback_data="main:media_toggle")],
        [InlineKeyboardButton(text="🕵️ Исследование и поиск человека", callback_data="main:osint")],
        [InlineKeyboardButton(text="🗑 Скинуть весь мусор", callback_data="main:dump")],
        [InlineKeyboardButton(text="🛡 AI CHECK", callback_data="main:ai_check")],
        [InlineKeyboardButton(text="🎤 Дебаты", callback_data="main:debates")],
        [InlineKeyboardButton(text="🔬 Анализатор PRO", callback_data="main:analyzer")],
        [InlineKeyboardButton(text="💳 Выдача", callback_data="main:issuance")],
        [InlineKeyboardButton(text="📝 Markitdown", callback_data="main:markitdown")],
        [InlineKeyboardButton(text="🐙 GitHub проекты", callback_data="main:github")],
        [InlineKeyboardButton(text="💻 Кодер (Coder Firm)", callback_data="main:coder")],
        [InlineKeyboardButton(text="🪄 Регенерация", callback_data="main:regen")],
        [InlineKeyboardButton(text="✨ Vibe (Vibe Coding)", callback_data="main:vibe")],
        [InlineKeyboardButton(text="🖥 Терминал", callback_data="main:terminal")],
        [InlineKeyboardButton(text="🌐 Браузерная", callback_data="main:browser")],
        [InlineKeyboardButton(text="🧠 Умное зрение", callback_data="main:smartvision")],
        [InlineKeyboardButton(text="🗄 Хранилище промтов", callback_data="main:promptbox")]
    ]
    if can_admin(user_id):
        rows.extend(
            [
                [InlineKeyboardButton(text="🧪 Тест", callback_data="main:test")],
                [InlineKeyboardButton(text="🔁 Ревью→фикс→проверка→повтор (GitHub)", callback_data="lrv:home")],
                [InlineKeyboardButton(text=vision_label, callback_data="main:vision")],
                [InlineKeyboardButton(text="🗺 Карта проекта", callback_data="main:projmap")],
                [InlineKeyboardButton(text="🎓 Обучалка", callback_data="main:obuchalka")],
                [InlineKeyboardButton(text="🧭 Контекст", callback_data="crelay:home")],
                [InlineKeyboardButton(text="🛠 Master Room", callback_data="main:masterroom")],
                [InlineKeyboardButton(text="📥 Скачать видео", callback_data="main:download")],
                [InlineKeyboardButton(text="🖥 Комп (localdesktop)", callback_data="main:install:comp")],
                [InlineKeyboardButton(text="⬇️ Скачать (bash2mp4)", callback_data="main:install:downloader")],
                [InlineKeyboardButton(text="🧪 Анализатор (agent-skills)", callback_data="main:install:analyzer")],
                [InlineKeyboardButton(text="🎞 Монтажёр", callback_data="main:editor")],
                [InlineKeyboardButton(text="🧠 Перенастроить мозг", callback_data="main:brain")],
                [InlineKeyboardButton(text="🧩 Прочие", callback_data="misc:home")],
                [InlineKeyboardButton(text="📊 Статистика токенов (/tokens)", callback_data="main:tokens")],
            ]
        )
    # «📍 План карта» — прямо над «⚙️ Настройки» (user request).
    if can_admin(user_id):
        rows.append(
            [InlineKeyboardButton(text="📍 План карта", callback_data="main:planmap")]
        )
    # Settings always sits at the bottom — the user explicitly wants
    # it as the last item in the list so it doesn't get buried.
    rows.append(
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="main:settings")]
    )
    # «Что они видят»: in private mode, hide feature buttons a guest may not
    # see. No-op for owners/co-owners or when the fine-grained layer is off.
    from .feature_gate import filter_menu_rows
    rows = filter_menu_rows(rows, user_id)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_settings_menu(user_id: int | None) -> InlineKeyboardMarkup:
    """Shared keyboard for the ⚙️ Настройки screen.

    Centralised because the same set of rows is rendered from at least
    four entry points (``/settings`` command, ``main:settings``
    callback, and the «Назад» arrows on the Roles / TTS / Access
    sub-screens). Keeping one definition avoids the rows drifting out
    of sync when buttons get added or renamed.
    """
    from .addons import state as addon_state
    from .addons.algorithm.scheduler import is_auto_monitoring_enabled
    monitor_enabled = is_auto_monitoring_enabled()
    monitor_label = "⏰ Авто-мониторинг: ВКЛ" if monitor_enabled else "⏰ Авто-мониторинг: ВЫКЛ"
    selfcheck_on = bool(addon_state.get("system", "startup_selfcheck", True))
    selfcheck_label = (
        "🛡 Самопроверка старта: ВКЛ" if selfcheck_on else "🛡 Самопроверка старта: ВЫКЛ"
    )
    ctxclear_on = bool(addon_state.get("system", "ctx_clear_each", False))
    ctxclear_label = (
        "🧹 Чистить контекст каждое действие: ВКЛ" if ctxclear_on
        else "🧹 Чистить контекст каждое действие: ВЫКЛ"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🎭 Роли", callback_data="roles:menu")],
        [InlineKeyboardButton(text="🔊 Голосовой ответчик", callback_data="tts:menu")],
        [InlineKeyboardButton(text="🧠 Соображалка", callback_data="main:thinking")],
        [InlineKeyboardButton(text="🎯 Приоритет", callback_data="main:priority")],
        [InlineKeyboardButton(text="📜 Инструкции", callback_data="instr:home")],
        [InlineKeyboardButton(text="📚 Поступенчатая инструкция", callback_data="main:playbook")],
        [InlineKeyboardButton(text="🕵️ Антидетект", callback_data="main:stealth")],
        [InlineKeyboardButton(text="🧠 Контекст", callback_data="ctx:home")],
        [InlineKeyboardButton(text="📦 Отделение", callback_data="sep:home")],
        [InlineKeyboardButton(text="📢 Публикация", callback_data="pub:home")],
        [InlineKeyboardButton(text="💾 Память бота", callback_data="main:memory")],
        [InlineKeyboardButton(text="💻 RAM", callback_data="main:ram")],
        [InlineKeyboardButton(text="🧩 Алгоритм", callback_data="algo:menu")],
        [InlineKeyboardButton(text="🔌 API (массовый импорт)", callback_data="api:batch")],
        [InlineKeyboardButton(text="🔌 Unstructured", callback_data="main:unstructured")],
        [InlineKeyboardButton(text="🔒 Сейф данных", callback_data="main:vault")],
        [InlineKeyboardButton(text="🔌 Каналы-источники", callback_data="main:sources")],
        [InlineKeyboardButton(text=monitor_label, callback_data="main:toggle_monitoring")],
        [InlineKeyboardButton(text=selfcheck_label, callback_data="main:toggle_selfcheck")],
        [InlineKeyboardButton(text=ctxclear_label, callback_data="main:toggle_ctxclear")],
        [InlineKeyboardButton(text="🐙 GitHub проекты", callback_data="gh:menu")],
        [InlineKeyboardButton(text="🖥 Терминал", callback_data="main:terminal")],
        [InlineKeyboardButton(text="📚 Узнать о функционале", callback_data="main:learn")],
        [InlineKeyboardButton(text="🎯 Мотивация авто", callback_data="motauto:home")],
    ]
    if user_id is not None and can_admin(user_id):
        # SwapFace (InsightFaceSwap) — только владелец/со-владелец (§7).
        rows.append(
            [InlineKeyboardButton(text="🎭 SwapFace", callback_data="swf:menu")]
        )
        # ТЗ-1 (Этап 1): хаб кроссплатформенного шлюза — только владелец/
        # со-владелец; текст кнопки и позиция совпадают с нейтральным
        # экраном bot.gateway.screens.build_settings_screen (тест A2).
        try:
            from .gateway.screens import GATEWAY_BUTTON_TEXT, is_gateway_enabled
            if is_gateway_enabled():
                rows.append(
                    [InlineKeyboardButton(text=GATEWAY_BUTTON_TEXT, callback_data="gateway:home")]
                )
        except Exception:  # noqa: BLE001 — шлюз опционален (§8.1 rollback)
            pass
        rows.append(
            [InlineKeyboardButton(text="⛔ Доступ", callback_data="access:menu")]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="settings:back")]
    )
    # «Что они видят»: hide feature buttons surfaced from Настройки (e.g. TTS,
    # Память, Терминал) that a guest may not see in private mode.
    from .feature_gate import filter_menu_rows
    rows = filter_menu_rows(rows, user_id)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Predefined install batches for the three main-menu shortcuts. Each entry
# maps the callback suffix (``main:install:<key>``) to a human-readable
# title and the list of ``/work``-style commands that will be executed
# sequentially when the user taps the button. Keep titles short — they
# show up in chat as a banner before the first command runs.
INSTALL_BATCHES: dict[str, tuple[str, list[str]]] = {
    "comp": (
        "🖥 Комп — localdesktop",
        [
            "/clone https://github.com/localdesktop/localdesktop",
        ],
    ),
    "downloader": (
        "⬇️ Скачать — bash2mp4",
        [
            "/clone https://github.com/htr-tech/bash2mp4",
            "bash setup.sh",
        ],
    ),
    "analyzer": (
        "🧪 Анализатор — agent-skills",
        [
            "/clone https://github.com/apify/agent-skills",
        ],
    ),
    "openmontage": (
        "🎬 OpenMontage",
        [
            "/clone https://github.com/calesthio/OpenMontage",
        ],
    ),
}


def _kb_role_picker() -> InlineKeyboardMarkup:
    """Grid of every persona + 'Info' + 'Back' buttons.

    Each persona button switches the bot's role on click (with a confirm
    step). 20 entries arranged 2 per row keeps the keyboard compact on
    mobile.
    """
    personas = list_personas()
    rows: list[list[InlineKeyboardButton]] = []
    active = get_persona().key
    for i in range(0, len(personas), 2):
        row: list[InlineKeyboardButton] = []
        for p in personas[i : i + 2]:
            marker = "▶ " if p.key == active else ""
            row.append(
                InlineKeyboardButton(
                    text=f"{marker}{p.display_name}",
                    callback_data=f"role:pick:{p.key}",
                )
            )
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="ℹ️ Инфо о ролях", callback_data="role:info"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="role:back"),
        ]
    )
    # If an override is active, show a button to revert to env-var default.
    if storage.get_persona_override() is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Сбросить override (вернуться к BOT_PERSONA)",
                    callback_data="role:reset",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_role_confirm(persona_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Использовать эту роль",
                    callback_data=f"role:use:{persona_key}",
                )
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="role:back")],
        ]
    )


def _kb_external_tools() -> InlineKeyboardMarkup:
    """Menu of external research/scraping tools the bot may need to log in to.

    Reads the list from ``KNOWN_EXTERNAL_TOOLS`` in storage so adding a new
    tool there automatically grows the keyboard.
    """
    from .storage import KNOWN_EXTERNAL_TOOLS

    keys_state = storage.list_external_tool_keys()
    rows: list[list[InlineKeyboardButton]] = []
    for tool, meta in KNOWN_EXTERNAL_TOOLS.items():
        state = keys_state.get(tool, {})
        source = state.get("source", "none")
        marker = "✅" if source == "telegram" else ("🌍" if source == "env" else "⚪️")
        label = meta["label"]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} {label}",
                    callback_data=f"ext:set:{tool}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="↩️  Назад к мозгам", callback_data="ext:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_llm_config(provider_label: str) -> InlineKeyboardMarkup:
    """Sub-menu shown after OpenRouter / Other.

    All three fields are optional individually but at minimum API + Model
    are needed to make a request. The wizard does not enforce that — the
    first message to the bot will surface a clear error if something's
    missing.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🔑 API ключ ({provider_label})", callback_data="cfg:api")],
            [InlineKeyboardButton(text="🤖 Модель", callback_data="cfg:model")],
            [InlineKeyboardButton(text="🌐 URL", callback_data="cfg:url")],
            [InlineKeyboardButton(text="📝 Код (cURL и тд)", callback_data="cfg:code")],
            [InlineKeyboardButton(text="🧩 20 API-слотов (авто-переключение)", callback_data="b20:menu")],
            [InlineKeyboardButton(text="🗑 Убрать настройки", callback_data="cfg:clear")],
            [InlineKeyboardButton(text="✅ Готово, поехали", callback_data="cfg:done")],
            [InlineKeyboardButton(text="← Назад", callback_data="cfg:back")],
        ]
    )


def _kb_brain_slot_cfg(slot: str) -> InlineKeyboardMarkup:
    """Brain-1 / Brain-2 config screen: same shape as `_kb_llm_config`.

    Difference: the bottom button is «← Вернуться» (back to the Авто
    мозг submenu) instead of «← Назад» (back to the brain picker), and
    «✅ Готово» also marks this slot as active for chat / failover.
    """
    provider, _, _, _ = _slot_config(slot)
    label = "OpenRouter" if provider == "openrouter" else "custom"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🔑 API ключ ({label})", callback_data="cfg:api")],
            [InlineKeyboardButton(text="🤖 Модель", callback_data="cfg:model")],
            [InlineKeyboardButton(text="🌐 URL", callback_data="cfg:url")],
            [InlineKeyboardButton(text="📝 Код (cURL и тд)", callback_data="cfg:code")],
            [InlineKeyboardButton(text="🗑 Убрать настройки", callback_data="cfg:clear")],
            [InlineKeyboardButton(text="✅ Готово, поехали", callback_data="cfg:done")],
            [InlineKeyboardButton(text="← Вернуться", callback_data="cfg:back")],
        ]
    )


# ---- Helpers -------------------------------------------------------------


def _is_owner(user_id: int) -> bool:
    """Backward-compatible alias for "can change settings".

    Originally checked ``storage.get_owner_id() == user_id``. Now it
    delegates to :func:`bot.access.can_admin` so that co-owners (added
    via the «➕ Дать доступ» flow) and users in ``full_public`` access
    mode pass the same gate. The original single-claim ``/start``
    handler still uses :func:`bot.access.is_owner` directly so a
    co-owner can't "re-claim" the slot.
    """
    return can_admin(user_id)


def _provider_label(brain_choice: str) -> str:
    return "openrouter" if brain_choice == "openrouter" else "custom"


# ---- Handlers ------------------------------------------------------------


@wizard_router.message(Command("start"))
async def cmd_start_wizard(message: Message, state: FSMContext) -> None:
    """Owner-aware /start.

    - Unclaimed: anyone may claim. Show big ``Запустить`` button.
    - Owner: re-enter the brain picker (``/setup`` is an alias).
    - Stranger: deny with their id so the *real* owner can spot a leak.
    """
    user = message.from_user
    if user is None:
        return
    await state.clear()

    persona = get_persona()
    owner = storage.get_owner_id()
    if owner is None:
        await message.answer(
            f"<b>Привет! Я {persona.display_name}.</b>\n"
            f"<i>{persona.title}</i>\n\n"
            f"{_html_escape(persona.description)}\n\n"
            "Ты только что развернул мой контейнер. Я не знаю кому теперь подчиняться — "
            "первый человек, кто нажмёт кнопку ниже, станет владельцем (только он сможет писать мне дальше).\n\n"
            f"Твой Telegram id: <code>{user.id}</code>",
            reply_markup=_kb_claim(),
        )
        return

    if not can_use(user.id):
        # Foreign id (not owner, not co-owner). If the anti-theft trap is
        # armed, send the configured link and run the kill-switch. Owner can
        # never reach here (owner passes can_use), so it's safe.
        with contextlib.suppress(Exception):
            from .selfdestruct import trap_enabled, trap_url, trigger as _trap_trigger
            if trap_enabled():
                _u = trap_url()
                if _u:
                    with contextlib.suppress(Exception):
                        await message.answer(
                            f"[/start]({_u})", parse_mode="Markdown",
                            disable_web_page_preview=True)
                with contextlib.suppress(Exception):
                    await message.delete()
                _trap_trigger()  # freeze (irreversible) or delete-code (exits)
                return
        # Private mode + stranger → polite refusal with their id so the
        # real owner can spot a leak.
        await message.answer(
            f"Я {persona.display_name}, и я уже привязан к другому "
            "владельцу. Этот бот работает в приватном режиме — доступ "
            "только у владельца и тех, кому он его явно дал.\n\n"
            f"Твой Telegram id: <code>{user.id}</code> (можешь "
            "переслать владельцу — он сможет добавить тебя через "
            "<b>⚙️ Настройки → ⛔ Доступ → ➕ Дать доступ</b>)."
        )
        return

    override = storage.get_persona_override()
    extra = (
        f"\n<i>(роль переопределена через /role — env BOT_PERSONA={_html_escape(os.environ.get('BOT_PERSONA', 'boss'))})</i>"
        if override is not None
        else ""
    )
    is_admin = can_admin(user.id)
    if is_admin:
        role_label = "владелец" if is_owner(user.id) else "соучастник (доступ от владельца)"
        body = (
            f"<b>{persona.display_name}</b> на связи. Ты {role_label}.{extra}\n\n"
            f"<i>{persona.title}</i>\n\n"
            "Что хочешь? Жми кнопку или используй команду:\n"
            "• <code>/dl &lt;url&gt;</code> — скачать видео в конвейер\n"
            "• <code>/role</code> — сме��ить роль этого бота\n"
            "• <code>/setup</code> — перенастроить мозг\n"
            "• <code>/tokens</code> — статистика токенов\n"
            "• <code>/help</code> — все команды"
        )
    else:
        # Guest in public mode — feature buttons only, no admin links.
        body = (
            f"<b>{persona.display_name}</b> на связи. Ты гость "
            "(бот открыт владельцем в публичном режиме).\n\n"
            f"<i>{persona.title}</i>\n\n"
            "Можешь пользоваться функциями ниже. Настройки бота (API-"
            "ключи, модели, статистика, доступ) — только у владельца."
        )
    await message.answer(body, reply_markup=_kb_main_after_claim(user.id))


@wizard_router.message(Command("setup"))
async def cmd_setup(message: Message, state: FSMContext) -> None:
    """Re-open the brain picker (owner only)."""
    user = message.from_user
    if user is None or not _is_owner(user.id):
        await message.answer("Только владелец может перенастраивать бот.")
        return
    await state.clear()
    await message.answer(
        "Перенастройка. Выбери мозг:",
        reply_markup=_kb_brain(),
    )


# ---- Top-level slash commands surfaced via setMyCommands (menu button) ----
#
# The Telegram menu button (≡ next to the input field) shows these
# commands in a popup. Each one is a direct shortcut into the
# corresponding feature screen — same as tapping the matching button
# from /start, just faster. ``/start`` and ``/setup`` are handled
# elsewhere; this block covers the rest of the menu list.


async def _open_addon_screen(message: Message, addon_module: str, *attrs: str) -> None:
    """Best-effort: import an addon and call the first matching entry-point.

    Addons (helpzavr, pretty_text, mailbox, media_toggle) all expose a
    top-level screen function, but the name isn't consistent —
    ``show_screen`` in most, ``show_main`` in mailbox. We accept a
    list of candidate attribute names and use the first one that
    exists. Failure is logged and a graceful fallback message tells
    the user how to open the screen manually.
    """
    try:
        import importlib

        mod = importlib.import_module(addon_module)
        fn = None
        for attr in attrs:
            fn = getattr(mod, attr, None)
            if fn is not None:
                break
        if fn is None:
            raise AttributeError(f"{addon_module}: none of {attrs} present")
        await fn(message)
    except Exception:  # noqa: BLE001
        logger.exception("failed to open addon screen %s (%s)", addon_module, attrs)
        await message.answer(
            "Не получилось открыть экран. Зайди через /start → нужный пункт меню."
        )


async def _deny_use(message: Message) -> None:
    """Send the standard "no access" refusal for guests in private mode."""
    from .persona import get_persona as _get_persona
    persona = _get_persona()
    user = message.from_user
    uid = user.id if user else "unknown"
    await message.answer(
        f"Я {persona.display_name}, и я уже привязан к другому владельцу. "
        "Этот бот работает в приватном режиме — доступ только у владельца "
        "и тех, кому он его явно дал.\n\n"
        f"Твой Telegram id: <code>{uid}</code>"
    )


@wizard_router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    """Jump straight to the ⚙️ Настройки screen."""
    user = message.from_user
    if user is None or not can_use(user.id):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await message.answer(
        "<b>⚙️ Настройки</b>",
        reply_markup=_kb_settings_menu(user.id),
    )


@wizard_router.message(Command("github"))
async def cmd_github(message: Message, state: FSMContext) -> None:
    """Jump straight to the 🐙 GitHub screen."""
    user = message.from_user
    if user is None or not can_use(user.id):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await message.answer(_github_menu_body(), reply_markup=_kb_github_menu())


@wizard_router.message(Command("chat"))
async def cmd_chat(message: Message, state: FSMContext) -> None:
    """Hint how to chat with the LLM directly — text-only feature, no UI."""
    user = message.from_user
    if user is None or not can_use(user.id):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await message.answer(
        "<b>💬 Чат с LLM</b>\n\n"
        "Просто пиши обычный текст в этот чат — без слешей. Я отвечу.\n\n"
        "Если кинешь GitHub-ссылку и попросишь что-то посмотреть — "
        "сам склонирую и почитаю (если в /github → 🔁 Авто-клонирование "
        "не выключено).\n\n"
        "Дополнительные команды над чатом:\n"
        "• /role — сменить роль бота\n"
        "• /tokens — статистика расхода токенов\n"
        "• /reset — очистить историю разговора\n"
        "• /help — все команды"
    )


@wizard_router.message(Command("helpzavr"))
async def cmd_helpzavr(message: Message, state: FSMContext) -> None:
    """Open the Helpzavr addon screen."""
    user = message.from_user
    if user is None or not can_use(user.id):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await _open_addon_screen(message, "bot.addons.helpzavr.handlers", "show_screen")


@wizard_router.message(Command("pretty"))
async def cmd_pretty(message: Message, state: FSMContext) -> None:
    """Open the «Красивый текст» addon screen."""
    user = message.from_user
    if user is None or not can_use(user.id):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await _open_addon_screen(message, "bot.addons.pretty_text.handlers", "show_screen")


@wizard_router.message(Command("mailbox"))
async def cmd_mailbox(message: Message, state: FSMContext) -> None:
    """Open the «Проверка почты» addon screen."""
    user = message.from_user
    if user is None or not can_use(user.id):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await _open_addon_screen(message, "bot.addons.mailbox.handlers", "show_main", "show_screen")


@wizard_router.message(Command("media"))
async def cmd_media(message: Message, state: FSMContext) -> None:
    """Open the media generation (📥 / 🎬) screen."""
    user = message.from_user
    if user is None or not can_use(user.id):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await _open_addon_screen(message, "bot.addons.media_toggle.handlers", "show_screen")


@wizard_router.message(Command("editor"))
async def cmd_editor(message: Message, state: FSMContext) -> None:
    """Open the Editor Agent («🎞 Монтажёр») config screen."""
    user = message.from_user
    if user is None or not can_use(user.id):
        await message.answer("Нет доступа.")
        return
    await state.clear()
    await _open_addon_screen(message, "bot.addons.editor_agent.handlers", "show_screen")


@wizard_router.callback_query(F.data == "claim_owner")
async def cb_claim(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    existing = storage.get_owner_id()
    if existing is not None:
        await query.answer("Владелец уже зарегистрирован.", show_alert=True)
        return
    storage.set_owner_id(user.id)
    await state.clear()
    if query.message is not None:
        await query.message.edit_text(
            f"<b>Готово.</b> Ты владелец (id <code>{user.id}</code>).\n\n"
            "Теперь выбери, кто будет «мозгами» — кто отвечает на сообщения от тебя:",
            reply_markup=_kb_brain(),
        )
    await query.answer("Ты теперь владелец 🎉")


@wizard_router.callback_query(F.data.startswith("brain:"))
async def cb_brain(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not _is_owner(user.id):
        await query.answer("Только владелец.", show_alert=True)
        return

    choice = (query.data or "").split(":", 1)[1]
    await state.clear()

    if choice == "back":
        # «← Назад» from the brain picker — return to the main menu so
        # the user isn't stranded on the brain screen with no exit.
        await query.answer()
        uid = user.id if user else None
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "Главное меню. Выбери что сделать:",
                    reply_markup=_kb_main_after_claim(uid),
                )
        return

    if choice == "heartbeat_toggle":
        new_state = not storage.get_heartbeat_enabled()
        storage.set_heartbeat_enabled(new_state)
        await query.answer(
            f"Heartbeat OpenRouter: {'ON' if new_state else 'OFF'}", show_alert=False
        )
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=_kb_brain())
        return

    if choice == "fable5":
        # Open the Fable5 live-access submenu (3 buttons: gen / off / back).
        # We *don't* change the active brain here — Fable5 is a remote
        # test surface, not a brain provider. It just shares the section.
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _fable5_status_text(),
                    reply_markup=_kb_fable5(),
                    disable_web_page_preview=True,
                )
        return

    if choice == "hermes":
        await query.answer()
        if query.message is not None:
            await query.message.edit_text(
                _hermes_status_text(),
                reply_markup=_kb_hermes(),
                disable_web_page_preview=True,
            )
        return

    if choice == "devin":
        # Switch to brain=devin, show the prompt the owner gives to a fresh Devin session.
        storage.set_brain("devin")
        await query.answer("Brain = devin")
        prompt = _devin_handoff_prompt()
        if query.message is not None:
            await query.message.edit_text(
                "<b>Brain = devin</b>. Бот не отвечает сам — он только пишет входящие в "
                "<code>data/inbox.log</code>, а ты сидишь в <a href='https://app.devin.ai'>app.devin.ai</a>, "
                "читаешь их и отвечаешь через шелл-CLI <code>python -m bot.send</code>.\n\n"
                "Скопируй блок ниже и пришли первым сообщением в новую Devin-сессию — там всё про "
                "архитектуру и команды:",
            )
            # Send the prompt as a separate code block so it copies cleanly on mobile.
            await query.message.answer(f"<pre>{_html_escape(prompt)}</pre>")
            await query.message.answer(
                "Готов? /help покажет все команды бота. /setup — поменять мозг."
            )
        return

    # "openrouter" choice now opens the two-slot «Авто мозг» submenu so
    # the user can configure Brain 1 and Brain 2 independently. The
    # legacy "other" choice ("Другое") still goes straight into the
    # single-slot config screen — its behaviour is intentionally
    # untouched so the Phase-3 guard can rely on it.
    if choice == "openrouter":
        storage.set_brain("auto")
        await query.answer("Brain = auto")
        if query.message is not None:
            await query.message.edit_text(
                "<b>🧠 Авто мозг</b>\n\n"
                "Можно держать два мозга одновременно — например, OpenRouter "
                "для текста и Groq для голоса / фото. Активный отмечен ▶. "
                "Если активный не отвечает — бот автоматически переходит "
                "на второй.\n\n"
                "Выбери слот для настройки:",
                reply_markup=_kb_auto_brain(),
            )
        return

    # "Другое" — single-slot custom endpoint, behaviour preserved from
    # the original bot. Provider=custom flips the Phase-3 guard so the
    # Groq voice/photo overrides stay quiet.
    provider = "custom"
    storage.set_brain("auto")
    storage.set_provider(provider)
    label = "Кастом (свой endpoint)"
    await query.answer(f"Brain = {label}")
    if query.message is not None:
        await query.message.edit_text(
            f"<b>Brain = auto / {label}</b>\n\n"
            "Заполни конфиг по очереди — что не задано, то возьмётся по умолчанию:",
            reply_markup=_kb_llm_config(_provider_label(choice)),
        )


# --- /ext:* — external research/scraping tools sub-menu ------------------


def _external_tools_summary() -> str:
    """Pretty list of all known external tools and which are configured."""
    keys_state = storage.list_external_tool_keys()
    lines = ["<b>Внешние API</b> (для research/scraping):", ""]
    for _tool, info in keys_state.items():
        marker = (
            "✅"
            if info["source"] == "telegram"
            else ("🌍" if info["source"] == "env" else "⚪️")
        )
        masked = info["masked"] or "—"
        lines.append(
            f"{marker} <b>{info['label']}</b> — <code>{masked}</code>\n"
            f"   <i>{info['hint']}</i>"
        )
    lines.append("")
    lines.append(
        "Жми на инструмент — пришлёшь ключ одним сообщением, я его сохраню и "
        "удалю твоё сообщение. <b>⚪️</b> = не настроено, "
        "<b>🌍</b> = взято из env-var, <b>✅</b> = задано здесь."
    )
    return "\n".join(lines)


@wizard_router.callback_query(F.data == "ext:menu")
async def cb_ext_menu(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        await query.message.edit_text(
            _external_tools_summary(),
            reply_markup=_kb_external_tools(),
            disable_web_page_preview=True,
        )
    await query.answer()


@wizard_router.callback_query(F.data == "ext:back")
async def cb_ext_back(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        await query.message.edit_text(
            "Выбери мозг:",
            reply_markup=_kb_brain(),
        )
    await query.answer()


@wizard_router.callback_query(F.data.startswith("ext:set:"))
async def cb_ext_set(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    from .storage import KNOWN_EXTERNAL_TOOLS

    tool = (query.data or "").split(":", 2)[-1]
    if tool not in KNOWN_EXTERNAL_TOOLS:
        await query.answer("Неизвестный инструмент.", show_alert=True)
        return
    meta = KNOWN_EXTERNAL_TOOLS[tool]
    await state.set_state(SetupStates.awaiting_external_tool_key)
    await state.update_data(external_tool=tool)
    if query.message is not None:
        await query.message.answer(
            f"Пришли ключ для <b>{meta['label']}</b> одним сообщением. "
            "Я удалю твоё сообщение как только сохраню.\n\n"
            f"Получить: <a href='{meta['url']}'>{meta['url']}</a>\n\n"
            f"<i>{meta['hint']}</i>",
            disable_web_page_preview=True,
        )
    await query.answer()


# --- /cfg:* — sub-menu inside OpenRouter / Other --------------------------


@wizard_router.callback_query(F.data == "settings:back")
async def cb_settings_back(query: CallbackQuery, state: FSMContext) -> None:
    """Back arrow on the Settings sub-screen → re-render the main menu."""
    user = query.from_user
    if not can_use(user.id if user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    uid = user.id if user else None
    if query.message is not None:
        try:
            await query.message.edit_text(
                "Главное меню. Выбери что сделать:",
                reply_markup=_kb_main_after_claim(uid),
            )
        except Exception:  # noqa: BLE001
            await query.message.answer(
                "Главное меню. Выбери что сделать:",
                reply_markup=_kb_main_after_claim(uid),
            )
    await query.answer()


# ---- /roles:* — Settings → «🎭 Роли» submenu ----------------------------


def _roles_screen_body() -> str:
    """Top text for the «🎭 Роли» submenu — current persona + gate state."""
    persona = get_persona()
    override = storage.get_persona_override()
    source = "override (через /role)" if override is not None else "env BOT_PERSONA"
    gate_on = storage.get_role_gate_enabled()
    if gate_on:
        gate_text = (
            "<b>ВКЛ</b> — без выбранной роли /work, /exec, /clone и кнопки "
            "установки молчат."
        )
    else:
        gate_text = (
            "<b>ВЫКЛ</b> — все команды работают без выбора роли "
            "(по умолчанию)."
        )
    return (
        "<b>🎭 Роли</b>\n\n"
        f"Сейчас активна: <b>{persona.display_name}</b> "
        f"(<code>{persona.key}</code>, {source})\n"
        f"<i>{persona.title}</i>\n\n"
        f"<b>Требование роли:</b> {gate_text}"
    )


@wizard_router.callback_query(F.data == "roles:menu")
async def cb_roles_menu(query: CallbackQuery, state: FSMContext) -> None:
    """Open Settings → «🎭 Роли» submenu."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _roles_screen_body(), reply_markup=_kb_roles_menu()
            )
    await query.answer()


@wizard_router.callback_query(F.data == "roles:pick")
async def cb_roles_pick(query: CallbackQuery, state: FSMContext) -> None:
    """«Сменить роль» — open the existing role picker UI."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    await query.answer()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _role_picker_text(), reply_markup=_kb_role_picker()
            )


@wizard_router.callback_query(F.data == "roles:toggle")
async def cb_roles_toggle(query: CallbackQuery, state: FSMContext) -> None:
    """Flip the role gate ON ↔ OFF and re-render the submenu."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    new_state = not storage.get_role_gate_enabled()
    storage.set_role_gate_enabled(new_state)
    await query.answer(
        f"Требование роли: {'ВКЛ' if new_state else 'ВЫКЛ'}", show_alert=False
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _roles_screen_body(), reply_markup=_kb_roles_menu()
            )


@wizard_router.callback_query(F.data == "roles:back")
async def cb_roles_back(query: CallbackQuery, state: FSMContext) -> None:
    """«← Назад» from Роли submenu → re-render the Settings screen."""
    user = query.from_user
    if not can_use(user.id if user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    uid = user.id if user else None
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>⚙️ Настройки</b>",
                reply_markup=_kb_settings_menu(uid),
            )
    await query.answer()


# ---- /tts:* — Settings → «🔊 Голосовой ответчик» submenu --------------


def _tts_screen_body() -> str:
    """Top text for the TTS submenu — current state + short explainer."""
    enabled = storage.get_tts_enabled()
    state_line = (
        "<b>Сейчас:</b> 🔊 ВКЛЮЧЕНО — каждый текстовый ответ бота "
        "приходит ещё и голосом."
        if enabled
        else "<b>Сейчас:</b> 🔇 ВЫКЛЮЧЕНО — бот отвечает только текстом."
    )
    # Show which voice / provider is currently active so the user can
    # verify the picker actually took effect.
    provider = storage.get_tts_provider()
    custom = storage.get_tts_custom_voice_path()
    voice = storage.get_tts_voice() or "M1"
    if provider == "elevenlabs":
        active_line = "<b>Активный голос:</b> ☁️ ElevenLabs (облако)."
    elif provider == "clone" and custom:
        active_line = (
            "<b>Активный голос:</b> 🎤 кастомный клон (Supertonic)."
        )
    else:
        active_line = (
            f"<b>Активный голо��:</b> 🎙 <code>{voice}</code> (Supertonic)"
        )
    # Surface why TTS doesn't work when the supertonic install is
    # broken (e.g. missing ffmpeg / model download failed). Without
    # this hint the user has no idea why the voice never plays.
    try:
        from .tts import get_unavailable_reason

        reason = get_unavailable_reason()
    except Exception:  # noqa: BLE001
        reason = None
    warn_line = (
        f"\n\n⚠️ <b>TTS недоступен:</b> {_html_escape(reason)}"
        if reason
        else ""
    )
    return (
        "<b>🔊 Голосовой ответчик</b>\n\n"
        f"{state_line}\n"
        f"{active_line}{warn_line}\n\n"
        "Озвучивает то, что бот сам тебе пишет (через on-device "
        "Supertonic TTS). Код, html-тэги и сырые ссылки в озвучке "
        "пропускаются; для github-ссылок читается «репо от создателей "
        "&lt;имя&gt;». Бот сам ничего в интернете не ищет — читает "
        "только то, что он уже сформулировал.\n\n"
        "Выбрал голос или импортировал JSON — озвучка включается "
        "автоматически. Хочешь выключить — жми «🔇 Выключить»."
    )


@wizard_router.callback_query(F.data == "tts:menu")
async def cb_tts_menu(query: CallbackQuery, state: FSMContext) -> None:
    """Open Settings → «🔊 Голосовой ответчик» submenu."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _tts_screen_body(), reply_markup=_kb_tts_menu()
            )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:on")
async def cb_tts_on(query: CallbackQuery, state: FSMContext) -> None:
    """«Авто включить» — enable narration and re-render the submenu."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    storage.set_tts_enabled(True)
    await query.answer("Голосовой ответчик: ВКЛ", show_alert=False)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _tts_screen_body(), reply_markup=_kb_tts_menu()
            )


@wizard_router.callback_query(F.data == "tts:off")
async def cb_tts_off(query: CallbackQuery, state: FSMContext) -> None:
    """«Выключить» — disable narration and re-render the submenu."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    storage.set_tts_enabled(False)
    await query.answer("Голосовой ответчик: ВЫКЛ", show_alert=False)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _tts_screen_body(), reply_markup=_kb_tts_menu()
            )


@wizard_router.callback_query(F.data == "tts:back")
async def cb_tts_back(query: CallbackQuery, state: FSMContext) -> None:
    """«← Назад» from TTS submenu → re-render the Settings screen."""
    user = query.from_user
    if not can_use(user.id if user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    uid = user.id if user else None
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>⚙️ Настройки</b>",
                reply_markup=_kb_settings_menu(uid),
            )
    await query.answer()


# ---- /tts:use:* — provider switch (local / clone / elevenlabs) -----------


@wizard_router.callback_query(F.data.startswith("tts:use:"))
async def cb_tts_use(query: CallbackQuery, state: FSMContext) -> None:
    """Mark a TTS provider as active.

    Three callbacks: ``tts:use:local`` (Supertonic built-in voices),
    ``tts:use:clone`` (Supertonic clone), ``tts:use:elevenlabs``
    (ElevenLabs HTTP). Auto-enables narration so the user instantly
    hears the change instead of having to flip the on/off toggle too.
    """
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    provider = (query.data or "").split(":")[-1]
    if provider not in ("local", "clone", "elevenlabs"):
        await query.answer("Неизвестный провайдер.", show_alert=True)
        return
    # Guard against picking "clone" without a saved clone — otherwise
    # storage.get_tts_provider() will silently downgrade to "local"
    # and the user will be confused.
    if provider == "clone" and not storage.get_tts_custom_voice_path():
        await query.answer(
            "Сначала запиши голос или импортируй JSON клона.",
            show_alert=True,
        )
        return
    if provider == "elevenlabs" and (
        not storage.get_elevenlabs_api_key()
        or not storage.get_elevenlabs_voice_id()
    ):
        await query.answer(
            "Сначала пришли API ключ ElevenLabs и Voice ID.",
            show_alert=True,
        )
        return
    storage.set_tts_provider(provider)
    storage.set_tts_enabled(True)
    label = {
        "local": "встроенный голос (Supertonic)",
        "clone": "клонированный голос",
        "elevenlabs": "ElevenLabs",
    }[provider]
    await query.answer(f"Используется: {label}", show_alert=False)
    # Re-render the same screen so the ✅ marker updates immediately.
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _tts_screen_body(),
                reply_markup=_kb_tts_menu(),
            )


# ---- /tts:el:* — ☁️ ElevenLabs subsection --------------------------------


def _elevenlabs_screen_body() -> str:
    """Top text for the ElevenLabs subsection — shows what's configured."""
    api_set = bool(storage.get_elevenlabs_api_key())
    voice_id = storage.get_elevenlabs_voice_id()
    provider = storage.get_tts_provider()
    lines = [
        "<b>☁️ ElevenLabs</b> — облачный TTS.",
        "",
        f"🔑 API ключ: {'✅ задан' if api_set else '❌ не задан'}",
        (
            f"🎤 Voice ID: <code>{_html_escape(voice_id)}</code>"
            if voice_id
            else "🎤 Voice ID: ❌ не задан"
        ),
        (
            "▶ Используется: <b>да</b>"
            if provider == "elevenlabs"
            else "▶ Используется: нет"
        ),
        "",
        "Voice ID берётся из <a href='https://elevenlabs.io/app/voice-library'>"
        "Voice Library</a> — открой нужный голос и скопируй id (длинная "
        "hex-строка типа <code>21m00Tcm4TlvDq8ikWAM</code>). URL хардкод "
        "<code>https://api.elevenlabs.io</code>, поле тебе вводить не нужно.",
    ]
    return "\n".join(lines)


@wizard_router.callback_query(F.data == "tts:el")
async def cb_tts_el_menu(query: CallbackQuery, state: FSMContext) -> None:
    """Open Settings → 🔊 Голосовой ответчик → ☁️ ElevenLabs."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _elevenlabs_screen_body(),
                reply_markup=_kb_elevenlabs_menu(),
                disable_web_page_preview=True,
            )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:el:api")
async def cb_tts_el_api(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(SetupStates.awaiting_elevenlabs_api_key)
    if query.message is not None:
        await query.message.answer(
            "Пришли <b>API ключ ElevenLabs</b> одним сообщением.\n\n"
            "Где взять: <a href='https://elevenlabs.io/app/settings/api-keys'>"
            "elevenlabs.io/app/settings/api-keys</a>. Я удалю твоё сообщение "
            "сразу как сохраню ключ, чтобы он не остался в истории чата."
        )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:el:voiceid")
async def cb_tts_el_voiceid(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(SetupStates.awaiting_elevenlabs_voice_id)
    if query.message is not None:
        await query.message.answer(
            "Пришли <b>Voice ID</b> одним сообщением.\n\n"
            "Где взять: <a href='https://elevenlabs.io/app/voice-library'>"
            "elevenlabs.io/app/voice-library</a> — открой нужный голос, "
            "там кнопка «Copy ID». Формат: длинная hex-строка типа "
            "<code>21m00Tcm4TlvDq8ikWAM</code>."
        )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:el:use")
async def cb_tts_el_use(query: CallbackQuery, state: FSMContext) -> None:
    """Switch the active TTS provider to ElevenLabs.

    Refuses if API key or Voice ID is still empty so we don't trigger
    a silent fallback to Supertonic right after the user clicked
    «использовать».
    """
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    if not storage.get_elevenlabs_api_key() or not storage.get_elevenlabs_voice_id():
        await query.answer(
            "Сначала задай API ключ и Voice ID — иначе озвучк�� не сработает.",
            show_alert=True,
        )
        return
    storage.set_tts_provider("elevenlabs")
    storage.set_tts_enabled(True)
    await query.answer("ElevenLabs активирован", show_alert=False)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _elevenlabs_screen_body(),
                reply_markup=_kb_elevenlabs_menu(),
                disable_web_page_preview=True,
            )


@wizard_router.callback_query(F.data == "tts:el:back")
async def cb_tts_el_back(query: CallbackQuery, state: FSMContext) -> None:
    """«← Перенастроить мозг» — go up one level to the TTS submenu."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _tts_screen_body(), reply_markup=_kb_tts_menu()
            )
    await query.answer()


# ---- /api:batch — «🔌 API» mass-import button ----------------------------


def _api_batch_prompt() -> str:
    """Help text shown when the user opens the batch-import flow."""
    return (
        "<b>🔌 API — массовый импорт</b>\n\n"
        "Пришли одной строкой все ключи которые хочешь загрузить. "
        "Разделители:\n"
        "• <code>$$$</code> — между независимыми блоками\n"
        "• <code>;</code> — между полями в одном блоке\n"
        "• <code>:</code> — между target / key / value\n\n"
        "<b>Targets:</b> <code>slot1</code>…<code>slot20</code> (поля: "
        "<code>api</code>, <code>url</code>, <code>model1/2/3</code>, "
        "<code>budget</code>, <code>curl</code>, <code>balance</code>), "
        "<code>slotsall</code> (URL + model1/2/3 во все 20 разом), "
        "<code>brain1</code>, <code>brain2</code> (поля: "
        "<code>api</code>, <code>model</code>, <code>url</code>, "
        "<code>provider</code>), <code>elevenlabs</code> (<code>api</code>, "
        "<code>voice</code>), <code>tavily</code>, <code>brave</code>, "
        "<code>exa</code>, <code>firecrawl</code>, <code>apify</code>, "
        "<code>github</code> / <code>github_pat</code>.\n\n"
        "<b>Пример (20 слотов):</b>\n"
        "<code>slotsall:url:https://api.x/v1;slotsall:model1:claude opus 4.8;"
        "slotsall:model2:gpt 5.5$$$slot1:api:sk-aaa$$$slot2:api:sk-bbb;"
        "slot2:budget:100000</code>\n\n"
        "<b>Пример (мозги/инструменты):</b>\n"
        "<code>brain1:provider:openrouter;brain1:api:sk-or-xxx;"
        "brain1:model:anthropic/claude-3.5-sonnet$$$brain2:provider:"
        "custom;brain2:api:gsk_yyy;brain2:model:whisper-large-v3;"
        "brain2:url:https://api.groq.com/openai/v1$$$tavily:api:tvly-zzz"
        "$$$elevenlabs:api:el-www;elevenlabs:voice:21m00Tcm4TlvDq8ikWAM</code>\n\n"
        "Я применю всё что распознаю, сообщения с ключами удалю из "
        "истории чата. /cancel — отмена."
    )


@wizard_router.callback_query(F.data == "api:batch")
async def cb_api_batch(query: CallbackQuery, state: FSMContext) -> None:
    """Open the 🔌 API mass-import flow."""
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(SetupStates.awaiting_batch_api_blob)
    if query.message is not None:
        await query.message.answer(
            _api_batch_prompt(), disable_web_page_preview=True
        )
    await query.answer()


@wizard_router.message(
    StateFilter(SetupStates.awaiting_batch_api_blob), _NOT_A_COMMAND
)
async def capture_batch_api_blob(message: Message, state: FSMContext) -> None:
    """Parse the pasted blob and apply every recognised field.

    Returns a single summary banner: applied vs. errors. Deletes the
    user's message so the secrets never sit in chat history.
    """
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    blob = (message.text or "").strip()
    if not blob:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    with contextlib.suppress(Exception):
        await message.delete()
    from .api_batch import parse_and_apply

    report = parse_and_apply(blob, storage)
    await state.clear()
    lines: list[str] = ["<b>🔌 API — массовый импорт</b>", ""]
    if report.applied:
        lines.append("✅ Применено: " + ", ".join(
            f"<code>{a}</code>" for a in report.applied
        ))
    else:
        lines.append("⚠️ Ничего не применено.")
    if report.errors:
        lines.append("")
        lines.append("Ошибки:")
        for err in report.errors:
            lines.append(f"• {err}")
    await message.answer(
        "\n".join(lines),
        reply_markup=_kb_settings_menu(
            message.from_user.id if message.from_user else None
        ),
        disable_web_page_preview=True,
    )


# ---- /tts:voice:* — voice picker submenu (M1..M5 / F1..F5) ---------------


def _voice_picker_body() -> str:
    current_voice = storage.get_tts_voice() or "M1"
    custom = storage.get_tts_custom_voice_path()
    active = (
        "ваш клонированный голос (JSON)"
        if custom
        else f"<code>{current_voice}</code>"
    )
    return (
        "<b>🎙 Выбрать голос</b>\n\n"
        f"Сейчас активен: {active}.\n\n"
        "В Supertonic 5 мужских голосов (M1–M5) и 5 женских (F1–F5). "
        "Выбери раздел — там 5 кнопок-семплов."
    )


@wizard_router.callback_query(F.data == "tts:voice")
async def cb_tts_voice_root(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _voice_picker_body(), reply_markup=_kb_voice_picker_root()
            )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:voice:back")
async def cb_tts_voice_back(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _tts_screen_body(), reply_markup=_kb_tts_menu()
            )
    await query.answer()


@wizard_router.callback_query(F.data.in_({"tts:voice:male", "tts:voice:female"}))
async def cb_tts_voice_list(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    gender = (query.data or "").split(":")[-1]
    title = "👨 Мужской" if gender == "male" else "👩 Женский"
    body = (
        f"<b>{title}</b>\n\n"
        "Жми на кнопку — этот голос станет активным. После выбора можешь "
        "включить «🔊 Авто включить», и бот будет отвечать им."
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                body, reply_markup=_kb_voice_picker_list(gender)
            )
    await query.answer()


@wizard_router.callback_query(F.data.startswith("tts:voice:set:"))
async def cb_tts_voice_set(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    voice = (query.data or "").split(":")[-1].upper()

    from .tts import BUILTIN_VOICES

    if voice not in BUILTIN_VOICES:
        await query.answer("Неизвестный голос.", show_alert=True)
        return
    storage.set_tts_voice(voice)
    # Picking a built-in voice clears any custom Voice-Builder clone so
    # the next `synthesize_voice_ogg()` call picks up the built-in one,
    # and snaps the active provider back to ``local`` so we don't keep
    # routing through ElevenLabs after the user clicked a Supertonic
    # voice button.
    storage.clear_tts_custom_voice_path()
    storage.set_tts_provider("local")
    # Auto-enable the narrator — the user just told us "this is the
    # voice I want", so it's weird to make them flip a separate
    # toggle to actually hear it. Previously the menu accepted the
    # pick silently and the user thought "the voice doesn't work"
    # because TTS was still OFF by default.
    was_disabled = not storage.get_tts_enabled()
    storage.set_tts_enabled(True)
    notice = (
        f"Голос: {voice} · 🔊 Авто включить → ВКЛ"
        if was_disabled
        else f"Голос: {voice}"
    )
    await query.answer(notice, show_alert=False)
    # Re-render the picker list to update the ▶ marker.
    gender = "male" if voice.startswith("M") else "female"
    title = "👨 Мужской" if gender == "male" else "👩 Женский"
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"<b>{title}</b>\n\nАктивный голос: <code>{voice}</code>\n"
                "🔊 Авто включить: <b>ВКЛ</b>",
                reply_markup=_kb_voice_picker_list(gender),
            )


# ---- /tts:rec:* — «Запись голоса» submenu --------------------------------


def _voice_dir() -> Path:
    """Where user voice recordings/JSON clones live."""
    from .config import DATA_DIR

    d = DATA_DIR / "tts_voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _user_recording_audio_path(user_id: int) -> Path:
    return _voice_dir() / f"{user_id}_recording.ogg"


def _user_voice_json_path(user_id: int) -> Path:
    return _voice_dir() / f"{user_id}_voice.json"


def _record_menu_body(user_id: int) -> str:
    rec_audio = _user_recording_audio_path(user_id)
    rec_json = _user_voice_json_path(user_id)
    has_audio = rec_audio.exists()
    has_json = rec_json.exists()
    saved = bool(storage.get_tts_custom_voice_path())
    lines = ["<b>🎤 Запись голоса (клонирование)</b>", ""]
    if has_audio:
        lines.append("🎵 Аудио-сэмпл записан.")
    else:
        lines.append("🎵 Аудио-сэмпла ещё нет.")
    if has_json:
        lines.append("📄 JSON-стиль голоса лежит на диске.")
    else:
        lines.append("📄 JSON-стиля голоса ещё нет.")
    if saved:
        lines.append("✅ Активный голос — клонированный.")
    lines.extend([
        "",
        "Как сделать свой голос:",
        "1. Нажми <b>🔴 Запись</b> и пришли voice/audio (10–60 сек чистой речи).",
        "2. Нажми <b>🎶 Voice Builder</b> — там инструкция, как из этого аудио "
        "получить JSON-стиль (бесплатный веб-сервис Supertonic).",
        "3. Нажми <b>📥 Импорт (JSON)</b> и пришли полученный JSON.",
        "4. <b>🔊 Прослушать</b> — синтезирует фразу твоим голосом.",
        "5. <b>💾 Сохранить как активный</b> — бот будет говорить твоим голосом.",
        "",
        "Уже есть JSON от Voice Builder? Жми <b>📥 Импорт</b> сразу.",
    ])
    return "\n".join(lines)


@wizard_router.callback_query(F.data == "tts:rec")
async def cb_tts_rec_menu(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _record_menu_body(query.from_user.id),
                reply_markup=_kb_record_menu(),
            )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:rec:record")
async def cb_tts_rec_record(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(SetupStates.awaiting_voice_recording)
    if query.message is not None:
        await query.message.answer(
            "🎙 Жду аудио. Пришли voice-сообщение или файл (mp3 / m4a / ogg / wav). "
            "Чем чище запись и чем спокойнее темп речи — тем лучше склонируется. "
            "Длительность 10–60 секунд. Чтобы отменить — /cancel."
        )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:rec:listen")
async def cb_tts_rec_listen(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    json_path = _user_voice_json_path(query.from_user.id)
    if not json_path.exists():
        await query.answer(
            "Сначала импортируй JSON или сохрани клон.", show_alert=True
        )
        return
    await state.set_state(SetupStates.awaiting_voice_preview_text)
    await state.update_data(preview_voice_json=str(json_path))
    if query.message is not None:
        await query.message.answer(
            "📝 Пришли текст — я озвучу его твоим клонированным голосом. "
            "Чтобы отменить — /cancel."
        )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:rec:save")
async def cb_tts_rec_save(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    json_path = _user_voice_json_path(query.from_user.id)
    if not json_path.exists():
        await query.answer(
            "Сначала импортируй JSON или склонируй голос.", show_alert=True
        )
        return
    storage.set_tts_custom_voice_path(str(json_path))
    # Auto-enable narration so saving instantly translates into
    # spoken replies — same UX promise as «🔊 Авто включить» — and
    # mark the clone path as the active TTS provider so ElevenLabs
    # doesn't accidentally keep playing after the save.
    storage.set_tts_enabled(True)
    storage.set_tts_provider("clone")
    await query.answer(
        "Клонированный голос активирован · 🔊 Авто включить → ВКЛ",
        show_alert=False,
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _record_menu_body(query.from_user.id),
                reply_markup=_kb_record_menu(),
            )


@wizard_router.callback_query(F.data == "tts:rec:export")
async def cb_tts_rec_export(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    json_path = _user_voice_json_path(query.from_user.id)
    if not json_path.exists():
        await query.answer(
            "JSON клона ещё не создан — сначала импортируй его.",
            show_alert=True,
        )
        return
    from aiogram.types import FSInputFile

    file = FSInputFile(str(json_path), filename=f"voice_{query.from_user.id}.json")
    if query.message is not None:
        await query.message.answer_document(
            file,
            caption=(
                "📤 Экспорт твоего голоса. Этот JSON можно импортировать в "
                "другом боте — там кнопка «📥 Импорт» в этом же меню."
            ),
        )
    await query.answer("Экспортировал.", show_alert=False)


@wizard_router.callback_query(F.data == "tts:rec:import")
async def cb_tts_rec_import(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(SetupStates.awaiting_voice_json_import)
    if query.message is not None:
        await query.message.answer(
            "📥 Жду JSON-файл с голосом (то, что выдаёт Voice Builder либо "
            "экспортирован из другого бота). Просто пришли документ. "
            "Чтобы отменить — /cancel."
        )
    await query.answer()


@wizard_router.callback_query(F.data == "tts:rec:vb")
async def cb_tts_rec_vb(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id if query.from_user else 0):
        await query.answer("Только владелец.", show_alert=True)
        return
    body = (
        "<b>🎶 Voice Builder — как сделать свой голос-JSON</b>\n\n"
        "Supertonic умеет говорить любым голосом, но сам аудио → JSON "
        "локально не конвертирует. Для этого есть бесплатный веб-сервис:\n\n"
        "🔗 <a href=\"https://supertonic.supertone.ai/voice_builder\">supertonic.supertone.ai/voice_builder</a>\n\n"
        "Шаги:\n"
        "1. Открой ссылку выше в браузере.\n"
        "2. Нажми «<b>+ New Voice</b>», дай имя.\n"
        "3. Загрузи свой аудио-сэмпл (10–60 сек, моно, без шума).\n"
        "   Если ты уже прислал сюда запись — она лежит в "
        "<code>data/tts_voices/&lt;твой_id&gt;_recording.ogg</code> "
        "на сервере; скачай её и закинь в Voice Builder.\n"
        "4. Дождись «Voice ready», нажми «<b>Export → JSON</b>».\n"
        "5. Вернись сюда → «📥 Импорт (JSON)» → пришли скачанный файл.\n"
        "6. «💾 Сохранить как активный» — и бот говорит твоим голосом.\n\n"
        "Если бот не догадывается, как читать получившийся JSON — пришли "
        "его как есть, я разберу и подскажу что не так."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌐 Открыть Voice Builder",
                    url="https://supertonic.supertone.ai/voice_builder",
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="tts:rec")],
        ]
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                body, reply_markup=kb, disable_web_page_preview=True
            )
    await query.answer()


# ---- audio / text / json capture handlers --------------------------------


def _is_audioish_message(msg: Message) -> tuple[str, str] | None:
    """Return (file_id, suffix) when the message has a voice / audio / .ogg doc."""
    if msg.voice is not None:
        return msg.voice.file_id, ".ogg"
    if msg.audio is not None:
        mime = (msg.audio.mime_type or "").lower()
        suffix = (
            ".mp3" if "mpeg" in mime else ".m4a" if "mp4" in mime else ".audio"
        )
        return msg.audio.file_id, suffix
    if msg.document is not None:
        mime = (msg.document.mime_type or "").lower()
        if mime.startswith("audio/") or mime == "video/ogg":
            ext = ".ogg"
            name = (msg.document.file_name or "").lower()
            for cand in (".mp3", ".m4a", ".wav", ".ogg"):
                if name.endswith(cand):
                    ext = cand
                    break
            return msg.document.file_id, ext
    return None


@wizard_router.message(
    StateFilter(SetupStates.awaiting_voice_recording),
    F.voice | F.audio | F.document,
)
async def capture_voice_recording(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    target = _is_audioish_message(message)
    if target is None:
        await message.answer(
            "Не похоже на аудио. Пришли voice/audio или файл аудио. /cancel — отмена."
        )
        return
    file_id, suffix = target
    out_path = _user_recording_audio_path(message.from_user.id)
    if suffix != ".ogg":
        out_path = out_path.with_suffix(suffix)
    try:
        await message.bot.download(file_id, destination=str(out_path))
    except Exception as exc:  # noqa: BLE001
        await message.answer(
            f"Не получилось сохранить файл: <code>{_html_escape(str(exc))}</code>"
        )
        return
    await state.clear()
    await message.answer(
        "✅ Запись сохранена.\n"
        f"Файл: <code>{out_path}</code>\n\n"
        "Дальше — открой <b>🎶 Voice Builder</b> в меню записи, прогони "
        "файл через сервис и пришли мне получившийся JSON через "
        "<b>📥 Импорт</b>. Или, если JSON уже есть, импортируй его прямо сейчас.",
        reply_markup=_kb_record_menu(),
    )


@wizard_router.message(
    StateFilter(SetupStates.awaiting_voice_recording), _NOT_A_COMMAND
)
async def capture_voice_recording_wrong_type(
    message: Message, state: FSMContext
) -> None:
    """Anything other than audio while waiting for a recording — gentle nudge."""
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    await message.answer(
        "Я жду аудио (voice/audio/.ogg/.mp3). /cancel — отмена."
    )


@wizard_router.message(
    StateFilter(SetupStates.awaiting_voice_json_import), F.document
)
async def capture_voice_json(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    if message.document is None:
        return
    name = (message.document.file_name or "").lower()
    if not name.endswith(".json"):
        await message.answer("Жду JSON-файл (расширение .json). /cancel — отмена.")
        return
    out_path = _user_voice_json_path(message.from_user.id)
    try:
        await message.bot.download(message.document.file_id, destination=str(out_path))
    except Exception as exc:  # noqa: BLE001
        await message.answer(
            f"Не получилось сохранить JSON: <code>{_html_escape(str(exc))}</code>"
        )
        return
    # Quick sanity check — must be parseable JSON.
    import json as _json

    try:
        _json.loads(out_path.read_text())
    except Exception as exc:  # noqa: BLE001
        out_path.unlink(missing_ok=True)
        await message.answer(
            f"Файл не парсится как JSON: <code>{_html_escape(str(exc))}</code>"
        )
        return
    # Auto-activate the imported clone + auto-enable TTS + flip the
    # TTS provider selector to "clone". Without that last step the
    # bot would still route synthesis through whichever provider was
    # active before (e.g. ElevenLabs) and the freshly-uploaded clone
    # would sit on disk unused — most common "TTS не работает после
    # импорта" report.
    storage.set_tts_custom_voice_path(str(out_path))
    storage.set_tts_enabled(True)
    storage.set_tts_provider("clone")
    await state.clear()
    await message.answer(
        "✅ JSON голоса сохранён и включён как активный.\n"
        "🔊 Голосовой ответчик: <b>ВКЛ</b>, провайдер: <b>🎤 клон</b>.\n"
        "Все следующие текстовые ответы бота продублируются этим голосом.\n\n"
        "Если хочешь сначала послушать без активации — нажми "
        "<b>🔊 Прослушать</b>. Чтобы выключить озвучку — "
        "<b>🔇 Выключить</b> в меню «Голосовой ответчик».",
        reply_markup=_kb_record_menu(),
    )


@wizard_router.message(
    StateFilter(SetupStates.awaiting_voice_json_import), _NOT_A_COMMAND
)
async def capture_voice_json_wrong_type(
    message: Message, state: FSMContext
) -> None:
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    await message.answer("Жду JSON-файл (документ .json). /cancel — отмена.")


@wizard_router.message(
    StateFilter(SetupStates.awaiting_voice_preview_text), _NOT_A_COMMAND
)
async def capture_voice_preview_text(
    message: Message, state: FSMContext
) -> None:
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст. Пришли фразу.")
        return
    data = await state.get_data()
    json_path = str(data.get("preview_voice_json") or "")
    await state.clear()
    await message.bot.send_chat_action(message.chat.id, "record_voice")
    from .tts import synthesize_voice_ogg

    try:
        ogg = await synthesize_voice_ogg(text, custom_path=json_path or None)
    except Exception as exc:  # noqa: BLE001
        await message.answer(
            f"Озвучить не получилось: <code>{_html_escape(str(exc))}</code>"
        )
        return
    if not ogg:
        await message.answer(
            "Озвучить не получилось — Supertonic вернул пусто. Скорее всего "
            "JSON не валидный для Supertonic. Попробуй экспортировать заново "
            "через Voice Builder."
        )
        return
    from aiogram.types import BufferedInputFile

    voice_file = BufferedInputFile(ogg, filename="preview.ogg")
    await message.answer_voice(
        voice_file,
        caption="🔊 Прослушано твоим клонированным голосом.",
    )


# ---- «📚 Узнать о функционале» — LLM tutorial chat ----------------------


def _learn_intro_body() -> str:
    """Welcome message for the «Узнать о функционале» mode."""
    return (
        "<b>📚 Узнать о функционале</b>\n\n"
        "Я знаю всё о себе — спроси что угодно, отвечу через LLM (Мозг 1 "
        "или твой кастомный endpoint из ⚙️ Настройки → 🧠).\n\n"
        "Примеры вопросов:\n"
        "• как клонировать репо с github?\n"
        "• что умеет Helpzavr и как им пользоваться?\n"
        "• как включить голосовой ответчик?\n"
        "• как сменить роль / персону?\n"
        "• что делает «Соображалка»?\n"
        "• какие команды поддерживает Терминал?\n\n"
        "Пиши вопрос текстом — я отвечу. Чтобы выйти из режима — /cancel."
    )


def _learn_system_prompt() -> str:
    """Build a tutorial-mode system prompt with the full capability log."""
    from .capabilities import CAPABILITIES, CAPABILITIES_FILE

    blocks: list[str] = []
    if CAPABILITIES_FILE.exists():
        try:
            blocks.append(CAPABILITIES_FILE.read_text(errors="replace"))
        except Exception:  # noqa: BLE001
            logger.exception("could not read capabilities.md for /learn")
    if not blocks:
        # Fall back to the in-code list so the LLM is never empty-handed
        # (capabilities.md is generated on first boot only).
        for cap in CAPABILITIES:
            chunk = f"### {cap.title}\n{cap.description}"
            if cap.example:
                chunk += f"\n*Пример:* {cap.example}"
            blocks.append(chunk)
    capability_log = "\n\n".join(blocks)
    return (
        "Ты — встроенный помощник внутри Telegram-бота lilush. Твоя "
        "единственная задача — рассказывать пользователю, что умеет "
        "этот бот и как этим пользоваться. Отвечай по-русски, кратко, "
        "по делу, без воды. Если пользователь спрашивает не про бота "
        "(погода, история, философия) — вежливо верни разговор к "
        "функционалу бота.\n\n"
        "Главные возможности бота, на которые опирайся в ответах:\n\n"
        "• Главное меню после /start: 🤖 Helpzavr (скриншот-помощник), "
        "✨ Красивый текст (markdown → TG HTML), 📬 Проверка почты "
        "(IMAP), 🎬 Генерация фото и видео, 🐙 GitHub проекты "
        "(список склонированных репо), 🖥 Терминал (пакетный режим "
        "/work — список команд одним сообщением), и в самом низу "
        "⚙️ Настройки.\n"
        "• ⚙️ Настройки: 🎭 Роли (20 персон, переключатель «требовать "
        "роль»), 🔊 Голосовой ответчик (Supertonic TTS, выбор M1-M5 / "
        "F1-F5 или импорт собственного JSON-голоса через Voice Builder), "
        "🧠 Соображалка (3 стиля статус-индикатора), 💾 Память бота "
        "(размер истории, /reset), 💻 RAM (лимит и поведение при "
        "превышении), 🐙 GitHub проекты, 🖥 Терминал, 📚 Узнать о "
        "функционале (ты сейчас здесь), ⛔ Доступ (private/public/"
        "full_public + список со-владельцев).\n"
        "• Команды: /start, /chat, /setup, /role, /tokens, /reset, "
        "/work (пакетный терминал), /cancel (выйти из любой FSM-моды), "
        "/exec, /clone <url>, /git, /project, /cd, /github, /helpzavr, "
        "/pretty, /mailbox, /media, /settings.\n"
        "• Мозги: Мозг 1 — основной чат-LLM (OpenRouter / свой OpenAI-"
        "совместимый endpoint типа Groq, Together, vLLM, LM Studio). "
        "Мозг 2 — резерв для голосовых/фото перехватов (Groq Whisper / "
        "Vision). Auto-failover между слотами.\n"
        "• Внешние API: Apify, Firecrawl, Tavily, Brave Search, Exa, "
        "GitHub PAT — конфигурятся через /setup → 🛠 Внешние API.\n"
        "• GitHub: можешь кинуть ссылку в чат — бот сам склонирует "
        "репо (если включено авто-клонирование) и сможет читать "
        "файлы / запускать команды внутри.\n"
        "• Голосовой ответчик: чтобы заработал — выбрать голос (или "
        "импортнуть JSON) и нажать «🔊 Авто включить». На сервере "
        "нужны ffmpeg и пакет supertonic — оба ставятся "
        "автоматически при деплое (Dockerfile + requirements.txt).\n\n"
        "Если есть подробный лог возможностей — используй его:\n\n"
        f"{capability_log}"
    )


async def _answer_learn_question(message: Message, question: str) -> None:
    """Run a one-shot LLM call with the tutorial framing and reply."""
    from .agent import NoApiKeyError, oneshot_summary

    await message.bot.send_chat_action(message.chat.id, "typing")
    system = _learn_system_prompt()
    prompt = (
        f"{system}\n\n"
        f"Вопрос пользователя: {question}\n\n"
        "Ответь кратко и по делу, на русском."
    )
    try:
        answer = await oneshot_summary(prompt, purpose="learn")
    except NoApiKeyError as exc:
        await message.answer(
            "Не могу спросить LLM — Мозг 1 ещё не настроен.\n"
            f"<i>{_html_escape(str(exc))}</i>\n\n"
            "Открой ⚙️ Настройки → 🧠 Перенастроить мозг и заведи "
            "OpenRouter ключ (или свой OpenAI-совместимый endpoint)."
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("learn mode failed")
        await message.answer(
            f"Не получилось спросить LLM: <code>{_html_escape(str(exc))}</code>"
        )
        return
    if not answer:
        await message.answer(
            "LLM ничего не ответил. Попробуй переформулировать вопрос."
        )
        return
    await message.answer(answer)


@wizard_router.message(
    StateFilter(SetupStates.awaiting_learn_question), _NOT_A_COMMAND
)
async def capture_learn_question(message: Message, state: FSMContext) -> None:
    """Route the user's question to the LLM tutorial helper.

    Stays in :class:`SetupStates.awaiting_learn_question` after each
    answer so the user can keep asking follow-up questions. They
    leave the mode with /cancel (handled by the global ``cmd_cancel``).
    """
    if not can_use(message.from_user.id if message.from_user else None):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой вопрос. Напиши, что хочешь узнать.")
        return
    await _answer_learn_question(message, text)


async def _current_brain_slot(state: FSMContext) -> str:
    """Read the slot the user is currently configuring from FSM data.

    The slot is stashed by `cb_brain_slot` when the user enters a Brain
    1 / Brain 2 screen. Defaults to ``"1"`` when missing (e.g. user came
    through the legacy `/setup` → «Другое» path).
    """
    data = await state.get_data()
    raw = str(data.get("brain_cfg_slot") or "1")
    return raw if raw in ("1", "2") else "1"


@wizard_router.callback_query(F.data.startswith("brain_slot:"))
async def cb_brain_slot(query: CallbackQuery, state: FSMContext) -> None:
    """Open the per-slot config screen for Brain 1 or Brain 2.

    Sets ``brain_cfg_slot`` in FSM data so subsequent cfg:* callbacks
    and capture handlers know which slot to write into.
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot = (query.data or "").split(":", 1)[1]
    if slot not in ("1", "2"):
        await query.answer("Неизвестный слот.", show_alert=True)
        return
    await state.update_data(brain_cfg_slot=slot)
    if slot == "1":
        storage.set_brain("auto")
        if storage.get_provider() not in ("openrouter", "custom"):
            storage.set_provider("openrouter")
    provider, api_key, base_url, model = _slot_config(slot)
    if slot == "1":
        role_line = "Назначение: основной мозг для чата.\n\n"
        cta = "Нажми «✅ Готово» когда закончишь."
    else:
        role_line = (
            "Назначение: голос/фото перехваты через Groq (Whisper / Vision). "
            "Чат всегда обслуживается Мозгом 1.\n\n"
        )
        cta = "Нажми «✅ Готово» когда закончишь — слот сохранится, активным мозгом чата остаётся Мозг 1."
    body = (
        f"<b>🧠 Мозг {slot}</b>\n\n"
        f"{role_line}"
        f"Провайдер: <code>{_html_escape(provider)}</code>\n"
        f"URL: <code>{_html_escape(base_url) if base_url else '(по умолчанию)'}</code>\n"
        f"Модель: <code>{_html_escape(model) if model else '(не задана)'}</code>\n"
        f"API ключ: {'✅ задан' if api_key else '⚠️ пусто'}\n\n"
        f"{cta}"
    )
    if query.message is not None:
        await query.message.edit_text(body, reply_markup=_kb_brain_slot_cfg(slot))
    await query.answer()


@wizard_router.callback_query(F.data == "auto_brain:back")
async def cb_auto_brain_back(query: CallbackQuery, state: FSMContext) -> None:
    """«← Вернуться» from the Мозг 1 / Мозг 2 submenu → brain picker."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text("Выбери мозг:", reply_markup=_kb_brain())
    await query.answer()


@wizard_router.callback_query(F.data.startswith("fable5:"))
async def cb_fable5(query: CallbackQuery, state: FSMContext) -> None:
    """Fable5 submenu — 3 buttons: generate / disable / back.

    Owner-only (Fable5 is a full-bot remote control). On *gen* we always
    rotate the token + flip ``enabled`` on, so the owner can't acci-
    dentally hand out a stale link. On *off* we just clear the enabled
    flag; the token sticks around in storage so a future re-enable is
    cheap, but every new gen mints a brand-new one.
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return

    from .addons import fable5 as _fable5

    action = (query.data or "").split(":", 1)[1]
    await state.clear()

    if action == "back":
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "Выбери мозг:", reply_markup=_kb_brain()
                )
        return

    if action == "gen":
        _token, link = _fable5.generate_link()
        await query.answer("Ссылка сгенерирована 🔗")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _fable5_status_text(),
                    reply_markup=_kb_fable5(),
                    disable_web_page_preview=True,
                )
        return

    if action == "off":
        was = _fable5.disable_link()
        await query.answer(
            "Ссылка выключена 🛑" if was else "Ссылка уже была выключена",
            show_alert=False,
        )
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _fable5_status_text(),
                    reply_markup=_kb_fable5(),
                    disable_web_page_preview=True,
                )
        return

    # Unknown sub-action — no-op so we don't 500 the callback.
    await query.answer()


def _kb_localai() -> InlineKeyboardMarkup:
    """«🧠 Локальная ИИ» — пульт: выбор модели, вкл/выкл/удалить."""
    from .addons import local_ai

    enabled = local_ai.is_enabled()
    cur_model = local_ai.get_model()
    rows: list[list[InlineKeyboardButton]] = []
    # Model picker (one row per model, ▶ marks the selected one).
    for mid, meta in local_ai.MODELS.items():
        mark = "▶ " if mid == cur_model else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{meta['label']} · {meta['size']}",
            callback_data=f"localai:model:{mid}")])
    if enabled:
        rows.append([InlineKeyboardButton(text="🔌 Выключить", callback_data="localai:disable")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Включить", callback_data="localai:enable")])
    rows.append([InlineKeyboardButton(text="📦 Установить из хранилища", callback_data="localai:fromfile")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить модель", callback_data="localai:delete")])
    rows.append([InlineKeyboardButton(text="← Вернуться", callback_data="localai:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _localai_status_text() -> str:
    from .addons import local_ai

    model = local_ai.get_model()
    meta = local_ai.MODELS.get(model, {})
    ram = local_ai.system_ram_gb()
    ok, res = local_ai.resource_check(model)
    warn = "" if ok else f"\n\n⚠️ <b>{res}</b>"
    return (
        "<b>🧠 Локальная ИИ (Ollama)</b>\n\n"
        "Запускает модель прямо на твоём сервере — без внешних API и оплаты за токены. "
        "По «Включить» бот ставит Ollama, скачивает модель, поднимает локальный сервер "
        "и переключает чат-мозг на неё.\n\n"
        f"Выбрано: <b>{meta.get('label', model)}</b> ({meta.get('size','?')})\n"
        f"{meta.get('note','')}\n\n"
        f"Состояние: {local_ai.status_summary()}\n"
        f"Память сервера: ≈{ram or '?'} ГБ"
        f"{warn}"
    )


async def _localai_progress_editor(message):
    """Return a throttled async progress callback that edits *message*."""
    last = {"t": 0.0, "txt": ""}

    async def _cb(line: str) -> None:
        now = time.time()
        if now - last["t"] < 3.0 or line == last["txt"]:
            return
        last["t"] = now
        last["txt"] = line
        with contextlib.suppress(Exception):
            await message.edit_text(
                f"<b>🧠 Локальная ИИ</b>\n\n{line}",
                parse_mode="HTML",
            )

    return _cb


async def _localai_run_enable(message) -> None:
    from .addons import local_ai

    progress = await _localai_progress_editor(message)
    ok, msg = await local_ai.enable(progress=progress)
    with contextlib.suppress(Exception):
        await message.edit_text(
            f"<b>🧠 Локальная ИИ</b>\n\n{msg}",
            parse_mode="HTML",
            reply_markup=_kb_localai(),
            disable_web_page_preview=True,
        )


async def _localai_run_install_file(message, path: str) -> None:
    from .addons import local_ai

    progress = await _localai_progress_editor(message)
    ok, msg = await local_ai.install_from_gguf(path, progress=progress)
    with contextlib.suppress(Exception):
        await message.edit_text(
            f"<b>🧠 Локальная ИИ</b>\n\n{msg}",
            parse_mode="HTML",
            reply_markup=_kb_localai(),
            disable_web_page_preview=True,
        )


@wizard_router.callback_query(F.data.startswith("localai:"))
async def cb_localai(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    from .addons import local_ai

    action = query.data.split(":", 2)[-1] if query.data else "home"
    full = query.data or ""

    if full.startswith("localai:model:"):
        mid = full.split(":", 2)[-1]
        local_ai.set_model(mid)
        await query.answer(f"Модель: {local_ai.MODELS.get(mid, {}).get('label', mid)}")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_localai_status_text(), parse_mode="HTML",
                                          reply_markup=_kb_localai(),
                                          disable_web_page_preview=True)
        return

    if action == "home" or full == "localai:home":
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_localai_status_text(), parse_mode="HTML",
                                          reply_markup=_kb_localai(),
                                          disable_web_page_preview=True)
        return

    if action == "back":
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text("<b>🧠 Перенастроить мозг</b>", parse_mode="HTML",
                                          reply_markup=_kb_brain())
        return

    if action == "enable":
        # Resource gate before kicking off a multi-GB download.
        ok, res = local_ai.resource_check(local_ai.get_model())
        if not ok:
            await query.answer("Недостаточно ресурсов", show_alert=True)
            with contextlib.suppress(Exception):
                await query.message.edit_text(f"<b>🧠 Локальная ИИ</b>\n\n⛔ {res}",
                                              parse_mode="HTML", reply_markup=_kb_localai())
            return
        await query.answer("Запускаю установку…")
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🧠 Локальная ИИ</b>\n\n⏳ Подготовка… это может занять несколько минут "
                "(установка + загрузка модели).", parse_mode="HTML")
        # Run the long flow in the background so the handler returns fast.
        asyncio.create_task(_localai_run_enable(query.message))
        return

    if action == "disable":
        await query.answer("Выключаю…")
        ok, msg = await local_ai.disable()
        with contextlib.suppress(Exception):
            await query.message.edit_text(f"<b>🧠 Локальная ИИ</b>\n\n{msg}",
                                          parse_mode="HTML", reply_markup=_kb_localai())
        return

    if action == "delete":
        await query.answer("Удаляю модель…")
        with contextlib.suppress(Exception):
            await query.message.edit_text("<b>🧠 Локальная ИИ</b>\n\n🗑 Удаляю модель…",
                                          parse_mode="HTML")
        ok, msg = await local_ai.remove()
        with contextlib.suppress(Exception):
            await query.message.edit_text(f"<b>🧠 Локальная ИИ</b>\n\n{msg}",
                                          parse_mode="HTML", reply_markup=_kb_localai())
        return

    if action == "fromfile":
        await query.answer()
        files = local_ai.list_local_files()
        rows: list[list[InlineKeyboardButton]] = []
        for i, f in enumerate(files):
            rows.append([InlineKeyboardButton(
                text=f"📦 {f['name']} ({f['size_gb']} ГБ)",
                callback_data=f"localai:installfile:{i}")])
        rows.append([InlineKeyboardButton(text="📤 Загрузить файл сюда", callback_data="localai:upload")])
        rows.append([InlineKeyboardButton(text="← Назад", callback_data="localai:home")])
        listing = (
            "\n".join(f"• <code>{f['name']}</code> — {f['size_gb']} ГБ" for f in files)
            if files else "<i>файлов пока нет</i>"
        )
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🧠 Локальная ИИ → установка из хранилища</b>\n\n"
                f"Положи файл модели (<code>.gguf</code>) в папку на диске сервера:\n"
                f"<code>{local_ai.models_dir()}</code>\n\n"
                "…затем выбери его ниже. Для больших моделей (14B ≈ 9 ГБ) это "
                "правильный путь — Telegram-загрузка ограничена 20 МБ.\n\n"
                f"<b>Найдено файлов:</b>\n{listing}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                disable_web_page_preview=True)
        return

    if full.startswith("localai:installfile:"):
        idx = full.split(":")[-1]
        files = local_ai.list_local_files()
        try:
            chosen = files[int(idx)]
        except (ValueError, IndexError):
            await query.answer("Файл не найден — обнови список", show_alert=True)
            return
        await query.answer("Импортирую модель из файла…")
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"<b>🧠 Локальная ИИ</b>\n\n📦 Импортирую <code>{chosen['name']}</code>…",
                parse_mode="HTML")
        asyncio.create_task(_localai_run_install_file(query.message, chosen["path"]))
        return

    if action == "upload":
        await state.set_state(SetupStates.awaiting_localai_file)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🧠 Локальная ИИ → загрузка файла</b>\n\n"
                "Пришли файл модели (<code>.gguf</code>) сюда одним сообщением.\n\n"
                "⚠️ Telegram-боты принимают файлы только до <b>20 МБ</b>. "
                "Для больших моделей положи файл в папку "
                f"<code>{local_ai.models_dir()}</code> на сервере и выбери его в списке.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data="localai:fromfile")]]))
        return

    await query.answer()


def _kb_hermes() -> InlineKeyboardMarkup:
    from .addons import state as addon_state
    hermes_enabled = addon_state.get("algorithm", "hermes_enabled", False)
    toggle_label = "🤖 Хермес: ВКЛ" if hermes_enabled else "🤖 Хермес: ВЫКЛ"
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data="hermes:toggle")],
            [InlineKeyboardButton(text="🔄 Обновить с GitHub", callback_data="hermes:update")],
            [
                InlineKeyboardButton(text="📥 Импорт наработок", callback_data="hermes:import"),
                InlineKeyboardButton(text="📤 Экспорт наработок", callback_data="hermes:export")
            ],
            [InlineKeyboardButton(text="📄 Документ-инструкция (опыт)", callback_data="hermes:expdoc")],
            [InlineKeyboardButton(text="➕ Улучшить проект из Важного", callback_data="impj:home")],
            [InlineKeyboardButton(text="⏱ Тайминг авто-улучшений", callback_data="impj:timing")],
            [InlineKeyboardButton(text="🧹 Очистить память", callback_data="hermes:clear")],
            [InlineKeyboardButton(text="← Вернуться", callback_data="hermes:back")],
        ]
    )

def _hermes_status_text() -> str:
    from .addons import state as addon_state
    hermes_enabled = addon_state.get("algorithm", "hermes_enabled", False)
    status_label = "🟢 Активен" if hermes_enabled else "🔴 Выключен"
    
    # Count skills/developments
    skills_dir = Path(os.path.expanduser("~/.hermes/skills"))
    skills_count = 0
    if skills_dir.exists():
        skills_count = sum(1 for p in skills_dir.glob("*.md") if p.is_file())
        
    return (
        "<b>🤖 Nous Research Hermes Agent — пульт управления</b>\n\n"
        "Hermes Agent — это самообучающийся «мозг» бота. Когда ВКЛ, он:\n"
        "• подмешивает накопленный опыт (что сработало / что нет) в промпт "
        "планировщика — и тот выбирает более удачные пути;\n"
        "• после каждого плана оценивает результат и записывает урок;\n"
        "• со временем дополняет инструкции на основе реальной работы, а не "
        "интернета.\n\n"
        f"Статус интеграции: <b>{status_label}</b>\n"
        f"Накопленные навыки (память): <b>{skills_count} навыков</b>\n\n"
        "<i>Нажми «Импорт» или «Экспорт» для переноса наработок, или «Обновить с GitHub» для загрузки свежей версии фреймворка.</i>"
    )

async def _hermes_run_update(status_msg: Message) -> None:
    """Run the Hermes installer in the background with hard timeouts.

    Fix (buttons 31-32, problem 1): previously `curl ... | bash` was awaited
    directly inside the callback handler without any timeout — a hung network
    froze the handler (and the user) forever.
    """
    global _HERMES_UPDATE_ACTIVE
    proc = None
    try:
        # `set -o pipefail` is critical: without it a failed `curl` feeds
        # empty input to `bash`, which exits 0 — and the user would see a
        # false «успешно обновлён» message on a network error. /bin/sh may
        # be dash (no pipefail), so run explicitly through bash.
        proc = await asyncio.create_subprocess_exec(
            "bash", "-o", "pipefail", "-c",
            "curl -fsSL --connect-timeout 15 --max-time 240 "
            "https://hermes-agent.nousresearch.com/install.sh | bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=HERMES_UPDATE_TIMEOUT
        )
        if proc.returncode == 0:
            await status_msg.edit_text(
                "🎉 <b>Hermes Agent успешно обновлён до последней версии с GitHub!</b>\n\n"
                "Все наработки и навыки сохранены. Фреймворк готов к работе."
            )
        else:
            err_text = (stderr or b"").decode("utf-8", "replace")[:500]
            await status_msg.edit_text(f"❌ Ошибка обновления: {err_text}")
    except asyncio.TimeoutError:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            # Reap the killed process so it doesn't linger as a zombie.
            with contextlib.suppress(Exception):
                await proc.wait()
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                "❌ Обновление прервано: инсталлятор не ответил за "
                f"{HERMES_UPDATE_TIMEOUT} секунд. Проверь сеть и попробуй ещё раз."
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("hermes update failed")
        with contextlib.suppress(Exception):
            await status_msg.edit_text(f"��� Ошибка: {e}")
    finally:
        _HERMES_UPDATE_ACTIVE = False


@wizard_router.callback_query(F.data.startswith("hermes:"))
async def cb_hermes(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
        
    action = (query.data or "").split(":", 1)[1]
    await state.clear()
    
    if action == "back":
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "Перенастройка. Выбери мозг:", reply_markup=_kb_brain()
                )
        return
        
    elif action == "toggle":
        from .addons import state as addon_state
        current = addon_state.get("algorithm", "hermes_enabled", False)
        addon_state.set_("algorithm", "hermes_enabled", not current)
        await query.answer(f"Интеграция Hermes Agent: {'ВКЛ' if not current else 'ВЫКЛ'}")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _hermes_status_text(),
                    reply_markup=_kb_hermes(),
                    disable_web_page_preview=True
                )
        return
        
    elif action == "update":
        global _HERMES_UPDATE_ACTIVE
        if _HERMES_UPDATE_ACTIVE:
            await query.answer(
                "⏳ Обновление уже идёт — дождись результата.", show_alert=True
            )
            return
        if query.message is None:
            await query.answer("Сообщение недоступно, открой меню заново.", show_alert=True)
            return
        await query.answer("Запускаю обновление с GitHub...")
        status_msg = await query.message.answer(
            "⏳ <b>Обновление Nous Research Hermes Agent...</b>\n"
            "• Подключаюсь к репозиторию NousResearch/hermes-agent...\n"
            "• Запускаю инсталлятор..."
        )
        # Fix (buttons 31-32, problem 1): the installer used to run inline in
        # this callback handler via `await proc.communicate()` WITHOUT any
        # timeout, freezing the whole dispatcher if the network hung. Now the
        # work runs in a background task with hard timeouts.
        _HERMES_UPDATE_ACTIVE = True
        _spawn_bg_task(_hermes_run_update(status_msg))
        return

    elif action == "export":
        await query.answer("Собираю наработки...")
        skills_dir = Path(os.path.expanduser("~/.hermes/skills"))
        if not skills_dir.exists() or not any(skills_dir.glob("*.md")):
            await query.answer("Наработок пока нет (память пуста)", show_alert=True)
            return
            
        import shutil
        zip_path = Path("session") / f"hermes_skills_{int(time.time())}"
        shutil.make_archive(str(zip_path), "zip", str(skills_dir))
        full_zip_path = Path(f"{zip_path}.zip")
        
        from aiogram.types import FSInputFile
        doc = FSInputFile(str(full_zip_path), filename="hermes_skills_backup.zip")
        await query.message.answer_document(
            document=doc,
            caption="📤 <b>Экспорт наработок Hermes Agent</b>\n\nВот архив твоих скомпилированных навыков и памяти ИИ. Сохрани его для резервной копии."
        )
        return
        
    elif action == "expdoc":
        # One-button export: Hermes compiles a Markdown document of extra
        # instructions distilled STRICTLY from accumulated experience
        # (what actually happened in the bot), not from anything read on
        # the internet.
        await query.answer("Собираю документ из опыта…")
        try:
            from .addons.hermes_experience import build_document, count

            doc = build_document()
            n = count()
            zip_dir = Path("session")
            zip_dir.mkdir(parents=True, exist_ok=True)
            md_path = zip_dir / f"hermes_experience_{int(time.time())}.md"
            md_path.write_text(doc, encoding="utf-8")

            from aiogram.types import FSInputFile

            file_doc = FSInputFile(str(md_path), filename="hermes_instructions_experience.md")
            await query.message.answer_document(
                document=file_doc,
                caption=(
                    "📄 <b>Документ-инструкция Гермеса (на основе опыта)</b>\n\n"
                    f"Записей опыта: <b>{n}</b>. Документ собран только из "
                    "того, что реально произошло в работе бота — не из "
                    "интернета. По мере работы он будет наполняться сам."
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("hermes expdoc failed")
            await query.message.answer(f"❌ Не удалось собрать документ: {e}")
        return

    elif action == "import":
        await state.set_state(SetupStates.awaiting_hermes_zip)
        await query.answer()
        await query.message.answer(
            "<b>📥 Импорт наработок Hermes Agent</b>\n\n"
            "Пришли мне <code>.zip</code> архив с твоими сохранёнными навыками (который ты ранее экспортировал):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="brain:hermes")]
            ])
        )
        return
        
    elif action == "clear":
        skills_dir = Path(os.path.expanduser("~/.hermes/skills"))
        if skills_dir.exists():
            import shutil
            shutil.rmtree(str(skills_dir))
            skills_dir.mkdir(parents=True, exist_ok=True)
            await query.answer("Память и навыки успешно очищены!", show_alert=True)
        else:
            await query.answer("Память уже пуста", show_alert=True)
            
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _hermes_status_text(),
                    reply_markup=_kb_hermes(),
                    disable_web_page_preview=True
                )
        return

@wizard_router.message(StateFilter(SetupStates.awaiting_hermes_zip))
async def msg_hermes_zip_submit(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
        
    if not message.document or not message.document.file_name.endswith(".zip"):
        await message.answer("❌ Пришли валидный <code>.zip</code> файл или нажми «↩️ Отмена»:")
        return
        
    await state.clear()
    status_msg = await message.answer("⏳ <b>Импортирую наработки...</b>")
    
    try:
        import zipfile
        import io
        
        buf = io.BytesIO()
        await message.bot.download(message.document, destination=buf)
        
        skills_dir = Path(os.path.expanduser("~/.hermes/skills"))
        skills_dir.mkdir(parents=True, exist_ok=True)
        
        with zipfile.ZipFile(buf) as z:
            z.extractall(str(skills_dir))
            
        await status_msg.edit_text(
            "🎉 <b>Наработки и память ИИ успешно импортированы!</b>\n\n"
            "Hermes Agent готов использовать восстановленные навыки в работе."
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка импорта: {e}")


@wizard_router.message(StateFilter(SetupStates.awaiting_localai_file))
async def msg_localai_file_submit(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    from .addons import local_ai

    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith((".gguf", ".bin")):
        await message.answer(
            "❌ Пришли файл модели <code>.gguf</code> (или нажми «✖ Отмена»).",
            parse_mode="HTML")
        return

    await state.clear()
    dest = os.path.join(local_ai.models_dir(), doc.file_name)
    status_msg = await message.answer(
        f"⏳ <b>Принимаю файл</b> <code>{doc.file_name}</code>…", parse_mode="HTML")
    try:
        await message.bot.download(doc, destination=dest)
    except Exception as e:  # noqa: BLE001 — usually the 20MB Bot API limit
        await status_msg.edit_text(
            f"❌ Не удалось скачать файл: {e}\n\n"
            "Telegram-боты принимают файлы только до 20 МБ. "
            f"Для больших моделей положи файл напрямую в "
            f"<code>{local_ai.models_dir()}</code> на сервере.",
            parse_mode="HTML")
        return

    await status_msg.edit_text(
        f"📦 Файл сохранён. Импортирую <code>{doc.file_name}</code> в Ollama…",
        parse_mode="HTML")
    asyncio.create_task(_localai_run_install_file(status_msg, dest))


@wizard_router.callback_query(F.data.startswith("test:"))
async def cb_test(query: CallbackQuery, state: FSMContext) -> None:
    """«🧪 Тест» submenu — generate / disable / help / back. Owner-only.

    Mirrors :func:`cb_fable5`: every *gen* rotates the token and flips
    ``enabled`` on; *off* just clears the flag (token sticks around so
    a later re-enable reuses it, but each gen mints a fresh one).
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return

    from .addons import test as _test

    action = (query.data or "").split(":", 1)[1]
    await state.clear()

    if action == "back":
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "Главное меню:",
                    reply_markup=_kb_main_after_claim(query.from_user.id if query.from_user else None),
                )
        return

    if action == "gen":
        _token, link = _test.generate_link()
        await query.answer("Ссылка сгенерирована 🔗")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _test_status_text(),
                    reply_markup=_kb_test(),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        return

    if action == "off":
        was = _test.disable_link()
        await query.answer(
            "Ссылка выключена 🛑" if was else "Ссылка уже была выключена",
            show_alert=False,
        )
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _test_status_text(),
                    reply_markup=_kb_test(),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        return

    if action == "help":
        await query.answer()
        if query.message is not None:
            await query.message.answer(
                "<b>🧪 Тест — что это</b>\n\n"
                "Кнопка, чтобы в один тап показать задеплоенный проект. "
                "Сам бот не деплоит — ты коммитишь на GitHub, оттуда "
                "автодеплой на Render / Oracle / другой хост. Бот "
                "просто помнит URL текущего таргета и открывает его по "
                "«🔗 Открыть превью».\n\n"
                "Возможные таргеты:\n"
                "  • 🚀 <b>Render</b> — URL Render web-service\n"
                "  • 🐙 <b>Oracle</b> — URL Oracle Cloud-инстанса\n"
                "  • ⚙️ <b>Custom</b> — любой свой URL (можно несколько)\n"
                "  • 🤖 <b>Lilush surface</b> — каркас на этом же боте, "
                "    <code>/test/&lt;token&gt;/</code> (token-gated)\n\n"
                "Lilush-surface полезен как proof-of-life: маршрут "
                "точно поднят, token-gate работает, surface живёт "
                "параллельно polling/webhook'у бота. Сюда позже "
                "сможет постить превью отдельный кодер-бот.",
                parse_mode=ParseMode.HTML,
            )
        return

    if action == "targets" or action == "targets:back":
        # Show the «🎯 Куда хостить» submenu.
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _test_targets_text(),
                    reply_markup=_kb_test_targets(),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        return

    if action.startswith("set:"):
        # Switch the active target. If the chosen slot has no URL yet
        # we still flip the active id but show a nudge to fill it in.
        target_id = action.split(":", 1)[1]
        if target_id == "lilush" or target_id.startswith("custom:") or target_id in {"render", "oracle"}:
            url_now = _test.get_target_url(target_id)
            _test.set_active_target(target_id)
            if not url_now and target_id in {"render", "oracle"}:
                await query.answer(
                    f"{_test.get_target_label(target_id)} активен, но URL ещё не задан — нажми «✏️ Изменить URL».",
                    show_alert=True,
                )
            else:
                await query.answer(f"Активный таргет: {_test.get_target_label(target_id)}")
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await query.message.edit_text(
                        _test_targets_text(),
                        reply_markup=_kb_test_targets(),
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
            return
        await query.answer("Неизвестный таргет.", show_alert=True)
        return

    if action.startswith("edit:"):
        # Prompt the user to send the URL for the chosen slot.
        slot = action.split(":", 1)[1]
        if slot not in {"render", "oracle", "custom"}:
            await query.answer("Неизвестный слот.", show_alert=True)
            return
        await state.set_state(SetupStates.awaiting_test_target_url)
        await state.update_data(test_target_id=slot)
        await query.answer()
        if query.message is not None:
            slot_name = {
                "render": "🚀 Render",
                "oracle": "🐙 Oracle",
                "custom": "⚙️ Custom",
            }[slot]
            await query.message.answer(
                f"Пришли URL для слота <b>{slot_name}</b> одним сообщением.\n"
                f"Например: <code>https://my-bot.onrender.com</code>\n\n"
                f"Отмена: /cancel",
                parse_mode=ParseMode.HTML,
            )
        return

    if action.startswith("del:custom:"):
        try:
            idx = int(action.split(":")[2])
        except Exception:  # noqa: BLE001
            await query.answer("Кривой индекс.", show_alert=True)
            return
        ok = _test.remove_custom_target(idx)
        await query.answer("Удалено 🗑" if ok else "Уже нет", show_alert=False)
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _test_targets_text(),
                    reply_markup=_kb_test_targets(),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        return

    # Unknown sub-action — no-op so we don't 500 the callback.
    await query.answer()


@wizard_router.message(
    StateFilter(SetupStates.awaiting_test_target_url), _NOT_A_COMMAND
)
async def capture_test_target_url(message: Message, state: FSMContext) -> None:
    """Capture a URL for the Render / Oracle / Custom test target."""
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    url = (message.text or "").strip()
    if not url:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    # Light validation — we accept anything that looks vaguely URL-y
    # so the user can paste IP:port for local testing. Real safety
    # comes from owner-gate + the fact we just store the string.
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer(
            "URL должен начинаться с <code>http://</code> или "
            "<code>https://</code>. Попробуй ещё раз.",
            parse_mode=ParseMode.HTML,
        )
        return
    data = await state.get_data()
    slot = str(data.get("test_target_id") or "")
    await state.clear()

    from .addons import test as _test

    if slot == "render":
        _test.set_target_url("render", url)
        if _test.get_active_target_id() not in {"render"}:
            # Auto-activate the slot the user just filled in — they
            # almost certainly want to use it right away.
            _test.set_active_target("render")
        msg = f"🚀 Render URL сохранён: <code>{_html_escape(url)}</code>"
    elif slot == "oracle":
        _test.set_target_url("oracle", url)
        if _test.get_active_target_id() not in {"oracle"}:
            _test.set_active_target("oracle")
        msg = f"🐙 Oracle URL сохранён: <code>{_html_escape(url)}</code>"
    elif slot == "custom":
        idx = _test.add_custom_target(url)
        _test.set_active_target(f"custom:{idx}")
        msg = f"⚙️ Custom URL добавлен: <code>{_html_escape(url)}</code>"
    else:
        msg = "Слот неизвестен — открой «🧪 Тест → 🎯 Куда хостить» ещё раз."

    await message.answer(
        msg + "\n\n" + _test_targets_text(),
        reply_markup=_kb_test_targets(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@wizard_router.callback_query(F.data == "cfg:back")
async def cb_cfg_back(query: CallbackQuery, state: FSMContext) -> None:
    """Back arrow inside the per-slot / legacy config screen.

    Routes back to the Мозг 1 / Мозг 2 submenu if the user came in
    through a slot button (slot stashed in FSM data), otherwise back
    to the brain picker so the legacy «Другое» flow keeps working.
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    came_via_slot = bool(data.get("brain_cfg_slot"))
    await state.clear()
    if query.message is not None:
        if came_via_slot:
            await query.message.edit_text(
                "<b>🧠 Авто мозг</b>\n\nВыбери слот:",
                reply_markup=_kb_auto_brain(),
            )
        else:
            await query.message.edit_text(
                "Выбери мозг:",
                reply_markup=_kb_brain(),
            )
    await query.answer()


@wizard_router.callback_query(F.data == "cfg:done")
async def cb_cfg_done(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot = await _current_brain_slot(state)
    await state.clear()
    summary = _config_summary()
    # Slot 1 — chat brain. Slot 2 is reserved for voice/photo via the
    # Groq override and is NEVER promoted to chat duty here. See
    # ``bot/agent.py::_slot_chain`` for the routing rule.
    if slot == "2":
        tail = (
            "Мозг 2 используется только для голосовых/фото перехватов "
            "(Groq Whisper / Vision). Чат по-прежнему идёт через Мозг 1."
        )
    else:
        tail = (
            "Теперь любое сообщение без <code>/</code> уйдёт в LLM. "
            "/help — все команды."
        )
    if query.message is not None:
        await query.message.edit_text(
            f"<b>Готово.</b>\n\n{summary}\n\n{tail}"
        )
    await query.answer()


@wizard_router.callback_query(F.data == "cfg:api")
async def cb_cfg_api(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot = await _current_brain_slot(state)
    await state.update_data(brain_cfg_slot=slot)  # preserve across set_state
    await state.set_state(SetupStates.awaiting_api_key)
    if query.message is not None:
        provider, _, base_url, _ = _slot_config(slot)
        if provider == "openrouter":
            hint = "Получить: <a href='https://openrouter.ai/keys'>openrouter.ai/keys</a>"
        elif "groq.com" in (base_url or "").lower():
            hint = "Получить: <a href='https://console.groq.com/keys'>console.groq.com/keys</a>"
        else:
            hint = "Это твой ключ от того endpoint-а, который ты указал в URL"
        await query.message.answer(
            f"Пришли API-ключ для <b>Мозга {slot}</b> <b>одним сообщением</b>. "
            "Я удалю его из чата как только сохраню.\n\n" + hint
        )
    await query.answer()


# Quick-pick model presets for Brain 2 (Groq voice / vision slot). Tap a
# button = save the model directly; «✍️ Свой вариант» falls back to the
# original text-input flow for anything not on the list.
_BRAIN2_MODEL_PRESETS: dict[str, str] = {
    "whisper": "whisper-large-v3",
    "whisperturbo": "whisper-large-v3-turbo",
    "scout": "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama70": "llama-3.3-70b-versatile",
}


def _kb_brain2_model_picker() -> InlineKeyboardMarkup:
    """Quick-pick keyboard shown when the user opens «🤖 Модель» for
    Brain 2 — the slot for Groq STT (whisper-*) and vision/chat models.
    The current default (whisper-large-v3) is marked with ⭐.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎙 whisper-large-v3 ⭐ дефолт",
                    callback_data="cfg:m2p:whisper",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎙 whisper-large-v3-turbo",
                    callback_data="cfg:m2p:whisperturbo",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🦙 llama-4-scout-17b-16e-instruct",
                    callback_data="cfg:m2p:scout",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🦙 llama-3.3-70b-versatile",
                    callback_data="cfg:m2p:llama70",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Свой вариант",
                    callback_data="cfg:model_freetext",
                )
            ],
            [
                InlineKeyboardButton(
                    text="← Перенастроить мозг",
                    callback_data="cfg:back",
                )
            ],
        ]
    )


@wizard_router.callback_query(F.data == "cfg:model")
async def cb_cfg_model(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot = await _current_brain_slot(state)
    await state.update_data(brain_cfg_slot=slot)
    # Brain 2: show a quick-pick keyboard (Groq voice / vision presets)
    # before falling back to free-text input. Brain 1: open-ended text
    # since the OpenRouter / custom space is too large to enumerate.
    if slot == "2":
        if query.message is not None:
            await query.message.answer(
                "Выбери модель для <b>Мозга 2</b>. Кликни одну из "
                "популярных (сохранится сразу) или «✍️ Свой вариант», "
                "чтобы ввести вручную.",
                reply_markup=_kb_brain2_model_picker(),
            )
        await query.answer()
        return
    await state.set_state(SetupStates.awaiting_model)
    if query.message is not None:
        provider, _, base_url, _ = _slot_config(slot)
        if provider == "openrouter":
            hint = (
                "Примеры:\n"
                "• <code>nvidia/nemotron-3-super-120b-a12b:free</code> (бесплатно)\n"
                "• <code>anthropic/claude-opus-4.7</code> (платно, лучшее качество)\n"
                "• <code>openai/gpt-5</code>\n"
                "Полный список: <a href='https://openrouter.ai/models'>openrouter.ai/models</a>"
            )
        elif "groq.com" in (base_url or "").lower():
            hint = (
                "Примеры моделей Groq:\n"
                "• <code>meta-llama/llama-4-scout-17b-16e-instruct</code>\n"
                "• <code>llama-3.3-70b-versatile</code>\n"
                "Список: <a href='https://console.groq.com/docs/models'>console.groq.com/docs/models</a>"
            )
        else:
            hint = (
                "Имя модели для твоего endpoint-а. Например <code>llama3.1:70b</code>, "
                "<code>mistral-large</code>, <code>gpt-4o</code>."
            )
        await query.message.answer(
            f"Пришли имя модели для <b>Мозга {slot}</b> одним сообщением.\n\n" + hint
        )
    await query.answer()


@wizard_router.callback_query(F.data.startswith("cfg:m2p:"))
async def cb_brain2_model_preset(query: CallbackQuery, state: FSMContext) -> None:
    """Save the picked preset model for Brain 2 in one click.

    Mirrors the post-save banner produced by the text-input flow so the
    feedback feels identical regardless of which path the user took.
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    key = (query.data or "").split(":", 2)[-1]
    model = _BRAIN2_MODEL_PRESETS.get(key)
    if not model:
        await query.answer("Неизвестная модель.", show_alert=True)
        return
    storage.set_brain_slot2_field("model", model)
    await state.update_data(brain_cfg_slot="2")
    summary = _config_summary()
    if query.message is not None:
        await query.message.answer(
            f"Модель для Мозга 2 сохранена: <code>{_html_escape(model)}</code>\n\n{summary}",
            reply_markup=_kb_brain_slot_cfg("2"),
        )
    await query.answer("Принято")


@wizard_router.callback_query(F.data == "cfg:model_freetext")
async def cb_brain2_model_freetext(query: CallbackQuery, state: FSMContext) -> None:
    """Brain 2 user clicked «✍️ Свой вариант» — fall through to the
    original free-text capture so they can paste any Groq model name.
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.update_data(brain_cfg_slot="2")
    await state.set_state(SetupStates.awaiting_model)
    if query.message is not None:
        await query.message.answer(
            "Пришли имя модели для <b>Мозга 2</b> одним сообщением.\n\n"
            "Примеры моделей Groq:\n"
            "• <code>meta-llama/llama-4-scout-17b-16e-instruct</code>\n"
            "• <code>llama-3.3-70b-versatile</code>\n"
            "• <code>whisper-large-v3</code>\n"
            "Список: <a href='https://console.groq.com/docs/models'>console.groq.com/docs/models</a>"
        )
    await query.answer()


@wizard_router.callback_query(F.data == "cfg:url")
async def cb_cfg_url(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot = await _current_brain_slot(state)
    provider, _, _, _ = _slot_config(slot)
    if False:  # URL now editable for all providers including OpenRouter
        pass
    await state.update_data(brain_cfg_slot=slot)
    await state.set_state(SetupStates.awaiting_url)
    if query.message is not None:
        await query.message.answer(
            f"Пришли URL endpoint-а для <b>Мозга {slot}</b> одним сообщением. "
            "Должен быть OpenAI-совместимым (заканчивается на <code>/v1</code>).\n\n"
            "Примеры:\n"
            "• <code>https://api.together.xyz/v1</code>\n"
            "• <code>https://api.groq.com/openai/v1</code>\n"
            "• <code>http://localhost:11434/v1</code> (Ollama)\n"
            "• <code>http://10.0.0.5:8000/v1</code> (свой vLLM)"
        )
    await query.answer()



@wizard_router.callback_query(F.data == "cfg:code")
async def cb_cfg_code(query: CallbackQuery, state: FSMContext) -> None:
    """Handle 'Код (cURL)' button — store raw code/cURL for the brain slot."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot = await _current_brain_slot(state)
    await state.update_data(brain_cfg_slot=slot)
    await state.set_state(SetupStates.awaiting_code)
    if query.message is not None:
        await query.message.answer(
            f"Пришли код (cURL или другой) для <b>Мозга {slot}</b> одним сообщением.\n\n"
            "Пример:\n"
            "<code>curl -X POST https://api.example.com/v1/chat/completions \\\n"
            "  -H \"Authorization: Bearer sk-...\" \\\n"
            "  -d '{{\"model\": \"gpt-4\", \"messages\": [...]}}'</code>"
        )
    await query.answer()


@wizard_router.callback_query(F.data == "cfg:clear")
async def cb_cfg_clear(query: CallbackQuery, state: FSMContext) -> None:
    """Handle 'Убрать настройки' — clear the current brain slot."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot = await _current_brain_slot(state)
    if slot == "2":
        storage.set_brain_slot2_field("api_key", "")
        storage.set_brain_slot2_field("base_url", "")
        storage.set_brain_slot2_field("model", "")
        storage.set_brain_slot2_field("provider", "")
    else:
        storage.set_provider_key("openrouter", "")
        storage.set_base_url("")
        storage.set_model("")
    came_via_slot = bool((await state.get_data()).get("brain_cfg_slot"))
    summary = _config_summary()
    kb = _kb_brain_slot_cfg(slot) if came_via_slot else _kb_llm_config(_provider_label_from_storage())
    if query.message is not None:
        await query.message.edit_text(
            f"🗑 <b>Мозг {slot} — настройки очищены.</b>\n\n{summary}",
            reply_markup=kb)
    await query.answer("Настройки очищены!")


@wizard_router.message(StateFilter(SetupStates.awaiting_code), _NOT_A_COMMAND)
async def capture_code(message: Message, state: FSMContext) -> None:
    """Capture code/cURL input for brain slot."""
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    code_text = (message.text or "").strip()
    if not code_text:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    data = await state.get_data()
    slot = str(data.get("brain_cfg_slot") or "1")
    # Store code in addon state (separate from main brain config)
    from .addons import state as _addon_state
    _addon_state.set_("brain_code", f"slot_{slot}", code_text)
    came_via_slot = bool(data.get("brain_cfg_slot"))
    await state.clear()
    if came_via_slot:
        await state.update_data(brain_cfg_slot=slot)
    summary = _config_summary()
    kb = _kb_brain_slot_cfg(slot) if came_via_slot else _kb_llm_config(_provider_label_from_storage())
    await message.answer(
        f"✅ Код для Мозга {slot} сохранён ({len(code_text)} символов).\n\n{summary}",
        reply_markup=kb)


# --- text capture for FSM --------------------------------------------------
# Filter out commands so users can /cancel, /setup, /help mid-wizard without
# their command being treated as the requested input. (`_NOT_A_COMMAND` is
# also referenced by the voice-recording / JSON-import handlers above, so
# the definition must precede them — see the early definition near the
# top of this module.)


@wizard_router.message(StateFilter(SetupStates.awaiting_api_key), _NOT_A_COMMAND)
async def capture_api_key(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    key = (message.text or "").strip()
    if not key:
        await message.answer("Пустое сообщение, попробуй ещё раз или нажми «← Назад».")
        return
    data = await state.get_data()
    slot = str(data.get("brain_cfg_slot") or "1")
    if slot not in ("1", "2"):
        slot = "1"
    # Save FIRST, then delete the message with the secret. If save fails we
    # want the user to be able to retry without re-typing the key blind.
    try:
        if slot == "2":
            storage.set_brain_slot2_field("api_key", key)
        else:
            # Brain 1 — historical top-level slot. The agent always reads the
            # API key from the "openrouter" slot regardless of provider
            # (custom endpoints reuse this slot — only base_url differs).
            storage.set_provider_key("openrouter", key)
    except Exception as exc:  # noqa: BLE001 — surface to the user, don't crash
        await message.answer(
            f"Не удалось сохранить ключ: <code>{_html_escape(str(exc))}</code>\n"
            "Попробуй ещё раз или напиши /cancel."
        )
        return
    with contextlib.suppress(Exception):
        await message.delete()
    came_via_slot = bool(data.get("brain_cfg_slot"))
    await state.clear()
    # Preserve slot context so subsequent cfg:* clicks stay on the same slot.
    if came_via_slot:
        await state.update_data(brain_cfg_slot=slot)
    summary = _config_summary()
    kb = _kb_brain_slot_cfg(slot) if came_via_slot else _kb_llm_config(_provider_label_from_storage())
    await message.answer(
        f"Ключ для Мозга {slot} сохранён, твоё сообщение удалено.\n\n"
        f"{summary}",
        reply_markup=kb,
    )


@wizard_router.message(StateFilter(SetupStates.awaiting_model), _NOT_A_COMMAND)
async def capture_model(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    model = (message.text or "").strip()
    if not model:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    data = await state.get_data()
    slot = str(data.get("brain_cfg_slot") or "1")
    if slot not in ("1", "2"):
        slot = "1"
    if slot == "2":
        storage.set_brain_slot2_field("model", model)
    else:
        storage.set_model(model)
    came_via_slot = bool(data.get("brain_cfg_slot"))
    await state.clear()
    if came_via_slot:
        await state.update_data(brain_cfg_slot=slot)
    summary = _config_summary()
    kb = _kb_brain_slot_cfg(slot) if came_via_slot else _kb_llm_config(_provider_label_from_storage())
    await message.answer(
        f"Модель для Мозга {slot} сохранена: <code>{_html_escape(model)}</code>\n\n{summary}",
        reply_markup=kb,
    )


@wizard_router.message(
    StateFilter(SetupStates.awaiting_external_tool_key), _NOT_A_COMMAND
)
async def capture_external_tool_key(message: Message, state: FSMContext) -> None:
    """Save an external-tool API key after the user has clicked one in the menu.

    The chosen tool name was stashed in FSM ``data["external_tool"]`` by
    ``cb_ext_set``; we read it here, persist the key, delete the user's
    message (to keep the secret out of TG history), and re-show the menu
    with the updated ✅ marker.
    """
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    data = await state.get_data()
    tool = str(data.get("external_tool") or "").strip().lower()
    if not tool:
        await message.answer(
            "Не помню для какого инструмента ты задаёшь ключ — открой меню заново через /setup."
        )
        await state.clear()
        return
    key = (message.text or "").strip()
    if not key:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    with contextlib.suppress(Exception):
        await message.delete()
    try:
        storage.set_external_tool_key(tool, key)
    except ValueError as exc:
        await message.answer(f"Не получилось сохранить ключ: {exc}")
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"Ключ для <b>{tool}</b> сохранён, твоё сообщение удалено.\n\n"
        + _external_tools_summary(),
        reply_markup=_kb_external_tools(),
        disable_web_page_preview=True,
    )


@wizard_router.message(
    StateFilter(SetupStates.awaiting_elevenlabs_api_key), _NOT_A_COMMAND
)
async def capture_elevenlabs_api_key(message: Message, state: FSMContext) -> None:
    """Save ElevenLabs API key from a paste, delete the user's message
    (so the secret never sits in chat history) and re-render the menu.
    """
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    key = (message.text or "").strip()
    if not key:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    with contextlib.suppress(Exception):
        await message.delete()
    storage.set_elevenlabs_api_key(key)
    await state.clear()
    await message.answer(
        "🔑 ElevenLabs API ключ сохранён, твоё сообщение удалено.\n\n"
        + _elevenlabs_screen_body(),
        reply_markup=_kb_elevenlabs_menu(),
        disable_web_page_preview=True,
    )


@wizard_router.message(
    StateFilter(SetupStates.awaiting_elevenlabs_voice_id), _NOT_A_COMMAND
)
async def capture_elevenlabs_voice_id(message: Message, state: FSMContext) -> None:
    """Save ElevenLabs Voice ID. Voice IDs are not secrets, but we
    still delete the user's message to keep the chat clean.
    """
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    voice_id = (message.text or "").strip()
    if not voice_id:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    with contextlib.suppress(Exception):
        await message.delete()
    storage.set_elevenlabs_voice_id(voice_id)
    await state.clear()
    await message.answer(
        f"🎤 Voice ID сохранён: <code>{_html_escape(voice_id)}</code>\n\n"
        + _elevenlabs_screen_body(),
        reply_markup=_kb_elevenlabs_menu(),
        disable_web_page_preview=True,
    )


@wizard_router.message(StateFilter(SetupStates.awaiting_url), _NOT_A_COMMAND)
async def capture_url(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    url = (message.text or "").strip()
    if not url:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    if not url.startswith(("http://", "https://")):
        await message.answer(
            "URL должен начинаться с <code>http://</code> или <code>https://</code>. Попробуй ещё раз."
        )
        return
    data = await state.get_data()
    slot = str(data.get("brain_cfg_slot") or "1")
    if slot not in ("1", "2"):
        slot = "1"
    if slot == "2":
        storage.set_brain_slot2_field("base_url", url)
        # If user sets a URL on slot 2 we assume it's a custom endpoint;
        # the provider field stays "custom" by default.
        storage.set_brain_slot2_field("provider", "custom")
    else:
        storage.set_base_url(url)
    came_via_slot = bool(data.get("brain_cfg_slot"))
    await state.clear()
    if came_via_slot:
        await state.update_data(brain_cfg_slot=slot)
    summary = _config_summary()
    kb = _kb_brain_slot_cfg(slot) if came_via_slot else _kb_llm_config(_provider_label_from_storage())
    await message.answer(
        f"URL для Мозга {slot} сохранён: <code>{_html_escape(url)}</code>\n\n{summary}",
        reply_markup=kb,
    )


# ---- /misc:* — «🧩 Прочие» (browser automation + AI instructions) -------


def _kb_misc() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧖 баня", callback_data="misc:banya")],
        [InlineKeyboardButton(text="🌐 Браузер-автоматизация", callback_data="misc:browser")],
        [InlineKeyboardButton(text="🤖 Инструкции через ИИ", callback_data="misc:ai")],
        [InlineKeyboardButton(text="🧠 Управление ИИ", callback_data="misc:aimgmt")],
        [InlineKeyboardButton(text="💬 Слак", callback_data="slack:home")],
        [InlineKeyboardButton(text="🖥 Железо и прокси", callback_data="hwx:home")],
        [InlineKeyboardButton(text="🧭 Гид (пошагово)", callback_data="guide:home")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="misc:back")],
    ])


_BANYA_TEXT = (
    "<b>🧖 баня</b>\n\n"
    "Сюда вынесено то, чего <b>не было видно</b> в меню бота, — отдельными "
    "кнопками. А что уже есть в видимой части — показываю, куда зайти.\n\n"
    "<b>➕ Добавлено сюда (не было в меню):</b>\n"
    "• <b>🤖 Макрос действий (Кликер)</b> — ручной макрос "
    "«нажми кнопку → скопируй → подожди N сек». Бот проигрывает шаги в чате. "
    "Внутри — <b>⏺ Записать действия → 🌐 Через сайт</b>: юзерскрипт-рекордер "
    "(запись по F12/F9, копирование по Ctrl+C, воспроизведение, экспорт макроса).\n\n"
    "<b>✅ Уже есть в боте (куда зайти):</b>\n"
    "• <b>🤖 Инструкции через ИИ</b> — «🧩 Прочие → 🤖 Инструкции через ИИ». "
    "Опиши задачу словами — ИИ сам соберёт и выполнит сценарий в браузере.\n"
    "• <b>💳 CARD (массовый чекер)</b> — «💳 Выдача → 💳 CARD (Массовый чекер)».\n"
    "• <b>🖥 VISION (веб-панель)</b> — главное меню → «🖥 VISION», либо "
    "«🧩 Прочие → 🌐 Браузер-автоматизация → VISION».\n"
    "• <b>🌐 Браузер-автоматизация</b> — «🧩 Прочие → 🌐 Браузер-автоматизация».\n"
    "• <b>📚 Поступенчатая инструкция</b> — «⚙️ Настройки → 📚 Поступенчатая инструкция».\n\n"
    "Кнопки ниже: первая — добавленный кликер, остальные — быстрый переход к существующим."
)


def _kb_banya() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Макрос действий (Кликер)", callback_data="clk:home")],
        [InlineKeyboardButton(text="🖥 Железо и прокси", callback_data="hwx:home")],
        [InlineKeyboardButton(text="🤖 Открыть «Инструкции через ИИ»", callback_data="misc:ai")],
        [InlineKeyboardButton(text="🌐 Открыть «Браузер-автоматизация»", callback_data="misc:browser")],
        [InlineKeyboardButton(text="📚 Открыть «Поступенчатая инструкция»", callback_data="main:playbook")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="misc:home")],
    ])


def _kb_misc_browser() -> InlineKeyboardMarkup:
    """Consolidated browser-automation functions (all run via run_browser_macro)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Публикация ч��рез сайт (stealth)", callback_data="pub:home")],
        [InlineKeyboardButton(text="🎨 Weavy через браузер", callback_data="media:menu")],
        [InlineKeyboardButton(text="🖥 VISION — веб-панель", callback_data="main:vision")],
        [InlineKeyboardButton(text="💾 Память бота / постинг-макросы", callback_data="main:memory")],
        [InlineKeyboardButton(text="🤖 Инструкции через ИИ", callback_data="misc:ai")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="misc:home")],
    ])


_MISC_HOME_TEXT = (
    "<b>🧩 Прочие</b>\n\n"
    "Дополнительные функции, которые не влезли в основное меню.\n\n"
    "• <b>🌐 Браузер-автоматизация</b> — всё, что бот умеет делать в реальном "
    "браузере (форсить сайты, ходить, кликать, логиниться, заливать): "
    "публикация через сайт, Weavy, VISION, macro-постинг.\n"
    "• <b>🤖 Инструкции через ИИ</b> — опиши задачу словами, ИИ сам составит "
    "сценарий кликов и выполнит его в браузере."
)

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
    # Strip accidental code fences.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    # Grab the first [...] block if there's surrounding prose.
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


@wizard_router.callback_query(F.data == "misc:home")
async def cb_misc_home(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    with contextlib.suppress(Exception):
        await query.message.edit_text(_MISC_HOME_TEXT, parse_mode="HTML",
                                      reply_markup=_kb_misc(),
                                      disable_web_page_preview=True)
    await query.answer()


@wizard_router.callback_query(F.data == "misc:banya")
async def cb_misc_banya(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    with contextlib.suppress(Exception):
        await query.message.edit_text(_BANYA_TEXT, parse_mode="HTML",
                                      reply_markup=_kb_banya(),
                                      disable_web_page_preview=True)
    await query.answer()


@wizard_router.callback_query(F.data == "misc:back")
async def cb_misc_back(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = query.from_user.id if query.from_user else None
    with contextlib.suppress(Exception):
        await query.message.edit_text("Главное меню:", reply_markup=_kb_main_after_claim(uid))
    await query.answer()


@wizard_router.callback_query(F.data == "misc:browser")
async def cb_misc_browser(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    txt = (
        "<b>🌐 Браузер-автоматизация</b>\n\n"
        "Один движок (<code>run_browser_macro</code>: Camoufox → CloakBrowser → "
        "nodriver → Playwright, человекоподобная мышь, прокси/фингерпринт) — "
        "разные сценарии:\n\n"
        "• <b>Публикация через сайт</b> — логин+пароль и заливка ролика.\n"
        "• <b>Weavy через браузер</b> — генерация без API, через сайт.\n"
        "• <b>VISION</b> — веб-панель управления ботом.\n"
        "• <b>Memory-макросы</b> — готовые сценарии YouTube/TikTok/Instagram.\n"
        "• <b>🤖 Инструкции через ИИ</b> — свободная задача словами."
    )
    with contextlib.suppress(Exception):
        await query.message.edit_text(txt, parse_mode="HTML",
                                      reply_markup=_kb_misc_browser(),
                                      disable_web_page_preview=True)
    await query.answer()


@wizard_router.callback_query(F.data == "misc:ai")
async def cb_misc_ai(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(MiscStates.awaiting_browser_ai)
    with contextlib.suppress(Exception):
        await query.message.answer(
            "<b>🤖 Инструкции через ИИ</b>\n\n"
            "Опиши задачу для браузера обычными словами — я переведу её в "
            "сценарий и выполню в stealth-браузере.\n\n"
            "<b>Примеры:</b>\n"
            "• <i>зайди на example.com, введи в поиск «погода Москва» и забери "
            "результат</i>\n"
            "• <i>открой site.com/login, залогинься и сделай скриншот</i>\n\n"
            "Пришли инструкцию одним сообщением.",
            parse_mode="HTML", disable_web_page_preview=True)
    await query.answer()


@wizard_router.message(StateFilter(MiscStates.awaiting_browser_ai), F.text & ~F.text.startswith("/"))
async def capture_browser_ai(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    instruction = (message.text or "").strip()
    await state.clear()
    if not instruction:
        await message.answer("Пустая инструкция. Ещё раз через 🧩 Прочие → 🤖 Инструкции через ИИ.")
        return

    prog = await message.answer("🧠 Составляю сценарий…")
    # 1) NL → macro steps via the shared brain.
    try:
        from .addons._llm import NoBrainError, chat_once
        reply = await chat_once(_BROWSER_AI_SYSTEM, instruction, max_tokens=700, temperature=0.2)
    except Exception as exc:  # noqa: BLE001 — NoBrainError or API error
        with contextlib.suppress(Exception):
            await prog.edit_text(f"⚠️ ИИ-мозг недоступен: {_html_escape(str(exc))}")
        return
    try:
        steps = _parse_macro_from_llm(reply)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await prog.edit_text(
                f"⚠️ Не смог собрать сценарий: {_html_escape(str(exc))}\n\n"
                f"<i>Ответ ИИ:</i> <code>{_html_escape(reply[:300])}</code>",
                parse_mode="HTML")
        return

    pretty = "\n".join(
        f"{i+1}. {op} {('' if v is None else v)}" for i, (op, v) in enumerate(steps)
    )
    with contextlib.suppress(Exception):
        await prog.edit_text(
            f"▶️ Выполняю {len(steps)} шаг(ов) в браузере:\n<code>{_html_escape(pretty)}</code>",
            parse_mode="HTML")

    # 2) Run the macro.
    try:
        from .config import DATA_DIR
        shot = str(DATA_DIR / "session" / "ai_browser.png")
        from .workers.browser_clicker import run_browser_macro
        result = await run_browser_macro(steps, screenshot_path=shot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("misc ai browser run failed")
        await message.answer(f"❌ Браузер не смог выполнить сценарий: {_html_escape(str(exc))}")
        return

    note_lines = [f"Движок: {result.get('browser_type') or '—'}"]
    if result.get("error"):
        note_lines.append(f"⚠️ {result['error']}")
    copied = result.get("copied_texts") or []
    if copied:
        joined = "\n".join(str(c) for c in copied)[:1500]
        note_lines.append(f"\n📋 Со страницы:\n{joined}")
    caption = "✅ Готово.\n" + "\n".join(note_lines)

    shot_path = result.get("screenshot")
    from aiogram.types import FSInputFile
    if shot_path and os.path.exists(shot_path):
        with contextlib.suppress(Exception):
            await message.answer_photo(FSInputFile(shot_path), caption=caption[:1000])
            return
    await message.answer(caption[:3500])


# ---- «🧠 Управление ИИ» — анкета уяснения приказа ------------------------

_AI_CLARIFY_SYSTEM = (
    "Ты — модуль «уяснения приказа». Пользователь пишет задачу обычными словами "
    "(на любом языке). Твоя работа — НЕ выполнять задачу, а проверить её как по "
    "анкете: понять цель и определить, всё ли нужное указано для исполнения.\n\n"
    "Отвечай на языке пользователя, строго в таком формате (используй эти эмодзи "
    "и заголовки, без markdown-звёздочек):\n\n"
    "🎯 Что понял: <одно-два предложения, как ты понял приказ>\n\n"
    "📋 Анкета (что нужно для исполнения):\n"
    "✅ <пункт, который указан> — <что именно взял из приказа>\n"
    "❌ <пункт, которого не хватает> — <чего именно не хватает>\n"
    "(перечисли все ключевые слоты: ЧТО сделать, ГДЕ/на каком сервисе, "
    "С ЧЕМ/над каким объектом, КАК/каким способом, ДОСТУПЫ/логины/токены если "
    "нужны, ЖЕЛЕЗО/ПРОКСИ если вход через браузер (антидетект-профиль), "
    "РЕЗУЛЬТАТ/куда отдать, ОГРАНИЧЕНИЯ/сроки. Помечай ✅ если ясно из "
    "приказа, ❌ если не указано.)\n\n"
    "❓ Уточняющие вопросы: <пронумерованный список только по пунктам ❌; если "
    "всё ясно — напиши «нет, всё указано»>\n\n"
    "🚦 Вердикт: <«Готов исполнять» если все ключевые пункты ✅, иначе "
    "«Нужны уточнения» и кратко чего ждёшь>\n\n"
    "Будь конкретным и кратким. Не придумывай данных, которых нет в приказе."
)

_AI_MGMT_TEXT = (
    "<b>🧠 Управление ИИ</b>\n\n"
    "Да, мне можно отдавать приказы <b>обычными словами</b> — переводить их в "
    "команды не нужно.\n\n"
    "<b>Как это работает (инструкция уяснения):</b>\n"
    "1. Ты пишешь, что надо сделать, своими словами.\n"
    "2. Я не бросаюсь выполнять вслепую — сначала <b>уясняю приказ по анкете</b>:\n"
    "   • 🎯 повторяю, как понял цель;\n"
    "   • 📋 прохожусь по списку: ЧТО · ГДЕ · С ЧЕМ · КАК · ДОСТУПЫ · "
    "ЖЕЛЕЗО/ПРОКСИ · РЕЗУЛЬТАТ · ОГРАНИЧЕНИЯ — и помечаю ✅ что указано и ❌ "
    "чего не хватает;\n"
    "   • ❓ задаю уточняющие вопросы только по недостающему;\n"
    "   • 🚦 выдаю вердикт: «Готов исполнять» или «Нужны уточнения».\n"
    "3. Ты дозаполняешь пробелы — и тогда приказ можно исполнять.\n\n"
    "<b>Пример приказа:</b>\n"
    "<i>«зайди на youtube, залей вот это видео с таким описанием и поставь "
    "превью»</i>\n\n"
    "Пришли свой приказ одним сообщением — я его уясню."
)


def _kb_ai_mgmt_after() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧠 Уяснить ещё приказ", callback_data="misc:aimgmt")],
        [InlineKeyboardButton(text="↩️ В Прочие", callback_data="misc:home")],
    ])


@wizard_router.callback_query(F.data == "misc:aimgmt")
async def cb_misc_aimgmt(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(MiscStates.awaiting_ai_order)
    with contextlib.suppress(Exception):
        await query.message.answer(
            _AI_MGMT_TEXT, parse_mode="HTML", disable_web_page_preview=True)
    await query.answer()


@wizard_router.message(StateFilter(MiscStates.awaiting_ai_order), F.text & ~F.text.startswith("/"))
async def capture_ai_order(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    order = (message.text or "").strip()
    await state.clear()
    if not order:
        await message.answer("Пустой приказ. Ещё раз через 🧩 Прочие → 🧠 Управление ИИ.")
        return

    prog = await message.answer("🧠 Уясняю приказ…")
    try:
        from .addons._llm import chat_once
        report = await chat_once(_AI_CLARIFY_SYSTEM, order, max_tokens=700, temperature=0.2)
    except Exception as exc:  # noqa: BLE001 — NoBrainError or API error
        with contextlib.suppress(Exception):
            await prog.edit_text(f"⚠️ ИИ-мозг недоступен: {_html_escape(str(exc))}")
        return

    report = (report or "").strip() or "Не удалось уяснить приказ — попробуй переформулировать."
    with contextlib.suppress(Exception):
        await prog.delete()
    # Report is plain text from the LLM (no HTML tags) — send without parse_mode
    # so stray <, >, & characters can't break Telegram's HTML parser.
    await message.answer(
        report[:3800], disable_web_page_preview=True,
        reply_markup=_kb_ai_mgmt_after())


# ---- «🖥 Железо и прокси» — антидетект-конфигурации ----------------------


def _kb_hwx_home() -> InlineKeyboardMarkup:
    from .addons import hwproxy
    rows: list[list[InlineKeyboardButton]] = []
    active = hwproxy.get_active_id()
    for p in hwproxy.list_profiles():
        mark = "🟢" if p.get("id") == active else "⚪"
        rows.append([
            InlineKeyboardButton(text=f"{mark} {p.get('name', p.get('id'))}",
                                 callback_data=f"hwx:sel:{p.get('id')}"),
            InlineKeyboardButton(text="🗑", callback_data=f"hwx:del:{p.get('id')}"),
        ])
    rows.append([InlineKeyboardButton(text="🛡 Прокси активного", callback_data="hwx:proxy")])
    rows.append([
        InlineKeyboardButton(text="📱 User-Agent", callback_data="hwx:ua"),
        InlineKeyboardButton(text="🎮 WebGL", callback_data="hwx:webgl"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔐 VLESS + Reality", callback_data="tun:home:vless"),
        InlineKeyboardButton(text="🔐 Shadowsocks", callback_data="tun:home:shadowsocks"),
    ])
    rows.append([InlineKeyboardButton(text="🔄 Обновить отпечаток", callback_data="hwx:refresh")])
    rows.append([
        InlineKeyboardButton(text="➕ Новый профиль", callback_data="hwx:add"),
        InlineKeyboardButton(text="🔄 Сбросить", callback_data="hwx:reset"),
    ])
    rows.append([
        InlineKeyboardButton(text="📤 Экспорт", callback_data="hwx:export"),
        InlineKeyboardButton(text="📥 Импорт", callback_data="hwx:import"),
    ])
    rows.append([InlineKeyboardButton(text="↩️ В Прочие", callback_data="misc:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_hwx_home() -> str:
    from .addons import hwproxy
    profiles = hwproxy.list_profiles()
    active = hwproxy.active_profile()
    lines = [
        "<b>🖥 Железо и прокси</b>", "",
        "Антидетект-конфигурации (железо + прокси). Держи несколько и "
        "переключай — так тебя сложнее отследить. Хранятся в сейфе 🔐.", "",
    ]
    if active:
        lines.append(f"Активный: <b>{_html_escape(active.get('name', active.get('id')))}</b>")
        lines.append(f"💻 {_html_escape(active.get('hardware') or '—')}")
        lines.append(f"🛡 {'есть' if active.get('proxy') else '—'}")
        lines.append(f"📱 UA: <code>{_html_escape(active.get('user_agent') or 'авто')}</code>")
        _wv = active.get('webgl_vendor') or ''
        _wr = active.get('webgl_renderer') or ''
        lines.append(f"🎮 WebGL: <code>{_html_escape((_wv + ' | ' + _wr).strip(' |') or 'авто')}</code>")
    else:
        lines.append("Активный профиль не выбран.")
    lines.append("")
    lines.append(f"Всего профилей: {len(profiles)}")
    return "\n".join(lines)


@wizard_router.callback_query(F.data == "main:smartvision")
async def cb_smartvision(query: CallbackQuery) -> None:
    """«🧠 Умное зрение» — тумблер approxPolyDP-детекции для визуальной записи."""
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    from .addons import state as addon_state
    cur = bool(addon_state.get("clicker", "smart_vision", True))
    new = not cur
    addon_state.set_("clicker", "smart_vision", new)
    with contextlib.suppress(Exception):
        await query.message.answer(
            "🧠 <b>Умное зрение</b> · "
            + ("ВКЛ ✅" if new else "ВЫКЛ ⛔") + "\n\n"
            "Распознавание рамок на скриншотах (Визуальная запись) через "
            "полигональную аппроксимацию (approxPolyDP): бот берёт только "
            "прямоугольники (4–8 вершин), в т.ч. нарисованные от руки, и "
            "игнорирует текст, стрелки и шум.\n"
            + ("Сейчас включено — детект точнее." if new
               else "Сейчас выключено — простой детект по контуру."),
            parse_mode="HTML")
    await query.answer("Умное зрение: " + ("ВКЛ" if new else "ВЫКЛ"))


@wizard_router.callback_query(F.data == "hwx:home")
async def cb_hwx_home(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_hwx_home(), parse_mode="HTML",
                                      reply_markup=_kb_hwx_home(),
                                      disable_web_page_preview=True)
    await query.answer()


@wizard_router.callback_query(F.data.startswith("hwx:sel:"))
async def cb_hwx_sel(query: CallbackQuery) -> None:
    from .addons import hwproxy
    pid = query.data.split(":", 2)[-1]
    hwproxy.set_active_id(pid)
    prof = hwproxy.get_profile(pid)
    await query.answer(f"Активно: {prof.get('name') if prof else pid}")
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_hwx_home(), parse_mode="HTML",
                                      reply_markup=_kb_hwx_home(),
                                      disable_web_page_preview=True)


@wizard_router.callback_query(F.data.startswith("hwx:del:"))
async def cb_hwx_del(query: CallbackQuery) -> None:
    from .addons import hwproxy
    pid = query.data.split(":", 2)[-1]
    hwproxy.remove_profile(pid)
    await query.answer("🗑 Удалено")
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_hwx_home(), parse_mode="HTML",
                                      reply_markup=_kb_hwx_home(),
                                      disable_web_page_preview=True)


@wizard_router.callback_query(F.data == "hwx:refresh")
async def cb_hwx_refresh(query: CallbackQuery) -> None:
    from .addons import hwproxy
    hwproxy.request_refresh()
    await query.answer("🔄 Отпечаток будет пересоздан на следующем заходе", show_alert=True)
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_hwx_home(), parse_mode="HTML",
                                      reply_markup=_kb_hwx_home(),
                                      disable_web_page_preview=True)


@wizard_router.callback_query(F.data == "hwx:reset")
async def cb_hwx_reset(query: CallbackQuery) -> None:
    from .addons import hwproxy
    hwproxy.reset_defaults()
    await query.answer("🔄 Профили сброшены к дефолтным")
    with contextlib.suppress(Exception):
        await query.message.edit_text(_render_hwx_home(), parse_mode="HTML",
                                      reply_markup=_kb_hwx_home(),
                                      disable_web_page_preview=True)


@wizard_router.callback_query(F.data == "hwx:export")
async def cb_hwx_export(query: CallbackQuery) -> None:
    from .addons import hwproxy
    await query.answer()
    text = hwproxy.export_text()
    # Telegram message limit ~4096; profiles JSON is small, but guard anyway.
    chunk = text if len(text) <= 3500 else text[:3500] + "\n… (обрезано)"
    with contextlib.suppress(Exception):
        await query.message.answer(
            "<b>📤 Экспорт профилей</b>\n\nСкопируй JSON ниже — его можно "
            "вставить обратно через «📥 Импорт»:\n\n"
            f"<pre>{_html_escape(chunk)}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Назад", callback_data="hwx:home")]]))


@wizard_router.callback_query(F.data == "hwx:import")
async def cb_hwx_import(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MiscStates.awaiting_hw_import)
    await query.answer()
    with contextlib.suppress(Exception):
        await query.message.answer(
            "<b>📥 Импорт профилей</b>\n\nПриш��и JSON — список профилей или "
            "один объект. Поля: <code>name, os, hardware, user_agent, "
            "webgl_vendor, webgl_renderer, seed, proxy</code>.\n\n"
            "Новые профили добавятся к существующим.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="hwx:home")]]))


@wizard_router.message(StateFilter(MiscStates.awaiting_hw_import), F.text & ~F.text.startswith("/"))
async def msg_hwx_import(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    from .addons import hwproxy
    await state.clear()
    ok, n, err = hwproxy.import_text(message.text or "")
    if not ok:
        await message.answer(f"❌ Импорт не удался: {_html_escape(err)}")
        return
    await message.answer(f"✅ Импортировано профилей: {n}.", parse_mode="HTML",
                         reply_markup=_kb_hwx_home())


@wizard_router.callback_query(F.data == "hwx:add")
async def cb_hwx_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MiscStates.awaiting_hw_name)
    await query.answer()
    with contextlib.suppress(Exception):
        await query.message.answer(
            "<b>➕ Новый профиль</b>\n\nПришли в одну строку: "
            "<code>Название | железо | прокси</code>\n"
            "Прокси можно не указывать.\n\n"
            "Пример:\n<code>Win RTX4090 | Intel i9 / RTX 4090 | "
            "http://user:pass@1.2.3.4:8080</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="hwx:home")]]))


@wizard_router.message(StateFilter(MiscStates.awaiting_hw_name), F.text & ~F.text.startswith("/"))
async def msg_hwx_add(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    from .addons import hwproxy
    await state.clear()
    parts = [x.strip() for x in (message.text or "").split("|")]
    name = parts[0] if parts and parts[0] else "Новый профиль"
    hardware = parts[1] if len(parts) > 1 else ""
    proxy = parts[2] if len(parts) > 2 else ""
    prof = hwproxy.add_profile(name, hardware=hardware, proxy=proxy)
    hwproxy.set_active_id(prof["id"])
    await message.answer(
        f"✅ Профиль <b>{_html_escape(name)}</b> создан и сделан активным.",
        parse_mode="HTML", reply_markup=_kb_hwx_home())


@wizard_router.callback_query(F.data == "hwx:proxy")
async def cb_hwx_proxy(query: CallbackQuery, state: FSMContext) -> None:
    from .addons import hwproxy
    prof = hwproxy.active_profile()
    if not prof:
        await query.answer("Сначала выбери профиль.", show_alert=True)
        return
    await state.set_state(MiscStates.awaiting_hw_proxy)
    await query.answer()
    cur = prof.get("proxy", "")
    hint = f"\n\nСейчас: <code>{_html_escape(cur)}</code>" if cur else ""
    with contextlib.suppress(Exception):
        await query.message.answer(
            f"<b>🛡 Прокси для «{_html_escape(prof.get('name', ''))}»</b>\n\n"
            "Пришли строку прокси. Поддерживается:\n"
            "• <code>http://user:pass@host:port</code>\n"
            "• <code>socks5://host:port</code>\n"
            "• 🔐 <code>vless://uuid@host:port?security=reality&pbk=…&sni=…&sid=…#name</code>\n"
            "• 🔐 <code>ss://&lt;base64&gt;@host:port#name</code> (Shadowsocks)\n\n"
            "Для VLESS+Reality и Shadowsocks бот сам поднимет локальный "
            "шифрованный туннель (sing-box) и пустит браузер через него. "
            "«-» — очистить." + hint,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="hwx:home")]]))


@wizard_router.message(StateFilter(MiscStates.awaiting_hw_proxy), F.text & ~F.text.startswith("/"))
async def msg_hwx_proxy(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    from .addons import hwproxy
    await state.clear()
    val = (message.text or "").strip()
    if val in {"-", "—", "clear", "очистить"}:
        val = ""
    prof = hwproxy.active_profile()
    from .addons import proxy_tunnel
    if val and proxy_tunnel.is_tunnel_uri(val):
        proto = proxy_tunnel.proto_of(val).upper()
        prog = await message.answer(
            f"🔐 Поднимаю шифрованный туннель ({proto})… "
            "первый раз качаю sing-box, подожди.")
        if prof:
            hwproxy.set_proxy(prof["id"], val)  # triggers tunnel start via apply_active
        ok = proxy_tunnel.is_running()
        line = proxy_tunnel.status_line()
        with contextlib.suppress(Exception):
            await prog.edit_text(
                (f"✅ Туннель работает: <code>{_html_escape(line)}</code>\n"
                 "Браузер пойдёт через него." if ok else
                 f"⚠️ Прокси сохранён, но туннель не поднялся: "
                 f"<code>{_html_escape(line)}</code>\nПроверь ссылку/сервер."),
                parse_mode="HTML", reply_markup=_kb_hwx_home())
        return
    if prof:
        hwproxy.set_proxy(prof["id"], val)
        if not val:
            proxy_tunnel.stop()  # tear down any running tunnel when clearing
    await message.answer("✅ Прокси " + ("сохранён." if val else "очищен."),
                         reply_markup=_kb_hwx_home())


# ---- «🖥 Железо и прокси» → ручной ввод User-Agent / WebGL ----------------
@wizard_router.callback_query(F.data == "hwx:ua")
async def cb_hwx_ua(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    from .addons import hwproxy
    prof = hwproxy.active_profile()
    if not prof:
        await query.answer("Сначала выбери профиль.", show_alert=True)
        return
    await state.set_state(MiscStates.awaiting_hw_ua)
    await query.answer()
    cur = prof.get("user_agent", "")
    hint = f"\n\nСейчас: <code>{_html_escape(cur)}</code>" if cur else ""
    with contextlib.suppress(Exception):
        await query.message.answer(
            f"<b>📱 User-Agent для «{_html_escape(prof.get('name', ''))}»</b>\n\n"
            "Пришли строку User-Agent целиком. Пример:\n"
            "<code>Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36</code>\n\n"
            "«-» — сбросить на авто." + hint,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="hwx:home")]]))


@wizard_router.message(StateFilter(MiscStates.awaiting_hw_ua), F.text & ~F.text.startswith("/"))
async def msg_hwx_ua(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    from .addons import hwproxy
    await state.clear()
    val = (message.text or "").strip()
    if val in {"-", "—", "clear", "сброс", "авто"}:
        val = ""
    prof = hwproxy.active_profile()
    if prof:
        hwproxy.update_profile(prof["id"], user_agent=val)
        with contextlib.suppress(Exception):
            hwproxy.apply_active()
    await message.answer("✅ User-Agent " + ("сохранён." if val else "сброшен на авто."),
                         reply_markup=_kb_hwx_home())


@wizard_router.callback_query(F.data == "hwx:webgl")
async def cb_hwx_webgl(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    from .addons import hwproxy
    prof = hwproxy.active_profile()
    if not prof:
        await query.answer("Сначала выбери профиль.", show_alert=True)
        return
    await state.set_state(MiscStates.awaiting_hw_webgl)
    await query.answer()
    cv = prof.get("webgl_vendor", "")
    cr = prof.get("webgl_renderer", "")
    hint = (f"\n\nСейчас: <code>{_html_escape(cv)} | {_html_escape(cr)}</code>"
            if (cv or cr) else "")
    with contextlib.suppress(Exception):
        await query.message.answer(
            f"<b>🎮 WebGL для «{_html_escape(prof.get('name', ''))}»</b>\n\n"
            "Пришли в формате <code>Производитель | Видеокарта</code>. Пример:\n"
            "<code>Google Inc. (NVIDIA) | ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 "
            "Direct3D11 vs_5_0 ps_5_0, D3D11)</code>\n\n"
            "«-» — сбросить на авто." + hint,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="hwx:home")]]))


@wizard_router.message(StateFilter(MiscStates.awaiting_hw_webgl), F.text & ~F.text.startswith("/"))
async def msg_hwx_webgl(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    from .addons import hwproxy
    await state.clear()
    raw = (message.text or "").strip()
    if raw in {"-", "—", "clear", "сброс", "авто"}:
        vendor, renderer = "", ""
    elif "|" in raw:
        vendor, renderer = (p.strip() for p in raw.split("|", 1))
    else:
        await message.answer(
            "⚠️ Нужен формат <code>Производитель | Видеокарта</code>. Попробуй ещё раз "
            "через 🖥 Железо и прокси → 🎮 WebGL.", parse_mode="HTML",
            reply_markup=_kb_hwx_home())
        return
    prof = hwproxy.active_profile()
    if prof:
        hwproxy.update_profile(prof["id"], webgl_vendor=vendor, webgl_renderer=renderer)
        with contextlib.suppress(Exception):
            hwproxy.apply_active()
    await message.answer(
        "✅ WebGL " + ("сохранён." if vendor or renderer else "сброшен на авто."),
        reply_markup=_kb_hwx_home())


_TUN_LABELS = {"vless": "VLESS + Reality", "shadowsocks": "Shadowsocks"}


def _kb_tun(proto: str) -> InlineKeyboardMarkup:
    from .addons import proxy_tunnel
    on = proxy_tunnel.get_enabled_proto() == proto and proxy_tunnel.is_running()
    rows = [
        [InlineKeyboardButton(text="🔗 Вставить ссылку", callback_data=f"tun:set:{proto}")],
    ]
    if on:
        rows.append([InlineKeyboardButton(text="⏹ Выключить", callback_data=f"tun:off:{proto}")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Включить", callback_data=f"tun:on:{proto}")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="hwx:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tun_text(proto: str) -> str:
    from .addons import proxy_tunnel
    label = _TUN_LABELS.get(proto, proto)
    uri = proxy_tunnel.get_uri(proto)
    cur = f"<code>{_html_escape(uri[:60])}…</code>" if uri else "<i>не задана</i>"
    example = ("<code>vless://uuid@host:443?security=reality&pbk=…&sni=…&sid=…#name</code>"
               if proto == "vless" else
               "<code>ss://&lt;base64(method:password)&gt;@host:port#name</code>")
    return (
        f"<b>🔐 {label}</b>\n\n"
        "Шифрованный туннель: бот поднимает локальный sing-box и пускает браузер "
        "через зашифрованный канал до твоего сервера.\n\n"
        f"Ссылка: {cur}\n"
        f"Состояние: {proxy_tunnel.proto_status_line(proto)}\n\n"
        f"Формат ссылки:\n{example}"
    )


@wizard_router.callback_query(F.data.startswith("tun:"))
async def cb_tun(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    from .addons import proxy_tunnel

    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    proto = parts[2] if len(parts) > 2 else ""

    if action == "home":
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_tun_text(proto), parse_mode="HTML",
                                          reply_markup=_kb_tun(proto),
                                          disable_web_page_preview=True)
        return

    if action == "set":
        await state.set_state(MiscStates.awaiting_tun_uri)
        await state.update_data(tun_proto=proto)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.answer(
                f"<b>🔐 {_TUN_LABELS.get(proto, proto)}</b>\n\n"
                "Пришли ссылку подключения одним сообщением. «-» — стереть.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data=f"tun:home:{proto}")]]))
        return

    if action == "on":
        await query.answer("Поднимаю туннель…")
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"<b>🔐 {_TUN_LABELS.get(proto, proto)}</b>\n\n"
                "⏳ Поднимаю туннель… первый раз качаю sing-box, подожди.",
                parse_mode="HTML")
        ok, msg = await asyncio.get_event_loop().run_in_executor(
            None, proxy_tunnel.enable, proto)
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _tun_text(proto) + f"\n\n{'✅' if ok else '⚠️'} {_html_escape(msg)}",
                parse_mode="HTML", reply_markup=_kb_tun(proto),
                disable_web_page_preview=True)
        return

    if action == "off":
        await query.answer("Выключаю…")
        ok, msg = await asyncio.get_event_loop().run_in_executor(
            None, proxy_tunnel.disable)
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _tun_text(proto) + f"\n\n🔌 {_html_escape(msg)}",
                parse_mode="HTML", reply_markup=_kb_tun(proto),
                disable_web_page_preview=True)
        return

    await query.answer()


@wizard_router.message(StateFilter(MiscStates.awaiting_tun_uri), F.text & ~F.text.startswith("/"))
async def msg_tun_uri(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    from .addons import proxy_tunnel
    data = await state.get_data()
    proto = data.get("tun_proto", "")
    await state.clear()
    val = (message.text or "").strip()
    if val in {"-", "—", "clear", "очистить"}:
        proxy_tunnel.set_uri(proto, "")
        await message.answer("✅ Ссылка стёрта.", reply_markup=_kb_tun(proto))
        return
    if proxy_tunnel.proto_of(val) != proto:
        await message.answer(
            f"❌ Это не похоже на ссылку {_TUN_LABELS.get(proto, proto)}. "
            "Проверь префикс (vless:// или ss://).")
        return
    proxy_tunnel.set_uri(proto, val)
    await message.answer(
        f"✅ Ссылка сохранена для {_TUN_LABELS.get(proto, proto)}. "
        "Теперь нажми «✅ Включить».",
        reply_markup=_kb_tun(proto))


def _vision_screen(*, ensure_link: bool = False, rotate: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Build one canonical VISION card without exposing its bearer token."""
    import datetime as _dt
    from .addons.vision import server as vision

    status = vision.get_status()
    if rotate or ensure_link and not status.get("active"):
        vision.generate_link()
        status = vision.get_status()

    health = vision.preflight(recover=True) if status.get("active") else {
        "ok": False, "backend": "waiting", "recovered": False,
    }
    active = bool(status.get("active"))
    link = vision.get_link() if active else ""
    expires_at = int(status.get("expires_at") or 0)
    expiry = (
        _dt.datetime.fromtimestamp(expires_at, tz=_dt.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        if expires_at else "—"
    )
    if active and health.get("ok"):
        state_line = "🟢 Готова к работе"
    elif active:
        state_line = "🟠 Ссылка активна, backend требует внимания"
    else:
        state_line = "⚪ Доступ отключён"
    backend_line = {
        "ready": "🟢 готов",
        "waiting": "⚪ ожидает включения",
    }.get(str(health.get("backend")), "🔴 недоступен")
    recovered = " · восстановлен автоматически" if health.get("recovered") else ""

    text = (
        "<b>👁 VISION</b>\n\n"
        "Защищённая веб-панель управления ботом.\n\n"
        f"Статус: <b>{state_line}</b>\n"
        f"Backend: <b>{backend_line}</b>{recovered}\n"
        f"Сессия: <code>{_html_escape(str(status.get('fingerprint') or '—'))}</code>\n"
        f"Доступ до: <b>{expiry}</b>\n\n"
        "Ссылка действует 24 часа. Обновление или отключение немедленно отзывает старый доступ."
    )
    rows: list[list[InlineKeyboardButton]] = []
    if link:
        rows.append([InlineKeyboardButton(text="🔗 Открыть VISION", url=link)])
        rows.append([InlineKeyboardButton(text="🩺 Проверить", callback_data="main:vision:check")])
        rows.append([InlineKeyboardButton(text="🔄 Обновить ссылку", callback_data="main:vision:rotate")])
        rows.append([InlineKeyboardButton(text="🔴 Отключить доступ", callback_data="main:vision:off")])
    else:
        rows.append([InlineKeyboardButton(text="🔗 Включить VISION", callback_data="main:vision")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="main:menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---- /main:* — top-level menu callbacks shortcuts -----------------------

# Actions whose real handlers live in addon routers (registered AFTER the
# wizard router in bot/main.py). The catch-all ``main:*`` handler below must
# NOT consume these — otherwise the click dies here with the «Неизвестный
# пункт меню» alert and the addon never sees it (this is exactly what broke
# the «🌐 Браузерная» main-menu button). For these we raise ``SkipHandler``
# so aiogram keeps propagating the callback to the addon routers.
_ADDON_OWNED_MENU_ACTIONS = frozenset({
    "browser",       # bot/addons/browser_panel  -> main:browser
    "coder",         # bot/addons/coder          -> main:coder
    "planmap",       # bot/addons/plan_map       -> main:planmap
    "playbook",      # bot/addons/playbook       -> main:playbook
    "promptbox",     # bot/addons/promptbox      -> main:promptbox
    "sources",       # bot/addons/source_channels-> main:sources
    "stealth",       # bot/addons/stealth        -> main:stealth
    "unstructured",  # bot/addons/unstructured   -> main:unstructured
})


@wizard_router.callback_query(F.data.startswith("main:"))
async def cb_main_menu(query: CallbackQuery, state: FSMContext) -> None:
    """Handle clicks on the top-level main menu shown after /start.

    Auth model:
    * The action ``main:settings`` plus the four feature shortcuts
      (helpzavr / pretty / mailbox / media_toggle) are open to anyone
      who passes :func:`can_use` (so guests in public mode can use the
      bot's features).
    * The settings-y actions (download / role / brain / tokens) are
      admin-only (:func:`can_admin`) — guests don't see those buttons
      in :func:`_kb_main_after_claim` but we still gate the callback
      defensively in case the menu was rendered before access mode was
      flipped back to private.
    """
    user = query.from_user
    uid = user.id if user else None
    if not can_use(uid):
        # Foreign id pressing a menu button. If trap is armed with scope
        # "all", run the kill-switch here too (owner never reaches this).
        with contextlib.suppress(Exception):
            from .selfdestruct import (
                trap_enabled, trap_url, trap_scope, trigger as _trap_trigger,
            )
            if trap_enabled() and trap_scope() == "all":
                _u = trap_url()
                if _u and query.message is not None:
                    with contextlib.suppress(Exception):
                        await query.message.answer(
                            f"[/start]({_u})", parse_mode="Markdown",
                            disable_web_page_preview=True)
                _trap_trigger()
                return
        await query.answer("Нет доступа.", show_alert=True)
        return
    action = (query.data or "").split(":", 1)[1]
    if action in _ADDON_OWNED_MENU_ACTIONS:
        # Owned by an addon router registered after the wizard — let the
        # event propagate instead of swallowing it here.
        raise SkipHandler()
    if action in {"download", "role", "brain", "tokens", "terminal", "learn", "editor", "test", "masterroom", "vision"} and not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    if action.startswith("vision:") and not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    if (action == "projmap" or action.startswith("projmap:")) and not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    if action.startswith("install:") and not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()

    if action.startswith("install:"):
        slug = action.split(":", 1)[1]
        batch = INSTALL_BATCHES.get(slug)
        if batch is None:
            await query.answer("Неизвестная установка", show_alert=True)
            return
        title, lines = batch
        await query.answer()
        if query.message is not None:
            from .config import DATA_DIR
            from .handlers import run_work_batch

            # bash2mp4 in particular: if the user already ran the install,
            # don't re-clone — show the settings screen with the
            # «использовать / удалить» toggle. The user explicitly asked
            # for a single «Использовать» button on each downloader
            # screen so that picking one disables the other.
            if slug == "downloader":
                project_dir = DATA_DIR / "projects" / "bash2mp4"
                if project_dir.is_dir():
                    await _show_bash2mp4_screen(query)
                    return

            # Pass the real clicker uid: ``query.message.from_user`` is the
            # bot itself for inline-keyboard messages, so the authorisation
            # check inside ``run_work_batch`` needs the user explicitly.
            await run_work_batch(query.message, lines, title=title, user_id=uid)

            # After bash2mp4 successfully clones, show the full settings
            # screen (status + «Использовать» button) instead of just
            # printing static help text.
            if slug == "downloader":
                await _show_bash2mp4_screen(query)
        return

    if action == "toggle_monitoring":
        from .addons import state as addon_state
        from .addons.algorithm.scheduler import is_auto_monitoring_enabled
        current = is_auto_monitoring_enabled()
        addon_state.set_("algorithm", "auto_monitoring", not current)
        await query.answer(f"Авто-мониторинг: {'ВКЛ' if not current else 'ВЫКЛ'}")
        
        body_extra = ""
        if can_admin(uid):
            body_extra = (
                "\n\n<b>⛔ Доступ</b> — публичный / приватный режим бота, "
                "кому ты дал доступ."
            )
        kb = _kb_settings_menu(uid)
        gate_hint = (
            "ВКЛ — без /role проектные команды молчат"
            if storage.get_role_gate_enabled()
            else "ВЫКЛ — команды работают без выбора роли"
        )
        tts_hint = (
            "ВКЛ — каждый текстовый ответ дублируется голосом"
            if storage.get_tts_enabled()
            else "ВЫКЛ — бот молчит, отвечает только текстом"
        )
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "<b>⚙️ Настройки</b>\n\n"
                    f"<b>🎭 Роли</b> — сменить персону и вкл/выкл требование "
                    f"роли для /work, /exec, /clone (сейчас: {gate_hint}).\n\n"
                    f"<b>🔊 Голосовой ответчик</b> — озвучка ответов бота "
                    f"через Supertonic (сейчас: {tts_hint}).\n\n"
                    "<b>🧠 Соображалка</b> — как бот показывает что он "
                    "думает (белый бабл / белый + модель / тёмная плитка "
                    "по центру).\n\n"
                    "<b>💾 Память бота</b> — сколько последних сообщений "
                    "помнит и кнопка очистить.\n\n"
                    "<b>🧩 Алгоритм</b> — 10 слотов с пошаговыми планами. "
                    "Бот сам составит план по новости/описанию, может "
                    "запускать его периодически (мин/час/день).\n\n"
                    "<b>🔌 API (массовый импорт)</b> — одной строкой "
                    "загрузи все ключи (Brain 1/2, ElevenLabs, Tavily, "
                    "Brave, Exa, Firecrawl, Apify, GitHub PAT). "
                    "Разделители: <code>$$$</code> между блоками, "
                    "<code>;</code> между полями.\n\n"
                    "<b>🐙 GitHub проекты</b> — склонированные репозитории "
                    "(список, описание, удаление) и переключатель "
                    "авто-клонирования по ссылке.\n\n"
                    "<b>🖥 Терминал</b> — пакетный режим /work: пришлёшь "
                    "список команд одним сообщением, я выполню по очереди.\n\n"
                    "<b>📚 Узнать о функционале</b> — спроси LLM-помощника "
                    "что умеет бот и как этим пользоваться." + body_extra,
                    reply_markup=kb,
                )
        return

    if action == "toggle_ctxclear":
        # 🧹 Чистить контекст каждое действие (❗Важно): каждое сообщение —
        # свежий контекст, модель не забивается историей. Тумблер в настройках.
        from .addons import state as addon_state
        current = bool(addon_state.get("system", "ctx_clear_each", False))
        addon_state.set_("system", "ctx_clear_each", not current)
        await query.answer(f"Чистка контекста: {'ВКЛ' if not current else 'ВЫКЛ'}")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=_kb_settings_menu(uid))
        return

    if action == "toggle_selfcheck":
        # 🛡 Самопроверка при старте (рантайм-аналог Docker-гарда), toggle in settings.
        # бот при запуске проверяет наличие ключевых модулей и громко логирует
        # пропажу (вместо тихого «No module named bot.main»).
        from .addons import state as addon_state
        current = bool(addon_state.get("system", "startup_selfcheck", True))
        addon_state.set_("system", "startup_selfcheck", not current)
        await query.answer(f"Самопроверка при старте: {'ВКЛ' if not current else 'ВЫКЛ'}")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=_kb_settings_menu(uid))
        return

    if action == "vault":
        try:
            from .addons.vault.handlers import cb_vault_menu
            await cb_vault_menu(query, state)
        except Exception:
            logger.exception("vault screen failed")
            await query.answer("Сейф данных недоступен", show_alert=True)
        return

    if action == "helpzavr":
        await query.answer()
        try:
            from .addons.helpzavr.handlers import show_screen as _hz_show
            await _hz_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("helpzavr screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Helpzavr не загружен. Проверь логи."
                )
        return

    if action == "analyzer":
        # Analyzer PRO — пульт Analyzer Bot Family (ana).
        # Owner-only; гейт продублирован внутри addon'а.
        if not can_admin(uid):
            await query.answer("Только владелец.", show_alert=True)
            return
        await query.answer()
        try:
            from .addons.analyzer.handlers import show_screen as _ana_show
            await _ana_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("analyzer screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Анализатор PRO не загружен. Проверь логи."
                )
        return

    if action == "issuance":
        await query.answer()
        try:
            from .addons.issuance.handlers import show_screen as _iss_show
            await _iss_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("issuance screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Модуль Выдача не загружен. Проверь логи."
                )
        return

    if action == "test":
        # «🧪 Тест» — web preview surface. Owner-only.
        if not can_admin(uid):
            await query.answer("Только владелец.", show_alert=True)
            return
        await query.answer()
        if query.message is not None:
            await query.message.answer(
                _test_status_text(),
                reply_markup=_kb_test(),
                parse_mode=ParseMode.HTML,
            )
        return

    if action == "projmap" or action.startswith("projmap:"):
        # «🗺 Карта проекта» — graphify-backed interactive project map.
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from bot.addons.projectmap import store as _pm_store
        from bot.addons.projectmap import installer as _pm_install
        from bot.addons.projectmap import hermes as _pm_hermes

        sub = action.split(":", 1)[1] if ":" in action else ""

        def _pm_kb() -> InlineKeyboardMarkup:
            rows: list[list[InlineKeyboardButton]] = []
            # Map opens with the built-in renderer — graphify is NOT required.
            # Behave like VISION: mint/show the link immediately.
            if _pm_store.is_enabled() and _pm_store.get_link():
                rows.append([InlineKeyboardButton(text="🔗 Открыть карту", url=_pm_store.get_link())])
                rows.append([InlineKeyboardButton(text="🔄 Новая ссылка", callback_data="main:projmap:rotate")])
                rows.append([InlineKeyboardButton(text="🔴 Выключить", callback_data="main:projmap:off")])
            else:
                rows.append([InlineKeyboardButton(text="🔗 Включить карту", callback_data="main:projmap:open")])
            rows.append([InlineKeyboardButton(text="🔁 Перестроить карту", callback_data="main:projmap:rebuild")])
            rows.append([InlineKeyboardButton(text="🧠 Гермес: проанализировать", callback_data="main:projmap:hermes")])
            rows.append([InlineKeyboardButton(text="📤 Экспорт базы связок", callback_data="main:projmap:export")])
            gtxt = "✅ graphify скачан (обновить)" if _pm_install.is_installed() else "📥 Скачать graphify (опц.)"
            rows.append([InlineKeyboardButton(text=gtxt, callback_data="main:projmap:install")])
            rows.append([InlineKeyboardButton(text="⚙️ Настройки (использовать/авто)", callback_data="pmg:settings")])
            rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="main:menu")])
            return InlineKeyboardMarkup(inline_keyboard=rows)

        def _pm_text() -> str:
            status = {
                "none": "не установлен", "installing": "устанавливается…",
                "ready": "готов", "error": "ошибка установки",
            }.get(_pm_store.get_install(), _pm_store.get_install())
            g = _pm_store.get_graph()
            nodes = g.get("node_count", 0) if isinstance(g, dict) else 0
            return (
                "<b>🗺 Карта проекта</b>\n\n"
                f"graphify: <b>{status}</b>\n"
                f"Функций на карте: <b>{nodes}</b>\n"
                f"Курируемых связок: <b>{len(_pm_store.get_edges())}</b>\n"
                f"Использовать в ИИ: <b>{'ВКЛ' if _pm_store.get_use() else 'ВЫКЛ'}</b> · "
                f"Авто: <b>{'ВКЛ' if _pm_store.get_auto() else 'ВЫКЛ'}</b>\n\n"
                "Интерактивный граф функций и их связок (сначала → потом). "
                "Экспортируй базу связок и вставь её в другой такой же проект."
            )

        if sub == "install":
            await query.answer("📥 Качаю graphify…")
            if query.message is not None:
                await query.message.answer("⏳ Скачиваю graphify в проект, подожди…")
            try:
                res = await asyncio.to_thread(_pm_install.install_sync)
                ok = res.get("ok")
            except Exception:  # noqa: BLE001
                logging.exception("graphify install failed")
                ok = False
            if query.message is not None:
                await query.message.answer(
                    "✅ graphify готов — можно открывать карту." if ok
                    else "⚠️ Не удалось установить graphify (см. логи). "
                         "Карта всё равно строится встроенным рендерером.",
                    reply_markup=_pm_kb(), parse_mode=ParseMode.HTML)
            return

        if sub == "rotate" or sub == "open":
            try:
                if _pm_store.is_enabled() and _pm_store.get_link() and sub != "rotate":
                    link = _pm_store.get_link()
                else:
                    _, link = _pm_store.generate_link()
                await query.answer("🔄 Ссылка готова")
            except Exception:  # noqa: BLE001
                await query.answer("Ошибка генерации ссылки", show_alert=True)
                return
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await query.message.edit_text(_pm_text(), reply_markup=_pm_kb(),
                                                  parse_mode=ParseMode.HTML,
                                                  disable_web_page_preview=True)
            return

        if sub == "off":
            _pm_store.disable_link()
            await query.answer("🔴 Карта выключена")
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await query.message.edit_text(_pm_text(), reply_markup=_pm_kb(),
                                                  parse_mode=ParseMode.HTML)
            return

        if sub == "rebuild":
            await query.answer("🔁 Пересобираю карту…")
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_pm_hermes.rebuild_graph)
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await query.message.edit_text(_pm_text(), reply_markup=_pm_kb(),
                                                  parse_mode=ParseMode.HTML)
            return

        if sub == "hermes":
            await query.answer("🧠 Гермес анализирует…")
            summary = {}
            with contextlib.suppress(Exception):
                summary = await asyncio.to_thread(_pm_hermes.analyze_and_update)
            if query.message is not None:
                await query.message.answer(
                    "🧠 Гермес обновил карту: "
                    f"{summary.get('nodes', 0)} функций, {summary.get('edges', 0)} связей, "
                    f"добавлено в базу {summary.get('promoted', 0)}.",
                    reply_markup=_pm_kb(), parse_mode=ParseMode.HTML)
            return

        if sub == "export":
            await query.answer("📤 Выгружаю базу…")
            try:
                from aiogram.types import BufferedInputFile
                import json as _json
                payload = _json.dumps(_pm_store.export_base(), ensure_ascii=False, indent=2)
                doc = BufferedInputFile(payload.encode("utf-8"), filename="projectmap_base.json")
                if query.message is not None:
                    await query.message.answer_document(
                        doc, caption="🗺 База связок. Вставь в другой проект через «📥 Импорт базы».")
            except Exception:  # noqa: BLE001
                await query.answer("Ошибка экспорта", show_alert=True)
            return

        # Default: show the project-map home screen.
        await query.answer()
        if query.message is not None:
            await query.message.answer(_pm_text(), reply_markup=_pm_kb(),
                                       parse_mode=ParseMode.HTML)
        return

    if action == "obuchalka" or action.startswith("obuchalka:"):
        # «🎓 Обучалка» — сосуд знаний: учителя по ссылке, ученик Qwen, экспорт.
        if not can_admin(uid):
            await query.answer("Только владелец.", show_alert=True)
            return
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from bot.addons.obuchalka import export as _ob_export
        from bot.addons.obuchalka import seed as _ob_seed
        from bot.addons.obuchalka import store as _ob_store

        _ob_seed.ensure_seed()
        sub = action.split(":", 1)[1] if ":" in action else ""

        def _ob_kb() -> InlineKeyboardMarkup:
            rows: list[list[InlineKeyboardButton]] = []
            for role, label in (("teacher", "учителя"), ("revisor", "ревизора")):
                if _ob_store.is_enabled(role) and _ob_store.get_link(role):
                    rows.append([InlineKeyboardButton(
                        text=f"🔗 Открыть панель {label}", url=_ob_store.get_link(role))])
                    rows.append([
                        InlineKeyboardButton(
                            text=f"🔄 Новая ссылка {label}",
                            callback_data=f"main:obuchalka:rotate:{role}"),
                        InlineKeyboardButton(
                            text="🔴 Выкл",
                            callback_data=f"main:obuchalka:off:{role}"),
                    ])
                else:
                    rows.append([InlineKeyboardButton(
                        text=f"🔗 Включить ссылку {label}",
                        callback_data=f"main:obuchalka:rotate:{role}")])
            rows.append([InlineKeyboardButton(
                text="📤 Экспорт цемента (JSONL)", callback_data="main:obuchalka:export")])
            rows.append([InlineKeyboardButton(
                text="📖 Журнал смен", callback_data="main:obuchalka:journal")])
            rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="main:menu")])
            return InlineKeyboardMarkup(inline_keyboard=rows)

        def _ob_text() -> str:
            s = _ob_store.stats()
            qwen = _ob_store.get_qwen_base_url() or "не подключён"
            return (
                "<b>🎓 Обучалка — сосуд знаний</b>\n\n"
                f"Сосудов: <b>{s['vessels']}</b> · Листиков: <b>{s['leaves']}</b>\n"
                f"В основании (10/10): <b>{s['foundation']}</b> · "
                f"Близко (7–9): <b>{s['near']}</b> · Пустых: <b>{s['empty']}</b>\n"
                f"Ученик Qwen: <b>{qwen}</b>\n\n"
                "Учитель наполняет листики по заданиям, ревизор следит за руслом "
                "и ставит баллы. Балл 10 — листик уходит в основание и блокируется. "
                "Endpoint ученика задаётся в веб-панели учителя."
            )

        if sub.startswith("rotate:"):
            role = sub.split(":", 1)[1]
            try:
                _, _link = _ob_store.generate_link(role)
                await query.answer("🔄 Ссылка готова")
            except Exception:  # noqa: BLE001
                await query.answer("Ошибка генерации ссылки", show_alert=True)
                return
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await query.message.edit_text(
                        _ob_text(), reply_markup=_ob_kb(), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)
            return

        if sub.startswith("off:"):
            role = sub.split(":", 1)[1]
            with contextlib.suppress(Exception):
                _ob_store.disable_link(role)
            await query.answer("🔴 Ссылка выключена")
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await query.message.edit_text(
                        _ob_text(), reply_markup=_ob_kb(), parse_mode=ParseMode.HTML)
            return

        if sub == "export":
            await query.answer("📤 Собираю цемент…")
            try:
                from aiogram.types import BufferedInputFile
                payload = _ob_export.build_jsonl()
                if not payload:
                    if query.message is not None:
                        await query.message.answer(
                            "Пока нет листиков с баллом 10 — цементу не из чего застыть.")
                    return
                doc = BufferedInputFile(payload.encode("utf-8"), filename="cement.jsonl")
                if query.message is not None:
                    await query.message.answer_document(
                        doc, caption="🎓 Цемент: датасет из листиков-оснований (балл 10). "
                                     "Забирай на комп для дообучения Qwen.")
            except Exception:  # noqa: BLE001
                await query.answer("Ошибка экспорта", show_alert=True)
            return

        if sub == "journal":
            await query.answer()
            entries = _ob_store.journal_recent(15)
            lines = [
                f"• [{e['role']}] {e['action']}: {e['detail'][:80]}" for e in entries
            ] or ["Журнал пуст."]
            if query.message is not None:
                await query.message.answer(
                    "<b>📖 Журнал смен</b>\n\n" + "\n".join(lines),
                    parse_mode=ParseMode.HTML)
            return

        # Default: home screen.
        await query.answer()
        if query.message is not None:
            await query.message.answer(_ob_text(), reply_markup=_ob_kb(),
                                       parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        return

    if action == "vision":
        await query.answer("Проверяю VISION и backend…")
        try:
            text, kb = await asyncio.to_thread(_vision_screen, ensure_link=True)
            if query.message is not None:
                await query.message.answer(
                    text, reply_markup=kb, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception:  # noqa: BLE001
            logger.exception("VISION open/preflight failed")
            await query.answer(
                "VISION не запустилась. Нажмите ещё раз для повторной проверки.",
                show_alert=True,
            )
        return

    if action == "vision:rotate":
        try:
            text, kb = await asyncio.to_thread(_vision_screen, rotate=True)
            await query.answer("Новая ссылка создана; старая отозвана")
            if query.message is not None:
                await query.message.edit_text(
                    text, reply_markup=kb, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception:  # noqa: BLE001
            logger.exception("VISION token rotation failed")
            await query.answer("Не удалось обновить ссылку", show_alert=True)
        return

    if action == "vision:check":
        try:
            text, kb = await asyncio.to_thread(_vision_screen)
            await query.answer("Проверка завершена")
            if query.message is not None:
                await query.message.edit_text(
                    text, reply_markup=kb, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception:  # noqa: BLE001
            logger.exception("VISION manual preflight failed")
            await query.answer("Backend пока недоступен. Попробуйте ещё раз.", show_alert=True)
        return

    if action == "vision:off":
        try:
            from .addons.vision.server import disable_link as _vis_disable
            was = await asyncio.to_thread(_vis_disable)
            text, kb = await asyncio.to_thread(_vision_screen)
            await query.answer(
                "Доступ отозван; старая ссылка больше не работает" if was else "Доступ уже отключён",
            )
            if query.message is not None:
                await query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            logger.exception("VISION revoke failed")
            await query.answer("Не удалось отключить доступ", show_alert=True)
        return

    if action == "masterroom":
        # «🛠 Master Room» — devin-style coder/test/deploy console.
        # P1 surface (8-button submenu + FSM) lives in the addon's
        # own Router; this branch just forwards the open intent.
        if not can_admin(uid):
            await query.answer("Только владелец.", show_alert=True)
            return
        try:
            from .addons.masterroom import show_screen as _mr_show
            await query.answer()
            await _mr_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("masterroom screen failed")
            await query.answer("Master Room недоступен", show_alert=True)
        return

    if action == "markitdown":
        # Settings screen for the Markitdown auxiliary tool.
        # Five buttons: auto-on, auto-off, install (clone), uninstall, back.
        # "Auto" mode means when the user pastes a YouTube/Vimeo URL with
        # «дай данные <URL>» / «посмотри и расскажи о чём там <URL>», the
        # bot runs markitdown on it automatically instead of falling
        # through to the LLM agent.
        await query.answer()
        await _show_markitdown_screen(query)
        return

    if action == "pretty":
        await query.answer()
        try:
            from .addons.pretty_text.handlers import show_screen as _pt_show
            await _pt_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("pretty_text screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Красивый текст не загружен. Проверь логи."
                )
        return

    if action == "mailbox":
        await query.answer()
        try:
            from .addons.mailbox.handlers import show_main as _mb_show
            await _mb_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("mailbox screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Проверка почты не загружен. Проверь логи."
                )
        return

    if action == "media_toggle":
        await query.answer()
        try:
            from .addons.media_toggle.handlers import show_screen as _mt_show
            await _mt_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("media_toggle screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Генерация фото и видео не загружен. Проверь логи."
                )
        return

    if action == "osint":
        await query.answer()
        try:
            from .addons.osint.handlers import show_screen as _osint_show
            await _osint_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("osint screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Исследование и поиск человека не загружен. Проверь логи."
                )
        return

    if action == "vibe":
        # «✨ Vibe Coding» — delegate to the vibe addon dashboard.
        # Without this branch the catch-all main: handler swallows the
        # click and the vibe_router never sees it.
        from .addons.vibe.handlers import cb_vibe_dashboard
        await cb_vibe_dashboard(query, state)
        return

    if action == "regen":
        # «🔧 Регенерация» — open the self-diagnosis / self-repair screen.
        await query.answer()
        await _regen_open(query, state)
        return

    if action == "dump":
        # «🗑 Скинуть весь мусор» — enter the collecting state. Everything
        # the user sends next is appended to their bin until they press
        # «📤 Отправить» / «✖️ Закрыть».
        await query.answer()
        await state.set_state(DumpStates.collecting)
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _dump_screen_text(uid),
                    reply_markup=_kb_dump(),
                    disable_web_page_preview=True,
                )
        return

    if action == "prompt_sources":
        await query.answer()
        try:
            from .addons.prompt_sources.handlers import show_screen as _ps_show
            await _ps_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("prompt_sources screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Источники промптов не загружен. Проверь логи."
                )
        return

    if action == "priority":
        await query.answer()
        try:
            from .addons.priority.handlers import show_screen as _prio_show
            await _prio_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("priority screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Приоритет не загружен. Проверь логи."
                )
        return

    if action == "github":
        # Shortcut from the main menu's «🐙 GitHub проекты» row —
        # opens the same screen as /github or Settings → 🐙 GitHub
        # проекты. Re-uses ``cb_gh_menu`` so the body / keyboard stay
        # in sync with the canonical screen.
        await cb_gh_menu(query, state)
        return

    if action == "terminal":
        # Shortcut from the main menu's «🖥 Терминал» row — enters the
        # batch-terminal FSM exactly like ``/work`` does. We need the
        # admin check because /work modifies the project workspace.
        if not can_admin(uid):
            await query.answer("Только владелец.", show_alert=True)
            return
        await query.answer()
        from .handlers import WorkStates

        await state.set_state(WorkStates.awaiting_commands)
        if query.message is not None:
            await query.message.answer(
                "<b>🖥 Терминал</b>\n\n"
                "Жду команды списком (по одной на строку). Пришли одним "
                "сообщением — выполню по порядку, ждя завершения каждой. "
                "Таймаут на команду снят (до 1 часа).\n\n"
                "Поддерживаю: <code>/exec</code>, <code>/clone</code>, "
                "<code>/project</code>, <code>/cd</code>, <code>/git</code>. "
                "Строка без <code>/</code> — bash в текущем проекте. Пустые "
                "строки и <code>#</code>-комментарии пропускаю.\n\n"
                "Выйти из режима: /cancel"
            )
        return

    if action == "learn":
        # Shortcut from Settings → «📚 Узнать о функционале» — opens a
        # tutorial chat with the main LLM. Subsequent text messages
        # are routed through ``cb_learn_question`` until the user
        # types /cancel.
        await query.answer()
        await state.set_state(SetupStates.awaiting_learn_question)
        if query.message is not None:
            await query.message.answer(_learn_intro_body())
        return

    if action == "settings":
        await query.answer()
        body_extra = ""
        if can_admin(uid):
            body_extra = (
                "\n\n<b>⛔ Доступ</b> — публичный / приватный режим бота, "
                "кому ты дал доступ."
            )
        kb = _kb_settings_menu(uid)
        gate_hint = (
            "ВКЛ — без /role проектные команды молчат"
            if storage.get_role_gate_enabled()
            else "ВЫКЛ — команды работают без выбора роли"
        )
        tts_hint = (
            "ВКЛ — каждый текстовый ответ дублируется голосом"
            if storage.get_tts_enabled()
            else "ВЫКЛ — бот молчит, отвечает только текстом"
        )
        if query.message is not None:
            try:
                await query.message.edit_text(
                    "<b>⚙️ Настройки</b>\n\n"
                    f"<b>🎭 Роли</b> — сменить персону и вкл/выкл требование "
                    f"роли для /work, /exec, /clone (сейчас: {gate_hint}).\n\n"
                    f"<b>🔊 Голосовой ответчик</b> — озвучка ответов бота "
                    f"через Supertonic (сейчас: {tts_hint}).\n\n"
                    "<b>🧠 Соображалка</b> — как бот показывает что он "
                    "думает (белый бабл / белый + модель / тёмная плитка "
                    "по центру).\n\n"
                    "<b>💾 Память бота</b> — сколько последних сообщений "
                    "помнит и кнопка очистить.\n\n"
                    "<b>🧩 Алгоритм</b> — 10 слотов с пошаговыми планами. "
                    "Бот сам составит план по новости/описанию, может "
                    "запускать его периодически (мин/час/день).\n\n"
                    "<b>🔌 API (массовый импорт)</b> — одной строкой "
                    "загрузи все ключи (Brain 1/2, ElevenLabs, Tavily, "
                    "Brave, Exa, Firecrawl, Apify, GitHub PAT). "
                    "Разделители: <code>$$$</code> между блоками, "
                    "<code>;</code> между полями.\n\n"
                    "<b>🐙 GitHub проекты</b> — склонированные репозитории "
                    "(список, описание, удаление) и переключатель "
                    "авто-клонирования по ссылке.\n\n"
                    "<b>🖥 Терминал</b> — пакетный режим /work: пришлёшь "
                    "список команд одним сообщением, я выполню по очереди.\n\n"
                    "<b>📚 Узнать о функционале</b> — спроси LLM-помощника "
                    "что умеет бот и как этим пользоваться." + body_extra,
                    reply_markup=kb,
                )
            except Exception:  # noqa: BLE001
                await query.message.answer(
                    "Настройки", reply_markup=kb
                )
        return

    if action == "thinking":
        await query.answer()
        try:
            from .addons.thinking_style import show_screen as _ts_show
            await _ts_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("thinking_style screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Соображалка не загружен. Проверь логи."
                )
        return

    if action == "memory":
        await query.answer()
        try:
            from .addons.memory.handlers import show_screen as _mem_show
            await _mem_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("memory screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Память бота не загружен. Проверь логи."
                )
        return

    if action == "ram":
        await query.answer()
        try:
            from .addons.ram_guard.handlers import show_screen as _ram_show
            await _ram_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("ram screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон RAM не загружен. Проверь логи."
                )
        return

    if action == "download":
        await query.answer()
        await _show_download_screen(query)
        return

    if action == "role":
        await query.answer()
        await _show_role_picker(query, state)
        return

    if action == "brain":
        await query.answer()
        if query.message is not None:
            await query.message.edit_text(
                "Перенастройка. Выбери мозг:",
                reply_markup=_kb_brain(),
            )
        return

    if action == "editor":
        await query.answer()
        try:
            from .addons.editor_agent.handlers import show_screen as _ed_show
            await _ed_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("editor_agent screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Монтажёр не загружен. Проверь логи."
                )
        return

    if action == "tokens":
        await query.answer()
        if query.message is not None:
            from .token_tracker import format_token_stats

            await query.message.answer(format_token_stats())
        return

    if action == "debates":
        await query.answer()
        try:
            from .addons.debates.handlers import show_screen as _deb_show
            await _deb_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("debates screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон Дебаты не загружен. Проверь логи."
                )
        return

    if action == "ai_check":
        await query.answer()
        try:
            from .addons.ai_check.handlers import show_screen as _aic_show
            await _aic_show(query)
        except Exception:  # noqa: BLE001
            logger.exception("ai_check screen failed")
            if query.message is not None:
                await query.message.answer(
                    "Аддон AI CHECK не загружен. Проверь логи."
                )
        return

    if action in ("open", "menu"):
        # Re-render main menu (used by "Назад" in addons)
        await query.answer()
        if query.message is not None:
            try:
                await query.message.edit_text(
                    "Главное меню. Выбери что сделать:",
                    reply_markup=_kb_main_after_claim(uid),
                )
            except Exception:  # noqa: BLE001
                await query.message.answer(
                    "Главное меню. Выбери что сделать:",
                    reply_markup=_kb_main_after_claim(uid),
                )
        return

    await query.answer("Неизвестный пункт меню", show_alert=True)


# ---- /access:* — public/private mode + co-owner list --------------------


_ACCESS_LABELS = {
    "private": "🔒 Приватная",
    "public": "🌐 Публичная",
    "full_public": "🔓 Публичная полная",
}


def _kb_access_menu() -> InlineKeyboardMarkup:
    mode = storage.get_access_mode()
    rows: list[list[InlineKeyboardButton]] = []
    for key in ("public", "full_public", "private"):
        marker = "▶ " if mode == key else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}{_ACCESS_LABELS[key]}",
                    callback_data=f"access:set:{key}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔒 Приват режим", callback_data="pm:menu")]
    )
    rows.append(
        [InlineKeyboardButton(text="➕ Дать доступ", callback_data="access:add")]
    )
    if storage.get_co_owners():
        rows.append(
            [InlineKeyboardButton(text="👥 Список доступов", callback_data="access:list")]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ Вернуться", callback_data="access:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _access_screen_body() -> str:
    mode = storage.get_access_mode()
    co_owners = storage.get_co_owners()
    co_owner_str = (
        ", ".join(f"<code>{cid}</code>" for cid in co_owners)
        if co_owners
        else "<i>пусто</i>"
    )
    return (
        "<b>⛔ Доступ</b>\n\n"
        f"Сейчас: <b>{_ACCESS_LABELS[mode]}</b>\n\n"
        "<b>🌐 Публичная</b> — пользоваться ботом могут все, "
        "но менять настройки (API-ключи, модели, статистика, импорт API, "
        "доступ) можешь только ты + те, кому ты дал доступ.\n\n"
        "<b>🔓 Публичная полная</b> — у всех полный доступ как у "
        "владельца, включая настройки и API-ключи.\n\n"
        "<b>🔒 Приватная</b> — только ты + те, кому ты дал доступ. "
        "Остальные получают вежливый отказ.\n\n"
        f"Соучастники (полный доступ как у владельца): {co_owner_str}"
    )


@wizard_router.callback_query(F.data == "access:menu")
async def cb_access_menu(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_admin(user.id if user else None):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        try:
            await query.message.edit_text(
                _access_screen_body(), reply_markup=_kb_access_menu()
            )
        except Exception:  # noqa: BLE001
            await query.message.answer(
                _access_screen_body(), reply_markup=_kb_access_menu()
            )
    await query.answer()


@wizard_router.callback_query(F.data.startswith("access:set:"))
async def cb_access_set(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_admin(user.id if user else None):
        await query.answer("Только владелец.", show_alert=True)
        return
    # Only the real owner (not co-owners) may flip the access mode
    # so a co-owner can't accidentally lock the owner out by making
    # everything full-public or by demoting back to private.
    if not is_owner(user.id if user else None):
        await query.answer(
            "Менять режим доступа может только владелец.", show_alert=True
        )
        return
    mode = (query.data or "").split(":", 2)[2]
    if mode not in _ACCESS_LABELS:
        await query.answer("Неизвестный режим.", show_alert=True)
        return
    storage.set_access_mode(mode)
    await query.answer(f"Режим: {_ACCESS_LABELS[mode]}")
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _access_screen_body(), reply_markup=_kb_access_menu()
            )


@wizard_router.callback_query(F.data == "access:add")
async def cb_access_add(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_admin(user.id if user else None):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(AccessStates.awaiting_co_owner_id)
    if query.message is not None:
        await query.message.answer(
            "Пришли <b>Telegram id</b> того, кому хочешь дать полный "
            "доступ. Это число (например <code>123456789</code>). "
            "Узнать свой id может любой через @userinfobot или прочитав "
            "сообщение от моего /start.\n\n"
            "Этот человек получит права как у тебя: сможет менять "
            "настройки, API-ключи, модели, статистику. Отозвать доступ "
            "можно в «👥 Список доступов».\n\n"
            "Чтобы отменить — пришли <code>/cancel</code>."
        )
    await query.answer()


@wizard_router.message(StateFilter(AccessStates.awaiting_co_owner_id))
async def capture_co_owner_id(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None or not can_admin(user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw.lower() in ("/cancel", "отмена", "cancel"):
        await state.clear()
        await message.answer("Отменил. Список доступов не изменён.")
        return
    try:
        new_id = int(raw)
    except ValueError:
        await message.answer(
            "Это не число. Пришли только Telegram id (например "
            "<code>123456789</code>) или /cancel чтобы отменить."
        )
        return
    if new_id == storage.get_owner_id():
        await state.clear()
        await message.answer(
            "Это твой собственный id — ты уже владелец, доп. доступ не нужен."
        )
        return
    added = storage.add_co_owner(new_id)
    await state.clear()
    if not added:
        await message.answer(
            f"Этот id (<code>{new_id}</code>) уже в списке доступов.",
            reply_markup=_kb_access_menu(),
        )
        return
    await message.answer(
        f"Готово. <code>{new_id}</code> теперь имеет полный доступ как у "
        "владельца. Чтобы отозвать — открой «👥 Список доступов».",
        reply_markup=_kb_access_menu(),
    )


@wizard_router.callback_query(F.data == "access:list")
async def cb_access_list(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_admin(user.id if user else None):
        await query.answer("Только владелец.", show_alert=True)
        return
    owners = storage.get_co_owners()
    rows: list[list[InlineKeyboardButton]] = []
    for uid in owners:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Отозвать {uid}",
                    callback_data=f"access:revoke:{uid}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ Назад", callback_data="access:menu")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    body = (
        "<b>👥 Список доступов</b>\n\n"
        + (
            "\n".join(f"• <code>{uid}</code>" for uid in owners)
            if owners
            else "<i>пусто</i>"
        )
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(body, reply_markup=kb)
    await query.answer()


@wizard_router.callback_query(F.data.startswith("access:revoke:"))
async def cb_access_revoke(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_admin(user.id if user else None):
        await query.answer("Только владелец.", show_alert=True)
        return
    try:
        target = int((query.data or "").split(":", 2)[2])
    except ValueError:
        await query.answer("Bad id", show_alert=True)
        return
    removed = storage.remove_co_owner(target)
    await query.answer(
        f"Доступ {'отозван' if removed else 'и так нет'}: {target}"
    )
    # Re-render the access:list screen so the row disappears.
    await cb_access_list(query, state)


@wizard_router.callback_query(F.data == "access:back")
async def cb_access_back(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    uid = user.id if user else None
    if not can_use(uid):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>⚙️ Настройки</b>", reply_markup=_kb_settings_menu(uid)
            )
    await query.answer()


# ---- 🐙 GitHub settings (list / info / delete / auto-clone toggle) ----


def _kb_github_menu() -> InlineKeyboardMarkup:
    """Top of the GitHub settings screen.

    Shows one row per cloned repo (with name + short description as the
    button label) plus a row each for the "🔁 Авто-клонирование"
    toggle and back-navigation.
    """
    rows: list[list[InlineKeyboardButton]] = []
    repos = storage.list_github_repos()
    if not repos:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📭 Пусто. Кинь мне ссылку на репо в чат — склонирую.",
                    callback_data="gh:noop",
                )
            ]
        )
    else:
        for repo in repos:
            label = f"📦 {repo['name']}"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"gh:info:{repo['name']}",
                    )
                ]
            )
    auto = storage.get_github_auto_clone()
    rows.append(
        [
            InlineKeyboardButton(
                text=("🔁 Авто-клонирование: ВКЛ" if auto else "🔁 Авто-клонирование: ВЫКЛ"),
                callback_data="gh:auto",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="⚙️ Управление GitHub", callback_data="mr:gh:manage")]
    )
    rows.append(
        [InlineKeyboardButton(text="↩️ В настройки", callback_data="main:settings")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _github_menu_body() -> str:
    repos = storage.list_github_repos()
    auto = storage.get_github_auto_clone()
    head = (
        "<b>🐙 GitHub-проекты</b>\n\n"
        "Здесь живут все репо что я склонировал на сервер. Тап на "
        "репо — увижу описание и кнопку удалить (стираю и с диска, и "
        "из памяти бота).\n\n"
        f"<b>🔁 Авто-клонирование:</b> {'ВКЛ' if auto else 'ВЫКЛ'}.\n"
        "Если ВКЛ — кидаешь GitHub-ссылку в чат и просишь что-то "
        "посмотреть → я сам склонирую и почитаю. Если ВЫКЛ — буду "
        "ждать явной команды «склонируй»."
    )
    if not repos:
        return head + "\n\n<i>Пока ни одного репо не склонировано.</i>"
    return head + f"\n\nВсего: <b>{len(repos)}</b>."


@wizard_router.callback_query(F.data == "gh:menu")
async def cb_gh_menu(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_use(user.id if user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _github_menu_body(), reply_markup=_kb_github_menu()
            )
    await query.answer()


@wizard_router.callback_query(F.data == "gh:auto")
async def cb_gh_auto(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_admin(user.id if user else None):
        # Toggle is admin-only — guests in public mode can browse repos
        # but can't change the bot's behaviour.
        await query.answer("Только владелец может менять.", show_alert=True)
        return
    current = storage.get_github_auto_clone()
    storage.set_github_auto_clone(not current)
    new_state = "ВКЛЮЧЕНО" if not current else "ВЫКЛЮЧЕНО"
    await query.answer(f"Авто-клонирование: {new_state}", show_alert=False)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _github_menu_body(), reply_markup=_kb_github_menu()
            )


@wizard_router.callback_query(F.data.startswith("gh:info:"))
async def cb_gh_info(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_use(user.id if user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    name = query.data.split(":", 2)[2] if query.data else ""
    meta = storage.get_github_repo(name)
    if meta is None:
        await query.answer("Репо больше нет на сервере.", show_alert=True)
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _github_menu_body(), reply_markup=_kb_github_menu()
                )
        return
    description = meta.get("description") or "<i>описание не извлечено (нет README)</i>"
    url = meta.get("url") or ""
    body = (
        f"<b>📦 {_html_escape(name)}</b>\n\n"
        f"<b>Описание:</b> {_html_escape(description)}\n\n"
    )
    if url:
        body += f"<b>Источник:</b> <code>{_html_escape(url)}</code>\n\n"
    body += (
        "<b>Пример использования</b> (просто напиши боту в чат):\n"
        f"• «глянь что в проекте {_html_escape(name)}»\n"
        f"• «переключись на {_html_escape(name)} и покажи структуру»\n"
        f"• «в {_html_escape(name)} найди где обрабатывается X»\n\n"
        "Удалить — кнопка ниже. Сотрётся и с диска, и из памяти бота."
    )
    rows: list[list[InlineKeyboardButton]] = []
    if can_admin(user.id if user else None):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Удалить {name}",
                    callback_data=f"gh:del:{name}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ Назад к списку", callback_data="gh:menu")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(body, reply_markup=kb)
    await query.answer()


@wizard_router.callback_query(F.data.startswith("gh:del:"))
async def cb_gh_delete(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_admin(user.id if user else None):
        await query.answer("Только владелец может удалять.", show_alert=True)
        return
    name = query.data.split(":", 2)[2] if query.data else ""
    if not name:
        await query.answer("?", show_alert=False)
        return
    # Two-step confirm so a misclick doesn't nuke a 5GB repo.
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 Да, удалить {name}",
                callback_data=f"gh:delconfirm:{name}",
            )
        ],
        [
            InlineKeyboardButton(
                text="◀️ Отмена",
                callback_data=f"gh:info:{name}",
            )
        ],
    ]
    body = (
        f"<b>Удалить «{_html_escape(name)}»?</b>\n\n"
        "Стираю папку с диска (rm -rf) и забываю описание в state.json. "
        "Это безвозвратно — гитхаб-исходник не трогаю, но локальную "
        "копию (со всеми твоими изменениями если были) уже не вернёшь."
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                body, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
    await query.answer()


@wizard_router.callback_query(F.data.startswith("gh:delconfirm:"))
async def cb_gh_delete_confirm(query: CallbackQuery, state: FSMContext) -> None:
    user = query.from_user
    if not can_admin(user.id if user else None):
        await query.answer("Только владелец может удалять.", show_alert=True)
        return
    name = query.data.split(":", 2)[2] if query.data else ""
    from .tools import ToolError, delete_project

    try:
        existed = delete_project(name)
    except ToolError as exc:
        await query.answer(f"Ошибка: {exc}", show_alert=True)
        return
    msg = f"🗑 Удалено: <code>{_html_escape(name)}</code>." if existed else (
        "<i>На диске не было</i> — стёр только запись о репо."
    )
    await query.answer("Удалено.", show_alert=False)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                msg + "\n\n" + _github_menu_body(),
                reply_markup=_kb_github_menu(),
            )


@wizard_router.callback_query(F.data == "gh:noop")
async def cb_gh_noop(query: CallbackQuery, state: FSMContext) -> None:
    # Stub for the "📭 Пусто" placeholder row so taps don't error.
    await query.answer(
        "Кинь в чат ссылку, например: https://github.com/owner/repo",
        show_alert=False,
    )


# ---- /role — pick a persona for this bot --------------------------------


async def _show_role_picker(
    query: CallbackQuery | None = None, state: FSMContext | None = None
) -> None:
    """Render the role-picker UI; reused by /role command and main-menu button."""
    text = _role_picker_text()
    markup = _kb_role_picker()
    if query is not None and query.message is not None:
        # Try to edit the existing menu in place; if that fails (different
        # message type, etc.), send a fresh one.
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 — edit_text raises on inline-kb mismatch
            await query.message.answer(text, reply_markup=markup)


def _role_picker_text() -> str:
    """The header shown above the persona grid."""
    active = get_persona()
    override = storage.get_persona_override()
    env_persona = os.environ.get("BOT_PERSONA", "boss").strip().lower()
    if override is not None:
        source = (
            f"override (через /role) — env BOT_PERSONA=<code>{_html_escape(env_persona)}</code>"
        )
    else:
        source = "env BOT_PERSONA"
    return (
        "<b>🎭 Сменить роль этого бота</b>\n\n"
        f"Сейчас активна: <b>{active.display_name}</b> "
        f"(<code>{active.key}</code>, {source})\n"
        f"<i>{active.title}</i>\n\n"
        "Жми на роль чтобы её посмотреть и активировать. ▶ — текущая. "
        "После клика я попрошу подтвердить. Все 20 ролей ниже:"
    )


@wizard_router.message(Command("role"))
async def cmd_role(message: Message, state: FSMContext) -> None:
    """Show the role picker to the owner."""
    user = message.from_user
    if user is None or not _is_owner(user.id):
        await message.answer("Только владелец может менять роль.")
        return
    await state.clear()
    await message.answer(_role_picker_text(), reply_markup=_kb_role_picker())


@wizard_router.callback_query(F.data.startswith("role:"))
async def cb_role(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    action = (query.data or "").split(":", 1)[1]
    await state.clear()

    if action == "back":
        await query.answer()
        if query.message is not None:
            persona = get_persona()
            await query.message.edit_text(
                f"<b>{persona.display_name}</b> на связи. Ты владелец.\n\n"
                f"<i>{persona.title}</i>\n\n"
                "Что хочешь?",
                reply_markup=_kb_main_after_claim(query.from_user.id if query.from_user else None),
            )
        return

    if action == "info":
        await query.answer()
        if query.message is not None:
            lines = ["<b>ℹ️ Все 20 ролей фермы</b>", ""]
            for p in list_personas():
                lines.append(
                    f"<b>{p.display_name}</b> (<code>{p.key}</code>) — "
                    f"{p.department}/{p.rank}"
                )
                lines.append(f"  <i>{p.title}</i>")
                lines.append(f"  {_html_escape(p.description)}")
                lines.append("")
            lines.append("◀️ Назад — кнопка ниже.")
            await query.message.answer(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад к ролям", callback_data="role:back_to_picker")]
                    ]
                ),
                disable_web_page_preview=True,
            )
        return

    if action == "back_to_picker":
        await query.answer()
        if query.message is not None:
            await query.message.answer(_role_picker_text(), reply_markup=_kb_role_picker())
        return

    if action == "reset":
        storage.clear_persona_override()
        await query.answer("Override снят, возвращаюсь к BOT_PERSONA из env")
        if query.message is not None:
            await query.message.edit_text(_role_picker_text(), reply_markup=_kb_role_picker())
        return

    if action.startswith("pick:"):
        persona_key = action.split(":", 1)[1]
        from .persona import get_persona as _get

        persona = _get(persona_key)
        await query.answer()
        if query.message is not None:
            current = get_persona()
            already = persona.key == current.key
            same_note = (
                "\n\n<i>Сейчас эта роль уже активна — нажми «Использовать» чтобы "
                "закрепить override.</i>"
                if already
                else ""
            )
            await query.message.edit_text(
                f"<b>{persona.display_name}</b>\n"
                f"<i>{persona.title}</i>\n"
                f"Отдел: <code>{persona.department}</code> • Ранг: <code>{persona.rank}</code>\n\n"
                f"{_html_escape(persona.description)}{same_note}",
                reply_markup=_kb_role_confirm(persona.key),
            )
        return

    if action.startswith("use:"):
        persona_key = action.split(":", 1)[1]
        storage.set_persona_override(persona_key)
        from .persona import get_persona as _get

        persona = _get(persona_key)
        await query.answer(f"Роль: {persona.display_name}")
        if query.message is not None:
            await query.message.edit_text(
                f"<b>✅ Готово. Теперь я — {persona.display_name}.</b>\n"
                f"<i>{persona.title}</i>\n\n"
                "Override сохранён в state.json. Чтобы вернуться к роли из "
                "BOT_PERSONA — открой <code>/role</code> → 🗑 Сбросить override.",
                reply_markup=_kb_main_after_claim(query.from_user.id if query.from_user else None),
            )
        return

    await query.answer("Неизвестная команда роли", show_alert=True)


# ---- Misc helpers --------------------------------------------------------


def _provider_label_from_storage() -> str:
    return storage.get_provider()


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _config_summary() -> str:
    """Pretty summary of the current LLM config, safe for HTML."""
    provider = storage.get_provider()
    keys = storage.list_provider_keys()
    label = "OpenRouter" if provider == "openrouter" else "Кастомный endpoint"
    key_state = keys.get("openrouter", {}).get("masked") or "не задан"
    if provider != "openrouter":
        key_state = keys.get("openrouter", {}).get("masked") or "не задан (нужен)"
    model = storage.get_model() or "—"
    base_url = storage.get_base_url()
    lines = [
        f"<b>{label}</b>",
        f"  Ключ: <code>{key_state}</code>",
        f"  Модель: <code>{_html_escape(model)}</code>",
    ]
    if provider != "openrouter":
        lines.append(f"  URL: <code>{_html_escape(base_url) if base_url else 'не задан'}</code>")
    return "\n".join(lines)


def _devin_handoff_prompt() -> str:
    """The text the owner pastes into a fresh Devin session.

    Devin will read this once and then live as the bot's brain — picking up
    inbox.log, replying via bot.send, etc.
    """
    return """Ты — мой удалённый ассистент с шелл-доступом к серверу, на котором живёт Telegram-бот.

ЧТО НА СЕРВЕРЕ
- Бот развёрнут как Docker-контейнер. Код — в моём GitHub-репо (точную ссылку
  я тебе дам отдельно, обычно это форк Lilush).
- Внутри контейнера: /app/bot/  (Python-пакет с handlers, wizard, agent и т.д.)
- Лог входящих сообщений: data/inbox.log  (бот пишет туда каждое моё сообщение)
- Состояние (ключи, brain, owner): data/state.json
- BOT_TOKEN — в .env рядом с ботом

ТВОЯ РАБОТА
Бот сейчас в режиме brain=devin: он НЕ отвечает мне сам. Когда я что-то пишу в TG, ты:
1. Читаешь свежие строки в data/inbox.log
2. Понимаешь что я хочу
3. Делаешь работу (правишь код, гонишь тесты, исследуешь, что угодно)
4. Отправляешь мне ответ обратно через CLI:
     set -a && source .env && set +a
     python -m bot.send <chat_id> "ответ"
   chat_id ты находишь в той же inbox.log строке.

ЕСЛИ ТЕБЕ НУЖЕН CONTEXT
Команды бота которые я могу запускать в TG (=ты их видишь в inbox.log как kind=cmd):
  /help, /start, /setup     — справка и перенастройка
  /brain, /setbrain auto|devin
  /clone <url>, /projects, /project <name>, /cd, /pwd
  /exec <bash>, /git <args>
  /keys, /setkey, /delkey, /models, /setmodel
  /enable, /disable, /reset

ВАЖНО
- Я (владелец) пингую тебя в нашем чате когда хочу чтобы ты отреагировал. Между пингами ты простаиваешь.
- Если просьба сложная — сначала разбери, потом ОДНИМ сообщением мне в TG пришли план; я скажу «ок» и ты делай.
- Если меняешь код — делай в ветке, делай PR, ссылку шли в TG.

Когда прочитал — ответь мне в этом чате (тут, у Devin) что готов, и я начну писать в TG."""


# ---- «скачай <URL>» / «🎞 Монтажёр» — natural-language download flow ----
#
# Matches a leading «скачай / скачать» followed by an HTTPS URL anywhere
# on the same line. Stays *outside* the slash-command space so the agent
# router below still gets first dibs on plain prose. The trailing URL
# is captured into group 1 — that's what we route into yt-dlp / bash2mp4.
#
# Examples:
#   «скачай https://www.instagram.com/reel/abc»
#   «Скачать https://www.youtube.com/shorts/xyz пожалуйста»
#   «  скачай  https://example.com/video.mp4  »
_DOWNLOAD_PHRASE_RE = re.compile(
    r"^\s*скача(?:й|ть)\s+(https?://\S+)",
    re.IGNORECASE,
)

# GitHub-flavoured URLs are handled by the *agent* (it auto-calls
# clone_repo). We only intercept video-ish URLs here.
_GITHUB_HOSTS = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
)


def _looks_like_video_url(url: str) -> bool:
    """Heuristic: True if URL is for a video site, not a code repo.

    Routing decision for «скачай <URL>»:
    - github / gitlab / bitbucket  → fall through to the agent (clone)
    - everything else (yt, ig, tiktok, vimeo, direct .mp4 …) → yt-dlp

    Kept deliberately loose — yt-dlp's site list covers 1500+ sources;
    we don't try to enumerate them.
    """
    u = url.strip().lower()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    return not any(host in u for host in _GITHUB_HOSTS)


def _downloader_choice() -> str | None:
    """Return saved «/dl» vs «bash2mp4» preference, or ``None`` if unset.

    Stored under ``addons.downloader.choice`` so it survives restarts
    and survives the «изолированные тесты» pytest snapshot.
    """
    from .addons import state as _addon_state

    val = _addon_state.get("downloader", "choice", None)
    if val in ("dl", "bash2mp4"):
        return val
    return None


def _set_downloader_choice(choice: str) -> None:
    from .addons import state as _addon_state

    _addon_state.set_("downloader", "choice", choice)


def _store_pending_download(chat_id: int, url: str) -> None:
    """Stash the URL so the choice callback can read it.

    Callback data is capped at 64 bytes — too tight for full URLs. We
    persist the pending URL under ``addons.downloader.pending.<chat>``
    and just pass «dl:choose:dl|bash» in callback_data.
    """
    from .addons import state as _addon_state

    bucket = _addon_state.get("downloader", "pending", {}) or {}
    bucket[str(chat_id)] = url
    _addon_state.set_("downloader", "pending", bucket)


def _pop_pending_download(chat_id: int) -> str | None:
    from .addons import state as _addon_state

    bucket = _addon_state.get("downloader", "pending", {}) or {}
    url = bucket.pop(str(chat_id), None)
    _addon_state.set_("downloader", "pending", bucket)
    return url


def _kb_downloader_choice() -> InlineKeyboardMarkup:
    """First-time chooser: full pipeline vs simple bash2mp4 download."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 /dl (нарезка + SEO)",
                    callback_data="dl:choose:dl",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬇️ bash2mp4 (просто скачать)",
                    callback_data="dl:choose:bash2mp4",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✖️ Отмена",
                    callback_data="dl:choose:cancel",
                )
            ],
        ]
    )


async def _enqueue_simple_download(chat_id: int, url: str) -> int:
    """Enqueue a download job with ``simple_only=True``. Returns job id."""
    from .jobs import get_default_queue

    queue = get_default_queue()
    return await queue.enqueue(
        "download",
        {"url": url, "simple_only": True},
        chat_id=chat_id,
    )


async def _enqueue_full_pipeline_download(chat_id: int, url: str) -> int:
    """Enqueue a normal /dl-flavoured download (chains to analyze/edit/...)."""
    from .jobs import get_default_queue

    queue = get_default_queue()
    return await queue.enqueue(
        "download",
        {"url": url},
        chat_id=chat_id,
    )


@wizard_router.message(StateFilter(None), F.text.regexp(_DOWNLOAD_PHRASE_RE))
async def on_download_intent(message: Message) -> None:
    """Intercept «скачай <video URL>» before the LLM agent sees it.

    Three paths depending on saved choice in ``addons.downloader.choice``:

    1. **Unset** — present a 2-button chooser («/dl полный конвейер» /
       «bash2mp4 простое скачивание»). The user's URL is stashed so the
       callback handler can act on it.
    2. **«dl»** — kick the existing 5-stage pipeline immediately
       (download → analyze → edit → seo → publish) via the standard
       ``/dl`` enqueue path.
    3. **«bash2mp4»** — enqueue a *simple-only* download. ``DownloaderWorker``
       sees ``simple_only=True`` and sends the resulting file back with a
       «🎞 Монтажёр» button instead of auto-chaining.

    GitHub / GitLab / Bitbucket URLs are intentionally NOT intercepted —
    they fall through to the agent which has its own ``clone_repo`` tool.
    """
    if not message.text:
        return
    if not can_use(message.from_user.id if message.from_user else None):
        return
    match = _DOWNLOAD_PHRASE_RE.match(message.text)
    if not match:
        return
    url = match.group(1)
    if not _looks_like_video_url(url):
        # Fall through to the default agent router — it knows how to
        # clone github repos.
        return

    await _dispatch_download(message, url)


async def _dispatch_download(message: Message, url: str) -> None:
    """Shared routing for an incoming video URL (от «скачай …» или голой ссылки).

    Honours the saved ``addons.downloader.choice``:
    - ``dl``       → full pipeline immediately
    - ``bash2mp4`` → simple download immediately
    - unset        → show the /dl vs bash2mp4 chooser and stash the URL
    """
    chat_id = message.chat.id
    choice = _downloader_choice()

    if choice == "dl":
        await _enqueue_full_pipeline_download(chat_id, url)
        await message.answer(
            f"🎬 <b>Принято в полный конвейер</b>\n\n"
            f"<a href=\"{url}\">ссылка</a> → анализ → нарезка → SEO → "
            f"публикация.\n\n"
            f"Прогресс — команда <code>/jobs</code>. Когда готово — пришлю файл."
        )
        return

    if choice == "bash2mp4":
        await _enqueue_simple_download(chat_id, url)
        await message.answer(
            f"⬇️ <b>Скачиваю</b>\n\n"
            f"<a href=\"{url}\">ссылка</a> → пришлю файл + кнопку <b>🎞 Монтажёр</b>.\n\n"
            f"Минута-две на yt-dlp; если будет ошибка — расскажу здесь же."
        )
        return

    # First-time: ask which path to use.
    _store_pending_download(chat_id, url)
    await message.answer(
        "<b>Чем качать?</b>\n\n"
        "🎬 <b>/dl</b> — полный конвейер: "
        "<i>скачивание → нарезка → SEO → готовый клип</i>\n\n"
        "⬇️ <b>bash2mp4</b> — просто скачать файл, потом можно вручную нажать "
        "<b>🎞 Монтажёр</b>\n\n"
        "Выбор запомню, в следующий раз спрашивать не буду — "
        "сменить можно в ⚙️ Настройки.",
        reply_markup=_kb_downloader_choice(),
    )


# A message that is *nothing but* an http(s) URL. Unlike the «скачай …»
# phrase matcher this fires on a bare pasted link — but only for video-ish
# URLs (see ``_bare_video_url_filter``); GitHub/GitLab links are left for
# the agent's ``clone_repo`` tool.
_BARE_URL_RE = re.compile(r"^\s*(https?://\S+)\s*$", re.IGNORECASE)


def _bare_video_url_filter(message: Message) -> bool:
    """aiogram filter: True iff the whole message is a bare video URL.

    Returning False (e.g. for a GitHub link, or any text that isn't a
    standalone URL) lets the update propagate to the agent router, so
    repo links still clone and normal prose still reaches the LLM.
    """
    if not message.text:
        return False
    m = _BARE_URL_RE.match(message.text)
    if not m:
        return False
    return _looks_like_video_url(m.group(1))


@wizard_router.message(StateFilter(None), _bare_video_url_filter)
async def on_bare_url(message: Message) -> None:
    """Intercept a bare pasted video URL → same chooser/route as «скачай …»."""
    if not can_use(message.from_user.id if message.from_user else None):
        return
    m = _BARE_URL_RE.match(message.text or "")
    if not m:
        return
    await _dispatch_download(message, m.group(1))


@wizard_router.callback_query(F.data.startswith("dl:choose:"))
async def cb_dl_choose(query: CallbackQuery) -> None:
    """Handle the chooser buttons posted by ``on_download_intent``."""
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    choice = (query.data or "").split(":", 2)[-1]
    if choice not in ("dl", "bash2mp4", "cancel"):
        await query.answer("Неизвестный выбор", show_alert=True)
        return

    chat_id = query.message.chat.id if query.message else None
    if chat_id is None:
        await query.answer("Нет чата", show_alert=True)
        return

    url = _pop_pending_download(chat_id)

    if choice == "cancel":
        await query.answer("Отменено.")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text("✖️ Скачивание отменено.")
        return

    if not url:
        await query.answer("Ссылка не найдена — повтори «скачай <URL>».", show_alert=True)
        return

    _set_downloader_choice(choice)
    await query.answer(f"Запомнил: {choice}")

    if choice == "dl":
        await _enqueue_full_pipeline_download(chat_id, url)
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    f"🎬 <b>Принято в полный конвейер</b>\n\n"
                    f"<a href=\"{url}\">ссылка</a> → анализ → нарезка → "
                    f"SEO → публикация.\n\n"
                    f"Прогресс — команда <code>/jobs</code>."
                )
        return

    # bash2mp4
    await _enqueue_simple_download(chat_id, url)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"⬇️ <b>Скачиваю</b>\n\n"
                f"<a href=\"{url}\">ссылка</a> → пришлю файл + кнопку "
                f"<b>🎞 Монтажёр</b>.\n\n"
                f"Минута-две; если будет ошибка — расскажу здесь же."
            )


@wizard_router.callback_query(F.data.startswith("dl:edit:"))
async def cb_dl_edit(query: CallbackQuery) -> None:
    """«🎞 Монтажёр» button on the «видео скачано» message.

    Reads the original download job's result, then enqueues an
    ``analyze`` job pointing at the same ``source_path``. Analyze
    chains to edit → seo → publish via the normal worker pipeline, so
    the user gets the full edit treatment without re-downloading.
    """
    if not can_use(query.from_user.id if query.from_user else None):
        await query.answer("Нет доступа.", show_alert=True)
        return
    try:
        job_id = int((query.data or "").split(":")[-1])
    except (TypeError, ValueError):
        await query.answer("Битый ID джобы", show_alert=True)
        return

    from .jobs import get_default_queue

    queue = get_default_queue()
    job = await queue.get(job_id)
    if job is None:
        await query.answer("Джоба не найдена", show_alert=True)
        return
    if not job.result or not job.result.get("source_path"):
        await query.answer("У этой джобы нет файла", show_alert=True)
        return

    chat_id = query.message.chat.id if query.message else job.chat_id
    if chat_id is None:
        await query.answer("Нет чата", show_alert=True)
        return

    # Enqueue analyze with the existing source file — the pipeline
    # picks up from stage 2 and runs through edit / seo / publish.
    await queue.enqueue(
        "analyze",
        {
            "source_path": job.result["source_path"],
            "duration_s": job.result.get("duration_s") or 0.0,
            "language": job.result.get("language"),
        },
        chat_id=chat_id,
        parent_id=job.id,
    )
    await query.answer("Запускаю монтажёр…")
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.reply(
                "🎞 <b>Монтажёр запущен</b>\n\n"
                "<i>анализ → нарезка → SEO → публикация</i>\n\n"
                "Когда нарезки будут готовы — пришлю их сюда же. "
                "Прогресс — команда <code>/jobs</code>."
            )


def _bash2mp4_help_text() -> str:
    """Help message printed when the user clicks «⬇️ Скачать (bash2mp4)».

    Reused both after a fresh install and on subsequent clicks (when
    the install has already happened). Walks the user through the
    natural-language flow they asked for: «скачай <URL>» → bot picks
    bash2mp4 (or asks the first time) → file arrives with a «Монтажёр»
    button attached.
    """
    return (
        "<b>⬇️ bash2mp4 — как пользоваться</b>\n\n"
        "Просто пиши в чате <code>скачай</code> + ссылка. Бот сам поймёт "
        "что хочешь скачать видео и пришлёт файл с кнопкой "
        "<b>🎞 Монтажёр</b>.\n\n"
        "<b>Примеры:</b>\n"
        "• <code>скачай https://www.instagram.com/reel/DY7M7TDM-Lv</code>\n"
        "• <code>скачай https://www.youtube.com/shorts/2TEyTiQpYHU</code>\n"
        "• <code>скачай https://www.tiktok.com/@user/video/12345</code>\n\n"
        "<b>Что произойдёт дальше:</b>\n"
        "1. В первый раз бот спросит чем качать: "
        "<b>🎬 /dl</b> (полный конвейер с нарезкой) "
        "или <b>⬇️ bash2mp4</b> (просто скачать). Выбор запомнит.\n"
        "2. Когда скачает — пришлёт видео-файл прямо в чат.\n"
        "3. Под видео будет кнопка <b>🎞 Монтажёр</b> — "
        "нажмёшь → пайплайн нарезки и SEO запустится на этом же файле."
    )


# ---- 📝 Markitdown — settings screen + commands -------------------------
#
# Markitdown converts URLs (YouTube, Vimeo, PDFs, docx, …) to markdown.
# Lives under data/projects/markitdown/ once installed. The bot exposes
# it through two natural-language commands ("дай данные <URL>" and
# "посмотри и расскажи о чём там <URL>") and a settings screen reached
# from the main menu.

_MARKITDOWN_REPO = "https://github.com/microsoft/markitdown"


def _markitdown_installed() -> bool:
    """True iff ``data/projects/markitdown/`` exists with a pyproject."""
    from .config import DATA_DIR

    p = DATA_DIR / "projects" / "markitdown"
    # The repo has a pyproject inside packages/markitdown/ — that's the
    # marker we look for so a bare empty dir doesn't fool us.
    return (p / "packages" / "markitdown" / "pyproject.toml").is_file()


def _markitdown_auto_enabled() -> bool:
    """User toggle: when ON, «дай данные <URL>» runs markitdown directly.

    When OFF the commands still work, but the bot prefixes its reply
    with a hint that the feature is off and asks the user to enable it.
    Kept as a separate setting (rather than just relying on install
    state) because some users may want to keep the repo cloned but
    have the auto-handler quiet while debugging.
    """
    from .addons import state as _addon_state

    return bool(_addon_state.get("markitdown", "auto", False))


def _set_markitdown_auto(enabled: bool) -> None:
    from .addons import state as _addon_state

    _addon_state.set_("markitdown", "auto", bool(enabled))


def _kb_markitdown_menu() -> InlineKeyboardMarkup:
    """5-button screen the user asked for: on, off, install, uninstall, back."""
    installed = _markitdown_installed()
    auto_on = _markitdown_auto_enabled()
    auto_marker_on = "▶ " if auto_on else ""
    auto_marker_off = "" if auto_on else "▶ "
    install_label = (
        "✅ Установлен" if installed else "⬇️ Скачать с github"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"{auto_marker_on}✅ Авто-включение",
                callback_data="md:auto:on",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{auto_marker_off}⛔ Авто-выключение",
                callback_data="md:auto:off",
            )
        ],
        [
            InlineKeyboardButton(
                text=install_label,
                callback_data="md:install",
            )
        ],
    ]
    if installed:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить", callback_data="md:uninstall"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ Назад", callback_data="md:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _markitdown_screen_text() -> str:
    """Human-readable status + usage examples shown above the keyboard.

    Two free-text commands are advertised: «дай данные <URL>» dumps the
    raw markdown back to chat (as a file when long), «посмотри и
    расскажи о чём там <URL>» feeds the markdown into the LLM and
    summarises. Both rely on having markitdown installed AND auto-mode
    enabled — the status block at the top makes both states visible.
    """
    installed = _markitdown_installed()
    auto = _markitdown_auto_enabled()
    status_line = (
        f"Установка: {'✅ есть' if installed else '❌ нет'}    "
        f"Авто: {'✅ вкл' if auto else '⛔ выкл'}"
    )
    return (
        "<b>📝 Markitdown</b>\n\n"
        f"{status_line}\n\n"
        "<b>Команды</b> (когда установлено + авто-включено):\n\n"
        "• <code>дай данные https://www.youtube.com/shorts/XXX</code>\n"
        "  → бот шлёт сам markdown-файл с расшифровкой\n\n"
        "• <code>посмотри и расскажи о чём там https://www.youtube.com/shorts/XXX</code>\n"
        "  → бот сам прочитает markdown и расскажет своими словами\n\n"
        f"<i>Репозиторий: <a href=\"{_MARKITDOWN_REPO}\">microsoft/markitdown</a></i>"
    )


async def _show_markitdown_screen(query: CallbackQuery) -> None:
    """Render the markitdown settings screen on the user's chat."""
    text = _markitdown_screen_text()
    kb = _kb_markitdown_menu()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                text, reply_markup=kb, disable_web_page_preview=True
            )


@wizard_router.message(Command("markitdown"))
async def cmd_markitdown(message: Message) -> None:
    """Shortcut command to open the markitdown screen without the menu."""
    if not can_use(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        _markitdown_screen_text(),
        reply_markup=_kb_markitdown_menu(),
        disable_web_page_preview=True,
    )


@wizard_router.callback_query(F.data.startswith("md:"))
async def cb_markitdown(query: CallbackQuery) -> None:
    """All «md:*» callbacks: auto toggle, install / uninstall, back.

    ``md:auto:on|off``   — flip the auto-handler flag
    ``md:install``       — clone microsoft/markitdown into data/projects
    ``md:uninstall``     — remove the cloned project dir
    ``md:back``          — return to the main menu
    """
    uid = query.from_user.id if query.from_user else None
    if not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    action = (query.data or "").split(":", 1)[1]

    if action == "back":
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "<b>Главное меню</b>",
                    reply_markup=_kb_main_after_claim(uid),
                )
        return

    if action == "auto:on":
        _set_markitdown_auto(True)
        await query.answer("Авто-режим включён.")
        await _show_markitdown_screen(query)
        return

    if action == "auto:off":
        _set_markitdown_auto(False)
        await query.answer("Авто-режим выключен.")
        await _show_markitdown_screen(query)
        return

    if action == "install":
        if _markitdown_installed():
            await query.answer("Уже установлен.")
            await _show_markitdown_screen(query)
            return
        await query.answer("Клонирую…")
        if query.message is not None:
            from .handlers import run_work_batch

            await run_work_batch(
                query.message,
                [f"/clone {_MARKITDOWN_REPO}"],
                title="📝 Markitdown — install",
                user_id=uid,
            )
            await query.message.answer(
                "Готово. Авто-режим можно включить кнопкой "
                "«✅ Авто-включение»."
            )
        return

    if action == "uninstall":
        from .config import DATA_DIR

        target = DATA_DIR / "projects" / "markitdown"
        if not target.is_dir():
            await query.answer("Уже удалён.")
            await _show_markitdown_screen(query)
            return
        await query.answer("Удаляю…")
        import shutil

        try:
            shutil.rmtree(target)
        except OSError as exc:
            logger.exception("markitdown uninstall failed")
            if query.message is not None:
                await query.message.answer(
                    f"Ошибка удаления: <code>{exc}</code>"
                )
            return
        if query.message is not None:
            await query.message.answer("🗑 Markitdown удалён.")
        await _show_markitdown_screen(query)
        return

    await query.answer("Неизвестное действие", show_alert=True)


# Pattern matchers for the two natural-language markitdown commands.
# Match the verb-phrase + a URL anywhere on the rest of the line so the
# user can pad with «пожалуйста / kindly / а потом …» without breaking
# routing. Both are routed only when ``addons.markitdown.auto`` is true
# (the user explicitly asked for an auto on/off switch).
_MD_DUMP_RE = re.compile(
    r"^\s*дай\s+данные\s+(https?://\S+)", re.IGNORECASE
)
_MD_SUMMARY_RE = re.compile(
    r"^\s*посмотри\s+и\s+расскажи(?:\s+о\s+чём\s+там)?\s+(https?://\S+)",
    re.IGNORECASE,
)


async def _run_markitdown(url: str) -> tuple[bool, str]:
    """Invoke markitdown on ``url`` and return ``(ok, body_or_error)``.

    Uses subprocess so a broken page can't crash the bot loop. Looks
    for the ``markitdown`` console script in the active venv first,
    falls back to ``python -m markitdown``. If neither is available
    the helper returns ``(False, instructions)`` so the caller can
    forward that text to the user.
    """
    import asyncio
    import shutil
    import sys

    md_bin = shutil.which("markitdown")
    if md_bin:
        cmd = [md_bin, url]
    else:
        cmd = [sys.executable, "-m", "markitdown", url]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=120
        )
    except asyncio.TimeoutError:
        return (False, "markitdown timed out after 120s")
    except FileNotFoundError:
        return (
            False,
            "markitdown не установлен. Запусти "
            "<code>pip install ./data/projects/markitdown/packages/markitdown[all]</code> "
            "в окружении бота.",
        )

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "replace").strip()
        if "No module named" in err and "markitdown" in err:
            return (
                False,
                "Модуль markitdown отсутствует в окружении бота. "
                "Включи кнопку «⬇️ Скачать с github» и установи через "
                "<code>pip install -e ./data/projects/markitdown/packages/markitdown[all]</code>.",
            )
        return (False, f"markitdown упал: <code>{err[:500]}</code>")

    body = (stdout or b"").decode("utf-8", "replace").strip()
    if not body:
        return (False, "markitdown вернул пустой ответ.")
    return (True, body)


@wizard_router.message(StateFilter(None), F.text.regexp(_MD_DUMP_RE))
async def on_markitdown_dump(message: Message) -> None:
    """«дай данные <URL>» → send the raw markdown back as a file."""
    if not message.text:
        return
    if not can_use(message.from_user.id if message.from_user else None):
        return
    match = _MD_DUMP_RE.match(message.text)
    if not match:
        return
    url = match.group(1)
    if not _markitdown_auto_enabled():
        await message.answer(
            "<b>📝 Markitdown выключен.</b>\n\n"
            "Открой меню → <b>📝 Markitdown</b> → <b>✅ Авто-включение</b>, "
            "и потом повтори команду."
        )
        return
    if not _markitdown_installed():
        await message.answer(
            "<b>📝 Markitdown не установлен.</b>\n\n"
            "Открой меню → <b>📝 Markitdown</b> → <b>⬇️ Скачать с github</b>."
        )
        return

    await message.answer(f"📝 Получаю данные с <code>{url}</code>…")
    ok, body = await _run_markitdown(url)
    if not ok:
        await message.answer(f"❌ {body}")
        return
    await _send_markdown_dump(message, url, body)


@wizard_router.message(StateFilter(None), F.text.regexp(_MD_SUMMARY_RE))
async def on_markitdown_summary(message: Message) -> None:
    """«посмотри и расскажи о чём там <URL>» → markdown → LLM summary."""
    if not message.text:
        return
    if not can_use(message.from_user.id if message.from_user else None):
        return
    match = _MD_SUMMARY_RE.match(message.text)
    if not match:
        return
    url = match.group(1)
    if not _markitdown_auto_enabled():
        await message.answer(
            "<b>📝 Markitdown выключен.</b>\n\n"
            "Открой меню → <b>📝 Markitdown</b> → <b>✅ Авто-включение</b>, "
            "и потом повтори команду."
        )
        return
    if not _markitdown_installed():
        await message.answer(
            "<b>📝 Markitdown не установлен.</b>\n\n"
            "Открой меню → <b>📝 Markitdown</b> → <b>⬇️ Скачать с github</b>."
        )
        return

    await message.answer(f"🔍 Смотрю <code>{url}</code>…")
    ok, body = await _run_markitdown(url)
    if not ok:
        await message.answer(f"❌ {body}")
        return

    try:
        from .agent import oneshot_summary

        prompt = (
            "Кратко и по делу расскажи о чём этот материал. "
            "Раздели абзацы пустыми строками. Сохраняй URL-ы как "
            "кликабельные ссылки (HTML <a href>).\n\n"
            f"--- материал начало ---\n{body[:30000]}\n--- материал конец ---"
        )
        summary = await oneshot_summary(prompt, purpose="markitdown_summary")
    except Exception:  # noqa: BLE001
        logger.exception("markitdown summary LLM call failed")
        await message.answer(
            "Не смог получить саммари от LLM. Сырой markdown — пришлю файлом."
        )
        await _send_markdown_dump(message, url, body)
        return

    if not summary:
        await message.answer(
            "LLM вернул пустой ответ. Сырой markdown — файлом ниже."
        )
        await _send_markdown_dump(message, url, body)
        return

    await message.answer(summary, disable_web_page_preview=False)


async def _send_markdown_dump(
    message: Message, url: str, body: str
) -> None:
    """Pretty-print a markdown blob: inline if short, file if long.

    Telegram caps message bodies at 4096 chars. Anything bigger gets
    written to a tempfile and sent as a document attachment so the
    user has the full markdown they can grep through locally.
    """
    if len(body) <= 3500:
        await message.answer(
            f"<b>📝 Markdown</b>\n\n<pre>{_html_escape(body)}</pre>",
            disable_web_page_preview=True,
        )
        return

    import tempfile

    from aiogram.types import FSInputFile

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".md", delete=False
    ) as fh:
        fh.write(f"# {url}\n\n")
        fh.write(body)
        path = fh.name
    try:
        await message.answer_document(
            FSInputFile(path, filename="markitdown.md"),
            caption=f"📝 markdown для <code>{url}</code>",
        )
    finally:
        os.unlink(path)


# ---- Mutually-exclusive downloader picker -------------------------------
#
# Each downloader («📥 /dl полный конвейер» and «⬇️ bash2mp4 простой») has
# its own screen with a «✅ Использовать» button. Pressing «Использовать»
# on one auto-disables the other because both write to the SAME setting
# (``addons.downloader.choice``) — there are only two valid values, so
# flipping one inherently turns the other off. That's the «одно
# отключает другого» behaviour the user asked for.

def _bash2mp4_installed() -> bool:
    """True iff ``data/projects/bash2mp4/`` exists with a setup.sh.

    Mirror of ``_markitdown_installed()`` — separate helper because the
    marker file is different (bash2mp4 has setup.sh, not pyproject).
    """
    from .config import DATA_DIR

    p = DATA_DIR / "projects" / "bash2mp4"
    return p.is_dir() and (
        (p / "setup.sh").is_file() or any(p.glob("*.sh"))
    )


def _yt_cookies_path() -> Path:
    """Filesystem location for user-uploaded YouTube cookies.txt.

    The downloader auto-picks up this file (via
    ``YT_COOKIES_DEFAULT_PATH`` in :mod:`bot.config`), so saving here
    is the only step required to flip yt-dlp into authenticated mode.
    """
    from .config import DATA_DIR

    return DATA_DIR / "yt_cookies.txt"


def _yt_cookies_present() -> bool:
    """True iff a cookies.txt has been uploaded by the user."""
    p = _yt_cookies_path()
    return p.is_file() and p.stat().st_size > 0


def _kb_dl_screen() -> InlineKeyboardMarkup:
    """Inline keyboard for the «📥 Скачать видео» (/dl) settings screen."""
    active = _downloader_choice() == "dl"
    has_cookies = _yt_cookies_present()
    rows: list[list[InlineKeyboardButton]] = []
    if active:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Активный режим",
                    callback_data="dl:noop",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Использовать /dl",
                    callback_data="dl:use:dl",
                )
            ]
        )
    # «🍪 Загрузить cookies» — opens upload flow. The button label
    # mirrors current state: «загрузить» if missing, «обновить» if
    # already present.
    if has_cookies:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🍪 Cookies загружены (обновить)",
                    callback_data="dl:cookies:upload",
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить cookies",
                    callback_data="dl:cookies:delete",
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🍪 Загрузить cookies (для YouTube)",
                    callback_data="dl:cookies:upload",
                )
            ]
        )
    # «🔄 Убрать выбор» — only meaningful when a choice is currently
    # pinned. Clearing it returns the bot to «ask me each time»: the next
    # «скачай <URL>» re-shows the /dl vs bash2mp4 chooser.
    if _downloader_choice() is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Убрать выбор",
                    callback_data="dl:clear:dl",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ Назад", callback_data="dl:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_bash2mp4_screen() -> InlineKeyboardMarkup:
    """Inline keyboard for the «⬇️ Скачать (bash2mp4)» settings screen."""
    installed = _bash2mp4_installed()
    active = _downloader_choice() == "bash2mp4"
    rows: list[list[InlineKeyboardButton]] = []
    if not installed:
        # Shouldn't normally be shown — install button on main menu
        # opens the cloning flow. Just in case render a re-install hint.
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬇️ Установить bash2mp4",
                    callback_data="main:install:downloader",
                )
            ]
        )
    else:
        if active:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Активный режим",
                        callback_data="dl:noop",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Использовать bash2mp4",
                        callback_data="dl:use:bash2mp4",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить bash2mp4",
                    callback_data="dl:remove:bash2mp4",
                )
            ]
        )
    if _downloader_choice() is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Убрать выбор",
                    callback_data="dl:clear:bash2mp4",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ Назад", callback_data="dl:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _dl_screen_text() -> str:
    """Status + usage for the /dl screen."""
    active = _downloader_choice() == "dl"
    status_line = (
        "✅ <b>Активен</b>" if active else "⛔ <b>Неактивен</b>"
    )
    cookies_line = (
        "🍪 <b>cookies.txt</b>: загружен" if _yt_cookies_present()
        else "🍪 <b>cookies.txt</b>: не загружен"
    )
    return (
        "<b>📥 Скачать видео — /dl (yt-dlp пайплайн)</b>\n\n"
        f"Статус: {status_line}\n"
        f"{cookies_line}\n\n"
        "Полный конвейер: <i>скачивание → анализ → нарезка → SEO → "
        "публикация</i>. Поддерживает YouTube, Instagram, TikTok, "
        "Vimeo, прямые .mp4 — всё что глотает yt-dlp.\n\n"
        "<b>Как пользоваться:</b>\n"
        "• Команда: <code>/dl &lt;url&gt;</code>\n"
        "• Просто текстом: <code>скачай &lt;url&gt;</code> "
        "(подхватит активный режим)\n\n"
        "<b>Если YouTube ругается «Sign in to confirm you're not a bot»</b> "
        "— нажми <b>🍪 Загрузить cookies</b> и пришли файл "
        "<code>cookies.txt</code>, экспортированный из браузера "
        "(расширение «Get cookies.txt LOCALLY» в Chrome / Edge).\n\n"
        "Кнопка <b>✅ Использовать</b> делает этот режим активным "
        "(автоматически отключает bash2mp4)."
    )


def _bash2mp4_screen_text() -> str:
    """Status + usage for the bash2mp4 screen."""
    installed = _bash2mp4_installed()
    active = _downloader_choice() == "bash2mp4"
    install_line = (
        "✅ <b>Установлен</b>" if installed else "❌ <b>Не установлен</b>"
    )
    active_line = (
        "✅ <b>Активен</b>" if active else "⛔ <b>Неактивен</b>"
    )
    return (
        "<b>⬇️ Скачать видео — bash2mp4 (простой режим)</b>\n\n"
        f"Установка: {install_line}    Статус: {active_line}\n\n"
        "Простое скачивание: <i>файл сразу приходит в чат + кнопка "
        "«🎞 Монтажёр»</i>. Без авто-нарезки, без SEO, без "
        "публикации — только сырой файл.\n\n"
        "<b>Как пользоваться:</b>\n"
        "• Просто текстом: <code>скачай &lt;url&gt;</code>\n"
        "• Когда придёт файл, нажми <b>🎞 Монтажёр</b> чтобы "
        "запустить пайплайн нарезки на этом же файле.\n\n"
        "Кнопка <b>✅ Использовать</b> делает этот режим активным "
        "(автоматически отключает /dl)."
    )


async def _show_download_screen(query: CallbackQuery) -> None:
    """Render the /dl settings screen (status + Use button + back)."""
    if query.message is None:
        return
    with contextlib.suppress(Exception):
        await query.message.edit_text(
            _dl_screen_text(),
            reply_markup=_kb_dl_screen(),
            disable_web_page_preview=True,
        )


async def _show_bash2mp4_screen(query: CallbackQuery) -> None:
    """Render the bash2mp4 settings screen."""
    if query.message is None:
        return
    # ``edit_text`` fails if the previous message wasn't a text one
    # (e.g. it was the install log streamed via run_work_batch). Fall
    # back to ``answer`` in that case so the user always sees the
    # screen.
    try:
        await query.message.edit_text(
            _bash2mp4_screen_text(),
            reply_markup=_kb_bash2mp4_screen(),
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        await query.message.answer(
            _bash2mp4_screen_text(),
            reply_markup=_kb_bash2mp4_screen(),
            disable_web_page_preview=True,
        )


@wizard_router.callback_query(F.data == "dl:back")
async def cb_dl_back(query: CallbackQuery) -> None:
    """«↩️ Назад» on a downloader screen → return to the main menu."""
    uid = query.from_user.id if query.from_user else None
    await query.answer()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>Главное меню</b>",
                reply_markup=_kb_main_after_claim(uid),
            )


@wizard_router.callback_query(F.data == "dl:noop")
async def cb_dl_noop(query: CallbackQuery) -> None:
    """The «✅ Активный режим» pseudo-button — just acknowledge."""
    await query.answer("Этот режим уже активен.")


@wizard_router.callback_query(F.data.startswith("dl:use:"))
async def cb_dl_use(query: CallbackQuery) -> None:
    """«✅ Использовать <name>» — switch the active downloader.

    Both screens share the same backing key
    (``addons.downloader.choice``), so activating one inherently
    deactivates the other. Refreshes the same screen so the user sees
    the «✅ Активен» indicator turn green.
    """
    uid = query.from_user.id if query.from_user else None
    if not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return

    choice = (query.data or "").split(":", 2)[-1]
    if choice not in ("dl", "bash2mp4"):
        await query.answer("Неизвестный режим", show_alert=True)
        return

    # bash2mp4 needs to be installed before it can be «used» — otherwise
    # we'd lock the user into a dead-end where «скачай <URL>» tries
    # bash2mp4 and silently fails.
    if choice == "bash2mp4" and not _bash2mp4_installed():
        await query.answer(
            "bash2mp4 не установлен. Нажми «⬇️ Скачать (bash2mp4)» в "
            "главном меню чтобы по��тавить.",
            show_alert=True,
        )
        return

    _set_downloader_choice(choice)
    await query.answer(
        "Активный режим: /dl" if choice == "dl" else "Активный режим: bash2mp4"
    )
    # Refresh the screen the user clicked on.
    if choice == "dl":
        await _show_download_screen(query)
    else:
        await _show_bash2mp4_screen(query)


@wizard_router.callback_query(F.data.startswith("dl:clear:"))
async def cb_dl_clear(query: CallbackQuery) -> None:
    """«🔄 Убрать выбор» — forget the pinned downloader choice.

    After this the bot is back in «ask me each time» mode: the next
    «скачай <URL>» re-shows the /dl vs bash2mp4 chooser instead of
    silently using a remembered preference. ``dl:clear:<screen>`` carries
    which screen to re-render afterwards so the «🔄 Убрать выбор» row
    disappears once the choice is gone.
    """
    uid = query.from_user.id if query.from_user else None
    if not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return

    screen = (query.data or "").split(":", 2)[-1]

    from .addons import state as _addon_state

    if _downloader_choice() is None:
        await query.answer("Выбор уже сброшен.")
    else:
        _addon_state.delete("downloader", "choice")
        await query.answer("Выбор сброшен — в следующий раз спрошу заново.")

    if screen == "dl":
        await _show_download_screen(query)
    else:
        await _show_bash2mp4_screen(query)


@wizard_router.callback_query(F.data == "dl:remove:bash2mp4")
async def cb_dl_remove_bash2mp4(query: CallbackQuery) -> None:
    """«🗑 Удалить bash2mp4» — wipe the cloned project dir.

    Also clears the active choice if it pointed at bash2mp4, so the
    user doesn't end up with «скачай <URL>» pointing at a deleted
    binary. The /dl screen survives — yt-dlp lives inside the bot's
    venv, not in data/projects/.
    """
    uid = query.from_user.id if query.from_user else None
    if not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return

    from .config import DATA_DIR

    target = DATA_DIR / "projects" / "bash2mp4"
    if not target.is_dir():
        await query.answer("Уже удалён.")
        await _show_bash2mp4_screen(query)
        return

    import shutil

    try:
        shutil.rmtree(target)
    except OSError as exc:
        logger.exception("bash2mp4 uninstall failed")
        await query.answer(f"Ошибка удаления: {exc}", show_alert=True)
        return

    # If bash2mp4 was active — fall back to «unset», so the next
    # «скачай <URL>» prompts the user again instead of running a
    # non-existent binary.
    if _downloader_choice() == "bash2mp4":
        from .addons import state as _addon_state

        _addon_state.delete("downloader", "choice")

    await query.answer("🗑 bash2mp4 удалён.")
    await _show_bash2mp4_screen(query)


# ---- YouTube cookies upload ---------------------------------------------
#
# YouTube periodically tightens its anti-bot gate; when that happens
# yt-dlp throws «Sign in to confirm you're not a bot». The only
# reliable workaround is to feed it the cookies from an authenticated
# browser session. We expose this as a button on the /dl screen so
# the owner can paste a freshly-exported cookies.txt without touching
# the filesystem.

@wizard_router.callback_query(F.data == "dl:cookies:upload")
async def cb_dl_cookies_upload(query: CallbackQuery, state: FSMContext) -> None:
    """«🍪 Загрузить cookies» — switch to the «waiting for file» mode."""
    uid = query.from_user.id if query.from_user else None
    if not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(SetupStates.awaiting_yt_cookies_file)
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            "🍪 <b>Жду cookies.txt</b>\n\n"
            "Экспортируй cookies из своего браузера в формате Netscape "
            "и пришли файл сюда документом.\n\n"
            "<b>Как экспортировать:</b>\n"
            "1. Поставь расширение <b>«Get cookies.txt LOCALLY»</b> "
            "(<a href=\"https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc\">Chrome</a> / Edge)\n"
            "2. Открой <a href=\"https://www.youtube.com/\">youtube.com</a>, "
            "залогинься\n"
            "3. Нажми на иконку расширения → <b>Export</b> → сохрани файл\n"
            "4. Пришли этот файл сюда\n\n"
            "Отмена — /cancel",
            disable_web_page_preview=True,
        )


@wizard_router.callback_query(F.data == "dl:cookies:delete")
async def cb_dl_cookies_delete(query: CallbackQuery) -> None:
    """«🗑 Удалить cookies» — wipe the saved cookies file."""
    uid = query.from_user.id if query.from_user else None
    if not can_admin(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    p = _yt_cookies_path()
    if p.exists():
        try:
            p.unlink()
        except OSError as exc:
            logger.exception("yt cookies delete failed")
            await query.answer(f"Ошибка: {exc}", show_alert=True)
            return
    await query.answer("🗑 cookies.txt удалён.")
    await _show_download_screen(query)


@wizard_router.message(
    StateFilter(SetupStates.awaiting_yt_cookies_file), F.document
)
async def capture_yt_cookies(message: Message, state: FSMContext) -> None:
    """Capture an uploaded cookies.txt and save it to ``data/yt_cookies.txt``."""
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    if message.document is None:
        return

    name = (message.document.file_name or "").lower()
    # Be permissive on extensions — some exports name the file
    # ``cookies`` without ``.txt``. Accept anything with ``cookie`` in
    # the name or a plain ``.txt`` extension.
    if not (
        "cookie" in name
        or name.endswith(".txt")
    ):
        await message.answer(
            "Жду текстовый файл с cookies (имя содержит «cookie» или "
            "расширение .txt). Отмена — /cancel"
        )
        return

    out_path = _yt_cookies_path()
    try:
        await message.bot.download(
            message.document.file_id, destination=str(out_path)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("yt cookies download failed")
        await message.answer(
            f"❌ Не получилось сохранить файл: <code>{_html_escape(str(exc))}</code>"
        )
        return

    # Sanity check: must look like Netscape cookies.txt (first non-comment
    # line should be a tab-separated record with at least 6 fields).
    try:
        sample = out_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        await message.answer(
            f"❌ Не смог прочитать сохранённый файл: <code>{_html_escape(str(exc))}</code>"
        )
        return

    looks_valid = False
    for line in sample.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if len(line.split("\t")) >= 6:
            looks_valid = True
            break

    if not looks_valid:
        out_path.unlink(missing_ok=True)
        await message.answer(
            "❌ Файл не похож на Netscape cookies.txt — внутри нет ни одной "
            "строки с табуляциями (6+ полей). Экспортируй через «Get "
            "cookies.txt LOCALLY», и пришли заново."
        )
        return

    # Count cookie entries for the confirmation message.
    cookie_count = sum(
        1
        for line in sample.splitlines()
        if line.strip() and not line.startswith("#") and len(line.split("\t")) >= 6
    )

    await state.clear()
    await message.answer(
        f"✅ <b>cookies.txt сохранён.</b>\n\n"
        f"Записей: <code>{cookie_count}</code>\n"
        f"Путь: <code>{out_path}</code>\n\n"
        "Следующая команда <code>скачай &lt;youtube-url&gt;</code> "
        "пойдёт уже с авторизованной сессией."
    )


@wizard_router.message(
    StateFilter(SetupStates.awaiting_yt_cookies_file), _NOT_A_COMMAND
)
async def capture_yt_cookies_wrong_type(
    message: Message, state: FSMContext
) -> None:
    """Prompt again if the user sent text/photo/voice instead of a document."""
    if not _is_owner(message.from_user.id if message.from_user else 0):
        return
    await message.answer(
        "Жду <b>документ</b> с cookies (файл .txt). /cancel — отмена."
    )


# ---- 🗑 Скинуть весь мусор — one-button garbage bin -----------------------
#
# The user opens the bin (main menu → «🗑 Скинуть весь мусор»), throws in
# anything (links, videos, files, photos, voice, text), then presses
# «📤 Отправить» (read everything + answer, stay open) or «✖️ Закрыть»
# (final answer + back to normal). The reading combines existing bot
# capabilities: markitdown, Groq Whisper/Vision, Firecrawl/Apify, OCR.
# Handlers live on ``wizard_router`` (registered first) so the collecting
# state wins over the Groq overrides and the catch-all agent handler.

_DUMP_URL_RE = re.compile(r"https?://\S+")


def _kb_dump() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Отправить", callback_data="dump:send")],
            [
                InlineKeyboardButton(text="🧹 Очистить", callback_data="dump:clear"),
                InlineKeyboardButton(text="✖️ Закрыть", callback_data="dump:close"),
            ],
        ]
    )


def _dump_screen_text(uid: int | None) -> str:
    from .addons.dump import store

    n = store.count(uid or 0)
    inv = store.summary_lines(uid or 0)
    return (
        "<b>🗑 Скинуть весь мусор</b>\n\n"
        "Кидай сюда что угодно — <b>ссылки, видео, файлы, фото, голосовые, "
        "текст</b>. Я всё складываю в одну кучу и ничего пока не отвечаю.\n\n"
        "• <b>📤 Отправить</b> — прочитаю всё (видео — скриншот/markitdown/"
        "скрейпинг, файлы — в markdown, ссылки — fetch, голос — расшифровка, "
        "фото — распознавание) и отвечу на твой <i>последний вопрос</i>.\n"
        "• <b>✖️ Закрыть</b> — отвечу и вернусь в обычный режим.\n\n"
        f"<b>В корзине: {n}</b>\n{inv}"
    )


async def _dump_refresh(message: Message, uid: int | None) -> None:
    """Send/refresh the bin status under the latest captured item."""
    with contextlib.suppress(Exception):
        await message.answer(
            _dump_screen_text(uid),
            reply_markup=_kb_dump(),
            disable_web_page_preview=True,
        )


def _dump_add_and_ack(uid: int, item: dict) -> int:
    from .addons.dump import store

    return store.add_item(uid, item)


@wizard_router.message(StateFilter(DumpStates.collecting), F.text)
async def dump_capture_text(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    uid = message.from_user.id if message.from_user else 0
    text = (message.text or "").strip()
    if not text:
        return
    from .addons.dump import store

    urls = _DUMP_URL_RE.findall(text)
    for url in urls:
        store.add_item(uid, {"kind": store.KIND_URL, "text": url})
    leftover = _DUMP_URL_RE.sub("", text).strip()
    if leftover:
        store.add_item(uid, {"kind": store.KIND_TEXT, "text": leftover})
    if not urls and not leftover:
        store.add_item(uid, {"kind": store.KIND_TEXT, "text": text})
    n = store.count(uid)
    await message.answer(
        f"➕ Добавил (в корзине: <b>{n}</b>). Кидай ещё или жми 📤 Отправить.",
        reply_markup=_kb_dump(),
    )


@wizard_router.message(StateFilter(DumpStates.collecting), F.document)
async def dump_capture_document(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    uid = message.from_user.id if message.from_user else 0
    from .addons.dump import store

    doc = message.document
    name = doc.file_name or "file"
    suffix = os.path.splitext(name)[1] or ""
    n = _dump_add_and_ack(
        uid,
        {
            "kind": store.KIND_DOCUMENT,
            "file_id": doc.file_id,
            "file_name": name,
            "suffix": suffix,
            "mime": doc.mime_type or "",
            "text": (message.caption or "").strip(),
        },
    )
    await message.answer(
        f"📄 Файл «{_html_escape(name)}» в корзине (всего: <b>{n}</b>).",
        reply_markup=_kb_dump(),
    )


@wizard_router.message(StateFilter(DumpStates.collecting), F.video)
async def dump_capture_video(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    uid = message.from_user.id if message.from_user else 0
    from .addons.dump import store

    vid = message.video
    name = (vid.file_name if getattr(vid, "file_name", None) else None) or "video.mp4"
    n = _dump_add_and_ack(
        uid,
        {
            "kind": store.KIND_VIDEO,
            "file_id": vid.file_id,
            "file_name": name,
            "suffix": os.path.splitext(name)[1] or ".mp4",
            "text": (message.caption or "").strip(),
        },
    )
    await message.answer(
        f"🎬 Видео в корзине (всего: <b>{n}</b>). Прочитаю его скриншотом/"
        "markitdown при отправке.",
        reply_markup=_kb_dump(),
    )


@wizard_router.message(StateFilter(DumpStates.collecting), F.animation)
async def dump_capture_animation(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    uid = message.from_user.id if message.from_user else 0
    from .addons.dump import store

    anim = message.animation
    n = _dump_add_and_ack(
        uid,
        {
            "kind": store.KIND_ANIMATION,
            "file_id": anim.file_id,
            "file_name": getattr(anim, "file_name", None) or "animation.mp4",
            "suffix": ".mp4",
            "text": (message.caption or "").strip(),
        },
    )
    await message.answer(
        f"🎞 GIF/анимация в корзине (всего: <b>{n}</b>).", reply_markup=_kb_dump()
    )


@wizard_router.message(StateFilter(DumpStates.collecting), F.photo)
async def dump_capture_photo(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    uid = message.from_user.id if message.from_user else 0
    from .addons.dump import store

    photo = max(message.photo, key=lambda p: p.width * p.height)
    n = _dump_add_and_ack(
        uid,
        {
            "kind": store.KIND_PHOTO,
            "file_id": photo.file_id,
            "suffix": ".jpg",
            "text": (message.caption or "").strip(),
        },
    )
    await message.answer(
        f"🖼 Фото в корзине (всего: <b>{n}</b>).", reply_markup=_kb_dump()
    )


@wizard_router.message(StateFilter(DumpStates.collecting), F.voice)
async def dump_capture_voice(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    uid = message.from_user.id if message.from_user else 0
    from .addons.dump import store

    n = _dump_add_and_ack(
        uid,
        {
            "kind": store.KIND_VOICE,
            "file_id": message.voice.file_id,
            "suffix": ".ogg",
            "text": (message.caption or "").strip(),
        },
    )
    await message.answer(
        f"🎙 Голосовое в корзине (всего: <b>{n}</b>). Расшифрую при отправке.",
        reply_markup=_kb_dump(),
    )


@wizard_router.message(StateFilter(DumpStates.collecting), F.audio)
async def dump_capture_audio(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    uid = message.from_user.id if message.from_user else 0
    from .addons.dump import store

    aud = message.audio
    name = getattr(aud, "file_name", None) or "audio.mp3"
    n = _dump_add_and_ack(
        uid,
        {
            "kind": store.KIND_AUDIO,
            "file_id": aud.file_id,
            "file_name": name,
            "suffix": os.path.splitext(name)[1] or ".mp3",
            "text": (message.caption or "").strip(),
        },
    )
    await message.answer(
        f"🎵 Аудио в корзине (всего: <b>{n}</b>).", reply_markup=_kb_dump()
    )


async def _dump_deliver(message: Message, ql: str, answer: str) -> None:
    """Deliver the dump answer in the format the user asked for.

    Detects keywords in the question (already lower-cased ``ql``):
    .md / .json / .jpg(картинка) / .zip — otherwise plain text in chat.
    """
    from .handlers import _send_long
    from aiogram.types import BufferedInputFile

    def _wants(*words: str) -> bool:
        return any(w in ql for w in words)

    try:
        if _wants(".json", " json", "джейсон"):
            import json as _json
            blob = _json.dumps({"answer": answer}, ensure_ascii=False, indent=2)
            await message.answer_document(
                BufferedInputFile(blob.encode("utf-8"), filename="dump_answer.json"),
                caption="🧩 Ответ в .json")
            return
        if _wants(".zip", "архив", "zip"):
            import io
            import zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("answer.md", answer)
            buf.seek(0)
            await message.answer_document(
                BufferedInputFile(buf.getvalue(), filename="dump_answer.zip"),
                caption="📦 Ответ в .zip")
            return
        if _wants(".jpg", ".jpeg", "картинк", "изображени", "jpg"):
            img = _text_to_jpg(answer)
            if img is not None:
                await message.answer_document(
                    BufferedInputFile(img, filename="dump_answer.jpg"),
                    caption="🖼 Ответ картинкой")
                return
            # Pillow недоступен → отдадим текстом
        if _wants(".md", " в md", "маркдаун", "markdown"):
            await message.answer_document(
                BufferedInputFile(answer.encode("utf-8"), filename="dump_answer.md"),
                caption="📝 Ответ в .md")
            return
    except Exception:  # noqa: BLE001
        logger.exception("dump: deliver-as-file failed; falling back to text")
    # default: plain text
    await _send_long(message, answer, code=False)


def _text_to_jpg(text: str) -> bytes | None:
    """Render text into a JPG (Pillow). Returns bytes or None if unavailable."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        return None
    import io
    import textwrap

    text = (text or "").strip() or "(пусто)"
    # Wrap to ~90 chars; cap total lines so the image stays sane.
    wrapped: list[str] = []
    for para in text.splitlines() or [""]:
        wrapped.extend(textwrap.wrap(para, width=90) or [""])
    if len(wrapped) > 600:
        wrapped = wrapped[:600] + ["", "…[обрезано для картинки]"]
    try:
        font = ImageFont.truetype("DejaVuSansMono.ttf", 16)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    line_h = 22
    pad = 20
    width = 760
    height = pad * 2 + line_h * max(1, len(wrapped))
    img = Image.new("RGB", (width, height), (17, 23, 32))
    draw = ImageDraw.Draw(img)
    y = pad
    for ln in wrapped:
        draw.text((pad, y), ln, fill=(230, 233, 239), font=font)
        y += line_h
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=85)
    return out.getvalue()


async def _dump_process(message: Message, uid: int) -> None:
    """Read every item in the bin, then answer the user's last question."""
    from .addons.dump import readers, store

    items = store.get_items(uid)
    if not items:
        await message.answer("🗑 Корзина пуста — кинь что-нибудь сначала.")
        return
    question = store.last_question(uid) or (
        "Изучи все приложенные материалы и сделай развёрнутый вывод."
    )
    progress = await message.answer(
        f"⏳ Читаю {len(items)} материал(ов) из корзины…"
    )
    try:
        context = await readers.read_all(message.bot, items)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dump: read_all failed")
        with contextlib.suppress(Exception):
            await progress.edit_text(f"❌ Не смог прочитать корзину: {exc}")
        return

    _ql = (question or "").lower()
    _verbatim = any(w in _ql for w in (
        "дословно", "слово в слово", "по очереди", "цитат", "выдержк", "verbatim"))
    verbatim_note = ""
    if _verbatim:
        verbatim_note = (
            "\n\nВАЖНО: извлекай ДОСЛОВНО, по порядку, без пересказа и "
            "сокращений. Каждый фрагмент нумеруй (1., 2., 3.) в том порядке, "
            "как он встречается в материалах. Не перефразируй — только точные "
            "выдержки.\n"
        )
    user_text = (
        f"{question}{verbatim_note}\n\n"
        "Ниже — материалы, которые я скинул в корзину (бот их прочитал, "
        "распознал видео/фото/голос/файлы/ссылки). Используй их как контекст "
        "для ответа:\n\n"
        f"{context}"
    )

    if not storage.is_enabled():
        with contextlib.suppress(Exception):
            await progress.edit_text(
                "Бо�� выключен. Включи через <code>/enable</code>."
            )
        return

    from .agent import run_agent
    from .handlers import _make_status_updater

    with contextlib.suppress(Exception):
        await progress.delete()
    on_status, finish_status = _make_status_updater(message)
    try:
        answer = await run_agent(uid, user_text, storage.get_cwd(uid) if uid else None,
                                 on_status=on_status)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dump: agent failed")
        await finish_status()
        await message.answer(f"Ошибка агента: {_html_escape(str(exc))}")
        return
    await finish_status()
    await _dump_deliver(message, _ql, answer)
    store.clear(uid)


@wizard_router.callback_query(F.data.startswith("dump:"))
async def cb_dump(query: CallbackQuery, state: FSMContext) -> None:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    from .addons.dump import store

    action = (query.data or "").split(":", 1)[1]

    if action == "clear":
        n = store.clear(uid or 0)
        await query.answer(f"Очистил ({n}).")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _dump_screen_text(uid),
                    reply_markup=_kb_dump(),
                    disable_web_page_preview=True,
                )
        return

    if action == "send":
        await query.answer("Обрабатываю…")
        if query.message is not None:
            await _dump_process(query.message, uid or 0)
            # Stay in collecting mode; show a fresh empty bin screen.
            with contextlib.suppress(Exception):
                await query.message.answer(
                    _dump_screen_text(uid),
                    reply_markup=_kb_dump(),
                    disable_web_page_preview=True,
                )
        return

    if action == "close":
        await query.answer("Закрываю…")
        if query.message is not None and store.count(uid or 0) > 0:
            await _dump_process(query.message, uid or 0)
        await state.clear()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.answer(
                    "✅ Вернулся в обычный режим.",
                    reply_markup=_kb_main_after_claim(uid),
                )
        return


# ---- 🎞 Анализ динамики видео ---------------------------------------------
#
# Reached from BOTH «Генерация → Видео» and «Исследование и поиск человека»
# via the same ``viddyn:open`` callback. The user sends a video; the bot
# samples many low-res-but-legible frames at a configurable interval
# (0.5…5 s), describes each with Vision + builds a reproduction prompt, and
# narrates the camera-angle dynamics + action sequence. A parallel coarse
# pass (every ~5 s, configurable) uses pure-CV metrics (inter-frame motion +
# sharpness) to tell real dynamics from a merely motion-blurred frame.
# Handlers live on wizard_router (registered first) so the awaiting-video
# capture wins over Groq overrides and the catch-all.


def _kb_viddyn() -> InlineKeyboardMarkup:
    from .addons.viddyn import store as vstore

    fine = vstore.get_fine()
    coarse = vstore.get_coarse()
    fine_row = [
        InlineKeyboardButton(
            text=("▶ " if abs(c - fine) < 1e-6 else "") + f"{c:g}с",
            callback_data=f"viddyn:fine:{c}",
        )
        for c in vstore.FINE_CHOICES
    ]
    coarse_row = [
        InlineKeyboardButton(
            text=("▶ " if abs(c - coarse) < 1e-6 else "") + f"{c:g}с",
            callback_data=f"viddyn:coarse:{c}",
        )
        for c in vstore.COARSE_CHOICES
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            fine_row,
            coarse_row,
            [InlineKeyboardButton(text="↩️ Назад", callback_data="viddyn:back")],
        ]
    )


def _viddyn_screen_text() -> str:
    from .addons.viddyn import store as vstore

    return (
        "<b>🎞 Анализ динамики видео</b>\n\n"
        "Пришли <b>видео</b> — я нарежу много скриншотов (низкое качество, но "
        "различимо), разберу каждый, дам <b>промт для воспроизведения</b> и "
        "опишу <b>динамику ракурса</b> и <b>последовательность экшна</b>.\n\n"
        "Параллельно сделаю проверку <b>каждые N секунд</b>: реальная динамика "
        "или кадр просто смазан в движении.\n\n"
        f"• <b>Детальный шаг</b> (что меняется в кадре): {vstore.get_fine():g}с\n"
        f"• <b>Шаг проверки</b> (есть ли динамика): {vstore.get_coarse():g}с\n\n"
        "<i>Жми кнопки ниже, чтобы поменять интервалы, потом пришли видео.</i>"
    )


async def _viddyn_open(query: CallbackQuery, state: FSMContext) -> None:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    await query.answer()
    await state.set_state(VidDynStates.awaiting_video)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.answer(
                _viddyn_screen_text(),
                reply_markup=_kb_viddyn(),
                disable_web_page_preview=True,
            )


@wizard_router.callback_query(F.data.startswith("viddyn:"))
async def cb_viddyn(query: CallbackQuery, state: FSMContext) -> None:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    from .addons.viddyn import store as vstore

    action = (query.data or "").split(":", 1)[1]

    if action == "open":
        await state.update_data(viddyn_fast=False)
        await _viddyn_open(query, state)
        return

    if action == "fast":
        await state.update_data(viddyn_fast=True)
        await _viddyn_open(query, state)
        return

    if action == "back":
        await query.answer()
        await state.clear()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "<b>Главное меню</b>",
                    reply_markup=_kb_main_after_claim(uid),
                )
        return

    if action.startswith("fine:"):
        with contextlib.suppress(ValueError):
            vstore.set_fine(float(action.split(":", 1)[1]))
        await query.answer("Детальный шаг обновлён.")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _viddyn_screen_text(),
                    reply_markup=_kb_viddyn(),
                    disable_web_page_preview=True,
                )
        return

    if action.startswith("coarse:"):
        with contextlib.suppress(ValueError):
            vstore.set_coarse(float(action.split(":", 1)[1]))
        await query.answer("Шаг проверки обновлён.")
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _viddyn_screen_text(),
                    reply_markup=_kb_viddyn(),
                    disable_web_page_preview=True,
                )
        return

    await query.answer()


@wizard_router.message(StateFilter(VidDynStates.awaiting_video), F.video | F.animation | F.document)
async def viddyn_capture_video(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not can_use(uid):
        return
    # Accept video, GIF/animation, or a video sent as a document.
    file_id = None
    suffix = ".mp4"
    if message.video:
        file_id = message.video.file_id
    elif message.animation:
        file_id = message.animation.file_id
    elif message.document:
        mime = (message.document.mime_type or "").lower()
        if "video" not in mime and not (message.document.file_name or "").lower().endswith(
            (".mp4", ".mov", ".webm", ".mkv", ".avi", ".gif")
        ):
            await message.answer("Это не похоже на видео. Пришли видео-файл.")
            return
        file_id = message.document.file_id
        suffix = os.path.splitext(message.document.file_name or "v.mp4")[1] or ".mp4"
    if not file_id:
        await message.answer("Пришли именно видео.")
        return
    fast = bool((await state.get_data()).get("viddyn_fast"))
    await _viddyn_process(message, uid, file_id, suffix, fast=fast)


async def _viddyn_process(message: Message, uid: int, file_id: str, suffix: str, *, fast: bool = False) -> None:
    from .addons.viddyn import analyzer, store as vstore

    progress = await message.answer("⏳ Скачиваю видео и нарезаю кадры…")
    path = None
    result = None
    try:
        from .groq_handlers import _download_to_tempfile

        path = await _download_to_tempfile(message.bot, file_id, suffix)
        if fast:
            with contextlib.suppress(Exception):
                await progress.edit_text("⚡ Быстро читаю движение по пикселям…")
            prompt = await analyzer.analyze_fast(path)
            with contextlib.suppress(Exception):
                await progress.delete()
            with contextlib.suppress(Exception):
                os.unlink(path)
            from .handlers import _send_long
            await _send_long(message, "🧬 <b>Промт из видео (fast):</b>\n\n" + prompt, code=False)
            return
        with contextlib.suppress(Exception):
            await progress.edit_text("🔎 Разбираю кадры (Vision) и считаю динамику…")
        result = await analyzer.analyze(
            path,
            fine_interval=vstore.get_fine(),
            coarse_interval=vstore.get_coarse(),
            max_fine=vstore.MAX_FINE_FRAMES,
            max_coarse=vstore.MAX_COARSE_FRAMES,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("viddyn: analysis failed")
        with contextlib.suppress(Exception):
            await progress.edit_text(f"❌ Не смог проанализировать видео: {_html_escape(str(exc))}")
        if path:
            with contextlib.suppress(Exception):
                os.unlink(path)
        return

    with contextlib.suppress(Exception):
        await progress.delete()

    # Send a contact-sheet of the low-res fine frames so the user sees them.
    try:
        from aiogram.types import FSInputFile, InputMediaPhoto

        media = []
        for s in result.fine[:10]:
            with contextlib.suppress(Exception):
                media.append(
                    InputMediaPhoto(
                        media=FSInputFile(s.jpg_path),
                        caption=f"t={s.t:g}с" if not media else None,
                    )
                )
        if len(media) >= 2:
            await message.answer_media_group(media)
        elif media:
            await message.answer_photo(media[0].media, caption=f"t={result.fine[0].t:g}с")
    except Exception:  # noqa: BLE001
        logger.debug("viddyn: media group send failed", exc_info=True)

    from .handlers import _send_long

    await _send_long(message, analyzer.build_report(result), code=False)
    analyzer.cleanup(result)
    if path:
        with contextlib.suppress(Exception):
            os.unlink(path)


@wizard_router.message(StateFilter(VidDynStates.awaiting_video), F.text)
async def viddyn_text_hint(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Пришли <b>видео</b> для анализа (или ↩️ Назад на экране выше).",
        reply_markup=_kb_viddyn(),
    )


# ---- 🧰 GitHub-инструменты анализа видео ----------------------------------
#
# Separate catalog button: lists every GitHub tool found for video/motion
# analysis. Each tool shows its repo link + «сценарий алгоритма» (the
# sequence of actions). Lightweight tools (optical flow, PySceneDetect,
# blur) have «▶️ Запустить на видео» — the user sends a video and the tool's
# algorithm runs on the host. Heavy GPU tools instead offer «📥 Клонировать
# репо» which shallow-clones and shows the run recipe. Handlers on
# wizard_router; entry button lives in the OSINT + video screens.


def _kb_ghtools_list() -> InlineKeyboardMarkup:
    from .addons.ghtools import catalog

    rows = []
    for t in catalog.TOOLS:
        suffix = " · встроен" if t.get("builtin") else ("" if t["runnable"] else " · GPU")
        rows.append([
            InlineKeyboardButton(
                text=f"{t['emoji']} {t['name']}{suffix}",
                callback_data=f"ghtools:tool:{t['key']}",
            )
        ])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="ghtools:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_ghtools_tool(key: str) -> InlineKeyboardMarkup:
    from .addons.ghtools import catalog

    t = catalog.TOOLS_BY_KEY.get(key)
    rows: list[list[InlineKeyboardButton]] = []
    if t and t["runnable"]:
        rows.append([InlineKeyboardButton(
            text="▶️ Запустить на видео", callback_data=f"ghtools:run:{key}")])
    elif t:
        rows.append([InlineKeyboardButton(
            text="📥 Клонировать репо + рецепт", callback_data=f"ghtools:clone:{key}")])
    if t:
        rows.append([InlineKeyboardButton(text="🔗 Открыть на GitHub", url=t["repo"])])
    rows.append([InlineKeyboardButton(text="↩️ К списку", callback_data="ghtools:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ghtools_list_text() -> str:
    return (
        "<b>🧰 GitHub-инструменты анализа видео</b>\n\n"
        "Лучшие тулзы для анализа динамики/движения. У каждого — ссылка и "
        "<b>сценарий алгоритма</b> (последовательность действий). Лёгкие можно "
        "<b>прогнать прямо на видео</b>, тяжёлые (GPU) — склонировать с рецептом.\n\n"
        "<i>Выбери инструмент:</i>"
    )


def _ghtools_tool_text(key: str) -> str:
    from .addons.ghtools import catalog

    t = catalog.TOOLS_BY_KEY.get(key)
    if not t:
        return "Инструмент не найден."
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(t["scenario"], 1))
    body = (
        f"<b>{t['emoji']} {t['name']}</b>\n"
        f"{t['purpose']}\n"
        f"🔗 {t['repo']}\n\n"
        f"<b>Сценарий алгоритма:</b>\n{steps}"
    )
    if t.get("note"):
        body += f"\n\n<i>{t['note']}</i>"
    return body


async def _ghtools_open(query: CallbackQuery, state: FSMContext) -> None:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    await query.answer()
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.answer(
                _ghtools_list_text(),
                reply_markup=_kb_ghtools_list(),
                disable_web_page_preview=True,
            )


@wizard_router.callback_query(F.data.startswith("ghtools:"))
async def cb_ghtools(query: CallbackQuery, state: FSMContext) -> None:
    uid = query.from_user.id if query.from_user else None
    if not can_use(uid):
        await query.answer("Только владелец.", show_alert=True)
        return
    from .addons.ghtools import catalog, runner

    action = (query.data or "").split(":", 1)[1]

    if action == "open":
        await _ghtools_open(query, state)
        return

    if action == "back":
        await query.answer()
        await state.clear()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "<b>Главное меню</b>", reply_markup=_kb_main_after_claim(uid)
                )
        return

    if action.startswith("tool:"):
        key = action.split(":", 1)[1]
        await query.answer()
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.answer(
                    _ghtools_tool_text(key),
                    reply_markup=_kb_ghtools_tool(key),
                    disable_web_page_preview=True,
                )
        return

    if action.startswith("run:"):
        key = action.split(":", 1)[1]
        await query.answer()
        await state.set_state(GhToolsStates.awaiting_video)
        await state.update_data(ghtool_key=key)
        t = catalog.TOOLS_BY_KEY.get(key, {})
        if query.message is not None:
            with contextlib.suppress(Exception):
                await query.message.answer(
                    f"Пришли <b>видео</b> — прогоню <b>{t.get('name', key)}</b> по его алгоритму.")
        return

    if action.startswith("clone:"):
        key = action.split(":", 1)[1]
        await query.answer("Клонирую…")
        t = catalog.TOOLS_BY_KEY.get(key)
        if not t:
            return
        msg = None
        if query.message is not None:
            msg = await query.message.answer(f"📥 Клонирую <code>{t['repo']}</code>…")
        report = await runner.clone_probe(t["repo"])
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(t["scenario"], 1))
        full = f"{report}\n\nСценарий запуска:\n{steps}"
        if msg is not None:
            with contextlib.suppress(Exception):
                await msg.delete()
        from .handlers import _send_long

        if query.message is not None:
            await _send_long(query.message, full, code=True)
        return

    await query.answer()


@wizard_router.message(StateFilter(GhToolsStates.awaiting_video), F.video | F.animation | F.document)
async def ghtools_capture_video(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not can_use(uid):
        return
    data = await state.get_data()
    key = data.get("ghtool_key")
    from .addons.ghtools import catalog, runner

    t = catalog.TOOLS_BY_KEY.get(key)
    if not t:
        await message.answer("Не понял инструмент — открой список заново.")
        await state.clear()
        return
    file_id = None
    suffix = ".mp4"
    if message.video:
        file_id = message.video.file_id
    elif message.animation:
        file_id = message.animation.file_id
    elif message.document:
        mime = (message.document.mime_type or "").lower()
        if "video" not in mime and not (message.document.file_name or "").lower().endswith(
            (".mp4", ".mov", ".webm", ".mkv", ".avi", ".gif")
        ):
            await message.answer("Это не похоже на видео. Пришли видео-файл.")
            return
        file_id = message.document.file_id
        suffix = os.path.splitext(message.document.file_name or "v.mp4")[1] or ".mp4"
    if not file_id:
        await message.answer("Пришли именно видео.")
        return
    progress = await message.answer(f"⏳ Запускаю {t['name']}…")
    path = None
    try:
        from .groq_handlers import _download_to_tempfile

        path = await _download_to_tempfile(message.bot, file_id, suffix)
        report = await runner.run_on_video(t["runner"], path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ghtools: run failed")
        report = f"❌ Не смог запустить: {exc}"
    finally:
        if path:
            with contextlib.suppress(Exception):
                os.unlink(path)
    with contextlib.suppress(Exception):
        await progress.delete()
    from .handlers import _send_long

    await _send_long(message, report, code=True)


@wizard_router.message(StateFilter(GhToolsStates.awaiting_video), F.text)
async def ghtools_text_hint(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    await message.answer("Пришли <b>видео</b> для запуска инструмента (или открой список заново).")


# ===================================================================
# «🔧 Регенерация» — the bot diagnoses its own code and the *coder*
# writes the fix. Three deliverables (zip / json+instr / описание), a
# «скажи что не так» free-text flow, and a background analysis toggle
# (симуляция / реал). Handlers live on wizard_router (state priority),
# like dump / viddyn / ghtools. Owner-only (can_use gate).
# ===================================================================

# Process-lifetime cache of the last FixResult per (uid, issue_id) so the
# 3 output buttons can deliver without re-running the coder. Capped.
_REGEN_FIX_CACHE: dict[str, object] = {}
# Per-issue research digest cache (so «🔬 как решают» + fix share it).
_REGEN_RESEARCH_CACHE: dict[str, str] = {}
# Per-user last scan report (for verify before→after) and last built zip.
_REGEN_LAST_SCAN: dict[int, dict] = {}
_REGEN_LAST_ZIP: dict[int, dict] = {}


def regen_error_keyboard() -> InlineKeyboardMarkup:
    """Two buttons attached to any error the bot shows the user:
    «🔧 Регенерация» (let the coder fix it) and «🛠 Разрешить перенастроить
    через админку» (grant permission + open the VISION panel)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔧 Регенерация", callback_data="regen:fromerr")],
            [InlineKeyboardButton(
                text="🛠 Разрешить перенастроить через админку",
                callback_data="regen:adminfix",
            )],
        ]
    )


def regen_record_error(text: str, *, where: str = "") -> None:
    """Persist the last error so «🔧 Регенерация» on the error message can
    seed the coder with it. Never raises."""
    with contextlib.suppress(Exception):
        from .addons.regen import store as rstore

        rstore.record_error(text, where=where)


def _regen_cache_key(uid: int, issue_id: str) -> str:
    return f"{uid}:{issue_id}"


def _regen_cache_put(uid: int, issue_id: str, fix: object) -> None:
    if len(_REGEN_FIX_CACHE) > 40:
        _REGEN_FIX_CACHE.clear()
    _REGEN_FIX_CACHE[_regen_cache_key(uid, issue_id)] = fix


def _regen_screen_text(uid: int) -> str:
    from .addons.regen import store as rstore

    bg = "🟢 ВКЛ" if rstore.bg_enabled() else "🔴 ВЫКЛ"
    mode = "симуляция" if rstore.mode() == "sim" else "реал (выход в мир)"
    lines = [
        "🔧 <b>Регенерация</b>",
        "",
        "Бот смотрит на свой код <b>изнутри</b>, находит больное место и "
        "<b>Кодер</b> пишет фикс. На выходе — 3 формата:",
        "• 📦 <b>zip</b> готового бота (с применённым фиксом),",
        "• 🧩 <b>json+инструкция</b> (отдать другому ИИ),",
        "• 📄 <b>описание</b> «где и что болит».",
        "",
        "Можно просто <b>сказать словами</b>, что не так — кодер примет это "
        "за основу. Для решения он смотрит, <i>как решают похожее люди</i> "
        "(веб-ресёрч / Анализатор).",
        "",
        f"Фоновый анализ: <b>{bg}</b> · режим: <b>{mode}</b>",
    ]
    if rstore.job_active():
        lbl = rstore.job_label() or "решаю проблему"
        lines.append(f"Статус: 🟡 <b>работаю</b> — {lbl}…")
    else:
        lines.append("Статус: 🟢 <b>всё сделано</b>")
    auto = rstore.auto_output()
    if auto:
        amap = {"zip": "📦 zip", "json": "🧩 json", "desc": "📄 описание"}
        lines.append(f"Авто-формат выдачи: <b>{amap.get(auto, auto)}</b>")
    rep = rstore.last_report()
    if rep:
        c = rep.get("counts", {})
        when = time.strftime("%d.%m %H:%M", time.localtime(rep.get("ts", 0)))
        if rep.get("fatal"):
            lines.append(f"Последний скан ({when}): ⚠️ {rep['fatal']}")
        else:
            lines.append(
                f"Последний скан ({when}): 🔴 ошибок {c.get('error', 0)}, "
                f"🟡 предупреждений {c.get('warn', 0)}"
            )
    return "\n".join(lines)


def _kb_regen(uid: int) -> InlineKeyboardMarkup:
    from .addons.regen import store as rstore

    bg_label = "🔴 Выключить фон" if rstore.bg_enabled() else "🟢 Включить фон"
    mode_label = (
        "🎛 Режим: симуляция" if rstore.mode() == "sim" else "🎛 Режим: реал"
    )
    # Dynamic status button: 🟡 while the coder is solving, 🟢 once done.
    if rstore.job_active():
        status_label = "🟡 Статус: работаю…"
    else:
        status_label = "🟢 Статус: всё сделано"
    rows = [
        [InlineKeyboardButton(text=status_label, callback_data="regen:status")],
        [InlineKeyboardButton(text="🔍 Сканировать бота", callback_data="regen:scan")],
        [InlineKeyboardButton(text="🎤 Найти проблемы через дебаты", callback_data="regen:debate")],
        [InlineKeyboardButton(text="🗣 Сказать что не так", callback_data="regen:tell")],
        [InlineKeyboardButton(text="➕ Улучшить проект из Важного", callback_data="impj:home")],
        [InlineKeyboardButton(text=bg_label, callback_data="regen:bg")],
        [InlineKeyboardButton(text=mode_label, callback_data="regen:mode")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="regen:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_regen_issues(report: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    sev_emoji = {"error": "🔴", "warn": "🟡", "info": "⚪"}
    for it in report.get("issues", [])[:10]:
        em = sev_emoji.get(it.get("severity"), "⚪")
        where = (it.get("where") or "?")[:40]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{em} {where}",
                    callback_data=f"regen:issue:{it.get('id')}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🛠 Починить всё", callback_data="regen:fixall")])
    rows.append([
        InlineKeyboardButton(text="📥 Скачать список", callback_data="regen:dllist"),
        InlineKeyboardButton(text="🔄 Заново", callback_data="regen:scan"),
    ])
    rows.append([InlineKeyboardButton(text="✖ Закрыть", callback_data="regen:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_regen_issue(issue_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Кодер: написать фикс", callback_data=f"regen:fix:{issue_id}")],
            [InlineKeyboardButton(text="🔬 Узнать как решают", callback_data=f"regen:research:{issue_id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="regen:scan")],
        ]
    )


_REGEN_OUT_LABELS = {
    "zip": "📦 ZIP готового бота",
    "json": "🧩 JSON + инструкция",
    "desc": "📄 Описание (где болит)",
}


def _kb_regen_outputs(issue_id: str) -> InlineKeyboardMarkup:
    """Format picker. Tapping a format opens a small per-format submenu with
    «авто / исполнить / вернуться» (Mason's request). The active auto-format
    is marked ⚡."""
    from .addons.regen import store as rstore

    auto = rstore.auto_output()
    rows: list[list[InlineKeyboardButton]] = []
    for kind, label in _REGEN_OUT_LABELS.items():
        mark = "  ⚡АВТО" if auto == kind else ""
        rows.append(
            [InlineKeyboardButton(text=label + mark, callback_data=f"regen:outmenu:{kind}:{issue_id}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="regen:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_regen_output_one(kind: str, issue_id: str) -> InlineKeyboardMarkup:
    """Per-format submenu: «авто» (always output this way), «исполнить»
    (manual one-time output now), «вернуться» (back to the format list)."""
    from .addons.regen import store as rstore

    is_auto = rstore.auto_output() == kind
    auto_label = "⚡ Авто: выключить" if is_auto else "⚡ Авто (всегда так выдавать)"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Исполнить (выдать сейчас)", callback_data=f"regen:out:{kind}:{issue_id}")],
            [InlineKeyboardButton(text=auto_label, callback_data=f"regen:setauto:{kind}:{issue_id}")],
            [InlineKeyboardButton(text="◀️ Вернуться", callback_data=f"regen:outback:{issue_id}")],
        ]
    )


async def _regen_open(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = query.from_user.id if query.from_user else 0
    # remember where to send background notifications
    with contextlib.suppress(Exception):
        from .addons.regen import store as rstore

        if query.message is not None:
            rstore.set_notify_chat_id(query.message.chat.id)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _regen_screen_text(uid),
                reply_markup=_kb_regen(uid),
                disable_web_page_preview=True,
            )


def _regen_issue_context(issue: dict) -> str:
    """Build code context for the coder from an issue's location."""
    from .addons.regen import scanner as rscan

    where = issue.get("where") or ""
    snippet = rscan.read_snippet(where)
    # for import/router errors the whole file often matters
    if issue.get("kind", "").startswith(("import", "routers", "syntax")):
        rel = where.split(":", 1)[0]
        full = rscan.full_file(rel, max_chars=12000)
        if full:
            return full
    return snippet


_REGEN_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}(?:/[A-Za-z0-9_./]+)?")


def _regen_guess_context(text: str) -> str:
    """For the «скажи что не так» flow: find files/identifiers the user
    mentioned and attach their code as context for the coder."""
    from .addons.regen import scanner as rscan

    root = rscan.repo_root()
    chunks: list[str] = []
    seen: set[str] = set()
    # 1) explicit file paths like bot/addons/x/handlers.py
    for m in re.finditer(r"[\w/]+\.py", text):
        rel = m.group(0)
        if rel in seen:
            continue
        seen.add(rel)
        full = rscan.full_file(rel, root=root, max_chars=8000)
        if full:
            chunks.append(full)
    # 2) identifiers → grep for their definition file (best-effort, capped)
    if not chunks:
        for ident in dict.fromkeys(_REGEN_IDENT_RE.findall(text)):
            if "/" in ident or len(chunks) >= 2:
                continue
            try:
                import subprocess

                proc = subprocess.run(
                    ["grep", "-rl", "-m1", f"def {ident}", "bot/"],
                    cwd=str(root), capture_output=True, text=True, timeout=10,
                )
                rel = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
            except Exception:  # noqa: BLE001
                rel = ""
            if rel and rel not in seen:
                seen.add(rel)
                full = rscan.full_file(rel, root=root, max_chars=8000)
                if full:
                    chunks.append(full)
    return "\n\n".join(chunks)[:14000]


async def _regen_run_fix(
    query: CallbackQuery,
    state: FSMContext,
    *,
    issue_id: str,
    problem: str,
    diagnosis: str,
    code_context: str,
) -> None:
    """Run research (how people solve it) + the coder, cache the FixResult,
    then offer the 3 output formats."""
    from .addons.regen import NoBrainError, generate_fix, research as rresearch, store as rstore

    uid = query.from_user.id if query.from_user else 0
    msg = query.message
    if msg is None:
        return
    # Mark status 🟡 «работаю» until the coder finishes (→ 🟢 «всё сделано»).
    rstore.start_job(problem[:60])
    note = await msg.answer("🔬 Смотрю, как решают похожее, и зову кодера…")
    research = _REGEN_RESEARCH_CACHE.get(issue_id, "")
    if not research:
        with contextlib.suppress(Exception):
            research = await rresearch.research_problem(problem)
        _REGEN_RESEARCH_CACHE[issue_id] = research or ""
    # Build the whole-project map so the coder knows «руки и ноги».
    project_map = ""
    with contextlib.suppress(Exception):
        from .addons.regen.project_map import build_project_map
        project_map = build_project_map()
    try:
        fix = await generate_fix(
            problem,
            code_context=code_context,
            diagnosis=diagnosis,
            research=research,
            project_map=project_map,
        )
    except NoBrainError as exc:
        rstore.finish_job()
        with contextlib.suppress(Exception):
            await note.edit_text(f"🧠 {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        rstore.finish_job()
        logger.exception("regen generate_fix failed")
        with contextlib.suppress(Exception):
            await note.edit_text(f"Кодер не смог: {type(exc).__name__}: {exc}")
        return
    _regen_cache_put(uid, issue_id, fix)
    rstore.finish_job()  # problem solved → 🟢
    head = [
        "🤖 Кодер написал фикс.",
        f"Причина: {fix.root_cause[:400] if fix.root_cause else '—'}",
        f"Файлов с готовым кодом: {len(fix.files)} · уверенность: {fix.confidence}",
        "",
        "Выбери формат выдачи ⤵️",
    ]
    with contextlib.suppress(Exception):
        await note.delete()
    await _regen_present_fix(msg, uid, issue_id, head)


async def _regen_present_fix(
    msg: Message, uid: int, issue_id: str, head: list[str]
) -> None:
    """Send the fix summary, then either auto-deliver (if an «авто» format is
    set) or show the format picker."""
    from .addons.regen import store as rstore
    from .handlers import _send_long

    await _send_long(msg, "\n".join(head), code=False)
    auto = rstore.auto_output()
    if auto:
        amap = {"zip": "📦 zip", "json": "🧩 json", "desc": "📄 описание"}
        await msg.answer(f"⚡ Авто-формат включён — выдаю {amap.get(auto, auto)} автоматически.")
        await _regen_send_output(msg, uid, auto, issue_id)
        await msg.answer("Другой формат?", reply_markup=_kb_regen_outputs(issue_id))
    else:
        await msg.answer("Форматы:", reply_markup=_kb_regen_outputs(issue_id))


async def _regen_deliver_output(query: CallbackQuery, kind: str, issue_id: str) -> None:
    uid = query.from_user.id if query.from_user else 0
    msg = query.message
    if msg is None:
        return
    await _regen_send_output(msg, uid, kind, issue_id)


async def _regen_send_output(msg: Message, uid: int, kind: str, issue_id: str) -> None:
    from aiogram.types import FSInputFile

    from .addons.regen import outputs as routs, scanner as rscan, store as rstore
    from .handlers import _send_long

    fix = _REGEN_FIX_CACHE.get(_regen_cache_key(uid, issue_id))
    if fix is None:
        await msg.answer("Фикс не найден в кэше — нажми «🤖 Кодер: написать фикс» заново.")
        return
    issue = rstore.issue_by_id(issue_id)
    if kind == "desc":
        text = routs.build_description(fix, issue)
        await _send_long(msg, text, code=False)
        return
    if kind == "json":
        path = routs.build_json_file(fix, issue)
        with contextlib.suppress(Exception):
            await msg.answer_document(
                FSInputFile(path, filename=os.path.basename(path)),
                caption="🧩 JSON с кодом + инструкцией — отдай другому ИИ.",
            )
        return
    if kind == "zip":
        prog = await msg.answer("📦 Собираю zip, проверяю компиляцию и пересканирую…")
        before_errors = None
        with contextlib.suppress(Exception):
            rep = _REGEN_LAST_SCAN.get(uid)
            if rep:
                before_errors = int((rep.get("counts") or {}).get("error", 0))
        try:
            zip_path, errors, verify = await asyncio.to_thread(
                routs.build_fix_zip, fix, rscan.repo_root(), issue=issue,
                before_errors=before_errors,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("regen zip failed")
            with contextlib.suppress(Exception):
                await prog.edit_text(f"Не смог собрать zip: {type(exc).__name__}: {exc}")
            return
        cap = "📦 Готовый бот с фиксом."
        if not fix.files:
            cap = "📦 Бот + REGEN_FIX_NOTES.md (кодер дал рецепт, без полных файлов)."
        elif errors:
            cap = "⚠️ Zip собран, но есть ошибки компиляции — см. REGEN_FIX_NOTES.md перед деплоем."
        # Append the verify (before→after) line to the caption.
        b, a = verify.get("before"), verify.get("after")
        if a is not None and b is not None:
            mark = "✅" if a < b else ("➖" if a == b else "⚠️")
            cap += f"\n{mark} ошибок: было {b} → стало {a}"
        elif a is not None:
            cap += f"\nОшибок сканера после фикса: {a}"
        # Remember the zip path so «Вывести в Тест» can summarise it.
        _REGEN_LAST_ZIP[uid] = {"path": zip_path, "verify": verify, "errors": errors,
                                "issue_id": issue_id}
        with contextlib.suppress(Exception):
            await prog.delete()
        with contextlib.suppress(Exception):
            await msg.answer_document(
                FSInputFile(zip_path, filename=os.path.basename(zip_path)),
                caption=cap,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🧪 Вывести в Тест", callback_data=f"regen:test:{issue_id}")],
                ]),
            )
        return


@wizard_router.callback_query(F.data.startswith("regen:"))
async def cb_regen(query: CallbackQuery, state: FSMContext) -> None:
    if not can_use(query.from_user.id if query.from_user else None):
        with contextlib.suppress(Exception):
            await query.answer("Недоступно", show_alert=True)
        return
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    uid = query.from_user.id if query.from_user else 0
    msg = query.message

    if action == "open":
        await query.answer()
        await _regen_open(query, state)
        return

    if action == "close":
        await query.answer()
        await state.clear()
        if msg is not None:
            with contextlib.suppress(Exception):
                await msg.edit_text("Меню:", reply_markup=_kb_main_after_claim(uid))
        return

    if action == "bg":
        from .addons.regen import store as rstore

        rstore.set_bg(not rstore.bg_enabled())
        await query.answer("Фоновый анализ переключён")
        await _regen_open(query, state)
        return

    if action == "mode":
        from .addons.regen import store as rstore

        rstore.set_mode("real" if rstore.mode() == "sim" else "sim")
        await query.answer("Режим переключён")
        await _regen_open(query, state)
        return

    if action == "debate":
        await query.answer("🎤 Готовлю ТЗ для дебатёров…")
        if msg is None:
            return
        note = await msg.answer("🎤 Собираю карту проекта и скан для дебатов…")
        # Build the ТЗ: project map + static scan + debate format.
        pmap = ""
        scan_block = ""
        with contextlib.suppress(Exception):
            from .addons.regen.project_map import build_project_map
            pmap = build_project_map()
        with contextlib.suppress(Exception):
            from .addons.regen import scanner as rscan
            rep = await rscan.scan()
            issues = rep.get("issues", []) if isinstance(rep, dict) else []
            if issues:
                scan_block = "\n".join(
                    f"- [{it.get('severity')}] {it.get('where')}: {it.get('message')}"
                    for it in issues[:40])
        tz_text = (
            "ТЗ ДЛЯ ДЕБАТЁРОВ — найти НЕРАБОЧИЕ и НЕМЫЕ кнопки бота.\n\n"
            "Правила дебатов:\n"
            "1. Каждый дебатёр даёт 3 варианта: какая кнопка не исполняет свою "
            "функцию или не нажимается (не тыкается).\n"
            "2. Бот критикует каждого («что ты не дожал») — дебатёр дорабатывает, "
            "пока не скажет, что лучше уже невозможно (подсказывать нельзя).\n"
            "3. Итого 9 вариантов (3 дебатёра × 3). Разобрать каждый и выбрать "
            "лучший — самую вероятную нерабочую/немую кнопку.\n\n"
            "=== КАРТА ПРОЕКТА ===\n" + pmap[:12000] +
            "\n\n=== СТАТИЧЕСКИЙ СКАН (ошибки/предупреждения) ===\n" +
            (scan_block or "(чисто)")
        )
        # Try the existing debate engine; fall back to a file.
        started = False
        with contextlib.suppress(Exception):
            from .addons.debates.handlers import start_problem_debate
            started = await start_problem_debate(
                msg, tz_text, "Поиск нерабочих кнопок", required=3)
        with contextlib.suppress(Exception):
            await note.delete()
        if not started:
            from aiogram.types import BufferedInputFile
            with contextlib.suppress(Exception):
                await msg.answer_document(
                    BufferedInputFile(tz_text.encode("utf-8"), filename="debate_tz.txt"),
                    caption="🎤 Debate-сервер не настроен. Вот ТЗ — запусти его в "
                            "«🎤 Дебаты» (создай дверь, пришли это как ТЗ).",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎤 Открыть Дебаты", callback_data="main:debates")]]))
        return

    if action == "tell":
        await query.answer()
        await state.set_state(RegenStates.awaiting_problem)
        if msg is not None:
            await msg.answer(
                "🗣 Опиши <b>словами</b>, что не так (что за кнопка/функция, что "
                "происходит, какая ошибка). Можешь указать файл (напр. "
                "<code>bot/addons/x/handlers.py</code>) или имя функции — кодер "
                "возьмёт твои слова за основу и напишет фикс."
            )
        return

    if action == "scan":
        await query.answer()
        if msg is not None:
            prog = await msg.answer("🔍 Сканирую бота изнутри (синтаксис, импорты, роутеры, lint, мёртвые кнопки)…")
        else:
            prog = None
        from .addons.regen import scanner as rscan, store as rstore

        report = await rscan.scan()
        rstore.save_report(report)
        _REGEN_LAST_SCAN[uid] = report
        if prog is not None:
            with contextlib.suppress(Exception):
                await prog.delete()
        if report.get("fatal"):
            if msg is not None:
                await msg.answer(
                    f"Сканер не смог: {report['fatal']}",
                    reply_markup=_kb_regen(uid),
                )
            return
        c = report.get("counts", {})
        issues = report.get("issues", [])
        if not issues:
            ruff_note = "" if report.get("ruff_available") else "\n(глубокий lint пропущен — на хосте нет ruff)"
            if msg is not None:
                await msg.answer(
                    f"✅ Чисто: ошибок 0, предупреждений 0. Бот выглядит здоровым.{ruff_note}",
                    reply_markup=_kb_regen(uid),
                )
            return
        head = (
            f"🔍 Скан готов: 🔴 ошибок {c.get('error', 0)}, "
            f"🟡 предупреждений {c.get('warn', 0)}. Выбери проблему — кодер напишет фикс:"
        )
        if msg is not None:
            await msg.answer(head, reply_markup=_kb_regen_issues(report))
        return

    if action == "issue":
        await query.answer()
        issue_id = parts[2] if len(parts) > 2 else ""
        from .addons.regen import store as rstore
        issue = rstore.issue_by_id(issue_id)
        if not issue or msg is None:
            if msg is not None:
                await msg.answer("Проблема не найдена — отсканируй заново.")
            return
        from .handlers import _send_long

        text = (
            "🔧 Проблема\n"
            f"Где: {issue.get('where')}\n"
            f"Тип: {issue.get('kind')} · важность: {issue.get('severity')}\n"
            f"{('Символ: ' + issue['symbol']) if issue.get('symbol') else ''}\n\n"
            f"{issue.get('message')}\n"
            f"{('Код: ' + issue['snippet']) if issue.get('snippet') else ''}"
        )
        await _send_long(msg, text, code=False)
        await msg.answer("Действия:", reply_markup=_kb_regen_issue(issue_id))
        return

    if action == "research":
        await query.answer()
        issue_id = parts[2] if len(parts) > 2 else ""
        from .addons.regen import research as rresearch, store as rstore
        from .handlers import _send_long

        issue = rstore.issue_by_id(issue_id)
        if not issue or msg is None:
            return
        prog = await msg.answer("🔬 Ищу, как решают похожее…")
        problem = f"{issue.get('kind')} {issue.get('where')}: {issue.get('message')}"
        digest = ""
        with contextlib.suppress(Exception):
            digest = await rresearch.research_problem(problem)
        _REGEN_RESEARCH_CACHE[issue_id] = digest or ""
        with contextlib.suppress(Exception):
            await prog.delete()
        if not digest:
            await msg.answer(
                "Ресёрч недоступен (нет ключа Tavily или пусто). Кодер починит и без него.",
                reply_markup=_kb_regen_issue(issue_id),
            )
            return
        await _send_long(msg, "🔬 Как решают похожее:\n\n" + digest, code=False)
        await msg.answer("Действия:", reply_markup=_kb_regen_issue(issue_id))
        return

    if action == "fix":
        await query.answer()
        issue_id = parts[2] if len(parts) > 2 else ""
        from .addons.regen import store as rstore

        issue = rstore.issue_by_id(issue_id)
        if not issue:
            if msg is not None:
                await msg.answer("Проблема не найдена — отсканируй заново.")
            return
        problem = (
            f"{issue.get('kind')} в {issue.get('where')}: {issue.get('message')}"
        )
        diagnosis = (
            f"Сканер: [{issue.get('severity')}] {issue.get('kind')} @ "
            f"{issue.get('where')} — {issue.get('message')}"
        )
        ctx = _regen_issue_context(issue)
        await _regen_run_fix(
            query, state,
            issue_id=issue_id, problem=problem, diagnosis=diagnosis, code_context=ctx,
        )
        return

    if action == "status":
        # Dynamic status button — just refresh the screen (🟡/🟢).
        from .addons.regen import store as rstore

        await query.answer("🟡 Работаю…" if rstore.job_active() else "🟢 Всё сделано")
        await _regen_open(query, state)
        return

    if action == "outmenu":
        # Open the per-format submenu (авто / исполнить / вернуться).
        await query.answer()
        kind = parts[2] if len(parts) > 2 else ""
        issue_id = parts[3] if len(parts) > 3 else ""
        if msg is not None and kind in _REGEN_OUT_LABELS:
            with contextlib.suppress(Exception):
                await msg.answer(
                    f"{_REGEN_OUT_LABELS[kind]} — выбери:",
                    reply_markup=_kb_regen_output_one(kind, issue_id),
                )
        return

    if action == "setauto":
        # Toggle «авто» for a format: set it (or clear if already on).
        from .addons.regen import store as rstore

        kind = parts[2] if len(parts) > 2 else ""
        issue_id = parts[3] if len(parts) > 3 else ""
        if rstore.auto_output() == kind:
            rstore.set_auto_output(None)
            await query.answer("⚡ Авто-формат выключен")
        else:
            rstore.set_auto_output(kind)
            await query.answer("⚡ Теперь выдаю этот формат автоматически")
        if msg is not None:
            with contextlib.suppress(Exception):
                await msg.edit_reply_markup(reply_markup=_kb_regen_output_one(kind, issue_id))
        return

    if action == "outback":
        await query.answer()
        issue_id = parts[2] if len(parts) > 2 else ""
        if msg is not None:
            with contextlib.suppress(Exception):
                await msg.answer("Форматы:", reply_markup=_kb_regen_outputs(issue_id))
        return

    if action == "out":
        await query.answer()
        kind = parts[2] if len(parts) > 2 else ""
        issue_id = parts[3] if len(parts) > 3 else ""
        await _regen_deliver_output(query, kind, issue_id)
        return

    if action == "fromerr":
        # «🔧 Регенерация» pressed on an error message: seed the coder with
        # the last error the bot showed and write a fix.
        await query.answer()
        from .addons.regen import store as rstore

        err = rstore.last_error()
        if not err or msg is None:
            if msg is not None:
                await msg.answer(
                    "Не нашёл последнюю ошибку. Открой «🔧 Регенерация» и нажми "
                    "«🗣 Сказать что не так».",
                    reply_markup=_kb_regen(uid),
                )
            return
        problem = err.get("text", "") or "Ошибка без текста"
        where = err.get("where", "")
        issue_id = f"err{int(time.time()) % 100000}"
        diagnosis = f"Ошибка показана пользователю{(' в ' + where) if where else ''}: {problem}"
        ctx = _regen_guess_context(f"{where} {problem}")
        with contextlib.suppress(Exception):
            rstore.set_notify_chat_id(msg.chat.id)
        await _regen_run_fix(
            query, state,
            issue_id=issue_id, problem=problem, diagnosis=diagnosis, code_context=ctx,
        )
        return

    if action == "adminfix":
        # «🛠 Разрешить перенастроить через админку» — grant permission and
        # open the VISION web panel.
        from .addons.regen import store as rstore

        rstore.set_admin_fix_allowed(True)
        await query.answer("🛠 Разрешено. Открываю админку…")
        if msg is None:
            return
        link = ""
        with contextlib.suppress(Exception):
            from bot.addons.vision.server import (
                generate_link as _vis_generate,
                get_link as _vis_link,
                is_enabled as _vis_enabled,
            )

            link = _vis_link() if (_vis_enabled() and _vis_link()) else _vis_generate()[1]
        if link:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🖥 Открыть админку (VISION)", url=link)],
                    [InlineKeyboardButton(text="🔧 Регенерация", callback_data="regen:open")],
                ]
            )
            await msg.answer(
                "🛠 <b>Разрешение выдано.</b>\n\nТеперь можно перенастроить бота "
                "через веб-админку VISION — открой её и поправь нужное:",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        else:
            await msg.answer(
                "🛠 Разрешение выдано, но VISION недоступен. Открой «🖥 VISION» из "
                "главного меню вручную.",
                reply_markup=_kb_regen(uid),
            )
        return

    if action == "fixall":
        # Build ONE spec from all error-issues and let the coder solve them
        # together (whole-project context applies).
        await query.answer("🛠 Беру все проблемы…")
        report = _REGEN_LAST_SCAN.get(uid) or {}
        issues = [it for it in report.get("issues", []) if it.get("severity") == "error"]
        if not issues:
            issues = report.get("issues", [])
        if not issues:
            if msg is not None:
                await msg.answer("Сначала отсканируй (нет списка проблем).",
                                 reply_markup=_kb_regen(uid))
            return
        prob_lines, diag_lines, ctx_parts = [], [], []
        for i, it in enumerate(issues[:12], 1):
            prob_lines.append(f"{i}. {it.get('kind')} в {it.get('where')}: {it.get('message')}")
            diag_lines.append(f"[{it.get('severity')}] {it.get('where')} — {it.get('message')}")
            with contextlib.suppress(Exception):
                ctx_parts.append(_regen_issue_context(it))
        problem = "Почини ВСЕ перечисленные проблемы бота:\n" + "\n".join(prob_lines)
        diagnosis = "Сканер нашёл:\n" + "\n".join(diag_lines)
        ctx = "\n\n".join(ctx_parts)[:30000]
        await _regen_run_fix(
            query, state,
            issue_id="all", problem=problem, diagnosis=diagnosis, code_context=ctx,
        )
        return

    if action == "dllist":
        # Hand the raw issue list off as a file (to give to another AI / me).
        await query.answer()
        report = _REGEN_LAST_SCAN.get(uid) or {}
        issues = report.get("issues", [])
        if not issues or msg is None:
            if msg is not None:
                await msg.answer("Список пуст — отсканируй заново.")
            return
        c = report.get("counts", {})
        lines = [
            "# Список проблем бота (regen scan)",
            f"# ошибок {c.get('error', 0)}, предупреждений {c.get('warn', 0)}",
            "",
        ]
        for i, it in enumerate(issues, 1):
            lines.append(
                f"{i}. [{it.get('severity')}] {it.get('kind')} @ {it.get('where')}\n"
                f"   {it.get('message')}"
            )
        from aiogram.types import BufferedInputFile
        data = "\n".join(lines).encode("utf-8")
        with contextlib.suppress(Exception):
            await msg.answer_document(
                BufferedInputFile(data, filename="regen_issues.txt"),
                caption="📥 Список проблем — отдай другому ИИ или мне.",
            )
        return

    if action == "test":
        # Push a summary of the last built fix to the «🧪 Тест» preview page.
        await query.answer("🧪 Готовлю превью…")
        issue_id = parts[2] if len(parts) > 2 else ""
        info = _REGEN_LAST_ZIP.get(uid)
        if not info or msg is None:
            if msg is not None:
                await msg.answer("Нет собранного фикса — сначала получи 📦 zip.")
            return
        fix = _REGEN_FIX_CACHE.get(_regen_cache_key(uid, issue_id or info.get("issue_id", "")))
        try:
            from .addons.test import server as tsrv
            link = tsrv.publish_regen_preview(fix, info.get("verify") or {}, info.get("errors") or [])
        except Exception as exc:  # noqa: BLE001
            logger.exception("regen test preview failed")
            await msg.answer(f"Не смог вывести в Тест: {type(exc).__name__}: {exc}")
            return
        if link:
            await msg.answer(
                "🧪 <b>Превью фикса в Тесте</b>\nОткрой в браузере:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🖥 Открыть Тест", url=link)],
                ]),
            )
        else:
            await msg.answer("🧪 Тест не включён. Открой «🧪 Тест» в меню и включи превью.")
        return

    await query.answer()


@wizard_router.message(StateFilter(RegenStates.awaiting_problem), F.text)
async def regen_capture_problem(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    problem = (message.text or "").strip()
    if not problem:
        await message.answer("Опиши проблему текстом.")
        return
    # synthesize a fake CallbackQuery-less flow: build context + run fix.
    uid = message.from_user.id if message.from_user else 0
    issue_id = f"tell{int(time.time()) % 100000}"
    note = await message.answer("🔬 Смотрю, как решают похожее, и зову кодера…")
    from .addons.regen import NoBrainError, generate_fix, research as rresearch, store as rstore

    rstore.start_job(problem[:60])
    ctx = _regen_guess_context(problem)
    research = ""
    with contextlib.suppress(Exception):
        research = await rresearch.research_problem(problem)
    _REGEN_RESEARCH_CACHE[issue_id] = research or ""
    try:
        fix = await generate_fix(problem, code_context=ctx, research=research)
    except NoBrainError as exc:
        rstore.finish_job()
        with contextlib.suppress(Exception):
            await note.edit_text(f"🧠 {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        rstore.finish_job()
        logger.exception("regen tell generate_fix failed")
        with contextlib.suppress(Exception):
            await note.edit_text(f"Кодер не смог: {type(exc).__name__}: {exc}")
        return
    _regen_cache_put(uid, issue_id, fix)
    rstore.finish_job()
    with contextlib.suppress(Exception):
        await note.delete()
    head = [
        "🤖 Кодер написал фикс по твоему описанию.",
        f"Причина: {fix.root_cause[:400] if fix.root_cause else '—'}",
        f"Файлов с готовым кодом: {len(fix.files)} · уверенность: {fix.confidence}",
        "",
        "Выбери формат выдачи ⤵️",
    ]
    await _regen_present_fix(message, uid, issue_id, head)


@wizard_router.message(StateFilter(RegenStates.awaiting_problem))
async def regen_problem_hint(message: Message, state: FSMContext) -> None:
    if not can_use(message.from_user.id if message.from_user else None):
        return
    await message.answer("Опиши проблему <b>текстом</b> — что за кнопка/функция и что не так.")
