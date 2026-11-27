"""Фикс 1.1 (ТЗ §4.4, R6) — FSM-ввод токена Discord/VK в Telegram.

Ключевые инварианты:

* состояние «жду токен» ставится ТОЛЬКО для владельца (can_admin);
* сообщение с токеном удаляется НЕМЕДЛЕННО — даже если токен кривой;
* валидный токен сохраняется через ``set_token`` (валидация — мок,
  сети в тестах нет), невалидный — остаёмся в состоянии;
* «Отмена» чистит состояние и возвращает live-экран платформы;
* токен никогда не цитируется в ответах бота.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# fakes (без запуска Dispatcher / без сети)
# ---------------------------------------------------------------------------


class FakeFSM:
    """Минимальный дублёр aiogram FSMContext."""

    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}
        self.cleared = False

    async def set_state(self, state) -> None:
        self.state = state

    async def get_state(self):
        return self.state

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}
        self.cleared = True


class FakeMessage:
    def __init__(self, text: str = "", user_id: int = 111) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.bot = SimpleNamespace()
        self.deleted = False
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))


class FakeQuery:
    def __init__(self, action: str, user_id: int = 111) -> None:
        self.data = action
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(user_id=user_id)
        self.bot = SimpleNamespace()
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str = "", **kwargs):
        self.answers.append((text, kwargs))


@pytest.fixture()
def wui(monkeypatch):
    """wizard_ui с включённым шлюзом и can_admin=True по умолчанию."""
    import bot.access as access
    import bot.gateway.screens as screens
    import bot.gateway.wizard_ui as wizard_ui

    monkeypatch.setattr(screens, "is_gateway_enabled", lambda: True)
    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    return wizard_ui


# ---------------------------------------------------------------------------
# enter_token: состояние ставится только для владельца
# ---------------------------------------------------------------------------


async def test_enter_token_denied_for_guest(wui, monkeypatch):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: False)
    query = FakeQuery("gateway:discord:enter_token", user_id=222)
    state = FakeFSM()

    await wui.on_gateway_callback(query, state)

    assert state.state is None
    assert state.data == {}
    assert query.answers, "guest must get an explicit refusal"
    assert query.answers[0][1].get("show_alert") is True


@pytest.mark.parametrize("platform", ["discord", "vk"])
async def test_enter_token_sets_state_for_owner(wui, platform):
    query = FakeQuery(f"gateway:{platform}:enter_token")
    state = FakeFSM()

    await wui.on_gateway_callback(query, state)

    assert state.state == wui.GatewayTokenInput.waiting_token
    assert state.data.get("gateway_token_platform") == platform
    # приглашение с предупреждением об удалении + кнопка «Отмена»
    assert query.message.edits, "prompt must replace the screen"
    text, kwargs = query.message.edits[0]
    assert "удалено" in text
    kb = kwargs.get("reply_markup")
    cancels = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"gateway:{platform}:cancel_token" in cancels


# ---------------------------------------------------------------------------
# сообщение с токеном: удаляется ВСЕГДА, до валидации
# ---------------------------------------------------------------------------


def _token_state(platform: str) -> FakeFSM:
    state = FakeFSM()
    state.state = "GatewayTokenInput:waiting_token"
    state.data = {"gateway_token_platform": platform}
    return state


async def test_token_message_deleted_even_when_invalid(wui, monkeypatch):
    from bot.gateway.adapters import discord_gw

    saved: list[str] = []

    async def fake_validate(token):
        return False, None, "Токен не принят Discord."

    monkeypatch.setattr(discord_gw, "validate_token", fake_validate)
    monkeypatch.setattr(discord_gw, "set_token", saved.append)

    message = FakeMessage(text="totally-invalid-token")
    state = _token_state("discord")

    await wui.on_token_message(message, state)

    assert message.deleted is True          # удалено ДО ответа об ошибке
    assert saved == []                      # ничего не сохранили
    assert state.cleared is False           # остаёмся в состоянии (повтор)
    assert message.answers, "error + retry hint expected"
    err_text = message.answers[0][0]
    assert "totally-invalid-token" not in err_text  # токен не цитируем


async def test_valid_discord_token_saved(wui, monkeypatch):
    from bot.gateway.adapters import discord_gw

    saved: list[str] = []
    tags: list[str] = []

    async def fake_validate(token):
        return True, "Лилуш#1234", None

    monkeypatch.setattr(discord_gw, "validate_token", fake_validate)
    monkeypatch.setattr(discord_gw, "set_token", saved.append)
    monkeypatch.setattr(discord_gw, "_set_bot_tag", tags.append)

    message = FakeMessage(text="  MTIz.super.secret  ")
    state = _token_state("discord")

    await wui.on_token_message(message, state)

    assert message.deleted is True
    assert saved == ["MTIz.super.secret"]
    assert tags == ["Лилуш#1234"]
    assert state.cleared is True
    assert message.answers, "live screen must be re-sent"
    for text, _ in message.answers:
        assert "MTIz.super.secret" not in text  # токен не цитируем


async def test_valid_vk_token_saved(wui, monkeypatch):
    import bot.gateway.wizard_ui as wizard_ui
    from bot.gateway.adapters import vk_gw

    saved: list[str] = []

    async def fake_validate(token):
        return True, "Моя группа", None

    monkeypatch.setattr(wizard_ui, "_validate_vk_token", fake_validate)
    monkeypatch.setattr(vk_gw, "set_token", saved.append)

    message = FakeMessage(text="vk1.a.community-token")
    state = _token_state("vk")

    await wui.on_token_message(message, state)

    assert message.deleted is True
    assert saved == ["vk1.a.community-token"]
    assert state.cleared is True


async def test_token_message_from_guest_is_dropped(wui, monkeypatch):
    """Гонка: состояние досталось не-владельцу → удалить и выйти."""
    import bot.access as access
    from bot.gateway.adapters import discord_gw

    monkeypatch.setattr(access, "can_admin", lambda uid: False)
    saved: list[str] = []
    monkeypatch.setattr(discord_gw, "set_token", saved.append)

    message = FakeMessage(text="whatever", user_id=999)
    state = _token_state("discord")

    await wui.on_token_message(message, state)

    assert message.deleted is True
    assert saved == []
    assert state.cleared is True


# ---------------------------------------------------------------------------
# «Отмена»
# ---------------------------------------------------------------------------


async def test_cancel_clears_state_and_returns_live_screen(wui, monkeypatch):
    import bot.gateway.wizard_ui as wizard_ui
    from bot.gateway.dispatch import ScreenResult
    from bot.gateway.menu import MenuScreen

    dispatched: list[str] = []

    async def fake_dispatch(action, ctx):
        dispatched.append(action)
        return ScreenResult(
            screen=MenuScreen(screen_id="gateway:discord:live",
                              title="🎮 Discord")
        )

    monkeypatch.setattr(wizard_ui, "dispatch_action", fake_dispatch)

    query = FakeQuery("gateway:discord:cancel_token")
    state = _token_state("discord")

    await wui.on_gateway_callback(query, state)

    assert state.cleared is True
    assert dispatched == ["gateway:discord:live"]
    assert query.message.edits, "live screen must be rendered back"


# ---------------------------------------------------------------------------
# кнопка «Подключить» на live-экранах (сторона рендера)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "screen_id,adapter_name,enter_action",
    [
        ("gateway:discord:live", "discord_gw", "gateway:discord:enter_token"),
        ("gateway:vk:live", "vk_gw", "gateway:vk:enter_token"),
    ],
)
def test_connect_button_shown_only_without_token(
    wui, monkeypatch, screen_id, adapter_name, enter_action
):
    import importlib

    from bot.gateway.menu import MenuScreen

    adapter = importlib.import_module(f"bot.gateway.adapters.{adapter_name}")
    screen = MenuScreen(screen_id=screen_id, title="x", rows=[])

    monkeypatch.setattr(adapter, "get_token", lambda: None)
    actions = [
        b.action for row in wui._augment_screen(screen).rows for b in row
    ]
    assert enter_action in actions

    monkeypatch.setattr(adapter, "get_token", lambda: "secret")
    actions = [
        b.action for row in wui._augment_screen(screen).rows for b in row
    ]
    assert enter_action not in actions


def test_enter_token_actions_fit_callback_limit():
    from bot.gateway.menu import MAX_ACTION_BYTES
    from bot.gateway.wizard_ui import CANCEL_TOKEN_ACTIONS, ENTER_TOKEN_ACTIONS

    for action in (*ENTER_TOKEN_ACTIONS, *CANCEL_TOKEN_ACTIONS):
        assert len(action.encode("utf-8")) <= MAX_ACTION_BYTES
