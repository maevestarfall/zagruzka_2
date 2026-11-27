"""Adapter contract for the Platform Gateway (ТЗ-1 §3.2).

Every surface (Telegram, web, Discord, VK, …) implements this small ABC.
The core never talks to a platform SDK directly — adapters are the only
place where ``discord.py`` / ``vkbottle`` types may appear, which is what
keeps them optional dependencies (§13.3) and swappable (§12, риск
«библиотека умрёт»).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..menu import MenuScreen


class GatewayAdapter(ABC):
    """One platform surface: render screens, send text, report health."""

    #: platform key used in GatewayContext / identity links.
    platform: str = "base"

    @abstractmethod
    def render(self, screen: MenuScreen, user_id: int | None) -> Any:
        """Convert a neutral screen into the platform-native structure."""

    async def send(self, target: Any, text: str) -> None:  # pragma: no cover
        """Send plain text to a platform-native target (chat / channel)."""
        raise NotImplementedError

    async def start(self) -> None:
        """Start background work (long-poll / websocket). No-op by default."""

    async def stop(self) -> None:
        """Graceful shutdown. No-op by default."""

    def health(self) -> dict:
        """Data for the «📡 Статус» screen: enabled, uptime, errors, …"""
        return {"platform": self.platform, "enabled": True}
