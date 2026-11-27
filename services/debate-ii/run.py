"""
Single-process entry point: launches FastAPI (uvicorn) AND the Telegram bot
concurrently. On Render, this is what `python run.py` runs.
"""

import asyncio
import logging
import os
import signal

import uvicorn

import telegram_bot
from app import app as fastapi_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("runner")


async def run_uvicorn():
    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()


async def run_bot():
    bot_app = telegram_bot.build_app()
    # park forever; sit out gracefully if the bot is disabled (no token / no owner)
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    if bot_app is None:
        log.warning("Telegram bot disabled (no TELEGRAM_BOT_TOKEN/OWNER_TG_ID) — serving API only")
        await stop.wait()
        return
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(
        allowed_updates=["message", "callback_query"])
    log.info("Telegram bot polling started")
    await stop.wait()
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()


async def main():
    await asyncio.gather(run_uvicorn(), run_bot())


if __name__ == "__main__":
    asyncio.run(main())
