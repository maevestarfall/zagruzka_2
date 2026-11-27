# Algo-slots handlers: preset list, step-by-step config, params, and run flow.
# Co-authored with CoCo
"""«🧩 Алгоритм» — UI for reusable photo→video preset slots.

Flow: list slots → enter a slot → [▶️ Использовать] [⚙️ Настроить] [↩️].
Configure asks (each skippable): фото-пример → промт фото → фото для видео
(превью галереи) → промт видео → 📐 параметры (модель/формат/качество ×2).
Run: show reference + photo prompt → user sends photo → img2img → result +
[🔁 ещё] [🎬 видео] → img2video using the just-generated image.

Isolated: own callback prefix ``aps:``, own state key. Reuses
``bot.media.image_to_image`` / ``image_to_video`` with per-slot overrides.
"""

from __future__ import annotations

import base64
import contextlib
import io
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


class AlgoStates(StatesGroup):
    cfg_ref = State()
    cfg_photo_prompt = State()
    cfg_preview = State()
    cfg_video_prompt = State()
    param_custom = State()   # custom model/quality text
    use_photo = State()      # run: user inserts a photo


def _esc(s) -> str:
    import html
    return html.escape(str(s or ""))


async def _owner(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner
    return _is_owner(query.from_user.id if query.from_user else 0)


async def _photo_bytes(message: Message) -> bytes | None:
    """Download the largest photo (or image document) the user sent."""
    fid = None
    if message.photo:
        fid = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        fid = message.document.file_id
    if not fid:
        return None
    buf = io.BytesIO()
    with contextlib.suppress(Exception):
        await message.bot.download(fid, destination=buf)
        return buf.getvalue()
    return None


async def _media_url_to_bytes(url: str) -> bytes | None:
    """Resolve an img2img result URL (http or data:) to raw bytes."""
    if not url:
        return None
    if url.startswith("data:"):
        with contextlib.suppress(Exception):
            b64 = url.split(",", 1)[1]
            return base64.b64decode(b64)
        return None
    with contextlib.suppress(Exception):
        import httpx
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.get(url)
            if r.status_code < 400:
                return r.content
    return None


# ─── Param helpers ──────────────────────────────────────────────────────────

def _photo_size(slot: dict) -> str | None:
    """Build a WIDTHxHEIGHT size string from slot photo params."""
    p = (slot.get("params") or {}).get("photo") or {}
    q = (p.get("quality") or "").strip()
    fmt = (p.get("format") or "").strip()
    if not q:
        return None
    try:
        base = int("".join(ch for ch in q if ch.isdigit()) or "0")
    except ValueError:
        return None
    if base <= 0:
        return None
    if fmt == "16:9":
        return f"{base}x{int(base * 9 / 16)}"
    if fmt == "9:16":
        return f"{int(base * 9 / 16)}x{base}"
    return f"{base}x{base}"  # 1:1 or unset


# ─── Keyboards ────────────────────────────────────────────────────────────────

def menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🧩 Алгоритм", callback_data="aps:home")


def _kb_home() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in store.get_slots():
        flags = ("🖼" if s.get("has_ref") else "·") + ("🎬" if s.get("has_preview") else "·")
        rows.append([InlineKeyboardButton(
            text=f"{flags} {s.get('name')}", callback_data=f"aps:slot:{s['id']}")])
    if len(store.get_slots()) < store.MAX_SLOTS:
        rows.append([InlineKeyboardButton(text="➕ Новый слот", callback_data="aps:add")])
    rows.append([InlineKeyboardButton(text="👁 Примеры (галерея)", callback_data="aps:gallery")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="main:media_toggle")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_slot(slot: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Использовать", callback_data=f"aps:use:{slot['id']}")],
        [InlineKeyboardButton(text="⚙️ Настроить (изменить)", callback_data=f"aps:cfg:{slot['id']}")],
        [InlineKeyboardButton(text="📐 Параметры фото и видео", callback_data=f"aps:params:{slot['id']}")],
        [InlineKeyboardButton(text="🗑 Удалить слот", callback_data=f"aps:del:{slot['id']}")],
        [InlineKeyboardButton(text="↩️ Вернуться", callback_data="aps:home")],
    ])


def _kb_skip(slot_id: str, step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"aps:skip:{slot_id}:{step}")],
        [InlineKeyboardButton(text="✖ Отмена", callback_data=f"aps:slot:{slot_id}")],
    ])


def _kb_params(slot: dict) -> InlineKeyboardMarkup:
    sid = slot["id"]
    p = (slot.get("params") or {}).get("photo") or {}
    v = (slot.get("params") or {}).get("video") or {}
    def mk(label, cur):
        return f"{label}: {cur or '—'}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ФОТО", callback_data="aps:noop")],
        [InlineKeyboardButton(text=mk("модель", p.get("model")), callback_data=f"aps:pp:{sid}:photo:model")],
        [InlineKeyboardButton(text=mk("формат", p.get("format")), callback_data=f"aps:pp:{sid}:photo:format")],
        [InlineKeyboardButton(text=mk("качество", p.get("quality")), callback_data=f"aps:pp:{sid}:photo:quality")],
        [InlineKeyboardButton(text="🎬 ВИДЕО", callback_data="aps:noop")],
        [InlineKeyboardButton(text=mk("качество", v.get("quality")), callback_data=f"aps:pp:{sid}:video:quality")],
        [InlineKeyboardButton(text=mk("формат", v.get("format")), callback_data=f"aps:pp:{sid}:video:format")],
        [InlineKeyboardButton(text=mk("модель", v.get("model")), callback_data=f"aps:pp:{sid}:video:model")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"aps:slot:{sid}")],
    ])


def _kb_param_choices(sid: str, media: str, key: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if key == "format":
        opts = ["16:9", "9:16", "1:1"] if media == "photo" else ["9:16", "16:9"]
        rows = [[InlineKeyboardButton(text=o, callback_data=f"aps:ps:{sid}:{media}:format:{o}")] for o in opts]
    elif key == "quality":
        opts = ["512", "768", "1024"] if media == "photo" else ["480", "720"]
        rows = [[InlineKeyboardButton(text=o, callback_data=f"aps:ps:{sid}:{media}:quality:{o}")] for o in opts]
        rows.append([InlineKeyboardButton(text="✍️ Своё (число)", callback_data=f"aps:pc:{sid}:{media}:quality")])
    elif key == "model":
        rows.append([InlineKeyboardButton(text="✍️ Ввести модель", callback_data=f"aps:pc:{sid}:{media}:model")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"aps:params:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_result(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Ещё попытка", callback_data=f"aps:use:{sid}")],
        [InlineKeyboardButton(text="🎬 Генерация видео", callback_data=f"aps:video:{sid}")],
        [InlineKeyboardButton(text="↩️ К слоту", callback_data=f"aps:slot:{sid}")],
    ])


def _slot_text(slot: dict) -> str:
    p = (slot.get("params") or {}).get("photo") or {}
    v = (slot.get("params") or {}).get("video") or {}
    return (
        f"<b>🧩 {_esc(slot.get('name'))}</b>\n\n"
        f"🖼 фото-пример: {'есть' if slot.get('has_ref') else 'нет'}\n"
        f"✍️ промт фото: {_esc((slot.get('photo_prompt') or '')[:60]) or 'нет'}\n"
        f"🎬 превью видео: {'есть' if slot.get('has_preview') else 'нет'}\n"
        f"✍️ промт видео: {_esc((slot.get('video_prompt') or '')[:60]) or 'нет'}\n"
        f"📐 фото: {p.get('model') or '—'} · {p.get('format') or '—'} · {p.get('quality') or '—'}\n"
        f"📐 видео: {v.get('model') or '—'} · {v.get('format') or '—'} · {v.get('quality') or '—'}"
    )


def build_algo_slots_router() -> Router:
    router = Router(name="algo_slots")

    @router.callback_query(F.data == "aps:noop")
    async def _noop(query: CallbackQuery) -> None:
        await query.answer()

    @router.callback_query(F.data == "aps:home")
    async def _home(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            await query.answer("Только владелец.", show_alert=True)
            return
        await state.clear()
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>🧩 Алгоритм — пресеты фото→видео</b>\n\n"
                "Выбери слот или создай новый.", parse_mode="HTML",
                reply_markup=_kb_home())

    @router.callback_query(F.data == "aps:add")
    async def _add(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        s = store.add_slot()
        if not s:
            await query.answer("Достигнут лимит 10 слотов.", show_alert=True)
            return
        await query.answer("Создан")
        with contextlib.suppress(Exception):
            await query.message.edit_text(_slot_text(s), parse_mode="HTML",
                                          reply_markup=_kb_slot(s))

    @router.callback_query(F.data.startswith("aps:slot:"))
    async def _slot(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.clear()
        sid = query.data.split(":")[-1]
        s = store.get_slot(sid)
        if not s:
            await query.answer("Слот не найден.", show_alert=True)
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(_slot_text(s), parse_mode="HTML",
                                          reply_markup=_kb_slot(s))

    @router.callback_query(F.data.startswith("aps:del:"))
    async def _del(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        sid = query.data.split(":")[-1]
        store.remove_slot(sid)
        await query.answer("Слот удалён")
        with contextlib.suppress(Exception):
            await query.message.edit_text("<b>🧩 Алгоритм</b>", parse_mode="HTML",
                                          reply_markup=_kb_home())

    # ── Configure (step chain) ──────────────────────────────────────────────
    @router.callback_query(F.data.startswith("aps:cfg:"))
    async def _cfg(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        sid = query.data.split(":")[-1]
        await state.update_data(slot_id=sid)
        await state.set_state(AlgoStates.cfg_ref)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>⚙️ Настройка — шаг 1/4</b>\n\nПришли <b>фото-пример</b> "
                "(референс, показывается при запуске).",
                parse_mode="HTML", reply_markup=_kb_skip(sid, "ref"))

    async def _ask_photo_prompt(target, sid: str, state: FSMContext):
        await state.set_state(AlgoStates.cfg_photo_prompt)
        await target.answer(
            "<b>Шаг 2/4</b> — пришли <b>промт для фото</b> (текст).",
            parse_mode="HTML", reply_markup=_kb_skip(sid, "photo_prompt"))

    async def _ask_preview(target, sid: str, state: FSMContext):
        await state.set_state(AlgoStates.cfg_preview)
        await target.answer(
            "<b>Шаг 3/4</b> — пришли <b>фото для видео</b> (превью галереи).",
            parse_mode="HTML", reply_markup=_kb_skip(sid, "preview"))

    async def _ask_video_prompt(target, sid: str, state: FSMContext):
        await state.set_state(AlgoStates.cfg_video_prompt)
        await target.answer(
            "<b>Шаг 4/4</b> — пришли <b>промт для видео</b> (текст).",
            parse_mode="HTML", reply_markup=_kb_skip(sid, "video_prompt"))

    async def _cfg_done(target, sid: str, state: FSMContext):
        await state.clear()
        s = store.get_slot(sid)
        await target.answer("✅ Слот настроен.", parse_mode="HTML",
                            reply_markup=_kb_slot(s) if s else None)

    @router.message(AlgoStates.cfg_ref)
    async def _cfg_ref(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("slot_id")
        img = await _photo_bytes(message)
        if img is None:
            await message.answer("Нужно фото. Пришли картинку или нажми «Пропустить».")
            return
        store.save_image(sid, "ref", img)
        await _ask_photo_prompt(message, sid, state)

    @router.message(AlgoStates.cfg_photo_prompt, F.text)
    async def _cfg_pp(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("slot_id")
        store.update_slot(sid, photo_prompt=(message.text or "").strip())
        await _ask_preview(message, sid, state)

    @router.message(AlgoStates.cfg_preview)
    async def _cfg_prev(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("slot_id")
        img = await _photo_bytes(message)
        if img is None:
            await message.answer("Нужно фото. Пришли картинку или нажми «Пропустить».")
            return
        store.save_image(sid, "preview", img)
        await _ask_video_prompt(message, sid, state)

    @router.message(AlgoStates.cfg_video_prompt, F.text)
    async def _cfg_vp(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("slot_id")
        store.update_slot(sid, video_prompt=(message.text or "").strip())
        await _cfg_done(message, sid, state)

    @router.callback_query(F.data.startswith("aps:skip:"))
    async def _skip(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        _, _, sid, step = query.data.split(":", 3)
        await query.answer("Пропущено")
        msg = query.message
        if step == "ref":
            await _ask_photo_prompt(msg, sid, state)
        elif step == "photo_prompt":
            await _ask_preview(msg, sid, state)
        elif step == "preview":
            await _ask_video_prompt(msg, sid, state)
        else:
            await _cfg_done(msg, sid, state)

    # ── Params ──────────────────────────────────────────────────────────────
    @router.callback_query(F.data.startswith("aps:params:"))
    async def _params(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        await state.clear()
        sid = query.data.split(":")[-1]
        s = store.get_slot(sid)
        if not s:
            await query.answer("Слот не найден.", show_alert=True)
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>📐 Параметры фото и видео</b>\n\nНажми параметр, чтобы изменить.",
                parse_mode="HTML", reply_markup=_kb_params(s))

    @router.callback_query(F.data.startswith("aps:pp:"))
    async def _param_pick(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        _, _, sid, media, key = query.data.split(":", 4)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"<b>📐 {media} · {key}</b>\n\nВыбери значение:",
                parse_mode="HTML", reply_markup=_kb_param_choices(sid, media, key))

    @router.callback_query(F.data.startswith("aps:ps:"))
    async def _param_set(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        _, _, sid, media, key, value = query.data.split(":", 5)
        store.set_param(sid, media, key, value)
        await query.answer("Сохранено")
        s = store.get_slot(sid)
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "<b>📐 Параметры фото и видео</b>", parse_mode="HTML",
                reply_markup=_kb_params(s))

    @router.callback_query(F.data.startswith("aps:pc:"))
    async def _param_custom(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        _, _, sid, media, key = query.data.split(":", 4)
        await state.update_data(pc_sid=sid, pc_media=media, pc_key=key)
        await state.set_state(AlgoStates.param_custom)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"Пришли значение для <b>{media} · {key}</b> текстом.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отмена", callback_data=f"aps:params:{sid}")]]))

    @router.message(AlgoStates.param_custom, F.text)
    async def _param_custom_capture(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid, media, key = data.get("pc_sid"), data.get("pc_media"), data.get("pc_key")
        store.set_param(sid, media, key, (message.text or "").strip())
        await state.clear()
        s = store.get_slot(sid)
        await message.answer("✅ Сохранено.", reply_markup=_kb_params(s) if s else None)

    # ── Gallery ──────────────────────────────────────────────────────────────
    @router.callback_query(F.data == "aps:gallery")
    async def _gallery(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        await query.answer()
        slots = [s for s in store.get_slots() if s.get("has_preview")][:store.GALLERY_MAX]
        if not slots:
            with contextlib.suppress(Exception):
                await query.message.answer("Нет превью. Настрой слоты (шаг «фото для видео»).")
            return
        for s in slots:
            img = store.load_image(s["id"], "preview")
            if img:
                with contextlib.suppress(Exception):
                    await query.message.answer_photo(
                        BufferedInputFile(img, filename="preview.jpg"),
                        caption=f"🧩 {_esc(s.get('name'))}")
        with contextlib.suppress(Exception):
            await query.message.answer("Галерея ⤴️", reply_markup=_kb_home())

    # ── Use (run) ────────────────────────────────────────────────────────────
    @router.callback_query(F.data.startswith("aps:use:"))
    async def _use(query: CallbackQuery, state: FSMContext) -> None:
        if not await _owner(query):
            return
        sid = query.data.split(":")[-1]
        s = store.get_slot(sid)
        if not s:
            await query.answer("Слот не найден.", show_alert=True)
            return
        await state.set_state(AlgoStates.use_photo)
        await state.update_data(slot_id=sid)
        await query.answer()
        msg = query.message
        ref = store.load_image(sid, "ref")
        cap = (f"<b>▶️ {_esc(s.get('name'))}</b>\n\n"
               f"Промт фото: <i>{_esc(s.get('photo_prompt') or '—')}</i>\n\n"
               "Пришли <b>фото</b> — сгенерирую по этому промту с твоими "
               "параметрами.")
        with contextlib.suppress(Exception):
            if ref:
                await msg.answer_photo(BufferedInputFile(ref, filename="ref.jpg"),
                                       caption=cap, parse_mode="HTML")
            else:
                await msg.answer(cap, parse_mode="HTML")

    @router.message(AlgoStates.use_photo)
    async def _use_photo(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        sid = data.get("slot_id")
        s = store.get_slot(sid)
        if not s:
            await state.clear()
            return
        img = await _photo_bytes(message)
        if img is None:
            await message.answer("Нужно фото. Пришли картинку.")
            return
        prog = await message.answer("⏳ Генерирую картинку…")
        pm = (s.get("params") or {}).get("photo") or {}
        from ...media import image_to_image, MediaError
        try:
            png_url, _ = await image_to_image(
                img, s.get("photo_prompt") or "",
                model=(pm.get("model") or None),
                size=_photo_size(s),
            )
        except MediaError as e:
            await prog.edit_text(f"❌ Генерация не удалась: {e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("aps: img2img failed")
            await prog.edit_text(f"❌ Ошибка: {type(e).__name__}: {e}")
            return
        result_bytes = await _media_url_to_bytes(png_url)
        await state.clear()
        with contextlib.suppress(Exception):
            await prog.delete()
        if not result_bytes:
            await message.answer("Готово, но не смог получить картинку-результат.")
            return
        # cache result for the video step
        store.save_image(sid, "last_result", result_bytes)
        with contextlib.suppress(Exception):
            await message.answer_photo(
                BufferedInputFile(result_bytes, filename="result.jpg"),
                caption="✅ Готово. Дальше?", reply_markup=_kb_result(sid))

    @router.callback_query(F.data.startswith("aps:video:"))
    async def _video(query: CallbackQuery) -> None:
        if not await _owner(query):
            return
        sid = query.data.split(":")[-1]
        s = store.get_slot(sid)
        if not s:
            await query.answer("Слот не найден.", show_alert=True)
            return
        result = store.load_image(sid, "last_result")
        if not result:
            await query.answer("Сначала сгенерируй картинку («Использовать»).", show_alert=True)
            return
        await query.answer("🎬 Генерирую видео…")
        prog = await query.message.answer("⏳ Генерирую видео из картинки…")
        vp = (s.get("params") or {}).get("video") or {}
        from ...media import image_to_video, MediaError
        try:
            video_url = await image_to_video(
                result, s.get("video_prompt") or "",
                quality=(vp.get("quality") or "auto"),
                model=(vp.get("model") or None),
                aspect=(vp.get("format") or None),
            )
        except MediaError as e:
            await prog.edit_text(f"❌ Видео не удалось: {e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("aps: img2video failed")
            await prog.edit_text(f"❌ Ошибка: {type(e).__name__}: {e}")
            return
        with contextlib.suppress(Exception):
            await prog.delete()
        with contextlib.suppress(Exception):
            await query.message.answer_video(video_url, caption="🎬 Готово!",
                                              reply_markup=_kb_slot(s))

    return router
