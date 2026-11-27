"""Tests for the priority addon store + system-prompt directive."""

from __future__ import annotations

import pytest


@pytest.fixture()
def clean_priority():
    from bot.addons import state as addon_state

    en = addon_state.get("priority", "enabled", None)
    md = addon_state.get("priority", "mode", None)
    addon_state.delete("priority", "enabled")
    addon_state.delete("priority", "mode")
    yield
    if en is not None:
        addon_state.set_("priority", "enabled", en)
    if md is not None:
        addon_state.set_("priority", "mode", md)


def test_directive_empty_when_disabled(clean_priority):
    from bot.addons.priority import priority_directive, is_enabled

    assert not is_enabled()
    assert priority_directive() == ""


def test_modes_and_directives(clean_priority):
    from bot.addons.priority import (
        MODES,
        priority_directive,
        set_enabled,
        set_mode,
    )

    set_enabled(True)
    set_mode("only_bot")
    d = priority_directive()
    assert "только" in d.lower() and "интернет" in d.lower()

    set_mode("instruction")
    d = priority_directive()
    assert "дерево" in d.lower()

    set_mode("bot_then_internet")
    d = priority_directive()
    assert d  # non-empty

    assert set(MODES) == {"only_bot", "bot_then_internet", "instruction"}


def test_directive_flows_into_system_prompt(clean_priority, monkeypatch, tmp_path):
    from bot.addons.priority import set_enabled, set_mode
    import bot.agent as agent

    set_enabled(True)
    set_mode("only_bot")
    prompt = agent._build_system_prompt()
    assert "Приоритет ответа" in prompt
