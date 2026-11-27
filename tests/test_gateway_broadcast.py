"""Этап 6 ТЗ-1 (R8) — broadcast уведомлений владельцу на платформы.

* dark launch: все флаги по умолчанию False, broadcast — no-op;
* схема хранения — строго ТЗ §3.5: ``broadcast = {"discord": bool, "vk": bool}``;
* доставка только на привязанные аккаунты владельца;
* сбой доставки не бросает исключений наружу;
* экран «Уведомления»: чекбоксы, тест-кнопка, только владелец;
* живой Статус: аптайм/ошибки адаптеров в ``gateway:status``.

Все тесты — без сети: addon-state в RAM, адаптеры мокаются.
"""

from __future__ import annotations

import pytest

from bot.gateway import broadcast
from bot.gateway.dispatch import GatewayContext, dispatch_action


@pytest.fixture()
def mem_state(monkeypatch):
    from bot.addons import state as addon_state

    store: dict = {}

    def fake_get(addon, key, default=None):
        return store.get((addon, key), default)

    def fake_set(addon, key, value):
        store[(addon, key)] = value

    monkeypatch.setattr(addon_state, "get", fake_get)
    monkeypatch.setattr(addon_state, "set_", fake_set)
    monkeypatch.setattr(broadcast.addon_state, "get", fake_get)
    monkeypatch.setattr(broadcast.addon_state, "set_", fake_set)
    return store


@pytest.fixture()
def as_admin(monkeypatch):
    """Единый источник прав: и диспетчер, и экраны (паттерн wave2b)."""
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: uid == 42)
    monkeypatch.setattr(broadcast, "_is_admin", lambda uid: uid == 42)
    from bot.gateway import dispatch
    monkeypatch.setattr(dispatch, "_is_owner", lambda uid: uid == 42)


@pytest.fixture()
def owner_links(monkeypatch):
    from bot.gateway import identity

    links = [
        {"platform": "discord", "external_id": "555", "label": "User#1"},
        {"platform": "vk", "external_id": "9001", "label": "vk user"},
    ]
    monkeypatch.setattr(identity, "get_links", lambda: list(links))
    return links


# ---------------------------------------------------------------------------
# флаги (dark launch + схема §3.5)
# ---------------------------------------------------------------------------


def test_flags_default_false(mem_state):
    assert broadcast.get_flags() == {"discord": False, "vk": False}


def test_set_flag_persists_schema(mem_state):
    broadcast.set_flag("discord", True)
    assert mem_state[("gateway", "broadcast")] == {"discord": True, "vk": False}


def test_set_flag_unknown_platform(mem_state):
    with pytest.raises(ValueError):
        broadcast.set_flag("icq", True)


# ---------------------------------------------------------------------------
# доставка
# ---------------------------------------------------------------------------


async def test_broadcast_noop_when_all_off(mem_state, owner_links, monkeypatch):
    called = []

    async def fake_dm(external_id, text):
        called.append(external_id)
        return True

    from bot.gateway.adapters import discord_gw
    monkeypatch.setattr(discord_gw, "send_dm", fake_dm)

    results = await broadcast.broadcast_to_owner("привет")
    assert results == {}
    assert called == []


async def test_broadcast_delivers_to_linked_only(mem_state, owner_links,
                                                 monkeypatch):
    sent = {"discord": [], "vk": []}

    async def fake_dm(external_id, text):
        sent["discord"].append((external_id, text))
        return True

    async def fake_vk(external_id, text):
        sent["vk"].append((external_id, text))
        return True

    from bot.gateway.adapters import discord_gw, vk_gw
    monkeypatch.setattr(discord_gw, "send_dm", fake_dm)
    monkeypatch.setattr(vk_gw, "send_text", fake_vk)

    broadcast.set_flag("discord", True)
    broadcast.set_flag("vk", True)
    results = await broadcast.broadcast_to_owner("тест")

    assert results == {"discord": True, "vk": True}
    assert sent["discord"] == [("555", "тест")]
    assert sent["vk"] == [("9001", "тест")]


async def test_broadcast_failure_never_raises(mem_state, owner_links,
                                              monkeypatch):
    async def boom(external_id, text):
        raise RuntimeError("adapter down")

    from bot.gateway.adapters import discord_gw
    monkeypatch.setattr(discord_gw, "send_dm", boom)
    broadcast.set_flag("discord", True)

    results = await broadcast.broadcast_to_owner("тест")
    assert results == {"discord": False}


async def test_notify_owner_triggers_broadcast(mem_state, owner_links,
                                               monkeypatch):
    """Врезка: identity._notify_owner дублирует текст в broadcast."""
    from bot.gateway import identity

    texts = []

    async def fake_broadcast(text):
        texts.append(text)
        return {}

    monkeypatch.setattr(broadcast, "broadcast_to_owner", fake_broadcast)
    monkeypatch.setattr(identity, "_get_bot", lambda: None)

    import asyncio
    await identity._notify_owner("событие")
    for _ in range(3):
        await asyncio.sleep(0)
    assert texts == ["событие"]


# ---------------------------------------------------------------------------
# экран «Уведомления»
# ---------------------------------------------------------------------------


def test_notify_screen_forbidden_for_guest(mem_state, as_admin):
    screen = broadcast.build_gateway_notify(None)
    assert "Только владелец" in screen.body


def test_notify_screen_shows_toggles(mem_state, as_admin):
    screen = broadcast.build_gateway_notify(42)
    actions = [b.action for row in screen.rows for b in row]
    assert "gateway:notify:toggle:discord" in actions
    assert "gateway:notify:toggle:vk" in actions
    assert "gateway:notify:test" in actions


async def test_toggle_via_dispatch(mem_state, as_admin):
    ctx = GatewayContext(platform="telegram", external_id="42", user_id=42)
    result = await dispatch_action("gateway:notify:toggle:discord", ctx)
    assert result.screen is not None
    assert broadcast.get_flags()["discord"] is True


async def test_toggle_forbidden_for_guest(mem_state, as_admin):
    ctx = GatewayContext(platform="discord", external_id="1", user_id=None)
    result = await dispatch_action("gateway:notify:toggle:vk", ctx)
    assert result.error == "forbidden"


# ---------------------------------------------------------------------------
# живой Статус (R10)
# ---------------------------------------------------------------------------


def test_status_screen_shows_adapter_diagnostics(mem_state, monkeypatch):
    import time

    from bot.gateway import screens
    from bot.gateway.adapters import discord_gw, vk_gw

    monkeypatch.setattr(screens, "_is_admin",
                        lambda uid: uid == 42, raising=False)
    monkeypatch.setattr(
        discord_gw, "get_status",
        lambda: {"state": "online", "since": time.time() - 300,
                 "error": None, "guilds": 2},
    )
    monkeypatch.setattr(
        vk_gw, "get_status",
        lambda: {"state": "error", "since": 0,
                 "error": "auth_failed", "guilds": 0},
    )
    screen = screens.build_gateway_status(42)
    assert "online" in screen.body
    assert "аптайм" in screen.body
    assert "auth_failed" in screen.body
