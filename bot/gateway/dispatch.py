"""Single action router for all platforms (ТЗ-1 §3.4).

``dispatch_action(action, ctx)`` receives the SAME callback string the
Telegram buttons use today (``roles:menu``, ``gateway:home``, …) and:

1. Gates it — owner-only ``gateway:*`` actions require the mapped TG
   user to be owner/co-owner; guest actions go through the existing
   :mod:`bot.feature_gate` (никакого дублирования гейтинга, §13.5).
2. Resolves it to a registered screen (``SCREEN_REGISTRY``) or a
   registered action handler (``ACTION_HANDLERS``).
3. Falls back to the polite ``tg_only`` stub with a deep-link when the
   screen has not been migrated yet (§3.4) — a lawful permanent answer,
   not a temporary hack.

In Telegram itself nothing is forced through here: aiogram routers keep
working as before; ``dispatch_action`` is the path for web (этап 1) and
Discord/VK (этапы 2–3), plus the ``gateway:*`` screens everywhere.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .menu import MenuScreen, SCREEN_REGISTRY, build_screen

logger = logging.getLogger("gateway.dispatch")


@dataclass
class GatewayContext:
    platform: str                    # "telegram" | "web" | "discord" | "vk"
    external_id: str = ""            # user id on that platform
    user_id: int | None = None       # mapped TG user_id (None = not linked → guest)
    reply: Callable | None = None    # how to answer on that platform (optional)


@dataclass
class ScreenResult:
    screen: MenuScreen | None = None   # new screen (redraw the message)
    text: str | None = None            # or just text
    error: str | None = None           # machine-readable error code
    deeplink: str | None = None        # for the tg_only stub


# --------------------------------------------------------------------------
# action → screen aliases (actions that today simply open another screen)
# --------------------------------------------------------------------------

ACTION_SCREEN_ALIASES: dict[str, str] = {
    "main:menu": "main",
    "main:settings": "settings",
    "settings:back": "main",
}

# --------------------------------------------------------------------------
# action handlers (mutating actions owned by the gateway itself)
# --------------------------------------------------------------------------

ActionHandler = Callable[[str, GatewayContext], "ScreenResult | Awaitable[ScreenResult]"]

#: exact action string → handler. Populated by :mod:`bot.gateway.screens`
#: (gateway's own toggles) — and later by migrated wizard actions.
ACTION_HANDLERS: dict[str, ActionHandler] = {}


#: action prefix → handler, for parametrised actions like
#: ``gateway:unlink:<platform>:<external_id>``. Longest prefix wins.
ACTION_PREFIX_HANDLERS: dict[str, ActionHandler] = {}


def register_action(action: str, handler: ActionHandler) -> None:
    ACTION_HANDLERS[action] = handler


def register_action_prefix(prefix: str, handler: ActionHandler) -> None:
    ACTION_PREFIX_HANDLERS[prefix] = handler


def _find_handler(action: str) -> ActionHandler | None:
    handler = ACTION_HANDLERS.get(action)
    if handler is not None:
        return handler
    best: ActionHandler | None = None
    best_len = -1
    for prefix, h in ACTION_PREFIX_HANDLERS.items():
        if action.startswith(prefix) and len(prefix) > best_len:
            best, best_len = h, len(prefix)
    return best


# --------------------------------------------------------------------------
# gating helpers
# --------------------------------------------------------------------------

def _is_owner(user_id: int | None) -> bool:
    """Owner/co-owner check — same helper the TG keyboards use.

    Deliberately routed through :func:`bot.access.can_admin` (not raw
    storage) so the gateway can never disagree with the «⛔ Доступ» /
    «🌐 Платформы» visibility rules in :mod:`bot.wizard`. Fails closed.
    """
    if user_id is None:
        return False
    try:
        from .. import access
        return bool(access.can_admin(int(user_id)))
    except Exception:  # noqa: BLE001
        logger.exception("owner check failed closed")
        return False


def _guest_may_use(action: str, user_id: int | None) -> bool:
    """Re-use the existing feature gate for non-owner actions (§13.5).

    Mirrors :func:`bot.feature_gate.build_feature_use_gate_middleware`:
    unknown features pass through; known features check ``guest_can_use``.
    """
    try:
        from ..feature_gate import resolve_feature
        from ..storage import storage

        if not storage.pm_is_enabled():
            return True
        feature = resolve_feature(action, None)
        if feature is None:
            return True
        from ..access import guest_can_use
        return guest_can_use(user_id, feature)
    except Exception:  # noqa: BLE001
        logger.exception("guest gate failed open")
        return True


def _deeplink() -> str | None:
    """Best-effort ``t.me`` deep-link for the tg_only stub."""
    try:
        from ..addons import state as addon_state
        username = addon_state.get("gateway", "bot_username", "")
        if username:
            return f"https://t.me/{username}"
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------
# the dispatcher
# --------------------------------------------------------------------------

async def dispatch_action(action: str, ctx: GatewayContext) -> ScreenResult:
    """Route one action string coming from any platform."""
    action = (action or "").strip()
    if not action:
        return ScreenResult(error="empty_action")

    # 1. Security: gateway admin screens are owner-only on EVERY platform
    # (Elevation-of-Privilege control from §12.1 — gate at the dispatcher,
    # not at the render: a hidden button is not protection).
    if action.startswith("gateway:") and not _is_owner(ctx.user_id):
        return ScreenResult(
            error="forbidden",
            text="⛔ Только владелец может управлять платформами.",
        )

    # 2. Guest feature gating — same toggles, same texts as TG (A8).
    if not _is_owner(ctx.user_id) and not _guest_may_use(action, ctx.user_id):
        return ScreenResult(
            error="forbidden",
            text="⛔ Эта функция недоступна. Обратись к владельцу бота.",
        )

    # 3. Registered mutating action?
    handler = _find_handler(action)
    if handler is not None:
        try:
            result = handler(action, ctx)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:  # noqa: BLE001
            # §9А.4: токены не логируются — settoken-действия маскируем.
            safe = action.split(":settoken:")[0] + ":settoken:***" \
                if ":settoken:" in action else action
            logger.exception("gateway action handler failed: %s", safe)
            return ScreenResult(error="handler_failed",
                                text="⚠️ Не получилось выполнить действие. Попробуй ещё раз.")

    # 4. Action that opens a screen (alias or direct screen id).
    screen_id = ACTION_SCREEN_ALIASES.get(action, action)
    if screen_id in SCREEN_REGISTRY:
        try:
            screen = build_screen(screen_id, ctx.user_id)
            if screen is not None:
                return ScreenResult(screen=screen)
        except Exception:  # noqa: BLE001
            logger.exception("screen build failed: %s", screen_id)
            return ScreenResult(error="screen_failed",
                                text="⚠️ Экран сейчас недоступен.")

    # 5. Not migrated yet → polite stub with a Telegram deep-link (§3.4).
    return ScreenResult(
        error="tg_only",
        text="⏳ Этот экран пока доступен только в Telegram.",
        deeplink=_deeplink(),
    )
