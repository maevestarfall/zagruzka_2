# Master Room handlers: devin-style console with GitHub deploy via batched zip upload.
# Co-authored with CoCo
"""«🛠 Master Room» — devin-style console inside Lilush (v0.3, P1).

UI layout (main screen, after «🛠 Master Room» in the wizard root):

    📊 Статус            🌐 Кодер-серверы
    🚀 Новая задача      📂 Проекты
    🐙 GitHub-аккаунты   🎯 Таргеты
    ⚙️ Настройки         ❓ Помощь
    « Назад

FSM «🚀 Новая задача» — 5 steps:

    1. awaiting_title       — short title for the project card
    2. awaiting_prompt      — full natural-language brief
    3. awaiting_stack       — pick one of 7 STACK_CHOICES
    4. awaiting_target      — pick render / oracle / custom
    5. awaiting_budget      — pick 100K / 200K / 500K / 1M
       → confirming         — review screen → POST /coder/task

All screens are owner-only (same `_is_owner` import used by Analyzer
PRO / Debates). Tokens for coder servers and GitHub accounts are
collected via FSM text inputs; we never echo them back in messages.

For P1 the coder-bot itself doesn't exist yet — when the FSM hits
its terminal step we POST through :class:`MasterRoomCoderClient` to
the active server; if no server is configured (the common P1 case)
we surface a friendly «🔧 Подключи кодер-сервер в '🌐 Кодер-серверы'»
notice instead of crashing.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import state as mr_state
from .client import CoderClientError, MasterRoomCoderClient

logger = logging.getLogger(__name__)


# ─── FSM state group ──────────────────────────────────────────────────────────


class MasterRoomStates(StatesGroup):
    awaiting_server_name = State()
    awaiting_server_url = State()
    awaiting_server_token = State()

    awaiting_github_name = State()
    awaiting_github_owner = State()
    awaiting_github_token_ref = State()

    # «✏️ Редактировать все репозитории» — waiting for the zip to walk slots.
    awaiting_editall_zip = State()

    # --- «Вход в GitHub» (login + password) flow ---
    awaiting_gh_web_login = State()
    awaiting_gh_web_password = State()

    awaiting_target_url = State()    # for editing render/oracle URL templates
    awaiting_custom_target_name = State()
    awaiting_custom_target_url = State()

    awaiting_title = State()
    awaiting_prompt = State()
    awaiting_stack = State()
    awaiting_target = State()
    awaiting_budget = State()
    confirming = State()

    # --- Deploy zip to GitHub flow ---
    awaiting_deploy_token = State()
    awaiting_deploy_repo_name = State()
    awaiting_deploy_zip = State()

    # --- Storage (file manager) ---
    awaiting_storage_upload = State()

    # --- Render.com browser-deploy ---
    awaiting_render_bot_token = State()
    # --- Render.com «Способ входа» (login method) ---
    awaiting_render_gh_login = State()
    awaiting_render_gh_password = State()
    awaiting_render_email = State()
    awaiting_render_password = State()
    awaiting_render_cookie_upload = State()


# ─── Owner gate ───────────────────────────────────────────────────────────────


async def _is_owner_query(query: CallbackQuery) -> bool:
    from ...wizard import _is_owner  # local import to avoid cycle
    return _is_owner(query.from_user.id if query.from_user else 0)


async def _is_owner_msg(message: Message) -> bool:
    from ...wizard import _is_owner
    return _is_owner(message.from_user.id if message.from_user else 0)


async def _deny(query: CallbackQuery) -> None:
    await query.answer("Только владелец.", show_alert=True)


def _html_escape(s: Any) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def _send_or_edit(
    query: CallbackQuery, body: str, kb: InlineKeyboardMarkup | None = None
) -> None:
    """Edit the existing message when possible, otherwise post a new one.

    Telegram throws if you try to edit a message you don't own (e.g.
    the user typed a free-form reply after a keyboard prompt). The
    fallback keeps the FSM moving regardless.
    """
    try:
        await query.message.edit_text(
            body, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception:  # noqa: BLE001
        try:
            await query.message.answer(
                body, reply_markup=kb, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("masterroom: failed to send screen")


# ─── Main screen ──────────────────────────────────────────────────────────────


HELP_TEXT = (
    "<b>🛠 Master Room — что это</b>\n\n"
    "Devin-style консоль для кодинга, тестов и деплоя прямо из Lilush.\n\n"
    "<b>Как это работает:</b>\n"
    "1. Подключи <b>🌐 кодер-сервер</b> — это отдельный сервис-обёртка\n"
    "   над Claude API (URL + Bearer токен).\n"
    "2. Добавь <b>🐙 GitHub-аккаунт</b> — owner + ссылка на env-var\n"
    "   с токеном. Сам токен в state.json не пишется.\n"
    "3. Настрой <b>🎯 таргеты</b> (Render / Oracle / Другое) — URL-шаблон\n"
    "   куда auto-deploy раскатит готовый проект.\n"
    "4. <b>🚀 Новая задача</b> — пройди FSM из 5 шагов, кодер-бот\n"
    "   получит prompt + stack + target + budget и начнёт работу.\n"
    "5. Прогресс прилетает через webhook <code>/masterroom/&lt;token&gt;/event</code>,\n"
    "   финал — через <code>/done</code>.\n\n"
    "<b>Что НЕ нужно:</b>\n"
    "• Render API key — деплой = git push, Render слушает репо\n"
    "  через Blueprint (<code>render.yaml</code>).\n"
    "• Oracle SSH key — Oracle тянет через GitHub Actions + cloud-init.\n"
    "• Anthropic API key — он живёт у кодер-бота, не у Lilush.\n"
)


async def _deploy_zip_to_github(status_msg, gh, repo_name: str, mode: str, buf) -> None:
    """Shared deploy core: unzip a buffer, (create repo), push batched.

    Used both by the FSM zip step and the «zip dropped in chat» auto flow.
    Edits ``status_msg`` in place with a percentage progress bar.
    """
    import zipfile
    from .github import GitHubError, create_repo, get_repo, push_files_batched
    try:
        zf = zipfile.ZipFile(buf)
        entries = [n for n in zf.namelist()
                   if not n.endswith("/") and "__pycache__" not in n and ".DS_Store" not in n]
    except Exception as e:
        await status_msg.edit_text(f"❌ Zip повреждён: {e}")
        return
    if not entries:
        await status_msg.edit_text("❌ Архив пустой.")
        return
    top_dirs = {e.split("/")[0] for e in entries}
    single_root = (top_dirs.pop() + "/") if len(top_dirs) == 1 else ""
    files_to_push = []
    for entry in entries:
        rel = entry[len(single_root):] if single_root and entry.startswith(single_root) else entry
        if not rel:
            continue
        try:
            files_to_push.append((rel, zf.read(entry)))
        except Exception:
            continue
    total = len(files_to_push)
    await status_msg.edit_text(f"📦 <b>{total}</b> файлов распаковано.", parse_mode="HTML")

    default_branch = "main"
    if mode == "new":
        vis = "private" if mr_state.get_gh_auto_private() else "public"
        auto_readme = mr_state.get_gh_auto_readme()
        try:
            repo_obj = await create_repo(gh, repo_name, visibility=vis,
                                         auto_init=auto_readme,
                                         description=f"Deployed ({total} files)")
            default_branch = repo_obj.get("default_branch", "main") or "main"
        except GitHubError as e:
            if e.status == 422:
                try:
                    existing = await get_repo(gh, repo_name)
                    default_branch = existing.get("default_branch", "main") or "main"
                except Exception:
                    pass
            else:
                await status_msg.edit_text(f"❌ Не создать репо: {_html_escape(str(e))}")
                return
    else:
        try:
            existing = await get_repo(gh, repo_name)
            default_branch = existing.get("default_branch", "main") or "main"
        except GitHubError as e:
            await status_msg.edit_text(f"❌ Репо не найден: {_html_escape(str(e))}")
            return

    batch_size = 95

    def _bar(pct: int) -> str:
        filled = pct // 10
        return "█" * filled + "░" * (10 - filled)

    async def on_batch(num: int, total_b: int, sha: str) -> None:
        done = min(num * batch_size, total)
        pct = int(done * 100 / total) if total else 100
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                f"⬆️ <b>Загрузка на GitHub…</b>\n\n"
                f"{_bar(pct)} <b>{pct}%</b>\n"
                f"батч {num}/{total_b} · {done}/{total} файлов",
                parse_mode="HTML")

    try:
        result = await push_files_batched(gh, repo_name, files_to_push,
                                         branch=default_branch,
                                         batch_size=batch_size,
                                         on_batch_done=on_batch)
    except GitHubError as e:
        await status_msg.edit_text(f"❌ Ошибка загрузки: {_html_escape(str(e))}")
        return
    repo_url = f"https://github.com/{gh['owner']}/{repo_name}"
    owner = gh.get("owner", "")
    # Remember this repo so the «🖥 Render на сервер» button knows what to deploy.
    mr_state.set_render_pending(repo_name, owner)
    # Remember it as this slot's last-uploaded repo — the default target for
    # «Редактировать все репозитории» auto-editing on this slot.
    with contextlib.suppress(Exception):
        if gh.get("id"):
            mr_state.set_last_uploaded_repo(gh["id"], repo_name)
    auto_render = mr_state.get_render_auto()
    render_note = (
        "\n\n🖥 <i>Авто-загрузка на render.com включена — запускаю…</i>"
        if auto_render else ""
    )
    await status_msg.edit_text(
        f"✅ <b>Готово!</b>\n\n"
        f"📦 {result['total_files']} файлов · 📝 {result['commits']} коммитов\n"
        f"🔗 <a href=\"{repo_url}\">{gh['owner']}/{repo_name}</a>"
        f"{render_note}",
        parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🖥 Render на сервер", callback_data="mr:render:deploy")],
            [InlineKeyboardButton(text="⚙️ Управление GitHub", callback_data="mr:gh:manage")],
        ]),
    )
    # Auto-deploy to render.com right after a successful GitHub push.
    if auto_render:
        await _run_render_deploy(status_msg, repo_name, owner)


async def _resolve_slot_repo(gh: dict) -> str:
    """Pick the repository this slot should auto-edit.

    Order: explicit «репозиторий для авто-редактирования» → last uploaded on
    this slot → GitHub lookup (exactly one repo → it; two or more → the most
    recently pushed). Returns "" if the slot has no repo to edit.
    """
    aid = gh.get("id", "")
    explicit = mr_state.get_auto_edit_repo(aid) if aid else ""
    if explicit:
        return explicit
    last = mr_state.get_last_uploaded_repo(aid) if aid else ""
    if last:
        return last
    try:
        from .github import list_repos
        repos = await list_repos(gh)
    except Exception:  # noqa: BLE001
        return ""
    if not repos:
        return ""
    # list_repos is sorted pushed-desc, so [0] is the most recent (== last
    # uploaded when there are two or more).
    return str(repos[0].get("name", "") or "")


async def _run_editall_walk(reply, bot, file_id: str) -> None:
    """Edit every filled GitHub slot's auto-edit repo with one dropped zip.

    Walks slots in order: switch to slot's token → edit its auto-edit repo →
    switch to the next. ``reply`` is any object with ``.answer(...)`` (a
    Message). Shared by the chat-zip flow and the «Управление GitHub» flow.
    """
    from .github import _resolve_token
    slots = [a for a in mr_state.get_github_accounts() if _resolve_token(a)]
    if not slots:
        await reply.answer("Нет заполненных слотов GitHub.")
        return
    import io
    try:
        file = await bot.get_file(file_id)
        raw_buf = io.BytesIO()
        await bot.download_file(file.file_path, raw_buf)
        archive_bytes = raw_buf.getvalue()
    except Exception as e:  # noqa: BLE001
        await reply.answer(f"❌ Не скачать архив: {_html_escape(str(e))}")
        return
    done, failed = 0, 0
    for i, gh in enumerate(slots, start=1):
        owner = gh.get("owner", "?")
        await reply.answer(
            f"🔌 Слот {i}/{len(slots)}: <b>{_html_escape(owner)}</b> — подключаюсь…",
            parse_mode="HTML")
        repo = await _resolve_slot_repo(gh)
        if not repo:
            failed += 1
            await reply.answer(
                f"⚠️ Слот {i} ({_html_escape(owner)}): нет репозитория для "
                "авто-редактирования — пропускаю.")
            continue
        status_msg = await reply.answer(
            f"⏳ Слот {i}: редактирую <code>{_html_escape(owner)}/{_html_escape(repo)}</code>…",
            parse_mode="HTML")
        try:
            await _deploy_zip_to_github(status_msg, gh, repo, "existing",
                                        io.BytesIO(archive_bytes))
            done += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            with contextlib.suppress(Exception):
                await status_msg.edit_text(
                    f"❌ Слот {i} ({_html_escape(owner)}): {_html_escape(str(e))}")
    await reply.answer(
        f"🏁 <b>Готово по всем слотам.</b>\n"
        f"✅ обновлено: {done} · ⚠️ пропущено/ошибки: {failed} · всего слотов: {len(slots)}",
        parse_mode="HTML")


def _render_login_label(m: str) -> str:
    """Human label for a render.com login method id."""
    return {
        "github": "GitHub OAuth",
        "email": "Email + пароль",
        "cookie": "Импорт cookie",
    }.get(m, "GitHub OAuth")


async def _run_render_deploy(msg, repo_name: str, owner: str) -> None:
    """Run the render.com browser deploy and report progress on ``msg``'s chat.

    Shared by the auto-deploy path (after a GitHub push) and the manual
    «🖥 Render на сервер» / «🚀 Загрузить сейчас» buttons.
    """
    bot_token = mr_state.get_render_bot_token()
    old_project = mr_state.get_render_last_project()
    delete_prev = mr_state.get_render_delete_prev()
    pause_prev = mr_state.get_render_pause_prev()
    login_method = mr_state.get_render_login_method()
    gh_login = mr_state.get_render_gh_login()
    gh_password = mr_state.get_render_gh_password()
    render_email = mr_state.get_render_email()
    render_password = mr_state.get_render_password()
    if not bot_token:
        with contextlib.suppress(Exception):
            await msg.answer(
                "⚠️ Не задан <b>токен бота</b> для render.com.\n"
                "Открой 🖥 render.com → 🤖 Токен бота и впиши значение.",
                parse_mode="HTML")
        return

    if old_project and old_project != repo_name and delete_prev:
        prev_line = f"🗑 Сначала удаляю старый сервис <code>{_html_escape(old_project)}</code>.\n"
    elif old_project and old_project != repo_name and pause_prev:
        prev_line = f"💤 Сначала гашу старый сервис <code>{_html_escape(old_project)}</code>.\n"
    else:
        prev_line = ""
    prog = await msg.answer(
        f"🖥 <b>render.com</b> (браузер · стелс): запускаю stealth-браузер…\n"
        + prev_line
        + f"🚀 Затем поднимаю <code>{_html_escape(repo_name)}</code>.",
        parse_mode="HTML")

    from .render_deploy import deploy_to_render

    import time as _time
    _last = {"t": 0.0}

    def _bar(pct: int) -> str:
        filled = max(0, min(10, pct // 10))
        return "█" * filled + "░" * (10 - filled)

    async def _progress(done: int, total: int, op: str, label: str) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        now = _time.time()
        # Throttle to ~1 edit / 1.6s (Telegram rate limits); always show 100%.
        if now - _last["t"] < 1.6 and pct < 100:
            return
        _last["t"] = now
        with contextlib.suppress(Exception):
            await prog.edit_text(
                f"🖥 <b>render.com</b> (браузер · стелс)\n"
                f"{label}…\n"
                f"{_bar(pct)} <b>{pct}%</b> · шаг {done}/{total}",
                parse_mode="HTML")

    try:
        report = await deploy_to_render(
            repo_name, owner,
            bot_token=bot_token, old_project=old_project,
            delete_prev=delete_prev, pause_prev=pause_prev,
            login_method=login_method, gh_login=gh_login, gh_password=gh_password,
            email=render_email, password=render_password,
            progress=_progress)
    except Exception as e:  # noqa: BLE001
        logger.exception("render deploy failed")
        with contextlib.suppress(Exception):
            await prog.edit_text(f"❌ Render: {_html_escape(str(e))}", parse_mode="HTML")
        return

    # Remember the new project so the next deploy cleans it up.
    mr_state.set_render_last_project(repo_name)

    lines: list[str] = []
    if report.get("login") is not None:
        lg = report["login"]
        ok = not lg.get("error")
        login_label = _render_login_label(report.get("login_method") or login_method)
        lines.append(
            f"🔐 Вход ({_html_escape(login_label)}): "
            + ("✅" if ok else f"⚠️ {_html_escape(lg.get('error'))}"))
    if report.get("suspend") is not None:
        s = report["suspend"]
        ok = not s.get("error")
        act = "🗑 Удаление" if report.get("prev_action") == "delete" else "💤 Suspend"
        lines.append(
            f"{act} <code>{_html_escape(old_project)}</code>: "
            + ("✅" if ok else f"⚠️ {_html_escape(s.get('error'))}"))
    c = report.get("create") or {}
    eng = c.get("browser_type") or "—"
    if report.get("ok") and report.get("verified"):
        lines.append(f"🚀 Новый сервис <b>{_html_escape(repo_name)}</b> создан и отправлен на деплой (подтверждено).")
    elif report.get("ok"):
        lines.append(
            f"🟡 Новый сервис <b>{_html_escape(repo_name)}</b>: шаги выполнены, но "
            "подтвердить результат со страницы не вышло. Проверь сервис на render и скриншот.")
    else:
        lines.append(f"⚠️ Создание сервиса: {_html_escape(report.get('error') or 'не удалось')}")
    lines.append(f"Движок: {_html_escape(eng)}")

    with contextlib.suppress(Exception):
        await prog.edit_text("🖥 <b>render.com</b>\n" + "\n".join(lines), parse_mode="HTML")

    # Send screenshots so the user can visually verify each phase.
    import os as _os

    from aiogram.types import FSInputFile
    for shot in report.get("screenshots", []):
        if shot and _os.path.exists(shot):
            with contextlib.suppress(Exception):
                await msg.answer_photo(FSInputFile(shot))


def _render_home() -> str:
    srv = mr_state.get_active_server()
    n_srv = len(mr_state.get_servers())
    gh = mr_state.get_active_github_account()
    n_gh = len(mr_state.get_github_accounts())
    tgt = mr_state.get_active_target()
    n_proj = len(mr_state.get_projects())
    lines = ["<b>🛠 Master Room</b>", ""]
    lines.append(
        f"🌐 Кодер: <b>{_html_escape(srv['name']) if srv else 'не задан'}</b>"
        f" ({n_srv} всего)"
    )
    lines.append(
        f"🐙 GitHub: <b>{_html_escape(gh['name']) if gh else 'не задан'}</b>"
        f" ({n_gh} всего)"
    )
    lines.append(
        f"🎯 Таргет: <b>{_html_escape(tgt['name']) if tgt else 'не задан'}</b>"
    )
    lines.append(f"📂 Проекты: <b>{n_proj}</b>")
    lines.append("")
    if not srv:
        lines.append("<i>Подключи кодер-сервер, чтобы запускать задачи.</i>")
    return "\n".join(lines)


def _kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статус", callback_data="mr:status"),
            InlineKeyboardButton(text="🌐 Кодер-серверы", callback_data="mr:servers"),
        ],
        [
            InlineKeyboardButton(text="🚀 Новая задача", callback_data="mr:new"),
            InlineKeyboardButton(text="📂 Проекты", callback_data="mr:projects"),
        ],
        [
            InlineKeyboardButton(text="🐙 GitHub Проекты", callback_data="mr:github"),
            InlineKeyboardButton(text="🎯 Таргеты", callback_data="mr:targets"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="mr:settings"),
            InlineKeyboardButton(text="💾 Хранилище", callback_data="mr:storage"),
        ],
        [
            InlineKeyboardButton(text="🔐 Персональные данные и синхронизация",
                                 callback_data="mr:pdsync"),
        ],
        [
            InlineKeyboardButton(text="🖥 render.com", callback_data="mr:render"),
        ],
        [
            InlineKeyboardButton(text="❓ Помощь", callback_data="mr:help"),
        ],
        [
            InlineKeyboardButton(text="« Назад", callback_data="mr:back"),
        ],
    ])


async def show_screen(message_or_query: Any) -> None:
    """Render the Master Room home screen.

    Accepts either a :class:`CallbackQuery` (edit in place) or a
    :class:`Message` (fresh post) so we can drive it from both the
    main-menu callback and a future ``/masterroom`` command.
    """
    body = _render_home()
    kb = _kb_home()
    if isinstance(message_or_query, CallbackQuery):
        await _send_or_edit(message_or_query, body, kb)
    else:
        await message_or_query.answer(
            body, reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )


# ─── Status screen ────────────────────────────────────────────────────────────


async def _show_status(query: CallbackQuery) -> None:
    srv = mr_state.get_active_server()
    gh = mr_state.get_active_github_account()
    tgt = mr_state.get_active_target()
    defaults = mr_state.get_defaults()
    link = mr_state.get_link()
    lines = ["<b>📊 Статус Master Room</b>", ""]
    lines.append(f"🔗 Webhook URL: {'<code>' + _html_escape(link) + '</code>' if link else '<i>ссылка не сгенерирована</i>'}")
    lines.append(f"🟢 Включён: <b>{'да' if mr_state.is_enabled() else 'нет'}</b>")
    lines.append("")
    lines.append(
        f"🌐 Кодер: <b>{_html_escape(srv['name']) + ' — ' + _html_escape(srv['url']) if srv else 'не задан'}</b>"
    )
    lines.append(
        f"🐙 GitHub: <b>{_html_escape(gh['name']) + ' (' + _html_escape(gh['owner']) + ')' if gh else 'не задан'}</b>"
    )
    lines.append(
        f"🎯 Таргет: <b>{_html_escape(tgt['name']) if tgt else 'не задан'}</b>"
        + (f"\n   URL: <code>{_html_escape(tgt['url_template'])}</code>" if tgt and tgt.get("url_template") else "")
    )
    lines.append("")
    lines.append(
        f"⚙️ Дефолты: stack=<code>{_html_escape(defaults['stack'])}</code>, "
        f"budget=<code>{defaults['budget']:,}</code>, "
        f"target=<code>{_html_escape(defaults['target_id'])}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Перевыпустить ссылку", callback_data="mr:link:gen"),
         InlineKeyboardButton(text="🛑 Выключить", callback_data="mr:link:off")],
        [InlineKeyboardButton(text="« Назад", callback_data="mr:menu")],
    ])
    await _send_or_edit(query, "\n".join(lines), kb)


# ─── Coder servers screen ─────────────────────────────────────────────────────


def _kb_servers() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    active_id = mr_state.get_active_server_id()
    for s in mr_state.get_servers():
        marker = "🟢" if s["id"] == active_id else "⚪"
        rows.append([
            InlineKeyboardButton(text=f"{marker} {s['name']}",
                                 callback_data=f"mr:srv:select:{s['id']}"),
            InlineKeyboardButton(text="🗑",
                                 callback_data=f"mr:srv:rm:{s['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить сервер", callback_data="mr:srv:add")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_servers() -> str:
    servers = mr_state.get_servers()
    if not servers:
        return ("<b>🌐 Кодер-серверы</b>\n\n"
                "Список пуст. Добавь первый через <b>➕ Добавить сервер</b>.\n\n"
                "<i>Это HTTP-обёртка над Claude API; контракт — см. "
                "<code>TZ_MASTER_ROOM_v0.3.md</code> §4.5.</i>")
    lines = ["<b>🌐 Кодер-серверы</b>", ""]
    active_id = mr_state.get_active_server_id()
    for s in servers:
        marker = "🟢" if s["id"] == active_id else "⚪"
        lines.append(f"{marker} <b>{_html_escape(s['name'])}</b>")
        lines.append(f"   🔗 <code>{_html_escape(s['url'])}</code>")
    return "\n".join(lines)


# ─── GitHub accounts screen ──────────────────────────────────────────────────


def _kb_github() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    active_id = mr_state.get_active_github_id()
    for a in mr_state.get_github_accounts():
        marker = "🟢" if a["id"] == active_id else "⚪"
        rows.append([
            InlineKeyboardButton(text=f"{marker} {a['name']} ({a['owner']})",
                                 callback_data=f"mr:gh:select:{a['id']}"),
            InlineKeyboardButton(text="🗑",
                                 callback_data=f"mr:gh:rm:{a['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="mr:gh:add")])
    rows.append([InlineKeyboardButton(text="⚙️ Управление GitHub", callback_data="mr:gh:manage")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_github() -> str:
    accounts = mr_state.get_github_accounts()
    if not accounts:
        return ("<b>🐙 GitHub-аккаунты</b>\n\n"
                "Список пуст. Подключи аккаунт, под которым кодер будет\n"
                "создавать репозитории и пушить код.")
    lines = ["<b>🐙 GitHub-аккаунты</b>", ""]
    active_id = mr_state.get_active_github_id()
    for a in accounts:
        marker = "🟢" if a["id"] == active_id else "⚪"
        lines.append(f"{marker} <b>{_html_escape(a['name'])}</b>")
        lines.append(f"   owner: <code>{_html_escape(a['owner'])}</code>")
        lines.append(f"   token env: <code>{_html_escape(a['token_ref'])}</code>"
                     f" · visibility: {_html_escape(a['default_visibility'])}")
    return "\n".join(lines)


# ─── Targets screen ──────────────────────────────────────────────────────────


def _kb_targets() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    active = mr_state.get_active_target_id()
    for tid in ("render", "oracle"):
        tgt = mr_state.get_target(tid) or {}
        marker = "🟢" if active == tid else "⚪"
        url_short = tgt.get("url_template", "") or "не задан"
        if len(url_short) > 30:
            url_short = url_short[:27] + "…"
        rows.append([
            InlineKeyboardButton(text=f"{marker} {tgt.get('name', tid)} — {url_short}",
                                 callback_data=f"mr:tgt:select:{tid}"),
            InlineKeyboardButton(text="✏️",
                                 callback_data=f"mr:tgt:edit:{tid}"),
        ])
    for c in mr_state.list_custom_targets():
        marker = "🟢" if active == c["id"] else "⚪"
        url_short = c.get("url_template", "") or "—"
        if len(url_short) > 30:
            url_short = url_short[:27] + "…"
        raw_id = c["id"].split(":", 1)[1]
        rows.append([
            InlineKeyboardButton(text=f"{marker} {c['name']} — {url_short}",
                                 callback_data=f"mr:tgt:select:{c['id']}"),
            InlineKeyboardButton(text="🗑",
                                 callback_data=f"mr:tgt:rm:{raw_id}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Custom target", callback_data="mr:tgt:add")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_targets() -> str:
    return ("<b>🎯 Таргеты деплоя</b>\n\n"
            "URL-шаблон — куда кодер залит готовый проект.\n"
            "Render и Oracle подключены через GitHub Actions /\n"
            "<code>render.yaml</code> Blueprint — Lilush сам не дёргает API.\n")


# ─── Projects screen ─────────────────────────────────────────────────────────


_STATUS_EMOJI = {
    "queued": "⏳", "coding": "🛠", "testing": "🧪",
    "committed": "📦", "deploying": "🚀", "done": "✅",
    "failed": "❌", "cancelled": "🚫",
}


def _kb_projects() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in mr_state.get_projects()[:10]:
        emoji = _STATUS_EMOJI.get(p.get("status", ""), "•")
        title = str(p.get("title", "(без названия)"))[:30]
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {title}",
            callback_data=f"mr:proj:detail:{p['id']}",
        )])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_projects() -> str:
    projects = mr_state.get_projects()
    if not projects:
        return ("<b>📂 Проекты</b>\n\n"
                "Пусто. Запусти первую задачу через <b>🚀 Новая задача</b>.")
    lines = [f"<b>📂 Проекты ({len(projects)})</b>", ""]
    for p in projects[:10]:
        emoji = _STATUS_EMOJI.get(p.get("status", ""), "•")
        lines.append(
            f"{emoji} <b>{_html_escape(p.get('title', '(без названия)'))}</b>"
            f" · <code>{_html_escape(p.get('stack', ''))}</code>"
        )
        if p.get("preview_url"):
            lines.append(f"   🔗 <code>{_html_escape(p['preview_url'])}</code>")
    return "\n".join(lines)


def _render_project_detail(p: dict[str, Any]) -> str:
    lines = [f"<b>{_STATUS_EMOJI.get(p.get('status', ''), '•')} {_html_escape(p.get('title', ''))}</b>", ""]
    lines.append(f"task_id: <code>{_html_escape(p.get('task_id', ''))}</code>")
    lines.append(f"stack: <code>{_html_escape(p.get('stack', ''))}</code>")
    lines.append(f"target: <code>{_html_escape(p.get('target_id', ''))}</code>")
    lines.append(f"budget: <code>{int(p.get('budget', 0)):,}</code>")
    lines.append(f"github: <code>{_html_escape(p.get('github_repo', '—'))}</code>")
    lines.append(f"status: <code>{_html_escape(p.get('status', ''))}</code>")
    if p.get("preview_url"):
        lines.append(f"preview: <code>{_html_escape(p['preview_url'])}</code>")
    if p.get("deploy_url"):
        lines.append(f"deploy: <code>{_html_escape(p['deploy_url'])}</code>")
    if p.get("error"):
        lines.append(f"\n<b>❌ ошибка:</b>\n<code>{_html_escape(p['error'])}</code>")
    log = p.get("log") or []
    if log:
        lines.append("\n<b>📋 последние записи лога:</b>")
        for entry in log[-5:]:
            lines.append(f"• <code>{_html_escape(entry.get('line', ''))}</code>")
    return "\n".join(lines)


def _kb_project_detail(p: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if p.get("preview_url"):
        rows.append([InlineKeyboardButton(text="🔗 Открыть превью", url=p["preview_url"])])
    if p.get("status") in {"queued", "coding", "testing", "deploying"}:
        rows.append([InlineKeyboardButton(text="🛑 Отменить",
                                          callback_data=f"mr:proj:cancel:{p['id']}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить запись",
                                      callback_data=f"mr:proj:rm:{p['id']}")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:projects")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── Settings ────────────────────────────────────────────────────────────────


def _kb_settings() -> InlineKeyboardMarkup:
    d = mr_state.get_defaults()
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(
        text=f"📚 stack: {mr_state.STACK_LABEL.get(d['stack'], d['stack'])}",
        callback_data="mr:settings:stack")])
    rows.append([InlineKeyboardButton(
        text=f"💰 budget: {d['budget']:,}",
        callback_data="mr:settings:budget")])
    zip_on = mr_state.get_zip_export_enabled()
    rows.append([InlineKeyboardButton(
        text=f"⚙️ Выгрузка ZIP: [ {'ВКЛ 🟢' if zip_on else 'ВЫКЛ'} ]",
        callback_data="mr:zip:toggle")])
    rows.append([InlineKeyboardButton(
        text="📥 Скачать ZIP проекта",
        callback_data="mr:zip:download")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_stack_picker(prefix: str) -> InlineKeyboardMarkup:
    """``prefix`` is either ``mr:settings:stack:set`` (settings) or
    ``mr:fsm:stack`` (FSM step). Same picker, different commit path.
    """
    rows = []
    for choice in mr_state.STACK_CHOICES:
        rows.append([InlineKeyboardButton(
            text=mr_state.STACK_LABEL[choice],
            callback_data=f"{prefix}:{choice}",
        )])
    rows.append([InlineKeyboardButton(text="« Назад",
                                      callback_data="mr:settings" if prefix.startswith("mr:settings") else "mr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_budget_picker(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for choice in mr_state.BUDGET_CHOICES:
        rows.append([InlineKeyboardButton(
            text=f"💰 {choice:,}",
            callback_data=f"{prefix}:{choice}",
        )])
    rows.append([InlineKeyboardButton(text="« Назад",
                                      callback_data="mr:settings" if prefix.startswith("mr:settings") else "mr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── Router ──────────────────────────────────────────────────────────────────


def build_masterroom_router() -> Router:  # noqa: C901 — UI router stays flat on purpose
    router = Router(name="masterroom")

    # 🔐 Персональные данные и синхронизация (ЧАСТЬ 18.2) — отдельный
    # модуль со СТРОГИМ owner-гейтом (bot.access.is_owner, НЕ can_admin).
    from .sync.handlers import build_pdsync_router
    router.include_router(build_pdsync_router())

    # --- Entry / nav --------------------------------------------------

    @router.callback_query(F.data == "main:masterroom")
    async def _from_main(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        await show_screen(query)

    @router.callback_query(F.data == "mr:menu")
    async def _menu(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        await show_screen(query)

    @router.callback_query(F.data == "mr:back")
    async def _back(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        # Hand back to the wizard main menu (same kb the main menu uses).
        uid = query.from_user.id if query.from_user else 0
        try:
            from ...wizard import _kb_main_after_claim
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await query.message.edit_text(
                        "Главное меню. Выбери что сделать:",
                        reply_markup=_kb_main_after_claim(uid),
                    )
        except Exception:  # noqa: BLE001
            if query.message is not None:
                await query.message.answer("Главное меню — нажми /start")

    @router.callback_query(F.data == "mr:status")
    async def _status(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        await _show_status(query)

    @router.callback_query(F.data == "mr:help")
    async def _help(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="mr:menu")]
        ])
        await _send_or_edit(query, HELP_TEXT, kb)

    # --- Link generation ---------------------------------------------

    @router.callback_query(F.data == "mr:link:gen")
    async def _link_gen(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        token, link = mr_state.generate_link()
        await query.answer("🔗 Новая ссылка готова")
        msg = (
            "<b>🔗 Webhook URL обновлён</b>\n\n"
            f"<code>{_html_escape(link or '/masterroom/' + token + '/')}</code>\n\n"
            "Эту ссылку забивай в <code>callback_url</code> кодер-боту."
        )
        await _send_or_edit(query, msg, InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="mr:status")]
        ]))

    @router.callback_query(F.data == "mr:link:off")
    async def _link_off(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        was = mr_state.disable_link()
        await query.answer("🛑 Выключено" if was else "Уже было выключено")
        await _show_status(query)

    # --- Coder servers -----------------------------------------------

    @router.callback_query(F.data == "mr:servers")
    async def _servers(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        await _send_or_edit(query, _render_servers(), _kb_servers())

    @router.callback_query(F.data.startswith("mr:srv:select:"))
    async def _srv_select(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        sid = query.data.split(":", 3)[3]
        mr_state.set_active_server_id(sid)
        await query.answer("🟢 Активный кодер обновлён")
        await _send_or_edit(query, _render_servers(), _kb_servers())

    @router.callback_query(F.data.startswith("mr:srv:rm:"))
    async def _srv_rm(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        sid = query.data.split(":", 3)[3]
        mr_state.remove_server(sid)
        await query.answer("🗑 Удалено")
        await _send_or_edit(query, _render_servers(), _kb_servers())

    @router.callback_query(F.data == "mr:srv:add")
    async def _srv_add(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_server_name)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>➕ Новый кодер-сервер — шаг 1/3</b>\n\nПришли <b>название</b> сервера (например <code>claude-prod</code>).",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:servers")]
            ]),
        )

    @router.message(MasterRoomStates.awaiting_server_name)
    async def _srv_add_name(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        name = (message.text or "").strip()
        if not name:
            await message.answer("Название не может быть пустым. Пришли ещё раз.")
            return
        await state.update_data(srv_name=name)
        await state.set_state(MasterRoomStates.awaiting_server_url)
        await message.answer(
            "<b>Шаг 2/3</b> — пришли <b>URL</b> сервера (без trailing slash, например <code>https://coder.example.com</code>).",
            parse_mode="HTML",
        )

    @router.message(MasterRoomStates.awaiting_server_url)
    async def _srv_add_url(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        url = (message.text or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await message.answer("URL должен начинаться с http(s)://. Пришли ещё раз.")
            return
        await state.update_data(srv_url=url)
        await state.set_state(MasterRoomStates.awaiting_server_token)
        await message.answer(
            "<b>Шаг 3/3</b> — пришли <b>Bearer-токен</b> сервера. Хранится в state.json в открытом виде; "
            "если опасно — заведи env-var и подставь <code>$ENV_VAR</code>.",
            parse_mode="HTML",
        )

    @router.message(MasterRoomStates.awaiting_server_token)
    async def _srv_add_token(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        token = (message.text or "").strip()
        if not token:
            await message.answer("Токен пуст. Пришли ещё раз.")
            return
        data = await state.get_data()
        server = mr_state.add_server(data.get("srv_name", "coder"),
                                     data.get("srv_url", ""), token)
        await state.clear()
        # Try to delete the token message so it doesn't linger in the chat log.
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            pass
        await message.answer(
            f"✅ Сервер <b>{_html_escape(server['name'])}</b> добавлен и сделан активным.",
            parse_mode="HTML",
            reply_markup=_kb_servers(),
        )

    # --- GitHub accounts ---------------------------------------------

    @router.callback_query(F.data == "mr:github")
    async def _gh(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        await _send_or_edit(query, _render_github(), _kb_github())

    @router.callback_query(F.data.startswith("mr:gh:select:"))
    async def _gh_select(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        aid = query.data.split(":", 3)[3]
        mr_state.set_active_github_id(aid)
        await query.answer("🟢 Активный GitHub-аккаунт обновлён")
        await _send_or_edit(query, _render_github(), _kb_github())

    @router.callback_query(F.data.startswith("mr:gh:rm:"))
    async def _gh_rm(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        aid = query.data.split(":", 3)[3]
        mr_state.remove_github_account(aid)
        await query.answer("🗑 Удалено")
        await _send_or_edit(query, _render_github(), _kb_github())

    @router.callback_query(F.data == "mr:gh:add")
    async def _gh_add(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_github_name)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>➕ Новый GitHub-аккаунт — шаг 1/3</b>\n\nПришли <b>короткое имя</b> (для UI, например <code>main</code>).",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:github")]
            ]),
        )

    @router.message(MasterRoomStates.awaiting_github_name)
    async def _gh_add_name(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        name = (message.text or "").strip()
        if not name:
            await message.answer("Имя не может быть пустым.")
            return
        await state.update_data(gh_name=name)
        await state.set_state(MasterRoomStates.awaiting_github_owner)
        await message.answer(
            "<b>Шаг 2/3</b> — пришли <b>owner</b> (логин или org-name на GitHub).",
            parse_mode="HTML",
        )

    @router.message(MasterRoomStates.awaiting_github_owner)
    async def _gh_add_owner(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        owner = (message.text or "").strip()
        if not owner:
            await message.answer("Owner не может быть пустым.")
            return
        await state.update_data(gh_owner=owner)
        await state.set_state(MasterRoomStates.awaiting_github_token_ref)
        await message.answer(
            "<b>Шаг 3/3</b> — пришли <b>GitHub-токен</b>.\n\n"
            "Создай его тут: "
            "<a href=\"https://github.com/settings/tokens?type=beta\">github.com/settings/tokens</a>\n"
            "<i>Если хочешь дойти сам: github.com → твой аватар → Settings → "
            "Developer settings (самый низ левой колонки) → Personal access "
            "tokens → Fine-grained tokens → Generate new token.</i>\n\n"
            "<b>На странице Generate new token:</b>\n"
            "• <b>Token name</b> — любое (например <code>lilush-bot</code>)\n"
            "• <b>Expiration</b> — на твой выбор (90 дней / без срока)\n"
            "• <b>Resource owner</b> — твой аккаунт\n"
            "• <b>Repository access → All repositories</b> ⚠️ (обязательно — новый "
            "репо ещё не существует, чтобы выбрать его поштучно)\n"
            "• <b>Permissions → Repository permissions:</b>\n"
            "   – <b>Administration</b> → Read and write (создание/удаление репо)\n"
            "   – <b>Contents</b> → Read and write (заливка файлов и коммиты)\n"
            "   – <b>Metadata</b> → Read-only (ставится автоматически)\n"
            "• <b>Generate token</b> → скопируй <code>github_pat_...</code>\n\n"
            "Вставь токен (<code>github_pat_...</code> или <code>ghp_...</code>).\n"
            "Можно прислать <b>имя env-var</b> (например <code>GITHUB_TOKEN</code>) — тогда "
            "токен берётся из окружения и в state не пишется.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(MasterRoomStates.awaiting_github_token_ref)
    async def _gh_add_token_ref(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        raw = (message.text or "").strip()
        data = await state.get_data()
        if mr_state.github_slots_full():
            await state.clear()
            await message.answer(
                f"❌ Достигнут лимит {mr_state.MAX_GITHUB_SLOTS} слотов GitHub. "
                "Удали лишний вход в «⚙️ Управление GitHub».")
            return
        # If it looks like a real token, store it directly; otherwise treat
        # it as an env-var name (backward compatible).
        if raw.startswith("github_pat_") or raw.startswith("ghp_") or raw.startswith("gho_"):
            acc = mr_state.add_github_account(
                data.get("gh_name", ""), data.get("gh_owner", ""),
                token_ref="GITHUB_TOKEN", token=raw,
            )
            stored_note = "🔑 токен сохранён в боте"
            # Delete the message with the token so it doesn't linger in chat
            with contextlib.suppress(Exception):
                await message.delete()
        else:
            token_ref = raw or "GITHUB_TOKEN"
            acc = mr_state.add_github_account(
                data.get("gh_name", ""), data.get("gh_owner", ""),
                token_ref=token_ref,
            )
            stored_note = f"📦 читаю из env <code>{_html_escape(token_ref)}</code>"
        await state.clear()
        await message.answer(
            f"✅ GitHub-аккаунт <b>{_html_escape(acc['name'])}</b> "
            f"({_html_escape(acc['owner'])}) добавлен.\n{stored_note}",
            parse_mode="HTML",
            reply_markup=_kb_github(),
        )

    # --- Targets ------------------------------------------------------

    @router.callback_query(F.data == "mr:targets")
    async def _targets(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        await _send_or_edit(query, _render_targets(), _kb_targets())

    @router.callback_query(F.data.startswith("mr:tgt:select:"))
    async def _tgt_select(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        tid = query.data.split(":", 3)[3]
        mr_state.set_active_target_id(tid)
        await query.answer("🟢 Активный таргет обновлён")
        await _send_or_edit(query, _render_targets(), _kb_targets())

    @router.callback_query(F.data.startswith("mr:tgt:edit:"))
    async def _tgt_edit(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        tid = query.data.split(":", 3)[3]
        if tid not in {"render", "oracle"}:
            await query.answer("Custom targets редактируются через ➕ Add", show_alert=True)
            return
        await state.update_data(edit_target_id=tid)
        await state.set_state(MasterRoomStates.awaiting_target_url)
        await query.answer()
        await _send_or_edit(
            query,
            f"<b>✏️ URL-шаблон для {tid}</b>\n\nПришли URL (например <code>https://my-bot.onrender.com</code>).",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:targets")]
            ]),
        )

    @router.message(MasterRoomStates.awaiting_target_url)
    async def _tgt_url_set(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        url = (message.text or "").strip()
        data = await state.get_data()
        tid = data.get("edit_target_id", "render")
        mr_state.set_target_url(tid, url)
        await state.clear()
        await message.answer(
            f"✅ URL для <b>{tid}</b> обновлён.",
            parse_mode="HTML",
            reply_markup=_kb_targets(),
        )

    @router.callback_query(F.data.startswith("mr:tgt:rm:"))
    async def _tgt_rm(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        raw = query.data.split(":", 3)[3]
        mr_state.remove_custom_target(raw)
        await query.answer("🗑 Удалено")
        await _send_or_edit(query, _render_targets(), _kb_targets())

    @router.callback_query(F.data == "mr:tgt:add")
    async def _tgt_add(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_custom_target_name)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>➕ Custom target — шаг 1/2</b>\n\nПришли <b>название</b> (например <code>vercel-staging</code>).",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:targets")]
            ]),
        )

    @router.message(MasterRoomStates.awaiting_custom_target_name)
    async def _tgt_add_name(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        name = (message.text or "").strip()
        if not name:
            await message.answer("Название не может быть пустым.")
            return
        await state.update_data(tgt_name=name)
        await state.set_state(MasterRoomStates.awaiting_custom_target_url)
        await message.answer(
            "<b>Шаг 2/2</b> — пришли <b>URL-шаблон</b> (например <code>https://staging-{slug}.vercel.app</code>).",
            parse_mode="HTML",
        )

    @router.message(MasterRoomStates.awaiting_custom_target_url)
    async def _tgt_add_url(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        url = (message.text or "").strip()
        if not url:
            await message.answer("URL не может быть пустым.")
            return
        data = await state.get_data()
        tgt = mr_state.add_custom_target(data.get("tgt_name", ""), url)
        await state.clear()
        await message.answer(
            f"✅ Custom target <b>{_html_escape(tgt['name'])}</b> добавлен.",
            parse_mode="HTML",
            reply_markup=_kb_targets(),
        )

    # --- Projects -----------------------------------------------------

    @router.callback_query(F.data == "mr:projects")
    async def _projects(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        await _send_or_edit(query, _render_projects(), _kb_projects())

    @router.callback_query(F.data.startswith("mr:proj:detail:"))
    async def _proj_detail(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        pid = query.data.split(":", 3)[3]
        p = mr_state.get_project(pid)
        if not p:
            await query.answer("Проект не найден", show_alert=True)
            return await _send_or_edit(query, _render_projects(), _kb_projects())
        await query.answer()
        await _send_or_edit(query, _render_project_detail(p), _kb_project_detail(p))

    @router.callback_query(F.data.startswith("mr:proj:cancel:"))
    async def _proj_cancel(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        pid = query.data.split(":", 3)[3]
        p = mr_state.get_project(pid)
        if not p:
            await query.answer("Проект не найден", show_alert=True)
            return
        srv = mr_state.get_active_server()
        if srv and p.get("task_id"):
            try:
                async with MasterRoomCoderClient(srv) as cli:
                    await cli.cancel(p["task_id"])
            except CoderClientError as e:
                logger.warning("masterroom cancel failed: %s", e)
        mr_state.update_project(p["id"], status="cancelled")
        await query.answer("🛑 Отменено")
        p = mr_state.get_project(pid)
        await _send_or_edit(query, _render_project_detail(p), _kb_project_detail(p))

    @router.callback_query(F.data.startswith("mr:proj:rm:"))
    async def _proj_rm(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        pid = query.data.split(":", 3)[3]
        mr_state.remove_project(pid)
        await query.answer("🗑 Удалено")
        await _send_or_edit(query, _render_projects(), _kb_projects())

    # --- Settings -----------------------------------------------------

    @router.callback_query(F.data == "mr:settings")
    async def _settings(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>⚙️ Настройки</b>\n\nДефолты для новых задач — можно переопределить в FSM.",
            _kb_settings(),
        )

    @router.callback_query(F.data == "mr:settings:stack")
    async def _settings_stack(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        await _send_or_edit(query, "<b>📚 Default stack</b>",
                            _kb_stack_picker("mr:settings:stack:set"))

    @router.callback_query(F.data.startswith("mr:settings:stack:set:"))
    async def _settings_stack_set(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        choice = query.data.split(":", 4)[4]
        mr_state.set_defaults(stack=choice)
        await query.answer("✅ Сохранено")
        await _send_or_edit(
            query, "<b>⚙️ Настройки</b>", _kb_settings(),
        )

    @router.callback_query(F.data == "mr:settings:budget")
    async def _settings_budget(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        await _send_or_edit(query, "<b>💰 Default budget</b>",
                            _kb_budget_picker("mr:settings:budget:set"))

    @router.callback_query(F.data.startswith("mr:settings:budget:set:"))
    async def _settings_budget_set(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        choice = int(query.data.split(":", 4)[4])
        mr_state.set_defaults(budget=choice)
        await query.answer("✅ Сохранено")
        await _send_or_edit(query, "<b>⚙️ Настройки</b>", _kb_settings())

    # --- ZIP project export (toggle + download) ----------------------
    @router.callback_query(F.data == "mr:zip:toggle")
    async def _zip_toggle(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        new = mr_state.toggle_zip_export_enabled()
        await query.answer("✅ Выгрузка ZIP включена" if new else "Выгрузка ZIP выключена")
        await _send_or_edit(query, "<b>⚙️ Настройки</b>", _kb_settings())

    @router.callback_query(F.data == "mr:zip:download")
    async def _zip_download(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        if not mr_state.get_zip_export_enabled():
            await query.answer("Выгрузка ZIP отключена в настройках!", show_alert=True)
            return
        await query.answer()
        status = None
        if query.message is not None:
            with contextlib.suppress(Exception):
                status = await query.message.answer("⏳ Собираю ZIP-архив проекта, подождите...")
        import asyncio
        import os

        from aiogram.types import FSInputFile

        from .project_zip import build_project_zip
        try:
            zip_path = await asyncio.to_thread(build_project_zip)
        except Exception as exc:  # noqa: BLE001
            logger.exception("masterroom: project zip build failed")
            if query.message is not None:
                await query.message.answer(f"⚠️ Не удалось собрать архив: {_html_escape(exc)}")
            return
        try:
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            doc = FSInputFile(zip_path, filename="project_backup.zip")
            if query.message is not None:
                await query.message.answer_document(
                    document=doc,
                    caption=f"📦 Полный бэкап проекта собран успешно! ({size_mb:.1f} МБ)",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("masterroom: project zip send failed")
            if query.message is not None:
                await query.message.answer(f"⚠️ Архив собран, но не отправился: {_html_escape(exc)}")
        finally:
            with contextlib.suppress(Exception):
                if status is not None:
                    await status.delete()
            with contextlib.suppress(Exception):
                os.remove(zip_path)

    # --- New task FSM ------------------------------------------------

    @router.callback_query(F.data == "mr:new")
    async def _new(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        if not mr_state.get_active_server():
            await query.answer("Сначала подключи кодер-сервер.", show_alert=True)
            await _send_or_edit(query, _render_servers(), _kb_servers())
            return
        await state.set_state(MasterRoomStates.awaiting_title)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>🚀 Новая задача — шаг 1/5</b>\n\nПришли <b>короткое название</b> (для карточки проекта).",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:menu")]
            ]),
        )

    @router.message(MasterRoomStates.awaiting_title)
    async def _new_title(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        title = (message.text or "").strip()
        if not title:
            await message.answer("Название не может быть пустым.")
            return
        await state.update_data(title=title)
        await state.set_state(MasterRoomStates.awaiting_prompt)
        await message.answer(
            "<b>Шаг 2/5</b> — пришли <b>brief</b> (текстом, что именно должен сделать кодер).",
            parse_mode="HTML",
        )

    @router.message(MasterRoomStates.awaiting_prompt)
    async def _new_prompt(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        prompt = (message.text or "").strip()
        if not prompt:
            await message.answer("Brief не может быть пустым.")
            return
        await state.update_data(prompt=prompt)
        await state.set_state(MasterRoomStates.awaiting_stack)
        await message.answer(
            "<b>Шаг 3/5</b> — выбери <b>stack</b>:",
            parse_mode="HTML",
            reply_markup=_kb_stack_picker("mr:fsm:stack"),
        )

    @router.callback_query(F.data.startswith("mr:fsm:stack:"))
    async def _new_stack(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        choice = query.data.split(":", 3)[3]
        if choice not in mr_state.STACK_CHOICES:
            await query.answer("Неизвестный stack", show_alert=True)
            return
        await state.update_data(stack=choice)
        await state.set_state(MasterRoomStates.awaiting_target)
        await query.answer()
        rows: list[list[InlineKeyboardButton]] = []
        for tid in ("render", "oracle"):
            tgt = mr_state.get_target(tid) or {}
            rows.append([InlineKeyboardButton(
                text=f"{tgt.get('name', tid)}",
                callback_data=f"mr:fsm:target:{tid}",
            )])
        for c in mr_state.list_custom_targets():
            rows.append([InlineKeyboardButton(
                text=f"{c['name']}",
                callback_data=f"mr:fsm:target:{c['id']}",
            )])
        rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="mr:menu")])
        await _send_or_edit(
            query,
            "<b>Шаг 4/5</b> — выбери <b>таргет</b> (куда деплоим):",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("mr:fsm:target:"))
    async def _new_target(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        # split into at most 4 parts so 'custom:<uuid>' stays whole
        tid = query.data.split(":", 3)[3]
        if not mr_state.get_target(tid):
            await query.answer("Таргет не найден", show_alert=True)
            return
        await state.update_data(target_id=tid)
        await state.set_state(MasterRoomStates.awaiting_budget)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>Шаг 5/5</b> — выбери <b>budget</b> (токены):",
            _kb_budget_picker("mr:fsm:budget"),
        )

    @router.callback_query(F.data.startswith("mr:fsm:budget:"))
    async def _new_budget(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        budget = int(query.data.split(":", 3)[3])
        if budget not in mr_state.BUDGET_CHOICES:
            await query.answer("Неизвестный budget", show_alert=True)
            return
        await state.update_data(budget=budget)
        await state.set_state(MasterRoomStates.confirming)
        data = await state.get_data()
        tgt = mr_state.get_target(data.get("target_id", "")) or {}
        body = (
            "<b>📋 Подтверди задачу</b>\n\n"
            f"<b>{_html_escape(data.get('title', ''))}</b>\n"
            f"stack: <code>{_html_escape(data.get('stack', ''))}</code>\n"
            f"target: <code>{_html_escape(tgt.get('name', data.get('target_id', '')))}</code>\n"
            f"budget: <code>{budget:,}</code>\n\n"
            f"<i>brief:</i>\n<code>{_html_escape(data.get('prompt', '')[:500])}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Запустить", callback_data="mr:fsm:start")],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:menu")],
        ])
        await query.answer()
        await _send_or_edit(query, body, kb)

    @router.callback_query(F.data == "mr:fsm:start")
    async def _new_start(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        data = await state.get_data()
        srv = mr_state.get_active_server()
        if not srv:
            await query.answer("Кодер-сервер не задан.", show_alert=True)
            await state.clear()
            return
        gh = mr_state.get_active_github_account()
        github_payload = (
            {"account_id": gh["id"], "owner": gh["owner"],
             "token_ref": gh["token_ref"]}
            if gh else None
        )
        callback_url = mr_state.get_link()
        prompt_with_devin_tools = (
            f"{data.get('prompt', '')}\n\n"
            "=== DEVIN.AI AUTONOMOUS WORKSPACE INSTRUCTIONS ===\n"
            "You are Devin, an elite autonomous AI software engineer operating inside this Master Room workshop.\n"
            "You have access to a full suite of professional software engineering tools. Use them to write, test, and deploy high-quality code:\n"
            "1. Terminal/Shell: You can execute any bash commands (e.g. bun run, npm install, pytest, git) to run, test, and debug code.\n"
            "2. File System: You can read, write, and edit files directly in the repository.\n"
            "3. Contract-First Design: Always define/check Zod contracts in packages/contracts before writing backend/frontend logic.\n"
            "4. DB & Migrations: Use PostgreSQL 18 and Prisma ORM. Generate primary keys as UUIDv7.\n"
            "5. Testing: Run tests (Bun Test, Playwright) and iterate on failures until 100% success. Never push untested code.\n"
            "6. Deployment: Deploy by pushing to git. The target platform will automatically pick up and deploy your changes.\n\n"
            "Act autonomously, think step-by-step, verify every change, and deliver production-grade code."
        )
        try:
            async with MasterRoomCoderClient(srv) as cli:
                resp = await cli.create_task(
                    prompt=prompt_with_devin_tools,
                    stack=data.get("stack", mr_state.DEFAULT_STACK),
                    target_id=data.get("target_id", mr_state.DEFAULT_TARGET_ID),
                    budget=int(data.get("budget", mr_state.DEFAULT_BUDGET)),
                    callback_url=callback_url,
                    github=github_payload,
                    title=data.get("title", ""),
                )
        except CoderClientError as e:
            await state.clear()
            await query.answer("Ошибка кодера", show_alert=True)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data="mr:menu")]
            ])
            await _send_or_edit(
                query,
                f"<b>❌ Кодер-сервер вернул ошибку</b>\n\n<code>{_html_escape(str(e))}</code>\n"
                f"<code>status={e.status}</code>",
                kb,
            )
            return
        task_id = str(resp.get("task_id", "") or "")
        project = mr_state.add_project({
            "task_id": task_id,
            "title": data.get("title", ""),
            "prompt": data.get("prompt", ""),
            "stack": data.get("stack", ""),
            "target_id": data.get("target_id", ""),
            "budget": int(data.get("budget", mr_state.DEFAULT_BUDGET)),
            "github_account_id": gh["id"] if gh else None,
            "status": resp.get("status", "queued") or "queued",
            "server_id": srv["id"],
        })
        await state.clear()
        await query.answer("🚀 Задача отправлена")
        await _send_or_edit(query, _render_project_detail(project),
                            _kb_project_detail(project))

    # ─── ⚙️ Управление GitHub ──────────────────────────────────────────────────

    @router.callback_query(F.data == "mr:gh:manage")
    async def _gh_manage(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        gh = mr_state.get_active_github_account()
        from .github import _resolve_token
        has_token = bool(gh and _resolve_token(gh))
        await query.answer()
        if not has_token:
            await _send_or_edit(
                query,
                "<b>⚙️ Управление GitHub</b>\n\n"
                "🔑 Нет подключённых аккаунтов.\n"
                "Подключи токен, чтобы управлять репозиториями.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔐 Вход в GitHub (логин + пароль)", callback_data="mr:gh:weblogin")],
                    [InlineKeyboardButton(text="🔑 Подключить GitHub", callback_data="mr:gh:manage:addtoken")],
                    [InlineKeyboardButton(text="🔑 Получить токен с правами", callback_data="mr:gh:tokenhelp")],
                    [InlineKeyboardButton(text="« Назад", callback_data="main:github")],
                ]),
            )
            return
        # Token connected — list accounts as drill-in buttons.
        accounts = mr_state.get_github_accounts()
        active_id = mr_state.get_active_github_id()
        rows: list[list[InlineKeyboardButton]] = []
        for a in accounts:
            marker = "🟢" if a["id"] == active_id else "⚪"
            rows.append([InlineKeyboardButton(
                text=f"{marker} {a['owner']}",
                callback_data=f"mr:gh:acc:{a['id']}")])
        rows.append([InlineKeyboardButton(text="🔐 Вход в GitHub (логин + пароль)", callback_data="mr:gh:weblogin")])
        rows.append([InlineKeyboardButton(text="🔑 Подключить токен", callback_data="mr:gh:manage:addtoken")])
        rows.append([InlineKeyboardButton(text="🔑 Получить токен с правами", callback_data="mr:gh:tokenhelp")])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data="main:github")])
        await _send_or_edit(
            query,
            "<b>⚙️ Управление GitHub</b>\n\n"
            "Выбери аккаунт, чтобы управлять его репозиториями,\n"
            "или подключи новый токен.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    # --- «Вход в GitHub» — owner enters login + password, then takes a token ---
    @router.callback_query(F.data == "mr:gh:weblogin")
    async def _gh_web_login_start(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_gh_web_login)
        await query.answer()
        saved_login = mr_state.get_gh_web_login()
        hint = (f"\n\nСейчас сохранён логин: <code>{_html_escape(saved_login)}</code>"
                if saved_login else "")
        hwline = ""
        with contextlib.suppress(Exception):
            from ..hwproxy import summary_line
            hwline = f"\n\n🖥 Железо/прокси: <code>{_html_escape(summary_line())}</code>\n" \
                     "<i>меняется в «🧩 Прочие → 🖥 Железо и прокси»</i>"
        await _send_or_edit(
            query,
            "<b>🔐 Вход в GitHub — шаг 1/2</b>\n\n"
            "Пришли <b>логин или email</b> от твоего GitHub-аккаунта "
            "(то, чем заходишь на github.com)." + hint + hwline,
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:gh:manage")]
            ]),
        )

    @router.message(MasterRoomStates.awaiting_gh_web_login)
    async def _gh_web_login_user(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        login = (message.text or "").strip()
        if not login:
            await message.answer("Логин не может быть пустым.")
            return
        mr_state.set_gh_web_login(login)
        await state.set_state(MasterRoomStates.awaiting_gh_web_password)
        await message.answer(
            "<b>🔐 Вход в GitHub — шаг 2/2</b>\n\n"
            "Теперь пришли <b>пароль</b> от GitHub.\n"
            "<i>Сообщение с паролем я сразу удалю из чата.</i>",
            parse_mode="HTML",
        )

    @router.message(MasterRoomStates.awaiting_gh_web_password)
    async def _gh_web_login_pass(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        password = (message.text or "").strip()
        if not password:
            await message.answer("Пароль не может быть пустым.")
            return
        mr_state.set_gh_web_password(password)
        # Delete the password message so it doesn't linger in the chat.
        with contextlib.suppress(Exception):
            await message.delete()
        # Apply the active hardware/proxy profile so a future browser login
        # uses the chosen antidetect fingerprint.
        with contextlib.suppress(Exception):
            from ..hwproxy import apply_active
            apply_active()
        await state.clear()
        login = mr_state.get_gh_web_login()
        await message.answer(
            "<b>✅ Данные входа в GitHub сохранены</b>\n\n"
            f"Логин: <code>{_html_escape(login)}</code>\n"
            "Пароль: <code>••••••••</code> (сохранён)\n\n"
            "Теперь нажми <b>«🔑 Взять токен»</b> — откроется страница GitHub, "
            "где по инструкции создаётся fine-grained токен. Скопируй "
            "<code>github_pat_...</code> и вставь через «🔑 Подключить токен».\n\n"
            "<i>⚠️ GitHub не выдаёт токен автоматически по паролю (защита/2FA), "
            "поэтому токен создаётся вручную на их странице — вход сохранён, "
            "чтобы тебе было удобнее туда зайти.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔑 Взять токен",
                    url="https://github.com/settings/tokens?type=beta")],
                [InlineKeyboardButton(
                    text="🔑 Подключить токен", callback_data="mr:gh:manage:addtoken")],
                [InlineKeyboardButton(text="« Назад", callback_data="mr:gh:manage")],
            ]),
        )

    # --- Token help: how to create a GitHub token with the right scopes ---
    @router.callback_query(F.data == "mr:gh:tokenhelp")
    async def _gh_token_help(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        await _send_or_edit(
            query,
            "<b>🔑 Получение токена с правами</b>\n\n"
            "Создай токен: "
            "<a href=\"https://github.com/settings/tokens?type=beta\">github.com/settings/tokens</a>\n"
            "<i>Если хочешь дойти сам: github.com → твой аватар → Settings → "
            "Developer settings (самый низ левой колонки) → Personal access "
            "tokens → Fine-grained tokens → Generate new token.</i>\n\n"
            "<b>На странице Generate new token:</b>\n"
            "• <b>Token name</b> — любое (например <code>lilush-bot</code>)\n"
            "• <b>Expiration</b> — на твой выбор (90 дней / без срока)\n"
            "• <b>Resource owner</b> — твой аккаунт\n"
            "• <b>Repository access → All repositories</b> ⚠️ (обязательно — новый "
            "репо ещё не существует, чтобы выбрать его поштучно)\n"
            "• <b>Permissions → Repository permissions:</b>\n"
            "   – <b>Administration</b> → Read and write (создание/удаление репо)\n"
            "   – <b>Contents</b> → Read and write (заливка файлов и коммиты)\n"
            "   – <b>Metadata</b> → Read-only (ставится автоматически)\n"
            "• <b>Generate token</b> → скопируй <code>github_pat_...</code>\n\n"
            "Готовый токен (<code>github_pat_...</code>) вставь через "
            "«🔑 Подключить токен».",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔗 Открыть github.com/settings/tokens",
                    url="https://github.com/settings/tokens?type=beta")],
                [InlineKeyboardButton(
                    text="🔑 Подключить токен", callback_data="mr:gh:manage:addtoken")],
                [InlineKeyboardButton(text="« Назад", callback_data="mr:gh:manage")],
            ]),
        )

    # --- Per-account screen (drill-in) ---
    @router.callback_query(F.data.startswith("mr:gh:acc:"))
    async def _gh_account(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        aid = query.data.split(":", 3)[3]
        acc = mr_state.get_github_account(aid)
        if not acc:
            await query.answer("Аккаунт не найден.", show_alert=True)
            return
        # Make it the active account so repo actions target it.
        mr_state.set_active_github_id(aid)
        await query.answer()
        _ae = mr_state.get_auto_edit_repo(aid) or mr_state.get_last_uploaded_repo(aid) or "авто"
        await _send_or_edit(
            query,
            f"<b>🐙 Аккаунт: {_html_escape(acc['owner'])}</b>\n\n"
            "Что делаем с репозиториями?",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📂 Новый репо", callback_data="mr:gh:manage:new")],
                [InlineKeyboardButton(text="📂 Обновить репо", callback_data="mr:gh:manage:update")],
                [InlineKeyboardButton(text="🗑 Удалить репо", callback_data="mr:gh:manage:delrepo")],
                [InlineKeyboardButton(text="👁 Мои репозитории", callback_data="mr:gh:manage:list")],
                [InlineKeyboardButton(text=f"📌 Репо для авто-ред.: {_html_escape(_ae)}",
                                      callback_data=f"mr:gh:autoedit:{aid}")],
                [InlineKeyboardButton(text="✏️ Редактировать все репозитории",
                                      callback_data="mr:gh:editall:ask")],
                [InlineKeyboardButton(text="🗑 Удалить этот вход", callback_data=f"mr:gh:rm:{aid}")],
                [InlineKeyboardButton(text="« Назад", callback_data="mr:gh:manage")],
            ]),
        )

    # --- «Репо для авто-редактирования» picker + «Редактировать все слоты» ---
    @router.callback_query(F.data.startswith("mr:gh:autoedit:"))
    async def _gh_autoedit(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        aid = query.data.split(":", 3)[3]
        acc = mr_state.get_github_account(aid)
        if not acc:
            await query.answer("Аккаунт не найден.", show_alert=True)
            return
        await query.answer("Загружаю репозитории…")
        try:
            from .github import list_repos
            repos = await list_repos(acc)
        except Exception as e:  # noqa: BLE001
            await query.message.answer(f"❌ Не получить список репо: {_html_escape(str(e))}")
            return
        names = [str(r.get("name", "")) for r in repos if r.get("name")][:20]
        await state.update_data(ae_aid=aid, ae_repos=names)
        cur = mr_state.get_auto_edit_repo(aid)
        cur_lbl = cur or (mr_state.get_last_uploaded_repo(aid) or "по правилу (1→он, 2+→последний)")
        rows = [[InlineKeyboardButton(
            text=("✅ " if n == cur else "") + n, callback_data=f"mr:gh:aeset:{i}")]
            for i, n in enumerate(names)]
        rows.append([InlineKeyboardButton(text="♻️ Авто (по правилу)", callback_data="mr:gh:aeset:-1")])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data=f"mr:gh:acc:{aid}")])
        await _send_or_edit(
            query,
            "<b>📌 Репозиторий для авто-редактирования</b>\n\n"
            f"Текущий: <code>{_html_escape(cur_lbl)}</code>\n\n"
            "Если не выбирать — правило: один репо → он; два и более → последний по заливке.",
            InlineKeyboardMarkup(inline_keyboard=rows))

    @router.callback_query(F.data.startswith("mr:gh:aeset:"))
    async def _gh_autoedit_set(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        idx = query.data.split(":")[-1]
        data = await state.get_data()
        aid = data.get("ae_aid", "")
        names = data.get("ae_repos", []) or []
        if not aid:
            await query.answer("Открой выбор заново.", show_alert=True)
            return
        if idx == "-1":
            mr_state.set_auto_edit_repo(aid, "")
            await query.answer("Сброшено на авто-правило")
        else:
            try:
                repo = names[int(idx)]
            except Exception:  # noqa: BLE001
                repo = ""
            mr_state.set_auto_edit_repo(aid, repo)
            await query.answer(f"Выбрано: {repo}" if repo else "Готово")
        await state.update_data(ae_aid=None, ae_repos=None)
        await _send_or_edit(
            query, "✅ Сохранено.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« К аккаунту", callback_data=f"mr:gh:acc:{aid}")]]))

    @router.callback_query(F.data == "mr:gh:editall:ask")
    async def _gh_editall_ask(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_editall_zip)
        await query.answer()
        await query.message.answer(
            "✏️ Пришли <b>.zip</b> проекта одним файлом — отредактирую "
            "авто-репозиторий в каждом заполненном слоте GitHub по очереди.",
            parse_mode="HTML")

    @router.message(MasterRoomStates.awaiting_editall_zip, F.document)
    async def _gh_editall_zip(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        doc = message.document
        if not doc or not (doc.file_name or "").lower().endswith(".zip"):
            await message.answer("❌ Нужен <b>.zip</b> файл.", parse_mode="HTML")
            return
        await state.clear()
        await _run_editall_walk(message, message.bot, doc.file_id)

    # --- Add token (inline within manage) ---
    @router.callback_query(F.data == "mr:gh:manage:addtoken")
    async def _gh_manage_addtoken(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_deploy_token)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>🔑 Подключение GitHub</b>\n\n"
            "Создай токен: "
            "<a href=\"https://github.com/settings/tokens?type=beta\">github.com/settings/tokens</a>\n"
            "<i>Если хочешь дойти сам: github.com → твой аватар → Settings → "
            "Developer settings (самый низ левой колонки) → Personal access "
            "tokens → Fine-grained tokens → Generate new token.</i>\n\n"
            "<b>На странице Generate new token:</b>\n"
            "• <b>Token name</b> — любое (например <code>lilush-bot</code>)\n"
            "• <b>Expiration</b> — на твой выбор (90 дней / без срока)\n"
            "• <b>Resource owner</b> — твой аккаунт\n"
            "• <b>Repository access → All repositories</b> ⚠️ (обязательно — новый "
            "репо ещё не существует, чтобы выбрать его поштучно)\n"
            "• <b>Permissions → Repository permissions:</b>\n"
            "   – <b>Administration</b> → Read and write (создание/удаление репо)\n"
            "   – <b>Contents</b> → Read and write (заливка файлов и коммиты)\n"
            "   – <b>Metadata</b> → Read-only (ставится автоматически)\n"
            "• <b>Generate token</b> → скопируй <code>github_pat_...</code>\n\n"
            "Вставь токен (<code>github_pat_...</code> или <code>ghp_...</code>).",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:gh:manage")],
            ]),
        )

    @router.message(MasterRoomStates.awaiting_deploy_token)
    async def _gh_manage_token_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        token = (message.text or "").strip()
        with contextlib.suppress(Exception):
            await message.delete()
        if not token:
            await message.answer("Пустой токен. Пришли ещё раз.")
            return
        from .github import GitHubError, get_authenticated_user
        try:
            user = await get_authenticated_user(token)
        except GitHubError as e:
            await message.answer(
                f"❌ Токен не подошёл: {_html_escape(str(e))}\n"
                "Проверь права и пришли снова.", parse_mode="HTML")
            return
        owner = user.get("login", "")
        if not owner:
            await message.answer("❌ Не удалось определить логин.")
            return
        if mr_state.github_slots_full():
            await state.clear()
            await message.answer(
                f"❌ Достигнут лимит {mr_state.MAX_GITHUB_SLOTS} слотов GitHub. "
                "Удали лишний вход в «⚙️ Управление GitHub».")
            return
        acc = mr_state.add_github_account(owner, owner, token=token)
        mr_state.set_active_github_id(acc["id"])
        await state.clear()
        await message.answer(
            f"✅ Подключён: <b>{_html_escape(owner)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Управление GitHub", callback_data="mr:gh:manage")],
            ]),
        )

    # --- List repos ---
    @router.callback_query(F.data == "mr:gh:manage:list")
    async def _gh_manage_list(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        gh = mr_state.get_active_github_account()
        from .github import _resolve_token
        if not gh or not _resolve_token(gh):
            await query.answer("Сначала подключи токен.", show_alert=True)
            return
        await query.answer("⏳ Загружаю список...")
        import httpx
        from .github import GITHUB_API, _headers, _resolve_token as _rt
        tok = _rt(gh)
        owner = gh["owner"]
        async with httpx.AsyncClient(timeout=30.0) as client:
            # /user/repos returns the authenticated token's repos INCLUDING
            # private ones; /users/{owner}/repos would show only public.
            r = await client.get(
                f"{GITHUB_API}/user/repos",
                params={"per_page": 50, "sort": "updated", "affiliation": "owner"},
                headers=_headers(tok),
            )
        if r.status_code >= 400:
            await _send_or_edit(query, f"❌ Ошибка {r.status_code}",
                                InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="« Назад", callback_data="mr:gh:manage")]]))
            return
        repos = r.json()
        if not repos:
            text = "<b>👁 Мои репозитории</b>\n\nСписок пуст."
        else:
            lines = [f"<b>👁 Репозитории ({owner})</b>", ""]
            for repo in repos[:20]:
                vis = "🔒" if repo.get("private") else "🌐"
                lines.append(f"{vis} <code>{repo['name']}</code>")
            if len(repos) > 20:
                lines.append(f"\n<i>...и ещё {len(repos) - 20}</i>")
            text = "\n".join(lines)
        await _send_or_edit(query, text, InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="mr:gh:manage")]]))

    # --- New repo (create + push zip) ---
    @router.callback_query(F.data == "mr:gh:manage:new")
    async def _gh_manage_new(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        gh = mr_state.get_active_github_account()
        from .github import _resolve_token
        if not gh or not _resolve_token(gh):
            await query.answer("Сначала подключи токен.", show_alert=True)
            return
        await state.update_data(deploy_mode="new")
        await query.answer()
        body, kb = _new_repo_config_screen(gh)
        await _send_or_edit(query, body, kb)

    def _new_repo_config_screen(gh):
        priv = mr_state.get_gh_auto_private()
        readme = mr_state.get_gh_auto_readme()
        body = (
            "<b>📂 Новый репо</b>\n\n"
            f"Аккаунт: <code>{_html_escape(gh['owner'])}</code>\n\n"
            "<b>Настройки (можно переключать):</b>\n"
            f"🔒 Приват: <b>{'ВКЛ' if priv else 'ВЫКЛ'}</b>\n"
            f"📄 README: <b>{'ВКЛ' if readme else 'ВЫКЛ'}</b>\n\n"
            "Выбери, как задать название:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🔒 Приват: {'ВКЛ' if priv else 'ВЫКЛ'}",
                                     callback_data="mr:gh:cfg:private"),
                InlineKeyboardButton(text=f"📄 README: {'ВКЛ' if readme else 'ВЫКЛ'}",
                                     callback_data="mr:gh:cfg:readme"),
            ],
            [InlineKeyboardButton(text="✏️ Ввести название", callback_data="mr:gh:newname:manual")],
            [InlineKeyboardButton(text="🎲 Авто название", callback_data="mr:gh:newname:auto")],
            [InlineKeyboardButton(text="⚡ Авто-всё (название + загрузка)", callback_data="mr:gh:newauto")],
            [InlineKeyboardButton(text="« Назад", callback_data="mr:gh:manage")],
        ])
        return body, kb

    # --- Toggles for private / README ---
    @router.callback_query(F.data == "mr:gh:cfg:private")
    async def _gh_cfg_private(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_gh_auto_private(not mr_state.get_gh_auto_private())
        gh = mr_state.get_active_github_account()
        await query.answer("🔒 Приватность переключена")
        body, kb = _new_repo_config_screen(gh)
        await _send_or_edit(query, body, kb)

    @router.callback_query(F.data == "mr:gh:cfg:readme")
    async def _gh_cfg_readme(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_gh_auto_readme(not mr_state.get_gh_auto_readme())
        gh = mr_state.get_active_github_account()
        await query.answer("📄 README переключён")
        body, kb = _new_repo_config_screen(gh)
        await _send_or_edit(query, body, kb)

    # --- Name choice: manual ---
    @router.callback_query(F.data == "mr:gh:newname:manual")
    async def _gh_newname_manual(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        gh = mr_state.get_active_github_account()
        await state.update_data(deploy_mode="new")
        await state.set_state(MasterRoomStates.awaiting_deploy_repo_name)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>✏️ Название репозитория</b>\n\n"
            "Пришли <b>имя</b> (латиницей, без пробелов).\n"
            f"Будет создан: <code>{_html_escape(gh['owner'])}/&lt;имя&gt;</code>",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:gh:manage:new")],
            ]),
        )

    async def _start_zip_step(query, state, repo_name: str):
        gh = mr_state.get_active_github_account()
        await state.update_data(deploy_mode="new", deploy_repo_name=repo_name)
        await state.set_state(MasterRoomStates.awaiting_deploy_zip)
        priv = "private" if mr_state.get_gh_auto_private() else "public"
        readme = "с README" if mr_state.get_gh_auto_readme() else "без README"
        await _send_or_edit(
            query,
            f"<b>📦 Прикрепи файл</b>\n\n"
            f"Репо: <code>{_html_escape(gh['owner'])}/{_html_escape(repo_name)}</code>\n"
            f"({priv}, {readme})\n\n"
            "Нажми <b>📎 Прикрепить файл</b> ниже (или просто пришли .zip "
            "как документ через скрепку).",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📎 Прикрепить файл", callback_data="mr:gh:attach")],
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:gh:manage")],
            ]),
        )

    # --- Name choice: auto ---
    @router.callback_query(F.data == "mr:gh:newname:auto")
    async def _gh_newname_auto(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        await _start_zip_step(query, state, mr_state.next_auto_repo_name())

    # --- Auto-everything (name + private + readme ON) ---
    @router.callback_query(F.data == "mr:gh:newauto")
    async def _gh_newauto(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_gh_auto_private(True)
        mr_state.set_gh_auto_readme(True)
        await query.answer("⚡ Авто: приват + README + авто-название")
        await _start_zip_step(query, state, mr_state.next_auto_repo_name())

    # --- Attach-file hint button (Telegram opens picker via paperclip) ---
    @router.callback_query(F.data == "mr:gh:attach")
    async def _gh_attach(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer(
            "Нажми скрепку 📎 внизу → Файл → выбери .zip. Я поймаю его.",
            show_alert=True,
        )

    # --- Update existing repo ---
    @router.callback_query(F.data == "mr:gh:manage:update")
    async def _gh_manage_update(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        gh = mr_state.get_active_github_account()
        from .github import _resolve_token
        if not gh or not _resolve_token(gh):
            await query.answer("Сначала подключи токен.", show_alert=True)
            return
        await state.update_data(deploy_mode="update")
        await state.set_state(MasterRoomStates.awaiting_deploy_repo_name)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>📂 Обновить репо</b>\n\n"
            "Пришли <b>имя существующего репозитория</b>.\n"
            f"Файлы будут залиты в: <code>{_html_escape(gh['owner'])}/&lt;имя&gt;</code>",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:gh:manage")],
            ]),
        )

    # --- Delete repo ---
    @router.callback_query(F.data == "mr:gh:manage:delrepo")
    async def _gh_manage_delrepo(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        gh = mr_state.get_active_github_account()
        from .github import _resolve_token
        if not gh or not _resolve_token(gh):
            await query.answer("Сначала подключи токен.", show_alert=True)
            return
        await state.set_state(MasterRoomStates.awaiting_deploy_repo_name)
        await state.update_data(deploy_mode="delete")
        await query.answer()
        await _send_or_edit(
            query,
            "<b>🗑 Удалить репо</b>\n\n"
            "Пришли <b>имя репозитория</b> для удаления.\n"
            f"⚠️ Это удалит <code>{_html_escape(gh['owner'])}/&lt;имя&gt;</code> <b>навсегда</b>!",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:gh:manage")],
            ]),
        )

    # --- Repo name handler (shared for new/update/delete) ---
    @router.message(MasterRoomStates.awaiting_deploy_repo_name)
    async def _gh_deploy_repo_name(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        name = (message.text or "").strip().replace(" ", "-")
        if not name:
            await message.answer("Имя не может быть пустым.")
            return
        data = await state.get_data()
        mode = data.get("deploy_mode", "new")
        gh = mr_state.get_active_github_account()
        if mode == "delete":
            # Delete the repo.
            from .github import _resolve_token
            import httpx
            from .github import GITHUB_API, _headers
            tok = _resolve_token(gh)
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.delete(
                    f"{GITHUB_API}/repos/{gh['owner']}/{name}",
                    headers=_headers(tok),
                )
            await state.clear()
            if r.status_code == 204:
                await message.answer(
                    f"✅ Репозиторий <code>{_html_escape(gh['owner'])}/{_html_escape(name)}</code> удалён.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚙️ Управление GitHub", callback_data="mr:gh:manage")]]),
                )
            else:
                await message.answer(
                    f"❌ Не удалось удалить ({r.status_code}). Проверь права токена (delete_repo).",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚙️ Управление GitHub", callback_data="mr:gh:manage")]]),
                )
            return
        # new or update → ask for zip
        await state.update_data(deploy_repo_name=name)
        await state.set_state(MasterRoomStates.awaiting_deploy_zip)
        action = "Создам и залью" if mode == "new" else "Обновлю"
        await message.answer(
            f"<b>📦 Последний шаг</b>\n\n"
            f"Репо: <code>{_html_escape(gh['owner'])}/{_html_escape(name)}</code>\n"
            f"{action} файлы.\n\n"
            "Пришли <b>.zip файл</b> как документ (📎 → файл).",
            parse_mode="HTML",
        )

    # --- Receive ZIP (shared for new/update) ---
    @router.message(MasterRoomStates.awaiting_deploy_zip)
    async def _gh_deploy_zip(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        doc = message.document
        if not doc or not (doc.file_name or "").lower().endswith(".zip"):
            await message.answer("❌ Нужен <b>.zip</b> файл.", parse_mode="HTML")
            return
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await message.answer("❌ Файл > 20 МБ — лимит Telegram.", parse_mode="HTML")
            return
        data = await state.get_data()
        repo_name = data.get("deploy_repo_name", "my-project")
        mode = data.get("deploy_mode", "new")
        await state.clear()
        gh = mr_state.get_active_github_account()
        if not gh:
            await message.answer("❌ GitHub не подключён.")
            return
        status_msg = await message.answer("⏳ <b>Скачиваю и распаковываю...</b>", parse_mode="HTML")
        import io
        try:
            from aiogram import Bot
            bot: Bot = message.bot  # type: ignore[assignment]
            file = await bot.get_file(doc.file_id)
            buf = io.BytesIO()
            await bot.download_file(file.file_path, buf)
            buf.seek(0)
        except Exception as e:
            await status_msg.edit_text(f"❌ Не удалось скачать: {e}")
            return
        await _deploy_zip_to_github(status_msg, gh, repo_name, mode, buf)

    # ─── 💾 Хранилище (file manager in /data) ────────────────────────────────

    def _storage_dir():
        from ...config import DATA_DIR
        d = DATA_DIR / "storage"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _storage_files():
        """All files under /data/storage, recursively, sorted by relative path.

        Recursive so generated/montaged videos that land in subfolders
        (e.g. «сделанное видео/») show up here too. The same ordered list
        backs both the rendered list and the index-based callbacks.
        """
        d = _storage_dir()
        files = [p for p in d.rglob("*") if p.is_file()]
        return sorted(files, key=lambda p: str(p.relative_to(d)).lower())

    def _human_size(n: int) -> str:
        for unit in ("Б", "КБ", "МБ", "ГБ"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.0f} ТБ"

    def _render_storage():
        d = _storage_dir()
        files = _storage_files()
        rows: list[list[InlineKeyboardButton]] = []
        if not files:
            body = ("<b>💾 Хранилище</b>\n\n"
                    "Папка пуста. Загрузи файл (видео, zip, mp3) — он "
                    "сохранится в <code>/data/storage</code> и переживёт "
                    "перезапуск сервера.\n\n"
                    "<i>Сюда же приходят сгенерированные и смонтированные видео.</i>")
        else:
            lines = ["<b>💾 Хранилище</b>", "", "Файлы в <code>/data/storage</code>:"]
            for i, p in enumerate(files):
                rel = str(p.relative_to(d))
                size = _human_size(p.stat().st_size)
                ext = p.suffix.lower()
                icon = ("🎬" if ext in {".mp4", ".mov", ".mkv", ".avi", ".webm"}
                        else "📦" if ext == ".zip"
                        else "🎵" if ext in {".mp3", ".wav", ".m4a", ".ogg"}
                        else "📄")
                lines.append(f"{icon} <code>{_html_escape(rel)}</code> · {size}")
                rows.append([InlineKeyboardButton(
                    text=f"{icon} {rel[:30]}", callback_data=f"mr:st:f:{i}")])
            body = "\n".join(lines)
        rows.append([InlineKeyboardButton(text="⬆️ Загрузить файл", callback_data="mr:st:upload")])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:menu")])
        return body, InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data == "mr:storage")
    async def _storage_screen(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        body, kb = _render_storage()
        await _send_or_edit(query, body, kb)

    @router.callback_query(F.data == "mr:st:upload")
    async def _storage_upload(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_storage_upload)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>⬆️ Загрузка в хранилище</b>\n\n"
            "Пришли файл как документ (📎 → файл): видео, zip, mp3 — что угодно.\n"
            "Сохраню в <code>/data/storage</code>.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:storage")],
            ]),
        )

    @router.message(MasterRoomStates.awaiting_storage_upload)
    async def _storage_receive(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        doc = message.document
        if not doc:
            await message.answer("Нужен файл как документ (📎 → файл).")
            return
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await message.answer("❌ Файл > 20 МБ — лимит Telegram на скачивание ботом.")
            return
        await state.clear()
        status = await message.answer("⏳ Сохраняю...")
        try:
            from aiogram import Bot
            bot: Bot = message.bot  # type: ignore[assignment]
            file = await bot.get_file(doc.file_id)
            dest = _storage_dir() / (doc.file_name or f"file_{doc.file_unique_id}")
            await bot.download_file(file.file_path, str(dest))
        except Exception as e:
            await status.edit_text(f"❌ Не удалось сохранить: {e}")
            return
        await status.delete()
        body, kb = _render_storage()
        await message.answer(
            f"✅ Сохранено: <code>{_html_escape(dest.name)}</code>\n\n{body}",
            parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

    @router.callback_query(F.data.startswith("mr:st:f:"))
    async def _storage_file(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        idx = int(query.data.split(":")[-1])
        d = _storage_dir()
        files = _storage_files()
        if idx < 0 or idx >= len(files):
            await query.answer("Файл не найден.", show_alert=True)
            body, kb = _render_storage()
            await _send_or_edit(query, body, kb)
            return
        p = files[idx]
        ext = p.suffix.lower()
        size = _human_size(p.stat().st_size)
        is_video = ext in {".mp4", ".mov", ".mkv", ".avi", ".webm"}
        rows: list[list[InlineKeyboardButton]] = []
        if ext == ".zip":
            rows.append([InlineKeyboardButton(text="📦 Распаковать в мастерскую (кодер)",
                                              callback_data=f"mr:st:unzip:{idx}")])
        if is_video:
            rows.append([InlineKeyboardButton(text="🎬 В Монтажёр",
                                              callback_data=f"mr:st:edit:{idx}")])
            rows.append([InlineKeyboardButton(text="🌀 Омни монтаж",
                                              callback_data=f"mr:st:omni:{idx}")])
            rows.append([InlineKeyboardButton(text="🚀 Публикация",
                                              callback_data=f"mr:st:pub:{idx}")])
        rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mr:st:del:{idx}")])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:storage")])
        await query.answer()
        await _send_or_edit(
            query,
            f"<b>📄 {_html_escape(p.name)}</b>\n\n"
            f"Размер: {size}\n"
            f"Путь: <code>/data/storage/{_html_escape(str(p.relative_to(_storage_dir())))}</code>",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("mr:st:pub:"))
    async def _storage_to_pub(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        idx = int(query.data.split(":")[-1])
        d = _storage_dir()
        files = _storage_files()
        if idx < 0 or idx >= len(files):
            await query.answer("Файл не найден.", show_alert=True)
            return
        with contextlib.suppress(Exception):
            from ..publication import store as pub_store
            pub_store.set_pending_video(str(files[idx]))
        await query.answer()
        await _send_or_edit(
            query,
            "🚀 Видео выбрано для публикации.\n\nОткрой <b>📢 Публикация</b>, "
            "выбери аккаунт и нажми «🚀 Опубликовать видео» — зальётся это видео.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Открыть Публикацию", callback_data="pub:home")],
                [InlineKeyboardButton(text="« Назад", callback_data="mr:storage")]]))

    @router.callback_query(F.data.startswith("mr:st:edit:"))
    async def _storage_to_editor(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        idx = int(query.data.split(":")[-1])
        d = _storage_dir()
        files = _storage_files()
        if idx < 0 or idx >= len(files):
            await query.answer("Файл не найден.", show_alert=True)
            return
        p = files[idx]
        await query.answer("🎬 Запускаю монтажёр...")
        # Feed the file into the same job pipeline the «🎞 Монтажёр» button
        # uses: enqueue an `analyze` job → analyze → edit → seo → publish.
        try:
            from ...jobs import get_default_queue
            queue = get_default_queue()
            chat_id = query.message.chat.id if query.message else None
            await queue.enqueue(
                "analyze",
                {"source_path": str(p), "duration_s": 0.0, "language": None},
                chat_id=chat_id,
            )
        except Exception as e:
            await _send_or_edit(
                query, f"❌ Не удалось запустить монтажёр: {_html_escape(str(e))}",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="« Назад", callback_data="mr:storage")]]))
            return
        await _send_or_edit(
            query,
            "🎞 <b>Монтажёр запущен</b>\n\n"
            f"Источник: <code>{_html_escape(p.name)}</code>\n"
            "<i>анализ → нарезка → SEO → публикация</i>\n\n"
            "Готовые нарезки попадут в <code>/data/storage/сделанное видео/</code> "
            "и придут сюда. Прогресс — <code>/jobs</code>.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Хранилище", callback_data="mr:storage")]]),
        )

    @router.callback_query(F.data.startswith("mr:st:omni:"))
    async def _storage_to_omni(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        idx = int(query.data.split(":")[-1])
        files = _storage_files()
        if idx < 0 or idx >= len(files):
            await query.answer("Файл не найден.", show_alert=True)
            return
        p = files[idx]
        with contextlib.suppress(Exception):
            from ..editor_agent.omni import set_selected_video
            set_selected_video(str(p))
        await query.answer("🌀 Видео передано в Омни")
        await _send_or_edit(
            query,
            "🌀 <b>Видео выбрано для Омни монтажа.</b>\n\n"
            f"Файл: <code>{_html_escape(p.name)}</code>\n\n"
            "Открой <b>🎬 Омни от Гугл</b> (Монтажёр → Омни от Гугл), "
            "проверь промты и нажми «🌀 Смонтировать через Омни».",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎬 Открыть Омни", callback_data="omni:home")],
                [InlineKeyboardButton(text="« Назад", callback_data="mr:storage")]]))

    @router.callback_query(F.data.startswith("mr:st:unzip:"))
    async def _storage_unzip(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        idx = int(query.data.split(":")[-1])
        d = _storage_dir()
        files = _storage_files()
        if idx < 0 or idx >= len(files) or files[idx].suffix.lower() != ".zip":
            await query.answer("Это не zip.", show_alert=True)
            return
        p = files[idx]
        await query.answer("📦 Распаковываю...")
        import zipfile
        from ...config import PROJECTS_DIR
        target = PROJECTS_DIR / p.stem
        target.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if "__pycache__" not in n]
                for n in names:
                    if n.endswith("/"):
                        continue
                    zf.extract(n, target)
            count = len([n for n in names if not n.endswith("/")])
        except Exception as e:
            await _send_or_edit(query, f"❌ Не распаковать: {e}",
                                InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="« Назад", callback_data="mr:storage")]]))
            return
        await _send_or_edit(
            query,
            f"✅ Распаковано <b>{count}</b> файлов в мастерскую:\n"
            f"<code>{target}</code>\n\n"
            "Теперь кодер может работать с этим проектом.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Хранилище", callback_data="mr:storage")]]),
        )

    @router.callback_query(F.data.startswith("mr:st:del:"))
    async def _storage_delete(query: CallbackQuery) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        idx = int(query.data.split(":")[-1])
        d = _storage_dir()
        files = _storage_files()
        if 0 <= idx < len(files):
            with contextlib.suppress(Exception):
                files[idx].unlink()
            await query.answer("🗑 Удалено")
        else:
            await query.answer("Файл не найден.")
        body, kb = _render_storage()
        await _send_or_edit(query, body, kb)

    # ─── 📥 ZIP прислан прямо в чат (без активного шага) ─────────────────────

    def _is_loose_zip(message: Message) -> bool:
        d = message.document
        return bool(d and (d.file_name or "").lower().endswith(".zip"))

    @router.message(_is_loose_zip)
    async def _chat_zip_dropped(message: Message, state: FSMContext) -> None:
        # Only react when no FSM flow is active (deploy/storage zip steps,
        # registered earlier, already handle their own zips by state).
        if await state.get_state() is not None:
            return
        if not await _is_owner_msg(message):
            return
        doc = message.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            return  # too big to download; let other handlers/agent see it
        await state.update_data(chatzip_file_id=doc.file_id,
                                chatzip_name=doc.file_name or "archive.zip")
        await message.answer(
            f"📥 Получил архив <code>{_html_escape(doc.file_name or 'archive.zip')}</code>.\n\n"
            "Что с ним сделать?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💾 В хранилище", callback_data="mr:chatzip:store")],
                [InlineKeyboardButton(text="⚡ Создать репо авто (название+приват+README+загрузка)",
                                      callback_data="mr:chatzip:auto")],
                [InlineKeyboardButton(text="✏️ Редактировать все репозитории",
                                      callback_data="mr:chatzip:editall")],
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:chatzip:cancel")],
            ]),
        )

    @router.callback_query(F.data == "mr:chatzip:cancel")
    async def _chat_zip_cancel(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await query.answer("Отменено")
        with contextlib.suppress(Exception):
            await query.message.edit_text("✖ Отменено.")

    @router.callback_query(F.data == "mr:chatzip:store")
    async def _chat_zip_store(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        data = await state.get_data()
        file_id = data.get("chatzip_file_id")
        name = data.get("chatzip_name", "archive.zip")
        await state.clear()
        if not file_id:
            await query.answer("Файл не найден, пришли заново.", show_alert=True)
            return
        await query.answer("💾 Сохраняю...")
        try:
            from aiogram import Bot
            bot: Bot = query.bot  # type: ignore[assignment]
            file = await bot.get_file(file_id)
            dest = _storage_dir() / name
            await bot.download_file(file.file_path, str(dest))
        except Exception as e:
            await _send_or_edit(query, f"❌ Не сохранить: {_html_escape(str(e))}",
                                InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="« Хранилище", callback_data="mr:storage")]]))
            return
        body, kb = _render_storage()
        await _send_or_edit(query, f"✅ Сохранено: <code>{_html_escape(name)}</code>\n\n{body}", kb)

    @router.callback_query(F.data == "mr:chatzip:auto")
    async def _chat_zip_auto(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        from .github import _resolve_token
        gh = mr_state.get_active_github_account()
        if not gh or not _resolve_token(gh):
            await query.answer("Сначала подключи GitHub в Управление GitHub.", show_alert=True)
            return
        data = await state.get_data()
        file_id = data.get("chatzip_file_id")
        await state.clear()
        if not file_id:
            await query.answer("Файл не найден, пришли заново.", show_alert=True)
            return
        await query.answer("⚡ Создаю репо и загружаю...")
        mr_state.set_gh_auto_private(True)
        mr_state.set_gh_auto_readme(True)
        repo_name = mr_state.next_auto_repo_name()
        import io
        status_msg = await query.message.answer("⏳ <b>Скачиваю архив...</b>", parse_mode="HTML")
        try:
            from aiogram import Bot
            bot: Bot = query.bot  # type: ignore[assignment]
            file = await bot.get_file(file_id)
            buf = io.BytesIO()
            await bot.download_file(file.file_path, buf)
            buf.seek(0)
        except Exception as e:
            await status_msg.edit_text(f"❌ Не скачать: {_html_escape(str(e))}")
            return
        await _deploy_zip_to_github(status_msg, gh, repo_name, "new", buf)

    @router.callback_query(F.data == "mr:chatzip:editall")
    async def _chat_zip_editall(query: CallbackQuery, state: FSMContext) -> None:
        # «✏️ Редактировать все репозитории» — walk every filled GitHub slot in
        # order, editing each slot's auto-edit repo with the dropped zip. One
        # token is active at a time (switch → edit → switch to next).
        if not await _is_owner_query(query):
            return await _deny(query)
        data = await state.get_data()
        file_id = data.get("chatzip_file_id")
        await state.clear()
        if not file_id:
            await query.answer("Файл не найден, пришли заново.", show_alert=True)
            return
        await query.answer("✏️ Редактирую по всем слотам…")
        await _run_editall_walk(query.message, query.bot, file_id)

    # ─── 🖥 render.com browser-deploy screen ───────────────────────────────

    def _kb_render() -> InlineKeyboardMarkup:
        auto = "включена ✅" if mr_state.get_render_auto() else "выключена ⛔"
        bt = "задан ✅" if mr_state.get_render_bot_token() else "не задан ⛔"
        login = _render_login_label(mr_state.get_render_login_method())
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📖 Инструкция (как это работает)", callback_data="mr:render:howto")],
            [InlineKeyboardButton(text=f"🤖 Токен бота: {bt}", callback_data="mr:render:bottoken")],
            [InlineKeyboardButton(text=f"🔐 Способ входа: {login}", callback_data="mr:render:login")],
            [InlineKeyboardButton(text=f"🔁 Авто-загрузка: {auto}", callback_data="mr:render:auto")],
            [InlineKeyboardButton(text="🧩 Прочие", callback_data="mr:render:misc")],
            [InlineKeyboardButton(text="🚀 Загрузить на render сейчас", callback_data="mr:render:deploy")],
            [InlineKeyboardButton(text="« Вернуться", callback_data="mr:menu")],
        ])

    def _kb_render_misc() -> InlineKeyboardMarkup:
        d = "вкл ✅" if mr_state.get_render_delete_prev() else "выкл ⛔"
        p = "вкл ✅" if mr_state.get_render_pause_prev() else "выкл ⛔"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 Удалять предыдущий проект: {d}", callback_data="mr:render:misc:delete")],
            [InlineKeyboardButton(text=f"💤 Паузить предыдущий проект: {p}", callback_data="mr:render:misc:pause")],
            [InlineKeyboardButton(text="« Вернуться", callback_data="mr:render")],
        ])

    def _render_screen_text() -> str:
        last = mr_state.get_render_last_project() or "—"
        pending = (mr_state.get_render_pending() or {}).get("repo") or "—"
        return (
            "<b>🖥 render.com</b>\n\n"
            "Деплой готового репозитория на render.com через stealth-браузер: "
            "бот сам заходит на dashboard, гасит старый сервис и поднимает новый "
            "(Free 512 МБ, env <code>BOT_TOKEN</code>, health-check <code>/healthz</code>).\n\n"
            f"• Последний задеплоенный: <code>{_html_escape(last)}</code>\n"
            f"• Готов к загрузке: <code>{_html_escape(pending)}</code>\n\n"
            "Не знаешь, с чего начать — жми <b>📖 Инструкция</b>."
        )

    @router.callback_query(F.data == "mr:render")
    async def _render_home_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        await _send_or_edit(query, _render_screen_text(), _kb_render())

    @router.callback_query(F.data == "mr:render:auto")
    async def _render_auto_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_render_auto(not mr_state.get_render_auto())
        await query.answer("Готово")
        await _send_or_edit(query, _render_screen_text(), _kb_render())

    # --- 📖 Инструкция (пошагово: что делать и что выйдет) ---
    def _render_howto_text() -> str:
        return (
            "<b>📖 render.com — как это работает</b>\n\n"
            "Делай по шагам, справа — что произойдёт:\n\n"
            "<b>1. Опубликуй репозиторий на GitHub</b>\n"
            "→ бот запомнит его как «Готов к загрузке».\n\n"
            "<b>2. 🤖 Токен бота</b>\n"
            "Впиши значение для env <code>BOT_TOKEN</code> (твоё «123»).\n"
            "→ без него деплой не запустится; оно подставится в переменную "
            "<code>BOT_TOKEN</code> на render.\n\n"
            "<b>3. 🔐 Способ входа</b> (выбери один)\n"
            "• <b>3️⃣ Импорт cookie</b> — пришли <code>storage_state.json</code>. "
            "→ самый надёжный: бот заходит уже залогиненным, без логина/пароля.\n"
            "• <b>1️⃣ GitHub OAuth</b> — задай логин+пароль GitHub. "
            "→ вход через «Continue with GitHub» (2FA может прервать).\n"
            "• <b>2️⃣ Email + пароль</b> — задай email+пароль render. "
            "→ прямой вход в форму render.\n"
            "→ после первого входа сессия сохраняется и держится сама.\n\n"
            "<b>4. 🧩 Прочие (опционально)</b>\n"
            "Удалять или паузить предыдущий сервис при новом деплое.\n"
            "→ старый сервис будет удалён / поставлен на Suspend.\n\n"
            "<b>5. 🔁 Авто-загрузка (опционально)</b>\n"
            "→ деплой на render будет стартовать сразу после публикации на GitHub.\n\n"
            "<b>6. 🚀 Загрузить на render сейчас</b>\n"
            "Бот по очереди: 🔐 входит → 💤/🗑 гасит-или-удаляет старый → "
            "🚀 создаёт новый Free-сервис (512 МБ, <code>BOT_TOKEN</code>, "
            "health-check <code>/healthz</code>).\n"
            "→ в чат идёт прогресс-бар по шагам, в конце — отчёт:\n"
            "  ✅ «создан и отправлен на деплой (подтверждено)» — всё ок;\n"
            "  🟡 «шаги выполнены, но не подтверждено» — проверь сервис и скриншот;\n"
            "  ⚠️ ошибка (напр. «не залогинен» / «вёрстка изменилась»).\n\n"
            "<i>Вход и клики идут по живому сайту render — если render поменяет "
            "вёрстку, тексты кнопок в макросе может понадобиться поправить.</i>"
        )

    @router.callback_query(F.data == "mr:render:howto")
    async def _render_howto_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        await _send_or_edit(
            query, _render_howto_text(),
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Вернуться", callback_data="mr:render")],
            ]))

    # --- Прочие (тумблеры удаления/паузы предыдущего проекта) ---
    @router.callback_query(F.data == "mr:render:misc")
    async def _render_misc_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>🧩 Прочие</b>\n\n"
            "• <b>Удалять предыдущий проект</b> — старый сервис удаляется "
            "(имеет приоритет над паузой).\n"
            "• <b>Паузить предыдущий проект</b> — старый сервис ставится на "
            "Suspend (это то, что было сделано раньше).",
            _kb_render_misc())

    @router.callback_query(F.data == "mr:render:misc:delete")
    async def _render_misc_delete(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_render_delete_prev(not mr_state.get_render_delete_prev())
        await query.answer("Готово")
        await _send_or_edit(query, "<b>🧩 Прочие</b>", _kb_render_misc())

    @router.callback_query(F.data == "mr:render:misc:pause")
    async def _render_misc_pause(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_render_pause_prev(not mr_state.get_render_pause_prev())
        await query.answer("Готово")
        await _send_or_edit(query, "<b>🧩 Прочие</b>", _kb_render_misc())

    # --- Bot token (the value typed for BOT_TOKEN on render) ---
    @router.callback_query(F.data == "mr:render:bottoken")
    async def _render_bottoken_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_render_bot_token)
        await query.answer()
        cur = mr_state.get_render_bot_token()
        rows = [[InlineKeyboardButton(text="✖ Отмена", callback_data="mr:render")]]
        if cur:
            rows.insert(0, [InlineKeyboardButton(text="🗑 Удалить токен", callback_data="mr:render:bottoken:del")])
        await _send_or_edit(
            query,
            "<b>🤖 Токен бота для render</b>\n\n"
            "Пришли значение, которое бот впишет в env-переменную "
            "<code>BOT_TOKEN</code> на render.com (то самое «123»). Оно "
            "сохраняется и подставляется при каждом деплое, пока не поменяешь/удалишь.\n\n"
            + ("<i>Сейчас токен задан.</i>" if cur else "<i>Сейчас не задан.</i>"),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data == "mr:render:bottoken:del")
    async def _render_bottoken_del(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_render_bot_token("")
        await state.clear()
        await query.answer("Удалено")
        await _send_or_edit(query, _render_screen_text(), _kb_render())

    @router.message(MasterRoomStates.awaiting_render_bot_token)
    async def _render_bottoken_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        val = (message.text or "").strip()
        with contextlib.suppress(Exception):
            await message.delete()
        if not val:
            await message.answer("Пусто. Пришли значение ещё раз.")
            return
        mr_state.set_render_bot_token(val)
        await state.clear()
        await message.answer("✅ Токен бота сохранён.", reply_markup=_kb_render())

    # ─── 🔐 Способ входа (login method) ──────────────────────────────────
    def _kb_render_login() -> InlineKeyboardMarkup:
        cur = mr_state.get_render_login_method()
        def _mk(m: str) -> str:
            return "✅" if cur == m else "⚪️"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{_mk('github')} 1️⃣ GitHub OAuth",
                                  callback_data="mr:render:login:github")],
            [InlineKeyboardButton(text=f"{_mk('email')} 2️⃣ Email + пароль",
                                  callback_data="mr:render:login:email")],
            [InlineKeyboardButton(text=f"{_mk('cookie')} 3️⃣ Импорт cookie/сессии",
                                  callback_data="mr:render:login:cookie")],
            [InlineKeyboardButton(text="« Вернуться", callback_data="mr:render")],
        ])

    def _render_login_screen_text() -> str:
        cur = mr_state.get_render_login_method()
        return (
            "<b>🔐 Способ входа на render.com</b>\n\n"
            "Перед деплоем бот должен авторизоваться на render.com. "
            "Сессия сохраняется (cookies + localStorage), поэтому вход "
            "держится между запусками.\n\n"
            f"Текущий способ: <b>{_html_escape(_render_login_label(cur))}</b>\n\n"
            "<b>1️⃣ GitHub OAuth</b> — вход через GitHub.\n"
            "<b>2️⃣ Email + пароль</b> — прямой вход по render email/паролю.\n"
            "<b>3️⃣ Импорт cookie/сессии</b> — разовый импорт storage_state.\n\n"
            "<i>Выбери способ и настрой его на следующем экране.</i>"
        )

    def _kb_render_login_github() -> InlineKeyboardMarkup:
        u = "задан ✅" if mr_state.get_render_gh_login() else "не задан ⛔"
        p = "задан ✅" if mr_state.get_render_gh_password() else "не задан ⛔"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"👤 Логин GitHub: {u}",
                                  callback_data="mr:render:login:gh:user")],
            [InlineKeyboardButton(text=f"🔑 Пароль GitHub: {p}",
                                  callback_data="mr:render:login:gh:pass")],
            [InlineKeyboardButton(text="« Назад", callback_data="mr:render:login")],
        ])

    def _render_login_github_text() -> str:
        return (
            "<b>1️⃣ GitHub OAuth</b>\n\n"
            "Бот заходит на render.com и жмёт «Continue with GitHub». Если в "
            "сохранённой сессии браузера ещё нет входа в GitHub, он заполнит "
            "форму логина GitHub этими данными и подтвердит доступ Render "
            "(один раз). Дальше сессия держится сама.\n\n"
            "⚠️ Это <b>веб-логин</b> GitHub (как в браузере), а не PAT-токен. "
            "Если на аккаунте включена 2FA — автоматический вход может "
            "прерваться на коде; тогда лучше способ 2 или 3.\n\n"
            "🖥 Железо/прокси берётся из «🧩 Прочие → 🖥 Железо и прокси».\n\n"
            "Задай логин и пароль GitHub ниже."
        )

    @router.callback_query(F.data == "mr:render:login")
    async def _render_login_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        await query.answer()
        await _send_or_edit(query, _render_login_screen_text(), _kb_render_login())

    @router.callback_query(F.data == "mr:render:login:github")
    async def _render_login_github_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        mr_state.set_render_login_method("github")
        await query.answer("GitHub OAuth")
        await _send_or_edit(query, _render_login_github_text(), _kb_render_login_github())

    def _kb_render_login_email() -> InlineKeyboardMarkup:
        e = "задан ✅" if mr_state.get_render_email() else "не задан ⛔"
        p = "задан ✅" if mr_state.get_render_password() else "не задан ⛔"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📧 Email render: {e}",
                                  callback_data="mr:render:login:em:user")],
            [InlineKeyboardButton(text=f"🔑 Пароль render: {p}",
                                  callback_data="mr:render:login:em:pass")],
            [InlineKeyboardButton(text="« Назад", callback_data="mr:render:login")],
        ])

    def _render_login_email_text() -> str:
        return (
            "<b>2️⃣ Email + пароль</b>\n\n"
            "Прямой вход на render.com по email и паролю аккаунта render. "
            "Бот открывает страницу входа render, заполняет форму этими "
            "данными и жмёт «Sign in». Сессия сохраняется (cookies), дальше "
            "вход держится сам.\n\n"
            "✅ Самый прямой способ, если у render-аккаунта есть пароль "
            "(не только OAuth) и нет 2FA.\n\n"
            "Задай email и пароль ниже."
        )

    @router.callback_query(F.data == "mr:render:login:email")
    async def _render_login_email_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        mr_state.set_render_login_method("email")
        await query.answer("Email + пароль")
        await _send_or_edit(query, _render_login_email_text(), _kb_render_login_email())

    @router.callback_query(F.data == "mr:render:login:em:user")
    async def _render_email_user_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_render_email)
        await query.answer()
        cur = mr_state.get_render_email()
        rows = [[InlineKeyboardButton(text="✖ Отмена", callback_data="mr:render:login:email")]]
        if cur:
            rows.insert(0, [InlineKeyboardButton(text="🗑 Удалить", callback_data="mr:render:login:em:user:del")])
        await _send_or_edit(
            query,
            "<b>📧 Email render</b>\n\n"
            "Пришли email аккаунта render.com.\n\n"
            + (f"<i>Сейчас: <code>{_html_escape(cur)}</code></i>" if cur else "<i>Сейчас не задан.</i>"),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data == "mr:render:login:em:user:del")
    async def _render_email_user_del(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_render_email("")
        await state.clear()
        await query.answer("Удалено")
        await _send_or_edit(query, _render_login_email_text(), _kb_render_login_email())

    @router.message(MasterRoomStates.awaiting_render_email)
    async def _render_email_user_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        val = (message.text or "").strip()
        with contextlib.suppress(Exception):
            await message.delete()
        if not val:
            await message.answer("Пусто. Пришли email ещё раз.")
            return
        mr_state.set_render_email(val)
        await state.clear()
        await message.answer("✅ Email render сохранён.", reply_markup=_kb_render_login_email())

    @router.callback_query(F.data == "mr:render:login:em:pass")
    async def _render_email_pass_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_render_password)
        await query.answer()
        cur = mr_state.get_render_password()
        rows = [[InlineKeyboardButton(text="✖ Отмена", callback_data="mr:render:login:email")]]
        if cur:
            rows.insert(0, [InlineKeyboardButton(text="🗑 Удалить", callback_data="mr:render:login:em:pass:del")])
        await _send_or_edit(
            query,
            "<b>🔑 Пароль render</b>\n\n"
            "Пришли пароль аккаунта render.com. Сообщение будет сразу удалено.\n\n"
            + ("<i>Сейчас задан.</i>" if cur else "<i>Сейчас не задан.</i>"),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data == "mr:render:login:em:pass:del")
    async def _render_email_pass_del(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_render_password("")
        await state.clear()
        await query.answer("Удалено")
        await _send_or_edit(query, _render_login_email_text(), _kb_render_login_email())

    @router.message(MasterRoomStates.awaiting_render_password)
    async def _render_email_pass_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        val = (message.text or "").strip()
        with contextlib.suppress(Exception):
            await message.delete()
        if not val:
            await message.answer("Пусто. Пришли пароль ещё раз.")
            return
        mr_state.set_render_password(val)
        await state.clear()
        await message.answer("✅ Пароль render сохранён.", reply_markup=_kb_render_login_email())

    def _render_session_path():
        """Path of the saved render browser session (cookies + localStorage).

        Mirrors render_deploy.deploy_to_render: ``DATA_DIR/session/render_session.json``.
        """
        from pathlib import Path
        try:
            from ...config import DATA_DIR
            base = DATA_DIR / "session"
        except Exception:  # noqa: BLE001
            base = Path("session")
        return base / "render_session.json"

    def _kb_render_login_cookie() -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text="⬆️ Загрузить storage_state.json",
                                      callback_data="mr:render:login:ck:upload")]]
        if _render_session_path().exists():
            rows.append([InlineKeyboardButton(text="🗑 Удалить сессию",
                                              callback_data="mr:render:login:ck:del")])
        rows.append([InlineKeyboardButton(text="« Назад", callback_data="mr:render:login")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _render_login_cookie_text() -> str:
        present = _render_session_path().exists()
        status = "✅ сессия загружена" if present else "⛔ сессии нет"
        return (
            "<b>3️⃣ Импорт cookie/сессии</b>\n\n"
            "Разовый импорт готовой сессии браузера: пришли файл "
            "<code>storage_state.json</code> (формат Playwright — cookies + "
            "localStorage). Бот запишет его как сессию render, и при деплое "
            "вход уже будет выполнен — без логина/пароля.\n\n"
            "Где взять: в браузере, где ты уже залогинен на render.com, "
            "экспортируй storage_state (Playwright "
            "<code>context.storage_state(path=...)</code>) или соответствующий "
            "JSON с cookies.\n\n"
            f"Статус: <b>{status}</b>"
        )

    @router.callback_query(F.data == "mr:render:login:cookie")
    async def _render_login_cookie_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        mr_state.set_render_login_method("cookie")
        await query.answer("Импорт cookie")
        await _send_or_edit(query, _render_login_cookie_text(), _kb_render_login_cookie())

    @router.callback_query(F.data == "mr:render:login:ck:upload")
    async def _render_cookie_upload_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_render_cookie_upload)
        await query.answer()
        await _send_or_edit(
            query,
            "<b>⬆️ Импорт сессии render</b>\n\n"
            "Пришли файл <code>storage_state.json</code> как документ "
            "(📎 → файл). Я запишу его как сессию render.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отмена", callback_data="mr:render:login:cookie")],
            ]))

    @router.callback_query(F.data == "mr:render:login:ck:del")
    async def _render_cookie_del(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        with contextlib.suppress(Exception):
            _render_session_path().unlink(missing_ok=True)
        await state.clear()
        await query.answer("Сессия удалена")
        await _send_or_edit(query, _render_login_cookie_text(), _kb_render_login_cookie())

    @router.message(MasterRoomStates.awaiting_render_cookie_upload)
    async def _render_cookie_receive(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        doc = message.document
        if not doc:
            await message.answer("Нужен файл как документ (📎 → файл).")
            return
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await message.answer("❌ Файл > 20 МБ — лимит Telegram на скачивание ботом.")
            return
        await state.clear()
        status = await message.answer("⏳ Импортирую сессию…")
        try:
            import io
            import json as _json
            from aiogram import Bot
            bot: Bot = message.bot  # type: ignore[assignment]
            file = await bot.get_file(doc.file_id)
            buf = io.BytesIO()
            await bot.download_file(file.file_path, buf)
            buf.seek(0)
            raw = buf.read()
            # Validate it parses as JSON (storage_state is a JSON object).
            _json.loads(raw.decode("utf-8"))
            dest = _render_session_path()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        except Exception as e:  # noqa: BLE001
            await status.edit_text(f"❌ Не удалось импортировать: {_html_escape(str(e))}")
            return
        mr_state.set_render_login_method("cookie")
        with contextlib.suppress(Exception):
            await status.delete()
        await message.answer(
            "✅ Сессия render импортирована. Теперь деплой будет заходить "
            "уже залогиненным.",
            reply_markup=_kb_render_login_cookie())

    # --- GitHub web login (used by the GitHub-OAuth method) ---
    @router.callback_query(F.data == "mr:render:login:gh:user")
    async def _render_gh_user_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_render_gh_login)
        await query.answer()
        cur = mr_state.get_render_gh_login()
        rows = [[InlineKeyboardButton(text="✖ Отмена", callback_data="mr:render:login:github")]]
        if cur:
            rows.insert(0, [InlineKeyboardButton(text="🗑 Удалить", callback_data="mr:render:login:gh:user:del")])
        await _send_or_edit(
            query,
            "<b>👤 Логин GitHub</b>\n\n"
            "Пришли username или email GitHub-аккаунта для входа на render.com.\n\n"
            + (f"<i>Сейчас: <code>{_html_escape(cur)}</code></i>" if cur else "<i>Сейчас не задан.</i>"),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data == "mr:render:login:gh:user:del")
    async def _render_gh_user_del(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_render_gh_login("")
        await state.clear()
        await query.answer("Удалено")
        await _send_or_edit(query, _render_login_github_text(), _kb_render_login_github())

    @router.message(MasterRoomStates.awaiting_render_gh_login)
    async def _render_gh_user_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        val = (message.text or "").strip()
        with contextlib.suppress(Exception):
            await message.delete()
        if not val:
            await message.answer("Пусто. Пришли логин ещё раз.")
            return
        mr_state.set_render_gh_login(val)
        await state.clear()
        await message.answer("✅ Логин GitHub сохранён.", reply_markup=_kb_render_login_github())

    @router.callback_query(F.data == "mr:render:login:gh:pass")
    async def _render_gh_pass_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.set_state(MasterRoomStates.awaiting_render_gh_password)
        await query.answer()
        cur = mr_state.get_render_gh_password()
        rows = [[InlineKeyboardButton(text="✖ Отмена", callback_data="mr:render:login:github")]]
        if cur:
            rows.insert(0, [InlineKeyboardButton(text="🗑 Удалить", callback_data="mr:render:login:gh:pass:del")])
        await _send_or_edit(
            query,
            "<b>🔑 Пароль GitHub</b>\n\n"
            "Пришли пароль GitHub-аккаунта. Сообщение будет сразу удалено.\n\n"
            + ("<i>Сейчас задан.</i>" if cur else "<i>Сейчас не задан.</i>"),
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data == "mr:render:login:gh:pass:del")
    async def _render_gh_pass_del(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        mr_state.set_render_gh_password("")
        await state.clear()
        await query.answer("Удалено")
        await _send_or_edit(query, _render_login_github_text(), _kb_render_login_github())

    @router.message(MasterRoomStates.awaiting_render_gh_password)
    async def _render_gh_pass_msg(message: Message, state: FSMContext) -> None:
        if not await _is_owner_msg(message):
            return
        val = (message.text or "").strip()
        with contextlib.suppress(Exception):
            await message.delete()
        if not val:
            await message.answer("Пусто. Пришли пароль ещё раз.")
            return
        mr_state.set_render_gh_password(val)
        await state.clear()
        await message.answer("✅ Пароль GitHub сохранён.", reply_markup=_kb_render_login_github())

    # --- Deploy now (manual trigger) ---
    @router.callback_query(F.data == "mr:render:deploy")
    async def _render_deploy_cb(query: CallbackQuery, state: FSMContext) -> None:
        if not await _is_owner_query(query):
            return await _deny(query)
        await state.clear()
        pending = mr_state.get_render_pending() or {}
        repo_name = pending.get("repo", "")
        owner = pending.get("owner", "")
        if not repo_name:
            await query.answer()
            await _send_or_edit(
                query,
                "⚠️ Сначала опубликуй репозиторий на GitHub — тогда здесь "
                "появится, что деплоить.\n\n" + _render_screen_text(),
                _kb_render())
            return
        await query.answer("Запускаю…")
        await _run_render_deploy(query.message, repo_name, owner)

    return router
