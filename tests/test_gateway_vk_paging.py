"""Фикс 1.4 — пагинация VK-клавиатуры (FIX_PART_2, задача A).

Критерии из задания:

* экран с 8 рядами → страница 0 = 5 рядов контента + служебный ряд;
* «➡️ Вперёд» → страница 1;
* переход на ЛЮБОЙ другой экран сбрасывает страницу на 0;
* все callback-действия пагинации ≤ 64 байта (короткая форма при
  переполнении);
* экраны ≤ 5 рядов (в лимитах VK) — БЕЗ служебного ряда;
* truncated остаётся только для патологии «один ряд шире 5 кнопок».

Все тесты без сети: addon_state подменён in-memory фейком, dispatch —
моками.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot.gateway.dispatch import ScreenResult
from bot.gateway.menu import MenuButton, MenuScreen


class FakeAddonState:
    """In-memory drop-in for bot.addons.state (get / set_ / delete)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get(self, addon, key, default=None):
        return self.data.get((addon, key), default)

    def set_(self, addon, key, value):
        self.data[(addon, key)] = value

    def delete(self, addon, key):
        return self.data.pop((addon, key), None) is not None


@pytest.fixture()
def vk_gw(monkeypatch):
    import bot.gateway.adapters.vk_gw as vk_gw_mod

    fake = FakeAddonState()
    monkeypatch.setattr(vk_gw_mod, "addon_state", fake)
    yield vk_gw_mod, fake


@pytest.fixture()
def event_loop_run():
    loop = asyncio.new_event_loop()
    yield lambda coro: loop.run_until_complete(coro)
    loop.close()


def _big_screen(n_rows: int = 8, screen_id: str = "big") -> MenuScreen:
    rows = [
        [MenuButton(text=f"Кнопка {i}", action=f"main:btn{i}")]
        for i in range(n_rows)
    ]
    return MenuScreen(screen_id=screen_id, title="Большой экран", rows=rows)


def _actions_of(keyboard: dict) -> list[str]:
    out = []
    for row in keyboard["buttons"]:
        for btn in row:
            payload = btn["action"].get("payload")
            if payload:
                out.append(json.loads(payload)["action"])
    return out


# ---------------------------------------------------------------------------
# рендер страниц
# ---------------------------------------------------------------------------


def test_page0_is_5_content_rows_plus_nav(vk_gw):
    mod, _ = vk_gw
    keyboard, truncated = mod.screen_to_vk_keyboard(_big_screen(8), page=0)
    assert truncated is False
    assert len(keyboard["buttons"]) == 6  # 5 контента + 1 служебный
    labels = [b["action"]["label"] for b in keyboard["buttons"][-1]]
    assert any("стр. 1/2" in lbl for lbl in labels)
    assert any("Вперёд" in lbl for lbl in labels)
    assert not any("Назад" in lbl for lbl in labels)  # первая страница


def test_forward_action_opens_page1(vk_gw):
    mod, _ = vk_gw
    kb0, _ = mod.screen_to_vk_keyboard(_big_screen(8), page=0)
    forward = [
        json.loads(b["action"]["payload"])["action"]
        for b in kb0["buttons"][-1]
        if "Вперёд" in b["action"]["label"]
    ][0]
    assert forward == "gateway:page:big:1"

    kb1, _ = mod.screen_to_vk_keyboard(_big_screen(8), page=1)
    assert len(kb1["buttons"]) == 4  # 3 оставшихся ряда + служебный
    labels = [b["action"]["label"] for b in kb1["buttons"][-1]]
    assert any("стр. 2/2" in lbl for lbl in labels)
    assert any("Назад" in lbl for lbl in labels)
    assert not any("Вперёд" in lbl for lbl in labels)  # последняя страница


def test_small_screen_has_no_nav_row(vk_gw):
    mod, _ = vk_gw
    screen = _big_screen(5, screen_id="small5")
    keyboard, truncated = mod.screen_to_vk_keyboard(screen)
    assert truncated is False
    assert len(keyboard["buttons"]) == 5
    for action in _actions_of(keyboard):
        assert not action.startswith("gateway:page:")


def test_every_page_respects_vk_hard_limits(vk_gw):
    mod, _ = vk_gw
    # плотный экран: 8 рядов по 3 кнопки = 24 кнопки
    rows = [
        [MenuButton(text=f"K{r}-{c}", action=f"main:k{r}x{c}")
         for c in range(3)]
        for r in range(8)
    ]
    screen = MenuScreen(screen_id="dense", title="Плотный", rows=rows)
    page = 0
    seen = 0
    while True:
        kb, _ = mod.screen_to_vk_keyboard(screen, page=page)
        assert len(kb["buttons"]) <= mod.VK_MAX_ROWS
        assert sum(len(r) for r in kb["buttons"]) <= mod.VK_MAX_INLINE_BUTTONS
        for row in kb["buttons"]:
            assert len(row) <= mod.VK_MAX_PER_ROW
        seen += sum(
            1 for r in kb["buttons"] for b in r
            if not json.loads(b["action"]["payload"])["action"].startswith(
                "gateway:page:")
        )
        labels = [b["action"]["label"] for r in kb["buttons"] for b in r]
        if not any("Вперёд" in lbl for lbl in labels):
            break
        page += 1
    assert seen == 24  # ни одна кнопка не потеряна — суть фикса 1.4


def test_out_of_range_page_is_clamped(vk_gw):
    mod, _ = vk_gw
    kb, _ = mod.screen_to_vk_keyboard(_big_screen(8), page=99)
    labels = [b["action"]["label"] for b in kb["buttons"][-1]]
    assert any("стр. 2/2" in lbl for lbl in labels)


# ---------------------------------------------------------------------------
# callback ≤ 64 байта (короткая форма)
# ---------------------------------------------------------------------------


def test_page_actions_fit_64_bytes(vk_gw):
    mod, _ = vk_gw
    for page in (0, 1):
        kb, _ = mod.screen_to_vk_keyboard(_big_screen(8), page=page)
        for action in _actions_of(kb):
            assert len(action.encode("utf-8")) <= 64


def test_long_screen_id_uses_short_form(vk_gw):
    mod, _ = vk_gw
    long_id = "gateway:" + "x" * 80
    screen = _big_screen(8, screen_id=long_id)
    kb, _ = mod.screen_to_vk_keyboard(screen, page=0)
    nav_actions = [
        a for a in _actions_of(kb) if a.startswith("gateway:page:")
    ]
    assert nav_actions
    for action in nav_actions:
        assert len(action.encode("utf-8")) <= 64
        # короткая форма: gateway:page:<n> — без screen_id
        assert action.split(":")[2].isdigit()


# ---------------------------------------------------------------------------
# per-peer состояние страницы + обработка gateway:page:... в _reply_for_action
# ---------------------------------------------------------------------------


def test_page_action_rerenders_stored_screen(vk_gw, monkeypatch,
                                             event_loop_run):
    mod, fake = vk_gw
    screen = _big_screen(8, screen_id="big")

    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)

    dispatched = []

    async def fake_dispatch(action, ctx):
        dispatched.append(action)
        return ScreenResult(screen=screen)

    monkeypatch.setattr(mod, "dispatch_action", fake_dispatch)

    text, kb = event_loop_run(
        mod._reply_for_action("gateway:page:big:1", 1001)
    )
    # экран перерендерен повторным dispatch по его screen_id
    assert dispatched == ["big"]
    labels = [b["action"]["label"] for b in kb["buttons"][-1]]
    assert any("стр. 2/2" in lbl for lbl in labels)
    # состояние сохранено per-peer
    assert fake.get("gateway", "vk_page:1001") == {
        "screen_id": "big", "page": 1,
    }


def test_short_form_uses_stored_screen_id(vk_gw, monkeypatch,
                                          event_loop_run):
    mod, fake = vk_gw
    screen = _big_screen(8, screen_id="big")
    fake.set_("gateway", "vk_page:1001", {"screen_id": "big", "page": 0})

    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)

    async def fake_dispatch(action, ctx):
        assert action == "big"
        return ScreenResult(screen=screen)

    monkeypatch.setattr(mod, "dispatch_action", fake_dispatch)

    text, kb = event_loop_run(
        mod._reply_for_action("gateway:page:1", 1001)
    )
    labels = [b["action"]["label"] for b in kb["buttons"][-1]]
    assert any("стр. 2/2" in lbl for lbl in labels)


def test_short_form_without_state_gives_polite_error(vk_gw, monkeypatch,
                                                     event_loop_run):
    mod, _ = vk_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    text, kb = event_loop_run(mod._reply_for_action("gateway:page:1", 1001))
    assert kb is None
    assert "меню" in text.lower()


def test_other_screen_resets_page_to_zero(vk_gw, monkeypatch,
                                          event_loop_run):
    mod, fake = vk_gw
    fake.set_("gateway", "vk_page:1001", {"screen_id": "big", "page": 1})

    other = MenuScreen(
        screen_id="settings", title="Настройки",
        rows=[[MenuButton(text="A", action="main:a")]],
    )
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)

    async def fake_dispatch(action, ctx):
        return ScreenResult(screen=other)

    monkeypatch.setattr(mod, "dispatch_action", fake_dispatch)

    event_loop_run(mod._reply_for_action("main:settings", 1001))
    assert fake.get("gateway", "vk_page:1001") == {
        "screen_id": "settings", "page": 0,
    }


# ---------------------------------------------------------------------------
# truncated — только для патологии «ряд шире 5 кнопок»
# ---------------------------------------------------------------------------


def test_truncated_only_for_pathological_wide_row(vk_gw):
    mod, _ = vk_gw
    wide = MenuScreen(
        screen_id="wide", title="Широкий",
        rows=[[MenuButton(text=f"W{i}", action=f"main:w{i}")
               for i in range(7)]],  # 7 > 5 в одном ряду
    )
    keyboard, truncated = mod.screen_to_vk_keyboard(wide)
    assert truncated is True
    assert len(keyboard["buttons"][0]) == 5
    assert "Telegram" in mod._screen_text(wide, truncated)
