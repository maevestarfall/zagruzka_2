"""Часть A — адаптер Discord (этап 2 ТЗ «Кроссплатформенный шлюз»).

Модуль полностью самодостаточен и подключается ТОЛЬКО через публичные
API этапа 1 (см. docs/handoff/README.md):

* экран ``gateway:discord:live`` — через :func:`bot.gateway.menu.register_screen`;
* действия ``gateway:discord:start|stop|forget`` и префикс
  ``gateway:discord:settoken:`` — через :mod:`bot.gateway.dispatch`;
* привязка аккаунтов — через :mod:`bot.gateway.identity`;
* хранение токена/статуса — через ``bot.addons.state`` (ветка
  ``gateway`` в state.json, chmod 600). Токен НИКОГДА не логируется.

``discord.py`` — ленивая зависимость: импортируется только внутри
:func:`start`. Отсутствие библиотеки не роняет импорт модуля (A10) —
модуль обязан работать на голом Python + aiogram + aiohttp.

Команды в Discord (префиксные — стабильнее slash-синка):

* ``!lilush link <код>``  — привязка аккаунта одноразовым кодом (DM only);
* ``!lilush menu``        — текущий экран меню с кнопками (components),
  ``custom_id`` кнопки == action; клик → ``dispatch_action``.
"""

from __future__ import annotations

import asyncio
import logging
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
from ..menu import MenuButton, MenuScreen, build_screen, register_screen

logger = logging.getLogger("gateway.discord")

ADDON = "gateway"
TOKEN_KEY = "discord_token"
STATUS_KEY = "discord_status"
BOT_TAG_KEY = "discord_bot_tag"

PLATFORM = "discord"
COMMAND_PREFIX = "!lilush"
SETTOKEN_PREFIX = "gateway:discord:settoken:"
ENTER_TOKEN_ACTION = "gateway:discord:enter_token"

#: ТЗ §4.4 п.4 — токен проверяется ДО сохранения этим запросом.
DISCORD_API_ME = "https://discord.com/api/v10/users/@me"
VALIDATE_TIMEOUT = 10.0

INVALID_TOKEN_MSG = (
    "Токен не принят Discord. Проверь, что скопировал Bot Token "
    "(не Client Secret)."
)

#: Discord component limits: 5 rows × 5 buttons.
MAX_BUTTONS_PER_ROW = 5
MAX_ROWS = 5
MAX_BUTTONS_TOTAL = MAX_BUTTONS_PER_ROW * MAX_ROWS

#: reconnect backoff (сек): 1 → 2 → 4 → … → 60 (потолок).
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0

DEFAULT_SCREEN_ID = "main"

LINK_HINT = (
    "🔗 Твой Discord ещё не привязан к Лилуш.\n"
    "1. В Telegram открой «⚙️ Настройки → 🌐 Платформы → 🔗 Привязать акк» "
    "и получи 6-значный код.\n"
    f"2. Пришли мне в ЛИЧНЫЕ сообщения: `{COMMAND_PREFIX} link <код>` "
    "(или `/link <код>`).\n"
    "После этого команда меню станет доступна."
)

OVERFLOW_NOTE = "…кнопок больше, чем помещается в Discord — продолжение в Telegram."

# --------------------------------------------------------------------------
# runtime state (client task lives here, not in state.json)
# --------------------------------------------------------------------------

_client: Any = None            # discord.Client | None
_client_task: asyncio.Task | None = None

#: SwapFace ТЗ §3: публичные хуки слушателей каналов. Ключ — channel_id,
#: значение — список async-колбэков ``cb(message, event="create"|"edit")``.
_message_listeners: dict[int, list[Any]] = {}


def register_message_listener(channel_id: int, cb) -> None:  # noqa: ANN001
    """Подписать колбэк на create/edit сообщения канала (SwapFace §8)."""
    _message_listeners.setdefault(int(channel_id), []).append(cb)


def unregister_message_listener(channel_id: int, cb) -> None:  # noqa: ANN001
    """Снять подписку; отсутствие подписки — не ошибка."""
    listeners = _message_listeners.get(int(channel_id))
    if not listeners:
        return
    try:
        listeners.remove(cb)
    except ValueError:
        pass
    if not listeners:
        _message_listeners.pop(int(channel_id), None)


async def _notify_message_listeners(message, event: str) -> None:  # noqa: ANN001
    """Разослать событие подписчикам канала. Ошибки колбэков глотаются."""
    channel = getattr(message, "channel", None)
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return
    for cb in list(_message_listeners.get(int(channel_id), [])):
        try:
            await cb(message, event=event)
        except Exception:  # noqa: BLE001
            logger.exception("discord message listener failed (%s)", event)


# --------------------------------------------------------------------------
# 1. Хранение и статус подключения
# --------------------------------------------------------------------------

def _default_status() -> dict:
    return {"state": "off", "error": None, "guilds": 0, "since": 0.0}


def get_status() -> dict:
    """Текущий статус подключения — для экранов на всех платформах."""
    raw = addon_state.get(ADDON, STATUS_KEY, None)
    status = _default_status()
    if isinstance(raw, dict):
        status.update(raw)
    return status


def _set_status(state: str, *, error: str | None = None, guilds: int = 0) -> None:
    addon_state.set_(
        ADDON,
        STATUS_KEY,
        {"state": state, "error": error, "guilds": int(guilds), "since": time.time()},
    )


def get_token() -> str | None:
    token = addon_state.get(ADDON, TOKEN_KEY, None)
    return token if token else None


def set_token(token: str) -> None:
    """Сохранить токен Discord-бота. Токен не логируется никогда."""
    addon_state.set_(ADDON, TOKEN_KEY, str(token).strip())
    logger.info("discord token saved (masked)")


def get_bot_tag() -> str | None:
    """Сохранённый тег бота (username#discriminator) из последней валидации."""
    tag = addon_state.get(ADDON, BOT_TAG_KEY, None)
    return tag if tag else None


def _set_bot_tag(tag: str | None) -> None:
    addon_state.set_(ADDON, BOT_TAG_KEY, tag)


def clear_token() -> None:
    """Отключение + удаление токена (кнопка «забыть»)."""
    addon_state.set_(ADDON, TOKEN_KEY, None)
    _set_bot_tag(None)
    _set_status("off")
    logger.info("discord token cleared")


async def validate_token(token: str) -> tuple[bool, str | None, str | None]:
    """Проверка токена ДО сохранения (ТЗ §4.4 п.4, фикс 1.2).

    ``GET /users/@me`` с ``Authorization: Bot <token>``.
    Возвращает ``(ok, bot_tag, error_message)``. Токен не логируется.
    """
    token = (token or "").strip()
    if not token:
        return False, None, "⚠️ Пустой токен."
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=VALIDATE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                DISCORD_API_ME,
                headers={"Authorization": f"Bot {token}"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    username = str(data.get("username") or "bot")
                    discriminator = str(data.get("discriminator") or "0")
                    tag = (
                        username if discriminator in ("0", "0000", "")
                        else f"{username}#{discriminator}"
                    )
                    return True, tag, None
                if resp.status == 401:
                    return False, None, INVALID_TOKEN_MSG
                return False, None, f"Discord API вернул статус {resp.status}. Попробуй позже."
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — сеть/DNS/таймаут
        logger.warning("discord token validation failed: %s", type(exc).__name__)
        return False, None, (
            "Не удалось связаться с Discord API "
            f"({type(exc).__name__}). Проверь сеть и попробуй ещё раз."
        )


# --------------------------------------------------------------------------
# 2. Discord-клиент (ленивая зависимость, реконнект с backoff)
# --------------------------------------------------------------------------

async def start() -> None:
    """Запустить клиент, если есть токен. Никаких исключений наружу."""
    global _client_task

    if _client_task is not None and not _client_task.done():
        return  # уже работает

    token = get_token()
    if not token:
        _set_status("error", error="Токен не задан. Введи токен через сайт.")
        return

    try:
        import discord  # noqa: F401 — ленивая зависимость (правило №3)
    except Exception:  # noqa: BLE001 — ImportError и любые ошибки среды
        _set_status("error", error="discord.py не установлен")
        logger.warning("discord.py is not installed — adapter stays off")
        return

    _set_status("connecting")
    _client_task = asyncio.get_running_loop().create_task(_run_forever(token))


async def stop() -> None:
    """Остановить клиент (токен остаётся сохранённым)."""
    global _client, _client_task

    task, _client_task = _client_task, None
    client, _client = _client, None

    if client is not None:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            logger.exception("discord client close failed")
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _set_status("off")


async def _run_forever(token: str) -> None:
    """Жизненный цикл клиента: реконнект с экспоненциальной задержкой.

    Невалидный токен → статус ``error`` и выход (API не долбим).
    """
    delay = RECONNECT_BASE_DELAY
    while True:
        try:
            fatal = await _connect_once(token)
            if fatal:
                return
            # сессия успела выйти в online → отсчёт backoff заново
            if get_status().get("state") == "online":
                delay = RECONNECT_BASE_DELAY
            # обрыв соединения — реконнект с backoff
            _set_status("connecting", error="переподключение…")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _set_status("connecting", error=f"обрыв связи: {type(exc).__name__}")
            logger.warning("discord connection dropped: %s", type(exc).__name__)
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_DELAY)


async def _connect_once(token: str) -> bool:
    """Одна сессия клиента. Возвращает True при фатальной ошибке (стоп)."""
    global _client

    import discord

    # Фикс 2.3 (ТЗ §9Б.2, §9Б.5): минимальные intents вместо default() —
    # только guilds, guild_messages, dm_messages, message_content.
    intents = discord.Intents.none()
    for name in ("guilds", "guild_messages", "dm_messages", "message_content"):
        try:
            setattr(intents, name, True)
        except Exception:  # noqa: BLE001 — старые версии discord.py
            pass

    client_kwargs: dict[str, Any] = {"intents": intents}
    # Фикс 2.3: RAM-флаги — не чанкать гильдии и не кэшировать участников.
    client_kwargs["chunk_guilds_at_startup"] = False
    try:
        client_kwargs["member_cache_flags"] = discord.MemberCacheFlags.none()
    except Exception:  # noqa: BLE001
        pass

    try:
        client = discord.Client(**client_kwargs)
    except TypeError:
        # старые версии discord.py без RAM-флагов
        client = discord.Client(intents=intents)
    _client = client

    @client.event
    async def on_ready() -> None:  # noqa: ANN001
        _set_status("online", guilds=len(getattr(client, "guilds", []) or []))
        logger.info("discord online, guilds=%d", len(client.guilds))

    @client.event
    async def on_message(message) -> None:  # noqa: ANN001
        # SwapFace §8: сначала уведомляем подписчиков канала (create),
        # затем обычная обработка команд/медиа.
        await _notify_message_listeners(message, "create")
        await _handle_message(message)

    @client.event
    async def on_message_edit(before, after) -> None:  # noqa: ANN001
        # SwapFace §10: InsightFaceSwap редактирует placeholder → done.
        await _notify_message_listeners(after, "edit")

    @client.event
    async def on_interaction(interaction) -> None:  # noqa: ANN001
        await _handle_interaction(interaction)

    try:
        await client.start(token)
    except discord.LoginFailure:
        _set_status("error", error="Невалидный токен Discord. Проверь и введи заново.")
        logger.warning("discord login failure — stopping (no retry)")
        return True
    except getattr(discord, "PrivilegedIntentsRequired", ()):
        _set_status(
            "error",
            error="Включи MESSAGE CONTENT INTENT в Discord Developer Portal "
                  "→ Bot → Privileged Gateway Intents.",
        )
        logger.warning("discord privileged intents missing — stopping (no retry)")
        return True
    except asyncio.CancelledError:
        raise
    finally:
        if _client is client:
            _client = None
        try:
            if not client.is_closed():
                await client.close()
        except Exception:  # noqa: BLE001
            pass
    return False


# --------------------------------------------------------------------------
# 3. Команды и кнопки в Discord
# --------------------------------------------------------------------------

def handle_link_code(code: str, external_id: str, label: str = "",
                     is_dm: bool = True) -> str:
    """Привязка одноразовым кодом. Чистая функция — тестируется без Discord.

    Правило DM-only обеспечивает вызывающая сторона; здесь — вторая линия.
    """
    if not is_dm:
        return "🔒 Код принимаю только в личных сообщениях — не свети его в канале."
    ok, msg = identity.verify_code(str(code).strip(), PLATFORM, str(external_id),
                                   label=label)
    return msg


def menu_screen_for(external_id: str,
                    screen_id: str = DEFAULT_SCREEN_ID) -> tuple[MenuScreen | None, str | None]:
    """Экран меню для Discord-пользователя.

    Возвращает ``(screen, None)`` для привязанного аккаунта и
    ``(None, подсказка про /link)`` для гостя. Без реального Discord.
    """
    tg_user_id = identity.resolve_user_id(PLATFORM, str(external_id))
    if tg_user_id is None:
        return None, LINK_HINT
    screen = build_screen(screen_id, tg_user_id)
    if screen is None:
        return None, "⚠️ Экран сейчас недоступен. Попробуй ещё раз."
    return screen, None


def screen_to_discord(screen: MenuScreen) -> dict:
    """Нейтральный экран → примитивы Discord (контент + раскладка кнопок).

    Лимит Discord: 5 кнопок в ряду, 5 рядов. Если экран больше — первые
    25 кнопок + пометка «продолжение в Telegram». Чистая функция.
    """
    flat: list[MenuButton] = [btn for row in screen.rows for btn in row]
    overflow = len(flat) > MAX_BUTTONS_TOTAL
    flat = flat[:MAX_BUTTONS_TOTAL]

    rows: list[list[dict]] = []
    for i in range(0, len(flat), MAX_BUTTONS_PER_ROW):
        rows.append(
            [
                {
                    "label": (btn.text or " ")[:80],  # discord label limit
                    "custom_id": None if btn.url else btn.action,
                    "url": btn.url,
                }
                for btn in flat[i:i + MAX_BUTTONS_PER_ROW]
            ]
        )

    content = screen.title
    if screen.body:
        content += "\n\n" + screen.body
    if overflow:
        content += "\n\n" + OVERFLOW_NOTE
    return {"content": content[:2000], "rows": rows, "overflow": overflow}


def _parse_command(text: str) -> tuple[str, str] | None:
    """``!lilush link 123456`` → ("link", "123456"); None если не команда."""
    text = (text or "").strip()
    lowered = text.lower()
    if lowered.startswith(COMMAND_PREFIX):
        rest = text[len(COMMAND_PREFIX):].strip()
    elif lowered.startswith("/link") or lowered.startswith("/menu"):
        rest = text.lstrip("/").strip()
    else:
        return None
    if not rest:
        return ("menu", "")
    parts = rest.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("link", "menu"):
        return (cmd, arg)
    return None


#: Фикс 2.4: тексты чата с мозгом (Discord-путь).
BRAIN_ERROR_TEXT = "⚠️ Мозг сейчас недоступен. Попробуй ещё раз чуть позже."
#: Лимит одного сообщения Discord.
DISCORD_MESSAGE_LIMIT = 2000


async def _handle_free_text(message) -> None:  # noqa: ANN001
    """Фикс 2.4 (ТЗ R3): свободный текст → тот же мозг, что и в TG.

    Только ЛИЧНЫЕ сообщения (``message.guild is None``): в серверных
    каналах бот по-прежнему отвечает лишь на команды ``!lilush …`` —
    иначе он комментировал бы каждое сообщение канала. Привязанный
    пользователь → :func:`bot.gateway.brain.ask_brain` (обёртка над
    TG-пайплайном ``run_agent``), непривязанный → подсказка про /link.
    Ошибки мозга → вежливый текст; в лог — БЕЗ текста пользователя.
    """
    content = (getattr(message, "content", "") or "").strip()
    if not content or getattr(message, "guild", None) is not None:
        return

    external_id = str(message.author.id)
    tg_user_id = identity.resolve_user_id(PLATFORM, external_id)
    if tg_user_id is None:
        await message.channel.send(LINK_HINT)
        return

    from .. import brain  # lazy: тяжёлые зависимости мозга — по требованию

    try:
        answer = await brain.ask_brain(tg_user_id, content)
    except Exception:  # noqa: BLE001 — текст пользователя НЕ логируем
        logger.exception("discord brain reply failed (user_id=%s)", tg_user_id)
        await message.channel.send(BRAIN_ERROR_TEXT)
        return

    answer = answer or "…"
    for i in range(0, len(answer), DISCORD_MESSAGE_LIMIT):
        await message.channel.send(answer[i:i + DISCORD_MESSAGE_LIMIT])


async def _handle_incoming_media(message) -> bool:  # noqa: ANN001
    """Этап 5 (R11): фото в DM → регистрация артефакта в медиашине.

    Возвращает True, если сообщение обработано как медиа (дальше по
    конвейеру его пускать не надо). Работает только при включённом
    флаге ``media_enabled`` (rollback §8.1) и только в DM.
    """
    try:
        from .. import media_bus
    except Exception:  # noqa: BLE001
        return False
    if not media_bus.media_enabled():
        return False
    if getattr(message, "guild", None) is not None:
        return False
    attachments = list(getattr(message, "attachments", None) or [])
    att = None
    for candidate in attachments:
        ctype = (getattr(candidate, "content_type", "") or "").lower()
        if ctype.startswith("image/"):
            att = candidate
            break
    if att is None:
        return False

    external_id = str(message.author.id)
    tg_user_id = identity.resolve_user_id(PLATFORM, external_id)
    if tg_user_id is None:
        await message.channel.send(LINK_HINT)
        return True

    size = getattr(att, "size", 0) or 0
    if size > media_bus.MAX_FILE_BYTES:
        await message.channel.send(media_bus.TOO_BIG_TEXT)
        return True

    artifact = media_bus.register_artifact(
        kind="photo",
        origin={
            "platform": "discord",
            "channel_id": int(message.channel.id),
            "message_id": int(message.id),
            "attachment_id": int(att.id),
        },
        step="discord:upload",
    )
    screen = media_bus.build_artifact_screen(artifact["id"], tg_user_id)
    payload = screen_to_discord(screen)
    view = _build_view(payload["rows"])
    await message.channel.send(payload["content"], view=view)
    return True


async def send_dm(external_id: str, text: str) -> bool:
    """Личное сообщение пользователю Discord (broadcast R8 / доставка 📤).

    Возвращает False вместо исключения: доставка уведомлений никогда
    не должна ломать вызывающий код. Токен/текст не логируются.
    """
    client = _client
    if client is None or get_status().get("state") != "online":
        return False
    try:
        uid = int(external_id)
        user = client.get_user(uid) or await client.fetch_user(uid)
        await user.send(text[:DISCORD_MESSAGE_LIMIT])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("discord send_dm failed: %s", type(exc).__name__)
        return False


async def send_dm_file(external_id: str, data: bytes, filename: str,
                       caption: str = "") -> bool:
    """Доставка файла (📤 из медиашины) личным сообщением."""
    client = _client
    if client is None or get_status().get("state") != "online":
        return False
    try:
        import io

        import discord

        uid = int(external_id)
        user = client.get_user(uid) or await client.fetch_user(uid)
        await user.send(
            caption[:DISCORD_MESSAGE_LIMIT] or None,
            file=discord.File(io.BytesIO(data), filename=filename),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("discord send_dm_file failed: %s", type(exc).__name__)
        return False


async def fetch_attachment_bytes(origin: dict, max_bytes: int) -> bytes | None:
    """Резолвер медиашины (ТЗ §6А.6): свежий URL через fetch_message.

    Прямые CDN-URL не хранятся (протухают через 24 ч) — байты добываются
    по устойчивым координатам ``channel_id + message_id + attachment_id``.
    ``None`` = сообщение/вложение больше не существует (артефакт истёк).
    """
    client = _client
    if client is None:
        return None
    try:
        channel = client.get_channel(int(origin["channel_id"]))
        if channel is None:
            channel = await client.fetch_channel(int(origin["channel_id"]))
        message = await channel.fetch_message(int(origin["message_id"]))
        for att in getattr(message, "attachments", None) or []:
            if int(att.id) == int(origin["attachment_id"]):
                if (getattr(att, "size", 0) or 0) > max_bytes:
                    raise ValueError("file too big")
                return await att.read()
        return None
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("discord attachment fetch failed: %s", type(exc).__name__)
        return None


async def fetch_message_bytes(channel_id: int, message_id: int,
                              attachment_id: int, max_bytes: int) -> bytes | None:
    """Обобщение :func:`fetch_attachment_bytes` по явным координатам.

    SwapFace ТЗ §3/§5.4: байты вложений добываются по устойчивым
    ``channel_id + message_id + attachment_id`` (CDN-URL протухают).
    """
    return await fetch_attachment_bytes(
        {
            "channel_id": int(channel_id),
            "message_id": int(message_id),
            "attachment_id": int(attachment_id),
        },
        max_bytes,
    )


async def send_channel_text(channel_id: int, content: str) -> bool:
    """Post a command hint to a channel for the semi-manual SwapFace flow."""
    client = _client
    if client is None or get_status().get("state") != "online":
        return False
    try:
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        await channel.send((content or "")[:DISCORD_MESSAGE_LIMIT])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("discord send_channel_text failed: %s", type(exc).__name__)
        return False


async def send_channel_file(channel_id: int, data: bytes, filename: str,
                            content: str = "") -> bool:
    """Постинг файла в канал (SwapFace §8, режим 2). False = не доставлено."""
    client = _client
    if client is None or get_status().get("state") != "online":
        return False
    try:
        import io

        import discord

        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        await channel.send(
            (content or None) and content[:DISCORD_MESSAGE_LIMIT],
            file=discord.File(io.BytesIO(data), filename=filename),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("discord send_channel_file failed: %s", type(exc).__name__)
        return False


async def _handle_message(message) -> None:  # noqa: ANN001
    """Обработчик on_message — только для реального Discord-клиента."""
    try:
        if getattr(getattr(message, "author", None), "bot", False):
            return
        parsed = _parse_command(getattr(message, "content", "") or "")
        if parsed is None:
            # Этап 5: фото в DM → медиашина (если включена).
            if await _handle_incoming_media(message):
                return
            # Не команда и не /link — свободный текст → чат с мозгом (2.4).
            await _handle_free_text(message)
            return
        cmd, arg = parsed
        user = message.author
        external_id = str(user.id)

        if cmd == "link":
            is_dm = getattr(message, "guild", None) is None
            reply = handle_link_code(arg, external_id, label=str(user), is_dm=is_dm)
            await message.channel.send(reply)
            return

        # cmd == "menu"
        screen, hint = menu_screen_for(external_id)
        if screen is None:
            await message.channel.send(hint)
            return
        payload = screen_to_discord(screen)
        view = _build_view(payload["rows"])
        await message.channel.send(payload["content"], view=view)
    except Exception:  # noqa: BLE001
        logger.exception("discord message handler failed")


def _build_view(rows: list[list[dict]]):
    """Кнопки экрана → discord.ui.View (custom_id == action)."""
    import discord

    view = discord.ui.View(timeout=None)
    for row_idx, row in enumerate(rows[:MAX_ROWS]):
        for spec in row:
            if spec.get("url"):
                btn = discord.ui.Button(
                    label=spec["label"], url=spec["url"],
                    style=discord.ButtonStyle.link, row=row_idx,
                )
            else:
                btn = discord.ui.Button(
                    label=spec["label"], custom_id=spec["custom_id"],
                    style=discord.ButtonStyle.secondary, row=row_idx,
                )
            view.add_item(btn)
    return view


async def _handle_interaction(interaction) -> None:  # noqa: ANN001
    """Клик по кнопке: custom_id == action → dispatch_action → ответ."""
    try:
        data = getattr(interaction, "data", None) or {}
        action = data.get("custom_id")
        if not action:
            return
        user = interaction.user
        external_id = str(user.id)
        tg_user_id = identity.resolve_user_id(PLATFORM, external_id)

        ctx = GatewayContext(platform=PLATFORM, external_id=external_id,
                             user_id=tg_user_id)
        result = await dispatch_action(action, ctx)

        if result.screen is not None:
            payload = screen_to_discord(result.screen)
            view = _build_view(payload["rows"])
            await interaction.response.edit_message(
                content=payload["content"], view=view,
            )
            return
        if result.error == "tg_only":
            text = result.text or "⏳ Этот раздел пока только в Telegram."
            if result.deeplink:
                text += f"\n{result.deeplink}"
            await interaction.response.send_message(text, ephemeral=True)
            return
        if result.error == "forbidden":
            await interaction.response.send_message(
                result.text or "⛔ Недостаточно прав.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            result.text or "⚠️ Не получилось выполнить действие.", ephemeral=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("discord interaction handler failed")


# --------------------------------------------------------------------------
# 4. Экран gateway:discord:live + действия управления
# --------------------------------------------------------------------------

def _mask(token: str) -> str:
    token = token or ""
    if len(token) <= 8:
        return "…" if token else ""
    return f"{token[:4]}…{token[-4:]}"


def build_live_screen(user_id: int | None) -> MenuScreen:
    """«Живой» экран Discord: статус, серверы, ошибки, управление."""
    status = get_status()
    token = get_token()
    state = str(status.get("state", "off"))

    icon = {"off": "⚪️", "connecting": "🟡", "online": "🟢", "error": "🔴"}.get(state, "⚪️")
    label = {"off": "выключен", "connecting": "подключается…",
             "online": "онлайн", "error": "ошибка"}.get(state, state)

    lines = [f"Статус: {icon} {label}"]
    if token:
        lines.append(f"Токен: {_mask(token)}")
        bot_tag = get_bot_tag()
        if bot_tag:
            lines.append(f"Бот: {bot_tag}")
    else:
        lines.append(
            "Токен: не задан. Введи его кнопкой ниже или через сайт (VISION/Fable5)."
        )
    if state == "online":
        lines.append(f"Серверов: {int(status.get('guilds', 0))}")
    if status.get("error"):
        lines.append(f"⚠️ {status['error']}")
    lines.append("")
    lines.append(
        f"Команды в Discord: `{COMMAND_PREFIX} link <код>` — привязка, "
        f"`{COMMAND_PREFIX} menu` — меню."
    )

    rows: list[list[MenuButton]] = []
    if token:
        if state in ("online", "connecting"):
            rows.append([MenuButton(text="⏹ Остановить", action="gateway:discord:stop")])
        else:
            rows.append([MenuButton(text="▶️ Запустить", action="gateway:discord:start")])
        rows.append([MenuButton(text="🗑 Забыть токен", action="gateway:discord:forget")])
    rows.append([MenuButton(text="🔄 Обновить", action="gateway:discord:live")])
    rows.append([MenuButton(text="↩️ Назад", action="gateway:home")])

    return MenuScreen(
        screen_id="gateway:discord:live",
        title="🎮 Discord",
        body="\n".join(lines),
        rows=rows,
        parent="gateway:home",
    )


async def _act_start(action: str, ctx: GatewayContext) -> ScreenResult:
    await start()
    return ScreenResult(screen=build_live_screen(ctx.user_id))


async def _act_stop(action: str, ctx: GatewayContext) -> ScreenResult:
    await stop()
    return ScreenResult(screen=build_live_screen(ctx.user_id))


async def _act_forget(action: str, ctx: GatewayContext) -> ScreenResult:
    await stop()
    clear_token()
    return ScreenResult(screen=build_live_screen(ctx.user_id))


# --------------------------------------------------------------------------
# 5. Ввод токена (через сайт: POST /action → gateway:discord:settoken:<token>)
# --------------------------------------------------------------------------

async def _act_settoken(action: str, ctx: GatewayContext) -> ScreenResult:
    """Права уже проверены в dispatch (gateway:* → owner-only).

    Фикс 1.2: токен проверяется через ``/users/@me`` ДО сохранения.
    401 → не сохраняем, возвращаем понятную ошибку.
    """
    token = action[len(SETTOKEN_PREFIX):].strip()
    if not token:
        return ScreenResult(error="bad_action", text="⚠️ Пустой токен.")
    ok, bot_tag, err = await validate_token(token)
    if not ok:
        return ScreenResult(error="bad_token", text=f"⚠️ {err}")
    set_token(token)
    _set_bot_tag(bot_tag)
    return ScreenResult(screen=build_live_screen(ctx.user_id))


# --------------------------------------------------------------------------
# registration (публичные API этапа 1 — единственные точки интеграции)
# --------------------------------------------------------------------------

def register() -> None:
    register_screen("gateway:discord:live", build_live_screen)
    register_action("gateway:discord:start", _act_start)
    register_action("gateway:discord:stop", _act_stop)
    register_action("gateway:discord:forget", _act_forget)
    register_action_prefix(SETTOKEN_PREFIX, _act_settoken)


register()
