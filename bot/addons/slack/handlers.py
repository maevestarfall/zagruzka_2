"""Slack addon — aiogram router: connect → pick channel/DM → read & download
→ free-form prompt.

Reached from «🧩 Прочие → 💬 Слак» (callback ``slack:home``). Submenu:

* 🔌 Подключить Slack — enter/save the OAuth token (auth.test verified).
* 📡 Выбрать канал/DM — list visible conversations and pick one.
* 📝 Вставить промт — free-form instruction; the bot parses intent and acts.
* ⚙️ Промт по умолчанию — view / edit / toggle the default prompt.

Everything heavy (Slack API, file downloads) runs in a thread executor so the
aiogram event loop is never blocked. The token is never logged or echoed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import state as addon_state
from . import parsers
from .config import DEFAULT_PROMPT, get_settings
from .credentials import (
    clear_token,
    is_configured,
    load_token,
    looks_like_slack_token,
    mask_token,
    save_token,
)

logger = logging.getLogger(__name__)

_ADDON = "slack"
_AWAIT_KEY = "awaiting"          # one of: token | prompt | defprompt
_TELEGRAM_DOC_LIMIT = 49 * 1024 * 1024  # bots can send up to ~50MB


# ---- awaiting helpers ----------------------------------------------------


def _start_awaiting(chat_id: int, mode: str) -> None:
    addon_state.chat_set(_ADDON, chat_id, _AWAIT_KEY, mode)


def _stop_awaiting(chat_id: int) -> None:
    addon_state.chat_set(_ADDON, chat_id, _AWAIT_KEY, "")


def _awaiting(chat_id: int) -> str:
    return addon_state.chat_get(_ADDON, chat_id, _AWAIT_KEY, "") or ""


# ---- default-prompt state ------------------------------------------------


def _default_prompt_enabled() -> bool:
    return bool(addon_state.get(_ADDON, "default_prompt_enabled", True))


def _default_prompt_text() -> str:
    return addon_state.get(_ADDON, "default_prompt", DEFAULT_PROMPT) or DEFAULT_PROMPT


def _selected_channel() -> tuple[str, str]:
    return (
        addon_state.get(_ADDON, "sel_channel_id", "") or "",
        addon_state.get(_ADDON, "sel_channel_label", "") or "",
    )


# ---- keyboards -----------------------------------------------------------


def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключить Slack", callback_data="slack:connect")],
        [InlineKeyboardButton(text="📡 Выбрать канал/DM", callback_data="slack:channels")],
        [InlineKeyboardButton(text="📝 Вставить промт", callback_data="slack:prompt")],
        [InlineKeyboardButton(text="⚙️ Промт по умолчанию", callback_data="slack:defprompt")],
        [InlineKeyboardButton(text="🧹 Отключить Slack", callback_data="slack:disconnect")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="slack:back")],
    ])


def _kb_defprompt() -> InlineKeyboardMarkup:
    toggle = "🟢 Включён" if _default_prompt_enabled() else "⚪ Выключен"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="slack:defprompt:edit")],
        [InlineKeyboardButton(text=f"{toggle} (переключить)", callback_data="slack:defprompt:toggle")],
        [InlineKeyboardButton(text="♻️ Сбросить к стандартному", callback_data="slack:defprompt:reset")],
        [InlineKeyboardButton(text="▶️ Выполнить дефолтный промт", callback_data="slack:run")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="slack:home")],
    ])


def _kb_back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="slack:home")],
    ])


# ---- screen text ---------------------------------------------------------


def _main_text() -> str:
    cid, label = _selected_channel()
    if not is_configured():
        return (
            "<b>💬 Слак</b>\n\n"
            "⚠ Slack не подключён. Нажми <b>🔌 Подключить Slack</b> и пришли токен.\n\n"
            "Коннектор умеет читать историю каналов и личек <b>вместе со всеми "
            "ответами в тредах</b> и скачивать вложения."
        )
    sel = f"\nВыбран канал: <code>{label or cid}</code>" if cid else "\n<i>Канал не выбран.</i>"
    return (
        "<b>💬 Слак</b>\n\n"
        f"Токен: <code>{mask_token(load_token() or '')}</code>{sel}\n\n"
        "Что умею:\n"
        "• 📡 Выбрать канал/DM — список доступных переписок.\n"
        "• 📝 Вставить промт — свободным текстом: «вынуть таблицей», "
        "«скачать все zip», «таблицу + вложения».\n"
        "• Читаю ВСЮ историю (постранично) со всеми ответами в тредах.\n"
        "• Скачиваю вложения в хранилище и отдаю в Telegram."
    )


def _connect_text() -> str:
    from .client import scopes_hint
    return (
        "<b>🔌 Подключение Slack</b>\n\n"
        "Пришли <b>OAuth-токен</b> одним сообщением. Это Bot token "
        "(<code>xoxb-…</code>) или User token (<code>xoxp-…</code>).\n\n"
        "<b>Нужные права (scopes):</b>\n"
        f"<code>{scopes_hint()}</code>\n\n"
        "• <code>channels:history, groups:history, im:history, mpim:history</code> — "
        "чтение сообщений в публичных, приватных каналах и личках;\n"
        "• <code>users:read</code> — расшифровка имён авторов;\n"
        "• <code>files:read</code> — скачивание файлов.\n\n"
        "<i>Без этих прав чтение/скачивание работать не будет.</i>\n"
        "Токен нигде не показывается и не логируется — твоё сообщение с ним "
        "будет удалено сразу после сохранения."
    )


def _defprompt_text() -> str:
    state = "включён 🟢" if _default_prompt_enabled() else "выключен ⚪"
    return (
        "<b>⚙️ Промт по умолчанию</b>\n\n"
        f"Статус: <b>{state}</b>\n\n"
        "Текущий текст:\n"
        f"<code>{_default_prompt_text()}</code>\n\n"
        "Когда включён — кнопка «▶️ Выполнить дефолтный промт» сразу применит "
        "его к выбранному каналу. «📝 Вставить промт» всегда перекрывает дефолт "
        "тем, что ты введёшь вручную."
    )


# ---- show main -----------------------------------------------------------


async def show_main(target) -> None:
    text, kb = _main_text(), _kb_main()
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await target.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


# ---- execution -----------------------------------------------------------


def _sanitize(name: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", name).strip("_") or "channel"


async def _run_intent(message: Message, intent: parsers.PromptIntent) -> None:
    """Execute a parsed prompt intent against the selected channel."""
    from ..thinking_style import make_status_runner
    from .client import (
        SlackAuthError,
        SlackClient,
        SlackNotConfiguredError,
    )

    cid, label = _selected_channel()
    if not cid:
        await message.answer(
            "Сначала выбери канал: «💬 Слак → 📡 Выбрать канал/DM».",
            reply_markup=_kb_back_home(),
        )
        return

    status_update, status_finish = make_status_runner(message)
    await status_update("Подключаюсь к Slack…")

    loop = asyncio.get_event_loop()
    try:
        client = await loop.run_in_executor(None, SlackClient)
        await status_update("Читаю историю и треды (постранично)…")
        messages = await loop.run_in_executor(None, client.fetch_all_with_threads, cid)
    except SlackNotConfiguredError:
        await status_finish()
        await message.answer("⚠ Slack не подключён.", reply_markup=_kb_back_home())
        return
    except SlackAuthError as e:
        await status_finish()
        await message.answer(f"⚠ {e}", reply_markup=_kb_back_home())
        return
    except Exception as e:  # noqa: BLE001
        await status_finish()
        logger.exception("slack fetch failed")
        await message.answer(f"Ошибка Slack: {e}", reply_markup=_kb_back_home())
        return

    if not messages:
        await status_finish()
        await message.answer("В этом канале нет сообщений (или нет доступа).",
                             reply_markup=_kb_back_home())
        return

    total = sum(1 + len(m.replies) for m in messages)

    # ---- TABLE / DUMP ----
    if intent.want_table:
        await status_update("Формирую таблицу-выгрузку…")
        out_dir = Path(get_settings().storage_root) / _sanitize(label or cid)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        md_path = out_dir / f"slack_dump_{stamp}.md"
        csv_path = out_dir / f"slack_table_{stamp}.csv"
        md_path.write_text(parsers.render_markdown(label or cid, messages), encoding="utf-8")
        csv_path.write_text(parsers.render_csv(messages), encoding="utf-8-sig")

        await status_finish()
        # Inline preview when short.
        if len(messages) <= 10:
            for card in parsers.render_telegram_cards(messages, limit=10):
                with contextlib.suppress(Exception):
                    await message.answer(card)
        await message.answer(
            f"📊 Выгрузка готова: <b>{total}</b> сообщений "
            f"(верхнеуровневых: {len(messages)}). Таблица + читаемый дамп ниже."
        )
        with contextlib.suppress(Exception):
            await message.answer_document(FSInputFile(str(csv_path), filename=csv_path.name),
                                          caption="Таблица переписки (CSV)")
        with contextlib.suppress(Exception):
            await message.answer_document(FSInputFile(str(md_path), filename=md_path.name),
                                          caption="Читаемый дамп с тредами (Markdown)")
        if intent.want_code_map:
            await message.answer(
                "ℹ️ Режим сопоставления функций с кодом — отдельный промт-разбор; "
                "выгрузка выше готова к подаче в него."
            )
    else:
        await status_finish()

    # ---- DOWNLOAD ATTACHMENTS ----
    if intent.want_download:
        pairs = parsers.collect_files(messages, only_zip=intent.only_zip)
        if not pairs:
            kind = "zip-файлов" if intent.only_zip else "вложений"
            await message.answer(f"В переписке не найдено {kind}.", reply_markup=_kb_back_home())
            return
        date_dir = (
            Path(get_settings().storage_root)
            / _sanitize(label or cid)
            / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        await message.answer(
            f"📥 Нашёл файлов: <b>{len(pairs)}</b>"
            + (" (только zip)" if intent.only_zip else "")
            + ". Скачиваю в хранилище…"
        )
        saved: list[Path] = []
        errors: list[str] = []
        for _msg, f in pairs:
            dest = date_dir / f.name  # keep original filename as-is
            try:
                await loop.run_in_executor(None, client.download_file, f, dest)
                saved.append(dest)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{f.name}: {e}")

        # Report saved paths.
        if saved:
            listing = "\n".join(f"• <code>{p}</code>" for p in saved[:50])
            await message.answer(
                f"✅ Сохранено в хранилище ({len(saved)}):\n{listing}"
                + ("\n…" if len(saved) > 50 else "")
            )
            # Push them to Telegram (respecting the bot upload limit).
            for p in saved:
                try:
                    if p.stat().st_size > _TELEGRAM_DOC_LIMIT:
                        await message.answer(
                            f"⚠ {p.name} ({p.stat().st_size // (1024*1024)} МБ) "
                            "слишком велик для Telegram — лежит в хранилище по пути выше."
                        )
                        continue
                    await message.answer_document(FSInputFile(str(p), filename=p.name))
                except Exception as e:  # noqa: BLE001
                    logger.warning("send document failed for %s: %s", p, e)
        if errors:
            await message.answer("⚠ Не скачалось:\n" + "\n".join(errors[:20]))

    await message.answer("Готово.", reply_markup=_kb_back_home())


# ---- router --------------------------------------------------------------


def build_slack_router() -> Router:
    router = Router(name="slack_addon")

    @router.callback_query(F.data == "slack:back")
    async def on_back(query: CallbackQuery) -> None:
        with contextlib.suppress(Exception):
            from ...wizard import _kb_main_after_claim  # type: ignore
            uid = query.from_user.id if query.from_user else None
            if query.message is not None:
                await query.message.edit_text(
                    "Главное меню. Выбери что сделать:",
                    reply_markup=_kb_main_after_claim(uid),
                )
        await query.answer()

    @router.callback_query(F.data == "slack:home")
    async def on_home(query: CallbackQuery) -> None:
        if query.message is not None:
            _stop_awaiting(query.message.chat.id)
        await query.answer()
        await show_main(query)

    @router.callback_query(F.data == "slack:connect")
    async def on_connect(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        _start_awaiting(query.message.chat.id, "token")
        try:
            await query.message.edit_text(_connect_text(), reply_markup=_kb_back_home(),
                                          disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await query.message.answer(_connect_text(), reply_markup=_kb_back_home(),
                                       disable_web_page_preview=True)
        await query.answer()

    @router.callback_query(F.data == "slack:disconnect")
    async def on_disconnect(query: CallbackQuery) -> None:
        clear_token()
        addon_state.delete(_ADDON, "sel_channel_id")
        addon_state.delete(_ADDON, "sel_channel_label")
        await query.answer("Slack отключён, токен удалён")
        await show_main(query)

    @router.callback_query(F.data == "slack:channels")
    async def on_channels(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        from .client import SlackAuthError, SlackClient, SlackNotConfiguredError
        await query.answer()
        loop = asyncio.get_event_loop()
        try:
            client = await loop.run_in_executor(None, SlackClient)
            convs = await loop.run_in_executor(None, client.list_conversations, True)
        except SlackNotConfiguredError:
            await query.message.answer("⚠ Сначала подключи Slack.", reply_markup=_kb_main())
            return
        except SlackAuthError as e:
            await query.message.answer(f"⚠ {e}", reply_markup=_kb_main())
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("list conversations failed")
            await query.message.answer(f"Ошибка Slack: {e}", reply_markup=_kb_main())
            return
        if not convs:
            await query.message.answer("Не найдено каналов/DM, видимых этому токену.",
                                       reply_markup=_kb_main())
            return
        rows: list[list[InlineKeyboardButton]] = []
        for c in convs[:60]:
            rows.append([InlineKeyboardButton(text=c.name[:60], callback_data=f"slack:pick:{c.id}")])
        rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="slack:home")])
        # Stash label lookup for the pick handler.
        addon_state.set_(_ADDON, "conv_labels", {c.id: c.name for c in convs[:60]})
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                f"📡 Доступно переписок: {len(convs)}. Выбери канал/DM:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )

    @router.callback_query(F.data.startswith("slack:pick:"))
    async def on_pick(query: CallbackQuery) -> None:
        if query.message is None or query.data is None:
            await query.answer()
            return
        cid = query.data.split(":", 2)[2]
        labels = addon_state.get(_ADDON, "conv_labels", {}) or {}
        label = labels.get(cid, cid)
        addon_state.set_(_ADDON, "sel_channel_id", cid)
        addon_state.set_(_ADDON, "sel_channel_label", label)
        await query.answer(f"Выбрано: {label}")
        await show_main(query)

    @router.callback_query(F.data == "slack:prompt")
    async def on_prompt(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        cid, label = _selected_channel()
        _start_awaiting(query.message.chat.id, "prompt")
        hint = (
            "<b>📝 Вставить промт</b>\n\n"
            "Напиши, что сделать с перепиской Slack. Примеры:\n"
            "• <i>вынуть все письма в виде таблицы</i>\n"
            "• <i>скачать все zip-файлы в хранилище</i>\n"
            "• <i>вынуть переписку таблицей и скачать все вложения</i>\n\n"
        )
        if cid:
            hint += f"Текущий канал: <code>{label or cid}</code>"
        else:
            hint += "⚠ Канал ещё не выбран — сначала «📡 Выбрать канал/DM»."
        if _default_prompt_enabled():
            hint += f"\n\nЕсли пришлёшь пустое — применю дефолт: <code>{_default_prompt_text()}</code>"
        try:
            await query.message.edit_text(hint, reply_markup=_kb_back_home(),
                                          disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            await query.message.answer(hint, reply_markup=_kb_back_home(),
                                       disable_web_page_preview=True)
        await query.answer()

    @router.callback_query(F.data == "slack:defprompt")
    async def on_defprompt(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        with contextlib.suppress(Exception):
            await query.message.edit_text(_defprompt_text(), reply_markup=_kb_defprompt(),
                                          disable_web_page_preview=True)
        await query.answer()

    @router.callback_query(F.data == "slack:defprompt:edit")
    async def on_defprompt_edit(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        _start_awaiting(query.message.chat.id, "defprompt")
        await query.message.answer(
            "Пришли новый текст промта по умолчанию одним сообщением.\n"
            "Чтобы убрать дефолт — пришли «-» (минус)."
        )
        await query.answer()

    @router.callback_query(F.data == "slack:defprompt:toggle")
    async def on_defprompt_toggle(query: CallbackQuery) -> None:
        cur = _default_prompt_enabled()
        addon_state.set_(_ADDON, "default_prompt_enabled", not cur)
        with contextlib.suppress(Exception):
            await query.message.edit_text(_defprompt_text(), reply_markup=_kb_defprompt(),
                                          disable_web_page_preview=True)
        await query.answer("Дефолтный промт " + ("выключен" if cur else "включён"))

    @router.callback_query(F.data == "slack:defprompt:reset")
    async def on_defprompt_reset(query: CallbackQuery) -> None:
        addon_state.set_(_ADDON, "default_prompt", DEFAULT_PROMPT)
        addon_state.set_(_ADDON, "default_prompt_enabled", True)
        with contextlib.suppress(Exception):
            await query.message.edit_text(_defprompt_text(), reply_markup=_kb_defprompt(),
                                          disable_web_page_preview=True)
        await query.answer("Сброшено к стандартному")

    @router.callback_query(F.data == "slack:run")
    async def on_run(query: CallbackQuery) -> None:
        if query.message is None:
            await query.answer()
            return
        await query.answer()
        if not _default_prompt_enabled():
            await query.message.answer("Дефолтный промт выключен. Включи его или используй «📝 Вставить промт».",
                                       reply_markup=_kb_back_home())
            return
        intent = parsers.parse_prompt(_default_prompt_text())
        if intent.is_empty:
            intent.want_table = True
            intent.want_download = True
        await _run_intent(query.message, intent)

    # ---- free-form text input (token / prompt / defprompt) ----

    async def _is_awaiting(message: Message) -> bool:
        return bool(_awaiting(message.chat.id))

    @router.message(StateFilter(None), F.text & ~F.text.startswith("/"), _is_awaiting)
    async def on_text(message: Message) -> None:
        chat_id = message.chat.id
        mode = _awaiting(chat_id)
        value = (message.text or "").strip()

        if mode == "token":
            _stop_awaiting(chat_id)
            # Delete the secret-bearing message immediately.
            with contextlib.suppress(Exception):
                await message.delete()
            if not looks_like_slack_token(value):
                await message.answer(
                    "Это не похоже на Slack-токen (ожидается xoxb-… или xoxp-…). "
                    "Попробуй ещё раз через «🔌 Подключить Slack».",
                    reply_markup=_kb_back_home(),
                )
                return
            # Verify via auth.test before saving.
            from .client import SlackAuthError, SlackClient
            loop = asyncio.get_event_loop()
            try:
                client = await loop.run_in_executor(None, lambda: SlackClient(token=value))
                info = await loop.run_in_executor(None, client.auth_test)
            except SlackAuthError as e:
                await message.answer(f"⚠ {e}", reply_markup=_kb_back_home())
                return
            except Exception as e:  # noqa: BLE001
                await message.answer(f"Не удалось проверить токен: {e}", reply_markup=_kb_back_home())
                return
            save_token(value)
            team = info.get("team", "?")
            user = info.get("user", "?")
            await message.answer(
                f"✅ Slack подключён.\nTeam: <b>{team}</b>, бот/пользователь: <b>{user}</b>.\n"
                "Теперь выбери канал: «📡 Выбрать канал/DM».",
                reply_markup=_kb_main(),
            )
            return

        if mode == "defprompt":
            _stop_awaiting(chat_id)
            if value in {"-", "—", "убрать", "пусто"}:
                addon_state.set_(_ADDON, "default_prompt", "")
                addon_state.set_(_ADDON, "default_prompt_enabled", False)
                await message.answer("Дефолтный промт убран и выключен.", reply_markup=_kb_defprompt())
                return
            addon_state.set_(_ADDON, "default_prompt", value)
            addon_state.set_(_ADDON, "default_prompt_enabled", True)
            await message.answer("Сохранён новый промт по умолчанию.", reply_markup=_kb_defprompt())
            return

        if mode == "prompt":
            _stop_awaiting(chat_id)
            if not value:
                if _default_prompt_enabled() and _default_prompt_text():
                    value = _default_prompt_text()
                    await message.answer(f"Пусто — применяю дефолт: <code>{value}</code>")
                else:
                    await message.answer(
                        "Не понял задачу. Напиши, например: «вынуть таблицей и скачать все zip».",
                        reply_markup=_kb_back_home(),
                    )
                    return
            intent = parsers.parse_prompt(value)
            if intent.is_empty:
                await message.answer(
                    "Не распознал, что сделать. Доступно: выгрузка таблицей и/или "
                    "скачивание вложений (в т.ч. только zip). "
                    f"Можешь применить дефолт: <code>{_default_prompt_text()}</code>",
                    reply_markup=_kb_back_home(),
                )
                return
            await _run_intent(message, intent)
            return

        # Unknown mode — clear it.
        _stop_awaiting(chat_id)

    return router
