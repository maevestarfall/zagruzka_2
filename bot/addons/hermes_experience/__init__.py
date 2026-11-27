"""Hermes experience log — instructions distilled from what ACTUALLY happened.

Hermes' job here is to accumulate first-hand experience as the bot runs
(«источники не открылись», «план поиска сработал», «img2img дал артефакты»
…) and, on demand, compile a one-button Markdown document of *additional
instructions* — strictly grounded in those logged events, NOT in anything
read on the internet.

Storage rides in lilush's ``state.json`` via the shared addon-state
helper, so it survives restarts the same way every other setting does.
"""

from __future__ import annotations

from .store import (
    build_document,
    clear,
    count,
    list_entries,
    record,
)

__all__ = ["build_document", "clear", "count", "list_entries", "record"]
