"""Фикс 2.4 — «чат с мозгом» из Discord/VK (FIX_PART_2, задача B).

Критерии из задания (без сети, всё на моках):

* привязанный пользователь → ``ask_brain`` вызван и ответ отправлен;
* непривязанный → подсказка про привязку;
* команды («меню», «код …», ``!lilush …``, /link) в мозг НЕ уходят;
* ошибки мозга → вежливый текст, лог без текста пользователя.
"""

from __future__ import annotations

import asyncio
import logging
import types

import pytest


class FakeAddonState:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get(self, addon, key, default=None):
        return self.data.get((addon, key), default)

    def set_(self, addon, key, value):
        self.data[(addon, key)] = value

    def delete(self, addon, key):
        return self.data.pop((addon, key), None) is not None


@pytest.fixture()
def event_loop_run():
    loop = asyncio.new_event_loop()
    yield lambda coro: loop.run_until_complete(coro)
    loop.close()


@pytest.fixture()
def vk_gw(monkeypatch):
    import bot.gateway.adapters.vk_gw as vk_gw_mod

    monkeypatch.setattr(vk_gw_mod, "addon_state", FakeAddonState())
    yield vk_gw_mod


# ---------------------------------------------------------------------------
# bot.gateway.brain.ask_brain — обёртка над TG-пайплайном run_agent
# ---------------------------------------------------------------------------


def test_ask_brain_wraps_run_agent(monkeypatch, event_loop_run):
    from bot.gateway import brain
    import bot.storage as storage_mod
    import bot.agent as agent_mod

    monkeypatch.setattr(storage_mod.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage_mod.storage, "get_brain", lambda: "auto")
    monkeypatch.setattr(storage_mod.storage, "get_cwd", lambda uid: None)

    calls = {}

    async def fake_run_agent(user_id, text, cwd, **kwargs):
        calls.update(user_id=user_id, text=text, cwd=cwd)
        return "ответ мозга"

    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)

    answer = event_loop_run(brain.ask_brain(42, "привет"))
    assert answer == "ответ мозга"
    assert calls == {"user_id": 42, "text": "привет", "cwd": None}


def test_ask_brain_respects_disabled_bot(monkeypatch, event_loop_run):
    from bot.gateway import brain
    import bot.storage as storage_mod

    monkeypatch.setattr(storage_mod.storage, "is_enabled", lambda: False)
    answer = event_loop_run(brain.ask_brain(42, "привет"))
    assert answer == brain.DISABLED_TEXT


def test_ask_brain_respects_devin_brain(monkeypatch, event_loop_run):
    from bot.gateway import brain
    import bot.storage as storage_mod

    monkeypatch.setattr(storage_mod.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage_mod.storage, "get_brain", lambda: "devin")
    answer = event_loop_run(brain.ask_brain(42, "привет"))
    assert answer == brain.DEVIN_TEXT


# ---------------------------------------------------------------------------
# VK: handle_text_message — свободный текст
# ---------------------------------------------------------------------------


def _patch_brain(monkeypatch, module, answer="ответ мозга", error=None):
    """Подменить bot.gateway.brain.ask_brain и записать вызовы."""
    from bot.gateway import brain as brain_mod

    calls = []

    async def fake_ask(user_id, text):
        calls.append((user_id, text))
        if error is not None:
            raise error
        return answer

    monkeypatch.setattr(brain_mod, "ask_brain", fake_ask)
    return calls


def test_vk_linked_free_text_goes_to_brain(vk_gw, monkeypatch,
                                           event_loop_run):
    mod = vk_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    calls = _patch_brain(monkeypatch, mod)

    text, keyboard = event_loop_run(mod.handle_text_message(1001, "привет"))
    assert calls == [(42, "привет")]
    assert text == "ответ мозга"
    assert keyboard is None


def test_vk_unlinked_free_text_gives_link_hint(vk_gw, monkeypatch,
                                               event_loop_run):
    mod = vk_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: None)
    calls = _patch_brain(monkeypatch, mod)

    text, keyboard = event_loop_run(mod.handle_text_message(1001, "привет"))
    assert calls == []  # мозг не тронут
    assert "привязан" in text.lower()
    assert keyboard is None


def test_vk_commands_do_not_reach_brain(vk_gw, monkeypatch, event_loop_run):
    mod = vk_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    monkeypatch.setattr(
        mod.identity, "verify_code",
        lambda code, p, e, label="": (True, "✅ Привязано."),
    )

    from bot.gateway.dispatch import ScreenResult
    from bot.gateway.menu import MenuButton, MenuScreen

    async def fake_dispatch(action, ctx):
        return ScreenResult(screen=MenuScreen(
            screen_id="main", title="Меню",
            rows=[[MenuButton(text="A", action="main:a")]],
        ))

    monkeypatch.setattr(mod, "dispatch_action", fake_dispatch)
    calls = _patch_brain(monkeypatch, mod)

    event_loop_run(mod.handle_text_message(1001, "код 123456"))
    event_loop_run(mod.handle_text_message(1001, "меню"))
    assert calls == []  # ни код, ни «меню» в мозг не ушли


def test_vk_brain_error_gives_polite_text(vk_gw, monkeypatch, caplog,
                                          event_loop_run):
    mod = vk_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    secret_text = "секретное сообщение пользователя"
    _patch_brain(monkeypatch, mod, error=RuntimeError("boom"))

    with caplog.at_level(logging.ERROR):
        text, keyboard = event_loop_run(
            mod.handle_text_message(1001, secret_text)
        )
    assert "⚠️" in text
    assert keyboard is None
    assert secret_text not in caplog.text  # текст пользователя не в логах


# ---------------------------------------------------------------------------
# Discord: _handle_free_text / _handle_message
# ---------------------------------------------------------------------------


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


def _fake_message(content, guild=None, bot=False):
    author = types.SimpleNamespace(id=555, bot=bot)
    return types.SimpleNamespace(
        content=content, guild=guild, author=author, channel=FakeChannel(),
    )


@pytest.fixture()
def discord_gw():
    import bot.gateway.adapters.discord_gw as discord_gw_mod

    return discord_gw_mod


def test_discord_linked_dm_goes_to_brain(discord_gw, monkeypatch,
                                         event_loop_run):
    mod = discord_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    calls = _patch_brain(monkeypatch, mod)

    msg = _fake_message("привет")
    event_loop_run(mod._handle_message(msg))
    assert calls == [(42, "привет")]
    assert msg.channel.sent == ["ответ мозга"]


def test_discord_unlinked_dm_gives_link_hint(discord_gw, monkeypatch,
                                             event_loop_run):
    mod = discord_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: None)
    calls = _patch_brain(monkeypatch, mod)

    msg = _fake_message("привет")
    event_loop_run(mod._handle_message(msg))
    assert calls == []
    assert msg.channel.sent == [mod.LINK_HINT]


def test_discord_guild_free_text_ignored(discord_gw, monkeypatch,
                                         event_loop_run):
    """В серверных каналах свободный текст игнорируется — бот отвечает
    мозгом только в ЛС (иначе комментировал бы каждое сообщение)."""
    mod = discord_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    calls = _patch_brain(monkeypatch, mod)

    msg = _fake_message("привет", guild=object())
    event_loop_run(mod._handle_message(msg))
    assert calls == []
    assert msg.channel.sent == []


def test_discord_commands_do_not_reach_brain(discord_gw, monkeypatch,
                                             event_loop_run):
    mod = discord_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    monkeypatch.setattr(
        mod.identity, "verify_code",
        lambda code, p, e, label="": (True, "✅ Привязано."),
    )
    calls = _patch_brain(monkeypatch, mod)

    msg = _fake_message("!lilush link 123456")
    event_loop_run(mod._handle_message(msg))
    assert calls == []  # команда в мозг не ушла
    assert msg.channel.sent  # но ответ на команду отправлен


def test_discord_bot_author_ignored(discord_gw, monkeypatch,
                                    event_loop_run):
    mod = discord_gw
    calls = _patch_brain(monkeypatch, mod)

    msg = _fake_message("привет", bot=True)
    event_loop_run(mod._handle_message(msg))
    assert calls == []
    assert msg.channel.sent == []


def test_discord_brain_error_gives_polite_text(discord_gw, monkeypatch,
                                               caplog, event_loop_run):
    mod = discord_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    secret_text = "секретное сообщение пользователя"
    _patch_brain(monkeypatch, mod, error=RuntimeError("boom"))

    msg = _fake_message(secret_text)
    with caplog.at_level(logging.ERROR):
        event_loop_run(mod._handle_message(msg))
    assert msg.channel.sent == [mod.BRAIN_ERROR_TEXT]
    assert secret_text not in caplog.text


def test_discord_long_answer_is_chunked(discord_gw, monkeypatch,
                                        event_loop_run):
    mod = discord_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)
    _patch_brain(monkeypatch, mod, answer="x" * 4500)

    msg = _fake_message("привет")
    event_loop_run(mod._handle_message(msg))
    assert len(msg.channel.sent) == 3
    assert all(len(part) <= 2000 for part in msg.channel.sent)
