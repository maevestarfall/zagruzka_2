"""Tests for the per-feature guest gate (the «Функции» / «Что они видят» wiring).

:mod:`bot.feature_gate` is the layer that finally makes the per-feature toggles
do something: it resolves which feature an incoming update invokes, blocks it
when ``guest_can_use`` is False (USE), and hides menu buttons when
``guest_can_see`` is False (SEE). Owners/co-owners and non-private bots are
never affected.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

import bot.feature_gate as fg
from bot.private_mode import FEATURE_KEYS


# ---------------------------------------------------------------------------
# registry integrity
# ---------------------------------------------------------------------------
def test_every_feature_has_a_callback_route() -> None:
    # The registry must cover the whole private_mode catalogue, no extras.
    assert set(fg.FEATURE_CALLBACK_PREFIXES) == set(FEATURE_KEYS)
    assert set(fg.MENU_BUTTON_FEATURE.values()) <= set(FEATURE_KEYS)
    assert set(fg.FEATURE_COMMANDS) <= set(FEATURE_KEYS)


@pytest.mark.parametrize(
    "callback,expected",
    [
        ("coder:run", "coding"),
        ("main:coder", "coding"),
        ("analyzer:menu", "analysis"),
        ("main:terminal", "terminal"),
        ("tts:on", "tts"),
        ("mb:check", "mailbox"),
        ("osint:ai", "osint"),
        ("main:masterroom", "github"),
        ("psrc:home", "promptlab"),
        ("dump:send", "cleanup"),
        ("memory:files", "memory"),
        # not gated features → None (plain chat / admin / settings plumbing)
        ("main:settings", None),
        ("roles:menu", None),
        ("access:menu", None),
        (None, None),
    ],
)
def test_resolve_feature_from_callback(callback, expected) -> None:
    assert fg.resolve_feature(callback, None) == expected


def test_resolve_feature_from_command() -> None:
    assert fg.resolve_feature(None, "/prompt список") == "promptlab"
    assert fg.resolve_feature(None, "/промпт") == "promptlab"
    assert fg.resolve_feature(None, "/setup") is None
    assert fg.resolve_feature(None, "просто текст без слеша") is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _fresh_storage(monkeypatch):
    import bot.storage as S

    st = S.Storage(data_dir=Path(tempfile.mkdtemp()))
    st.set_owner_id(1)
    monkeypatch.setattr("bot.storage.storage", st)
    return st


class _User:
    def __init__(self, uid):
        self.id = uid


class _CallbackQuery:
    def __init__(self, uid, data):
        self.from_user = _User(uid)
        self.data = data
        self.answered = []

    async def answer(self, *a, **k):
        self.answered.append((a, k))


class _Update:
    def __init__(self, *, callback_query=None, message=None):
        self.callback_query = callback_query
        self.message = message
        self.pre_checkout_query = None


# ---------------------------------------------------------------------------
# USE gate middleware
# ---------------------------------------------------------------------------
def test_use_gate_noop_when_private_off(monkeypatch) -> None:
    _fresh_storage(monkeypatch)  # pm disabled by default
    mw = fg.build_feature_use_gate_middleware()
    calls = {"n": 0}

    async def handler(event, data):
        calls["n"] += 1
        return "passed"

    cq = _CallbackQuery(999, "coder:run")
    res = asyncio.run(mw(handler, _Update(callback_query=cq), {}))
    assert res == "passed" and calls["n"] == 1


def test_use_gate_blocks_guest_without_use_flag(monkeypatch) -> None:
    st = _fresh_storage(monkeypatch)
    st.pm_toggle_enabled()  # private layer ON, no feature flags set
    mw = fg.build_feature_use_gate_middleware()
    calls = {"n": 0}

    async def handler(event, data):
        calls["n"] += 1
        return "passed"

    cq = _CallbackQuery(999, "coder:run")
    res = asyncio.run(mw(handler, _Update(callback_query=cq), {}))
    assert res is None
    assert calls["n"] == 0          # handler never ran
    assert cq.answered              # guest got the «недоступно» alert


def test_use_gate_allows_guest_with_use_flag(monkeypatch) -> None:
    st = _fresh_storage(monkeypatch)
    st.pm_toggle_enabled()
    st.pm_toggle_flag("features", "coding")   # USE coder on, no payment configured
    mw = fg.build_feature_use_gate_middleware()
    calls = {"n": 0}

    async def handler(event, data):
        calls["n"] += 1
        return "passed"

    cq = _CallbackQuery(999, "coder:run")
    res = asyncio.run(mw(handler, _Update(callback_query=cq), {}))
    assert res == "passed" and calls["n"] == 1


def test_use_gate_owner_always_passes(monkeypatch) -> None:
    st = _fresh_storage(monkeypatch)
    st.pm_toggle_enabled()  # nothing enabled for guests
    mw = fg.build_feature_use_gate_middleware()
    calls = {"n": 0}

    async def handler(event, data):
        calls["n"] += 1
        return "passed"

    cq = _CallbackQuery(1, "coder:run")  # owner id
    res = asyncio.run(mw(handler, _Update(callback_query=cq), {}))
    assert res == "passed" and calls["n"] == 1


def test_use_gate_passes_unknown_feature(monkeypatch) -> None:
    st = _fresh_storage(monkeypatch)
    st.pm_toggle_enabled()
    mw = fg.build_feature_use_gate_middleware()
    calls = {"n": 0}

    async def handler(event, data):
        calls["n"] += 1
        return "passed"

    # settings / plain admin callbacks are not gated features → pass through
    cq = _CallbackQuery(999, "main:settings")
    res = asyncio.run(mw(handler, _Update(callback_query=cq), {}))
    assert res == "passed" and calls["n"] == 1


def test_use_gate_requires_balance_when_paid(monkeypatch) -> None:
    st = _fresh_storage(monkeypatch)
    st.pm_toggle_enabled()
    st.pm_toggle_flag("features", "coding")
    st.pm_toggle_method("stars")     # paid access configured → also needs balance
    mw = fg.build_feature_use_gate_middleware()
    calls = {"n": 0}

    async def handler(event, data):
        calls["n"] += 1
        return "passed"

    cq = _CallbackQuery(999, "coder:run")
    assert asyncio.run(mw(handler, _Update(callback_query=cq), {})) is None
    st.pm_add_balance(999, 10.0)
    cq2 = _CallbackQuery(999, "coder:run")
    assert asyncio.run(mw(handler, _Update(callback_query=cq2), {})) == "passed"


# ---------------------------------------------------------------------------
# SEE menu filter
# ---------------------------------------------------------------------------
def _rows():
    from aiogram.types import InlineKeyboardButton as B
    return [
        [B(text="Coder", callback_data="main:coder")],
        [B(text="OSINT", callback_data="main:osint")],
        [B(text="Settings", callback_data="main:settings")],
    ]


def _cbs(rows):
    return [b.callback_data for row in rows for b in row]


def test_see_filter_noop_when_private_off(monkeypatch) -> None:
    _fresh_storage(monkeypatch)
    assert _cbs(fg.filter_menu_rows(_rows(), 999)) == [
        "main:coder", "main:osint", "main:settings"
    ]


def test_see_filter_hides_unseen_features_for_guest(monkeypatch) -> None:
    st = _fresh_storage(monkeypatch)
    st.pm_toggle_enabled()  # nothing visible for guests
    kept = _cbs(fg.filter_menu_rows(_rows(), 999))
    assert kept == ["main:settings"]  # only the non-feature button survives


def test_see_filter_respects_visibility_flag(monkeypatch) -> None:
    st = _fresh_storage(monkeypatch)
    st.pm_toggle_enabled()
    st.pm_toggle_flag("visibility", "coding")
    kept = _cbs(fg.filter_menu_rows(_rows(), 999))
    assert kept == ["main:coder", "main:settings"]


def test_see_filter_owner_sees_everything(monkeypatch) -> None:
    st = _fresh_storage(monkeypatch)
    st.pm_toggle_enabled()
    kept = _cbs(fg.filter_menu_rows(_rows(), 1))  # owner
    assert kept == ["main:coder", "main:osint", "main:settings"]
