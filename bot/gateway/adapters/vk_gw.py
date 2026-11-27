"""VK adapter for the Platform Gateway (этап 3 ТЗ, часть B).

The owner stores a VK community token (scope: messages); the adapter
runs Bots Long Poll over plain aiohttp (no new dependencies, §13.3),
answers community DMs, links VK users via one-time codes
(:mod:`bot.gateway.identity`) and renders the same neutral screens as
Telegram through VK inline keyboards.

Integration surface — ONLY the public stage-1 APIs:

* ``addon_state("gateway", ...)`` for the token & status (chmod-600
  state.json, token never logged);
* ``identity.verify_code`` / ``identity.resolve_user_id`` for linking;
* ``dispatch_action`` for every button press (owner gating happens in
  the dispatcher, not here);
* ``register_screen`` / ``register_action`` / ``register_action_prefix``
  for the TG-side control screen ``gateway:vk:live``.

Nothing outside this file is modified (правило №1 handoff-README).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any

from ...addons import state as addon_state
from .. import identity
from ..dispatch import (
    GatewayContext,
    ScreenResult,
    dispatch_action,
    register_action,
    register_action_prefix,
)
from ..menu import MenuButton, MenuScreen, register_screen

logger = logging.getLogger("gateway.vk")

ADDON = "gateway"
PLATFORM = "vk"

VK_API_VERSION = "5.199"
VK_API_BASE = "https://api.vk.com/method"

#: VK inline keyboards allow at most 10 buttons (and 6 rows).
VK_MAX_INLINE_BUTTONS = 10
VK_MAX_ROWS = 6
VK_MAX_PER_ROW = 5

RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
LONGPOLL_WAIT = 25

#: «код 123456» — case-insensitive, tolerant to extra whitespace.
_CODE_RE = re.compile(r"^\s*код\s+(\d{6})\s*$", re.IGNORECASE)
_MENU_RE = re.compile(r"^\s*меню\s*$", re.IGNORECASE)

_NOT_LINKED_HINT = (
    "🔗 Твой VK ещё не привязан к боту.\n"
    "1. В Telegram открой «⚙️ Настройки → 🌐 Платформы → 🔗 Привязать акк».\n"
    "2. Получи 6-значный код.\n"
    "3. Напиши мне сюда: код 123456 (со своими цифрами)."
)

_OVERFLOW_NOTE = "\n\n➡️ Кнопок больше, чем помещается в VK — продолжение в Telegram."


# --------------------------------------------------------------------------
# storage & status (ключи из PART_B_VK.md §1)
# --------------------------------------------------------------------------

def _default_status() -> dict:
    return {"state": "off", "error": None, "group": None, "since": 0.0}


def get_status() -> dict:
    """Current adapter status for the screens (never raises)."""
    try:
        status = addon_state.get(ADDON, "vk_status", None)
    except Exception:  # noqa: BLE001
        logger.exception("vk status read failed")
        status = None
    if not isinstance(status, dict):
        return _default_status()
    merged = _default_status()
    merged.update(status)
    return merged


def _set_status(state: str, error: str | None = None,
                group: str | None = None) -> None:
    prev = get_status()
    status = {
        "state": state,
        "error": error,
        "group": group if group is not None else prev.get("group"),
        "since": time.time(),
    }
    try:
        addon_state.set_(ADDON, "vk_status", status)
    except Exception:  # noqa: BLE001
        logger.exception("vk status write failed")


def get_token() -> str | None:
    try:
        token = addon_state.get(ADDON, "vk_token", None)
    except Exception:  # noqa: BLE001
        logger.exception("vk token read failed")
        return None
    return str(token) if token else None


def set_token(token: str) -> None:
    """Persist the community token. NEVER log the value (правило №4)."""
    token = (token or "").strip()
    addon_state.set_(ADDON, "vk_token", token or None)
    if token:
        _set_status("off", error=None)
        logger.info("vk token saved (masked): %s", _mask(token))
    else:
        _set_status("off", error=None, group=None)


def clear_token() -> None:
    """Disconnect + forget the token (A9-style forget)."""
    addon_state.set_(ADDON, "vk_token", None)
    addon_state.set_(ADDON, "vk_status", _default_status())
    logger.info("vk token cleared")


def _mask(token: str) -> str:
    token = token or ""
    if len(token) <= 8:
        return "…" if token else ""
    return f"{token[:4]}…{token[-4:]}"


# --------------------------------------------------------------------------
# neutral screen → VK inline keyboard (с пагинацией, фикс 1.4 ТЗ §6)
# --------------------------------------------------------------------------

#: Пагинация: ≤ 5 рядов контента на страницу + 1 служебный ряд навигации.
VK_CONTENT_ROWS_PER_PAGE = 5
#: Служебный ряд занимает до 3 кнопок («⬅️», «стр. N/M», «➡️») — бюджет
#: контента на многостраничном экране = 10 − 3 = 7 кнопок.
VK_NAV_BUTTONS = 3

PAGE_ACTION_PREFIX = "gateway:page:"
#: VK payload у callback-кнопки ограничен; держим action ≤ 64 байта —
#: как и TG callback_data (bot.gateway.menu.MAX_ACTION_BYTES).
PAGE_ACTION_MAX_BYTES = 64


def _button_to_vk(btn: MenuButton) -> dict | None:
    if btn.url:
        return {
            "action": {
                "type": "open_link",
                "link": btn.url,
                "label": btn.text[:40],
            }
        }
    if btn.action:
        return {
            "action": {
                "type": "callback",
                "label": btn.text[:40],
                "payload": json.dumps({"action": btn.action},
                                      ensure_ascii=False),
            }
        }
    return None


def _normalize_rows(screen: MenuScreen) -> tuple[list[list[dict]], bool]:
    """Screen rows → VK-ready rows; clip pathological wide rows (>5).

    Returns ``(rows, truncated)`` where ``truncated=True`` ONLY for the
    pathology «один ряд шире 5 кнопок» — обычное переполнение экрана
    теперь решается пагинацией, а не усечением.
    """
    rows: list[list[dict]] = []
    truncated = False
    for row in screen.rows:
        vk_row: list[dict] = []
        for btn in row:
            rendered = _button_to_vk(btn)
            if rendered is None:
                continue
            if len(vk_row) >= VK_MAX_PER_ROW:
                truncated = True  # ряд шире лимита VK — кнопка теряется
                continue
            vk_row.append(rendered)
        if vk_row:
            rows.append(vk_row)
    return rows, truncated


def _paginate_rows(rows: list[list[dict]]) -> list[list[list[dict]]]:
    """Split VK rows into pages honouring BOTH limits (rows & buttons).

    Single page when everything fits into VK hard limits (≤ 6 rows,
    ≤ 10 buttons). Otherwise: ≤ 5 content rows и ≤ 7 content buttons
    per page — the 6th row / remaining 3 buttons are reserved for the
    «⬅️ | стр. N/M | ➡️» nav row.
    """
    total_buttons = sum(len(r) for r in rows)
    if len(rows) <= VK_MAX_ROWS and total_buttons <= VK_MAX_INLINE_BUTTONS:
        return [rows] if rows else [[]]

    budget = VK_MAX_INLINE_BUTTONS - VK_NAV_BUTTONS
    pages: list[list[list[dict]]] = []
    current: list[list[dict]] = []
    used = 0
    for row in rows:
        if current and (
            len(current) >= VK_CONTENT_ROWS_PER_PAGE
            or used + len(row) > budget
        ):
            pages.append(current)
            current, used = [], 0
        current.append(row)
        used += len(row)
    if current:
        pages.append(current)
    return pages or [[]]


def _page_action(screen_id: str, page: int) -> str:
    """``gateway:page:<screen_id>:<n>`` или короткая форма при > 64 байт.

    Короткая форма ``gateway:page:<n>`` опирается на сохранённый
    per-peer screen_id (addon-state ``vk_page:<peer_id>``).
    """
    action = f"{PAGE_ACTION_PREFIX}{screen_id}:{page}"
    if len(action.encode("utf-8")) > PAGE_ACTION_MAX_BYTES:
        return f"{PAGE_ACTION_PREFIX}{page}"
    return action


def _nav_row(screen_id: str, page: int, total_pages: int) -> list[dict]:
    row: list[dict] = []
    if page > 0:
        row.append(_button_to_vk(
            MenuButton(text="⬅️ Назад", action=_page_action(screen_id, page - 1))
        ))
    row.append(_button_to_vk(
        MenuButton(text=f"стр. {page + 1}/{total_pages}",
                   action=_page_action(screen_id, page))
    ))
    if page < total_pages - 1:
        row.append(_button_to_vk(
            MenuButton(text="➡️ Вперёд", action=_page_action(screen_id, page + 1))
        ))
    return [b for b in row if b is not None]


def screen_to_vk_keyboard(screen: MenuScreen,
                          page: int = 0) -> tuple[dict, bool]:
    """Render a neutral screen into VK inline-keyboard JSON (paged).

    Фикс 1.4: вместо усечения после 10 кнопок экран разбивается на
    страницы по ≤ 5 рядов контента; при > 1 страницы добавляется
    служебный ряд «⬅️ Назад | стр. N/M | ➡️ Вперёд» с callback-действиями
    ``gateway:page:<screen_id>:<n>``. Возвращает ``(keyboard, truncated)``
    где ``truncated=True`` остаётся только для патологии — один ряд шире
    5 кнопок (лишние кнопки ряда VK физически не покажет).
    """
    rows, truncated = _normalize_rows(screen)
    pages = _paginate_rows(rows)
    total_pages = len(pages)
    page = max(0, min(int(page), total_pages - 1))

    buttons = list(pages[page])
    if total_pages > 1:
        buttons.append(_nav_row(screen.screen_id, page, total_pages))
    return {"inline": True, "buttons": buttons}, truncated


def _screen_text(screen: MenuScreen, truncated: bool) -> str:
    parts = [screen.title]
    if screen.body:
        parts.append("")
        parts.append(screen.body)
    text = "\n".join(parts)
    if truncated:
        text += _OVERFLOW_NOTE
    return text


# --------------------------------------------------------------------------
# per-peer page state (фикс 1.4: addon-state «gateway», ключ vk_page:<peer>)
# --------------------------------------------------------------------------

def _page_state_key(peer_id: int) -> str:
    return f"vk_page:{peer_id}"


def _get_page_state(peer_id: int) -> dict:
    try:
        state = addon_state.get(ADDON, _page_state_key(peer_id), None)
    except Exception:  # noqa: BLE001
        logger.exception("vk page state read failed")
        state = None
    return state if isinstance(state, dict) else {}


def _set_page_state(peer_id: int, screen_id: str, page: int) -> None:
    try:
        addon_state.set_(ADDON, _page_state_key(peer_id),
                         {"screen_id": screen_id, "page": int(page)})
    except Exception:  # noqa: BLE001
        logger.exception("vk page state write failed")


def _parse_page_action(action: str, peer_id: int) -> tuple[str | None, int]:
    """``gateway:page:...`` → ``(screen_id | None, page)``.

    Полная форма: ``gateway:page:<screen_id>:<n>`` (screen_id может сам
    содержать «:», номер страницы — ПОСЛЕДНИЙ сегмент). Короткая форма
    ``gateway:page:<n>`` — screen_id берётся из сохранённого per-peer
    состояния (см. :func:`_page_action`).
    """
    rest = action[len(PAGE_ACTION_PREFIX):]
    if ":" in rest:
        screen_id, _, page_str = rest.rpartition(":")
    else:
        screen_id, page_str = "", rest
    try:
        page = int(page_str)
    except (TypeError, ValueError):
        return None, 0
    if not screen_id:
        screen_id = str(_get_page_state(peer_id).get("screen_id") or "")
    return (screen_id or None), max(0, page)


# --------------------------------------------------------------------------
# incoming VK messages / button presses
# --------------------------------------------------------------------------

def parse_code_command(text: str) -> str | None:
    """Extract the 6-digit code from «код XXXXXX» (case-insensitive)."""
    match = _CODE_RE.match(text or "")
    return match.group(1) if match else None


def is_menu_command(text: str) -> bool:
    return bool(_MENU_RE.match(text or ""))


def _extract_action(payload: Any) -> str | None:
    """Pull ``{"action": ...}`` out of a message/event payload."""
    if payload is None:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(payload, dict):
        action = payload.get("action")
        if isinstance(action, str) and action:
            return action
    return None


async def _reply_for_action(action: str, from_id: int,
                            label: str = "") -> tuple[str, dict | None]:
    """dispatch_action + result mapping (как в части A) → (text, keyboard).

    Фикс 1.4 — пагинация: служебное платформенное действие
    ``gateway:page:<screen_id>:<n>`` (или короткое ``gateway:page:<n>``)
    перехватывается ЗДЕСЬ, до общего dispatch (иначе owner-gate на
    ``gateway:*`` отрезал бы гостей от их же меню). Экран страницы
    перерендеривается ПОВТОРНЫМ вызовом ``dispatch_action(<screen_id>)``
    — в реестре dispatch «действие-открыть-экран» и есть его screen_id
    (шаг 4 диспетчера), т.е. никакого второго пути рендера не появляется.
    Любой ДРУГОЙ экран сбрасывает страницу на 0 (state ``vk_page:<peer>``).
    """
    tg_user_id = identity.resolve_user_id(PLATFORM, str(from_id))
    ctx = GatewayContext(
        platform=PLATFORM,
        external_id=str(from_id),
        user_id=tg_user_id,
    )

    page = 0
    if action.startswith(PAGE_ACTION_PREFIX):
        screen_id, page = _parse_page_action(action, from_id)
        if not screen_id:
            return "⚠️ Экран устарел — открой меню заново («меню»).", None
        action = screen_id  # re-render the stored screen at the new page

    result: ScreenResult = await dispatch_action(action, ctx)

    if result.screen is not None:
        keyboard, truncated = screen_to_vk_keyboard(result.screen, page=page)
        _set_page_state(from_id, result.screen.screen_id, page)
        text = _screen_text(result.screen, truncated)
        if result.text:
            text = f"{result.text}\n\n{text}"
        return text, keyboard

    if result.error == "tg_only":
        text = result.text or "⏳ Этот экран пока доступен только в Telegram."
        if result.deeplink:
            text += f"\n{result.deeplink}"
        return text, None

    if result.error == "forbidden":
        return result.text or "⛔ Доступ запрещён.", None

    if result.text:
        return result.text, None

    return "⚠️ Не получилось выполнить действие. Попробуй ещё раз.", None


_BRAIN_ERROR_TEXT = (
    "⚠️ Мозг сейчас недоступен. Попробуй ещё раз чуть позже."
)


async def handle_text_message(from_id: int, text: str,
                              label: str = "") -> tuple[str, dict | None]:
    """Handle a community DM text: «код …», «меню» или чат с мозгом.

    Фикс 2.4 (ТЗ R3): свободный текст (не «код …», не «меню») от
    ПРИВЯЗАННОГО пользователя уходит в тот же пайплайн диалога, что и
    обычное сообщение в TG (:func:`bot.gateway.brain.ask_brain`), ответ —
    обратно в VK. Непривязанный получает подсказку про привязку. Ошибки
    мозга → вежливый текст; в лог — БЕЗ текста пользователя.

    Pure with respect to networking — returns ``(text, keyboard | None)``
    so it is unit-testable without VK (критерий тестов №5).
    """
    code = parse_code_command(text)
    if code is not None:
        ok, message = identity.verify_code(
            code, PLATFORM, str(from_id), label=label or str(from_id)
        )
        return message, None

    if is_menu_command(text):
        tg_user_id = identity.resolve_user_id(PLATFORM, str(from_id))
        if tg_user_id is None:
            return _NOT_LINKED_HINT, None
        return await _reply_for_action("main:menu", from_id, label=label)

    tg_user_id = identity.resolve_user_id(PLATFORM, str(from_id))
    if tg_user_id is None:
        return _NOT_LINKED_HINT, None

    from .. import brain  # lazy: тяжёлые зависимости мозга — по требованию

    try:
        answer = await brain.ask_brain(tg_user_id, text or "")
    except Exception:  # noqa: BLE001 — текст пользователя НЕ логируем
        logger.exception("vk brain reply failed (user_id=%s)", tg_user_id)
        return _BRAIN_ERROR_TEXT, None
    return (answer or "…"), None


# --------------------------------------------------------------------------
# VK API client (Bots Long Poll, plain aiohttp — lazy import in start())
# --------------------------------------------------------------------------

_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def _api_call(session: Any, token: str, method: str,
                    **params: Any) -> dict:
    """One VK API call; raises ``VkApiError`` on an ``error`` payload."""
    payload = dict(params)
    payload["access_token"] = token
    payload["v"] = VK_API_VERSION
    async with session.post(f"{VK_API_BASE}/{method}", data=payload) as resp:
        data = await resp.json(content_type=None)
    if isinstance(data, dict) and "error" in data:
        err = data["error"] or {}
        raise VkApiError(
            int(err.get("error_code", 0)),
            str(err.get("error_msg", "unknown VK error")),
        )
    return data.get("response") if isinstance(data, dict) else data


class VkApiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"VK API error {code}: {message}")
        self.code = code
        self.message = message

    @property
    def is_auth(self) -> bool:
        # 5 = invalid token, 27 = group token invalid, 15 = access denied
        return self.code in (5, 15, 27)


async def _resolve_group(session: Any, token: str) -> tuple[int, str]:
    """Validate the token and return ``(group_id, group_name)``."""
    response = await _api_call(session, token, "groups.getById")
    groups = response.get("groups") if isinstance(response, dict) else response
    if not groups:
        raise VkApiError(27, "token is not a community token")
    group = groups[0]
    return int(group["id"]), str(group.get("name") or group["id"])


async def _send_message(session: Any, token: str, peer_id: int,
                        text: str, keyboard: dict | None = None) -> None:
    params: dict[str, Any] = {
        "peer_id": peer_id,
        "message": text[:4000],
        "random_id": random.randint(1, 2 ** 31 - 1),
    }
    if keyboard is not None:
        params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    try:
        await _api_call(session, token, "messages.send", **params)
    except VkApiError:
        logger.exception("vk messages.send failed")


async def _answer_event(session: Any, token: str, event: dict) -> None:
    """Ack a callback button press so VK stops the spinner."""
    try:
        await _api_call(
            session,
            token,
            "messages.sendMessageEventAnswer",
            event_id=str(event.get("event_id", "")),
            user_id=int(event.get("user_id", 0)),
            peer_id=int(event.get("peer_id", 0)),
        )
    except Exception:  # noqa: BLE001
        logger.debug("vk event answer failed", exc_info=True)


def _label_from_message(msg: dict) -> str:
    return str(msg.get("from_id", ""))


async def _get_user_name(session: Any, token: str, from_id: int) -> str:
    """Best-effort VK user name for ``identity.verify_code(label=имя)``.

    Falls back to the numeric id when ``users.get`` fails — linking must
    never break because of a cosmetic lookup.
    """
    try:
        response = await _api_call(session, token, "users.get",
                                   user_ids=from_id)
        if isinstance(response, list) and response:
            user = response[0]
            name = " ".join(
                part for part in
                (user.get("first_name"), user.get("last_name")) if part
            ).strip()
            if name:
                return name
    except Exception:  # noqa: BLE001
        logger.debug("vk users.get failed", exc_info=True)
    return str(from_id)


async def send_text(external_id: str, text: str) -> bool:
    """Личное сообщение через сообщество (broadcast R8 / медиашина).

    Открывает собственную aiohttp-сессию: вызывается вне long-poll
    цикла (из broadcast/identity). Возвращает False вместо исключения.
    """
    token = get_token()
    if not token:
        return False
    try:
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            await _api_call(
                session, token, "messages.send",
                peer_id=int(external_id),
                message=text[:4000],
                random_id=random.randint(1, 2 ** 31 - 1),
            )
        return True
    except Exception as exc:  # noqa: BLE001 — токен/текст не логируем
        logger.warning("vk send_text failed: %s", type(exc).__name__)
        return False


async def fetch_media_bytes(origin: dict, max_bytes: int) -> bytes | None:
    """Резолвер медиашины (ТЗ §6А.6): свежий URL через messages API.

    Ссылки из Long Poll протухают быстро, поэтому в ``origin`` лежат
    только ``peer_id + cmid``; свежий URL добывается прямо перед
    скачиванием. ``None`` = сообщение удалено (артефакт истёк).
    """
    token = get_token()
    if not token:
        return None
    try:
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            response = await _api_call(
                session, token, "messages.getByConversationMessageId",
                peer_id=int(origin["peer_id"]),
                conversation_message_ids=int(origin["cmid"]),
                extended=1,
            )
            items = (response or {}).get("items") or []
            if not items:
                return None
            url = None
            for att in items[0].get("attachments") or []:
                if att.get("type") == "photo":
                    sizes = (att.get("photo") or {}).get("sizes") or []
                    if sizes:
                        best = max(
                            sizes,
                            key=lambda s: (int(s.get("width", 0) or 0)
                                           * int(s.get("height", 0) or 0)),
                        )
                        url = best.get("url")
                        break
            if not url:
                return None
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        if len(data) > max_bytes:
            raise ValueError("file too big")
        return data
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("vk media fetch failed: %s", type(exc).__name__)
        return None


async def _handle_incoming_media(session: Any, token: str, msg: dict,
                                 from_id: int, peer_id: int) -> bool:
    """Этап 5 (R11): фото в личке сообщества → артефакт медиашины."""
    try:
        from .. import media_bus
    except Exception:  # noqa: BLE001
        return False
    if not media_bus.media_enabled():
        return False
    has_photo = any(
        (att or {}).get("type") == "photo"
        for att in (msg.get("attachments") or [])
    )
    if not has_photo:
        return False
    tg_user_id = identity.resolve_user_id(PLATFORM, str(from_id))
    if tg_user_id is None:
        await _send_message(session, token, peer_id, _NOT_LINKED_HINT)
        return True
    cmid = int(msg.get("conversation_message_id", 0) or 0)
    artifact = media_bus.register_artifact(
        kind="photo",
        origin={"platform": "vk", "peer_id": peer_id, "cmid": cmid},
        step="vk:upload",
    )
    text, keyboard = await _reply_for_action(
        f"gateway:media:art:{artifact['id']}", from_id,
    )
    await _send_message(session, token, peer_id, text, keyboard)
    return True


async def _handle_update(session: Any, token: str, update: dict) -> None:
    utype = update.get("type")
    obj = update.get("object") or {}

    if utype == "message_new":
        msg = obj.get("message") or obj  # both 5.199 and legacy shapes
        from_id = int(msg.get("from_id", 0))
        peer_id = int(msg.get("peer_id", from_id))
        if from_id <= 0 or peer_id != from_id:
            return  # community DMs only (verify_code: DM-only rule)
        action = _extract_action(msg.get("payload"))
        if action:
            text, keyboard = await _reply_for_action(
                action, from_id, label=_label_from_message(msg)
            )
        else:
            # Этап 5: фото → медиашина (если включена).
            if await _handle_incoming_media(session, token, msg,
                                            from_id, peer_id):
                return
            msg_text = str(msg.get("text", ""))
            label = _label_from_message(msg)
            if parse_code_command(msg_text) is not None:
                # ТЗ §3: verify_code(..., label=имя) — резолвим имя из VK.
                label = await _get_user_name(session, token, from_id)
            text, keyboard = await handle_text_message(
                from_id, msg_text, label=label,
            )
        await _send_message(session, token, peer_id, text, keyboard)

    elif utype == "message_event":
        from_id = int(obj.get("user_id", 0))
        peer_id = int(obj.get("peer_id", from_id))
        await _answer_event(session, token, obj)
        action = _extract_action(obj.get("payload"))
        if not action or from_id <= 0:
            return
        text, keyboard = await _reply_for_action(action, from_id)
        await _send_message(session, token, peer_id, text, keyboard)


async def _longpoll_loop(stop: asyncio.Event) -> None:
    """Bots Long Poll with exponential-backoff reconnect (1с → 60с)."""
    import aiohttp  # lazy: keeps module importable on bare Python (§13.3)

    delay = RECONNECT_MIN_DELAY
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=LONGPOLL_WAIT + 15)
    ) as session:
        while not stop.is_set():
            token = get_token()
            if not token:
                _set_status("off", error=None)
                return
            try:
                group_id, group_name = await _resolve_group(session, token)
                lp = await _api_call(
                    session, token, "groups.getLongPollServer",
                    group_id=group_id,
                )
                server, key, ts = lp["server"], lp["key"], lp["ts"]
                _set_status("online", error=None, group=group_name)
                delay = RECONNECT_MIN_DELAY

                while not stop.is_set():
                    async with session.get(
                        server,
                        params={"act": "a_check", "key": key,
                                "ts": ts, "wait": LONGPOLL_WAIT},
                    ) as resp:
                        data = await resp.json(content_type=None)
                    failed = data.get("failed")
                    if failed == 1:
                        ts = data.get("ts", ts)
                        continue
                    if failed in (2, 3):
                        break  # re-request the long-poll server
                    if failed is not None:
                        break
                    ts = data.get("ts", ts)
                    for update in data.get("updates", []) or []:
                        try:
                            await _handle_update(session, token, update)
                        except Exception:  # noqa: BLE001
                            logger.exception("vk update handling failed")

            except VkApiError as exc:
                if exc.is_auth:
                    # Invalid token → error status, DO NOT hammer the API.
                    _set_status("error", error="Невалидный токен VK.")
                    logger.warning("vk auth failed, adapter stopped")
                    return
                _set_status("error", error=exc.message)
                logger.warning("vk api error: %s", exc.message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _set_status("error", error=str(exc)[:200])
                logger.exception("vk long poll crashed, reconnecting")

            if stop.is_set():
                return
            _set_status("connecting", error=get_status().get("error"))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return  # stop requested during backoff
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, RECONNECT_MAX_DELAY)


async def start() -> None:
    """Start the Long Poll client if a token is configured."""
    global _task, _stop_event
    if _task is not None and not _task.done():
        return
    token = get_token()
    if not token:
        _set_status("off", error="Токен VK не задан.")
        return
    _set_status("connecting", error=None)
    _stop_event = asyncio.Event()
    _task = asyncio.get_running_loop().create_task(_longpoll_loop(_stop_event))
    logger.info("vk adapter started")


async def stop() -> None:
    """Graceful shutdown of the Long Poll client."""
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
    _stop_event = None
    if get_status().get("state") != "error":
        _set_status("off", error=None)
    logger.info("vk adapter stopped")


def is_running() -> bool:
    return _task is not None and not _task.done()


# --------------------------------------------------------------------------
# TG-side control screen «gateway:vk:live» + actions (§4 задания)
# --------------------------------------------------------------------------

_STATE_ICONS = {
    "off": "⚪️ выключен",
    "connecting": "🟡 подключается…",
    "online": "✅ онлайн",
    "error": "🔴 ошибка",
}


def build_vk_live_screen(user_id: int | None) -> MenuScreen:
    status = get_status()
    token = get_token()
    state = str(status.get("state", "off"))

    lines = [f"Статус: {_STATE_ICONS.get(state, state)}"]
    if status.get("group"):
        lines.append(f"Сообщество: {status['group']}")
    if token:
        lines.append(f"Токен: {_mask(token)}")
    else:
        lines.append("Токен: не задан (ввести можно с сайта — VISION → Меню).")
    if status.get("error"):
        lines.append(f"Ошибка: {status['error']}")
    if state == "online" and status.get("since"):
        mins = max(0, int((time.time() - float(status["since"])) // 60))
        lines.append(f"Аптайм: ~{mins} мин.")
    lines.append("")
    lines.append("Пользователи VK пишут сообществу «меню» или «код 123456».")

    rows: list[list[MenuButton]] = []
    if state == "online" or state == "connecting":
        rows.append([MenuButton(text="⏹ Остановить", action="gateway:vk:stop")])
    elif token:
        rows.append([MenuButton(text="▶️ Запустить", action="gateway:vk:start")])
    if token:
        rows.append([MenuButton(text="🗑 Забыть токен", action="gateway:vk:forget")])
    rows.append([MenuButton(text="🔄 Обновить", action="gateway:vk:live")])
    rows.append([MenuButton(text="↩️ Назад", action="gateway:home")])

    return MenuScreen(
        screen_id="gateway:vk:live",
        title="🔵 VK — управление",
        body="\n".join(lines),
        rows=rows,
        parent="gateway:home",
    )


async def _act_start(action: str, ctx: GatewayContext) -> ScreenResult:
    if not get_token():
        return ScreenResult(
            screen=build_vk_live_screen(ctx.user_id),
            text="⚠️ Сначала задай токен сообщества VK.",
        )
    await start()
    return ScreenResult(screen=build_vk_live_screen(ctx.user_id))


async def _act_stop(action: str, ctx: GatewayContext) -> ScreenResult:
    await stop()
    return ScreenResult(screen=build_vk_live_screen(ctx.user_id))


async def _act_forget(action: str, ctx: GatewayContext) -> ScreenResult:
    await stop()
    clear_token()
    return ScreenResult(screen=build_vk_live_screen(ctx.user_id))


_SETTOKEN_PREFIX = "gateway:vk:settoken:"


async def _act_settoken(action: str, ctx: GatewayContext) -> ScreenResult:
    """``gateway:vk:settoken:<token>`` — token input from the site.

    Owner-only enforcement уже сделан в dispatch (все ``gateway:*``,
    кроме link, проверяются на владельца до вызова обработчика).
    """
    token = action[len(_SETTOKEN_PREFIX):].strip()
    if not token:
        return ScreenResult(error="bad_action", text="⚠️ Пустой токен.")
    set_token(token)
    return ScreenResult(
        screen=build_vk_live_screen(ctx.user_id),
        text="✅ Токен VK сохранён. Нажми «▶️ Запустить».",
    )


def _register_all() -> None:
    """Wire the adapter into the stage-1 registries (screen + actions).

    Заглушка ``gateway:vk`` из этапа 1 не трогается — принимающая
    сторона сама переключит её на ``gateway:vk:live``.
    """
    register_screen("gateway:vk:live", build_vk_live_screen)
    register_action("gateway:vk:start", _act_start)
    register_action("gateway:vk:stop", _act_stop)
    register_action("gateway:vk:forget", _act_forget)
    register_action_prefix(_SETTOKEN_PREFIX, _act_settoken)


_register_all()
