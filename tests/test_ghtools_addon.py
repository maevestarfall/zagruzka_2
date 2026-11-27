"""Tests for «🧰 GitHub-инструменты» catalog + runners + wiring."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

# cv2 needs libGL.so.1 on Linux. In headless / CI environments where the
# shared library is absent, skip any test that tries to generate a test video.
def _cv2_available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False

_skip_no_cv2 = pytest.mark.skipif(
    not _cv2_available(),
    reason="cv2 not importable (missing libGL.so.1 or OpenCV not installed)",
)


def _moving_box_video() -> str:
    import cv2
    import numpy as np

    fd, p = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    vw = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
    for i in range(40):
        f = np.zeros((240, 320, 3), np.uint8)
        x = int(10 + (i / 40) * 270)
        cv2.rectangle(f, (x, 100), (x + 40, 140), (0, 255, 0), -1)
        vw.write(f)
    vw.release()
    return p


def test_catalog_integrity():
    from bot.addons.ghtools import catalog

    assert len(catalog.TOOLS) >= 5
    for t in catalog.TOOLS:
        assert t["repo"].startswith("https://github.com/")
        assert t["scenario"] and isinstance(t["scenario"], list)
        if t["runnable"]:
            assert t.get("runner")
    # the #1 best tool is integrated and marked builtin
    assert catalog.TOOLS_BY_KEY["opticalflow"].get("builtin") is True


@_skip_no_cv2
def test_host_runners_produce_output():
    from bot.addons.ghtools import runner

    p = _moving_box_video()
    try:
        for key in ("opticalflow", "blur", "pyscenedetect"):
            out = asyncio.run(runner.run_on_video(key, p))
            assert out and isinstance(out, str)
        # unknown runner degrades gracefully
        assert "не запускается" in asyncio.run(runner.run_on_video("nope", p))
    finally:
        os.unlink(p)


def test_list_keyboard_has_all_tools_and_back():
    import bot.wizard as wizard
    from bot.addons.ghtools import catalog

    cbs = [b.callback_data for r in wizard._kb_ghtools_list().inline_keyboard for b in r]
    for t in catalog.TOOLS:
        assert f"ghtools:tool:{t['key']}" in cbs
    assert "ghtools:back" in cbs


def test_tool_keyboard_run_vs_clone():
    import bot.wizard as wizard

    run_cbs = [b for r in wizard._kb_ghtools_tool("pyscenedetect").inline_keyboard for b in r]
    assert any(getattr(b, "callback_data", "") == "ghtools:run:pyscenedetect" for b in run_cbs)
    clone_cbs = [b for r in wizard._kb_ghtools_tool("raft").inline_keyboard for b in r]
    assert any(getattr(b, "callback_data", "") == "ghtools:clone:raft" for b in clone_cbs)


def test_entry_button_in_osint_and_video():
    import bot.media_ui as media_ui
    from bot.addons.osint.handlers import _kb_home

    mv = [b.callback_data for r in media_ui._kb_video_prompt().inline_keyboard for b in r]
    ov = [getattr(b, "callback_data", None) for r in _kb_home().inline_keyboard for b in r]
    assert "ghtools:open" in mv
    assert "ghtools:open" in ov


@_skip_no_cv2
def test_optical_flow_integrated_in_viddyn():
    from bot.addons.viddyn import analyzer

    p = _moving_box_video()
    try:
        r = asyncio.run(
            analyzer.analyze(
                p, fine_interval=1.0, coarse_interval=2.0, max_fine=12, max_coarse=12
            )
        )
        # flow metric is populated on at least one fine sample
        assert any(s.flow_from_prev > 0 for s in r.fine)
        # verdict uses the flow signal and finds real dynamics
        assert "flow=" in r.coarse_verdict
        assert "реальная динамика" in r.coarse_verdict
    finally:
        os.unlink(p)
