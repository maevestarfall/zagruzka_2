"""Mode 2 «🤝 Полуручной» (ТЗ SwapFace v2.0 §8).

Our gateway bot posts the frame to the channel; the owner runs
``/swapid`` by hand; we catch the reply **push-style** through
``discord_gw.register_message_listener`` (create AND edit events —
InsightFaceSwap edits its progress placeholder in place, §10).

Correlation uses §5.5 branch 4 (interaction id is not visible to our
bot for a human-invoked command): author == insight bot AND
interaction invoker == owner's Discord id AND timestamp in window.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import parser as sw_parser
from . import state as sw_state

logger = logging.getLogger("swapface.watch")


class SwapFaceWatchError(Exception):
    """User-facing (ru) error for the semi-manual mode."""


def message_to_dict(message) -> dict:  # noqa: ANN001 — discord.Message | dict
    """Normalise a discord.py Message (or an already-dict payload) for parser."""
    if isinstance(message, dict):
        message.setdefault("_timestamp", time.time())
        return message

    def _components(msg) -> list:  # noqa: ANN001
        rows = []
        for row in getattr(msg, "components", None) or []:
            children = []
            for comp in getattr(row, "children", None) or []:
                children.append({
                    "type": int(getattr(comp, "type", 0) or 0)
                    if not hasattr(getattr(comp, "type", None), "value")
                    else int(comp.type.value),
                    "style": int(getattr(comp, "style", 0) or 0)
                    if not hasattr(getattr(comp, "style", None), "value")
                    else int(comp.style.value),
                    "url": getattr(comp, "url", None),
                })
            rows.append({"type": 1, "components": children})
        return rows

    interaction_meta = {}
    meta = getattr(message, "interaction_metadata", None)
    if meta is not None:
        user = getattr(meta, "user", None)
        interaction_meta = {
            "id": str(getattr(meta, "id", "") or ""),
            "user": {"id": str(getattr(user, "id", "") or "")} if user else {},
        }
    legacy = {}
    inter = getattr(message, "interaction", None)
    if inter is not None:
        user = getattr(inter, "user", None)
        legacy = {
            "id": str(getattr(inter, "id", "") or ""),
            "user": {"id": str(getattr(user, "id", "") or "")} if user else {},
        }

    created = getattr(message, "created_at", None)
    ts = created.timestamp() if created is not None else time.time()

    return {
        "id": str(getattr(message, "id", "") or ""),
        "channel_id": str(getattr(getattr(message, "channel", None), "id", "") or ""),
        "content": str(getattr(message, "content", "") or ""),
        "author": {"id": str(getattr(getattr(message, "author", None), "id", "") or "")},
        "attachments": [
            {
                "id": str(getattr(att, "id", "") or ""),
                "url": str(getattr(att, "url", "") or ""),
                "filename": str(getattr(att, "filename", "") or ""),
            }
            for att in getattr(message, "attachments", None) or []
        ],
        "components": _components(message),
        "interaction_metadata": interaction_meta,
        "interaction": legacy,
        "nonce": str(getattr(message, "nonce", "") or ""),
        "_timestamp": ts,
    }


async def post_frame(image: bytes, filename: str, content: str) -> None:
    """Step 1 §8: put the frame into the channel via the gateway bot."""
    channel_id = sw_state.get_channel_id()
    if not channel_id:
        raise SwapFaceWatchError("channel_id не задан — настрой «🔗 Discord».")
    from ...gateway.adapters import discord_gw

    ok = await discord_gw.send_channel_file(
        int(channel_id), image, filename, content,
    )
    if not ok:
        raise SwapFaceWatchError(
            "Не удалось отправить кадр в канал — наш Discord-бот офлайн?",
        )


async def wait_for_reply(*, invoker_discord_id: str | None = None,
                         timeout: float | None = None,
                         gw=None, sleep=asyncio.sleep) -> dict:
    """Steps 4–5 §8: subscribe to the channel and await the final reply.

    ``gw`` — injectable gateway module (tests); defaults to the live
    ``discord_gw``. Listens to BOTH create and edit events.
    """
    channel_id = sw_state.get_channel_id()
    if not channel_id:
        raise SwapFaceWatchError("channel_id не задан — настрой «🔗 Discord».")
    if gw is None:
        from ...gateway.adapters import discord_gw as gw  # noqa: PLW0127

    timeout = timeout or sw_state.swap_timeout()
    app_id = sw_state.get_insight_app_id()
    window_start = time.time()
    loop = asyncio.get_running_loop()
    result: asyncio.Future = loop.create_future()

    async def _cb(message, event: str = "create") -> None:  # noqa: ANN001
        if result.done():
            return
        try:
            data = message_to_dict(message)
            matched = sw_parser.matches_interaction(
                data,
                insight_app_id=app_id,
                invoker_discord_id=invoker_discord_id,
                window_start=window_start,
            )
            if not matched and invoker_discord_id is None:
                # владелец без привязанного Discord id: доверяем автору
                # + окну времени (документированное ослабление §5.5.4)
                author = str((data.get("author") or {}).get("id") or "")
                matched = bool(app_id) and author == str(app_id) and (
                    float(data.get("_timestamp") or 0) >= window_start
                )
            if not matched:
                return
            parsed = sw_parser.parse_reply(data)
            if parsed.credits_remaining is not None:
                sw_state.set_credits_remaining(parsed.credits_remaining)
            if parsed.status == sw_parser.STATUS_IN_PROGRESS:
                return  # ждём edit того же сообщения
            result.set_result(data)
        except Exception:  # noqa: BLE001
            logger.exception("swapface watch callback failed")

    gw.register_message_listener(int(channel_id), _cb)
    try:
        return await asyncio.wait_for(result, timeout=timeout)
    except asyncio.TimeoutError:
        raise SwapFaceWatchError(
            f"InsightFaceSwap не ответил за {int(timeout)} сек.",
        ) from None
    finally:
        gw.unregister_message_listener(int(channel_id), _cb)


def manual_command_hint(command: str, idname: str) -> str:
    """Готовая строка для копирования владельцем (§8.2)."""
    if command == "swapid":
        return f"/swapid idname:{idname} image:<прикрепи картинку выше>"
    if command == "saveid":
        return f"/saveid idname:{idname} image:<прикрепи фото лица выше>"
    if command == "delid":
        return f"/delid idname:{idname}"
    if command == "setid":
        return f"/setid idname:{idname}"
    return f"/{command}"
