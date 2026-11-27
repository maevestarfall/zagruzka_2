"""Фикс 1.5 (этап 4 ТЗ) — тесты недостающих экранов волны 2 (wave 2b).

По образцу ``tests/test_gateway_wave2.py``:

1. Каждый builder возвращает MenuScreen с корректным screen_id/parent.
2. Все action всех экранов ≤ 64 байта UTF-8.
3. Owner-only экраны: гость → вежливый denied-экран, владелец → полный.
4. Mutating-действия зарегистрированы и меняют состояние (мок storage).
5. Ключи API / токены на экранах ЗАМАСКИРОВАНЫ (никогда целиком).
6. Деструктивная очистка памяти идёт через подтверждающий экран.
"""

from __future__ import annotations

import asyncio

import pytest

import bot.gateway.screens_wave2 as w2
import bot.gateway.screens_wave2b as w2b
from bot.gateway.dispatch import (
    ACTION_HANDLERS,
    ACTION_PREFIX_HANDLERS,
    GatewayContext,
    dispatch_action,
)
from bot.gateway.menu import (
    MAX_ACTION_BYTES,
    SCREEN_REGISTRY,
    build_screen,
    validate_screen,
)

WAVE2B_SCREENS = [
    "roles",
    "roles_pick",
    "memory",
    "memory_clear",
    "access",
    "terminal",
    "brains",
    "slack",
]

#: Screens whose content is owner-only (memory is per-user like in TG,
#: but its CLEAR confirmation screen is owner-only per the fix spec).
OWNER_ONLY_SCREENS = [
    "roles",
    "roles_pick",
    "memory_clear",
    "access",
    "terminal",
    "brains",
    "slack",
]

OWNER_ID = 111
GUEST_ID = 222


def _grant_admin(monkeypatch, value: bool) -> None:
    """Patch the single source of truth both wave2b and dispatch use."""
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: value)


def _all_buttons(screen):
    return [btn for row in screen.rows for btn in row]


# ---------------------------------------------------------------------------
# 1. Every screen builds for owner AND guest without exceptions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_id", WAVE2B_SCREENS)
def test_screen_builds_for_owner(monkeypatch, screen_id):
    _grant_admin(monkeypatch, True)
    screen = build_screen(screen_id, OWNER_ID)
    assert screen is not None
    assert screen.screen_id == screen_id
    assert screen.body  # never an empty screen
    assert screen.rows  # always at least «Назад»


@pytest.mark.parametrize("screen_id", WAVE2B_SCREENS)
def test_screen_builds_for_guest(monkeypatch, screen_id):
    _grant_admin(monkeypatch, False)
    screen = build_screen(screen_id, GUEST_ID)
    assert screen is not None
    assert screen.body


@pytest.mark.parametrize("screen_id", WAVE2B_SCREENS)
def test_screen_builds_for_anonymous(monkeypatch, screen_id):
    """user_id=None (не привязан) — тоже без исключений."""
    _grant_admin(monkeypatch, False)
    screen = build_screen(screen_id, None)
    assert screen is not None


def test_all_wave2b_screens_registered():
    for screen_id in WAVE2B_SCREENS:
        assert screen_id in SCREEN_REGISTRY


# ---------------------------------------------------------------------------
# 2. All actions fit into 64 bytes of UTF-8 (Telegram limit, §9А.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_id", WAVE2B_SCREENS)
@pytest.mark.parametrize("is_admin", [True, False])
def test_actions_fit_64_bytes(monkeypatch, screen_id, is_admin):
    _grant_admin(monkeypatch, is_admin)
    screen = build_screen(screen_id, OWNER_ID if is_admin else GUEST_ID)
    assert validate_screen(screen) == []
    for btn in _all_buttons(screen):
        assert len(btn.action.encode("utf-8")) <= MAX_ACTION_BYTES


def test_registered_action_strings_fit_64_bytes():
    actions = list(w2b._ACTION_TO_SCREEN) + [
        "roles:toggle",
        "role:reset",
        "memory:clear",
        "memory:clear:confirm",
        "term:guest:use",
        "term:guest:see",
    ]
    for action in actions:
        assert len(action.encode("utf-8")) <= MAX_ACTION_BYTES


def test_role_confirm_screen_actions_fit_64_bytes(monkeypatch):
    """Динамический confirm-экран роли тоже держит лимит payload."""
    from bot.persona import list_personas

    _grant_admin(monkeypatch, True)
    for p in list_personas():
        screen = w2b._build_role_confirm_screen(OWNER_ID, p.key)
        assert validate_screen(screen) == []


# ---------------------------------------------------------------------------
# 3. Every screen has a valid parent (an existing screen_id)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_id", WAVE2B_SCREENS)
def test_screen_parent_exists(monkeypatch, screen_id):
    _grant_admin(monkeypatch, True)
    screen = build_screen(screen_id, OWNER_ID)
    assert screen.parent in SCREEN_REGISTRY


# ---------------------------------------------------------------------------
# 4. Guests do not see owner-only content — polite refusal, «Назад» only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen_id", OWNER_ONLY_SCREENS)
def test_guest_gets_denial_screen(monkeypatch, screen_id):
    _grant_admin(monkeypatch, False)
    screen = build_screen(screen_id, GUEST_ID)
    assert screen.body == w2.OWNER_ONLY_TEXT
    buttons = _all_buttons(screen)
    assert len(buttons) == 1
    assert buttons[0].action == "main:menu"  # только «Назад»


@pytest.mark.parametrize("screen_id", OWNER_ONLY_SCREENS)
def test_owner_screen_has_no_owner_only_stub(monkeypatch, screen_id):
    _grant_admin(monkeypatch, True)
    screen = build_screen(screen_id, OWNER_ID)
    assert screen.body != w2.OWNER_ONLY_TEXT


def test_memory_screen_is_per_user_not_owner_only(monkeypatch):
    """Память — как в TG: свой экран у каждого привязанного пользователя."""
    _grant_admin(monkeypatch, False)
    screen = build_screen("memory", GUEST_ID)
    assert screen.body != w2.OWNER_ONLY_TEXT
    # Не привязан (user_id=None) → просьба привязать аккаунт, не память.
    anon = build_screen("memory", None)
    assert "привяжи" in anon.body


# ---------------------------------------------------------------------------
# A2 — keyboards / bodies are built from the TG source of truth
# ---------------------------------------------------------------------------


def _neutral_rows(screen):
    return [[(b.text, b.action or None, b.url) for b in row] for row in screen.rows]


def _tg_rows(markup):
    return [
        [(b.text, b.callback_data, b.url) for b in row]
        for row in markup.inline_keyboard
    ]


def test_roles_keyboard_matches_tg(monkeypatch):
    import bot.wizard as wizard

    _grant_admin(monkeypatch, True)
    screen = w2b.build_roles_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(wizard._kb_roles_menu())


def test_roles_pick_keyboard_matches_tg(monkeypatch):
    import bot.wizard as wizard

    _grant_admin(monkeypatch, True)
    screen = w2b.build_roles_pick_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(wizard._kb_role_picker())


def test_access_keyboard_matches_tg(monkeypatch):
    import bot.wizard as wizard

    _grant_admin(monkeypatch, True)
    screen = w2b.build_access_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(wizard._kb_access_menu())


def test_slack_keyboard_matches_tg(monkeypatch):
    from bot.addons.slack import handlers as sl

    _grant_admin(monkeypatch, True)
    screen = w2b.build_slack_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(sl._kb_main())


def test_memory_keyboard_matches_tg(monkeypatch):
    import bot.storage as storage_mod
    from bot.addons.memory import handlers as mem

    _grant_admin(monkeypatch, True)
    monkeypatch.setattr(
        storage_mod.storage,
        "get_history",
        lambda uid: [{"role": "user", "content": "hi"}],
    )
    screen = w2b.build_memory_screen(OWNER_ID)
    assert _neutral_rows(screen) == _tg_rows(mem._kb_screen(has_history=True))


# ---------------------------------------------------------------------------
# A8 — the ORIGINAL TG callbacks open neutral screens via dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action,screen_id", sorted(w2b._ACTION_TO_SCREEN.items()))
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
        "memory:set_limit",      # memory: ввод числа (FSM)
        "memory:files",          # memory: файловый менеджер
        "slack:connect",         # slack: ввод токена (FSM)
        "slack:prompt",          # slack: ввод промта (FSM)
        "slack:channels",        # slack: выбор канала (интерактив)
        "access:add",            # access: ввод user-id (FSM)
        "brain:openrouter",      # brains: ввод API-ключа (FSM)
        "role:info",             # roles: инфо-простыня в TG
    ):
        assert action not in ACTION_HANDLERS


# ---------------------------------------------------------------------------
# 5. Mutating actions: guest → forbidden, owner → one storage op + redraw
# ---------------------------------------------------------------------------

_MUTATING = [
    "roles:toggle",
    "role:reset",
    "role:pick:boss",
    "role:use:boss",
    "memory:clear",
    "memory:clear:confirm",
    "access:set:private",
    "term:guest:use",
    "term:guest:see",
    "brains:set:auto",
]


@pytest.mark.parametrize("action", _MUTATING)
def test_mutating_action_guest_forbidden(monkeypatch, action):
    _grant_admin(monkeypatch, False)
    ctx = GatewayContext(platform="web", user_id=GUEST_ID)
    result = asyncio.run(dispatch_action(action, ctx))
    assert result.error == "forbidden"


def test_roles_toggle_owner_flips_gate(monkeypatch):
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    flag = {"value": False}
    monkeypatch.setattr(
        storage_mod.storage, "get_role_gate_enabled", lambda: flag["value"]
    )
    monkeypatch.setattr(
        storage_mod.storage,
        "set_role_gate_enabled",
        lambda v: flag.__setitem__("value", bool(v)),
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("roles:toggle", ctx))
    assert result.error is None
    assert flag["value"] is True
    assert result.screen is not None and result.screen.screen_id == "roles"


def test_role_pick_opens_confirm_screen(monkeypatch):
    """Выбор роли — с подтверждением вторым экраном, как в TG."""
    _grant_admin(monkeypatch, True)
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("role:pick:boss", ctx))
    assert result.error is None
    assert result.screen is not None
    assert result.screen.screen_id == "role_confirm"
    actions = [b.action for row in result.screen.rows for b in row]
    assert "role:use:boss" in actions  # кнопка подтверждения


def test_role_use_owner_sets_override(monkeypatch):
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    calls = []
    monkeypatch.setattr(
        storage_mod.storage, "set_persona_override", lambda k: calls.append(k)
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("role:use:boss", ctx))
    assert result.error is None
    assert calls == ["boss"]  # ровно одна операция — как в TG
    assert result.screen is not None and result.screen.screen_id == "roles"


def test_role_use_unknown_key_rejected(monkeypatch):
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    calls = []
    monkeypatch.setattr(
        storage_mod.storage, "set_persona_override", lambda k: calls.append(k)
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("role:use:no_such_persona_xyz", ctx))
    assert result.error == "unknown_role"
    assert calls == []


def test_memory_clear_requires_confirmation(monkeypatch):
    """Деструктивное действие → сперва подтверждающий экран, БЕЗ очистки."""
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    calls = []
    monkeypatch.setattr(
        storage_mod.storage, "clear_history", lambda uid: calls.append(uid)
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("memory:clear", ctx))
    assert result.error is None
    assert result.screen is not None
    assert result.screen.screen_id == "memory_clear"
    assert calls == []  # ничего не удалено до подтверждения
    actions = [b.action for row in result.screen.rows for b in row]
    assert "memory:clear:confirm" in actions


def test_memory_clear_confirm_owner_clears(monkeypatch):
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    calls = []
    monkeypatch.setattr(
        storage_mod.storage, "clear_history", lambda uid: calls.append(uid)
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("memory:clear:confirm", ctx))
    assert result.error is None
    assert calls == [OWNER_ID]  # ровно одна операция — как в TG
    assert result.screen is not None and result.screen.screen_id == "memory"


def test_access_set_real_owner_only(monkeypatch):
    """Со-владелец (can_admin, но не is_owner) НЕ меняет режим — как в TG."""
    import bot.access as access
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    monkeypatch.setattr(access, "is_owner", lambda uid: False)
    calls = []
    monkeypatch.setattr(
        storage_mod.storage, "set_access_mode", lambda m: calls.append(m)
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("access:set:public", ctx))
    assert result.error == "forbidden"
    assert calls == []


def test_access_set_owner_changes_mode(monkeypatch):
    import bot.access as access
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    calls = []
    monkeypatch.setattr(
        storage_mod.storage, "set_access_mode", lambda m: calls.append(m)
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("access:set:public", ctx))
    assert result.error is None
    assert calls == ["public"]
    assert result.screen is not None and result.screen.screen_id == "access"


def test_access_set_unknown_mode_rejected(monkeypatch):
    import bot.access as access
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    monkeypatch.setattr(access, "is_owner", lambda uid: True)
    calls = []
    monkeypatch.setattr(
        storage_mod.storage, "set_access_mode", lambda m: calls.append(m)
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("access:set:banana", ctx))
    assert result.error == "unknown_mode"
    assert calls == []


@pytest.mark.parametrize(
    "action,kind", [("term:guest:use", "features"), ("term:guest:see", "visibility")]
)
def test_terminal_toggles_flip_pm_flag(monkeypatch, action, kind):
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    calls = []
    monkeypatch.setattr(
        storage_mod.storage,
        "pm_toggle_flag",
        lambda k, key: calls.append((k, key)) or True,
    )
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action(action, ctx))
    assert result.error is None
    assert calls == [(kind, "terminal")]
    assert result.screen is not None and result.screen.screen_id == "terminal"


def test_brains_set_owner_switches_mode(monkeypatch):
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)
    calls = []

    def _set(mode):
        if mode not in ("auto", "devin"):
            raise ValueError(mode)
        calls.append(mode)

    monkeypatch.setattr(storage_mod.storage, "set_brain", _set)
    ctx = GatewayContext(platform="web", user_id=OWNER_ID)
    result = asyncio.run(dispatch_action("brains:set:devin", ctx))
    assert result.error is None
    assert calls == ["devin"]
    assert result.screen is not None and result.screen.screen_id == "brains"

    result = asyncio.run(dispatch_action("brains:set:banana", ctx))
    assert result.error == "unknown_mode"
    assert calls == ["devin"]  # невалидный режим не записан


# ---------------------------------------------------------------------------
# Ключи / токены на экранах ЗАМАСКИРОВАНЫ (§9А.4)
# ---------------------------------------------------------------------------

_FAKE_KEY = "sk-or-v1-abcdef1234567890abcdef1234567890"


def test_brains_screen_masks_api_keys(monkeypatch):
    import bot.wizard as wizard

    _grant_admin(monkeypatch, True)
    monkeypatch.setattr(
        wizard,
        "_slot_config",
        lambda slot: ("openrouter", _FAKE_KEY, "", "gpt-x"),
    )
    screen = w2b.build_brains_screen(OWNER_ID)
    assert _FAKE_KEY not in screen.body  # никогда целиком
    assert w2b._mask(_FAKE_KEY) in screen.body  # но маска показана
    for btn in _all_buttons(screen):
        assert _FAKE_KEY not in btn.text and _FAKE_KEY not in btn.action


def test_slack_screen_masks_token(monkeypatch):
    from bot.addons.slack import credentials as sc
    from bot.addons.slack import handlers as sl

    _grant_admin(monkeypatch, True)
    token = "xoxb-1234567890-abcdefghijklmnop"
    monkeypatch.setattr(sl, "is_configured", lambda: True)
    monkeypatch.setattr(sl, "load_token", lambda: token)
    screen = w2b.build_slack_screen(OWNER_ID)
    assert token not in screen.body  # только маска
    assert sc.mask_token(token) in screen.body


def test_mask_never_reveals_short_tokens():
    assert w2b._mask("") == ""
    assert w2b._mask("abcd1234") == "…"
    masked = w2b._mask(_FAKE_KEY)
    assert len(masked) < len(_FAKE_KEY)
    assert _FAKE_KEY[8:-4] not in masked


# ---------------------------------------------------------------------------
# Degradation — a broken data source degrades the body, not the screen
# ---------------------------------------------------------------------------


def test_roles_screen_survives_broken_source(monkeypatch):
    import bot.wizard as wizard

    _grant_admin(monkeypatch, True)

    def _boom():
        raise RuntimeError("wizard down")

    monkeypatch.setattr(wizard, "_roles_screen_body", _boom)
    screen = w2b.build_roles_screen(OWNER_ID)
    assert screen is not None
    assert "недоступен" in screen.body
    assert validate_screen(screen) == []


def test_terminal_screen_survives_broken_source(monkeypatch):
    import bot.storage as storage_mod

    _grant_admin(monkeypatch, True)

    def _boom():
        raise RuntimeError("pm down")

    monkeypatch.setattr(storage_mod.storage, "pm_is_enabled", _boom)
    screen = w2b.build_terminal_screen(OWNER_ID)
    assert screen is not None
    assert "недоступен" in screen.body
    assert validate_screen(screen) == []


def test_screens_contain_no_html_markup(monkeypatch):
    """Тексты нейтральные: без <b>/<i> — их рендерит каждая платформа сама."""
    _grant_admin(monkeypatch, True)
    for screen_id in WAVE2B_SCREENS:
        screen = build_screen(screen_id, OWNER_ID)
        assert "<b>" not in screen.body
        assert "<i>" not in screen.body


def test_prefix_handlers_registered():
    for prefix in ("role:pick:", "role:use:", "access:set:", "brains:set:"):
        assert prefix in ACTION_PREFIX_HANDLERS
