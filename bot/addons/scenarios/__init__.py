"""«🧭 Сценарий» — маршруты между двумя сайтами (ТЗ rev.7.1).

Two tabs in one browser session: copy the email on site A, paste it on
site B, wait for the OTP code back on A, paste it on B, press the final
button — all from one Telegram button.
"""

from __future__ import annotations

__all__ = ["build_scenarios_router"]


def build_scenarios_router():
    from .handlers import build_scenarios_router as _build
    return _build()
