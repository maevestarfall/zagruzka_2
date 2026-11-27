# Context-relay Phase 0: calibration — measure the model's peak window.
# Co-authored with CoCo
"""Calibration is the TEST that drives everything (owner: «сначала тест, от
него делаем фазы»).

It runs a probe task, then a judge scores the answer segment-by-segment
(0–100) to build an efficiency CURVE — the closest faithful analog of the
owner's "measure quality each minute". From the curve we fix:

* ``curve``       — per-segment scores (the efficiency curve);
* ``peak_start`` / ``peak_end`` — the peak window (segment indices);
* ``peak_score``  — the peak quality;
* ``peak_ratio``  — peak_window / total → drives how finely the ТЗ is split;
* ``eff_levels``  — the descending efficiency scale applied to coder rotation
  positions in Phase 2 (position 0 = highest, i.e. the peak).

Honest note: an LLM doesn't degrade minute-by-minute inside one call; the
"segments" are slices of the single probe output, which is the implementable
form of the per-minute measurement.
"""

from __future__ import annotations

import logging

from . import store

logger = logging.getLogger(__name__)

_K_SEGMENTS = 5  # slices of the probe output ≈ the "minutes" of the test

_PROBE_TASK = (
    "Напиши функцию на Python `dedupe_keep_order(items)`, которая убирает "
    "дубликаты из списка, сохраняя порядок первого появления, и покрой её "
    "3 короткими тестами."
)

_CURVE_SYS = (
    "Ты — калибратор эффективности LLM. Тебе дают ЗАДАЧУ и ОДИН ответ модели. "
    f"Мысленно раздели ответ на {_K_SEGMENTS} равных последовательных частей и "
    "оцени полезность/качество КАЖДОЙ части от 0 до 100 (это кривая "
    "эффективности по ходу ответа). Верни СТРОГО JSON: "
    '{\"curve\": [<' + str(_K_SEGMENTS) + ' чисел 0..100>], \"note\": \"...\"}.'
)


def _peak_window(curve: list[int]) -> tuple[int, int, int]:
    """Return (peak_start, peak_end, peak_score): the contiguous window around
    the max whose scores stay within 80% of the peak."""
    if not curve:
        return 0, 0, 0
    peak_score = max(curve)
    pk = curve.index(peak_score)
    thr = 0.8 * peak_score
    start = pk
    while start - 1 >= 0 and curve[start - 1] >= thr:
        start -= 1
    end = pk
    while end + 1 < len(curve) and curve[end + 1] >= thr:
        end += 1
    return start, end, int(peak_score)


async def run_calibration(model_key: str | None = None) -> dict:
    """Run the probe, build the efficiency curve, fix the peak, save + return."""
    from ...agent import oneshot_chat

    key = model_key or store.current_model_key()

    answer = await oneshot_chat(
        "Ты — кодер. Реши задачу кратко и по делу.",
        _PROBE_TASK, purpose="relay-calib", temperature=0.3,
    )

    raw = await oneshot_chat(
        _CURVE_SYS,
        f"ЗАДАЧА:\n{_PROBE_TASK}\n\nОТВЕТ МОДЕЛИ:\n{answer[:6000]}",
        purpose="relay-calib-judge", temperature=0.0,
    )
    v = store.extract_json(raw) or {}
    curve = v.get("curve")
    if not isinstance(curve, list) or not curve:
        curve = [100, 80, 60, 40, 20]  # sane default shape
    curve = [max(0, min(100, int(round(float(x))))) for x in curve][:_K_SEGMENTS] or [100, 80, 60, 40, 20]

    peak_start, peak_end, peak_score = _peak_window(curve)
    peak_ratio = (peak_end - peak_start + 1) / len(curve)

    layout = store.derive_layout(peak_ratio)
    # eff_levels = the curve sorted high→low (the efficiency scale applied to
    # coder rotation positions in Phase 2), padded to the coder count.
    coders = int(layout.get("coders", 5))
    eff_levels = sorted(curve, reverse=True)
    while len(eff_levels) < coders:
        eff_levels.append(eff_levels[-1] if eff_levels else 20)
    layout["eff_levels"] = eff_levels[:coders]

    data = {
        "peak_ratio": layout["peak_ratio"],
        "peak_start": peak_start,
        "peak_end": peak_end,
        "peak_score": peak_score,
        "curve": curve,
        "layout": layout,
        "note": str(v.get("note", ""))[:300],
    }
    store.save_calibration(key, data)
    return {"model_key": key, **data}
