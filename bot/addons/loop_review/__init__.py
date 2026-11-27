# «🔁 Ревью→фикс→проверка→повтор» — loop-review addon.
# Idea ported from github.com/di-sukharev/loop-review. Co-authored with CoCo
"""Iterative independent-review loop over code (review → fix → repeat)."""

from __future__ import annotations

from .engine import run_loop
from .handlers import build_loop_review_router

__all__ = ["build_loop_review_router", "run_loop"]
