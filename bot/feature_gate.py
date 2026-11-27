"""Per-feature guest gating — enforcement of «Функции» / «Что они видят».

:mod:`bot.private_mode` already stores, per feature key, two independent
owner toggles:

  * USE  («Функции»)        → :func:`bot.access.guest_can_use`
  * SEE  («Что они видят»)  → :func:`bot.access.guest_can_see`

Those helpers were fully implemented and unit-tested, but *nothing consulted
them*: feature handlers gated only on the coarse :func:`bot.access.can_use` /
``can_admin``, so the per-feature toggles were cosmetic. This module is the
missing wiring:

  * :data:`FEATURE_CALLBACK_PREFIXES` / :data:`FEATURE_COMMANDS` map every
    feature key to the ``callback_data`` prefixes and slash-commands that
    *invoke* it. :func:`build_feature_use_gate_middleware` resolves the feature
    for an incoming guest update and blocks it when ``guest_can_use`` is False.
  * :data:`MENU_BUTTON_FEATURE` maps a menu button's ``callback_data`` to a
    feature key so :func:`filter_menu_rows` can drop buttons a guest may not
    *see*.

Design guarantees (all verified by tests):

  * Owner / co-owner are never gated — ``guest_can_use`` / ``guest_can_see``
    short-circuit to True for them, and the middleware double-checks
    ``is_owner_or_co_owner`` first.
  * When the fine-grained layer is OFF (``pm_is_enabled() is False``) the
    middleware and the menu filter are no-ops, so a non-private bot behaves
    exactly as before.
  * Updates that do not map to a known feature (plain chat, admin-only
    screens, settings plumbing) are passed straight through — the payment
    gate, not this layer, decides whether a guest may chat at all.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Iterable

from .private_mode import FEATURE_KEYS

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# feature → invocation surface (callback_data prefixes, startswith match)
# --------------------------------------------------------------------------
# Keys MUST be a subset of bot.private_mode.FEATURE_KEYS (asserted below).
FEATURE_CALLBACK_PREFIXES: dict[str, tuple[str, ...]] = {
    "coding":      ("coder", "main:coder", "main:vibe", "main:regen"),
    "analysis":    ("analyzer", "main:analyzer"),
    "github":      ("mr:", "main:github", "main:masterroom", "gh:"),
    "terminal":    ("main:terminal", "term:"),
    "media":       ("media_toggle", "main:media_toggle", "photogen",
                    "openreels", "viddyn", "main:download"),
    "helpzavr":    ("hz:", "main:helpzavr"),
    "pretty_text": ("pretty:", "main:pretty"),
    "editor":      ("editor_agent", "main:editor"),
    "cleanup":     ("dump:", "main:dump"),
    "debates":     ("debates:", "main:debates"),
    "osint":       ("osint", "main:osint"),
    "mailbox":     ("mb:", "main:mailbox"),
    "memory":      ("memory:", "main:memory"),
    "tts":         ("tts:",),
    "promptlab":   ("pl:", "psrc:", "main:prompt_sources"),
}

# feature → slash-commands (without the leading «/») that invoke it.
FEATURE_COMMANDS: dict[str, tuple[str, ...]] = {
    "promptlab": ("prompt", "promt", "промт", "промпт"),
}

# menu button callback_data → feature key (drives «что они видят» hiding).
MENU_BUTTON_FEATURE: dict[str, str] = {
    "main:helpzavr": "helpzavr",
    "main:pretty": "pretty_text",
    "main:mailbox": "mailbox",
    "main:media_toggle": "media",
    "main:osint": "osint",
    "main:dump": "cleanup",
    "main:debates": "debates",
    "main:analyzer": "analysis",
    "main:github": "github",
    "main:masterroom": "github",
    "main:coder": "coding",
    "main:vibe": "coding",
    "main:regen": "coding",
    "main:terminal": "terminal",
    "main:editor": "editor",
    "main:memory": "memory",
    "main:download": "media",
    "main:prompt_sources": "promptlab",
    "tts:menu": "tts",
}

# Fail fast if a registry key drifts away from the private_mode catalogue.
_unknown = (
    set(FEATURE_CALLBACK_PREFIXES) - set(FEATURE_KEYS)
    | set(FEATURE_COMMANDS) - set(FEATURE_KEYS)
    | set(MENU_BUTTON_FEATURE.values()) - set(FEATURE_KEYS)
)
if _unknown:  # pragma: no cover - guards against future typos
    raise RuntimeError(f"feature_gate references unknown feature keys: {sorted(_unknown)}")


def resolve_feature(callback_data: str | None, command_text: str | None) -> str | None:
    """Return the feature key an update invokes, or ``None`` if it is not a
    gated feature (plain chat, admin screen, settings plumbing, …).

    Commands win over callbacks (a feature is usually triggered by exactly
    one of them). For callbacks the *longest* matching prefix wins so that a
    specific ``main:coder`` is preferred over any shorter accidental match.
    """
    if command_text:
        head = command_text.strip().split(maxsplit=1)[0] if command_text.strip() else ""
        if head.startswith("/"):
            cmd = head[1:].split("@", 1)[0].lower()
            for key, cmds in FEATURE_COMMANDS.items():
                if cmd in cmds:
                    return key
    if callback_data:
        best: str | None = None
        best_len = -1
        for key, prefixes in FEATURE_CALLBACK_PREFIXES.items():
            for p in prefixes:
                if callback_data.startswith(p) and len(p) > best_len:
                    best, best_len = key, len(p)
        return best
    return None


def build_feature_use_gate_middleware():
    """aiogram outer middleware enforcing the «Функции» (USE) toggles.

    Register it AFTER the payment gate: the payment gate first decides whether
    a guest may interact at all (welcome / limit), and this layer then blocks
    individual features the owner has not switched on for guests.
    """
    from .storage import storage

    async def _gate(handler, event, data):
        try:
            cq = getattr(event, "callback_query", None)
            msg = getattr(event, "message", None)

            uid = None
            cb = None
            cmd = None
            if cq is not None and getattr(cq, "from_user", None) is not None:
                uid = cq.from_user.id
                cb = cq.data or ""
            elif msg is not None and getattr(msg, "from_user", None) is not None:
                uid = msg.from_user.id
                cmd = msg.text or ""

            if uid is None:
                return await handler(event, data)
            if not storage.pm_is_enabled():
                return await handler(event, data)
            if storage.is_owner_or_co_owner(uid):
                return await handler(event, data)

            feature = resolve_feature(cb, cmd)
            if feature is None:
                # Not a gated feature → let the payment gate / chat handle it.
                return await handler(event, data)

            from .access import guest_can_use

            if guest_can_use(uid, feature):
                return await handler(event, data)

            # Blocked: acknowledge so the client stops the spinner.
            with contextlib.suppress(Exception):
                if cq is not None:
                    await cq.answer(
                        "⛔ Эта функция недоступна. Обратись к владельцу бота.",
                        show_alert=True,
                    )
                elif msg is not None:
                    await msg.answer(
                        "⛔ Эта функция недоступна в приватном режиме."
                    )
            return None
        except Exception:  # never let the gate break message handling
            logger.exception("feature use-gate failed open")
            return await handler(event, data)

    return _gate


def filter_menu_rows(rows: Iterable[list], user_id: int | None) -> list[list]:
    """Drop menu buttons a guest may not *see* («Что они видят»).

    A no-op (returns the rows unchanged) when the fine-grained layer is off or
    the caller is the owner / a co-owner. Rows are returned as a fresh list so
    callers can keep using the result directly; empty rows are pruned.
    """
    rows = list(rows)
    try:
        from .storage import storage

        if not storage.pm_is_enabled() or storage.is_owner_or_co_owner(user_id):
            return rows

        from .access import guest_can_see

        out: list[list] = []
        for row in rows:
            kept = []
            for btn in row:
                cb = getattr(btn, "callback_data", None)
                feature = MENU_BUTTON_FEATURE.get(cb) if cb else None
                if feature is not None and not guest_can_see(user_id, feature):
                    continue
                kept.append(btn)
            if kept:
                out.append(kept)
        return out
    except Exception:  # never let menu filtering break a menu render
        logger.exception("menu see-filter failed open")
        return rows
