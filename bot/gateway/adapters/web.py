"""Web — the bridge into Fable5 / VISION (ТЗ-1 §3.2, §4.7).

The site does not run a background client: the SPA pulls screens over
the token-gated Fable5 REST API (``GET /menu``, ``POST /action``, wired
in :mod:`bot.addons.fable5.server`). This adapter only owns the JSON
rendering plus guest gating, so the HTTP layer stays a dumb pipe.
"""

from __future__ import annotations

from ..menu import MenuScreen, screen_to_json
from .base import GatewayAdapter


def screen_json_for(screen_id: str, user_id: int | None) -> dict | None:
    """Build a registered screen and render it as Fable5 JSON.

    Convenience used by the HTTP layer (``GET /menu``). Returns ``None``
    for unknown screens so the endpoint can answer 404.
    """
    from ..menu import build_screen

    screen = build_screen(screen_id, user_id)
    if screen is None:
        return None
    return WebAdapter().render(screen, user_id)


class WebAdapter(GatewayAdapter):
    platform = "web"

    def render(self, screen: MenuScreen, user_id: int | None) -> dict:
        """Neutral screen → Fable5 JSON contract, guest-gated once here."""
        from ...feature_gate import filter_menu_rows

        rows = filter_menu_rows(screen.rows, user_id)
        gated = MenuScreen(
            screen_id=screen.screen_id,
            title=screen.title,
            body=screen.body,
            rows=rows,
            parent=screen.parent,
        )
        return screen_to_json(gated)

    def health(self) -> dict:
        try:
            from ...addons.vision.server import is_enabled
            return {"platform": self.platform, "enabled": bool(is_enabled())}
        except Exception:  # noqa: BLE001
            return {"platform": self.platform, "enabled": False}
