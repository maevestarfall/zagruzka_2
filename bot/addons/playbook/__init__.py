"""«📚 Поступенчатая инструкция» — a tree of actions (general → specific)
where each leaf holds a step-by-step plan (tools + prompts + timings),
consulted first when the toggle is on and refined in the background by
Hermes.
"""

from __future__ import annotations

from . import executor, store  # noqa: F401

__all__ = ["store", "executor", "build_playbook_router", "hermes_loop"]


def build_playbook_router():
    from .handlers import build_playbook_router as _b

    return _b()


async def hermes_loop(bot, get_chat_id):
    from .hermes import hermes_loop as _l

    await _l(bot, get_chat_id)
