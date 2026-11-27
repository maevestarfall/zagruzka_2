"""Prompt-source manager — «📚 Источники промптов» for photo & video.

Lets the owner curate a per-kind list of websites that host ready-made
diffusion prompts. When the user asks the bot for «помощь ИИ» on a photo
or video idea, the bot pulls real example prompts from these sources,
ranks them by relevance/popularity and offers 4 options: 3 verbatim from
the sites + 1 hybrid tailored to the user's request.
"""

from __future__ import annotations

from .handlers import build_prompt_sources_router, show_screen

__all__ = ["build_prompt_sources_router", "show_screen"]
