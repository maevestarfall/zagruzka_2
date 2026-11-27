"""Telegram — the reference adapter (ТЗ-1 §3.2).

Telegram keeps behaving exactly as before: aiogram routers stay in
charge, and this adapter is a thin wrapper over
:func:`bot.gateway.menu.screen_to_tg` so the gateway pipeline can render
into TG through the same contract as every other platform.
"""

from __future__ import annotations

from ..menu import MenuScreen, screen_to_tg
from .base import GatewayAdapter


class TelegramAdapter(GatewayAdapter):
    platform = "telegram"

    def render(self, screen: MenuScreen, user_id: int | None):
        """Neutral screen → aiogram ``InlineKeyboardMarkup`` (1:1 legacy)."""
        return screen_to_tg(screen, user_id)

    def health(self) -> dict:
        return {"platform": self.platform, "enabled": True, "native": True}
