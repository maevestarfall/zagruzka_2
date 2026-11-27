"""Часть B — приёмочные тесты адаптера VK (этап 3 ТЗ).

Покрывает минимум из PART_B_VK.md:

1. Импорт модуля на голом Python + aiogram + aiohttp (без vkbottle —
   адаптер её и не использует: чистый Long Poll через aiohttp).
2. ``set_token`` / ``clear_token`` / ``get_status`` с подменённым
   ``addon_state``.
3. ``gateway:vk:settoken:<token>`` через ``dispatch_action``:
   гость → forbidden, владелец → токен сохранён + экран.
4. Все action экрана ``gateway:vk:live`` ≤ 64 байта.
5. Парсинг «код XXXXXX» — регистронезависимо, лишние пробелы.

Плюс: рендер VK-клавиатуры (лимит 10 кнопок + «продолжение в
Telegram»), маппинг результатов dispatch и подсказка для непривязанных.
"""

from __future__ import annotations

import asyncio
import json

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FakeAddonState:
    """In-memory drop-in for bot.addons.state (get / set_)."""

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
    """The adapter module with addon_state fully faked out."""
    import bot.gateway.adapters.vk_gw as vk_gw_mod

    fake = FakeAddonState()
    monkeypatch.setattr(vk_gw_mod, "addon_state", fake)
    yield vk_gw_mod, fake


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def event_loop_run():
    loop = asyncio.new_event_loop()
    yield lambda coro: loop.run_until_complete(coro)
    loop.close()


# ---------------------------------------------------------------------------
# 1. import without vkbottle / discord — bare Python + aiogram + aiohttp
# ---------------------------------------------------------------------------


def test_module_imports_without_vk_libraries():
    import sys

    assert "vkbottle" not in sys.modules  # library is not even referenced
    import bot.gateway.adapters.vk_gw as vk_gw_mod

    assert callable(vk_gw_mod.get_status)
    assert callable(vk_gw_mod.set_token)
    assert callable(vk_gw_mod.clear_token)
    assert asyncio.iscoroutinefunction(vk_gw_mod.start)
    assert asyncio.iscoroutinefunction(vk_gw_mod.stop)


# ---------------------------------------------------------------------------
# 2. set_token / clear_token / get_status с подменённым addon_state
# ---------------------------------------------------------------------------


def test_status_defaults_to_off(vk_gw):
    mod, _ = vk_gw
    status = mod.get_status()
    assert status["state"] == "off"
    assert status["error"] is None
    assert status["group"] is None


def test_set_and_clear_token(vk_gw):
    mod, fake = vk_gw
    mod.set_token("  vk1.a.secret-token-value  ")
    assert fake.get("gateway", "vk_token") == "vk1.a.secret-token-value"
    assert mod.get_token() == "vk1.a.secret-token-value"

    mod.clear_token()
    assert fake.get("gateway", "vk_token") is None
    assert mod.get_token() is None
    assert mod.get_status()["state"] == "off"


def test_token_is_never_logged_in_plain(vk_gw, caplog):
    mod, _ = vk_gw
    secret = "vk1.a.supersecrettokenvalue1234567890"
    with caplog.at_level("DEBUG"):
        mod.set_token(secret)
    assert secret not in caplog.text  # только маска, правило №4 README


def test_status_merges_partial_dict(vk_gw):
    mod, fake = vk_gw
    fake.set_("gateway", "vk_status", {"state": "online"})
    status = mod.get_status()
    assert status["state"] == "online"
    assert "error" in status and "group" in status and "since" in status


# ---------------------------------------------------------------------------
# 3. settoken через dispatch_action: гость → forbidden, владелец → ок
# ---------------------------------------------------------------------------


def test_settoken_guest_forbidden(vk_gw, monkeypatch, event_loop_run):
    mod, fake = vk_gw
    import bot.gateway.dispatch as dispatch_mod
    from bot.gateway.dispatch import GatewayContext, dispatch_action

    monkeypatch.setattr(dispatch_mod, "_is_owner", lambda uid: False)

    result = event_loop_run(
        dispatch_action(
            "gateway:vk:settoken:sometoken",
            GatewayContext(platform="web", external_id="x", user_id=None),
        )
    )
    assert result.error == "forbidden"
    assert fake.get("gateway", "vk_token") is None  # ничего не сохранено


def test_settoken_owner_saves_token_and_returns_screen(
    vk_gw, monkeypatch, event_loop_run
):
    mod, fake = vk_gw
    import bot.gateway.dispatch as dispatch_mod
    from bot.gateway.dispatch import GatewayContext, dispatch_action

    monkeypatch.setattr(dispatch_mod, "_is_owner", lambda uid: True)

    result = event_loop_run(
        dispatch_action(
            "gateway:vk:settoken:vk1.a.owner-token",
            GatewayContext(platform="web", external_id="1", user_id=42),
        )
    )
    assert result.error is None
    assert result.screen is not None
    assert result.screen.screen_id == "gateway:vk:live"
    assert fake.get("gateway", "vk_token") == "vk1.a.owner-token"


def test_settoken_empty_token_rejected(vk_gw, monkeypatch, event_loop_run):
    mod, fake = vk_gw
    import bot.gateway.dispatch as dispatch_mod
    from bot.gateway.dispatch import GatewayContext, dispatch_action

    monkeypatch.setattr(dispatch_mod, "_is_owner", lambda uid: True)

    result = event_loop_run(
        dispatch_action(
            "gateway:vk:settoken:",
            GatewayContext(platform="web", external_id="1", user_id=42),
        )
    )
    assert result.error == "bad_action"
    assert fake.get("gateway", "vk_token") is None


# ---------------------------------------------------------------------------
# 4. все action экрана gateway:vk:live ≤ 64 байта
# ---------------------------------------------------------------------------


def test_vk_live_screen_actions_fit_64_bytes(vk_gw):
    mod, _ = vk_gw
    from bot.gateway.menu import validate_screen

    # обе ветки: без токена и с токеном (разные наборы кнопок)
    for token in (None, "vk1.a.some-token"):
        if token:
            mod.set_token(token)
        screen = mod.build_vk_live_screen(42)
        assert validate_screen(screen) == []
        for row in screen.rows:
            for btn in row:
                assert len(btn.action.encode("utf-8")) <= 64


def test_vk_live_screen_registered(vk_gw):
    from bot.gateway.menu import SCREEN_REGISTRY

    assert "gateway:vk:live" in SCREEN_REGISTRY


def test_stage1_vk_stub_untouched():
    """Заглушку gateway:vk этапа 1 не трогаем (правило задания §4)."""
    from bot.gateway.menu import SCREEN_REGISTRY, build_screen
    import bot.gateway.screens as screens

    assert SCREEN_REGISTRY["gateway:vk"] is screens.build_gateway_vk
    stub = build_screen("gateway:vk", 42)
    assert stub is not None and stub.screen_id == "gateway:vk"


# ---------------------------------------------------------------------------
# 5. парсинг «код XXXXXX» — регистронезависимо, лишние пробелы
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("код 123456", "123456"),
        ("КОД 654321", "654321"),
        ("Код 000111", "000111"),
        ("  код   123456  ", "123456"),
        ("код\t987654", "987654"),
        ("код 12345", None),        # 5 цифр — мало
        ("код 1234567", None),      # 7 цифр — много
        ("код abcdef", None),
        ("123456", None),           # без слова «код»
        ("меню", None),
        ("", None),
    ],
)
def test_parse_code_command(text, expected):
    from bot.gateway.adapters.vk_gw import parse_code_command

    assert parse_code_command(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("меню", True),
        ("МЕНЮ", True),
        ("  Меню  ", True),
        ("меню пожалуйста", False),
        ("", False),
    ],
)
def test_is_menu_command(text, expected):
    from bot.gateway.adapters.vk_gw import is_menu_command

    assert is_menu_command(text) is expected


# ---------------------------------------------------------------------------
# «код …» / «меню» — сквозная логика без реального VK
# ---------------------------------------------------------------------------


def test_code_message_calls_verify_code(vk_gw, monkeypatch, event_loop_run):
    mod, _ = vk_gw
    calls = {}

    def fake_verify(code, platform, external_id, label=""):
        calls.update(code=code, platform=platform,
                     external_id=external_id, label=label)
        return True, "✅ Аккаунт привязан к владельцу."

    monkeypatch.setattr(mod.identity, "verify_code", fake_verify)
    text, keyboard = event_loop_run(
        mod.handle_text_message(1001, "КОД 123456", label="Вася")
    )
    assert calls == {
        "code": "123456", "platform": "vk",
        "external_id": "1001", "label": "Вася",
    }
    assert "привязан" in text
    assert keyboard is None


def test_menu_not_linked_gives_hint(vk_gw, monkeypatch, event_loop_run):
    mod, _ = vk_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: None)
    text, keyboard = event_loop_run(mod.handle_text_message(1001, "меню"))
    assert "код" in text.lower()
    assert keyboard is None


def test_menu_linked_renders_screen(vk_gw, monkeypatch, event_loop_run):
    mod, _ = vk_gw
    from bot.gateway.dispatch import ScreenResult
    from bot.gateway.menu import MenuButton, MenuScreen

    screen = MenuScreen(
        screen_id="main", title="Главное меню",
        rows=[[MenuButton(text="⚙️ Настройки", action="main:settings")]],
    )

    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)

    async def fake_dispatch(action, ctx):
        assert action == "main:menu"
        assert ctx.platform == "vk"
        assert ctx.external_id == "1001"
        assert ctx.user_id == 42
        return ScreenResult(screen=screen)

    monkeypatch.setattr(mod, "dispatch_action", fake_dispatch)
    text, keyboard = event_loop_run(mod.handle_text_message(1001, "Меню"))
    assert "Главное меню" in text
    assert keyboard is not None and keyboard["inline"] is True
    payload = json.loads(
        keyboard["buttons"][0][0]["action"]["payload"]
    )
    assert payload == {"action": "main:settings"}


def test_unknown_text_gives_usage_hint(vk_gw, monkeypatch, event_loop_run):
    """Адаптирован под фикс 2.4: свободный текст от НЕпривязанного
    даёт подсказку про привязку (а не общий usage-hint); привязанный
    теперь уходит в мозг — это покрыто test_gateway_brain_chat.py."""
    mod, _ = vk_gw
    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: None)
    text, keyboard = event_loop_run(mod.handle_text_message(1001, "привет"))
    assert "привязан" in text.lower() and "код" in text.lower()
    assert keyboard is None


# ---------------------------------------------------------------------------
# VK-клавиатура: лимит 10 кнопок + «продолжение в Telegram»
# ---------------------------------------------------------------------------


def test_keyboard_limit_10_buttons(vk_gw):
    """Адаптирован под фикс 1.4: большой экран теперь ПАГИНИРУЕТСЯ,
    а не усекается — лимит 10 кнопок соблюдается на каждой странице,
    truncated больше не взводится (нет патологии «ряд шире 5»)."""
    mod, _ = vk_gw
    from bot.gateway.menu import MenuButton, MenuScreen

    rows = [
        [MenuButton(text=f"Кнопка {i}", action=f"main:btn{i}")]
        for i in range(15)
    ]
    screen = MenuScreen(screen_id="big", title="Большой экран", rows=rows)
    keyboard, truncated = mod.screen_to_vk_keyboard(screen)
    total = sum(len(r) for r in keyboard["buttons"])
    assert total <= 10
    assert truncated is False  # пагинация вместо усечения
    assert "Telegram" not in mod._screen_text(screen, truncated)
    # служебный ряд навигации присутствует (страниц > 1)
    nav_labels = [b["action"]["label"] for b in keyboard["buttons"][-1]]
    assert any("стр." in lbl for lbl in nav_labels)


def test_keyboard_small_screen_not_truncated(vk_gw):
    mod, _ = vk_gw
    from bot.gateway.menu import MenuButton, MenuScreen

    screen = MenuScreen(
        screen_id="small", title="Мал", rows=[
            [MenuButton(text="A", action="main:a"),
             MenuButton(text="B", action="main:b")],
        ],
    )
    keyboard, truncated = mod.screen_to_vk_keyboard(screen)
    assert truncated is False
    assert sum(len(r) for r in keyboard["buttons"]) == 2


def test_keyboard_url_buttons_render_open_link(vk_gw):
    mod, _ = vk_gw
    from bot.gateway.menu import MenuButton, MenuScreen

    screen = MenuScreen(
        screen_id="s", title="t", rows=[
            [MenuButton(text="🔗 Сайт", action="", url="https://example.com")],
        ],
    )
    keyboard, _ = mod.screen_to_vk_keyboard(screen)
    btn = keyboard["buttons"][0][0]["action"]
    assert btn["type"] == "open_link"
    assert btn["link"] == "https://example.com"


# ---------------------------------------------------------------------------
# маппинг результатов dispatch → ответ VK (tg_only / forbidden / text)
# ---------------------------------------------------------------------------


def test_reply_maps_tg_only_with_deeplink(vk_gw, monkeypatch, event_loop_run):
    mod, _ = vk_gw
    from bot.gateway.dispatch import ScreenResult

    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: 42)

    async def fake_dispatch(action, ctx):
        return ScreenResult(error="tg_only", text="⏳ Только в Telegram.",
                            deeplink="https://t.me/lilush_bot")

    monkeypatch.setattr(mod, "dispatch_action", fake_dispatch)
    text, keyboard = event_loop_run(mod._reply_for_action("x:y", 1001))
    assert "Telegram" in text
    assert "https://t.me/lilush_bot" in text
    assert keyboard is None


def test_reply_maps_forbidden(vk_gw, monkeypatch, event_loop_run):
    mod, _ = vk_gw
    from bot.gateway.dispatch import ScreenResult

    monkeypatch.setattr(mod.identity, "resolve_user_id", lambda p, e: None)

    async def fake_dispatch(action, ctx):
        return ScreenResult(error="forbidden", text="⛔ Нельзя.")

    monkeypatch.setattr(mod, "dispatch_action", fake_dispatch)
    text, keyboard = event_loop_run(mod._reply_for_action("gateway:home", 1001))
    assert text == "⛔ Нельзя."
    assert keyboard is None


# ---------------------------------------------------------------------------
# payload из кнопки VK
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"action": "main:settings"}', "main:settings"),
        ({"action": "roles:menu"}, "roles:menu"),
        ('{"command": "start"}', None),
        ("not-json", None),
        (None, None),
        ("", None),
    ],
)
def test_extract_action(payload, expected):
    from bot.gateway.adapters.vk_gw import _extract_action

    assert _extract_action(payload) == expected


# ---------------------------------------------------------------------------
# start(): нет токена → статус off с понятной ошибкой, без исключений
# ---------------------------------------------------------------------------


def test_start_without_token_sets_off(vk_gw, event_loop_run):
    mod, _ = vk_gw
    event_loop_run(mod.start())
    status = mod.get_status()
    assert status["state"] == "off"
    assert not mod.is_running()


def test_stop_idempotent(vk_gw, event_loop_run):
    mod, _ = vk_gw
    event_loop_run(mod.stop())
    event_loop_run(mod.stop())
    assert not mod.is_running()
