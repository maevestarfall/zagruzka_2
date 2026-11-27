"""Tests for «🎞 Анализ динамики видео» (viddyn) settings + logic + wiring."""

from __future__ import annotations

import pytest


@pytest.fixture()
def reset_settings():
    from bot.addons.viddyn import store

    store.set_fine(store.FINE_DEFAULT)
    store.set_coarse(store.COARSE_DEFAULT)
    yield
    store.set_fine(store.FINE_DEFAULT)
    store.set_coarse(store.COARSE_DEFAULT)


def test_settings_get_set_and_clamp(reset_settings):
    from bot.addons.viddyn import store

    store.set_fine(2.0)
    assert store.get_fine() == 2.0
    store.set_fine(99.0)  # clamped to 5.0 max
    assert store.get_fine() == 5.0
    store.set_fine(0.1)  # clamped to 0.5 min
    assert store.get_fine() == 0.5
    store.set_coarse(10.0)
    assert store.get_coarse() == 10.0


def test_times_sampling_and_cap():
    from bot.addons.viddyn import analyzer

    # 10s @ 1s step → ~10 samples
    ts = analyzer._times(10.0, 1.0, cap=12)
    assert ts[0] == 0.0 and len(ts) <= 12
    # cap thins down
    ts2 = analyzer._times(100.0, 1.0, cap=12)
    assert len(ts2) == 12
    # zero duration → single frame at 0
    assert analyzer._times(0.0, 1.0, cap=12) == [0.0]


def test_classify_distinguishes_blur_from_dynamics():
    from bot.addons.viddyn.analyzer import FrameSample, _classify_coarse

    # real movement, sharp
    moving = [
        FrameSample(0, 0.0, "a", sharpness=300, motion_from_prev=0.0),
        FrameSample(1, 2.0, "b", sharpness=300, motion_from_prev=8.0),
    ]
    assert "реальная динамика" in _classify_coarse(moving)

    # blurry but nothing moving between samples → false-dynamics case
    blur = [
        FrameSample(0, 0.0, "a", sharpness=5, motion_from_prev=0.0),
        FrameSample(1, 2.0, "b", sharpness=5, motion_from_prev=0.2),
    ]
    assert "смаз без реальной динамики" in _classify_coarse(blur)

    # sharp + static
    static = [
        FrameSample(0, 0.0, "a", sharpness=300, motion_from_prev=0.0),
        FrameSample(1, 2.0, "b", sharpness=300, motion_from_prev=0.1),
    ]
    assert "статика" in _classify_coarse(static)


def test_build_report_has_sections():
    from bot.addons.viddyn.analyzer import AnalysisResult, FrameSample, build_report

    r = AnalysisResult(duration=6.0, fps=10.0, fine_interval=1.0, coarse_interval=5.0)
    r.fine = [FrameSample(0, 0.0, "x", prompt="a green box, side view")]
    r.coarse_verdict = "~5с: motion=8.0, sharp=300 → ✅ реальная динамика"
    r.narrative = "Камера статична, объект движется слева направо."
    out = build_report(r)
    assert "Анализ динамики видео" in out
    assert "Промты для воспроизведения" in out
    assert "реальная динамика" in out


def test_entry_buttons_present_in_both_screens():
    import bot.media_ui as media_ui
    from bot.addons.osint.handlers import _kb_home

    mv = [b.callback_data for r in media_ui._kb_video_prompt().inline_keyboard for b in r]
    ov = [getattr(b, "callback_data", None) for r in _kb_home().inline_keyboard for b in r]
    assert "viddyn:open" in mv
    assert "viddyn:open" in ov


def test_viddyn_keyboard_has_interval_choices():
    import bot.wizard as wizard

    cbs = [b.callback_data for r in wizard._kb_viddyn().inline_keyboard for b in r]
    assert any(c.startswith("viddyn:fine:") for c in cbs)
    assert any(c.startswith("viddyn:coarse:") for c in cbs)
