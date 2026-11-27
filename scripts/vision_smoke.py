"""Local smoke-test harness for the VISION panel.

Boots a bare aiohttp app with the VISION + Fable5 endpoints registered
(no Telegram polling), prints the panel URL and serves until killed.

Usage:  python3 scripts/vision_smoke.py [port]
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATA_DIR", "/tmp/vision_smoke_data")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402

from bot.addons.vision import server as vision  # noqa: E402
from bot.addons.fable5 import server as fable5  # noqa: E402


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099

    token, _link = vision.generate_link("")
    fable5.generate_link("")

    app = web.Application()
    vision.register_http_endpoints(app)
    fable5.register_http_endpoints(app)

    print(f"VISION: http://127.0.0.1:{port}/vision/{token}/", flush=True)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
