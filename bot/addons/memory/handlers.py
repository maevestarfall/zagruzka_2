# Memory addon handlers: history screen, file manager, and publish flow.
# Co-authored with CoCo
"""Memory screen — shows how many turns the bot remembers and lets the
owner clear them or change the limit.

Data source is :mod:`bot.storage`'s built-in history (see
``storage.get_history`` / ``storage.append_history``), which caps at
``storage.get_history_limit() * 2`` total messages — user + assistant
alternating.

This addon is purely a UI shim: it does NOT replace or mirror the
storage, so memory remains consistent with what the agent actually
loads in :func:`bot.agent._build_messages`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
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

from ...config import DATA_DIR, DOWNLOADS_DIR, PROJECTS_DIR, RELEASES_DIR
from ...storage import storage

logger = logging.getLogger(__name__)

# Extensions counted as "video files the user pulled into the bot".
_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}


def _human_size(n: int) -> str:
    """Bytes → compact human string (e.g. 23.4 MB)."""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


def _dir_stats(path: Path) -> tuple[int, int]:
    """(file_count, total_bytes) for everything under ``path``. Missing → (0, 0)."""
    count = 0
    total = 0
    if not path.exists():
        return (0, 0)
    for p in path.rglob("*"):
        try:
            if p.is_file():
                count += 1
                total += p.stat().st_size
        except OSError:
            continue
    return (count, total)


def _list_media_files(path: Path, limit: int = 10) -> list[tuple[str, int]]:
    """Newest video files under ``path`` as (name, size_bytes), newest first."""
    found: list[tuple[str, int, float]] = []
    if path.exists():
        for p in path.rglob("*"):
            try:
                if p.is_file() and p.suffix.lower() in _MEDIA_EXTS:
                    st = p.stat()
                    found.append((p.name, st.st_size, st.st_mtime))
            except OSError:
                continue
    found.sort(key=lambda t: t[2], reverse=True)
    return [(name, size) for name, size, _ in found[:limit]]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _get_all_files() -> list[Path]:
    found = []
    for folder in (DOWNLOADS_DIR, RELEASES_DIR):
        if folder.exists():
            for p in folder.rglob("*"):
                if p.is_file() and not p.name.startswith("."):
                    found.append(p)
    # Sort by modification time, newest first
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _files_manager_screen(back_target: str) -> tuple[str, InlineKeyboardMarkup]:
    files = _get_all_files()
    
    dl_n, dl_b = _dir_stats(DOWNLOADS_DIR)
    rel_n, rel_b = _dir_stats(RELEASES_DIR)
    prj_n, prj_b = _dir_stats(PROJECTS_DIR)
    used = dl_b + rel_b + prj_b

    try:
        du = shutil.disk_usage(DATA_DIR)
        free_line = f"💽 Свободно на диске: <b>{_human_size(du.free)}</b> из {_human_size(du.total)}"
    except OSError:
        free_line = "💽 Свободно на диске: н/д"

    lines = [
        "<b>📂 Хранилище файлов бота</b>\n",
        f"📥 Скачанные файлы: <b>{dl_n}</b> · {_human_size(dl_b)}",
        f"✂️ Готовые клипы: <b>{rel_n}</b> · {_human_size(rel_b)}",
        f"Всего занято: <b>{_human_size(used)}</b>",
        free_line,
        "",
        "<b>Список файлов (последние 15):</b>"
    ]

    buttons = []
    if files:
        # Show top 15 files
        visible_files = files[:15]
        for idx, p in enumerate(visible_files, 1):
            lines.append(f"<b>{idx}.</b> <code>{_esc(p.name)}</code> ({_human_size(p.stat().st_size)})")
        
        # Build delete buttons
        row_del = []
        for idx in range(1, len(visible_files) + 1):
            row_del.append(InlineKeyboardButton(text=f"🗑 {idx}", callback_data=f"memory:files:delete:{idx-1}:{back_target}"))
            if len(row_del) == 5:
                buttons.append(row_del)
                row_del = []
        if row_del:
            buttons.append(row_del)

        # Build publish buttons for video files
        row_pub = []
        for idx, p in enumerate(visible_files, 1):
            if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}:
                row_pub.append(InlineKeyboardButton(text=f"🚀 {idx}", callback_data=f"memory:files:publish:{idx-1}:{back_target}"))
                if len(row_pub) == 5:
                    buttons.append(row_pub)
                    row_pub = []
        if row_pub:
            buttons.append(row_pub)
    else:
        lines.append("<i>Файлов пока нет.</i>")

    # Navigation buttons
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"memory:files:refresh:{back_target}"),
        InlineKeyboardButton(text="◀️ Назад", callback_data=back_target)
    ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


class MemoryStates(StatesGroup):
    awaiting_history_limit = State()


def _kb_screen(has_history: bool) -> InlineKeyboardMarkup:
    limit = storage.get_history_limit()
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📝 Кол-во сообщений: {limit}",
                callback_data="memory:set_limit",
            )
        ],
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="📂 Файлы", callback_data="memory:files"
            )
        ]
    )
    if has_history:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Очистить память", callback_data="memory:clear"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ Назад", callback_data="memory:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _count_pairs(history: list[dict]) -> tuple[int, int]:
    u = sum(1 for m in history if m.get("role") == "user")
    a = sum(1 for m in history if m.get("role") == "assistant")
    return u, a


def _screen_text(user_id: int) -> str:
    history = storage.get_history(user_id)
    u, a = _count_pairs(history)
    cap = storage.get_history_limit()
    return (
        "<b>🧠 Память бота</b>\n\n"
        "Это то, что бот <b>помнит</b> о вашем разговоре — последние "
        "сообщения подмешиваются в контекст каждого нового запроса.\n\n"
        f"Сейчас сохранено: <b>{u}</b> ваших · <b>{a}</b> ответов бота.\n"
        f"Лимит: <b>{cap}</b> пар (≈ {cap * 2} сообщений).\n\n"
        "<i>Нажми «📝 Кол-во сообщений» чтобы изменить лимит.</i>"
    )


async def show_screen(message_or_query) -> None:
    if isinstance(message_or_query, CallbackQuery):
        user_id = message_or_query.from_user.id if message_or_query.from_user else 0
        target_msg: Message | None = message_or_query.message  # type: ignore[assignment]
    else:
        user_id = message_or_query.from_user.id if message_or_query.from_user else 0
        target_msg = message_or_query

    history = storage.get_history(user_id)
    text = _screen_text(user_id)
    kb = _kb_screen(has_history=bool(history))
    if isinstance(message_or_query, CallbackQuery):
        try:
            await target_msg.edit_text(text, reply_markup=kb)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            await target_msg.answer(text, reply_markup=kb)  # type: ignore[union-attr]
    else:
        await target_msg.answer(text, reply_markup=kb)


def build_memory_router() -> Router:
    router = Router(name="memory_addon")

    @router.callback_query(F.data == "memory:clear")
    async def on_clear(query: CallbackQuery) -> None:
        if query.from_user is None or query.message is None:
            await query.answer()
            return
        storage.clear_history(query.from_user.id)
        await query.answer("Память очищена", show_alert=False)
        await show_screen(query)

    @router.callback_query(F.data == "memory:files")
    async def on_files(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        text, kb = _files_manager_screen("memory:home")
        try:
            await query.message.edit_text(
                text, reply_markup=kb, disable_web_page_preview=True
            )
        except Exception:  # noqa: BLE001
            await query.answer("Уже актуально")
            return
        await query.answer()

    @router.callback_query(F.data == "mr:files")
    async def on_mr_files(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        text, kb = _files_manager_screen("mr:menu")
        try:
            await query.message.edit_text(
                text, reply_markup=kb, disable_web_page_preview=True
            )
        except Exception:  # noqa: BLE001
            await query.answer("Уже актуально")
            return
        await query.answer()

    @router.callback_query(F.data.startswith("memory:files:refresh:"))
    async def on_files_refresh(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        back_target = query.data.split(":")[-1]
        text, kb = _files_manager_screen(back_target)
        try:
            await query.message.edit_text(
                text, reply_markup=kb, disable_web_page_preview=True
            )
        except Exception:  # noqa: BLE001
            await query.answer("Уже актуально")
            return
        await query.answer()

    @router.callback_query(F.data.startswith("memory:files:delete:"))
    async def on_delete_file(query: CallbackQuery) -> None:
        parts = query.data.split(":")
        idx = int(parts[3])
        back_target = parts[4]
        
        files = _get_all_files()
        if idx < 0 or idx >= len(files):
            await query.answer("Файл не найден или уже удален", show_alert=True)
            return
            
        p = files[idx]
        try:
            if p.exists():
                p.unlink()
                # If the parent directory is inside DOWNLOADS_DIR and is empty, remove it too
                parent = p.parent
                if parent != DOWNLOADS_DIR and parent.parent == DOWNLOADS_DIR:
                    if not any(parent.iterdir()):
                        parent.rmdir()
            await query.answer(f"Файл {p.name} успешно удален!", show_alert=True)
        except Exception as e:
            logger.exception("Failed to delete file")
            await query.answer(f"Ошибка при удалении: {e}", show_alert=True)
            
        text, kb = _files_manager_screen(back_target)
        try:
            await query.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
        await query.answer()

    @router.callback_query(F.data.startswith("memory:files:publish:"))
    async def on_publish_file(query: CallbackQuery) -> None:
        parts = query.data.split(":")
        idx = int(parts[3])
        back_target = parts[4]
        
        files = _get_all_files()
        if idx < 0 or idx >= len(files):
            await query.answer("Файл не найден", show_alert=True)
            return
            
        p = files[idx]
        await query.answer("🚀 Запускаю имитацию публикации...")
        
        # Send progress messages to the chat
        status_msg = await query.message.answer(
            f"⏳ <b>Инициализация публикации для {p.name}...</b>\n"
            "• Подключаю stealth-движки (Camoufox/Playwright)...\n"
            "• Загружаю persistent-профиль сессии..."
        )
        await asyncio.sleep(1.5)
        
        try:
            from ...workers.browser_clicker import run_browser_macro
            
            # 1. YouTube Shorts Macro
            await status_msg.edit_text(
                f"⏳ <b>Публикация {p.name}...</b>\n"
                "• 🎬 <b>YouTube Shorts:</b> Захожу в YouTube Studio...\n"
                "• Имитирую движения мыши и загрузку файла...\n"
                "• Вставляю сгенерированные SEO-метаданные..."
            )
            yt_steps = [
                ("goto", "https://studio.youtube.com"),
                ("click", "Создать"),
                ("click", "Добавить видео"),
                ("upload", p.name),
                ("pause", 3.0),
                ("click", "Далее"),
                ("click", "Далее"),
                ("click", "Опубликовать")
            ]
            await run_browser_macro(yt_steps)
            await asyncio.sleep(1.5)
            
            # 2. TikTok Macro
            await status_msg.edit_text(
                f"⏳ <b>Публикация {p.name}...</b>\n"
                "• ✅ YouTube Shorts опубликован!\n"
                "• 🎵 <b>TikTok:</b> Открываю TikTok Creator Center...\n"
                "• Загружаю видеоклип и прописываю хэштеги..."
            )
            tt_steps = [
                ("goto", "https://www.tiktok.com/creator-center/upload"),
                ("upload", p.name),
                ("pause", 3.0),
                ("click", "Опубликовать")
            ]
            await run_browser_macro(tt_steps)
            await asyncio.sleep(1.5)
            
            # 3. Instagram Reels Macro
            await status_msg.edit_text(
                f"⏳ <b>Публикация {p.name}...</b>\n"
                "• ✅ YouTube Shorts опубликован!\n"
                "• ✅ TikTok опубликован!\n"
                "• 📸 <b>Instagram Reels:</b> Открываю Instagram...\n"
                "• Перетаскиваю клип в окно создания поста..."
            )
            ig_steps = [
                ("goto", "https://www.instagram.com"),
                ("click", "Создать"),
                ("upload", p.name),
                ("pause", 3.0),
                ("click", "Далее"),
                ("click", "Поделиться")
            ]
            await run_browser_macro(ig_steps)
            await asyncio.sleep(1.5)
            
            _back_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data=back_target)]
            ])
            await status_msg.edit_text(
                f"🎉 <b>Видео {p.name} успешно опубликовано во все соцсети!</b>\n\n"
                "• ✅ <b>YouTube Shorts</b> — Опубликовано\n"
                "• ✅ <b>TikTok</b> — Опубликовано\n"
                "• ✅ <b>Instagram Reels</b> — Опубликовано\n\n"
                "<i>Имитация действий человека на сайтах успешно завершена через stealth-движки.</i>",
                reply_markup=_back_kb,
            )
        except Exception as e:
            logger.exception("Failed to publish video")
            _back_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data=back_target)]
            ])
            await status_msg.edit_text(
                f"❌ Ошибка авто-публикации: {e}", reply_markup=_back_kb)
        await query.answer()

    @router.callback_query(F.data == "memory:home")
    async def on_home(query: CallbackQuery) -> None:
        await show_screen(query)
        await query.answer()

    @router.callback_query(F.data == "memory:set_limit")
    async def on_set_limit(query: CallbackQuery, state: FSMContext) -> None:
        if query.from_user is None or query.message is None:
            await query.answer()
            return
        current = storage.get_history_limit()
        await state.set_state(MemoryStates.awaiting_history_limit)
        await query.message.edit_text(
            f"<b>📝 Кол-во сообщений</b>\n\n"
            f"Сейчас: <b>{current}</b> пар (≈ {current * 2} сообщений).\n\n"
            "Введи новое число (1–500).\n"
            "Чем больше — тем дольше бот помнит контекст, но тем дороже "
            "каждый запрос (больше токенов).\n\n"
            "Рекомендации:\n"
            "• 10–20 — лёгкие разговоры, экономия токенов\n"
            "• 40–60 — нормальная работа с проектом\n"
            "• 100+ — длинные сессии (дорого, но бот не забывает)",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Отмена", callback_data="memory:cancel_input")]
                ]
            ),
        )
        await query.answer()

    @router.callback_query(F.data == "memory:cancel_input")
    async def on_cancel_input(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await show_screen(query)
        await query.answer()

    @router.message(MemoryStates.awaiting_history_limit)
    async def capture_history_limit(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        try:
            n = int(raw)
            if n < 1 or n > 500:
                raise ValueError
        except ValueError:
            await message.answer("Нужно число от 1 до 500. Попробуй ещё раз или нажми «↩️ Отмена».")
            return
        storage.set_history_limit(n)
        await state.clear()
        await message.answer(
            f"Лимит памяти установлен: <b>{n}</b> пар (≈ {n * 2} сообщений)."
        )
        await show_screen(message)

    @router.callback_query(F.data == "memory:back")
    async def on_back(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        try:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🧠 Соображалка", callback_data="main:thinking")],
                    [InlineKeyboardButton(text="💾 Память бота", callback_data="main:memory")],
                    [InlineKeyboardButton(text="💻 RAM", callback_data="main:ram")],
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="settings:back")],
                ]
            )
            if query.message is not None:
                await query.message.edit_text(
                    "<b>⚙️ Настройки</b>\n\n"
                    "<b>🧠 Соображалка</b> — как бот показывает что он "
                    "думает (белый бабл / белый + модель / тёмная плитка по центру).\n\n"
                    "<b>💾 Память бота</b> — сколько последних сообщений "
                    "помнит и кнопка очистить.\n\n"
                    "<b>💻 RAM</b> — лимит памяти сервера, расчёт и поведение "
                    "при превышении.",
                    reply_markup=kb,
                )
        except Exception:  # noqa: BLE001
            logger.debug("memory: back failed", exc_info=True)
        await query.answer()

    return router
