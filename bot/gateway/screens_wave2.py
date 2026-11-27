"""Wave 2-3 screen coverage for the Platform Gateway (этап 4 ТЗ, Part C).

Wave 2 registers neutral (``MenuScreen``) versions of the read-only /
informational screens so they open on every platform (site, Discord,
VK) instead of the «⏳ только в Telegram» stub:

===========  ==========================  ====================================
screen_id    TG source (callback)        content
===========  ==========================  ====================================
``tokens``   ``main:tokens``             token spend stats as text
``debates``  ``main:debates``            feature description + topics list
``promptbox``  ``main:promptbox``        saved prompts (read-only)
``github``   ``main:github``/``gh:menu`` cloned repos list (read-only)
``projmap``  ``main:projmap``            project-map summary as text
``misc``     ``misc:home``               «Прочие» functions list
===========  ==========================  ====================================

Wave 3 migrates the *simple* toggle/confirm actions whose TG handlers
are a single operation + redraw:

* ``gh:auto``    — GitHub auto-clone toggle (owner-only, same as TG);
* ``dump:clear`` — empty the «🗑 мусор» bin (per-user, same as TG).

Design rules honoured here (see ``docs/handoff/README.md`` /
``PART_C_SCREENS.md``):

* **Only public stage-1 APIs** are used for integration:
  :func:`bot.gateway.menu.register_screen`,
  :func:`bot.gateway.dispatch.register_action`.
* **Equivalence (A2)** — button texts / order / callback strings are
  taken 1:1 from the SAME builders :mod:`bot.wizard` and the addons use
  (read-only imports), so the neutral screens can never drift from TG.
* **Interactive dialogs stay in TG** — buttons that lead to FSM flows
  keep their original action string and are *not* registered as
  actions; :func:`bot.gateway.dispatch.dispatch_action` answers
  ``tg_only`` with a deep-link for them, exactly as the spec requires.
* **Owner gating inside builders** — admin screens (``tokens``,
  ``projmap``, ``misc``, ``github`` — plus ``debates``/``promptbox``,
  whose TG handlers are owner-only too) show a polite «Доступно только
  владельцу» screen for guests, modelled on ``build_settings_screen``.
* **Degradation** — every data source is imported lazily inside a
  ``try`` block; a broken addon degrades the body text, never the
  import of this module.
"""

from __future__ import annotations

import html as _html_mod
import logging
import re

from .dispatch import GatewayContext, ScreenResult, register_action
from .menu import MAX_ACTION_BYTES, MenuButton, MenuScreen, register_screen

logger = logging.getLogger("gateway.screens_wave2")

_TAG_RE = re.compile(r"<[^>]+>")

#: Text shown to non-owners on owner-only screens (per PART_C spec).
OWNER_ONLY_TEXT = "Доступно только владельцу"

#: Back button used on wave-2 screens and denial screens.
_BACK_MAIN = MenuButton(text="↩️ Назад", action="main:menu")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _plain(text: str | None) -> str:
    """Strip the HTML markup TG texts use → platform-neutral plain text."""
    return _html_mod.unescape(_TAG_RE.sub("", text or "")).strip()


def _is_admin(user_id: int | None) -> bool:
    """Owner/co-owner check — same helper as everywhere in the project.

    Routed through :func:`bot.access.can_admin`; fails closed.
    """
    if user_id is None:
        return False
    try:
        from .. import access

        return bool(access.can_admin(int(user_id)))
    except Exception:  # noqa: BLE001
        logger.exception("wave2 owner check failed closed")
        return False


def _denied(screen_id: str, title: str, parent: str = "main") -> MenuScreen:
    """Polite owner-only refusal screen: text + «Назад», nothing else."""
    return MenuScreen(
        screen_id=screen_id,
        title=title,
        body=OWNER_ONLY_TEXT,
        rows=[[_BACK_MAIN]],
        parent=parent,
    )


def _rows_from_tg(markup) -> list[list[MenuButton]]:
    """aiogram ``InlineKeyboardMarkup`` → neutral rows, 1:1 (text, action, url).

    Buttons whose callback_data somehow exceeds the 64-byte limit are
    skipped defensively (they would be broken in TG as well); this keeps
    ``validate_screen`` green for every wave-2 screen.
    """
    rows: list[list[MenuButton]] = []
    for tg_row in getattr(markup, "inline_keyboard", []) or []:
        row: list[MenuButton] = []
        for btn in tg_row:
            action = getattr(btn, "callback_data", None) or ""
            url = getattr(btn, "url", None)
            if action and len(action.encode("utf-8")) > MAX_ACTION_BYTES:
                logger.warning("wave2: dropping oversized action %r", action)
                continue
            row.append(MenuButton(text=btn.text, action=action, url=url))
        if row:
            rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Wave 2 · «tokens» — mirrors main:tokens (format_token_stats text)
# --------------------------------------------------------------------------


def build_tokens_screen(user_id: int | None) -> MenuScreen:
    title = "📊 Статистика токенов"
    if not _is_admin(user_id):
        return _denied("tokens", title)
    try:
        from ..token_tracker import format_token_stats

        body = _plain(format_token_stats())
    except Exception:  # noqa: BLE001
        logger.exception("token stats failed")
        body = "⚠️ Статистика токенов сейчас недоступна. Проверь логи."
    rows = [
        [MenuButton(text="🔄 Обновить", action="main:tokens")],
        [_BACK_MAIN],
    ]
    return MenuScreen(
        screen_id="tokens", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# Wave 2 · «debates» — mirrors main:debates home (description + topics)
# --------------------------------------------------------------------------


def _debate_topics() -> list[str]:
    """Topic files shipped with the project (docs/DEBATE_TOPICS/*.md)."""
    try:
        from pathlib import Path

        topics_dir = Path(__file__).resolve().parents[2] / "docs" / "DEBATE_TOPICS"
        return sorted(p.stem for p in topics_dir.glob("*.md"))
    except Exception:  # noqa: BLE001
        logger.exception("debate topics listing failed")
        return []


def build_debates_screen(user_id: int | None) -> MenuScreen:
    title = "🎤 Дебаты"
    # TG's main:debates handler is owner-only (bot/addons/debates/handlers.py).
    if not _is_admin(user_id):
        return _denied("debates", title)
    body = "Пульт сервера debate-ii: многодверные ИИ-дебаты."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from ..addons.debates import handlers as deb

        servers = deb._servers()
        body = _plain(deb._render_home(servers))
        rows = _rows_from_tg(deb._kb_home(bool(servers)))
    except Exception:  # noqa: BLE001
        logger.exception("debates home failed")
        body = "⚠️ Экран дебатов сейчас недоступен. Проверь логи."
    topics = _debate_topics()
    if topics:
        body += "\n\nТемы (docs/DEBATE_TOPICS):\n" + "\n".join(
            f"• {t}" for t in topics
        )
    return MenuScreen(
        screen_id="debates", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# Wave 2 · «promptbox» — mirrors main:promptbox home (read-only list)
# --------------------------------------------------------------------------


def build_promptbox_screen(user_id: int | None) -> MenuScreen:
    title = "🗄 Хранилище промтов"
    # TG's promptbox handlers are owner-only (`_ok` → wizard._is_owner).
    if not _is_admin(user_id):
        return _denied("promptbox", title)
    body = "Хранилище промтов."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from ..addons.promptbox import handlers as pb

        body = _plain(pb._render_home())
        saved = [
            f"{i + 1}. {s['desc'] or s['prompt'][:40]}"
            for i, s in enumerate(pb._get_slots())
            if s["desc"] or s["prompt"]
        ]
        if saved:
            body += "\n\nСохранённые промты:\n" + "\n".join(saved)
        else:
            body += "\n\nСохранённых промтов пока нет."
        rows = _rows_from_tg(pb._kb_home())
    except Exception:  # noqa: BLE001
        logger.exception("promptbox home failed")
        body = "⚠️ Хранилище промтов сейчас недоступно. Проверь логи."
    return MenuScreen(
        screen_id="promptbox", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# Wave 2 · «github» — mirrors gh:menu (repo list, read-only)
# --------------------------------------------------------------------------


def build_github_screen(user_id: int | None) -> MenuScreen:
    title = "🐙 GitHub проекты"
    if not _is_admin(user_id):
        return _denied("github", title)
    body = "Склонированные GitHub-репозитории."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from .. import wizard

        body = _plain(wizard._github_menu_body())
        rows = _rows_from_tg(wizard._kb_github_menu())
    except Exception:  # noqa: BLE001
        logger.exception("github menu failed")
        body = "⚠️ Список репозиториев сейчас недоступен. Проверь логи."
    return MenuScreen(
        screen_id="github", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# Wave 2 · «projmap» — mirrors main:projmap (summary text + same buttons)
# --------------------------------------------------------------------------


def build_projmap_screen(user_id: int | None) -> MenuScreen:
    title = "🗺 Карта проекта"
    if not _is_admin(user_id):
        return _denied("projmap", title)
    body = "Интерактивный граф функций проекта."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from ..addons.projectmap import installer as _pm_install
        from ..addons.projectmap import store as _pm_store

        status = {
            "none": "не установлен",
            "installing": "устанавливается…",
            "ready": "готов",
            "error": "ошибка установки",
        }.get(_pm_store.get_install(), _pm_store.get_install())
        graph = _pm_store.get_graph()
        nodes = graph.get("node_count", 0) if isinstance(graph, dict) else 0
        body = (
            "🗺 Карта проекта\n\n"
            f"graphify: {status}\n"
            f"Функций на карте: {nodes}\n"
            f"Курируемых связок: {len(_pm_store.get_edges())}\n"
            f"Использовать в ИИ: {'ВКЛ' if _pm_store.get_use() else 'ВЫКЛ'} · "
            f"Авто: {'ВКЛ' if _pm_store.get_auto() else 'ВЫКЛ'}\n\n"
            "Интерактивный граф функций и их связок (сначала → потом). "
            "Экспортируй базу связок и вставь её в другой такой же проект."
        )
        # Same rows / order / callback strings as the TG `_pm_kb()`.
        rows = []
        if _pm_store.is_enabled() and _pm_store.get_link():
            rows.append(
                [MenuButton(text="🔗 Открыть карту", action="", url=_pm_store.get_link())]
            )
            rows.append([MenuButton(text="🔄 Новая ссылка", action="main:projmap:rotate")])
            rows.append([MenuButton(text="🔴 Выключить", action="main:projmap:off")])
        else:
            rows.append([MenuButton(text="🔗 Включить карту", action="main:projmap:open")])
        rows.append([MenuButton(text="🔁 Перестроить карту", action="main:projmap:rebuild")])
        rows.append(
            [MenuButton(text="🧠 Гермес: проанализировать", action="main:projmap:hermes")]
        )
        rows.append([MenuButton(text="📤 Экспорт базы связок", action="main:projmap:export")])
        gtxt = (
            "✅ graphify скачан (обновить)"
            if _pm_install.is_installed()
            else "📥 Скачать graphify (опц.)"
        )
        rows.append([MenuButton(text=gtxt, action="main:projmap:install")])
        rows.append(
            [MenuButton(text="⚙️ Настройки (использовать/авто)", action="pmg:settings")]
        )
        rows.append([MenuButton(text="↩️ Назад", action="main:menu")])
    except Exception:  # noqa: BLE001
        logger.exception("projmap summary failed")
        body = "⚠️ Карта проекта сейчас недоступна. Проверь логи."
        rows = [[_BACK_MAIN]]
    return MenuScreen(
        screen_id="projmap", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# Wave 2 · «misc» — mirrors misc:home («🧩 Прочие»)
# --------------------------------------------------------------------------


def build_misc_screen(user_id: int | None) -> MenuScreen:
    title = "🧩 Прочие"
    if not _is_admin(user_id):
        return _denied("misc", title)
    body = "Дополнительные функции, которые не влезли в основное меню."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from .. import wizard

        body = _plain(wizard._MISC_HOME_TEXT)
        rows = _rows_from_tg(wizard._kb_misc())
        # «список прочих функций со статусами»: every function of this
        # screen is available (they are plain sub-screens, no toggles).
        funcs = [
            btn.text
            for row in rows
            for btn in row
            if btn.action and btn.action != "misc:back"
        ]
        if funcs:
            body += "\n\nФункции:\n" + "\n".join(f"• {t} — доступна" for t in funcs)
    except Exception:  # noqa: BLE001
        logger.exception("misc home failed")
        body = "⚠️ Экран «Прочие» сейчас недоступен. Проверь логи."
    return MenuScreen(
        screen_id="misc", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# Wave 2 · action → screen bridges (the SAME callback strings as TG)
# --------------------------------------------------------------------------

_SCREEN_BUILDERS = {
    "tokens": build_tokens_screen,
    "debates": build_debates_screen,
    "promptbox": build_promptbox_screen,
    "github": build_github_screen,
    "projmap": build_projmap_screen,
    "misc": build_misc_screen,
}

#: TG callback string → wave-2 screen id. Registered through the public
#: ``register_action`` API so pressing the ORIGINAL buttons (main menu,
#: settings) opens the neutral screen on non-TG platforms instead of
#: the tg_only stub. In TG itself nothing changes — aiogram routers win.
_ACTION_TO_SCREEN = {
    "main:tokens": "tokens",
    "main:debates": "debates",
    "main:promptbox": "promptbox",
    "pb:home": "promptbox",
    "main:github": "github",
    "gh:menu": "github",
    "main:projmap": "projmap",
    "misc:home": "misc",
}


def _make_screen_opener(screen_id: str):
    async def _open(action: str, ctx: GatewayContext) -> ScreenResult:
        return ScreenResult(screen=_SCREEN_BUILDERS[screen_id](ctx.user_id))

    return _open


# --------------------------------------------------------------------------
# Wave 3 · simple migrated actions (one operation + redraw, as in TG)
# --------------------------------------------------------------------------


async def _act_gh_auto(action: str, ctx: GatewayContext) -> ScreenResult:
    """``gh:auto`` — GitHub auto-clone toggle. TG: admin-only, one flip."""
    if not _is_admin(ctx.user_id):
        return ScreenResult(error="forbidden", text="⛔ Только владелец может менять.")
    try:
        from ..storage import storage

        storage.set_github_auto_clone(not storage.get_github_auto_clone())
    except Exception:  # noqa: BLE001
        logger.exception("gh:auto toggle failed")
        return ScreenResult(
            error="handler_failed", text="⚠️ Не получилось переключить. Попробуй ещё раз."
        )
    return ScreenResult(screen=build_github_screen(ctx.user_id))


async def _act_dump_clear(action: str, ctx: GatewayContext) -> ScreenResult:
    """``dump:clear`` — empty the user's «🗑 мусор» bin (confirm action).

    Same operation as the TG handler: one ``store.clear(uid)`` call.
    Guests go through the existing ``cleanup`` feature gate in dispatch.
    """
    if ctx.user_id is None:
        return ScreenResult(
            error="forbidden", text="⛔ Сначала привяжи аккаунт (код в «🔗 Привязка»)."
        )
    try:
        from ..addons.dump import store

        removed = store.clear(int(ctx.user_id))
    except Exception:  # noqa: BLE001
        logger.exception("dump:clear failed")
        return ScreenResult(
            error="handler_failed", text="⚠️ Не получилось очистить. Попробуй ещё раз."
        )
    return ScreenResult(text=f"🧹 Корзина очищена — удалено: {removed}.")


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def _register_all() -> None:
    for screen_id, builder in _SCREEN_BUILDERS.items():
        register_screen(screen_id, builder)
    for action, screen_id in _ACTION_TO_SCREEN.items():
        register_action(action, _make_screen_opener(screen_id))
    # Wave 3 — migrated simple actions.
    register_action("gh:auto", _act_gh_auto)
    register_action("dump:clear", _act_dump_clear)


_register_all()
