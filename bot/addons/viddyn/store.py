"""Settings for the «🎞 Анализ динамики видео» feature.

Two configurable sampling intervals:
* ``fine_interval``  — 0.5…5 s, the dense frame-by-frame pass (what changed
  in the frame, reproduction prompt per screenshot).
* ``coarse_interval`` — default 5 s (configurable), the sparse "is there
  real dynamics or just motion blur?" pass.

State lives under ``_settings.addons.viddyn.*``.
"""

from __future__ import annotations

ADDON = "viddyn"

FINE_DEFAULT = 1.0
COARSE_DEFAULT = 5.0
FINE_CHOICES = [0.5, 1.0, 2.0, 3.0, 5.0]
COARSE_CHOICES = [5.0, 10.0, 15.0, 30.0]

# Hard cap on how many frames we run Vision on, to keep cost/time sane.
MAX_FINE_FRAMES = 12
MAX_COARSE_FRAMES = 12


def get_fine() -> float:
    from .. import state as addon_state

    try:
        v = float(addon_state.get(ADDON, "fine_interval", FINE_DEFAULT))
    except (TypeError, ValueError):
        v = FINE_DEFAULT
    return min(max(v, 0.5), 5.0)


def set_fine(value: float) -> None:
    from .. import state as addon_state

    addon_state.set_(ADDON, "fine_interval", float(value))


def get_coarse() -> float:
    from .. import state as addon_state

    try:
        v = float(addon_state.get(ADDON, "coarse_interval", COARSE_DEFAULT))
    except (TypeError, ValueError):
        v = COARSE_DEFAULT
    return max(v, 1.0)


def set_coarse(value: float) -> None:
    from .. import state as addon_state

    addon_state.set_(ADDON, "coarse_interval", float(value))
