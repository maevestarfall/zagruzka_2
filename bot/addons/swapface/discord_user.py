"""Mode 1 «⚡ Автомат (user-токен)» (ТЗ SwapFace v2.0 §5.3, §7).

Invokes InsightFaceSwap slash commands from the OWNER's Discord account
via the Interactions REST API and watches the channel by **polling**
(the gateway is never started with a user token).

Self-bot: violates Discord ToS — the UI must obtain explicit consent
before this module is ever used (state.usertoken_accepted_at).
The token comes from env only; it is never written to state.json.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import parser as sw_parser
from . import state as sw_state
from .discord_rest import (
    DiscordRest,
    DiscordRestError,
    build_interaction_payload,
    find_insight_commands,
)

logger = logging.getLogger("swapface.user")

POLL_INTERVAL = 3.0


class SwapFaceUserError(Exception):
    """User-facing error (ru), safe to show in Telegram."""


def _require_config() -> tuple[str, str]:
    guild_id = sw_state.get_guild_id()
    channel_id = sw_state.get_channel_id()
    if not guild_id or not channel_id:
        raise SwapFaceUserError("Не заданы guild_id/channel_id — настрой «🔗 Discord».")
    return guild_id, channel_id


def _make_rest(client=None, sleep=asyncio.sleep) -> DiscordRest:
    token = sw_state.get_user_token()
    if not token:
        raise SwapFaceUserError(
            f"Env-переменная {sw_state.user_token_ref()} не задана — "
            "user-токен недоступен.",
        )
    return DiscordRest(token, is_bot=False, client=client, sleep=sleep)


async def check_token(rest: DiscordRest | None = None) -> dict:
    """§5.6: GET /users/@me под user-токеном. Возвращает профиль."""
    rest = rest or _make_rest()
    try:
        return await rest.me()
    except DiscordRestError as exc:
        raise SwapFaceUserError(f"Токен не принят Discord: {exc}") from None


async def scan_commands(rest: DiscordRest | None = None) -> tuple[str, dict]:
    """§5.2: сканировать command-index гильдии, сохранить в state."""
    guild_id, _ = _require_config()
    rest = rest or _make_rest()
    index = await rest.command_index(guild_id)
    app_id, commands = find_insight_commands(index)
    if not app_id:
        raise SwapFaceUserError(
            "InsightFaceSwap не найден на сервере — проверь guild_id "
            "и что бот добавлен.",
        )
    slim = {
        name: {"id": c["id"], "version": c["version"]}
        for name, c in commands.items()
    }
    sw_state.set_command_index(app_id, slim)
    return app_id, slim


def _command_or_fail(name: str) -> dict:
    commands = sw_state.get_commands()
    cmd = commands.get(name)
    if not cmd:
        raise SwapFaceUserError(
            f"Команда /{name} не отсканирована — жми «🔄 Пересканировать».",
        )
    return cmd


async def invoke_slash(name: str, options: list[dict], *,
                       image: bytes | None = None,
                       image_filename: str = "frame.png",
                       rest: DiscordRest | None = None) -> tuple[str | None, str]:
    """§5.3: полный вызов slash-команды. Возвращает (interaction_id, nonce)."""
    guild_id, channel_id = _require_config()
    app_id = sw_state.get_insight_app_id()
    if not app_id:
        raise SwapFaceUserError("insight_app_id неизвестен — сначала пересканируй команды.")
    cmd = _command_or_fail(name)
    rest = rest or _make_rest()

    attachments = None
    if image is not None:
        slot = await rest.create_attachment_slot(channel_id, image_filename, len(image))
        await rest.upload_to_slot(slot["upload_url"], image, "image/png")
        attachments = [{
            "id": "0",
            "filename": image_filename,
            "uploaded_filename": slot["upload_filename"],
        }]
        options = list(options) + [{"type": 11, "name": "image", "value": 0}]

    payload = build_interaction_payload(
        app_id=app_id, guild_id=guild_id, channel_id=channel_id,
        command=cmd, command_name=name, options=options,
        attachments=attachments,
    )
    return await rest.post_interaction(payload)


async def poll_for_reply(*, interaction_id: str | None, nonce: str | None,
                         timeout: float | None = None,
                         rest: DiscordRest | None = None,
                         poll_interval: float = POLL_INTERVAL,
                         sleep=asyncio.sleep) -> dict:
    """§5.3 step 5: poll messages until the final correlated reply.

    In-progress placeholders are remembered by ``message.id`` and
    re-fetched (edits change message content but keep the id, and the
    edited message re-appears in ``?after`` scans only sometimes — so we
    also re-read the tracked ids each cycle via a wider window).
    """
    _, channel_id = _require_config()
    rest = rest or _make_rest()
    timeout = timeout or sw_state.swap_timeout()
    app_id = sw_state.get_insight_app_id()

    deadline = time.monotonic() + timeout
    cursor: str | None = None
    tracked_ids: set[str] = set()

    while time.monotonic() < deadline:
        messages = await rest.get_messages_after(channel_id, cursor)
        # oldest → newest; API returns newest first
        for message in sorted(messages, key=lambda m: int(m.get("id") or 0)):
            mid = str(message.get("id") or "")
            if mid:
                cursor = mid if (cursor is None or int(mid) > int(cursor)) else cursor
            matched = mid in tracked_ids or sw_parser.matches_interaction(
                message, interaction_id=interaction_id, nonce=nonce,
                insight_app_id=app_id,
            )
            if not matched:
                continue
            parsed = sw_parser.parse_reply(message)
            if parsed.credits_remaining is not None:
                sw_state.set_credits_remaining(parsed.credits_remaining)
            if parsed.status == sw_parser.STATUS_IN_PROGRESS:
                tracked_ids.add(mid)
                # placeholder редактируется на месте — cursor назад,
                # чтобы поймать edit при повторном чтении
                if cursor and mid and int(cursor) >= int(mid):
                    cursor = str(int(mid) - 1)
                continue
            return message
        await sleep(poll_interval)
    raise SwapFaceUserError(
        f"InsightFaceSwap не ответил за {int(timeout)} сек (user-режим).",
    )


async def swap_image(image: bytes, idname: str, *,
                     rest: DiscordRest | None = None,
                     sleep=asyncio.sleep) -> dict:
    """/swapid end-to-end (mode 1). Returns the final Discord message dict."""
    rest = rest or _make_rest()
    interaction_id, nonce = await invoke_slash(
        "swapid",
        [{"type": 3, "name": "idname", "value": idname}],
        image=image, rest=rest,
    )
    return await poll_for_reply(
        interaction_id=interaction_id, nonce=nonce, rest=rest, sleep=sleep,
    )


async def save_face(image: bytes, idname: str, *,
                    rest: DiscordRest | None = None,
                    sleep=asyncio.sleep) -> dict:
    rest = rest or _make_rest()
    interaction_id, nonce = await invoke_slash(
        "saveid",
        [{"type": 3, "name": "idname", "value": idname}],
        image=image, image_filename=f"{idname}.jpg", rest=rest,
    )
    return await poll_for_reply(
        interaction_id=interaction_id, nonce=nonce, rest=rest, sleep=sleep,
    )


async def delete_face(idname: str, *, rest: DiscordRest | None = None,
                      sleep=asyncio.sleep) -> dict:
    rest = rest or _make_rest()
    interaction_id, nonce = await invoke_slash(
        "delid", [{"type": 3, "name": "idname", "value": idname}], rest=rest,
    )
    return await poll_for_reply(
        interaction_id=interaction_id, nonce=nonce, rest=rest, sleep=sleep,
    )


async def set_active(idname: str, *, rest: DiscordRest | None = None,
                     sleep=asyncio.sleep) -> dict:
    rest = rest or _make_rest()
    interaction_id, nonce = await invoke_slash(
        "setid", [{"type": 3, "name": "idname", "value": idname}], rest=rest,
    )
    return await poll_for_reply(
        interaction_id=interaction_id, nonce=nonce, rest=rest, sleep=sleep,
    )


async def list_faces(*, rest: DiscordRest | None = None,
                     sleep=asyncio.sleep) -> dict:
    rest = rest or _make_rest()
    interaction_id, nonce = await invoke_slash("listid", [], rest=rest)
    return await poll_for_reply(
        interaction_id=interaction_id, nonce=nonce, rest=rest, sleep=sleep,
    )
