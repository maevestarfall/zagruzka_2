"""Platform adapters for the gateway (ТЗ-1 §3.2).

Этап 1 ships ``telegram`` (референс) and ``web`` (Fable5/VISION bridge).
``discord`` / ``vk`` arrive на этапах 2–3 as optional extras — their
absence must never break imports of this package (§13.3).
"""

from __future__ import annotations

from .base import GatewayAdapter
from .telegram import TelegramAdapter
from .web import WebAdapter

__all__ = ["GatewayAdapter", "TelegramAdapter", "WebAdapter"]
