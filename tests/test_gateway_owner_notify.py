"""Фикс 1.3 (ТЗ §4.5) — уведомления владельцу о привязках.

* успешный ``verify_code`` → владелец получает сообщение с платформой,
  меткой, id и кнопкой «Отвязать»;
* 5-я неверная попытка (бан) → уведомление о переборе кодов;
* исключение при отправке НЕ ломает flow привязки — линк создаётся;
* ``gateway:unlink:*`` — рабочее действие, callback_data ≤ 64 байт.

Все тесты — без сети: ``_notify_owner`` мокается, addon-state — в RAM.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.gateway import identity


@pytest.fixture()
def mem_state(monkeypatch):
    """In-memory addon-state — тесты не пишут в data/state.json."""
    from bot.addons import state as addon_state

    store: dict = {}
    monkeypatch.setattr(
        addon_state, "get",
        lambda addon, key, default=None: store.get((addon, key), default),
    )
    monkeypatch.setattr(
        addon_state, "set_",
        lambda addon, key, value: store.__setitem__((addon, key), value),
    )
    return store


@pytest.fixture()
def notify_log(monkeypatch):
    """Мок ``_notify_owner`` — пишет вызовы в список."""
    calls: list[tuple[str, object]] = []

    async def fake_notify(text, reply_markup=None):
        calls.append((text, reply_markup))
        return True

    monkeypatch.setattr(identity, "_notify_owner", fake_notify)
    return calls


async def _drain_tasks() -> None:
    """Дать fire-and-forget задачам уведомлений отработать."""
    for _ in range(3):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# успешная привязка
# ---------------------------------------------------------------------------


async def test_verify_ok_notifies_owner_with_unlink_button(mem_state, notify_log):
    code = identity.generate_code(42, platform="discord")
    ok, _msg = identity.verify_code(code, "discord", "555", label="User#1")
    assert ok is True

    await _drain_tasks()

    assert len(notify_log) == 1
    text, markup = notify_log[0]
    assert "discord" in text
    assert "User#1" in text
    assert "555" in text
    buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "gateway:unlink:discord:555" in buttons


async def test_verify_ok_without_label_uses_external_id(mem_state, notify_log):
    code = identity.generate_code(42)
    ok, _ = identity.verify_code(code, "vk", "9001")
    assert ok is True

    await _drain_tasks()
    text, _markup = notify_log[0]
    assert "vk" in text and "9001" in text


# ---------------------------------------------------------------------------
# бан за перебор кодов
# ---------------------------------------------------------------------------


async def test_ban_notifies_owner(mem_state, notify_log):
    for _ in range(identity.MAX_FAILED_ATTEMPTS):
        ok, _ = identity.verify_code("000000", "discord", "777")
        assert ok is False

    await _drain_tasks()

    assert len(notify_log) == 1
    text, _markup = notify_log[0]
    assert "перебора" in text
    assert "777" in text
    assert "час" in text


# ---------------------------------------------------------------------------
# отказ отправки не ломает привязку
# ---------------------------------------------------------------------------


async def test_notify_exception_does_not_break_linking(mem_state, monkeypatch):
    async def broken_notify(text, reply_markup=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(identity, "_notify_owner", broken_notify)

    code = identity.generate_code(42)
    ok, _ = identity.verify_code(code, "discord", "555", label="User#1")
    assert ok is True

    await _drain_tasks()
    assert identity.resolve_user_id("discord", "555") == 42


async def test_sync_raising_notify_does_not_break_linking(mem_state, monkeypatch):
    def totally_broken(text, reply_markup=None):  # даже не корутина
        raise RuntimeError("boom")

    monkeypatch.setattr(identity, "_notify_owner", totally_broken)

    code = identity.generate_code(42)
    ok, _ = identity.verify_code(code, "discord", "556")
    assert ok is True
    assert identity.resolve_user_id("discord", "556") == 42


def test_verify_without_event_loop_still_links(mem_state, monkeypatch):
    """Sync-контекст (нет running loop) — уведомление молча скипается."""
    code = identity.generate_code(42)
    ok, _ = identity.verify_code(code, "discord", "557")
    assert ok is True
    assert identity.resolve_user_id("discord", "557") == 42


async def test_real_notify_never_raises_without_bot(mem_state, monkeypatch):
    """Настоящий ``_notify_owner`` без бота/владельца просто возвращает False."""
    monkeypatch.setattr(identity, "_bot", None)
    monkeypatch.setattr(identity, "_get_bot", lambda: None)
    assert await identity._notify_owner("hi") is False


# ---------------------------------------------------------------------------
# действие «Отвязать»
# ---------------------------------------------------------------------------


def test_unlink_prefix_registered_in_dispatch():
    from bot.gateway.dispatch import ACTION_PREFIX_HANDLERS

    assert identity.UNLINK_PREFIX in ACTION_PREFIX_HANDLERS


async def test_unlink_action_removes_link(mem_state, notify_log):
    code = identity.generate_code(42)
    ok, _ = identity.verify_code(code, "discord", "555")
    assert ok is True
    assert identity.resolve_user_id("discord", "555") == 42

    result = identity._handle_unlink_action("gateway:unlink:discord:555", None)
    assert result.text and "✅" in result.text
    assert identity.resolve_user_id("discord", "555") is None

    # повторная отвязка — вежливое сообщение, не исключение
    again = identity._handle_unlink_action("gateway:unlink:discord:555", None)
    assert again.text and "не найдена" in again.text


def test_unlink_action_fits_64_bytes_even_for_long_ids(mem_state):
    long_id = "x" * 120
    action = identity.unlink_action("discord", long_id)
    assert len(action.encode("utf-8")) <= identity.MAX_ACTION_BYTES
    assert action.startswith("gateway:unlink:ref:")
    # hash-ссылка разрешается обратно в исходную пару
    assert identity._resolve_unlink_target(action) == ("discord", long_id)


def test_unlink_action_short_ids_stay_readable():
    action = identity.unlink_action("vk", "12345")
    assert action == "gateway:unlink:vk:12345"
    assert identity._resolve_unlink_target(action) == ("vk", "12345")
