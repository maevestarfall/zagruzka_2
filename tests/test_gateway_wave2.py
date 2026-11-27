"""Часть C (этап 4 ТЗ) — тесты покрытия экранов волн 2-3.

Минимум из PART_C_SCREENS.md:

1. Каждый новый экран строится для владельца и для гостя без исключений.
2. Все action всех новых экранов ≤ 64 байта UTF-8.
3. У каждого экрана корректный ``parent`` (существующий screen_id).
4. Гость не видит owner-only экраны (вежливый отказ, только «Назад»).
5. Действия волны 3: гость → forbidden, владелец → работает
   (с подменённым storage / store).

Плюс A2/A8: bridge-actions (``main:tokens`` и т.д.) открывают экран
через ``dispatch_action`` вместо заглушки tg_only, а клавиатуры
нейтральных экранов совпадают 1:1 с TG-версиями.
"""

from __future__ import annotations

import asyncio

import pytest

# Importing the module registers everything (wave 2 screens + wave 3
# actions) through the public stage-1 APIs — exactly like screens.py.
import bot.gateway.screens_wave2 as w2
from bot.gateway.dispatch import ACTION_HANDLERS, GatewayContext, dispatch_action
from bot.gateway.menu import (
    MAX_ACTION_BYTES,
    SCREEN_REGISTRY,
    build_screen,
    validate_screen,
)

WAVE2_SCREENS = ["tokens", "debates", "promptbox", "github", "projmap", "misc"]

OWNER_ID = 111
GUEST_ID = 222


def _grant_admin(monkeypatch, value: bool) -> None:
    """Patch the single source of truth both wave2 and dispatch use."""
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: value)


def _all_buttons(screen):
    return [btn for row in screen.rows for btn in row]


# ---------------------------------------------------------------------------
# 1. Every screen builds for owner AND guest without exceptions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_id", WAVE2_SCREENS)
def test_screen_builds_for_owner(monkeypatch, screen_id):
    _grant_admin(monkeypatch, True)
    screen = build_screen(screen_id, OWNER_ID)
    assert screen is not None
    assert screen.screen_id == screen_id
    assert screen.body  # never an empty screen
    assert screen.rows  # always at least «Назад»


@pytest.mark.parametrize("screen_id", WAVE2_SCREENS)
def test_screen_builds_for_guest(monkeypatch, screen_id):
    _grant_admin(monkeypatch, False)
    screen = build_screen(screen_id, GUEST_ID)
    assert screen is not None
    assert screen.body


@pytest.mark.parametrize("screen_id", WAVE2_SCREENS)
def test_screen_builds_for_anonymous(monkeypatch, screen_id):
    """user_id=None (не привязан) — тоже без исключений."""
    _grant_admin(monkeypatch, False)
    screen = build_screen(screen_id, None)
    assert screen is not None


# ---------------------------------------------------------------------------
# 2. All actions fit into 64 bytes of UTF-8 (Telegram limit, §9А.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_id", WAVE2_SCREENS)
@pytest.mark.parametrize("is_admin", [True, False])
def test_actions_fit_64_bytes(monkeypatch, screen_id, is_admin):
    _grant_admin(monkeypatch, is_admin)
    screen = build_screen(screen_id, OWNER_ID if is_admin else GUEST_ID)
    assert validate_screen(screen) == []
    for btn in _all_buttons(screen):
        assert len(btn.action.encode("utf-8")) <= MAX_ACTION_BYTES


def test_registered_action_strings_fit_64_bytes():
    for action in list(w2._ACTION_TO_SCREEN) + ["gh:auto", "dump:clear"]:
        assert len(action.encode("utf-8")) <= MAX_ACTION_BYTES


# ---------------------------------------------------------------------------
# 3. Every screen has a valid parent (an existing screen_id)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_id", WAVE2_SCREENS)
def test_screen_parent_exists(monkeypatch, screen_id):
    _grant_admin(monkeypatch, True)
    screen = build_screen(screen_id, OWNER_ID)
    assert screen.parent == "main"
    assert screen.parent in SCREEN_REGISTRY


def test_all_wave2_screens_registered():
    for screen_id in WAVE2_SCREENS:
        assert screen_id in SCREEN_REGISTRY


# ---------------------------------------------------------------------------
# 4. Guests do not see owner-only content — polite refusal, «Назад» only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_id", WAVE2_SCREENS)
def test_guest_gets_denial_screen(monkeypatch, screen_id):
    _grant_admin(monkeypatch, False)
    screen = build_screen(screen_id, GUEST_ID)
    assert screen.body == w2.OWNER_ONLY_TEXT
    buttons = _all_buttons(screen)
    assert len(buttons) == 1
    assert buttons[0].action == "main:menu"  # только «Назад»


@pytest.mark.parametrize("screen_id", WAVE2_SCREENS)
def test_owner_screen_has_no_owner_only_stub(monkeypatch, screen_id):
    _grant_admin(monkeypatch, True)
    screen = build_screen(screen_id, OWNER_ID)
    assert screen.body != w2.OWNER_ONLY_TEXT


# ---------------------------------------------------------------------------
# A2 — keyboards are 1:1 with the TG source of truth
# ---------------------------------------------------------------------------


def _neutral_rows(screen):
    return [[(b.text, b.action or None, b.url) for b in row] for row in screen.rows]


def _tg_rows(markup):
    return [
        [(b.text, b.callback_data, b.url) for b in row]
        for row in markup.inline_keyboard
    ]


def test_github_keyboard_matches_tg(monkeypatch):
    import bot.wizard as wizard

    _grant_admin(monkeypatch, True)
    screen = w2.build_github_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(wizard._kb_github_menu())


def test_misc_keyboard_matches_tg(monkeypatch):
    import bot.wizard as wizard

    _grant_admin(monkeypatch, True)
    screen = w2.build_misc_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(wizard._kb_misc())


def test_promptbox_keyboard_matches_tg(monkeypatch):
    from bot.addons.promptbox import handlers as pb

    _grant_admin(monkeypatch, True)
    screen = w2.build_promptbox_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(pb._kb_home())


def test_debates_keyboard_matches_tg(monkeypatch):
    from bot.addons.debates import handlers as deb

    _grant_admin(monkeypatch, True)
    screen = w2.build_debates_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(deb._kb_home(bool(deb._servers())))


# ---------------------------------------------------------------------------
# A8 — the ORIGINAL TG callbacks open neutral screens via dispatch
# (instead of the «⏳ только в Telegram» stub)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action,screen_id", sorted(w2._ACTION_TO_SCREEN.items()))
def test_bridge_action_opens_screen(monkeypatch, action, screen_id):
    _grant_admin(monkeypatch, True)
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action(action, ctx))
    assert result.error != "tg_only"
    assert result.screen is not None
    assert result.screen.screen_id == screen_id


def test_interactive_dialogs_stay_tg_only():
    """FSM-кнопки НЕ регистрируем как действия — их глубина остаётся в TG."""
    for action in (
        "pb:add",                # promptbox: ввод промта (FSM)
        "pb:import",             # promptbox: загрузка файла (FSM)
        "pmg:import",            # projectmap: загрузка базы (FSM)
        "main:projmap:rebuild",  # фоновая перестройка карты
        "mr:gh:manage",          # github: интерактивное управление
        "deb:create",            # debates: мастер создания сервера
        "dump:send",             # dump: чтение мусора (пайплайн)
    ):
        assert action not in ACTION_HANDLERS


# ---------------------------------------------------------------------------
# 5. Wave 3 actions: guest → forbidden, owner → the same operation as TG
# ---------------------------------------------------------------------------


def test_gh_auto_guest_forbidden(monkeypatch):
    _grant_admin(monkeypatch, False)
    ctx = GatewayContext(platform="web", user_id=GUEST_ID)
    result = asyncio.run(dispatch_action("gh:auto", ctx))
    assert result.error == "forbidden"


def test_gh_auto_owner_toggles(monkeypatch):
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    flag = {"value": False}
    monkeypatch.setattr(
        storage_mod.storage, "get_github_auto_clone", lambda: flag["value"]
    )
    monkeypatch.setattr(
        storage_mod.storage,
        "set_github_auto_clone",
        lambda v: flag.__setitem__("value", bool(v)),
    )

    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("gh:auto", ctx))
    assert result.error is None
    assert flag["value"] is True  # выключено → включено, как в TG
    assert result.screen is not None and result.screen.screen_id == "github"

    result = asyncio.run(dispatch_action("gh:auto", ctx))
    assert flag["value"] is False  # и обратно


def test_dump_clear_unlinked_forbidden(monkeypatch):
    _grant_admin(monkeypatch, False)
    ctx = GatewayContext(platform="discord", user_id=None)  # не привязан
    result = asyncio.run(dispatch_action("dump:clear", ctx))
    assert result.error == "forbidden"


def test_dump_clear_owner_clears(monkeypatch):
    from bot.addons.dump import store as dump_store

    _grant_admin(monkeypatch, True)
    calls = []
    monkeypatch.setattr(dump_store, "clear", lambda uid: calls.append(uid) or 3)

    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("dump:clear", ctx))
    assert result.error is None
    assert calls == [OWNER_ID]  # ровно одна операция — как в TG
    assert "3" in (result.text or "")


def test_dump_clear_linked_guest_allowed_per_user(monkeypatch):
    """Корзина у каждого своя: привязанный гость чистит СВОЮ корзину."""
    import bot.storage as storage_mod
    from bot.addons.dump import store as dump_store

    _grant_admin(monkeypatch, False)
    # private mode off → feature gate пропускает (как в TG, где экран
    # dump доступен пользователю его собственной корзины)
    monkeypatch.setattr(storage_mod.storage, "pm_is_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(dump_store, "clear", lambda uid: calls.append(uid) or 0)

    ctx = GatewayContext(platform="vk", user_id=GUEST_ID)
    result = asyncio.run(dispatch_action("dump:clear", ctx))
    assert result.error is None
    assert calls == [GUEST_ID]


# ---------------------------------------------------------------------------
# Degradation — a broken data source degrades the body, not the screen
# ---------------------------------------------------------------------------


def test_tokens_screen_survives_broken_source(monkeypatch):
    import bot.token_tracker as tt

    _grant_admin(monkeypatch, True)

    def _boom():
        raise RuntimeError("stats backend down")

    monkeypatch.setattr(tt, "format_token_stats", _boom)
    screen = w2.build_tokens_screen(OWNER_ID)
    assert screen is not None
    assert "недоступна" in screen.body
    assert validate_screen(screen) == []


def test_screens_contain_no_html_markup(monkeypatch):
    """Тексты нейтральные: без <b>/<i> — их рендерит каждая платформа сама."""
    _grant_admin(monkeypatch, True)
    for screen_id in WAVE2_SCREENS:
        screen = build_screen(screen_id, OWNER_ID)
        assert "<b>" not in screen.body
        assert "<i>" not in screen.body
