"""Phase 2: open each screen behind main-menu buttons 24-30, extract every
sub-button from the real reply_markup and press it. Reports:
  * UNHANDLED callbacks (dead buttons)
  * exceptions raised inside handlers
  * presses after which the user sees nothing (no API calls)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("DATA_DIR", "/tmp/botdata_test")

logging.basicConfig(level=logging.ERROR)

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User

OWNER_ID = 111

# sub-buttons that are destructive / hit external LLM APIs — press but with
# a short timeout; anything slower than 15s in a *button handler* is a bug
# anyway (Telegram voids the callback after ~10s).
TIMEOUT = 15


class RecordingBot(Bot):
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


def make_cb_update(uid: int, data: str, update_id: int) -> Update:
    user = User(id=uid, is_bot=False, first_name="Tester")
    msg = Message(
        message_id=update_id,
        date=int(time.time()),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=42, is_bot=True, first_name="bot"),
        text="меню",
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id), from_user=user, chat_instance="ci",
            message=msg, data=data,
        ),
    )


def extract_callbacks(calls: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    """Pull (text, callback_data) pairs out of recorded reply_markups."""
    out: list[tuple[str, str]] = []
    for _, data in calls:
        rm = data.get("reply_markup") or {}
        for row in rm.get("inline_keyboard", []):
            for btn in row:
                cd = btn.get("callback_data")
                if cd:
                    out.append((btn.get("text", "?"), cd))
    return out


async def press(dp, bot, cb: str, upd_id: int) -> tuple[str, list]:
    bot.calls.clear()
    try:
        res = await asyncio.wait_for(
            dp.feed_update(bot, make_cb_update(OWNER_ID, cb, upd_id)),
            timeout=TIMEOUT,
        )
    except asyncio.TimeoutError:
        return "TIMEOUT", list(bot.calls)
    except Exception as e:  # noqa: BLE001
        return f"EXC {type(e).__name__}: {e}", list(bot.calls)
    if res is UNHANDLED:
        return "UNHANDLED", list(bot.calls)
    return "ok", list(bot.calls)


async def main() -> None:
    from bot.storage import storage
    storage.set_owner_id(OWNER_ID)

    from bot.wizard import wizard_router
    from bot.addons import build_addon_routers

    dp = Dispatcher()
    dp.include_router(wizard_router)
    for r in build_addon_routers():
        dp.include_router(r)

    bot = RecordingBot(
        token=os.environ["BOT_TOKEN"],
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    tops = [
        (24, "Контекст", "crelay:home"),
        (25, "Master Room", "main:masterroom"),
        (26, "Скачать видео", "main:download"),
        (28, "Скачать (bash2mp4)", "main:install:downloader"),
        (30, "Монтажёр", "main:editor"),
    ]
    # skip pressing crelay:calib / crelay:run sub-actions? no — press all;
    # they must fail gracefully without API keys.

    upd = 5000
    problems: list[str] = []
    for idx, label, top_cb in tops:
        upd += 1
        status, calls = await press(dp, bot, top_cb, upd)
        subs = extract_callbacks(calls)
        print(f"\n### Button {idx} {label} [{top_cb}] -> {status}, {len(subs)} sub-buttons")
        seen = set()
        for text, cb in subs:
            if cb in seen:
                continue
            seen.add(cb)
            upd += 1
            s, c = await press(dp, bot, cb, upd)
            visible = [n for n, _ in c]
            note = ""
            if s != "ok":
                note = f"  <<< PROBLEM: {s}"
                problems.append(f"btn{idx} {label} -> [{text}] {cb}: {s}")
            elif not c:
                note = "  <<< PROBLEM: no API calls at all (silent)"
                problems.append(f"btn{idx} {label} -> [{text}] {cb}: SILENT")
            elif visible == ["AnswerCallbackQuery"]:
                # only the toast — check if it was an alert with text
                d = c[0][1]
                if not d.get("text"):
                    note = "  <<< suspicious: only empty answer(), screen unchanged"
                    problems.append(f"btn{idx} {label} -> [{text}] {cb}: EMPTY-ANSWER-ONLY")
                else:
                    note = f"  (toast: {str(d.get('text'))[:60]})"
            print(f"   [{text[:38]:38}] {cb:32} -> {s}{note}")

    print("\n===== SUMMARY OF PROBLEMS =====")
    for p in problems:
        print(" *", p)
    if not problems:
        print(" none")

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
