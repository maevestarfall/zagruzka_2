"""Tests for the «🗑 Скинуть весь мусор» bin store + wiring."""

from __future__ import annotations

import pytest


@pytest.fixture()
def clean_bin():
    from bot.addons.dump import store

    uid = 999001
    store.clear(uid)
    yield uid
    store.clear(uid)


def test_add_count_clear(clean_bin):
    from bot.addons.dump import store

    uid = clean_bin
    assert store.count(uid) == 0
    n = store.add_item(uid, {"kind": store.KIND_TEXT, "text": "привет"})
    assert n == 1
    store.add_item(uid, {"kind": store.KIND_URL, "text": "https://x.com"})
    assert store.count(uid) == 2
    removed = store.clear(uid)
    assert removed == 2 and store.count(uid) == 0


def test_last_question_prefers_recent_text(clean_bin):
    from bot.addons.dump import store

    uid = clean_bin
    store.add_item(uid, {"kind": store.KIND_TEXT, "text": "первый"})
    store.add_item(uid, {"kind": store.KIND_PHOTO, "file_id": "f1"})
    store.add_item(uid, {"kind": store.KIND_TEXT, "text": "о чём это видео?"})
    assert store.last_question(uid) == "о чём это видео?"


def test_summary_lines_lists_items(clean_bin):
    from bot.addons.dump import store

    uid = clean_bin
    assert "пусто" in store.summary_lines(uid).lower()
    store.add_item(uid, {"kind": store.KIND_VIDEO, "file_name": "clip.mp4"})
    out = store.summary_lines(uid)
    assert "clip.mp4" in out and "🎬" in out


def test_menu_has_dump_button():
    import bot.wizard as wizard

    cbs = [
        b.callback_data
        for row in wizard._kb_main_after_claim(1).inline_keyboard
        for b in row
    ]
    assert "main:dump" in cbs


def test_dump_keyboard_actions():
    import bot.wizard as wizard

    cbs = [
        b.callback_data
        for row in wizard._kb_dump().inline_keyboard
        for b in row
    ]
    assert {"dump:send", "dump:clear", "dump:close"} <= set(cbs)


def test_capability_tree_mentions_dump():
    from bot.addons.priority.store import CAPABILITY_TREE

    assert "Скинуть весь мусор" in CAPABILITY_TREE
