# Context-relay (TEMPORAL RELAY) addon: calibration-driven multi-agent store.
# Co-authored with CoCo
"""State for «🧭 Контекст» — the TEMPORAL RELAY multi-agent pipeline.

Core idea (owner's design): a model's best output is concentrated in its
"peak". A one-time CALIBRATION per model measures that peak (`peak_ratio`)
and — crucially — that result RESHAPES the whole scenario:

* peak ≈ 2/10  → split the ТЗ finer (more parts, layered gold + non-gold);
* peak ≈ 1/20  → make almost everything from gold, minimal splitting, then
  improve it with patches.

So `derive_layout(peak_ratio)` turns the measured peak into the run shape
(parts count, coder count, patch depth). Everything downstream reads it.

We also persist the Assembler's priority list between runs (adaptive
feedback) and the last run's result.
"""

from __future__ import annotations

import json
import time

from .. import state as addon_state

ADDON = "context_relay"


# ─── model key (which model we calibrated) ────────────────────────────

def current_model_key() -> str:
    import contextlib
    with contextlib.suppress(Exception):
        from ...storage import storage
        return (storage.get_model() or "unknown") + "@" + storage.get_provider()
    return "unknown"


# ─── calibration (Phase 0 — the driver) ───────────────────────────────

def save_calibration(model_key: str, data: dict) -> None:
    cal = addon_state.get(ADDON, "calibration", {}) or {}
    cal[model_key] = {**data, "ts": int(time.time())}
    addon_state.set_(ADDON, "calibration", cal)


def get_calibration(model_key: str | None = None) -> dict | None:
    key = model_key or current_model_key()
    cal = addon_state.get(ADDON, "calibration", {}) or {}
    return cal.get(key)


def derive_layout(peak_ratio: float) -> dict:
    """Turn the measured peak into the run shape.

    ``peak_ratio`` is peak_window / total (0..1). Smaller peak → the model
    is only good in a tiny window → split the task into MORE parts so each
    part fits that small peak. Larger peak → fewer, bigger parts (almost all
    gold).
    """
    try:
        r = float(peak_ratio)
    except Exception:  # noqa: BLE001
        r = 0.2
    r = min(0.9, max(0.05, r))
    # parts ≈ 1 / peak_ratio, clamped to a sane 2..12 (12 = full agent budget).
    parts = int(round(1.0 / r))
    parts = min(12, max(2, parts))
    # non-gold quality levels to mine for patches: more when peak is tiny.
    levels = [80, 60, 40, 20] if r <= 0.25 else ([60, 30] if r <= 0.5 else [40])
    # How many graded alternatives each coder produces beyond its primary
    # (gold-candidate) version — the implementable form of the "100/80/60/40/20
    # segments". Tiny peak → mine more alternatives; big peak → almost all gold.
    alt_levels = len(levels)
    coders = 5
    # Default efficiency scale applied to coder rotation positions in Phase 2
    # (position 0 = peak). Calibration overrides this with the measured curve.
    eff_levels = [100, 80, 60, 40, 20][:coders]
    return {
        "peak_ratio": r,
        "parts": parts,
        "coders": coders,          # parallel fresh generations per part (round-robin)
        "patch_levels": levels,
        "alt_levels": alt_levels,  # graded alternatives per coder
        "eff_levels": eff_levels,  # position→efficiency scale (from the test)
        "mostly_gold": r > 0.5,    # 1/20-style: almost everything from gold
    }


# ─── priorities (Assembler → next run) ─────────────────────────────────

def save_priorities(parts_in_order: list[str]) -> None:
    addon_state.set_(ADDON, "priorities", list(parts_in_order or []))


def get_priorities() -> list[str]:
    v = addon_state.get(ADDON, "priorities", None)
    return list(v) if isinstance(v, list) else []


# ─── last run result ───────────────────────────────────────────────────

def save_last_result(payload: dict) -> None:
    addon_state.set_(ADDON, "last_result", {**payload, "ts": int(time.time())})


def get_last_result() -> dict:
    v = addon_state.get(ADDON, "last_result", None)
    return dict(v) if isinstance(v, dict) else {}


# ─── helpers ───────────────────────────────────────────────────────────

def extract_json(text: str):
    """Best-effort: pull the first JSON object/array out of an LLM reply."""
    if not text:
        return None
    import re
    # fenced ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        # first bare { .. } or [ .. ]
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        raw = m.group(1) if m else None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
