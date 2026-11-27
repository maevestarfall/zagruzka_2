"""Account linking for external platforms (ТЗ-1 §4.5).

The owner mints a one-time 6-digit code inside Telegram; sending that
code to the bot in Discord/VK DM links the external account to the TG
owner. Until linked, every external account is a **guest** and goes
through :mod:`bot.feature_gate` (R7-E2).

Storage — only via ``addon_state("gateway", ...)`` inside ``state.json``
(правило §13.4, никаких новых файлов):

* ``pending_codes``: ``[{code, user_id, expires_at, platform}]`` —
  TTL 10 минут, максимум 3 активных, одноразовые.
* ``links``: ``[{platform, external_id, user_id, linked_at, label}]``.
* ``link_attempts``: ``{external_id: {fails, banned_until}}`` — 5
  неверных кодов → бан на 1 час (R7-E3).

Codes are stored as-is: the short TTL + one-shot semantics + rate limit
make that acceptable per spec (§4.5, hash storage is MAY / optional).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from typing import Any

from ..addons import state as addon_state

logger = logging.getLogger("gateway.identity")

ADDON = "gateway"

CODE_TTL_SECONDS = 10 * 60          # 10 minutes
MAX_ACTIVE_CODES = 3
MAX_FAILED_ATTEMPTS = 5
BAN_SECONDS = 60 * 60               # 1 hour

UNLINK_PREFIX = "gateway:unlink:"
#: Telegram callback_data hard limit (§9А.3).
MAX_ACTION_BYTES = 64


# --------------------------------------------------------------------------
# owner notifications (фикс 1.3, ТЗ §4.5)
# --------------------------------------------------------------------------

#: Lazy aiogram Bot holder — registered by the TG surface at first
#: callback (см. wizard_ui) точно так же, как bot_username кэшируется в
#: addon-state; fallback — собрать Bot из BOT_TOKEN при первой отправке.
_bot: Any = None


def set_bot(bot: Any) -> None:
    """Register the running aiogram Bot for owner notifications."""
    global _bot
    if bot is not None:
        _bot = bot


def _get_bot() -> Any:
    """Return the registered Bot, lazily building one from config."""
    global _bot
    if _bot is None:
        try:
            from aiogram import Bot

            from ..config import BOT_TOKEN
            if BOT_TOKEN:
                _bot = Bot(BOT_TOKEN)
        except Exception:  # noqa: BLE001 — нет aiogram/токена — просто молчим
            logger.warning("owner notify: no bot available")
    return _bot


def _owner_id() -> int | None:
    try:
        from ..storage import storage
        owner = storage.get_owner_id()
        return int(owner) if owner else None
    except Exception:  # noqa: BLE001
        logger.warning("owner notify: owner_id lookup failed")
        return None


async def _notify_owner(text: str, reply_markup: Any = None) -> bool:
    """Send a service message to the owner. NEVER breaks the caller."""
    # Этап 6 (R8): дублируем на платформы с включённым broadcast-флагом.
    # Fire-and-forget: доставка в TG не зависит от Discord/VK.
    try:
        from . import broadcast
        broadcast.schedule_broadcast(text)
    except Exception:  # noqa: BLE001 — broadcast не должен ломать notify
        logger.warning("broadcast hook failed", exc_info=True)
    try:
        bot = _get_bot()
        owner = _owner_id()
        if bot is None or owner is None:
            logger.warning("owner notify skipped: bot or owner missing")
            return False
        await bot.send_message(owner, text, reply_markup=reply_markup)
        return True
    except Exception as exc:  # noqa: BLE001 — отправка не должна ломать flow
        logger.warning("owner notify failed: %s", type(exc).__name__)
        return False


def _schedule_notify(text: str, reply_markup: Any = None) -> None:
    """Fire-and-forget owner notification from sync code paths."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("owner notify skipped: no running event loop")
        return
    try:
        task = loop.create_task(_notify_owner(text, reply_markup))
        task.add_done_callback(_consume_task_error)
    except Exception as exc:  # noqa: BLE001 — flow привязки важнее уведомления
        logger.warning("owner notify scheduling failed: %s", type(exc).__name__)


def _consume_task_error(task: "asyncio.Task") -> None:
    with contextlib.suppress(Exception):
        task.exception()


def unlink_action(platform: str, external_id: str) -> str:
    """Build the «Отвязать» callback action, honouring the 64-byte limit.

    When the raw action would overflow, the (platform, external_id) pair
    is stored under a short hash in addon-state and the action becomes
    ``gateway:unlink:ref:<hash>``.
    """
    ext = str(external_id)
    action = f"{UNLINK_PREFIX}{platform}:{ext}"
    if len(action.encode("utf-8")) <= MAX_ACTION_BYTES:
        return action
    digest = hashlib.sha256(f"{platform}:{ext}".encode("utf-8")).hexdigest()[:16]
    refs = dict(addon_state.get(ADDON, "unlink_refs", {}) or {})
    refs[digest] = {"platform": platform, "external_id": ext}
    addon_state.set_(ADDON, "unlink_refs", refs)
    return f"{UNLINK_PREFIX}ref:{digest}"


def _unlink_markup(platform: str, external_id: str) -> Any:
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑 Отвязать",
                        callback_data=unlink_action(platform, external_id),
                    )
                ]
            ]
        )
    except Exception:  # noqa: BLE001 — уведомление уйдёт и без кнопки
        return None


# --------------------------------------------------------------------------
# pending codes
# --------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _active_codes() -> list[dict]:
    """Return non-expired codes, persisting the pruned list if changed."""
    codes = list(addon_state.get(ADDON, "pending_codes", []) or [])
    fresh = [c for c in codes if float(c.get("expires_at", 0)) > _now()]
    if len(fresh) != len(codes):
        addon_state.set_(ADDON, "pending_codes", fresh)
    return fresh


def generate_code(user_id: int, platform: str | None = None) -> str | None:
    """Mint a one-time 6-digit link code for ``user_id``.

    Returns ``None`` when the active-code limit (3) is already reached —
    the UI then tells the owner to wait or reuse an existing code.
    """
    codes = _active_codes()
    if len([c for c in codes if int(c.get("user_id", 0)) == int(user_id)]) >= MAX_ACTIVE_CODES:
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    codes.append(
        {
            "code": code,
            "user_id": int(user_id),
            "expires_at": _now() + CODE_TTL_SECONDS,
            "platform": platform,
        }
    )
    addon_state.set_(ADDON, "pending_codes", codes)
    return code


def active_codes_for(user_id: int) -> list[dict]:
    return [c for c in _active_codes() if int(c.get("user_id", 0)) == int(user_id)]


# --------------------------------------------------------------------------
# rate limiting (anti-bruteforce, R7-E3)
# --------------------------------------------------------------------------

def _attempts() -> dict:
    return dict(addon_state.get(ADDON, "link_attempts", {}) or {})


def is_banned(external_id: str) -> bool:
    entry = _attempts().get(str(external_id)) or {}
    return float(entry.get("banned_until", 0)) > _now()


def _register_failure(external_id: str, platform: str = "") -> bool:
    """Record a wrong code; return True when the ban threshold is hit."""
    attempts = _attempts()
    entry = attempts.get(str(external_id)) or {"fails": 0, "banned_until": 0}
    entry["fails"] = int(entry.get("fails", 0)) + 1
    banned = False
    if entry["fails"] >= MAX_FAILED_ATTEMPTS:
        entry["banned_until"] = _now() + BAN_SECONDS
        entry["fails"] = 0
        banned = True
    attempts[str(external_id)] = entry
    addon_state.set_(ADDON, "link_attempts", attempts)
    if banned:
        # Фикс 1.3 (ТЗ §4.5): владелец узнаёт о попытке перебора кодов.
        _schedule_notify(
            f"⚠️ Попытка перебора кодов привязки с {platform or 'неизвестной платформы'} "
            f"id {external_id}. Аккаунт заблокирован на 1 час."
        )
    return banned


def _clear_failures(external_id: str) -> None:
    attempts = _attempts()
    if str(external_id) in attempts:
        del attempts[str(external_id)]
        addon_state.set_(ADDON, "link_attempts", attempts)


# --------------------------------------------------------------------------
# links
# --------------------------------------------------------------------------

def get_links() -> list[dict]:
    return list(addon_state.get(ADDON, "links", []) or [])


def resolve_user_id(platform: str, external_id: str) -> int | None:
    """Map an external account to the linked TG user id (None = guest)."""
    for link in get_links():
        if link.get("platform") == platform and str(link.get("external_id")) == str(external_id):
            try:
                return int(link["user_id"])
            except Exception:  # noqa: BLE001
                return None
    return None


def verify_code(code: str, platform: str, external_id: str,
                label: str = "") -> tuple[bool, str]:
    """Consume a link code sent from an external platform (DM only!).

    Returns ``(ok, message)``. On success the link is persisted and the
    code burned; on failure the anti-bruteforce counter is bumped.
    The DM-only rule is enforced by the calling adapter (этапы 2–3).
    """
    ext = str(external_id)
    if is_banned(ext):
        return False, "⛔ Слишком много неверных кодов. Попробуй через час."

    codes = _active_codes()
    match = next((c for c in codes if c.get("code") == str(code).strip()), None)
    if match is None:
        banned = _register_failure(ext, platform)
        if banned:
            return False, "⛔ 5 неверных кодов подряд — попытки заблокированы на 1 час."
        return False, "❌ Код не подошёл. Проверь цифры и срок действия (10 минут)."

    wanted = match.get("platform")
    if wanted and wanted != platform:
        return False, f"❌ Этот код выписан для платформы «{wanted}»."

    # burn the code
    codes.remove(match)
    addon_state.set_(ADDON, "pending_codes", codes)
    _clear_failures(ext)

    links = get_links()
    links = [
        l for l in links
        if not (l.get("platform") == platform and str(l.get("external_id")) == ext)
    ]
    links.append(
        {
            "platform": platform,
            "external_id": ext,
            "user_id": int(match["user_id"]),
            "linked_at": _now(),
            "label": label or "",
        }
    )
    addon_state.set_(ADDON, "links", links)
    logger.info("gateway link created: platform=%s external_id=%s", platform, ext)
    # Фикс 1.3 (ТЗ §4.5): уведомить владельца + кнопка «Отвязать».
    _schedule_notify(
        f"🔗 К аккаунту привязан {platform}-аккаунт {label or ext} (id {ext}).",
        _unlink_markup(platform, ext),
    )
    return True, "✅ Аккаунт привязан к владельцу."


def unlink(platform: str, external_id: str) -> bool:
    """Remove a link; the account instantly becomes a guest again (A9)."""
    links = get_links()
    kept = [
        l for l in links
        if not (l.get("platform") == platform and str(l.get("external_id")) == str(external_id))
    ]
    if len(kept) == len(links):
        return False
    addon_state.set_(ADDON, "links", kept)
    logger.info("gateway link removed: platform=%s external_id=%s", platform, external_id)
    return True


# --------------------------------------------------------------------------
# «Отвязать» action (кнопка в уведомлении владельцу, фикс 1.3)
# --------------------------------------------------------------------------

def _resolve_unlink_target(action: str) -> tuple[str, str] | None:
    """``gateway:unlink:…`` → (platform, external_id) or None."""
    rest = action[len(UNLINK_PREFIX):]
    if rest.startswith("ref:"):
        refs = dict(addon_state.get(ADDON, "unlink_refs", {}) or {})
        entry = refs.get(rest[len("ref:"):])
        if not isinstance(entry, dict):
            return None
        platform = str(entry.get("platform") or "")
        ext = str(entry.get("external_id") or "")
    else:
        platform, _, ext = rest.partition(":")
    if not platform or not ext:
        return None
    return platform, ext


def _handle_unlink_action(action: str, ctx: Any) -> Any:
    """Handler for ``gateway:unlink:<platform>:<external_id>``.

    Owner-only gating happens centrally in the dispatcher (все
    ``gateway:*`` действия) — здесь только сама отвязка.
    """
    from .dispatch import ScreenResult

    target = _resolve_unlink_target(action)
    if target is None:
        return ScreenResult(text="⚠️ Не понял, какой аккаунт отвязать.")
    platform, ext = target
    if unlink(platform, ext):
        return ScreenResult(text=f"✅ {platform}-аккаунт (id {ext}) отвязан.")
    return ScreenResult(text="⚠️ Такая привязка не найдена (уже отвязана?).")


def _register_dispatch_actions() -> None:
    """Register the unlink prefix handler (idempotent, import-safe)."""
    try:
        from .dispatch import register_action_prefix
        register_action_prefix(UNLINK_PREFIX, _handle_unlink_action)
    except Exception:  # noqa: BLE001 — регистрация не должна ронять импорт
        logger.exception("failed to register unlink action")


_register_dispatch_actions()
