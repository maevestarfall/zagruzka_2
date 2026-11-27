"""Background scheduler for «🔌 Каналы-источники».

Wakes up every ``_TICK_SECONDS`` and, when auto-mode is ON, fires a
buffer-check at each daily time the owner configured (interpreted in the
addon's timezone, default Europe/Moscow). A per-minute marker prevents
the same scheduled time from firing twice.

Started by ``bot.main`` alongside the other background loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from aiogram import Bot

from . import processor, store

logger = logging.getLogger(__name__)

_TICK_SECONDS = 30


def _now_in_tz():
    tz_name = store.get_tz()
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 — fall back to local server time
        return datetime.now().astimezone()


async def _report(bot: Bot, text: str) -> None:
    chat_id = store.get_owner_chat_id()
    if chat_id is None:
        logger.info("source_channels: no owner chat set — skip report")
        return
    with contextlib.suppress(Exception):
        await bot.send_message(chat_id, text)


async def _maybe_run(bot: Bot) -> None:
    if not store.is_auto_enabled():
        return
    if not store.get_sources():
        return
    now = _now_in_tz()
    hhmm = now.strftime("%H:%M")
    if hhmm not in store.get_times():
        return
    marker = now.strftime("%Y-%m-%d %H:%M")
    if store.get_last_run_marker() == marker:
        return  # already fired this minute
    store.set_last_run_marker(marker)

    logger.info("source_channels: scheduled check at %s", hhmm)
    result = await processor.process_pending()
    if result["processed"] or store.pending_count() == 0:
        await _report(bot, result["report"])


async def scheduler_loop(bot: Bot) -> None:
    logger.info("source_channels scheduler started (tick=%ds)", _TICK_SECONDS)
    while True:
        try:
            await _maybe_run(bot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("source_channels scheduler tick failed — continuing")
        await asyncio.sleep(_TICK_SECONDS)
