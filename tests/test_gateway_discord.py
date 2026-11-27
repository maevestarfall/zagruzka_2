"""Часть A — тесты адаптера Discord (этап 2 ТЗ).

Покрывает минимум из PART_A_DISCORD.md:

1. Модуль импортируется без установленного ``discord.py`` (A10).
2. ``set_token`` / ``clear_token`` / ``get_status`` — с подменённым
   ``addon_state``.
3. ``settoken``-действие через ``dispatch_action``: гость → forbidden,
   владелец → токен сохранён и вернулся экран.
4. Рендер экрана: у ``gateway:discord:live`` все action ≤ 64 байта.
5. «Не привязан → подсказка /link» — чистой функцией, без Discord.

Плюс: лимиты Discord-раскладки (5×5, overflow → «продолжение в
Telegram»), реконнект-статусы, DM-only для кода привязки.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeState:
    """In-memory stand-in for bot.addons.state (как в test_gateway_stage1)."""

    def __init__(self):
        self.data: dict = {}

    def get(self, addon, key, default=None):
        return self.data.get((addon, key), default)

    def set_(self, addon, key, value):
        self.data[(addon, key)] = value


@pytest.fixture()
def gw():
    """Свежеимпортированный модуль адаптера."""
    import bot.gateway.adapters.discord_gw as module
    return module


@pytest.fixture()
def fake_state(monkeypatch, gw):
    fake = _FakeState()
    monkeypatch.setattr(gw, "addon_state", fake)
    return fake


@pytest.fixture()
def fake_identity_state(monkeypatch):
    from bot.gateway import identity

    fake = _FakeState()
    monkeypatch.setattr(identity, "addon_state", fake)
    return fake


# ---------------------------------------------------------------------------
# 1. Импорт без discord.py (A10)
# ---------------------------------------------------------------------------


def test_module_imports_without_discord_py(monkeypatch):
    """Модуль обязан импортироваться на голом Python + aiogram + aiohttp."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "discord" or name.startswith("discord."):
            raise ImportError("discord.py is not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    original = sys.modules.pop("bot.gateway.adapters.discord_gw", None)
    try:
        module = importlib.import_module("bot.gateway.adapters.discord_gw")
        assert module is not None
        assert callable(module.get_status)
    finally:
        # Интеграция: возвращаем исходный модуль, иначе dispatch-реестр
        # (settoken/forget-обработчики) ссылается на другой объект модуля,
        # чем фикстура gw в последующих тестах, — и monkeypatch state
        # перестаёт совпадать.
        if original is not None:
            sys.modules["bot.gateway.adapters.discord_gw"] = original
            original.register()  # ре-регистрация обратно на исходный модуль
            del module


def test_start_without_discord_py_sets_error_status(monkeypatch, gw, fake_state):
    """start() без библиотеки: статус error, никаких исключений наружу."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "discord" or name.startswith("discord."):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr(gw, "_client_task", None)
    gw.set_token("totally-secret-token")

    asyncio.run(gw.start())

    status = gw.get_status()
    assert status["state"] == "error"
    assert "discord.py не установлен" in (status["error"] or "")


def test_start_without_token_sets_error(monkeypatch, gw, fake_state):
    monkeypatch.setattr(gw, "_client_task", None)
    asyncio.run(gw.start())
    status = gw.get_status()
    assert status["state"] == "error"
    assert "токен" in (status["error"] or "").lower()


# ---------------------------------------------------------------------------
# 2. Хранение токена / статус
# ---------------------------------------------------------------------------


def test_token_roundtrip_and_status(gw, fake_state):
    assert gw.get_token() is None
    assert gw.get_status()["state"] == "off"

    gw.set_token("  abc123token  ")
    assert gw.get_token() == "abc123token"          # trimmed
    assert fake_state.get("gateway", "discord_token") == "abc123token"

    gw.clear_token()
    assert gw.get_token() is None
    assert gw.get_status()["state"] == "off"


def test_status_merges_defaults(gw, fake_state):
    fake_state.set_("gateway", "discord_status", {"state": "online"})
    status = gw.get_status()
    assert status["state"] == "online"
    # недостающие ключи дозаполняются
    assert set(status) >= {"state", "error", "guilds", "since"}


# ---------------------------------------------------------------------------
# 3. settoken через dispatch_action (гейтинг делает dispatch)
# ---------------------------------------------------------------------------


def _dispatch(action: str, *, user_id: int | None):
    from bot.gateway.dispatch import GatewayContext, dispatch_action

    ctx = GatewayContext(platform="web", external_id="x", user_id=user_id)
    return asyncio.run(dispatch_action(action, ctx))


def test_settoken_forbidden_for_guest(monkeypatch, gw, fake_state):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: False)
    result = _dispatch("gateway:discord:settoken:sekret", user_id=333)
    assert result.error == "forbidden"
    assert gw.get_token() is None  # токен НЕ сохранён


def test_settoken_owner_saves_token_and_returns_live_screen(
        monkeypatch, gw, fake_state):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    # Фикс 1.2: settoken валидирует токен через Discord API — мокаем сеть.
    async def _ok(token):
        return True, "lilush#0001", None

    monkeypatch.setattr(gw, "validate_token", _ok)
    result = _dispatch("gateway:discord:settoken:sekret-token-1", user_id=111)
    assert result.error is None
    assert result.screen is not None
    assert result.screen.screen_id == "gateway:discord:live"
    assert gw.get_token() == "sekret-token-1"
    assert gw.get_bot_tag() == "lilush#0001"


def test_settoken_invalid_token_not_saved(monkeypatch, gw, fake_state):
    """Фикс 1.2: 401 от Discord → токен НЕ сохраняется, ошибка с текстом ТЗ."""
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: True)

    async def _bad(token):
        return False, None, gw.INVALID_TOKEN_MSG

    monkeypatch.setattr(gw, "validate_token", _bad)
    result = _dispatch("gateway:discord:settoken:wrong-token", user_id=111)
    assert result.error == "bad_token"
    assert "не принят Discord" in (result.text or "")
    assert gw.get_token() is None


def test_validate_token_empty_returns_error(gw):
    ok, tag, err = asyncio.run(gw.validate_token("   "))
    assert ok is False
    assert tag is None
    assert err


def test_settoken_empty_token_rejected(monkeypatch, gw, fake_state):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    result = _dispatch("gateway:discord:settoken:", user_id=111)
    assert result.error == "bad_action"
    assert gw.get_token() is None


def test_forget_action_clears_token(monkeypatch, gw, fake_state):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    gw.set_token("to-be-forgotten")
    result = _dispatch("gateway:discord:forget", user_id=111)
    assert result.screen is not None
    assert gw.get_token() is None


# ---------------------------------------------------------------------------
# 4. Экран gateway:discord:live — регистрация и лимит 64 байта
# ---------------------------------------------------------------------------


def test_live_screen_registered_and_actions_fit_64_bytes(gw, fake_state):
    from bot.gateway import menu as gmenu

    assert "gateway:discord:live" in gmenu.SCREEN_REGISTRY
    screen = gmenu.build_screen("gateway:discord:live", 111)
    assert screen is not None
    assert gmenu.validate_screen(screen) == []


def test_live_screen_reflects_status(gw, fake_state):
    # без токена — нет кнопок управления, есть «Обновить» и «Назад»
    screen = gw.build_live_screen(111)
    actions = [b.action for row in screen.rows for b in row]
    assert "gateway:discord:start" not in actions
    assert "gateway:discord:live" in actions
    assert "gateway:home" in actions

    # с токеном и офф-статусом — кнопка «Запустить» и «Забыть токен»
    gw.set_token("tok-tok-tok-tok")
    screen = gw.build_live_screen(111)
    actions = [b.action for row in screen.rows for b in row]
    assert "gateway:discord:start" in actions
    assert "gateway:discord:forget" in actions

    # онлайн — кнопка «Остановить», число серверов в теле
    gw._set_status("online", guilds=7)
    screen = gw.build_live_screen(111)
    actions = [b.action for row in screen.rows for b in row]
    assert "gateway:discord:stop" in actions
    assert "7" in screen.body


def test_live_screen_never_leaks_full_token(gw, fake_state):
    token = "very-long-super-secret-discord-token-value"
    gw.set_token(token)
    screen = gw.build_live_screen(111)
    assert token not in screen.body  # только маска


# ---------------------------------------------------------------------------
# 5. «Не привязан → подсказка /link» + привязка кодом
# ---------------------------------------------------------------------------


def test_menu_for_unlinked_user_hints_link(gw, fake_identity_state):
    screen, hint = gw.menu_screen_for("55555")
    assert screen is None
    assert hint and "link" in hint


def test_menu_for_linked_user_returns_screen(monkeypatch, gw,
                                             fake_identity_state):
    import bot.access as access
    from bot.gateway import identity

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    code = identity.generate_code(111)
    ok, _ = identity.verify_code(code, "discord", "55555", label="Dis#1")
    assert ok

    screen, hint = gw.menu_screen_for("55555")
    assert hint is None
    assert screen is not None
    assert screen.screen_id == "main"


def test_handle_link_code_dm_only(gw, fake_identity_state):
    reply = gw.handle_link_code("123456", "55555", is_dm=False)
    assert "личны" in reply.lower()


def test_handle_link_code_wrong_code(gw, fake_identity_state):
    reply = gw.handle_link_code("000000", "55555", is_dm=True)
    assert "не подошёл" in reply.lower() or "заблокирован" in reply.lower()


def test_handle_link_code_success(gw, fake_identity_state):
    from bot.gateway import identity

    code = identity.generate_code(111)
    reply = gw.handle_link_code(code, "55555", label="Dis#1", is_dm=True)
    assert "привязан" in reply.lower()
    assert identity.resolve_user_id("discord", "55555") == 111


# ---------------------------------------------------------------------------
# Discord-раскладка: лимит 5×5, overflow → «продолжение в Telegram»
# ---------------------------------------------------------------------------


def test_screen_to_discord_respects_component_limits(gw):
    from bot.gateway.menu import MenuButton, MenuScreen

    rows = [[MenuButton(text=f"btn{i}", action=f"a:{i}")] for i in range(40)]
    screen = MenuScreen(screen_id="x", title="T", rows=rows)
    payload = gw.screen_to_discord(screen)

    assert payload["overflow"] is True
    assert len(payload["rows"]) <= gw.MAX_ROWS
    total = sum(len(r) for r in payload["rows"])
    assert total == gw.MAX_BUTTONS_TOTAL
    for row in payload["rows"]:
        assert len(row) <= gw.MAX_BUTTONS_PER_ROW
    assert "Telegram" in payload["content"]


def test_screen_to_discord_small_screen_no_overflow(gw):
    from bot.gateway.menu import MenuButton, MenuScreen

    rows = [
        [MenuButton(text="A", action="a:1"), MenuButton(text="B", action="a:2")],
        [MenuButton(text="Site", action="", url="https://example.com")],
    ]
    screen = MenuScreen(screen_id="x", title="T", body="body", rows=rows)
    payload = gw.screen_to_discord(screen)

    assert payload["overflow"] is False
    assert payload["rows"][0][0]["custom_id"] == "a:1"
    assert payload["rows"][0][2]["url"] == "https://example.com"
    assert payload["rows"][0][2]["custom_id"] is None
    assert "T" in payload["content"] and "body" in payload["content"]


# ---------------------------------------------------------------------------
# Парсер команд
# ---------------------------------------------------------------------------


def test_parse_command_variants(gw):
    assert gw._parse_command("!lilush link 123456") == ("link", "123456")
    assert gw._parse_command("!LILUSH MENU") == ("menu", "")
    assert gw._parse_command("!lilush") == ("menu", "")
    assert gw._parse_command("/link 654321") == ("link", "654321")
    assert gw._parse_command("/menu") == ("menu", "")
    assert gw._parse_command("hello world") is None
    assert gw._parse_command("") is None
    assert gw._parse_command("!lilush unknowncmd") is None


# ---------------------------------------------------------------------------
# Реконнект: экспоненциальная задержка ограничена 60с (константы)
# ---------------------------------------------------------------------------


def test_reconnect_backoff_constants(gw):
    assert gw.RECONNECT_BASE_DELAY == 1.0
    assert gw.RECONNECT_MAX_DELAY == 60.0
    # рост 1 → 2 → 4 → … и потолок
    delay = gw.RECONNECT_BASE_DELAY
    seen = []
    for _ in range(10):
        seen.append(delay)
        delay = min(delay * 2, gw.RECONNECT_MAX_DELAY)
    assert seen[0] == 1.0 and seen[1] == 2.0 and seen[-1] == 60.0
