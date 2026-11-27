"""Telegram UI for the media brain (img2video / img2img / rmbg).

Two distinct flows live here, both glued together by a single router so
they share callback prefixes (``media:`` for the wizard, ``photo:`` for
the per-photo handler).

1. ``media:*`` callbacks — ``/setup → 🎬 Мозг для видео и фото``
   wizard. Manages up to 3 provider slots (URL + API key + per-task
   model) and which slot is *active* for each task. Slot names are
   auto-derived from the URL (``deapi``, ``fal``, …) and fall back to
   the slot id when the URL is unknown.

2. ``photo:*`` callbacks + ``F.photo`` entry — runs whenever the owner
   sends a photo. Asks Фото/Видео, then drills into a prompt picker
   (img2img) or a quality picker (img2video) or a one-tap background
   removal (img-rmbg). Photo bytes are not cached — the file_id stashed
   in FSM data is re-downloaded on every leaf action (Telegram keeps
   file_id alive long enough for our use).
"""

from __future__ import annotations

import contextlib
import logging

import httpx
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

from .media import (
    KNOWN_MEDIA_PROVIDERS,
    VIDEO_QUALITY_MAP,
    WEAVY_DEFAULT_DURATION,
    WEAVY_DEFAULT_MODEL,
    WEAVY_DEFAULT_QUALITY,
    WEAVY_DURATIONS,
    WEAVY_GROK_MODEL,
    WEAVY_RESOLUTION_MAP,
    MediaError,
    image_to_image,
    image_to_video,
    remove_background,
    weavy_image_to_video,
)
from .media_llm import NoLLMError, generate_prompt_variants, translate_to_english
from .storage import storage

logger = logging.getLogger(__name__)
media_router = Router(name="media")


# ---- helpers -------------------------------------------------------------


def _is_owner(user_id: int | None) -> bool:
    """Backward-compat alias for "can change media settings".

    Media-generation settings (API URL, API key, model slots) used to be
    strictly owner-only. Now they're gated on :func:`bot.access.can_admin`
    so that co-owners and users in ``full_public`` mode can change them
    too.
    """
    from .access import can_admin

    return can_admin(user_id)


def _mask(s: str, *, head: int = 4, tail: int = 4) -> str:
    if not s:
        return "—"
    if len(s) <= head + tail + 1:
        return "***"
    return f"{s[:head]}…{s[-tail:]}"


def _html(s: str) -> str:
    # Avoid pulling handlers._html_escape — keep this file standalone.
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _ProgressShim:
    """Drop-in replacement for an in-chat progress message that routes
    edits through the shared :func:`make_status_runner`.

    The media flows historically did::

        progress_msg = await msg.answer("📥 Качаю фото…")
        await progress_msg.edit_text("…")
        await progress_msg.delete()

    Wrapping that pattern in this shim means the user's chosen
    "Соображалка" style is honoured here too — in ⬛ "чёрная по центру"
    no bubble is rendered at all and progress shows up as Telegram's
    native action indicator at the top of the chat. White / white+model
    keep behaving like before.
    """

    def __init__(self, msg: Message, initial: str) -> None:
        from .addons.thinking_style import make_status_runner
        from .config import DEFAULT_MODEL

        # ⚪ Белая + модель appends "· <model>" after every status line.
        # The user-visible "thinking" steps in media flows are LLM calls
        # (prompt-helper / RU→EN translate), so we surface the same
        # chat model as the main agent uses.
        def _model_hint() -> str:
            return storage.get_model() or DEFAULT_MODEL

        self._update, self._finish = make_status_runner(
            msg, model_hint=_model_hint
        )
        self._initial = initial

    async def open(self) -> _ProgressShim:
        await self._update(self._initial)
        return self

    async def edit_text(self, text: str, **_kwargs) -> None:
        # ``_kwargs`` swallows aiogram-specific kwargs that the runner
        # doesn't care about (parse_mode etc.) — the runner always uses
        # the default HTML parse mode anyway.
        await self._update(text)

    async def delete(self) -> None:
        await self._finish()


async def _open_progress(msg: Message, initial: str) -> _ProgressShim:
    """Build and prime a :class:`_ProgressShim` in one call."""
    shim = _ProgressShim(msg, initial)
    return await shim.open()


# ---- FSM states ----------------------------------------------------------


class MediaWizardStates(StatesGroup):
    awaiting_url = State()
    awaiting_api_key = State()
    awaiting_model_value = State()  # used for video_model / photo_model / rmbg_model
    # Universal custom-request provider (paste a cURL / docs request).
    awaiting_curl = State()          # pasting a cURL command / raw request
    awaiting_result_path = State()   # editing the result JSON path
    awaiting_template_name = State()  # naming a slot's template for the library


class PhotoFlowStates(StatesGroup):
    """FSM states for the photo flow.

    The ``awaiting_*`` states only fire under their own ``StateFilter``
    so plain text outside the flow still reaches the LLM chat handler.
    Variant selection is callback-driven (no text capture); only the
    keywords/prompt text-input branches need a state.
    """

    # Photo branch (img2img).
    awaiting_photo_prompt = State()
    awaiting_photo_keywords = State()
    # Video branch (img2video) — separate states so video prompt entry
    # does not collide with photo prompt entry if user re-uploads.
    awaiting_video_prompt = State()
    awaiting_video_keywords = State()
    # Weavy.ai guided branch — dedicated prompt-entry state so the Weavy
    # panel can capture a free-form prompt without colliding with the
    # plain img2video prompt above.
    awaiting_weavy_prompt = State()
    # Weavy.ai — pasting a custom model id/URL into the model picker.
    awaiting_weavy_model_add = State()
    # Weavy.ai account (browser login via Figma) — one state per field.
    awaiting_weavy_acc_login = State()
    awaiting_weavy_acc_password = State()
    awaiting_weavy_acc_joinlink = State()
    # Main-menu generation entry without an attached photo yet: wait for
    # the user to send one, remembering which branch (photo/video) they
    # picked so we can re-enter it once the image arrives.
    awaiting_photo_upload = State()
    # "⚙️ Настроить" guided provider setup (shared by photo + video):
    # user types model+site in plain words, the bot scrapes docs to find
    # the exact model id / API URL, then captures the API key.
    awaiting_cfg_modelsite = State()
    awaiting_cfg_apikey = State()


# ---- keyboards: media wizard --------------------------------------------


def _slot_summary_line(slot_id: str) -> str:
    slot = storage.get_media_slot(slot_id)
    if slot is None:
        return f"<b>{slot_id}.</b> <i>пусто</i>"
    name = slot.name or slot_id
    parts: list[str] = []
    if slot.is_custom:
        from .media_custom import template_summary

        parts.append("⚡ <b>custom</b>: <code>" + _html(template_summary(slot.custom)[:80]) + "</code>")
        if slot.video_model or slot.photo_model or slot.rmbg_model:
            mdl = slot.video_model or slot.photo_model or slot.rmbg_model
            parts.append(f"model: <code>{_html(mdl)}</code>")
        return f"<b>{slot_id} ({_html(name)}):</b> " + " · ".join(parts)
    parts.append(f"URL: <code>{_html(slot.url) if slot.url else '—'}</code>")
    parts.append(f"key: <code>{_mask(slot.api_key)}</code>")
    if slot.video_model:
        parts.append(f"🎬 <code>{_html(slot.video_model)}</code>")
    if slot.photo_model:
        parts.append(f"📷 <code>{_html(slot.photo_model)}</code>")
    if slot.rmbg_model:
        parts.append(f"🪄 <code>{_html(slot.rmbg_model)}</code>")
    return f"<b>{slot_id} ({_html(name)}):</b> " + " · ".join(parts)


def _active_summary_line() -> str:
    parts: list[str] = []
    for task in ("video", "photo", "rmbg"):
        slot = storage.get_active_media_slot(task)
        label = {"video": "🎬", "photo": "📷", "rmbg": "🪄"}[task]
        if slot is None:
            parts.append(f"{label}<i>—</i>")
        else:
            parts.append(f"{label}<b>{_html(slot.name or slot.slot_id)}</b>")
    return "Активные слоты: " + " | ".join(parts)


def _kb_media_menu() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for sid in storage.MEDIA_SLOT_IDS:
        slot = storage.get_media_slot(sid)
        if slot is None:
            label = f"⚪️ Слот {sid} — пусто"
        else:
            marker = "✅" if slot.configured else "⚙️"
            name = slot.name or sid
            label = f"{marker} Слот {sid} — {name}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"media:slot:{sid}")])
    rows.append([InlineKeyboardButton(text="↩️  Назад к мозгам", callback_data="media:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_media_slot(slot_id: str) -> InlineKeyboardMarkup:
    slot = storage.get_media_slot(slot_id)
    url = (slot.url if slot else "") or "—"
    api = _mask(slot.api_key) if slot else "—"
    vmodel = (slot.video_model if slot else "") or "—"
    pmodel = (slot.photo_model if slot else "") or "—"
    rmodel = (slot.rmbg_model if slot else "") or "—"

    active_marks: dict[str, str] = {}
    for task in ("video", "photo", "rmbg"):
        cur = storage.get_active_media_slot(task)
        active_marks[task] = "▶️" if cur is not None and cur.slot_id == slot_id else "⏵"

    is_custom = bool(slot and slot.is_custom)

    rows: list[list[InlineKeyboardButton]] = []
    # Universal "paste a request" entry — always available, top of the slot.
    rows.append([InlineKeyboardButton(
        text=("⚡ Кастом-запрос: настроен ✅" if is_custom else "⚡ Вставить cURL / запрос"),
        callback_data=f"media:curl:{slot_id}",
    )])
    if is_custom:
        rows.append([
            InlineKeyboardButton(text="🧪 Тест", callback_data=f"media:test:{slot_id}"),
            InlineKeyboardButton(text="🎯 result_path", callback_data=f"media:respath:{slot_id}"),
        ])
        rows.append([
            InlineKeyboardButton(text="💾 В шаблоны", callback_data=f"media:tplsave:{slot_id}"),
            InlineKeyboardButton(text="✖️ Убрать кастом", callback_data=f"media:customclear:{slot_id}"),
        ])
    rows.append([InlineKeyboardButton(text="📚 Шаблоны", callback_data=f"media:tplmenu:{slot_id}")])

    rows += [
        [InlineKeyboardButton(text=f"🌐 URL: {url[:40]}", callback_data=f"media:edit:{slot_id}:url")],
        [InlineKeyboardButton(text=f"🔑 API key: {api}", callback_data=f"media:edit:{slot_id}:api_key")],
        [InlineKeyboardButton(text=f"🎬 Видео-модель: {vmodel[:30]}", callback_data=f"media:model:{slot_id}:video_model")],
        [InlineKeyboardButton(text=f"📷 Фото-модель: {pmodel[:30]}", callback_data=f"media:model:{slot_id}:photo_model")],
        [InlineKeyboardButton(text=f"🪄 Rmbg-модель: {rmodel[:30]}", callback_data=f"media:model:{slot_id}:rmbg_model")],
        [
            InlineKeyboardButton(text=f"{active_marks['video']} использовать для видео", callback_data=f"media:active:{slot_id}:video"),
        ],
        [
            InlineKeyboardButton(text=f"{active_marks['photo']} использовать для фото", callback_data=f"media:active:{slot_id}:photo"),
        ],
        [
            InlineKeyboardButton(text=f"{active_marks['rmbg']} использовать для убрать-фон", callback_data=f"media:active:{slot_id}:rmbg"),
        ],
        [InlineKeyboardButton(text="🗑 Очистить слот", callback_data=f"media:delete:{slot_id}")],
        [InlineKeyboardButton(text="↩️  Назад к слотам", callback_data="media:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_model_picker(slot_id: str, field: str) -> InlineKeyboardMarkup:
    """Show preset model slugs from KNOWN_MEDIA_PROVIDERS + 'свой slug'."""
    slot = storage.get_media_slot(slot_id)
    provider = (slot.name if slot else "") or "deapi"
    meta = KNOWN_MEDIA_PROVIDERS.get(provider) or KNOWN_MEDIA_PROVIDERS["deapi"]
    # ``field`` is e.g. ``video_model`` → look at ``video_models`` key in meta.
    list_key = field.replace("_model", "_models")
    options: list[tuple[str, str]] = meta.get(list_key, [])  # type: ignore[arg-type]
    rows: list[list[InlineKeyboardButton]] = []
    for slug, hint in options:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{slug} — {hint[:40]}",
                    callback_data=f"media:setmodel:{slot_id}:{field}:{slug}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✏️ Свой slug сообщением", callback_data=f"media:askmodel:{slot_id}:{field}")])
    rows.append([InlineKeyboardButton(text="↩️  Назад", callback_data=f"media:slot:{slot_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _media_menu_text() -> str:
    parts = ["<b>🎬 Мозг для видео и фото</b>", ""]
    parts.append(_active_summary_line())
    parts.append("")
    for sid in storage.MEDIA_SLOT_IDS:
        parts.append(_slot_summary_line(sid))
    parts.append("")
    parts.append("<i>Заполни любой слот (URL + API key + минимум одна модель). "
                 "Затем сделай слот активным для нужной задачи кнопкой ▶️.</i>")
    return "\n".join(parts)


# ---- callbacks: media wizard --------------------------------------------


@media_router.callback_query(F.data == "media:menu")
async def cb_media_menu(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        await query.message.edit_text(
            _media_menu_text(),
            reply_markup=_kb_media_menu(),
            disable_web_page_preview=True,
        )
    await query.answer()


@media_router.callback_query(F.data == "media:back")
async def cb_media_back(query: CallbackQuery, state: FSMContext) -> None:
    # Lazy import to avoid circular dependency with wizard.py.
    from .wizard import _kb_brain  # type: ignore[attr-defined]

    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        await query.message.edit_text("Выбери мозг:", reply_markup=_kb_brain())
    await query.answer()


@media_router.callback_query(F.data.startswith("media:slot:"))
async def cb_media_slot(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot_id = (query.data or "").split(":")[-1]
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Нет такого слота.", show_alert=True)
        return
    await state.clear()
    slot = storage.get_media_slot(slot_id)
    name = (slot.name if slot else "") or slot_id
    if query.message is not None:
        await query.message.edit_text(
            f"<b>Слот {slot_id} ({_html(name)})</b>\n\n"
            f"{_slot_summary_line(slot_id)}\n\n"
            "<i>Заполни URL, API key и хотя бы одну модель. "
            "После заполнения URL слот сам определит провайдера: "
            "deAPI, OpenAI / OpenAI-совместимый, fal.ai или Replicate. "
            "Любой неизвестный URL трактуется как OpenAI-совместимый — "
            "никаких принудительных deAPI больше нет.</i>",
            reply_markup=_kb_media_slot(slot_id),
            disable_web_page_preview=True,
        )
    await query.answer()


@media_router.callback_query(F.data.startswith("media:edit:"))
async def cb_media_edit(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Bad callback.", show_alert=True)
        return
    _, _, slot_id, field = parts
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Нет такого слота.", show_alert=True)
        return
    await state.update_data(slot_id=slot_id, field=field)
    if field == "url":
        await state.set_state(MediaWizardStates.awaiting_url)
        prompt = (
            f"Пришли URL для слота <b>{slot_id}</b> одним сообщением.\n\n"
            "Примеры:\n"
            "• <code>https://api.deapi.ai</code>\n"
            "• <code>https://api.openai.com</code>\n"
            "• <code>https://fal.run</code>\n"
            "• <code>https://api.replicate.com</code>\n"
            "• <code>https://&lt;твой-OpenAI-совместимый-хост&gt;</code>\n\n"
            "<i>Слот автоматически получит имя по URL. "
            "Если URL не из известного списка — слот станет "
            "<b>openai_compatible</b> и будет ходить по эндпоинтам "
            "<code>/v1/images/edits</code> и <code>/v1/images/generations</code>.</i>"
        )
    elif field == "api_key":
        await state.set_state(MediaWizardStates.awaiting_api_key)
        prompt = (
            f"Пришли API key для слота <b>{slot_id}</b> одним сообщением. "
            "Я удалю твоё сообщение сразу после сохранения."
        )
    else:
        await query.answer("Bad field.", show_alert=True)
        return
    if query.message is not None:
        await query.message.answer(prompt, disable_web_page_preview=True)
    await query.answer()


@media_router.callback_query(F.data.startswith("media:model:"))
async def cb_media_model(query: CallbackQuery, state: FSMContext) -> None:
    """Show the model picker for video_model / photo_model / rmbg_model."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Bad callback.", show_alert=True)
        return
    _, _, slot_id, field = parts
    if slot_id not in storage.MEDIA_SLOT_IDS or field not in (
        "video_model", "photo_model", "rmbg_model",
    ):
        await query.answer("Bad slot/field.", show_alert=True)
        return
    await state.clear()
    slot = storage.get_media_slot(slot_id)
    provider = (slot.name if slot else "") or "deapi"
    meta = KNOWN_MEDIA_PROVIDERS.get(provider) or KNOWN_MEDIA_PROVIDERS["deapi"]
    list_key = field.replace("_model", "_models")
    options = meta.get(list_key, [])
    field_label = {
        "video_model": "🎬 видео-модели",
        "photo_model": "📷 фото-модели (img2img)",
        "rmbg_model": "🪄 модели убрать-фон",
    }[field]
    text_lines = [
        f"<b>Слот {slot_id} ({_html(provider)}): выбор {field_label}</b>",
        "",
        meta.get("hint", ""),
        "",
        f"Подходящие модели для <b>{provider}</b>:" if options else
        f"Для провайдера <b>{provider}</b> у меня нет каталога — кинь slug сообщением.",
    ]
    for slug, hint in options:
        text_lines.append(f"• <code>{_html(slug)}</code> — {_html(hint)}")
    if query.message is not None:
        await query.message.edit_text(
            "\n".join(text_lines),
            reply_markup=_kb_model_picker(slot_id, field),
            disable_web_page_preview=True,
        )
    await query.answer()


@media_router.callback_query(F.data.startswith("media:setmodel:"))
async def cb_media_setmodel(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    # callback_data shape: media:setmodel:<slot>:<field>:<slug>
    parts = (query.data or "").split(":", 4)
    if len(parts) != 5:
        await query.answer("Bad callback.", show_alert=True)
        return
    _, _, slot_id, field, slug = parts
    if slot_id not in storage.MEDIA_SLOT_IDS or field not in (
        "video_model", "photo_model", "rmbg_model",
    ):
        await query.answer("Bad slot/field.", show_alert=True)
        return
    storage.set_media_slot_field(slot_id, field, slug)
    if query.message is not None:
        await query.message.edit_text(
            f"<b>Слот {slot_id}</b> — {field} = <code>{_html(slug)}</code>\n\n"
            + _slot_summary_line(slot_id),
            reply_markup=_kb_media_slot(slot_id),
            disable_web_page_preview=True,
        )
    await query.answer("Сохранено")


@media_router.callback_query(F.data.startswith("media:askmodel:"))
async def cb_media_askmodel(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Bad callback.", show_alert=True)
        return
    _, _, slot_id, field = parts
    if slot_id not in storage.MEDIA_SLOT_IDS or field not in (
        "video_model", "photo_model", "rmbg_model",
    ):
        await query.answer("Bad slot/field.", show_alert=True)
        return
    await state.set_state(MediaWizardStates.awaiting_model_value)
    await state.update_data(slot_id=slot_id, field=field)
    field_label = {
        "video_model": "🎬 видео-модели",
        "photo_model": "📷 фото-модели",
        "rmbg_model": "🪄 модели убрать-фон",
    }[field]
    if query.message is not None:
        await query.message.answer(
            f"Пришли slug {field_label} для слота <b>{slot_id}</b> одним сообщением.",
            disable_web_page_preview=True,
        )
    await query.answer()


@media_router.callback_query(F.data.startswith("media:active:"))
async def cb_media_active(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Bad callback.", show_alert=True)
        return
    _, _, slot_id, task = parts
    if slot_id not in storage.MEDIA_SLOT_IDS or task not in storage.MEDIA_TASKS:
        await query.answer("Bad callback.", show_alert=True)
        return
    slot = storage.get_media_slot(slot_id)
    if slot is None or not slot.configured:
        await query.answer("Слот пуст — заполни URL и API key.", show_alert=True)
        return
    storage.set_active_media_slot(task, slot_id)
    if query.message is not None:
        await query.message.edit_text(
            f"<b>Слот {slot_id}</b> назначен активным для <b>{task}</b>.\n\n"
            + _slot_summary_line(slot_id),
            reply_markup=_kb_media_slot(slot_id),
            disable_web_page_preview=True,
        )
    await query.answer("Активирован")


@media_router.callback_query(F.data.startswith("media:delete:"))
async def cb_media_delete(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot_id = (query.data or "").split(":")[-1]
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Bad slot.", show_alert=True)
        return
    storage.delete_media_slot(slot_id)
    if query.message is not None:
        await query.message.edit_text(
            f"Слот {slot_id} очищен.\n\n" + _media_menu_text(),
            reply_markup=_kb_media_menu(),
            disable_web_page_preview=True,
        )
    await query.answer("Удалён")


# ---- text capture for media wizard --------------------------------------

_NOT_A_COMMAND = F.text & ~F.text.startswith("/")


@media_router.message(StateFilter(MediaWizardStates.awaiting_url), _NOT_A_COMMAND)
async def capture_media_url(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    url = (message.text or "").strip()
    if not url.startswith(("http://", "https://")):
        await message.answer(
            "URL должен начинаться с <code>http://</code> или <code>https://</code>. Попробуй ещё раз."
        )
        return
    data = await state.get_data()
    slot_id = str(data.get("slot_id") or "")
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await message.answer("Потерял контекст слота — открой /setup → 🎬 Мозг для видео и фото заново.")
        await state.clear()
        return
    storage.set_media_slot_field(slot_id, "url", url.rstrip("/"))
    await state.clear()
    await message.answer(
        f"URL сохранён для слота {slot_id}.\n\n" + _slot_summary_line(slot_id),
        reply_markup=_kb_media_slot(slot_id),
        disable_web_page_preview=True,
    )


@media_router.message(StateFilter(MediaWizardStates.awaiting_api_key), _NOT_A_COMMAND)
async def capture_media_api_key(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    key = (message.text or "").strip()
    if not key:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    data = await state.get_data()
    slot_id = str(data.get("slot_id") or "")
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await message.answer("Потерял контекст слота — открой /setup → 🎬 Мозг для видео и фото заново.")
        await state.clear()
        return
    storage.set_media_slot_field(slot_id, "api_key", key)
    with contextlib.suppress(Exception):
        await message.delete()
    await state.clear()
    await message.answer(
        f"API key сохранён для слота {slot_id}, твоё сообщение удалено.\n\n"
        + _slot_summary_line(slot_id),
        reply_markup=_kb_media_slot(slot_id),
        disable_web_page_preview=True,
    )


@media_router.message(StateFilter(MediaWizardStates.awaiting_model_value), _NOT_A_COMMAND)
async def capture_media_model(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    slug = (message.text or "").strip()
    if not slug:
        await message.answer("Пустое сообщение, попробуй ещё раз.")
        return
    data = await state.get_data()
    slot_id = str(data.get("slot_id") or "")
    field = str(data.get("field") or "")
    if slot_id not in storage.MEDIA_SLOT_IDS or field not in (
        "video_model", "photo_model", "rmbg_model",
    ):
        await message.answer("Потерял контекст — открой /setup → 🎬 Мозг для видео и фото заново.")
        await state.clear()
        return
    storage.set_media_slot_field(slot_id, field, slug)
    await state.clear()
    await message.answer(
        f"{field} = <code>{_html(slug)}</code> сохранено для слота {slot_id}.\n\n"
        + _slot_summary_line(slot_id),
        reply_markup=_kb_media_slot(slot_id),
        disable_web_page_preview=True,
    )


# ---- universal custom-request provider (paste cURL / docs request) ------

_CURL_HELP = (
    "<b>⚡ Универсальный слот — вставь cURL любого сайта</b>\n\n"
    "Пришли одним сообщением команду <code>curl …</code> из документации "
    "провайдера (fal, replicate, Weavy, kling, runway, любой свой API). "
    "Я разберу метод, URL, заголовки и тело, и сам подставлю плейсхолдеры "
    "там, где узнаю поля <i>prompt / image / model</i>.\n\n"
    "Если хочешь управлять подстановкой вручную — поставь в теле токены:\n"
    "<code>{{PROMPT}}</code> · <code>{{NEGATIVE_PROMPT}}</code> · "
    "<code>{{MODEL}}</code> · <code>{{API_KEY}}</code>\n"
    "<code>{{IMAGE_DATA_URL}}</code> (data:…;base64,…) · "
    "<code>{{IMAGE_B64}}</code> (чистый base64) · <code>{{IMAGE_URL}}</code>\n"
    "<code>{{WIDTH}}</code> · <code>{{HEIGHT}}</code> · <code>{{FPS}}</code> · "
    "<code>{{FRAMES}}</code> · <code>{{STEPS}}</code> · <code>{{SEED}}</code> · "
    "<code>{{DURATION}}</code> · <code>{{QUALITY}}</code> · <code>{{ASPECT}}</code>\n\n"
    "Если у API асинхронная очередь (submit → poll), это тоже ок: я найду "
    "URL статуса в ответе и буду опрашивать его до готового результата.\n\n"
    "<b>Пример:</b>\n"
    "<code>curl https://api.example.com/v1/generate \\\n"
    "  -H 'Authorization: Bearer KEY' -H 'Content-Type: application/json' \\\n"
    "  -d '{\"model\":\"my-model\",\"prompt\":\"hello\",\"image\":\"...\"}'</code>"
)


@media_router.callback_query(F.data.startswith("media:curl:"))
async def cb_media_curl(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot_id = (query.data or "").split(":")[-1]
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Нет такого слота.", show_alert=True)
        return
    await state.set_state(MediaWizardStates.awaiting_curl)
    await state.update_data(slot_id=slot_id)
    if query.message is not None:
        await query.message.answer(_CURL_HELP, disable_web_page_preview=True)
    await query.answer()


@media_router.message(StateFilter(MediaWizardStates.awaiting_curl), _NOT_A_COMMAND)
async def capture_media_curl(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    from .media_custom import (
        CustomMediaError,
        auto_tokenize,
        parse_curl,
        template_summary,
    )

    text = message.text or message.caption or ""
    data = await state.get_data()
    slot_id = str(data.get("slot_id") or "")
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await message.answer("Потерял контекст слота — открой /setup → 🎬 Мозг для видео и фото заново.")
        await state.clear()
        return
    try:
        custom = parse_curl(text)
    except CustomMediaError as exc:
        await message.answer(f"⚠️ {_html(str(exc))}\n\nПопробуй вставить полную команду curl ещё раз.")
        return
    custom, inserted = auto_tokenize(custom)
    storage.set_media_slot_custom(slot_id, custom)
    await state.clear()

    note = ""
    if inserted:
        note = "\n\n🔁 Подставил автоматически: " + ", ".join(f"<code>{t}</code>" for t in inserted)
    else:
        has_img = "{{IMAGE" in (custom.get("body") or "")
        has_prompt = "{{PROMPT}}" in (custom.get("body") or "")
        if not (has_img or has_prompt):
            note = (
                "\n\n⚠️ Не нашёл, куда вставлять prompt/картинку. Добавь в тело "
                "токены <code>{{PROMPT}}</code> и <code>{{IMAGE_DATA_URL}}</code> "
                "и пришли cURL ещё раз, либо проверь через 🧪 Тест."
            )
    await message.answer(
        f"✅ Кастом-запрос сохранён для слота <b>{slot_id}</b>:\n"
        f"<code>{_html(template_summary(custom))}</code>{note}\n\n"
        "Не забудь сделать слот активным (▶️) для нужной задачи.\n\n"
        + _slot_summary_line(slot_id),
        reply_markup=_kb_media_slot(slot_id),
        disable_web_page_preview=True,
    )


@media_router.callback_query(F.data.startswith("media:customclear:"))
async def cb_media_customclear(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot_id = (query.data or "").split(":")[-1]
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Bad slot.", show_alert=True)
        return
    storage.set_media_slot_custom(slot_id, None)
    if query.message is not None:
        await query.message.edit_text(
            f"Кастом-запрос убран со слота {slot_id} (URL/ключ/модели остались).\n\n"
            + _slot_summary_line(slot_id),
            reply_markup=_kb_media_slot(slot_id),
            disable_web_page_preview=True,
        )
    await query.answer("Убрано")


@media_router.callback_query(F.data.startswith("media:respath:"))
async def cb_media_respath(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot_id = (query.data or "").split(":")[-1]
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Bad slot.", show_alert=True)
        return
    cur = storage.get_media_slot_custom(slot_id).get("result_path") or "(авто)"
    await state.set_state(MediaWizardStates.awaiting_result_path)
    await state.update_data(slot_id=slot_id)
    if query.message is not None:
        await query.message.answer(
            f"Текущий <b>result_path</b>: <code>{_html(cur)}</code>\n\n"
            "Пришли путь до URL результата в JSON-ответе, например:\n"
            "<code>data.output[0].url</code> · <code>video.url</code> · "
            "<code>images[0].url</code>\n\n"
            "Пришли <code>-</code> чтобы вернуть авто-поиск URL.",
            disable_web_page_preview=True,
        )
    await query.answer()


@media_router.message(StateFilter(MediaWizardStates.awaiting_result_path), _NOT_A_COMMAND)
async def capture_media_result_path(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    path = (message.text or "").strip()
    data = await state.get_data()
    slot_id = str(data.get("slot_id") or "")
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await message.answer("Потерял контекст слота.")
        await state.clear()
        return
    custom = storage.get_media_slot_custom(slot_id)
    if not custom:
        await message.answer("На слоте нет кастом-запроса.")
        await state.clear()
        return
    custom["result_path"] = "" if path in ("-", "") else path
    storage.set_media_slot_custom(slot_id, custom)
    await state.clear()
    await message.answer(
        f"result_path обновлён для слота {slot_id}: "
        f"<code>{_html(custom['result_path'] or '(авто)')}</code>",
        reply_markup=_kb_media_slot(slot_id),
        disable_web_page_preview=True,
    )


@media_router.callback_query(F.data.startswith("media:test:"))
async def cb_media_test(query: CallbackQuery, state: FSMContext) -> None:
    """Dry-run preview: resolve placeholders and show the request we'd send.

    Safe — sends nothing over the network. Uses a tiny dummy image and a
    sample prompt so the owner can verify the tokens resolved correctly.
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot_id = (query.data or "").split(":")[-1]
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Bad slot.", show_alert=True)
        return
    from .media_custom import build_context, build_request

    slot = storage.get_media_slot(slot_id)
    if slot is None or not slot.is_custom:
        await query.answer("На слоте нет кастом-запроса.", show_alert=True)
        return
    ctx = build_context(
        image_bytes=b"\xff\xd8\xff\xe0TESTIMAGE",
        prompt="cinematic gentle motion, soft light",
        model=slot.video_model or slot.photo_model or slot.rmbg_model or "MODEL",
        api_key=slot.api_key or "API_KEY",
        width=768, height=768, frames=60, fps=30, steps=8, duration=5,
        quality="720", aspect="16:9",
    )
    req = build_request(slot.custom, ctx)
    # Mask Authorization for the preview.
    safe_headers = {}
    for k, v in (req.get("headers") or {}).items():
        safe_headers[k] = _mask(v, head=8, tail=4) if k.lower() == "authorization" else v
    import json as _json

    if "json" in req:
        body_preview = _json.dumps(req["json"], ensure_ascii=False)[:600]
    elif "content" in req:
        body_preview = req["content"][:600].decode("utf-8", "replace")
    elif "data" in req:
        body_preview = _json.dumps(req["data"], ensure_ascii=False)[:600]
    else:
        body_preview = "(пусто)"
    txt = (
        f"<b>🧪 Превью запроса слота {slot_id}</b> (ничего не отправлено)\n\n"
        f"<b>{req['method']}</b> <code>{_html(req['url'])}</code>\n\n"
        f"<b>Headers:</b>\n<code>{_html(_json.dumps(safe_headers, ensure_ascii=False))[:500]}</code>\n\n"
        f"<b>Body:</b>\n<code>{_html(body_preview)}</code>\n\n"
        f"<b>result_path:</b> <code>{_html(slot.custom.get('result_path') or '(авто-поиск URL)')}</code>\n"
        f"<b>poll:</b> <code>{'да' if slot.custom.get('poll') else 'нет (синхронно)'}</code>"
    )
    if query.message is not None:
        await query.message.answer(txt, disable_web_page_preview=True)
    await query.answer("Превью готово")


@media_router.callback_query(F.data.startswith("media:tplsave:"))
async def cb_media_tplsave(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot_id = (query.data or "").split(":")[-1]
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Bad slot.", show_alert=True)
        return
    if not storage.get_media_slot_custom(slot_id):
        await query.answer("На слоте нет кастом-запроса.", show_alert=True)
        return
    await state.set_state(MediaWizardStates.awaiting_template_name)
    await state.update_data(slot_id=slot_id)
    if query.message is not None:
        await query.message.answer(
            "Пришли имя для шаблона (например <code>kling-i2v</code> или "
            "<code>runway-gen3</code>). Он попадёт в 📚 Шаблоны и его можно "
            "будет загрузить в любой слот.",
            disable_web_page_preview=True,
        )
    await query.answer()


@media_router.message(StateFilter(MediaWizardStates.awaiting_template_name), _NOT_A_COMMAND)
async def capture_media_template_name(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    name = (message.text or "").strip()
    data = await state.get_data()
    slot_id = str(data.get("slot_id") or "")
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await message.answer("Потерял контекст слота.")
        await state.clear()
        return
    custom = storage.get_media_slot_custom(slot_id)
    if not custom:
        await message.answer("На слоте нет кастом-запроса.")
        await state.clear()
        return
    if not name:
        await message.answer("Пустое имя, попробуй ещё раз.")
        return
    name = name.replace(":", "-")[:40]
    storage.save_media_template(name, custom)
    await state.clear()
    await message.answer(
        f"💾 Шаблон <b>{_html(name)}</b> сохранён в библиотеку. "
        "Загрузить его в любой слот можно через 📚 Шаблоны.",
        reply_markup=_kb_media_slot(slot_id),
        disable_web_page_preview=True,
    )


def _kb_templates(slot_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Built-in presets from the catalogue stay available as starting points.
    for key, meta in KNOWN_MEDIA_PROVIDERS.items():
        label = meta.get("label") or key
        if meta.get("url"):
            rows.append([InlineKeyboardButton(
                text=f"📦 {label}",
                callback_data=f"media:tplbuiltin:{slot_id}:{key}",
            )])
    saved = storage.list_media_templates()
    for name in saved:
        rows.append([
            InlineKeyboardButton(text=f"⭐ {name}", callback_data=f"media:tplload:{slot_id}:{name}"),
            InlineKeyboardButton(text="🗑", callback_data=f"media:tpldel:{slot_id}:{name}"),
        ])
    rows.append([InlineKeyboardButton(text="↩️  Назад", callback_data=f"media:slot:{slot_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@media_router.callback_query(F.data.startswith("media:tplmenu:"))
async def cb_media_tplmenu(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    slot_id = (query.data or "").split(":")[-1]
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Bad slot.", show_alert=True)
        return
    saved = storage.list_media_templates()
    txt = (
        "<b>📚 Шаблоны</b>\n\n"
        "📦 — встроенные пресеты провайдеров (заполнят URL/модели слота).\n"
        "⭐ — твои сохранённые cURL-шаблоны.\n\n"
        f"Сохранено своих: <b>{len(saved)}</b>. Выбери шаблон, чтобы загрузить "
        f"его в слот <b>{slot_id}</b>."
    )
    if query.message is not None:
        await query.message.edit_text(txt, reply_markup=_kb_templates(slot_id), disable_web_page_preview=True)
    await query.answer()


@media_router.callback_query(F.data.startswith("media:tplload:"))
async def cb_media_tplload(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    parts = (query.data or "").split(":", 3)
    if len(parts) != 4:
        await query.answer("Bad callback.", show_alert=True)
        return
    _, _, slot_id, name = parts
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Bad slot.", show_alert=True)
        return
    tpl = storage.get_media_template(name)
    if not tpl:
        await query.answer("Шаблон не найден.", show_alert=True)
        return
    storage.set_media_slot_custom(slot_id, tpl)
    if query.message is not None:
        await query.message.edit_text(
            f"⭐ Шаблон <b>{_html(name)}</b> загружен в слот {slot_id}.\n\n"
            + _slot_summary_line(slot_id),
            reply_markup=_kb_media_slot(slot_id),
            disable_web_page_preview=True,
        )
    await query.answer("Загружено")


@media_router.callback_query(F.data.startswith("media:tpldel:"))
async def cb_media_tpldel(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    parts = (query.data or "").split(":", 3)
    if len(parts) != 4:
        await query.answer("Bad callback.", show_alert=True)
        return
    _, _, slot_id, name = parts
    storage.delete_media_template(name)
    if query.message is not None:
        await query.message.edit_text(
            f"Шаблон <b>{_html(name)}</b> удалён.",
            reply_markup=_kb_templates(slot_id),
            disable_web_page_preview=True,
        )
    await query.answer("Удалён")


@media_router.callback_query(F.data.startswith("media:tplbuiltin:"))
async def cb_media_tplbuiltin(query: CallbackQuery, state: FSMContext) -> None:
    """Seed a slot from a built-in provider preset (URL + model hints).

    This keeps the classic templates working and lets the owner start from
    a known provider, then optionally paste a cURL on top.
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    parts = (query.data or "").split(":", 3)
    if len(parts) != 4:
        await query.answer("Bad callback.", show_alert=True)
        return
    _, _, slot_id, key = parts
    if slot_id not in storage.MEDIA_SLOT_IDS:
        await query.answer("Bad slot.", show_alert=True)
        return
    meta = KNOWN_MEDIA_PROVIDERS.get(key)
    if not meta:
        await query.answer("Нет такого пресета.", show_alert=True)
        return
    if meta.get("url"):
        storage.set_media_slot_field(slot_id, "url", meta["url"])
    # Seed the first model hint for each task, if any.
    for list_key, field in (("video_models", "video_model"),
                            ("photo_models", "photo_model"),
                            ("rmbg_models", "rmbg_model")):
        opts = meta.get(list_key) or []
        if opts:
            storage.set_media_slot_field(slot_id, field, opts[0][0])
    if query.message is not None:
        await query.message.edit_text(
            f"📦 Пресет <b>{_html(meta.get('label') or key)}</b> загружен в слот {slot_id}. "
            "Допиши API key (и при желании вставь cURL поверх).\n\n"
            + _slot_summary_line(slot_id),
            reply_markup=_kb_media_slot(slot_id),
            disable_web_page_preview=True,
        )
    await query.answer("Загружено")


# ---- photo flow entry ---------------------------------------------------


def _kb_photo_kind() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📷 Фото", callback_data="photo:kind:photo"),
                InlineKeyboardButton(text="🎬 Видео", callback_data="photo:kind:video"),
            ],
            [InlineKeyboardButton(text="❌ Назад", callback_data="photo:cancel")],
        ]
    )


def _swapface_toggle_row(kind: str) -> list[InlineKeyboardButton]:
    """Тумблер и быстрый вход в лица/Discord на экранах фото и видео."""
    label = "🎭 SwapFace: выкл"
    try:
        from .addons.swapface import service as _swf

        if _swf.is_enabled(kind):
            label = "🎭 SwapFace: вкл"
    except Exception:  # noqa: BLE001
        pass
    return [
        InlineKeyboardButton(text=label, callback_data=f"photo:swf:{kind}"),
        InlineKeyboardButton(text="⚙️ Лица / Discord", callback_data="swf:menu"),
    ]


def _kb_photo_action() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Указ (LLM-помощник)", callback_data="photo:photo:idea")],
            [InlineKeyboardButton(text="⚙️ Настроить (модель/сайт/API)", callback_data="mcfg:start:photo")],
            [InlineKeyboardButton(text="🪄 Убрать фон", callback_data="photo:photo:rmbg")],
            _swapface_toggle_row("photo"),
            [InlineKeyboardButton(text="↩️ Вернуться", callback_data="photo:back:kind")],
        ]
    )


def _kb_variants(branch: str, count: int) -> InlineKeyboardMarkup:
    """3-variant picker. ``branch`` is ``"photo"`` or ``"video"``.

    Variant texts are NOT in callback_data (Telegram 64-byte limit) —
    instead the picked index goes back into the handler which reads
    the cached variants from FSM data.
    """
    numbers = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"{numbers[i]} Вариант {i + 1}",
                callback_data=f"photo:{branch}:var:{i}",
            )
        ]
        for i in range(min(count, len(numbers)))
    ]
    rows.append(
        [
            InlineKeyboardButton(text="🔁 Ещё варианты", callback_data=f"photo:{branch}:idea"),
            InlineKeyboardButton(text="↩️ Вернуться", callback_data=f"photo:back:{branch}_root"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_video_prompt() -> InlineKeyboardMarkup:
    """Pre-quality screen for the video branch: prompt entry options."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Указ (LLM-помощник)", callback_data="photo:video:idea")],
            [InlineKeyboardButton(text="⚙️ Настроить (модель/сайт/API)", callback_data="mcfg:start:video")],
            [InlineKeyboardButton(text="🌊 Weavy.ai", callback_data="weavy:open")],
            [InlineKeyboardButton(text="🎞 Анализ динамики видео", callback_data="viddyn:open")],
            [InlineKeyboardButton(text="🧰 GitHub-инструменты анализа", callback_data="ghtools:open")],
            [InlineKeyboardButton(text="→ Пропустить (caption/дефолт)", callback_data="photo:video:skip_prompt")],
            _swapface_toggle_row("video"),
            [InlineKeyboardButton(text="↩️ Вернуться", callback_data="photo:back:kind")],
        ]
    )


def _kb_video_quality() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="480", callback_data="photo:video:q:480"),
                InlineKeyboardButton(text="720", callback_data="photo:video:q:720"),
                InlineKeyboardButton(text="1080", callback_data="photo:video:q:1080"),
            ],
            [
                InlineKeyboardButton(text="2К", callback_data="photo:video:q:2k"),
                InlineKeyboardButton(text="Авто", callback_data="photo:video:q:auto"),
            ],
            [InlineKeyboardButton(text="🎞 GIF", callback_data="photo:video:q:gif")],
            [InlineKeyboardButton(text="↩️ Вернуться", callback_data="photo:back:video_root")],
        ]
    )


# NOTE: the auto-``F.photo`` entry point used to live here. It now sits
# in :mod:`bot.addons.photo_chooser` which asks the user where to route
# the photo (Helpzavr vs generation flow) before anything reacts. When
# the user picks "🎬 Генерация фото и видео" the chooser stashes the
# ``photo_file_id`` / ``photo_chat_id`` / ``caption_prompt`` FSM data
# this module's callbacks already expect, then re-shows ``_kb_photo_kind``.
# So this file's photo flow is unchanged — it just no longer competes
# with Helpzavr for an incoming photo.


@media_router.callback_query(F.data.startswith("photo:swf:"))
async def cb_photo_swapface_toggle(query: CallbackQuery) -> None:
    """Тумблер SwapFace на экранах фото/видео (ТЗ SwapFace §9.3)."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только для владельца", show_alert=True)
        return
    kind = (query.data or "").rsplit(":", 1)[-1]
    try:
        from .addons.swapface import service as _swf
        from .addons.swapface import state as _swf_state

        if kind == "video":
            _swf_state.set_enabled_video(not _swf_state.enabled_video())
        else:
            _swf_state.set_enabled_photo(not _swf_state.enabled_photo())
        enabled = _swf.is_enabled(kind)
    except Exception:  # noqa: BLE001
        await query.answer("SwapFace недоступен", show_alert=True)
        return
    note = ""
    if enabled:
        ready, reason = _swf.readiness(kind)
        if not ready:
            note = f" ⚠️ {reason}"
    kb = _kb_video_prompt() if kind == "video" else _kb_photo_action()
    with contextlib.suppress(Exception):
        await query.message.edit_reply_markup(reply_markup=kb)
    await query.answer(
        ("SwapFace включён." if enabled else "SwapFace выключен.") + note,
        show_alert=bool(note),
    )


async def _maybe_swapface(image_bytes: bytes, kind: str, msg: Message) -> bytes:
    """Шаг свапа между генерацией кадра и доставкой/видео (ТЗ SwapFace §9.3).

    Выключен → байты возвращаются как есть (пайплайн не трогается).
    Включён, но не готов → предупреждение и «продолжаем без свапа».
    Готов → preflight кредитов (§5.6) → ``service.swap_image()`` → владелец
    выбирает 1 из 2 вариантов (§5.4); выбранный (полное качество, уже в
    Master Room §6) продолжает пайплайн.
    """
    try:
        from .addons.swapface import handlers as _swf_ui
        from .addons.swapface import service as _swf
        from .addons.swapface import state as _swf_state
    except Exception:  # noqa: BLE001
        return image_bytes
    if not _swf.is_enabled(kind):
        return image_bytes
    ready, reason = _swf.readiness(kind)
    if not ready:
        with contextlib.suppress(Exception):
            await msg.answer(f"🎭 SwapFace пропущен: {_html(reason)} Продолжаю без свапа.")
        return image_bytes
    # §5.6 preflight: предупредить о низких кредитах и дать «Отмена».
    if _swf_state.credits_low():
        remaining = _swf_state.get_credits().get("remaining")
        proceed = await _swf_ui.confirm_low_credits(msg, remaining)
        if not proceed:
            with contextlib.suppress(Exception):
                await msg.answer("🎭 SwapFace отменён — продолжаю без свапа.")
            return image_bytes
    progress = await _open_progress(msg, "🎭 SwapFace: отправляю кадр в Discord…")
    try:
        result = await _swf.swap_image(image_bytes)
    except _swf.SwapFaceError as exc:
        await progress.edit_text(f"🎭 SwapFace: {_html(str(exc))}\nПродолжаю без свапа.")
        return image_bytes
    except Exception:  # noqa: BLE001
        logger.exception("swapface step failed")
        with contextlib.suppress(Exception):
            await progress.edit_text("🎭 SwapFace: внутренняя ошибка. Продолжаю без свапа.")
        return image_bytes
    with contextlib.suppress(Exception):
        await progress.delete()
    if not result.variants:
        return image_bytes
    credits = (
        f" · кредиты: {result.credits_remaining}"
        if result.credits_remaining is not None else ""
    )
    with contextlib.suppress(Exception):
        await msg.answer(
            f"🎭 SwapFace готов{credits}"
            + (" · из кэша" if result.from_cache else "")
            + " — выбери вариант 👇",
        )
    # §5.4/§9.3: интерактивный выбор 1 из 2; None → продолжить без свапа.
    chosen = await _swf_ui.choose_variant(msg, result.variants)
    return chosen if chosen is not None else image_bytes


@media_router.callback_query(F.data == "photo:cancel")
async def cb_photo_cancel(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.delete()
    await query.answer("Отменено")


@media_router.callback_query(F.data == "photo:back:kind")
async def cb_photo_back_kind(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    if query.message is not None:
        await query.message.edit_text(
            "Что делаем с фото?",
            reply_markup=_kb_photo_kind(),
        )
    await query.answer()


@media_router.callback_query(F.data == "photo:back:photo_root")
async def cb_photo_back_photo_root(query: CallbackQuery, state: FSMContext) -> None:
    """Return to the photo-branch root (re-arm prompt capture)."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(PhotoFlowStates.awaiting_photo_prompt)
    if query.message is not None:
        await query.message.edit_text(
            "📷 <b>Фото</b>: введи промпт сообщением, нажми «✨ Указ» для LLM-подсказки или «🪄 Убрать фон» ниже.",
            reply_markup=_kb_photo_action(),
        )
    await query.answer()


@media_router.callback_query(F.data == "photo:back:video_root")
async def cb_photo_back_video_root(query: CallbackQuery, state: FSMContext) -> None:
    """Return to the video-branch root (re-arm prompt entry)."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(PhotoFlowStates.awaiting_video_prompt)
    if query.message is not None:
        await query.message.edit_text(
            "🎬 <b>Видео</b>: напиши промпт для движения, жми «✨ Указ» или «Пропустить».",
            reply_markup=_kb_video_prompt(),
        )
    await query.answer()


_PHOTO_BRANCH_TEXT = (
    "📷 <b>Фото</b>: введи промпт сообщением — что сделать с картинкой, "
    "жми «✨ Указ» чтобы LLM выдал 3 варианта, "
    "или «🪄 Убрать фон» — верну PNG без фона.\n\n"
    "<i>Результат придёт и сжатым превью, и исходным файлом (без сжатия).</i>"
)
_VIDEO_BRANCH_TEXT = (
    "🎬 <b>Видео</b>: напиши промпт для движения сообщением (описание ракурса/эмоции/движения), "
    "жми «✨ Указ» чтобы LLM выдал 3 варианта, или «Пропустить» — "
    "использую caption как промпт (или дефолт если пустой).\n\n"
    "<i>После промпта — выбор качества (включая GIF).</i>"
)


def _kb_ask_photo(kind: str) -> InlineKeyboardMarkup:
    """Keyboard shown while we wait for the user to send a photo."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data="photo:back:kind")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="photo:cancel")],
        ]
    )


async def _ask_for_photo(query: CallbackQuery, state: FSMContext, kind: str) -> None:
    """Prompt the user to send a photo, remembering the chosen branch."""
    await state.set_state(PhotoFlowStates.awaiting_photo_upload)
    await state.update_data(pending_kind=kind)
    label = "📷 Фото" if kind == "photo" else "🎬 Видео"
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"{label}: пришли фото сообщением — и сразу продолжим.\n\n"
                "<i>Как только пришлёшь картинку, открою тот же экран, "
                "что и при отправке фото в чат.</i>",
                reply_markup=_kb_ask_photo(kind),
            )
    await query.answer()


@media_router.callback_query(F.data == "photo:kind:photo")
async def cb_photo_kind_photo(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    # No photo attached yet (entered from the main menu) — ask for one first.
    if not data.get("photo_file_id"):
        await _ask_for_photo(query, state, "photo")
        return
    # Activate the prompt-capture state — text outside this state still
    # reaches the LLM chat handler in the main router.
    await state.set_state(PhotoFlowStates.awaiting_photo_prompt)
    if query.message is not None:
        await query.message.edit_text(
            _PHOTO_BRANCH_TEXT,
            reply_markup=_kb_photo_action(),
        )
    await query.answer()


@media_router.callback_query(F.data == "photo:kind:video")
async def cb_photo_kind_video(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    # No photo attached yet (entered from the main menu) — ask for one first.
    if not data.get("photo_file_id"):
        await _ask_for_photo(query, state, "video")
        return
    # Pre-quality screen: gather/refine the motion prompt first.
    await state.set_state(PhotoFlowStates.awaiting_video_prompt)
    if query.message is not None:
        await query.message.edit_text(
            _VIDEO_BRANCH_TEXT,
            reply_markup=_kb_video_prompt(),
        )
    await query.answer()


@media_router.callback_query(F.data == "photogen:open")
async def cb_photogen_open(query: CallbackQuery, state: FSMContext) -> None:
    """Main-menu entry into the photo/video generation flow.

    Mirrors the per-photo chooser's "🎬 Генерация фото и видео" branch but
    without an attached photo: the user picks Фото/Видео first, and if no
    image is present yet we ask them to send one (see :func:`_ask_for_photo`).
    """
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.clear()
    if query.message is not None:
        await state.update_data(photo_chat_id=query.message.chat.id)
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "Что делаем?\n\n"
                "📷 <b>Фото</b> — обработать как изображение "
                "(img2img / убрать фон)\n"
                "🎬 <b>Видео</b> — анимировать в короткий ролик\n\n"
                "<i>Фото можно прислать сейчас или после выбора.</i>",
                reply_markup=_kb_photo_kind(),
            )
    await query.answer()


def _register_media_bus_artifact(file_id: str) -> None:
    """ТЗ-1 этап 5: TG-фото → артефакт медиашины (no-op при выключенной).

    Врезка минимальна и никогда не ломает существующий фото-flow:
    шина хранит только ``file_id`` (§6А.6), байты не скачиваются.
    """
    try:
        from .gateway import media_bus

        if media_bus.media_enabled():
            media_bus.register_artifact(
                kind="photo",
                origin={"platform": "telegram", "file_id": file_id},
                step="tg:upload",
            )
    except Exception:  # noqa: BLE001 — шина не должна ломать фото-flow
        logger.warning("media_bus tg hook failed", exc_info=True)


@media_router.message(StateFilter(PhotoFlowStates.awaiting_photo_upload), F.photo)
async def capture_pending_photo(message: Message, state: FSMContext) -> None:
    """Capture a photo sent while waiting in the main-menu generation flow.

    Registered on ``media_router`` (included before the photo_chooser
    addon) with a state filter, so it wins over the global chooser
    handler only while we are explicitly awaiting an upload.
    """
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    if not message.photo:
        return
    photo = max(message.photo, key=lambda p: p.width * p.height)
    _register_media_bus_artifact(photo.file_id)
    data = await state.get_data()
    kind = data.get("pending_kind") or "photo"
    await state.update_data(
        photo_file_id=photo.file_id,
        photo_chat_id=message.chat.id,
        caption_prompt=(message.caption or "").strip(),
        pending_kind=None,
    )
    if kind == "video":
        await state.set_state(PhotoFlowStates.awaiting_video_prompt)
        await message.answer(_VIDEO_BRANCH_TEXT, reply_markup=_kb_video_prompt())
    else:
        await state.set_state(PhotoFlowStates.awaiting_photo_prompt)
        await message.answer(_PHOTO_BRANCH_TEXT, reply_markup=_kb_photo_action())


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_photo_upload),
    F.document.mime_type.startswith("image/"),
)
async def capture_pending_photo_document(message: Message, state: FSMContext) -> None:
    """Same as :func:`capture_pending_photo` but for image documents."""
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    if message.document is None:
        return
    data = await state.get_data()
    kind = data.get("pending_kind") or "photo"
    await state.update_data(
        photo_file_id=message.document.file_id,
        photo_chat_id=message.chat.id,
        caption_prompt=(message.caption or "").strip(),
        pending_kind=None,
    )
    if kind == "video":
        await state.set_state(PhotoFlowStates.awaiting_video_prompt)
        await message.answer(_VIDEO_BRANCH_TEXT, reply_markup=_kb_video_prompt())
    else:
        await state.set_state(PhotoFlowStates.awaiting_photo_prompt)
        await message.answer(_PHOTO_BRANCH_TEXT, reply_markup=_kb_photo_action())


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_photo_prompt), _NOT_A_COMMAND,
)
async def capture_photo_prompt(message: Message, state: FSMContext) -> None:
    """Capture the img2img prompt and run img2img immediately (dual delivery)."""
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    if not data.get("photo_file_id"):
        await message.answer("Контекст фото потерян — пришли фото заново.")
        await state.clear()
        return
    prompt_ru = (message.text or "").strip()
    if not prompt_ru:
        return
    await state.set_state(None)
    # User typed prompt directly — assume it's already in the language
    # they want. We still pass it through translate_to_english so the
    # diffusion model gets natural English (it tolerates Russian poorly).
    await _run_img2img_flow(message, state, prompt_ru=prompt_ru)


@media_router.callback_query(F.data == "photo:photo:rmbg")
async def cb_photo_rmbg(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    file_id = data.get("photo_file_id")
    if not file_id or query.message is None or query.bot is None:
        await query.answer("Контекст потерян — пришли фото заново.", show_alert=True)
        return
    await query.answer("Запускаю…")
    msg = query.message
    progress_msg = await _open_progress(msg, "📥 Качаю фото…")

    try:
        image_bytes = await _download_photo(query.bot, str(file_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("photo download failed (rmbg)")
        await progress_msg.edit_text(f"Ошибка скачивания: {_html(str(exc))}")
        return

    async def on_progress(status: str, pct: float) -> None:
        with contextlib.suppress(Exception):
            await progress_msg.edit_text(f"🪄 rmbg: {status} {pct:.0f}%")

    try:
        png_url, jpg_url = await remove_background(image_bytes, on_progress=on_progress)
    except MediaError as exc:
        await progress_msg.edit_text(f"Провайдер: {_html(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("rmbg failed")
        await progress_msg.edit_text(f"Ошибка: {_html(str(exc))}")
        return

    try:
        png_bytes = await _fetch_url(png_url)
        jpg_bytes = await _fetch_url(jpg_url) if jpg_url else None
    except Exception as exc:  # noqa: BLE001
        logger.exception("rmbg result download failed")
        await progress_msg.edit_text(f"Провайдер вернул URL, но не скачался: {_html(str(exc))}\n{png_url}")
        return

    with contextlib.suppress(Exception):
        await progress_msg.delete()

    # Dual delivery: inline JPG preview (cheap to view) + PNG document
    # with alpha channel preserved (for Photoshop).
    caption = "🪄 Без фона · PNG с альфой во вложении (готово для Photoshop)"
    if jpg_bytes:
        with contextlib.suppress(Exception):
            await msg.answer_photo(
                BufferedInputFile(jpg_bytes, filename="no-bg-preview.jpg"),
                caption=caption,
            )
    await msg.answer_document(
        BufferedInputFile(png_bytes, filename="no-bg.png"),
        caption=None if jpg_bytes else caption,
    )
    await state.clear()


# ---- LLM prompt-helper ("Указ") -----------------------------------------


async def _start_idea_flow(
    query: CallbackQuery, state: FSMContext, *, branch: str
) -> None:
    """Common entry for ✨ Указ in either photo or video branch."""
    if branch == "photo":
        await state.set_state(PhotoFlowStates.awaiting_photo_keywords)
    else:
        await state.set_state(PhotoFlowStates.awaiting_video_keywords)
    if query.message is not None:
        await query.message.edit_text(
            "✨ <b>Указ</b>: пришли ключевые слова или короткое описание идеи "
            f"({'что хочешь увидеть на финальной картинке' if branch == 'photo' else 'какое движение / эмоция / ракурс'}).\n\n"
            "<i>LLM сгенерирует 3 варианта развёрнутого промпта на русском, "
            "ты выберешь — я переведу на английский и отправлю в модель.</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="↩️ Вернуться",
                        callback_data=f"photo:back:{branch}_root",
                    ),
                ]],
            ),
        )
    await query.answer()


@media_router.callback_query(F.data == "photo:photo:idea")
async def cb_photo_idea(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await _start_idea_flow(query, state, branch="photo")


@media_router.callback_query(F.data == "photo:video:idea")
async def cb_video_idea(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await _start_idea_flow(query, state, branch="video")


async def _try_sourced_variants(
    progress_msg, keywords: str, branch: str
) -> list[str] | None:
    """Return prompt variants pulled from the user's curated sources.

    Returns ``None`` when the prompt_sources addon isn't configured for
    this branch or anything goes wrong — caller then falls back to the
    classic generator. Records the outcome into Hermes' experience log.
    """
    kind = "photo" if branch == "photo" else "video"
    try:
        from .addons.prompt_sources.store import get_sources

        if not get_sources(kind):
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        await progress_msg.edit_text("🔎 Ищу промпты в источниках…")
    except Exception:  # noqa: BLE001
        pass
    try:
        from .addons.prompt_sources.ai import prompt_help

        result = await prompt_help(keywords, kind=kind)
        variants = result.get("variants") or []
        note = result.get("note", "")
        try:
            from .addons.hermes_experience import record as _hx

            _hx(
                "prompt_sources",
                f"[{kind}] запрос «{keywords[:80]}» → {len(variants)} вариантов; {note}",
                outcome="ok" if variants else "empty",
                lesson=(
                    "Источники не открываются — стоит проверить список сайтов."
                    if "не открыл" in note
                    else ""
                ),
            )
        except Exception:  # noqa: BLE001
            pass
        return variants or None
    except Exception as exc:  # noqa: BLE001
        logger.info("sourced variants failed, falling back: %s", exc)
        try:
            from .addons.hermes_experience import record as _hx

            _hx(
                "prompt_sources",
                f"[{kind}] помощь по источникам упала: {exc}",
                outcome="fail",
                lesson="Сбой получения промптов из источников — нужен фолбэк на обычный генератор.",
            )
        except Exception:  # noqa: BLE001
            pass
        return None


async def _handle_keywords(
    message: Message, state: FSMContext, *, branch: str
) -> None:
    """Generate 3 RU variants from user's keywords and present picker."""
    keywords = (message.text or "").strip()
    if not keywords:
        return
    data = await state.get_data()
    if not data.get("photo_file_id"):
        await message.answer("Контекст фото потерян — пришли фото заново.")
        await state.clear()
        return
    progress_msg = await _open_progress(message, "✨ LLM думает…")
    # If the user curated prompt-source sites for this branch, pull real
    # example prompts from them and offer 4 options (3 verbatim from the
    # sites + 1 hybrid). Falls back to the classic 3-variant generator on
    # any failure so the photo flow never dead-ends.
    sourced = await _try_sourced_variants(progress_msg, keywords, branch)
    if sourced is not None:
        variants = sourced
        await state.update_data(variants=variants, variants_branch=branch)
        body_lines = [f"✨ <b>Варианты промпта</b> для «{_html(keywords)[:200]}»:\n"]
        for i, v in enumerate(variants):
            tag = " 🧬" if i == len(variants) - 1 and len(variants) >= 4 else ""
            body_lines.append(f"<b>{i + 1}.</b>{tag} {_html(v)}")
        with contextlib.suppress(Exception):
            await progress_msg.delete()
        await message.answer(
            "\n\n".join(body_lines),
            reply_markup=_kb_variants(branch, len(variants)),
        )
        await state.set_state(None)
        return
    try:
        variants = await generate_prompt_variants(keywords, kind=branch, count=3)
    except NoLLMError as exc:
        await progress_msg.edit_text(
            f"⚠️ {_html(str(exc))}\n\n"
            "Использую твой текст как промпт без обработки.",
        )
        # No variants — treat keywords as a direct prompt.
        if branch == "photo":
            await state.set_state(None)
            await _run_img2img_flow(message, state, prompt_ru=keywords)
        else:
            await state.update_data(prompt=keywords, prompt_translated=False)
            await state.set_state(None)
            await message.answer(
                f"Промпт: <code>{_html(keywords)}</code>\n\nВыбери качество:",
                reply_markup=_kb_video_quality(),
            )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM variant generation failed")
        await progress_msg.edit_text(f"LLM ошибка: {_html(str(exc))}")
        return

    await state.update_data(variants=variants, variants_branch=branch)
    # Variant text is shown in the message body (callback data only has
    # the index — 64-byte limit makes inline labels impractical).
    body_lines = [f"✨ <b>Варианты промпта</b> для «{_html(keywords)[:200]}»:\n"]
    for i, v in enumerate(variants):
        body_lines.append(f"<b>{i + 1}.</b> {_html(v)}")
    with contextlib.suppress(Exception):
        await progress_msg.delete()
    await message.answer(
        "\n\n".join(body_lines),
        reply_markup=_kb_variants(branch, len(variants)),
    )
    # State is cleared because variant selection is callback-driven.
    await state.set_state(None)


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_photo_keywords), _NOT_A_COMMAND,
)
async def capture_photo_keywords(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    await _handle_keywords(message, state, branch="photo")


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_video_keywords), _NOT_A_COMMAND,
)
async def capture_video_keywords(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    await _handle_keywords(message, state, branch="video")


@media_router.callback_query(F.data.regexp(r"^photo:(photo|video):var:\d+$"))
async def cb_variant_pick(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    branch = parts[1]
    try:
        idx = int(parts[3])
    except (ValueError, IndexError):
        await query.answer("Bad variant.", show_alert=True)
        return
    data = await state.get_data()
    variants = data.get("variants") or []
    if not (0 <= idx < len(variants)):
        await query.answer("Вариант не найден.", show_alert=True)
        return
    chosen_ru = variants[idx]
    await query.answer("Перевожу промпт на EN…")
    if query.message is None:
        return
    msg = query.message
    if branch == "photo":
        await _run_img2img_flow(msg, state, prompt_ru=chosen_ru)
    else:
        # Video: translate now, then show quality picker.
        try:
            prompt_en = await translate_to_english(chosen_ru)
        except Exception as exc:  # noqa: BLE001
            logger.exception("translate failed")
            prompt_en = chosen_ru
            await msg.answer(f"⚠️ Перевод не удался ({_html(str(exc))}) — отправлю русский текст как есть.")
        await state.update_data(prompt=prompt_en, prompt_ru=chosen_ru, prompt_translated=True)
        await state.set_state(None)
        await msg.answer(
            f"✨ Промпт: <code>{_html(chosen_ru)}</code>\n"
            f"EN: <code>{_html(prompt_en)}</code>\n\nВыбери качество:",
            reply_markup=_kb_video_quality(),
        )


# ---- video pre-quality screen handlers ----------------------------------


@media_router.callback_query(F.data == "photo:video:skip_prompt")
async def cb_video_skip_prompt(query: CallbackQuery, state: FSMContext) -> None:
    """Skip prompt entry — use caption or default."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    caption_prompt = (data.get("caption_prompt") or "").strip()
    prompt = caption_prompt or "Cinematic gentle motion, smooth camera move, soft natural light"
    await state.update_data(prompt=prompt, prompt_translated=False)
    await state.set_state(None)
    if query.message is not None:
        await query.message.edit_text(
            f"🎬 <b>Видео</b>: промпт = <code>{_html(prompt)[:300]}</code>\n\nВыбери качество:",
            reply_markup=_kb_video_quality(),
        )
    await query.answer()


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_video_prompt), _NOT_A_COMMAND,
)
async def capture_video_prompt(message: Message, state: FSMContext) -> None:
    """User typed a free-form motion prompt — translate, show quality picker."""
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    data = await state.get_data()
    if not data.get("photo_file_id"):
        await message.answer("Контекст фото потерян — пришли фото заново.")
        await state.clear()
        return
    prompt_ru = (message.text or "").strip()
    if not prompt_ru:
        return
    progress_msg = await _open_progress(message, "✨ Перевожу промпт на EN…")
    prompt_en = await translate_to_english(prompt_ru)
    await state.update_data(prompt=prompt_en, prompt_ru=prompt_ru, prompt_translated=True)
    await state.set_state(None)
    with contextlib.suppress(Exception):
        await progress_msg.delete()
    await message.answer(
        f"🎬 Промпт принят:\n<code>{_html(prompt_ru)}</code>\n"
        f"EN: <code>{_html(prompt_en)}</code>\n\nВыбери качество:",
        reply_markup=_kb_video_quality(),
    )


# ---- video quality (incl. GIF) ------------------------------------------


@media_router.callback_query(F.data.startswith("photo:video:q:"))
async def cb_video_quality(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    quality = (query.data or "").split(":")[-1].lower()
    if quality not in VIDEO_QUALITY_MAP:
        await query.answer("Bad quality.", show_alert=True)
        return
    data = await state.get_data()
    file_id = data.get("photo_file_id")
    if not file_id or query.message is None or query.bot is None:
        await query.answer("Контекст потерян — пришли фото заново.", show_alert=True)
        return
    prompt = (data.get("prompt") or "").strip() or "Cinematic gentle motion, smooth camera move, soft natural light"
    await query.answer("Запускаю…")
    msg = query.message
    await _run_video_flow(msg, state, file_id=str(file_id), quality=quality, prompt=prompt, bot=query.bot)


# ---- Weavy.ai guided flow (prompt + image + model + length + quality) ----
#
# A single "панель" with several fillable buttons. Each picker writes its
# choice into FSM data and re-renders the panel, so the user assembles the
# whole request (промпт, картинка, модель, длина, качество) before tapping
# "🚀 Сгенерировать". The image is the photo already attached upstream.

# Built-in Weavy models (shipped with the bot). The user's own models —
# managed via the "➕ Добавить" button in the model picker — are appended
# at runtime by :func:`_weavy_all_models`.
_WEAVY_BUILTIN_MODELS = KNOWN_MEDIA_PROVIDERS["weavy"]["video_models"]
_WEAVY_QUALITIES = ["480", "720", "1080", "2k"]


def _weavy_all_models() -> list[tuple[str, str]]:
    """Built-in models + the user's custom models, as ``(id, label)`` pairs.

    Custom models (ids / URLs the user pasted) use their own value as the
    label so a full URL like the Grok one is still recognisable.
    """
    models: list[tuple[str, str]] = list(_WEAVY_BUILTIN_MODELS)
    builtin_ids = {mid for mid, _ in models}
    for custom in storage.get_weavy_models():
        if custom and custom not in builtin_ids:
            models.append((custom, custom))
    return models


def _weavy_cfg(data: dict) -> dict:
    """Read the current Weavy selections from FSM data, with defaults."""
    return {
        "prompt": (data.get("weavy_prompt") or "").strip(),
        "prompt_ru": (data.get("weavy_prompt_ru") or "").strip(),
        "model": data.get("weavy_model") or WEAVY_DEFAULT_MODEL,
        "duration": int(data.get("weavy_duration") or WEAVY_DEFAULT_DURATION),
        "quality": data.get("weavy_quality") or WEAVY_DEFAULT_QUALITY,
        "has_image": bool(data.get("photo_file_id")),
        "mode": data.get("weavy_mode") or "api",
    }


def _weavy_model_label(model_id: str) -> str:
    for mid, label in _weavy_all_models():
        if mid == model_id:
            return label.split("—")[0].strip() or mid
    return model_id


def _kb_weavy_mode() -> InlineKeyboardMarkup:
    """Entry chooser: API ('Обычно') vs Browser ('Браузер') vs Аккаунт.

    The "👤 Аккаунт" button opens the credentials screen used for the
    Figma sign-in on weavy.ai. "🔁 Авто" toggles whether that login is
    performed automatically whenever Weavy is opened through any mode.
    """
    auto_on = storage.get_weavy_auto()
    auto_label = "🔁 Авто вход: ВКЛ" if auto_on else "🔁 Авто вход: ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚙️ Обычно (API)", callback_data="weavy:mode:api"),
                InlineKeyboardButton(text="🌐 Браузер", callback_data="weavy:mode:browser"),
            ],
            [InlineKeyboardButton(text="👤 Аккаунт", callback_data="weavy:account")],
            [InlineKeyboardButton(text=auto_label, callback_data="weavy:auto:toggle")],
            [InlineKeyboardButton(text="↩️ Вернуться", callback_data="photo:back:video_root")],
        ]
    )


def _kb_weavy_panel(cfg: dict) -> InlineKeyboardMarkup:
    prompt_mark = "✅" if cfg["prompt"] else "—"
    img_mark = "✅" if cfg["has_image"] else "❌"
    res = WEAVY_RESOLUTION_MAP.get(cfg["quality"], cfg["quality"])
    is_browser = cfg["mode"] == "browser"
    # Run button differs per mode: API -> "Сгенерировать", Browser -> "Включить".
    if is_browser:
        run_btn = InlineKeyboardButton(text="▶️ Включить (через браузер)", callback_data="weavy:browserrun")
    else:
        run_btn = InlineKeyboardButton(text="🚀 Сгенерировать", callback_data="weavy:run")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✏️ Промпт: {prompt_mark}", callback_data="weavy:prompt")],
            [InlineKeyboardButton(text=f"🖼 Картинка: {img_mark}", callback_data="weavy:image")],
            [InlineKeyboardButton(text=f"🤖 Модель: {_weavy_model_label(cfg['model'])[:24]}", callback_data="weavy:model")],
            # ⏱ длина + 🎚 качество рядом (правый столбец) — оба выбираемые.
            [
                InlineKeyboardButton(text=f"⏱ Длина: {cfg['duration']}с", callback_data="weavy:dur"),
                InlineKeyboardButton(text=f"🎚 Качество: {res}", callback_data="weavy:qual"),
            ],
            [run_btn],
            [InlineKeyboardButton(text="↩️ Вернуться", callback_data="weavy:open")],
        ]
    )


def _weavy_panel_text(cfg: dict) -> str:
    prompt_line = f"<code>{_html(cfg['prompt_ru'] or cfg['prompt'])[:200]}</code>" if cfg["prompt"] else "<i>не задан</i>"
    img_line = "✅ прикреплена" if cfg["has_image"] else "❌ пришли фото заново"
    res = WEAVY_RESOLUTION_MAP.get(cfg["quality"], cfg["quality"])
    is_browser = cfg["mode"] == "browser"
    mode_badge = "🌐 режим: Браузер" if is_browser else "⚙️ режим: Обычно (API)"
    head = (
        "🌊 <b>Weavy.ai · видео из картинки</b>\n"
        f"{mode_badge}\n\n"
    )
    body = (
        f"✏️ Промпт: {prompt_line}\n"
        f"🖼 Картинка: {img_line}\n"
        f"🤖 Модель: <code>{_html(cfg['model'])}</code>\n"
        f"⏱ Длина: <b>{cfg['duration']}с</b>   🎚 Качество: <b>{res}</b>\n\n"
    )
    if is_browser:
        tail = (
            "🕵️ <i>Браузерный режим: открываю weavy.ai в стелс-браузере "
            "(Camoufox + человекоподобные движения мыши, антидетект-отпечаток "
            "и постоянный профиль по домену). Сайт видит живого пользователя, "
            "а не бота.</i>\n\n"
            "Заполни пункты и жми «▶️ Включить»."
        )
    else:
        tail = "<i>Заполни пункты и жми «🚀 Сгенерировать» (прямой API-запрос).</i>"
    return head + body + tail


async def _weavy_show_panel(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cfg = _weavy_cfg(data)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _weavy_panel_text(cfg), reply_markup=_kb_weavy_panel(cfg),
            )


@media_router.callback_query(F.data == "weavy:open")
async def cb_weavy_open(query: CallbackQuery, state: FSMContext) -> None:
    """Entry — pick API ('Обычно') or Browser ('Браузер') mode."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(None)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "🌊 <b>Weavy.ai</b>: как запустить?\n\n"
                "⚙️ <b>Обычно</b> — прямой API-запрос (быстро).\n"
                "🌐 <b>Браузер</b> — делаю всё на сайте weavy.ai в стелс-браузере, "
                "как живой пользователь.",
                reply_markup=_kb_weavy_mode(),
            )
    await query.answer()


@media_router.callback_query(F.data.startswith("weavy:mode:"))
async def cb_weavy_mode(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    mode = (query.data or "").split(":")[-1]
    if mode not in ("api", "browser"):
        await query.answer("Bad mode.", show_alert=True)
        return
    await state.update_data(weavy_mode=mode)
    await _weavy_show_panel(query, state)
    await query.answer("Браузер" if mode == "browser" else "Обычно (API)")


# ---- Weavy.ai account (Figma sign-in) + auto-login toggle ---------------


def _kb_weavy_account() -> InlineKeyboardMarkup:
    acc = storage.get_weavy_account()
    auto_on = storage.get_weavy_auto()
    login_mark = "✅" if acc["login"] else "—"
    pass_mark = "✅" if acc["password"] else "—"
    link_mark = "✅" if acc["join_link"] else "—"
    auto_label = "🔁 Авто вход: ВКЛ" if auto_on else "🔁 Авто вход: ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"👤 Логин: {login_mark}", callback_data="weavy:acc:login")],
            [InlineKeyboardButton(text=f"🔑 Пароль: {pass_mark}", callback_data="weavy:acc:password")],
            [InlineKeyboardButton(text=f"🔗 Ссылка присоединения: {link_mark}", callback_data="weavy:acc:joinlink")],
            [InlineKeyboardButton(text=auto_label, callback_data="weavy:auto:toggle")],
            [InlineKeyboardButton(text="🗑 Очистить аккаунт", callback_data="weavy:acc:clear")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="weavy:open")],
        ]
    )


def _weavy_account_text() -> str:
    acc = storage.get_weavy_account()
    auto_on = storage.get_weavy_auto()
    login = _html(acc["login"]) if acc["login"] else "<i>не задан</i>"
    pwd = "••••••" if acc["password"] else "<i>не задан</i>"
    link = f"<code>{_html(acc['join_link'])}</code>" if acc["join_link"] else "<i>не задана</i>"
    auto = "ВКЛ ✅" if auto_on else "ВЫКЛ"
    return (
        "👤 <b>Weavy.ai · аккаунт (вход через Figma)</b>\n\n"
        f"Логин: {login}\n"
        f"Пароль: {pwd}\n"
        f"Ссылка присоединения: {link}\n"
        f"Авто вход: <b>{auto}</b>\n\n"
        "<i>Эти данные используются для входа на weavy.ai через Figma в "
        "браузерном режиме. «Авто вход» — необязателен, но если он ВКЛ, "
        "вход выполняется автоматически при открытии Weavy любой из кнопок.</i>"
    )


@media_router.callback_query(F.data == "weavy:account")
async def cb_weavy_account(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(None)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _weavy_account_text(), reply_markup=_kb_weavy_account()
            )
    await query.answer()


@media_router.callback_query(F.data == "weavy:auto:toggle")
async def cb_weavy_auto_toggle(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    new_val = not storage.get_weavy_auto()
    storage.set_weavy_auto(new_val)
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _weavy_account_text(), reply_markup=_kb_weavy_account()
            )
    await query.answer(f"Авто вход: {'ВКЛ' if new_val else 'ВЫКЛ'}")


@media_router.callback_query(F.data == "weavy:acc:clear")
async def cb_weavy_acc_clear(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    storage.clear_weavy_account()
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                _weavy_account_text(), reply_markup=_kb_weavy_account()
            )
    await query.answer("Аккаунт очищен")


_WEAVY_ACC_FIELDS = {
    "login": (PhotoFlowStates.awaiting_weavy_acc_login, "👤 Пришли <b>логин</b> сообщением."),
    "password": (PhotoFlowStates.awaiting_weavy_acc_password, "🔑 Пришли <b>пароль</b> сообщением."),
    "joinlink": (PhotoFlowStates.awaiting_weavy_acc_joinlink, "🔗 Пришли <b>ссылку присоединения</b> сообщением."),
}


@media_router.callback_query(F.data.startswith("weavy:acc:"))
async def cb_weavy_acc_field(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    field = (query.data or "").split(":")[-1]
    spec = _WEAVY_ACC_FIELDS.get(field)
    if spec is None:
        # Not a field edit (e.g. weavy:acc:clear handled elsewhere).
        return
    target_state, prompt = spec
    await state.set_state(target_state)
    if query.message is not None:
        await query.message.edit_text(
            prompt,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="weavy:account")]]
            ),
        )
    await query.answer()


@media_router.message(StateFilter(PhotoFlowStates.awaiting_weavy_acc_login), _NOT_A_COMMAND)
async def capture_weavy_acc_login(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    storage.set_weavy_account_field("login", (message.text or "").strip())
    await state.set_state(None)
    await message.answer("✅ Логин сохранён.")
    await message.answer(_weavy_account_text(), reply_markup=_kb_weavy_account())


@media_router.message(StateFilter(PhotoFlowStates.awaiting_weavy_acc_password), _NOT_A_COMMAND)
async def capture_weavy_acc_password(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    storage.set_weavy_account_field("password", (message.text or "").strip())
    await state.set_state(None)
    # Delete the message with the raw password so it doesn't linger in chat.
    with contextlib.suppress(Exception):
        await message.delete()
    await message.answer("✅ Пароль сохранён (сообщение с паролем удалено).")
    await message.answer(_weavy_account_text(), reply_markup=_kb_weavy_account())


@media_router.message(StateFilter(PhotoFlowStates.awaiting_weavy_acc_joinlink), _NOT_A_COMMAND)
async def capture_weavy_acc_joinlink(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    storage.set_weavy_account_field("join_link", (message.text or "").strip())
    await state.set_state(None)
    await message.answer("✅ Ссылка присоединения сохранена.")
    await message.answer(_weavy_account_text(), reply_markup=_kb_weavy_account())


@media_router.callback_query(F.data == "weavy:panel")
async def cb_weavy_panel(query: CallbackQuery, state: FSMContext) -> None:
    """Re-show the assembly panel (back target from sub-pickers)."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(None)
    await _weavy_show_panel(query, state)
    await query.answer()


@media_router.callback_query(F.data == "weavy:prompt")
async def cb_weavy_prompt(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(PhotoFlowStates.awaiting_weavy_prompt)
    if query.message is not None:
        await query.message.edit_text(
            "🌊 <b>Weavy.ai</b>: пришли промпт сообщением "
            "(опиши движение/ракурс/настроение). Переведу на EN автоматически.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="weavy:panel")]]
            ),
        )
    await query.answer()


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_weavy_prompt), _NOT_A_COMMAND,
)
async def capture_weavy_prompt(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    prompt_ru = (message.text or "").strip()
    if not prompt_ru:
        return
    progress_msg = await _open_progress(message, "✨ Перевожу промпт на EN…")
    try:
        prompt_en = await translate_to_english(prompt_ru)
    except NoLLMError:
        prompt_en = prompt_ru
    with contextlib.suppress(Exception):
        await progress_msg.delete()
    await state.update_data(weavy_prompt=prompt_en, weavy_prompt_ru=prompt_ru)
    await state.set_state(None)
    data = await state.get_data()
    cfg = _weavy_cfg(data)
    await message.answer(_weavy_panel_text(cfg), reply_markup=_kb_weavy_panel(cfg))


@media_router.callback_query(F.data == "weavy:image")
async def cb_weavy_image(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("photo_file_id"):
        await query.answer("Картинка уже прикреплена ✅", show_alert=True)
    else:
        await query.answer("Картинка не найдена — пришли фото заново.", show_alert=True)


@media_router.callback_query(F.data == "weavy:model")
async def cb_weavy_model(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    current = data.get("weavy_model") or WEAVY_DEFAULT_MODEL
    models = _weavy_all_models()
    builtin_ids = {mid for mid, _ in _WEAVY_BUILTIN_MODELS}
    rows: list[list[InlineKeyboardButton]] = []
    for idx, (mid, label) in enumerate(models):
        mark = "✅ " if mid == current else ""
        row = [
            InlineKeyboardButton(
                text=f"{mark}{label[:46]}", callback_data=f"weavy:setmodel:{idx}"
            )
        ]
        # Custom (user-added) models get a delete button next to them.
        if mid not in builtin_ids:
            row.append(
                InlineKeyboardButton(text="🗑", callback_data=f"weavy:delmodel:{idx}")
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="weavy:addmodel")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="weavy:panel")])
    if query.message is not None:
        await query.message.edit_text(
            "🤖 <b>Weavy.ai · выбери модель</b>\n"
            "<i>«➕ Добавить» — вставь свой id/URL модели. «🗑» — удалить свою модель.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await query.answer()


@media_router.callback_query(F.data.startswith("weavy:setmodel:"))
async def cb_weavy_setmodel(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    try:
        idx = int((query.data or "").split(":")[-1])
        model_id = _weavy_all_models()[idx][0]
    except (ValueError, IndexError):
        await query.answer("Bad model.", show_alert=True)
        return
    await state.update_data(weavy_model=model_id)
    await _weavy_show_panel(query, state)
    await query.answer("Модель выбрана")


@media_router.callback_query(F.data.startswith("weavy:delmodel:"))
async def cb_weavy_delmodel(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    try:
        idx = int((query.data or "").split(":")[-1])
        model_id = _weavy_all_models()[idx][0]
    except (ValueError, IndexError):
        await query.answer("Bad model.", show_alert=True)
        return
    if model_id in {mid for mid, _ in _WEAVY_BUILTIN_MODELS}:
        await query.answer("Встроенную модель удалить нельзя.", show_alert=True)
        return
    storage.remove_weavy_model(model_id)
    # If the deleted model was selected, fall back to the default.
    data = await state.get_data()
    if (data.get("weavy_model") or WEAVY_DEFAULT_MODEL) == model_id:
        await state.update_data(weavy_model=WEAVY_DEFAULT_MODEL)
    await query.answer("Модель удалена")
    # Re-render the picker.
    await cb_weavy_model(query, state)


@media_router.callback_query(F.data == "weavy:addmodel")
async def cb_weavy_addmodel(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    await state.set_state(PhotoFlowStates.awaiting_weavy_model_add)
    if query.message is not None:
        await query.message.edit_text(
            "➕ <b>Weavy.ai · добавить модель</b>\n\n"
            "Пришли id модели или ссылку сообщением, например:\n"
            f"<code>{_html(WEAVY_GROK_MODEL)}</code>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="weavy:model")]]
            ),
        )
    await query.answer()


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_weavy_model_add), _NOT_A_COMMAND,
)
async def capture_weavy_model_add(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    model = (message.text or "").strip()
    if not model:
        return
    added = storage.add_weavy_model(model)
    # Select the newly added model and return to the panel.
    await state.update_data(weavy_model=model)
    await state.set_state(None)
    data = await state.get_data()
    cfg = _weavy_cfg(data)
    note = "✅ Модель добавлена и выбрана." if added else "ℹ️ Модель уже была в списке — выбрана."
    await message.answer(note)
    await message.answer(_weavy_panel_text(cfg), reply_markup=_kb_weavy_panel(cfg))


@media_router.callback_query(F.data == "weavy:dur")
async def cb_weavy_dur(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    row = [
        InlineKeyboardButton(text=f"{n}с", callback_data=f"weavy:setdur:{n}")
        for n in WEAVY_DURATIONS
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="↩️ Назад", callback_data="weavy:panel")],
    ])
    if query.message is not None:
        await query.message.edit_text("⏱ <b>Weavy.ai · длина видео</b>:", reply_markup=kb)
    await query.answer()


@media_router.callback_query(F.data.startswith("weavy:setdur:"))
async def cb_weavy_setdur(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    try:
        n = int((query.data or "").split(":")[-1])
    except ValueError:
        await query.answer("Bad duration.", show_alert=True)
        return
    await state.update_data(weavy_duration=n)
    await _weavy_show_panel(query, state)
    await query.answer(f"Длина: {n}с")


@media_router.callback_query(F.data == "weavy:qual")
async def cb_weavy_qual(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    row = [
        InlineKeyboardButton(text=WEAVY_RESOLUTION_MAP.get(q, q), callback_data=f"weavy:setqual:{q}")
        for q in _WEAVY_QUALITIES
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="↩️ Назад", callback_data="weavy:panel")],
    ])
    if query.message is not None:
        await query.message.edit_text("🎚 <b>Weavy.ai · качество</b>:", reply_markup=kb)
    await query.answer()


@media_router.callback_query(F.data.startswith("weavy:setqual:"))
async def cb_weavy_setqual(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    quality = (query.data or "").split(":")[-1].lower()
    if quality not in WEAVY_RESOLUTION_MAP:
        await query.answer("Bad quality.", show_alert=True)
        return
    await state.update_data(weavy_quality=quality)
    await _weavy_show_panel(query, state)
    await query.answer(f"Качество: {WEAVY_RESOLUTION_MAP[quality]}")


@media_router.callback_query(F.data == "weavy:run")
async def cb_weavy_run(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    cfg = _weavy_cfg(data)
    file_id = data.get("photo_file_id")
    if not file_id or query.message is None or query.bot is None:
        await query.answer("Картинка не найдена — пришли фото заново.", show_alert=True)
        return
    await query.answer("Запускаю Weavy.ai…")
    await _run_weavy_flow(
        query.message,
        state,
        file_id=str(file_id),
        prompt=cfg["prompt"] or "Cinematic gentle motion, smooth camera move, soft natural light",
        prompt_ru=cfg["prompt_ru"],
        model=cfg["model"],
        quality=cfg["quality"],
        duration=cfg["duration"],
        bot=query.bot,
    )


async def _run_weavy_flow(
    msg: Message,
    state: FSMContext,
    *,
    file_id: str,
    prompt: str,
    prompt_ru: str,
    model: str,
    quality: str,
    duration: int,
    bot,
) -> None:
    """Run Weavy.ai img2video and deliver inline preview + MP4 document."""
    res = WEAVY_RESOLUTION_MAP.get(quality, quality)
    progress_msg = await _open_progress(
        msg, f"🌊 Weavy.ai: {model} · {duration}с · {res}…"
    )
    try:
        image_bytes = await _download_photo(bot, file_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("photo download failed (weavy)")
        await progress_msg.edit_text(f"Ошибка скачивания: {_html(str(exc))}")
        return

    async def on_progress(status: str, pct: float) -> None:
        with contextlib.suppress(Exception):
            await progress_msg.edit_text(f"🌊 Weavy.ai [{model}/{res}]: {status} {pct:.0f}%")

    try:
        video_url = await weavy_image_to_video(
            image_bytes, prompt, model=model, quality=quality,
            duration=duration, on_progress=on_progress,
        )
    except MediaError as exc:
        await progress_msg.edit_text(f"Weavy.ai: {_html(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("weavy img2video failed")
        await progress_msg.edit_text(f"Ошибка: {_html(str(exc))}")
        return

    try:
        mp4_bytes = await _fetch_url(video_url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("weavy video download failed")
        await progress_msg.edit_text(
            f"Weavy вернул URL, но не скачался: {_html(str(exc))}\n{video_url}"
        )
        return

    with contextlib.suppress(Exception):
        await progress_msg.delete()

    caption = (
        f"🌊 Weavy.ai · {model} · {duration}с · {res}\n"
        f"<code>{_html(prompt_ru or prompt)[:600]}</code>"
    )
    with contextlib.suppress(Exception):
        await msg.answer_video(
            BufferedInputFile(mp4_bytes, filename="weavy.mp4"),
            caption=caption,
        )
    await msg.answer_document(
        BufferedInputFile(mp4_bytes, filename="weavy.mp4"),
    )
    await state.clear()


@media_router.callback_query(F.data == "weavy:browserrun")
async def cb_weavy_browserrun(query: CallbackQuery, state: FSMContext) -> None:
    """Browser mode — drive weavy.ai on the site via the stealth engine."""
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    data = await state.get_data()
    cfg = _weavy_cfg(data)
    file_id = data.get("photo_file_id")
    if not file_id or query.message is None or query.bot is None:
        await query.answer("Картинка не найдена — пришли фото заново.", show_alert=True)
        return
    await query.answer("Открываю weavy.ai в браузере…")
    await _run_weavy_browser_flow(
        query.message,
        state,
        file_id=str(file_id),
        prompt=cfg["prompt"] or "Cinematic gentle motion, smooth camera move, soft natural light",
        prompt_ru=cfg["prompt_ru"],
        model=cfg["model"],
        quality=cfg["quality"],
        duration=cfg["duration"],
        bot=query.bot,
    )


async def _run_weavy_browser_flow(
    msg: Message,
    state: FSMContext,
    *,
    file_id: str,
    prompt: str,
    prompt_ru: str,
    model: str,
    quality: str,
    duration: int,
    bot,
) -> None:
    """Do the whole Weavy.ai request *on the website* via the stealth browser.

    Uses :func:`bot.workers.browser_clicker.run_browser_macro` — Camoufox /
    CloakBrowser with human-like Bézier mouse moves and a persistent per-domain
    antidetect fingerprint, so weavy.ai sees a real user, not a bot. The same
    fields the API panel collects (prompt / image / model / length / quality)
    are typed/clicked into the page.

    The selectors below are best-effort for weavy.ai's node UI — tweak the
    macro to match the exact button/field labels of your weavy.ai workspace.
    """
    import contextlib as _ctx
    from pathlib import Path

    res = WEAVY_RESOLUTION_MAP.get(quality, quality)
    progress_msg = await _open_progress(
        msg, f"🌐 Weavy.ai (браузер): {model} · {duration}с · {res}…"
    )

    # 1) Stage the source image where the upload step can find it.
    try:
        image_bytes = await _download_photo(bot, file_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("photo download failed (weavy browser)")
        await progress_msg.edit_text(f"Ошибка скачивания: {_html(str(exc))}")
        return
    try:
        from .config import DATA_DIR
        attach_dir = Path(DATA_DIR) / "attachments"
        attach_dir.mkdir(parents=True, exist_ok=True)
        fname = "weavy_source.jpg"
        (attach_dir / fname).write_bytes(image_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("staging upload file failed")
        await progress_msg.edit_text(f"Не смог подготовить файл: {_html(str(exc))}")
        return

    with _ctx.suppress(Exception):
        await progress_msg.edit_text(
            "🌐 Веду весь сценарий на weavy.ai как живой пользователь "
            "(стелс-движок, человекоподобная мышь)…"
        )

    # 2) Build the on-site macro. run_browser_macro replays it with humanized
    #    mouse trajectories + antidetect fingerprint.
    steps: list[tuple] = [
        ("goto", "https://weavy.ai/"),
        ("pause", 4.0),
    ]

    # Optional Figma sign-in. When the user filled the account screen and
    # turned "Авто вход" ON, log in first so the page treats us as a
    # signed-in user before assembling the request. Best-effort selectors —
    # tweak the macro to match weavy.ai's actual login/Figma buttons.
    acc = storage.get_weavy_account()
    if storage.get_weavy_auto() and (acc.get("login") or acc.get("join_link")):
        with _ctx.suppress(Exception):
            await progress_msg.edit_text(
                "🔐 Авто-вход в weavy.ai через Figma…"
            )
        if acc.get("join_link"):
            steps += [
                ("goto", acc["join_link"]),
                ("pause", 3.0),
            ]
        steps += [
            ("click", "Figma"),
            ("pause", 3.0),
        ]
        if acc.get("login"):
            steps += [
                ("type", (acc["login"], "email")),
                ("pause", 0.8),
            ]
        if acc.get("password"):
            steps += [
                ("type", (acc["password"], "password")),
                ("pause", 0.8),
            ]
        steps += [
            ("click", "Log in"),
            ("pause", 4.0),
            ("goto", "https://weavy.ai/"),
            ("pause", 3.0),
        ]

    steps += [
        # Промпт.
        ("type", (prompt, "prompt")),
        ("pause", 1.0),
        # Картинка.
        ("upload", fname),
        ("pause", 2.0),
        # Модель / длина / качество — клики по видимому тексту (best-effort).
        ("click", model),
        ("pause", 0.8),
        ("click", f"{duration}s"),
        ("pause", 0.8),
        ("click", res),
        ("pause", 0.8),
        # Запуск.
        ("click", "Generate"),
        ("pause", 6.0),
        ("copy", ".results"),
    ]

    screenshot_path = "session/weavy_screenshot.png"
    try:
        from .workers.browser_clicker import run_browser_macro
        result = await run_browser_macro(steps, screenshot_path=screenshot_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("weavy browser macro failed")
        await progress_msg.edit_text(
            f"Браузерный движок недоступен: {_html(type(exc).__name__)}: {_html(str(exc))}"
        )
        return

    with _ctx.suppress(Exception):
        await progress_msg.delete()

    ok = bool(result.get("ok"))
    shot = result.get("screenshot")
    head = "✅" if ok else "⚠️"
    caption = (
        f"{head} 🌐 Weavy.ai (браузер) · {model} · {duration}с · {res}\n"
        f"<code>{_html(prompt_ru or prompt)[:400]}</code>"
    )
    if not ok and result.get("error"):
        caption += f"\n<i>{_html(str(result['error']))[:200]}</i>"

    sent = False
    if shot:
        try:
            shot_bytes = Path(shot).read_bytes()
            await msg.answer_photo(
                BufferedInputFile(shot_bytes, filename="weavy_browser.png"),
                caption=caption,
            )
            sent = True
        except Exception:  # noqa: BLE001
            logger.exception("send weavy screenshot failed")
    if not sent:
        await msg.answer(caption)

    # Surface any copied result text (e.g. a result link the page exposed).
    for label, text in (result.get("copied_texts") or []):
        if text and "не найден" not in text.lower():
            await msg.answer(f"📋 <b>{_html(str(label))}</b>:\n<code>{_html(str(text))[:600]}</code>")
    await state.clear()


# ---- "⚙️ Настроить": guided provider setup via doc-scraping ------------
#
# Shared by BOTH the photo and video branches (one function, a button in
# each). The user types the model + site in plain words ("омни
# https://fal.ai"). The bot scrapes the provider's docs with its analysis
# tools (Firecrawl + web search) and an LLM to extract the EXACT model id
# and API base URL, reports them, asks for the API key, saves everything
# into the media slot, then runs the normal photo/video generation with a
# quality + aspect (9:16 / 16:9) picker and dual delivery.

_CFG_ASPECT_PHOTO_SIZE = {
    "9:16": "768x1344",
    "16:9": "1344x768",
    "1:1": "1024x1024",
}


def _cfg_parse_model_site(text: str) -> tuple[str, str]:
    """Split free text like 'омни https://fal.ai' into (model_query, url)."""
    import re

    url = ""
    m = re.search(r"https?://[^\s]+", text or "")
    if m:
        url = m.group(0).rstrip(").,")
    model_query = (text or "").replace(url, "").strip(" \t\n-—:")
    return model_query, url


async def _cfg_research_docs(model_query: str, url: str) -> str:
    """Collect provider documentation text via the bot's analysis tools."""
    chunks: list[str] = []
    if url:
        with contextlib.suppress(Exception):
            from .external_tools import firecrawl_scrape

            doc = await firecrawl_scrape(url)
            if doc and not doc.startswith("ERROR"):
                chunks.append(doc[:6000])
    # Enrich / fall back to a web search for the API docs page.
    if not chunks:
        query = f"{model_query} {url} API documentation image to video model name endpoint".strip()
        for fn_name in ("exa_search", "brave_search", "tavily_search"):
            with contextlib.suppress(Exception):
                from . import external_tools as ext

                fn = getattr(ext, fn_name, None)
                if fn is None:
                    continue
                res = await fn(query)
                if res and not res.startswith("ERROR") and "API key" not in res:
                    chunks.append(res[:4000])
                    break
    return "\n\n".join(chunks)


async def _cfg_discover(model_query: str, url: str, task: str) -> dict:
    """Ask the LLM to extract exact provider settings from the docs."""
    import json

    from .media import KNOWN_MEDIA_PROVIDERS, detect_provider_name

    def _finalize(out: dict) -> dict:
        """Prefer URL-based provider detection over the LLM guess.

        A known host (fal.ai, deapi.ai, replicate.com, weavy.ai) always
        maps to its real adapter family — otherwise a missing/weak LLM
        answer leaves the family as the generic ``openai_compatible``,
        which has no img2video adapter and breaks video generation
        (the "нет адаптера для img2video" error). The host is
        authoritative, so it wins whenever it resolves to a known key.

        We also canonicalise the API base URL: if the user typed a
        provider's marketing site (e.g. ``fal.ai``) rather than its API
        host (``fal.run``), swap in the known API base so the adapter
        calls the right endpoint.
        """
        current_url = (out.get("url") or url or "").strip()
        detected = detect_provider_name(current_url)
        if detected and detected != "openai_compatible":
            out["family"] = detected
        elif not out.get("family"):
            out["family"] = "openai_compatible"
        # Canonicalise the API base for known providers when the entered
        # URL is the marketing site rather than the API host.
        fam = out.get("family")
        meta = KNOWN_MEDIA_PROVIDERS.get(fam or "")
        if meta:
            api_host = (meta.get("match_url") or "").lower()
            if api_host and api_host not in current_url.lower():
                out["url"] = meta["url"]
            else:
                out["url"] = current_url
            # Model fallback: if the discovered/typed model is not a real
            # model id for this provider, use the provider's first known
            # model for the task so generation actually works (e.g. the
            # user typing "омни" for fal, or the LLM being unavailable).
            model = (out.get("model") or "").strip()
            model_key = "video_models" if task == "video" else "photo_models"
            known = meta.get(model_key) or []
            known_ids = {mid for mid, _ in known}
            needs_path = fam in ("fal", "replicate")
            invalid = (
                not model
                or (model not in known_ids and (needs_path and "/" not in model))
            )
            if known and invalid:
                out["model"] = known[0][0]
        else:
            out["url"] = current_url
        return out

    docs = await _cfg_research_docs(model_query, url)
    families = "deapi | openai | fal | replicate | weavy | openai_compatible"
    sys = (
        "You configure an image/video generation provider for a Telegram bot. "
        "Given the user's requested model, the site URL, and scraped docs, "
        "return STRICT JSON only (no markdown) with keys: "
        "provider_family (one of: " + families + "), "
        "api_base_url (the exact base URL to call, e.g. https://fal.run), "
        "model_id (the EXACT model string to send as 'model'), "
        "quality_options (array of short quality/resolution labels found in docs), "
        "aspect_options (array, subset of ['9:16','16:9','1:1']), "
        "notes (one short sentence). "
        f"The target task is '{task}'. If unsure, infer the best-known values."
    )
    user = (
        f"Requested model words: {model_query!r}\n"
        f"Site URL: {url!r}\n\n"
        f"Scraped documentation (may be truncated):\n{docs[:8000] or '(no docs scraped — use your own knowledge)'}"
    )
    out = {
        "ok": False, "family": "openai_compatible", "url": url,
        "model": model_query, "quality_options": [], "aspect_options": ["9:16", "16:9"],
        "notes": "",
    }
    try:
        from .media_llm import _oneshot

        raw = await _oneshot(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            max_tokens=500,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cfg discover LLM failed: %s", exc)
        return _finalize(out)
    # Salvage JSON from the response.
    import re as _re

    m = _re.search(r"\{.*\}", raw, _re.S)
    if not m:
        return _finalize(out)
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return _finalize(out)
    out["ok"] = True
    out["family"] = (data.get("provider_family") or out["family"]).strip()
    out["url"] = (data.get("api_base_url") or url or "").strip()
    out["model"] = (data.get("model_id") or model_query or "").strip()
    qo = data.get("quality_options") or []
    out["quality_options"] = [str(x)[:12] for x in qo][:6] if isinstance(qo, list) else []
    ao = data.get("aspect_options") or ["9:16", "16:9"]
    out["aspect_options"] = [a for a in ao if a in ("9:16", "16:9", "1:1")] or ["9:16", "16:9"]
    out["notes"] = str(data.get("notes") or "")[:200]
    return _finalize(out)


def _cfg_pick_slot(name: str) -> str:
    """Reuse a same-provider slot, else the first empty one, else slot 1."""
    raws = storage.list_media_slots_raw()
    for sid, raw in raws.items():
        if (raw.get("name") or "") == name and raw.get("url") and raw.get("api_key"):
            return sid
    for sid in storage.MEDIA_SLOT_IDS:
        if not raws.get(sid):
            return sid
    return "1"


def _kb_cfg_quality(task: str, options: list[str]) -> InlineKeyboardMarkup:
    # Prefer doc-extracted options that map onto our known video presets;
    # otherwise fall back to the standard set.
    if task == "video":
        known = list(VIDEO_QUALITY_MAP.keys())  # 480/720/1080/2k/auto
        labels = [o for o in options if o in known] or ["480", "720", "1080", "auto"]
    else:
        labels = options or ["1024", "1536", "auto"]
    btns = [InlineKeyboardButton(text=o, callback_data=f"mcfg:q:{o}") for o in labels]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="photo:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_cfg_aspect(options: list[str]) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=a, callback_data=f"mcfg:a:{a}") for a in options]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="❌ Отмена", callback_data="photo:cancel")],
    ])


@media_router.callback_query(F.data.startswith("mcfg:start:"))
async def cb_cfg_start(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    task = (query.data or "").split(":")[-1]
    if task not in ("video", "photo"):
        await query.answer("Bad task.", show_alert=True)
        return
    await state.update_data(cfg_task=task)
    await state.set_state(PhotoFlowStates.awaiting_cfg_modelsite)
    label = "🎬 видео" if task == "video" else "📷 фото"
    if query.message is not None:
        await query.message.edit_text(
            f"⚙️ <b>Настройка ({label})</b>\n\n"
            "Напиши <b>модель и сайт обычными словами</b> — одним сообщением.\n"
            "Например: <code>омни https://fal.ai</code>\n\n"
            "<i>Я прочитаю документацию провайдера своими анализаторами "
            "(Firecrawl + поиск) и вытащу точное имя модели и точный API-URL.</i>",
        )
    await query.answer()


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_cfg_modelsite), _NOT_A_COMMAND,
)
async def capture_cfg_modelsite(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    text = (message.text or "").strip()
    if not text:
        return
    data = await state.get_data()
    task = data.get("cfg_task") or "video"
    model_query, url = _cfg_parse_model_site(text)
    progress_msg = await _open_progress(
        message, "🔎 Анализирую документацию провайдера…"
    )
    found = await _cfg_discover(model_query, url, task)
    with contextlib.suppress(Exception):
        await progress_msg.delete()

    await state.update_data(
        cfg_family=found["family"],
        cfg_url=found["url"],
        cfg_model=found["model"],
        cfg_quality_options=found["quality_options"],
        cfg_aspect_options=found["aspect_options"],
    )
    await state.set_state(PhotoFlowStates.awaiting_cfg_apikey)
    notes = f"\n<i>{_html(found['notes'])}</i>" if found.get("notes") else ""
    await message.answer(
        "✅ Я нашёл настройки:\n"
        f"• Провайдер: <code>{_html(found['family'])}</code>\n"
        f"• Модель: <code>{_html(found['model'])}</code>\n"
        f"• URL: <code>{_html(found['url'])}</code>{notes}\n\n"
        "🔑 <b>Введите свой API-ключ</b> — пришли его одним сообщением "
        "(я сохраню его в настройки и удалю сообщение с ключом).",
    )


@media_router.message(
    StateFilter(PhotoFlowStates.awaiting_cfg_apikey), _NOT_A_COMMAND,
)
async def capture_cfg_apikey(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    api_key = (message.text or "").strip()
    if not api_key:
        return
    # Wipe the message with the secret as soon as we have it.
    with contextlib.suppress(Exception):
        await message.delete()
    data = await state.get_data()
    task = data.get("cfg_task") or "video"
    family = data.get("cfg_family") or "openai_compatible"
    url = data.get("cfg_url") or ""
    model = data.get("cfg_model") or ""

    slot_id = _cfg_pick_slot(family)
    try:
        storage.set_media_slot_field(slot_id, "url", url)
        storage.set_media_slot_field(slot_id, "name", family)  # override URL auto-name
        storage.set_media_slot_field(slot_id, "api_key", api_key)
        model_field = "video_model" if task == "video" else "photo_model"
        storage.set_media_slot_field(slot_id, model_field, model)
        storage.set_active_media_slot(task, slot_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("cfg save slot failed")
        await message.answer(f"Не смог сохранить настройки: {_html(str(exc))}")
        return

    await state.set_state(None)
    label = "🎬 видео" if task == "video" else "📷 фото"
    qopts = data.get("cfg_quality_options") or []
    if data.get("photo_file_id"):
        await message.answer(
            f"💾 Сохранил в слот {slot_id} и сделал активным для {label}.\n\n"
            "Фото уже прикреплено ✅ — выбери <b>качество</b>:",
            reply_markup=_kb_cfg_quality(task, qopts),
        )
    else:
        await message.answer(
            f"💾 Сохранил в слот {slot_id} и сделал активным для {label}.\n\n"
            "📷 Теперь пришли фото — затем выберешь качество и формат."
        )


@media_router.callback_query(F.data.startswith("mcfg:q:"))
async def cb_cfg_quality(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    quality = (query.data or "").split(":")[-1]
    await state.update_data(cfg_quality=quality)
    data = await state.get_data()
    aspects = data.get("cfg_aspect_options") or ["9:16", "16:9"]
    if query.message is not None:
        await query.message.edit_text(
            f"🎚 Качество: <b>{_html(quality)}</b>\n\nВыбери <b>формат</b>:",
            reply_markup=_kb_cfg_aspect(aspects),
        )
    await query.answer()


@media_router.callback_query(F.data.startswith("mcfg:a:"))
async def cb_cfg_aspect(query: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer("Только владелец.", show_alert=True)
        return
    aspect = (query.data or "").split(":")[-1]
    await state.update_data(cfg_aspect=aspect)
    if query.message is None or query.bot is None:
        await query.answer("Контекст потерян.", show_alert=True)
        return
    await query.answer("Запускаю…")
    await _run_cfg_generation(query.message, state, bot=query.bot)


async def _run_cfg_generation(msg: Message, state: FSMContext, *, bot) -> None:
    """Run generation with the just-configured slot + chosen quality/aspect."""
    data = await state.get_data()
    task = data.get("cfg_task") or "video"
    quality = data.get("cfg_quality") or "auto"
    aspect = data.get("cfg_aspect") or "9:16"
    model = data.get("cfg_model") or None
    file_id = data.get("photo_file_id")
    prompt_ru = (data.get("caption_prompt") or "").strip()
    if not file_id:
        await msg.answer("Контекст фото потерян — пришли фото заново.")
        return

    progress_msg = await _open_progress(
        msg, f"📥 Качаю фото · {task} · {quality} · {aspect}…"
    )
    try:
        image_bytes = await _download_photo(bot, str(file_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("photo download failed (cfg)")
        await progress_msg.edit_text(f"Ошибка скачивания: {_html(str(exc))}")
        return

    async def on_progress(status: str, pct: float) -> None:
        with contextlib.suppress(Exception):
            await progress_msg.edit_text(f"⚙️ {task} [{quality}/{aspect}]: {status} {pct:.0f}%")

    if task == "video":
        # ТЗ SwapFace §9.3: свап кадра до image_to_video.
        image_bytes = await _maybe_swapface(image_bytes, "video", msg)
        prompt = prompt_ru or "Cinematic gentle motion, smooth camera move, soft natural light"
        try:
            video_url = await image_to_video(
                image_bytes, prompt, quality=quality, model=model,
                aspect=aspect, on_progress=on_progress,
            )
        except MediaError as exc:
            await progress_msg.edit_text(f"Провайдер: {_html(str(exc))}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("cfg img2video failed")
            await progress_msg.edit_text(f"Ошибка: {_html(str(exc))}")
            return
        try:
            mp4_bytes = await _fetch_url(video_url)
        except Exception as exc:  # noqa: BLE001
            await progress_msg.edit_text(f"Провайдер вернул URL, но не скачался: {_html(str(exc))}\n{video_url}")
            return
        with contextlib.suppress(Exception):
            await progress_msg.delete()
        caption = f"🎬 {quality} · {aspect} · <code>{_html(model or '')[:60]}</code>"
        with contextlib.suppress(Exception):
            await msg.answer_video(BufferedInputFile(mp4_bytes, filename="video.mp4"), caption=caption)
        await msg.answer_document(BufferedInputFile(mp4_bytes, filename="video.mp4"))
    else:
        prompt = prompt_ru or "high quality, keep the subject, enhance details"
        try:
            prompt_en = await translate_to_english(prompt)
        except Exception:  # noqa: BLE001
            prompt_en = prompt
        size = _CFG_ASPECT_PHOTO_SIZE.get(aspect, "1024x1024")
        try:
            png_url, jpg_url = await image_to_image(
                image_bytes, prompt_en, fmt="png", model=model,
                size=size, on_progress=on_progress,
            )
        except MediaError as exc:
            await progress_msg.edit_text(f"Провайдер: {_html(str(exc))}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("cfg img2img failed")
            await progress_msg.edit_text(f"Ошибка: {_html(str(exc))}")
            return
        try:
            png_bytes = await _fetch_url(png_url)
            jpg_bytes = await _fetch_url(jpg_url) if jpg_url else None
        except Exception as exc:  # noqa: BLE001
            await progress_msg.edit_text(f"Провайдер вернул URL, но не скачался: {_html(str(exc))}")
            return
        with contextlib.suppress(Exception):
            await progress_msg.delete()
        # ТЗ SwapFace §9.3: фото-флоу — свап после image_to_image.
        swapped = await _maybe_swapface(png_bytes, "photo", msg)
        if swapped is not png_bytes:
            png_bytes = swapped
            jpg_bytes = None
        caption = f"📷 {size} · {aspect} · <code>{_html(model or '')[:60]}</code>"
        if jpg_bytes:
            with contextlib.suppress(Exception):
                await msg.answer_photo(BufferedInputFile(jpg_bytes, filename="img.jpg"), caption=caption)
        await msg.answer_document(
            BufferedInputFile(png_bytes, filename="img.png"),
            caption=None if jpg_bytes else caption,
        )
    await state.clear()


# ---- workhorses: img2img / video flow + dual delivery -------------------


async def _run_img2img_flow(
    msg: Message, state: FSMContext, *, prompt_ru: str
) -> None:
    """Translate prompt → call img2img → dual delivery (inline + document).

    ``msg`` is the message the user replied with (used for ``answer_*``);
    ``state`` carries ``photo_file_id``.
    """
    data = await state.get_data()
    file_id = data.get("photo_file_id")
    if not file_id or msg.bot is None:
        await msg.answer("Контекст потерян — пришли фото заново.")
        return
    progress_msg = await _open_progress(msg, "✨ Перевожу промпт на EN…")
    prompt_en = await translate_to_english(prompt_ru)
    with contextlib.suppress(Exception):
        await progress_msg.edit_text(f"📥 Качаю фото…\n<i>EN: {_html(prompt_en)[:200]}</i>")
    try:
        image_bytes = await _download_photo(msg.bot, str(file_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("photo download failed (img2img)")
        await progress_msg.edit_text(f"Ошибка скачивания: {_html(str(exc))}")
        return

    async def on_progress(status: str, pct: float) -> None:
        with contextlib.suppress(Exception):
            await progress_msg.edit_text(f"📷 img2img: {status} {pct:.0f}%")

    # We always request PNG to get an uncompressed source. deAPI returns
    # both PNG and JPG alt-format URLs by default, so we get inline-
    # preview material for free.
    try:
        png_url, jpg_url = await image_to_image(
            image_bytes, prompt_en, fmt="png", on_progress=on_progress,
        )
    except MediaError as exc:
        await progress_msg.edit_text(f"Провайдер: {_html(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("img2img failed")
        await progress_msg.edit_text(f"Ошибка: {_html(str(exc))}")
        return

    try:
        png_bytes = await _fetch_url(png_url)
        jpg_bytes = await _fetch_url(jpg_url) if jpg_url else None
    except Exception as exc:  # noqa: BLE001
        logger.exception("img2img download failed")
        await progress_msg.edit_text(f"Провайдер вернул URL, но не скачался: {_html(str(exc))}")
        return

    with contextlib.suppress(Exception):
        await progress_msg.delete()

    # ТЗ SwapFace §9.3: фото-флоу — свап после image_to_image; оба
    # варианта уже отправлены владельцу внутри _maybe_swapface, а
    # финальный документ ниже уходит со свапнутым кадром (вариант 1).
    swapped = await _maybe_swapface(png_bytes, "photo", msg)
    if swapped is not png_bytes:
        png_bytes = swapped
        jpg_bytes = None  # jpg-превью больше не соответствует результату

    caption = (
        f"📷 img2img · <code>{_html(prompt_ru)[:200]}</code>\n"
        f"<i>EN: {_html(prompt_en)[:200]}</i>"
    )
    if jpg_bytes:
        with contextlib.suppress(Exception):
            await msg.answer_photo(
                BufferedInputFile(jpg_bytes, filename="img2img-preview.jpg"),
                caption=caption,
            )
    await msg.answer_document(
        BufferedInputFile(png_bytes, filename="img2img.png"),
        caption=None if jpg_bytes else caption,
    )
    await state.clear()


async def _run_video_flow(
    msg: Message,
    state: FSMContext,
    *,
    file_id: str,
    quality: str,
    prompt: str,
    bot,
) -> None:
    """Run img2video, deliver inline preview + uncompressed MP4 document.

    For ``quality == "gif"`` we still ask deAPI for a small mp4 (smallest
    quality preset) and additionally try to convert it to a real .gif
    via ffmpeg. The mp4 is always sent as the source file.
    """
    is_gif = quality == "gif"
    deapi_quality = "480" if is_gif else quality  # smallest preset for gif
    progress_msg = await _open_progress(
        msg, f"📥 Качаю фото, качество: {quality}…"
    )
    try:
        image_bytes = await _download_photo(bot, file_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("photo download failed (i2v)")
        await progress_msg.edit_text(f"Ошибка скачивания: {_html(str(exc))}")
        return

    # ТЗ SwapFace §9.3: свап кадра ПОСЛЕ получения исходника и ДО img2video.
    image_bytes = await _maybe_swapface(image_bytes, "video", msg)

    async def on_progress(status: str, pct: float) -> None:
        with contextlib.suppress(Exception):
            await progress_msg.edit_text(
                f"🎬 img2video [{quality}]: {status} {pct:.0f}%"
            )

    try:
        video_url = await image_to_video(
            image_bytes, prompt, quality=deapi_quality, on_progress=on_progress,
        )
    except MediaError as exc:
        await progress_msg.edit_text(f"Провайдер: {_html(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("img2video failed")
        await progress_msg.edit_text(f"Ошибка: {_html(str(exc))}")
        return

    try:
        mp4_bytes = await _fetch_url(video_url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("video download failed")
        await progress_msg.edit_text(f"Провайдер вернул URL, но не скачался: {_html(str(exc))}\n{video_url}")
        return

    with contextlib.suppress(Exception):
        await progress_msg.delete()

    caption = f"🎬 {quality} · <code>{_html(prompt)[:600]}</code>"
    if is_gif:
        # Try real-gif conversion via ffmpeg; fall back to mp4-as-animation.
        gif_bytes = await _convert_mp4_to_gif(mp4_bytes)
        if gif_bytes:
            with contextlib.suppress(Exception):
                await msg.answer_animation(
                    BufferedInputFile(gif_bytes, filename="loop.gif"),
                    caption=caption,
                )
            await msg.answer_document(
                BufferedInputFile(gif_bytes, filename="loop.gif"),
            )
        else:
            # ffmpeg not available — send mp4 as animation (TG renders it
            # like a GIF) and also as the source file.
            with contextlib.suppress(Exception):
                await msg.answer_animation(
                    BufferedInputFile(mp4_bytes, filename="loop.mp4"),
                    caption=caption + "\n<i>ffmpeg недоступен; отдаю mp4-as-animation</i>",
                )
            await msg.answer_document(BufferedInputFile(mp4_bytes, filename="loop.mp4"))
    else:
        # Inline preview + uncompressed MP4 document.
        with contextlib.suppress(Exception):
            await msg.answer_video(
                BufferedInputFile(mp4_bytes, filename="video.mp4"),
                caption=caption,
            )
        await msg.answer_document(
            BufferedInputFile(mp4_bytes, filename="video.mp4"),
        )
    await state.clear()


async def _convert_mp4_to_gif(mp4_bytes: bytes) -> bytes | None:
    """Convert mp4 bytes to gif via ffmpeg subprocess. Returns ``None``
    if ffmpeg is missing or the conversion fails.

    Uses palettegen+paletteuse for good color, capped at 12 fps / 480px
    width to keep the gif size reasonable.
    """
    import asyncio
    import shutil
    import tempfile
    from pathlib import Path as _P

    if not shutil.which("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        mp4 = _P(tmp) / "in.mp4"
        gif = _P(tmp) / "out.gif"
        mp4.write_bytes(mp4_bytes)
        # Two-pass via filter_complex inside a single ffmpeg invocation.
        cmd = [
            "ffmpeg", "-y", "-i", str(mp4),
            "-vf",
            "fps=12,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-loop", "0",
            str(gif),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("ffmpeg mp4→gif failed: %s", stderr.decode(errors="replace")[:300])
            return None
        return gif.read_bytes()


# ---- private helpers ----------------------------------------------------


async def _download_photo(bot, file_id: str) -> bytes:
    """Download the largest TG photo behind ``file_id`` as bytes."""
    tg_file = await bot.get_file(file_id)
    buf = await bot.download_file(tg_file.file_path)
    if hasattr(buf, "read"):
        return buf.read()
    if isinstance(buf, (bytes, bytearray)):
        return bytes(buf)
    # aiogram may return an aiohttp BytesIO-like buffer; coerce.
    return bytes(buf or b"")


async def _fetch_url(url: str) -> bytes:
    # OpenAI-compatible adapter returns ``data:image/...;base64,<...>``
    # inline payloads — decode them locally instead of hitting the network.
    if url.startswith("data:"):
        import base64 as _b64
        try:
            _meta, _, data = url.partition(",")
            if ";base64" in _meta:
                return _b64.b64decode(data)
            # urlencoded text payload — best-effort
            from urllib.parse import unquote_to_bytes
            return unquote_to_bytes(data)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"bad data: URL ({exc})") from exc
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content
