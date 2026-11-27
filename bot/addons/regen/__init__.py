"""«🔧 Регенерация» — the bot diagnoses its own code and the *coder*
writes the fix.

NO router: the UI handlers live on ``wizard_router`` (state priority),
like dump / viddyn / ghtools. Public surface:

* :mod:`scanner`     — self-diagnosis subprocess (compile + import +
  routers + ruff + dead-button scan).
* :mod:`research`    — «как решают люди» via Tavily (Анализатор PRO-style).
* :func:`generate_fix` (re-export from ``coder.regen_brain``) — the coder
  writes the real fix.
* :mod:`outputs`     — 3 deliverables: desc / json+instr / zip.
* :func:`regen_bg_loop` — background analysis loop (sim/real), registered
  in ``bot/main.py``.
"""

from __future__ import annotations

from ..coder.regen_brain import FixResult, NoBrainError, generate_fix
from . import outputs, research, scanner, store
from .background import regen_bg_loop, run_once

__all__ = [
    "FixResult",
    "NoBrainError",
    "generate_fix",
    "outputs",
    "research",
    "scanner",
    "store",
    "regen_bg_loop",
    "run_once",
]
