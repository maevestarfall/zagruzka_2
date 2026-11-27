# «🔁 Ревью→фикс→проверка→повтор» — loop-review addon (idea from
# github.com/di-sukharev/loop-review). Co-authored with CoCo
"""Iterative independent-review loop over code.

Each pass uses a FRESH reviewer (no shared context) that returns findings +
a 1–10 score; a fixer applies the findings; repeat until the reviewer scores
>= threshold (9.5) or reports no actionable findings, or the pass cap is hit.

Settings live in ``state.json`` under the ``loop_review`` addon namespace.
``enabled`` = run the loop automatically after the «🧭 Контекст» assembly.
"""

from __future__ import annotations

import json

from .. import state as addon_state

ADDON = "loop_review"

DEFAULT_MAX_PASSES = 4
DEFAULT_THRESHOLD = 9.5


def get_enabled() -> bool:
    """Auto-run the loop after «Контекст» assembly."""
    return bool(addon_state.get(ADDON, "enabled", False))


def set_enabled(val: bool) -> None:
    addon_state.set_(ADDON, "enabled", bool(val))


def get_max_passes() -> int:
    try:
        return int(addon_state.get(ADDON, "max_passes", DEFAULT_MAX_PASSES) or DEFAULT_MAX_PASSES)
    except Exception:  # noqa: BLE001
        return DEFAULT_MAX_PASSES


def set_max_passes(n: int) -> None:
    addon_state.set_(ADDON, "max_passes", max(1, min(10, int(n))))


def get_threshold() -> float:
    try:
        return float(addon_state.get(ADDON, "threshold", DEFAULT_THRESHOLD) or DEFAULT_THRESHOLD)
    except Exception:  # noqa: BLE001
        return DEFAULT_THRESHOLD


def extract_json(text: str):
    """Pull the first JSON object/array out of an LLM reply (best-effort)."""
    if not text:
        return None
    import re
    m = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        raw = m.group(1) if m else None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
