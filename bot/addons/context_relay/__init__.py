# «🧭 Контекст» — TEMPORAL RELAY multi-agent pipeline addon.
# Co-authored with CoCo
"""Calibration-driven multi-agent code pipeline.

Main-menu button «🧭 Контекст» → calibrate the model (find its peak), then
run Decomposer → 5 Coders → Comparator → Refiner → Assembler, each as a
fresh rotated agent, assembling the final code from the model's best
("golden") outputs.
"""

from __future__ import annotations

from .handlers import build_context_relay_router

__all__ = ["build_context_relay_router"]
