"""Wave 2b screen coverage for the Platform Gateway (фикс 1.5, ТЗ §7).

Adds the MISSING wave-2 screens on top of :mod:`bot.gateway.screens_wave2`
(which stays untouched — its screens have their own tests). Same pattern:
builders read REAL data from storage / addon state, screens open through
the public stage-1 APIs, mutating actions are separate ``register_action``
handlers, owner gating lives INSIDE the builders via ``access.can_admin``.

===============  ==========================  ===============================
screen_id        TG source (callback)        content
===============  ==========================  ===============================
``roles``        ``roles:menu``              current persona + gate state
``roles_pick``   ``roles:pick``              20-persona grid (pick = confirm)
``memory``       ``main:memory``             history status (per-user)
``memory_clear`` ``memory:clear``            confirm step before wiping
``access``       ``access:menu``             access mode + co-owner list
``terminal``     ``main:terminal``           terminal status + guest toggles
``brains``       ``main:brain``              brain slots (keys MASKED)
``slack``        ``slack:home``              Slack connector status
===============  ==========================  ===============================

Design rules honoured (same as wave 2, see ``docs/handoff/README.md``):

* **Only public stage-1 APIs** — ``register_screen`` / ``register_action``.
* **Equivalence (A2)** — bodies / keyboards are produced by the SAME
  builders TG uses (:mod:`bot.wizard`, memory / slack addon handlers),
  imported read-only, so neutral screens can never drift from TG.
* **Interactive dialogs stay in TG** — FSM buttons (``roles:pick`` role
  picker input, ``memory:set_limit``, ``slack:connect``, brain key entry
  …) keep their original action string; unregistered ones get the
  lawful ``tg_only`` deep-link stub from ``dispatch_action``.
* **Owner gating inside builders** — ``roles``/``roles_pick`` (TG's
  ``cb_roles_menu`` is owner-only), ``access``, ``terminal``, ``brains``
  and ``slack`` (reached via owner-only «🧩 Прочие», shows the masked
  Slack token) show the polite «Доступно только владельцу» screen for
  guests. ``memory`` is per-user like in TG, but the CLEAR action is
  owner-only per the fix spec — and destructive, so it goes through a
  confirmation screen (``memory_clear``).
* **Secrets are never shown in full** — API keys / tokens are masked
  with the same ``_mask`` scheme the Discord/VK adapters use.
* **Degradation** — every data source is imported lazily inside a
  ``try`` block; a broken addon degrades the body text, never the
  import of this module.
"""

from __future__ import annotations

import logging

from .dispatch import (
    GatewayContext,
    ScreenResult,
    register_action,
    register_action_prefix,
)
from .menu import MenuButton, MenuScreen, register_screen

# Re-used helpers from the wave-2 module (allowed by the fix spec).
from .screens_wave2 import _BACK_MAIN, _denied, _is_admin, _plain, _rows_from_tg

logger = logging.getLogger("gateway.screens_wave2b")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _mask(token: str) -> str:
    """Same masking scheme as the Discord/VK adapters (§9А.4)."""
    token = token or ""
    if len(token) <= 8:
        return "…" if token else ""
    return f"{token[:4]}…{token[-4:]}"


def _is_real_owner(user_id: int | None) -> bool:
    """The REAL owner (not a co-owner) — used for access-mode changes,
    mirroring ``cb_access_set`` in :mod:`bot.wizard`. Fails closed."""
    if user_id is None:
        return False
    try:
        from .. import access

        return bool(access.is_owner(int(user_id)))
    except Exception:  # noqa: BLE001
        logger.exception("wave2b real-owner check failed closed")
        return False


# --------------------------------------------------------------------------
# 1. «roles» — mirrors roles:menu (persona + gate) and roles:pick (grid)
# --------------------------------------------------------------------------


def build_roles_screen(user_id: int | None) -> MenuScreen:
    title = "🎭 Роли"
    # TG's cb_roles_menu is owner-only (bot/wizard.py, _is_owner check).
    if not _is_admin(user_id):
        return _denied("roles", title)
    body = "Смена персоны бота и требование роли."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from .. import wizard
        from ..persona import get_persona, list_personas

        body = _plain(wizard._roles_screen_body())
        active = get_persona().key
        names = [
            f"{'▶ ' if p.key == active else ''}{p.display_name} ({p.key})"
            for p in list_personas()
        ]
        if names:
            body += "\n\nВсе роли:\n" + "\n".join(f"• {n}" for n in names)
        rows = _rows_from_tg(wizard._kb_roles_menu())
    except Exception:  # noqa: BLE001
        logger.exception("roles screen failed")
        body = "⚠️ Экран ролей сейчас недоступен. Проверь логи."
        rows = [[_BACK_MAIN]]
    return MenuScreen(
        screen_id="roles", title=title, body=body, rows=rows, parent="main"
    )


def build_roles_pick_screen(user_id: int | None) -> MenuScreen:
    title = "🎭 Сменить роль"
    if not _is_admin(user_id):
        return _denied("roles_pick", title)
    body = "Выбери роль из списка."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from .. import wizard

        body = _plain(wizard._role_picker_text())
        rows = _rows_from_tg(wizard._kb_role_picker())
    except Exception:  # noqa: BLE001
        logger.exception("role picker screen failed")
        body = "⚠️ Список ролей сейчас недоступен. Проверь логи."
        rows = [[_BACK_MAIN]]
    return MenuScreen(
        screen_id="roles_pick", title=title, body=body, rows=rows, parent="roles"
    )


def _build_role_confirm_screen(user_id: int | None, persona_key: str) -> MenuScreen:
    """Confirm step for switching persona — same UX as TG ``role:pick:*``."""
    title = "🎭 Подтверждение роли"
    if not _is_admin(user_id):
        return _denied("role_confirm", title, parent="roles_pick")
    body = f"Активировать роль {persona_key}?"
    try:
        from ..persona import get_persona

        p = get_persona(persona_key)
        body = (
            f"{p.display_name}\n{p.title}\n"
            f"Отдел: {p.department} • Ранг: {p.rank}\n\n{p.description}"
        )
    except Exception:  # noqa: BLE001
        logger.exception("role confirm body failed")
    rows = [
        [MenuButton(text="✅ Использовать эту роль", action=f"role:use:{persona_key}")],
        [MenuButton(text="◀️ Назад", action="roles:pick")],
    ]
    return MenuScreen(
        screen_id="role_confirm", title=title, body=body, rows=rows, parent="roles_pick"
    )


# --------------------------------------------------------------------------
# 2. «memory» — mirrors main:memory (per-user history status)
# --------------------------------------------------------------------------


def build_memory_screen(user_id: int | None) -> MenuScreen:
    title = "💾 Память бота"
    if user_id is None:
        # Memory is per-user — an unlinked account has no history to show.
        return MenuScreen(
            screen_id="memory",
            title=title,
            body="⛔ Сначала привяжи аккаунт (код в «🔗 Привязка»).",
            rows=[[_BACK_MAIN]],
            parent="main",
        )
    body = "Статус памяти бота."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from ..addons.memory import handlers as mem
        from ..storage import storage

        history = storage.get_history(int(user_id))
        body = _plain(mem._screen_text(int(user_id)))
        rows = _rows_from_tg(mem._kb_screen(has_history=bool(history)))
    except Exception:  # noqa: BLE001
        logger.exception("memory screen failed")
        body = "⚠️ Экран памяти сейчас недоступен. Проверь логи."
        rows = [[_BACK_MAIN]]
    return MenuScreen(
        screen_id="memory", title=title, body=body, rows=rows, parent="main"
    )


def build_memory_clear_screen(user_id: int | None) -> MenuScreen:
    """Confirmation step before wiping the history (destructive → 2 taps)."""
    title = "🗑 Очистить память"
    # Clearing is owner-only per the fix spec (1.5, «очистка — только владелец»).
    if not _is_admin(user_id):
        return _denied("memory_clear", title, parent="memory")
    n = 0
    try:
        from ..storage import storage

        n = len(storage.get_history(int(user_id or 0)))
    except Exception:  # noqa: BLE001
        logger.exception("memory clear preview failed")
    body = (
        f"Точно очистить память? Будет удалено сообщений: {n}.\n"
        "Действие необратимо."
    )
    rows = [
        [MenuButton(text="✅ Да, очистить", action="memory:clear:confirm")],
        [MenuButton(text="◀️ Отмена", action="main:memory")],
    ]
    return MenuScreen(
        screen_id="memory_clear", title=title, body=body, rows=rows, parent="memory"
    )


# --------------------------------------------------------------------------
# 3. «access» — mirrors access:menu (owner ONLY)
# --------------------------------------------------------------------------


def build_access_screen(user_id: int | None) -> MenuScreen:
    title = "⛔ Доступ"
    if not _is_admin(user_id):
        return _denied("access", title)
    body = "Режим доступа бота."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from .. import wizard

        body = _plain(wizard._access_screen_body())
        rows = _rows_from_tg(wizard._kb_access_menu())
    except Exception:  # noqa: BLE001
        logger.exception("access screen failed")
        body = "⚠️ Экран доступа сейчас недоступен. Проверь логи."
        rows = [[_BACK_MAIN]]
    return MenuScreen(
        screen_id="access", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# 4. «terminal» — status + guest on/off toggles (owner ONLY)
# --------------------------------------------------------------------------


def build_terminal_screen(user_id: int | None) -> MenuScreen:
    title = "🖥 Терминал"
    if not _is_admin(user_id):
        return _denied("terminal", title)
    body = "Статус терминала."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from ..storage import storage

        pm_on = storage.pm_is_enabled()
        guest_use = storage.pm_get_flag("features", "terminal")
        guest_see = storage.pm_get_flag("visibility", "terminal")
        body = (
            "🖥 Терминал\n\n"
            "Пакетный режим команд (/exec, /clone, /project, /cd, /git). "
            "Сам ввод команд — диалог, он доступен в Telegram "
            "(кнопка ниже даст ссылку).\n\n"
            f"Приват-режим (гейтинг гостей): {'ВКЛ' if pm_on else 'ВЫКЛ'}\n"
            f"Гостям пользоваться: {'ВКЛ' if guest_use else 'ВЫКЛ'}\n"
            f"Гостям видно в меню: {'ВКЛ' if guest_see else 'ВЫКЛ'}"
        )
        use_label = (
            "🔒 Гостям пользоваться: ВКЛ" if guest_use else "🔓 Гостям пользоваться: ВЫКЛ"
        )
        see_label = (
            "👁 Гостям видно: ВКЛ" if guest_see else "🙈 Гостям видно: ВЫКЛ"
        )
        rows = [
            [MenuButton(text="⌨️ Ввести команды (Telegram)", action="main:terminal")],
            [MenuButton(text=use_label, action="term:guest:use")],
            [MenuButton(text=see_label, action="term:guest:see")],
            [_BACK_MAIN],
        ]
    except Exception:  # noqa: BLE001
        logger.exception("terminal screen failed")
        body = "⚠️ Экран терминала сейчас недоступен. Проверь логи."
        rows = [[_BACK_MAIN]]
    return MenuScreen(
        screen_id="terminal", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# 5. «brains» — brain slots + active mode, keys MASKED (owner ONLY)
# --------------------------------------------------------------------------


def build_brains_screen(user_id: int | None) -> MenuScreen:
    title = "🧠 Мозги и ключи"
    if not _is_admin(user_id):
        return _denied("brains", title)
    body = "Мозги бота."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from .. import wizard
        from ..storage import storage

        mode = storage.get_brain()  # "auto" | "devin"
        lines = [
            "🧠 Мозги бота",
            "",
            f"Активный режим: {'🤖 Авто (LLM)' if mode == 'auto' else '🎯 Devin (ручной)'}",
            "",
        ]
        for slot in ("1", "2"):
            provider, api_key, base_url, model = wizard._slot_config(slot)
            label = _plain(wizard._slot_label(slot))
            if api_key:
                # NEVER the full key — adapters' masking scheme (§9А.4).
                lines.append(f"{label} — ключ: {_mask(api_key)}")
            else:
                lines.append(f"{label} — ключ не задан")
            if model:
                lines.append(f"  модель: {model}")
        body = "\n".join(lines)
        auto_label = ("▶ " if mode == "auto" else "") + "🤖 Авто (LLM)"
        devin_label = ("▶ " if mode == "devin" else "") + "🎯 Devin (ручной)"
        rows = [
            [
                MenuButton(text=auto_label, action="brains:set:auto"),
                MenuButton(text=devin_label, action="brains:set:devin"),
            ]
        ]
        rows.extend(_rows_from_tg(wizard._kb_brain()))
    except Exception:  # noqa: BLE001
        logger.exception("brains screen failed")
        body = "⚠️ Экран мозгов сейчас недоступен. Проверь логи."
        rows = [[_BACK_MAIN]]
    return MenuScreen(
        screen_id="brains", title=title, body=body, rows=rows, parent="main"
    )


# --------------------------------------------------------------------------
# 6. «slack» — connector status (owner ONLY: reached via owner-only «Прочие»,
#    shows the masked token)
# --------------------------------------------------------------------------


def build_slack_screen(user_id: int | None) -> MenuScreen:
    title = "💬 Слак"
    if not _is_admin(user_id):
        return _denied("slack", title)
    body = "Slack-коннектор."
    rows: list[list[MenuButton]] = [[_BACK_MAIN]]
    try:
        from ..addons.slack import handlers as sl

        # _main_text() already masks the token via credentials.mask_token.
        body = _plain(sl._main_text())
        rows = _rows_from_tg(sl._kb_main())
    except Exception:  # noqa: BLE001
        logger.exception("slack screen failed")
        body = "⚠️ Экран Slack сейчас недоступен. Проверь логи."
        rows = [[_BACK_MAIN]]
    return MenuScreen(
        screen_id="slack", title=title, body=body, rows=rows, parent="misc"
    )


# --------------------------------------------------------------------------
# action → screen bridges (the SAME callback strings as TG)
# --------------------------------------------------------------------------

_SCREEN_BUILDERS = {
    "roles": build_roles_screen,
    "roles_pick": build_roles_pick_screen,
    "memory": build_memory_screen,
    "memory_clear": build_memory_clear_screen,
    "access": build_access_screen,
    "terminal": build_terminal_screen,
    "brains": build_brains_screen,
    "slack": build_slack_screen,
}

#: TG callback string → wave-2b screen id (registered via register_action;
#: aiogram routers still win inside Telegram itself).
_ACTION_TO_SCREEN = {
    "roles:menu": "roles",
    "roles:pick": "roles_pick",
    "role:back_to_picker": "roles_pick",
    "main:memory": "memory",
    "memory:home": "memory",
    "access:menu": "access",
    "main:brain": "brains",
    "slack:home": "slack",
    # «Назад» buttons of the mirrored TG keyboards → existing screens.
    "roles:back": "settings",
    "memory:back": "settings",
    "access:back": "settings",
    "slack:back": "misc",
}


def _make_screen_opener(screen_id: str):
    async def _open(action: str, ctx: GatewayContext) -> ScreenResult:
        from .menu import build_screen

        screen = (
            _SCREEN_BUILDERS[screen_id](ctx.user_id)
            if screen_id in _SCREEN_BUILDERS
            else build_screen(screen_id, ctx.user_id)
        )
        if screen is None:  # pragma: no cover - registry always has these
            return ScreenResult(error="screen_failed", text="⚠️ Экран сейчас недоступен.")
        return ScreenResult(screen=screen)

    return _open


# --------------------------------------------------------------------------
# mutating actions (each = one operation + redraw, exactly like TG)
# --------------------------------------------------------------------------

_FORBIDDEN = ScreenResult(error="forbidden", text="⛔ Только владелец может менять.")
_FAILED = ScreenResult(
    error="handler_failed", text="⚠️ Не получилось выполнить. Попробуй ещё раз."
)


async def _act_roles_toggle(action: str, ctx: GatewayContext) -> ScreenResult:
    """``roles:toggle`` — flip the role gate ON↔OFF (same as TG)."""
    if not _is_admin(ctx.user_id):
        return _FORBIDDEN
    try:
        from ..storage import storage

        storage.set_role_gate_enabled(not storage.get_role_gate_enabled())
    except Exception:  # noqa: BLE001
        logger.exception("roles:toggle failed")
        return _FAILED
    return ScreenResult(screen=build_roles_screen(ctx.user_id))


async def _act_role_pick(action: str, ctx: GatewayContext) -> ScreenResult:
    """``role:pick:<key>`` — confirm step before switching (same as TG)."""
    if not _is_admin(ctx.user_id):
        return _FORBIDDEN
    persona_key = action.split(":", 2)[2] if action.count(":") >= 2 else ""
    if not _persona_exists(persona_key):
        return ScreenResult(error="unknown_role", text="⚠️ Неизвестная роль.")
    return ScreenResult(screen=_build_role_confirm_screen(ctx.user_id, persona_key))


async def _act_role_use(action: str, ctx: GatewayContext) -> ScreenResult:
    """``role:use:<key>`` — persist the persona override (same as TG)."""
    if not _is_admin(ctx.user_id):
        return _FORBIDDEN
    persona_key = action.split(":", 2)[2] if action.count(":") >= 2 else ""
    if not _persona_exists(persona_key):
        return ScreenResult(error="unknown_role", text="⚠️ Неизвестная роль.")
    try:
        from ..storage import storage

        storage.set_persona_override(persona_key)
    except Exception:  # noqa: BLE001
        logger.exception("role:use failed")
        return _FAILED
    return ScreenResult(screen=build_roles_screen(ctx.user_id))


async def _act_role_reset(action: str, ctx: GatewayContext) -> ScreenResult:
    """``role:reset`` — drop the override, back to env BOT_PERSONA."""
    if not _is_admin(ctx.user_id):
        return _FORBIDDEN
    try:
        from ..storage import storage

        storage.clear_persona_override()
    except Exception:  # noqa: BLE001
        logger.exception("role:reset failed")
        return _FAILED
    return ScreenResult(screen=build_roles_pick_screen(ctx.user_id))


def _persona_exists(key: str) -> bool:
    if not key:
        return False
    try:
        from ..persona import list_personas

        return any(p.key == key for p in list_personas())
    except Exception:  # noqa: BLE001
        logger.exception("persona lookup failed closed")
        return False


async def _act_memory_clear(action: str, ctx: GatewayContext) -> ScreenResult:
    """``memory:clear`` — destructive → show the confirmation screen first."""
    if not _is_admin(ctx.user_id):
        return _FORBIDDEN
    return ScreenResult(screen=build_memory_clear_screen(ctx.user_id))


async def _act_memory_clear_confirm(action: str, ctx: GatewayContext) -> ScreenResult:
    """``memory:clear:confirm`` — actually wipe (one storage call, as TG)."""
    if not _is_admin(ctx.user_id):
        return _FORBIDDEN
    try:
        from ..storage import storage

        storage.clear_history(int(ctx.user_id))
    except Exception:  # noqa: BLE001
        logger.exception("memory:clear:confirm failed")
        return _FAILED
    return ScreenResult(screen=build_memory_screen(ctx.user_id))


async def _act_access_set(action: str, ctx: GatewayContext) -> ScreenResult:
    """``access:set:<mode>`` — REAL owner only (mirrors ``cb_access_set``)."""
    if not _is_admin(ctx.user_id):
        return _FORBIDDEN
    if not _is_real_owner(ctx.user_id):
        return ScreenResult(
            error="forbidden",
            text="⛔ Менять режим доступа может только владелец.",
        )
    mode = action.split(":", 2)[2] if action.count(":") >= 2 else ""
    try:
        from ..storage import storage

        if mode not in storage._ACCESS_MODES:
            return ScreenResult(error="unknown_mode", text="⚠️ Неизвестный режим.")
        storage.set_access_mode(mode)
    except Exception:  # noqa: BLE001
        logger.exception("access:set failed")
        return _FAILED
    return ScreenResult(screen=build_access_screen(ctx.user_id))


def _make_term_toggle(kind: str):
    async def _toggle(action: str, ctx: GatewayContext) -> ScreenResult:
        if not _is_admin(ctx.user_id):
            return _FORBIDDEN
        try:
            from ..storage import storage

            storage.pm_toggle_flag(kind, "terminal")
        except Exception:  # noqa: BLE001
            logger.exception("terminal %s toggle failed", kind)
            return _FAILED
        return ScreenResult(screen=build_terminal_screen(ctx.user_id))

    return _toggle


async def _act_brains_set(action: str, ctx: GatewayContext) -> ScreenResult:
    """``brains:set:auto|devin`` — switch the active brain mode."""
    if not _is_admin(ctx.user_id):
        return _FORBIDDEN
    mode = action.split(":", 2)[2] if action.count(":") >= 2 else ""
    try:
        from ..storage import storage

        storage.set_brain(mode)  # validates the mode itself
    except ValueError:
        return ScreenResult(error="unknown_mode", text="⚠️ Неизвестный режим мозга.")
    except Exception:  # noqa: BLE001
        logger.exception("brains:set failed")
        return _FAILED
    return ScreenResult(screen=build_brains_screen(ctx.user_id))


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def _register_all() -> None:
    for screen_id, builder in _SCREEN_BUILDERS.items():
        register_screen(screen_id, builder)
    for action, screen_id in _ACTION_TO_SCREEN.items():
        register_action(action, _make_screen_opener(screen_id))
    # Mutating actions (owner-gated inside each handler).
    register_action("roles:toggle", _act_roles_toggle)
    register_action("role:reset", _act_role_reset)
    register_action_prefix("role:pick:", _act_role_pick)
    register_action_prefix("role:use:", _act_role_use)
    register_action("memory:clear", _act_memory_clear)
    register_action("memory:clear:confirm", _act_memory_clear_confirm)
    register_action_prefix("access:set:", _act_access_set)
    register_action("term:guest:use", _make_term_toggle("features"))
    register_action("term:guest:see", _make_term_toggle("visibility"))
    register_action_prefix("brains:set:", _act_brains_set)


_register_all()
