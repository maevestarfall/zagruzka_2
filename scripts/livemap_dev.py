# Dev runner: serves the projectmap SPA with simulated live traffic.
# Co-authored with CoCo
"""Standalone preview: `python scripts/livemap_dev.py` then open
http://localhost:8899/projectmap/devtoken123/ — no Telegram needed."""
import asyncio
import os
import random
import sys

os.environ.setdefault("DATA_DIR", "/tmp/livemap_data")
os.environ["LIVEMAP_ENABLED"] = "1"
os.environ.setdefault("LIVEMAP_TEXT_MODE", "full")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiohttp import web  # noqa: E402

from bot.addons.projectmap import server, store, tracer  # noqa: E402

TOKEN = "devtoken123"
store.get_token = lambda: TOKEN
store.is_enabled = lambda: True


async def simulate():
    """Emit fake bot traffic so waves/tasks are visible on the map."""
    await asyncio.sleep(2)
    nodes = ["memory", "vault", "coder", "browser_panel", "tts", "vision",
             "video_gen", "osint", "heartbeat", "ram_guard"]
    n = 0
    while True:
        n += 1
        tid = f"t_dev{n}"
        tracer.task_begin(tid, 1, f"тестовая задача №{n}: сделай видео про котиков")
        tracer.emit("hop", tid, "llm_router", "agent")
        for _ in range(random.randint(2, 5)):
            a, b = random.sample(nodes, 2)
            tracer.emit("hop", tid, a, b)
            tracer.emit("node_start", tid, node=b)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            tracer.emit("node_end", tid, node=b,
                        duration_ms=random.uniform(80, 900))
        if n % 4 == 0:
            tracer.task_error(
                RuntimeError("simulated failure sk-abcdefghijklmnopqrstuvwxyz123456789"),
                node="video_gen", task_id=tid)
        else:
            tracer.emit("hop", tid, "agent", "user")
            tracer.task_finish(tid)
        await asyncio.sleep(random.uniform(1.0, 3.0))


async def main():
    app = web.Application()
    server.register_http_endpoints(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8899)
    await site.start()
    print(f"livemap dev server up: http://localhost:8899/projectmap/{TOKEN}/")
    asyncio.create_task(simulate())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
