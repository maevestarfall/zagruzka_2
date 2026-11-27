"""Prompt-library finder addon.

User sends a photo + a short wish ("хочу летать в небе"); the bot extracts
keywords, searches the configured prompt-source sites IN PRIORITY ORDER
(site #1, then #2, ...), returns the best ready-made prompts and one hybrid
prompt that blends them with the wish.

Sites are stored (priority-ordered) under addon-state key ``promptlab/sites``;
the default secondary source is youmind's Nano Banana Pro library.
"""

from __future__ import annotations

__all__ = ["build_promptlab_router"]


def build_promptlab_router():
    from .handlers import build_promptlab_router as _b
    return _b()
