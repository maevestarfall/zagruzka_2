"""OSINT addon — «🕵️ Исследование и поиск человека».

Curated AI-OSINT toolbox (derived from the Awesome-AI-OSINT repo) plus an
LLM assistant that builds a step-by-step research plan from a target
description (name / email / username / phone / photo).
"""

from __future__ import annotations

from .handlers import build_osint_router, show_screen

__all__ = ["build_osint_router", "show_screen"]
