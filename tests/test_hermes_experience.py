"""Tests for the Hermes experience log + document builder."""

from __future__ import annotations

import pytest


@pytest.fixture()
def clean_hx():
    from bot.addons import state as addon_state

    snap = addon_state.get("hermes_experience", "entries", None)
    addon_state.set_("hermes_experience", "entries", [])
    yield
    addon_state.set_("hermes_experience", "entries", snap if snap is not None else [])


def test_record_and_count(clean_hx):
    from bot.addons import hermes_experience as hx

    assert hx.count() == 0
    hx.record("osint", "построен план", outcome="ok", lesson="works")
    hx.record("prompt_sources", "источник не открылся", outcome="fail",
              lesson="проверить список")
    assert hx.count() == 2
    assert len(hx.list_entries("osint")) == 1


def test_document_is_grounded_in_entries(clean_hx):
    from bot.addons import hermes_experience as hx

    # Empty doc states there's no experience yet.
    doc0 = hx.build_document()
    assert "опыт" in doc0.lower()

    hx.record("prompt_sources", "lexica не ответила", outcome="fail",
              lesson="нужен фолбэк")
    hx.record("prompt_sources", "lexica не ответила снова", outcome="fail",
              lesson="нужен фолбэк")
    doc = hx.build_document()
    # Strictly-from-experience disclaimer present.
    assert "не из" in doc.lower() or "опыт" in doc.lower()
    # Aggregated lesson with a 2x confirmation count.
    assert "нужен фолбэк" in doc
    assert "2×" in doc
    # Feature label rendered.
    assert "Источники промптов" in doc


def test_clear(clean_hx):
    from bot.addons import hermes_experience as hx

    hx.record("general", "x")
    assert hx.count() == 1
    n = hx.clear()
    assert n == 1 and hx.count() == 0
