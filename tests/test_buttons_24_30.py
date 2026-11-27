"""Test harness: simulate pressing main-menu buttons 24-30 through the real Dispatcher.

Buttons (top-down index in _kb_main_after_claim for an admin):
  24: Контекст                    -> crelay:home
  25: Master Room                 -> main:masterroom
  26: Скачать видео               -> main:download
  27: Комп (localdesktop)         -> main:install:comp
  28: Скачать (bash2mp4)          -> main:install:downloader
  29: Анализатор (agent-skills)   -> main:install:analyzer
  30: Монтажёр                    -> main:editor
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("DATA_DIR", "/tmp/botdata_test")

logging.basicConfig(level=logging.WARNING)

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User

OWNER_ID = 111


class RecordingBot(Bot):
    """Bot that records API calls instead of hitting Telegram."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, method: TelegramMethod[TelegramType], request_timeout=None):
        name = type(method).__name__
        data = method.model_dump(exclude_none=True)
        self.calls.append((name, data))
        if name in ("SendMessage", "EditMessageText", "EditMessageReplyMarkup"):
            return Message(
                message_id=int(time.time() * 1000) % 100000,
                date=int(time.time()),
                chat=Chat(id=data.get("chat_id", 1), type="private"),
                from_user=User(id=42, is_bot=True, first_name="bot"),
                text=data.get("text", ""),
            )
        return True


def make_callback_update(uid: int, data: str, update_id: int) -> Update:
    user = User(id=uid, is_bot=False, first_name="Tester")
    msg = Message(
        message_id=update_id,
        date=int(time.time()),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=42, is_bot=True, first_name="bot"),
        text="Главное меню",
    )
    cq = CallbackQuery(
        id=str(update_id),
        from_user=user,
        chat_instance="ci",
        message=msg,
        data=data,
    )
    return Update(update_id=update_id, callback_query=cq)


async def main() -> None:
    # -- claim ownership so can_admin(OWNER_ID) is True ------------------
    from bot.storage import storage
    storage.set_owner_id(OWNER_ID)

    # -- build dispatcher mirroring bot/main.py order ---------------------
    from bot.wizard import wizard_router
    from bot.addons import build_addon_routers

    dp = Dispatcher()
    dp.include_router(wizard_router)
    n_addons = 0
    for r in build_addon_routers():
        dp.include_router(r)
        n_addons += 1
    print(f"[harness] wizard + {n_addons} addon routers registered")

    bot = RecordingBot(
        token=os.environ["BOT_TOKEN"],
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    buttons = [
        (24, "Контекст", "crelay:home"),
        (25, "Master Room", "main:masterroom"),
        (26, "Скачать видео", "main:download"),
        (27, "Комп (localdesktop)", "main:install:comp"),
        (28, "Скачать (bash2mp4)", "main:install:downloader"),
        (29, "Анализатор (agent-skills)", "main:install:analyzer"),
        (30, "Монтажёр", "main:editor"),
    ]

    upd_id = 1000
    for idx, label, cb in buttons:
        bot.calls.clear()
        upd_id += 1
        print(f"\n===== Button {idx}: {label} ({cb}) =====")
        try:
            result = await asyncio.wait_for(
                dp.feed_update(bot, make_callback_update(OWNER_ID, cb, upd_id)),
                timeout=30,
            )
            handled = result is not None and str(result) != "UNHANDLED"
            print(f"  dispatch result: {result!r}  handled={handled}")
        except asyncio.TimeoutError:
            print("  !! TIMEOUT (handler hung > 30s)")
        except Exception as e:  # noqa: BLE001
            print(f"  !! EXCEPTION {type(e).__name__}: {e}")
        for name, data in bot.calls:
            text = str(data.get("text", data.get("show_alert", "")))[:110].replace("\n", " | ")
            print(f"  -> {name}: {text}")
        if not bot.calls:
            print("  -> (no API calls — user sees NOTHING)")

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
