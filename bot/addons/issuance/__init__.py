"""Issuance and Card Verification addon.

Exposes a 'Выдача' main menu section with a 'CARD' bulk validation simulator.
"""

from __future__ import annotations

from .handlers import build_issuance_router

__all__ = ["build_issuance_router"]
