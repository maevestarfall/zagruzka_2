"""SwapFace addon — InsightFaceSwap (Discord) in the media pipeline.

See ТЗ «SwapFace» v2.0. Exposes :func:`build_swapface_router` for
lilush's dispatcher; everything else is reached through
:mod:`bot.addons.swapface.service` (pipeline) and the ``swf:`` Telegram
UI in :mod:`bot.addons.swapface.handlers`.
"""

from __future__ import annotations

__all__ = ["build_swapface_router", "setup"]


def build_swapface_router():
    from .handlers import build_swapface_router as _build

    return _build()


def setup():
    """Compatibility hook mirroring sibling addons; returns the router."""
    return build_swapface_router()
