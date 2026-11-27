"""Tests for the prompt_sources addon store + parsing helpers."""

from __future__ import annotations

import pytest


@pytest.fixture()
def clean_state():
    from bot.addons import state as addon_state

    snap = addon_state.get("prompt_sources", "sources_photo", None)
    snapv = addon_state.get("prompt_sources", "sources_video", None)
    addon_state.delete("prompt_sources", "sources_photo")
    addon_state.delete("prompt_sources", "sources_video")
    yield
    if snap is not None:
        addon_state.set_("prompt_sources", "sources_photo", snap)
    if snapv is not None:
        addon_state.set_("prompt_sources", "sources_video", snapv)


def test_defaults_seeded(clean_state):
    from bot.addons.prompt_sources import store

    for kind in store.KINDS:
        srcs = store.get_sources(kind)
        assert len(srcs) >= 3
        assert all(s.startswith("http") for s in srcs)


def test_add_remove_reset(clean_state):
    from bot.addons.prompt_sources import store

    before = len(store.get_sources("photo"))
    ok, msg = store.add_source("photo", "example-prompts.com")
    assert ok and msg == "https://example-prompts.com"
    assert len(store.get_sources("photo")) == before + 1
    # duplicate rejected
    ok2, _ = store.add_source("photo", "https://example-prompts.com")
    assert not ok2
    # bad url rejected
    ok3, _ = store.add_source("photo", "not a url")
    assert not ok3
    # remove the one we added (last index)
    assert store.remove_source("photo", before)
    assert len(store.get_sources("photo")) == before
    # reset restores defaults
    store.add_source("photo", "https://temp.example")
    store.reset_sources("photo")
    assert "https://temp.example" not in store.get_sources("photo")


def test_ai_json_parsing():
    from bot.addons.prompt_sources.ai import _coerce_variants, _parse_json_block

    raw = (
        "```json\n{\"variants\":[{\"kind\":\"site\",\"prompt\":\"a\"},"
        "{\"kind\":\"site\",\"prompt\":\"b\"},{\"kind\":\"site\",\"prompt\":\"c\"},"
        "{\"kind\":\"hybrid\",\"prompt\":\"d\"}]}\n```"
    )
    data = _parse_json_block(raw)
    assert data is not None
    variants, kinds = _coerce_variants(data)
    assert variants == ["a", "b", "c", "d"]
    assert kinds[-1] == "hybrid"
