"""Этап 1 ТЗ-1 — приёмочные тесты каркаса шлюза.

Ключевые инварианты (§6 плана тестирования):

* A2  — TG-клавиатуры, отрендеренные через нейтральный ``MenuScreen``,
  байт-в-байт совпадают с легаси ``_kb_main_after_claim`` /
  ``_kb_settings_menu`` (тот же текст, тот же callback_data, тот же
  порядок строк).
* R1  — «🌐 Платформы» видна только владельцу/со-владельцу.
* §9А.3 — все статические actions влезают в 64 байта callback_data.
* §4.7 — dispatch_action возвращает экран для screen-actions и
  вежливую заглушку для ещё не покрытых экранов.
* A9  — identity: link-код одноразовый, с TTL, привязка/отвязка работает.
"""

from __future__ import annotations

import asyncio

import pytest


def _tg_rows(markup) -> list[list[tuple[str, str | None, str | None]]]:
    """InlineKeyboardMarkup → [[(text, callback_data, url)]] for comparison."""
    return [
        [(b.text, b.callback_data, b.url) for b in row]
        for row in markup.inline_keyboard
    ]


# ---------------------------------------------------------------------------
# A2 — TG equivalence
# ---------------------------------------------------------------------------


def test_main_screen_matches_legacy_keyboard(monkeypatch):
    """Neutral «main» screen == legacy _kb_main_after_claim (admin view)."""
    import bot.access as access
    import bot.wizard as wizard
    from bot.gateway.menu import screen_to_tg
    from bot.gateway.screens import build_main_screen

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    monkeypatch.setattr(wizard, "can_admin", lambda uid: True)

    legacy = wizard._kb_main_after_claim(111)
    neutral = screen_to_tg(build_main_screen(111), 111)
    assert _tg_rows(neutral) == _tg_rows(legacy)


def test_main_screen_matches_legacy_keyboard_guest(monkeypatch):
    import bot.access as access
    import bot.wizard as wizard
    from bot.gateway.menu import screen_to_tg
    from bot.gateway.screens import build_main_screen

    monkeypatch.setattr(access, "can_admin", lambda uid: False)
    monkeypatch.setattr(wizard, "can_admin", lambda uid: False)

    legacy = wizard._kb_main_after_claim(222)
    neutral = screen_to_tg(build_main_screen(222), 222)
    assert _tg_rows(neutral) == _tg_rows(legacy)


def test_settings_screen_matches_legacy_keyboard(monkeypatch):
    import bot.access as access
    import bot.wizard as wizard
    from bot.gateway.menu import screen_to_tg
    from bot.gateway.screens import build_settings_screen

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    monkeypatch.setattr(wizard, "can_admin", lambda uid: True)

    legacy = wizard._kb_settings_menu(111)
    neutral = screen_to_tg(build_settings_screen(111), 111)
    assert _tg_rows(neutral) == _tg_rows(legacy)


# ---------------------------------------------------------------------------
# R1 — gateway button visibility
# ---------------------------------------------------------------------------


def test_gateway_button_admin_only(monkeypatch):
    import bot.access as access
    import bot.wizard as wizard
    from bot.gateway.screens import GATEWAY_BUTTON_TEXT

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    monkeypatch.setattr(wizard, "can_admin", lambda uid: True)
    texts = [
        b.text
        for row in wizard._kb_settings_menu(111).inline_keyboard
        for b in row
    ]
    assert GATEWAY_BUTTON_TEXT in texts

    monkeypatch.setattr(access, "can_admin", lambda uid: False)
    monkeypatch.setattr(wizard, "can_admin", lambda uid: False)
    texts = [
        b.text
        for row in wizard._kb_settings_menu(222).inline_keyboard
        for b in row
    ]
    assert GATEWAY_BUTTON_TEXT not in texts


def test_gateway_rollback_flag_hides_button(monkeypatch):
    import bot.access as access
    import bot.wizard as wizard
    from bot.gateway import screens as gs

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    monkeypatch.setattr(wizard, "can_admin", lambda uid: True)
    monkeypatch.setattr(gs, "is_gateway_enabled", lambda: False)
    texts = [
        b.text
        for row in wizard._kb_settings_menu(111).inline_keyboard
        for b in row
    ]
    assert gs.GATEWAY_BUTTON_TEXT not in texts


# ---------------------------------------------------------------------------
# §9А.3 — payload limits
# ---------------------------------------------------------------------------


def test_all_registered_screens_fit_payload_limits(monkeypatch):
    import bot.access as access
    from bot.gateway import menu as gmenu
    from bot.gateway import screens as gs  # noqa: F401 (registers builders)

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    for screen_id in gmenu.SCREEN_REGISTRY:
        screen = gmenu.build_screen(screen_id, 111)
        assert screen is not None, screen_id
        assert gmenu.validate_screen(screen) == [], screen_id


# ---------------------------------------------------------------------------
# §4.7 — dispatch
# ---------------------------------------------------------------------------


def _dispatch(action: str, *, platform: str = "web", user_id: int | None = 111):
    from bot.gateway.dispatch import GatewayContext, dispatch_action

    ctx = GatewayContext(platform=platform, external_id="x", user_id=user_id)
    return asyncio.run(dispatch_action(action, ctx))


def test_dispatch_screen_action_returns_screen(monkeypatch):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    result = _dispatch("main:settings")
    assert result.error is None
    assert result.screen is not None
    assert result.screen.screen_id == "settings"


def test_dispatch_unknown_screen_is_tg_only_stub(monkeypatch):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    # roles:menu мигрирован в волне 2b (фикс 1.5) — берём всё ещё
    # немигрированный TG-only экран.
    result = _dispatch("hermes:menu")  # not yet registered in gateway
    assert result.error == "tg_only"
    assert result.text  # вежливая заглушка с текстом


def test_dispatch_gateway_home_forbidden_for_guest(monkeypatch):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: False)
    result = _dispatch("gateway:home", user_id=333)
    assert result.error == "forbidden"


def test_dispatch_gateway_home_for_admin(monkeypatch):
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    result = _dispatch("gateway:home")
    assert result.error is None
    assert result.screen is not None
    assert result.screen.screen_id == "gateway:home"


# ---------------------------------------------------------------------------
# A9 — identity: one-time link codes + link/unlink
# ---------------------------------------------------------------------------


class _FakeState:
    """In-memory stand-in for bot.addons.state — keeps tests off state.json."""

    def __init__(self):
        self.data: dict = {}

    def get(self, addon, key, default=None):
        return self.data.get((addon, key), default)

    def set_(self, addon, key, value):
        self.data[(addon, key)] = value


@pytest.fixture()
def fake_identity_state(monkeypatch):
    from bot.gateway import identity

    fake = _FakeState()
    monkeypatch.setattr(identity, "addon_state", fake)
    return fake


def test_identity_link_code_flow(fake_identity_state):
    from bot.gateway import identity

    code = identity.generate_code(111)
    assert code and len(code) == 6

    # Consume: binds discord user to TG 111.
    ok, msg = identity.verify_code(code, "discord", "555", label="Dis#1")
    assert ok, msg
    assert identity.resolve_user_id("discord", "555") == 111

    # One-time: a second consume fails.
    ok2, _ = identity.verify_code(code, "discord", "666")
    assert not ok2

    # Unlink drops the binding.
    assert identity.unlink("discord", "555") is True
    assert identity.resolve_user_id("discord", "555") is None


def test_identity_expired_code(fake_identity_state, monkeypatch):
    from bot.gateway import identity

    monkeypatch.setattr(identity, "CODE_TTL_SECONDS", -1)  # instantly expired
    code = identity.generate_code(111)
    ok, _ = identity.verify_code(code, "vk", "777")
    assert not ok


def test_identity_bruteforce_ban(fake_identity_state):
    from bot.gateway import identity

    for _ in range(identity.MAX_FAILED_ATTEMPTS):
        ok, _ = identity.verify_code("000000", "vk", "999")
        assert not ok
    assert identity.is_banned("999")
    # Even a valid code is rejected while banned (R7-E3).
    code = identity.generate_code(111)
    ok, msg = identity.verify_code(code, "vk", "999")
    assert not ok and "час" in msg


def test_identity_active_code_limit(fake_identity_state):
    from bot.gateway import identity

    for _ in range(identity.MAX_ACTIVE_CODES):
        assert identity.generate_code(111) is not None
    assert identity.generate_code(111) is None  # limit of 3 reached


# ---------------------------------------------------------------------------
# Web adapter JSON contract
# ---------------------------------------------------------------------------


def test_web_adapter_json_contract(monkeypatch):
    import bot.access as access
    from bot.gateway.adapters.web import screen_json_for

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    payload = screen_json_for("settings", 111)
    assert payload is not None
    assert payload["screen_id"] == "settings"
    assert isinstance(payload["rows"], list) and payload["rows"]
    btn = payload["rows"][0][0]
    assert set(btn) == {"text", "action", "url"}

    assert screen_json_for("no_such_screen", 111) is None


# ---------------------------------------------------------------------------
# §9Б.6 — atomic state.json save + backup-fallback load
# ---------------------------------------------------------------------------


def test_storage_save_is_atomic_and_load_uses_backup(tmp_path):
    import json

    from bot.storage import Storage

    st = Storage(data_dir=tmp_path)
    st._state = {"k": "v1"}
    st._save()
    st._state = {"k": "v2"}
    st._save()

    # Main file has the latest data; backup keeps the previous good save.
    assert json.loads((tmp_path / "state.json").read_text())["k"] == "v2"
    assert json.loads((tmp_path / "state.json.bak").read_text())["k"] == "v1"
    # No leftover temp files.
    assert not (tmp_path / "state.json.tmp").exists()

    # Simulate a crash mid-write: corrupt the main file → _load falls
    # back to the backup instead of silently returning {} (§9Б.6).
    (tmp_path / "state.json").write_text('{"k": "v2', encoding="utf-8")
    recovered = Storage(data_dir=tmp_path)
    assert recovered._state == {"k": "v1"}


# ---------------------------------------------------------------------------
# §9Б.9 — schema_version for the gateway state branch
# ---------------------------------------------------------------------------


def test_gateway_schema_version_stamped_and_rollback_safe(monkeypatch):
    import bot.gateway as gw
    from bot.addons import state as addon_state

    stamped: dict = {}

    def fake_get(addon, key, default=None):
        return stamped.get((addon, key), default)

    def fake_set(addon, key, value):
        stamped[(addon, key)] = value

    monkeypatch.setattr(addon_state, "get", fake_get)
    monkeypatch.setattr(addon_state, "set_", fake_set)

    # Missing version → stamped to current.
    gw.ensure_schema()
    assert stamped[("gateway", "schema_version")] == gw.SCHEMA_VERSION

    # Version from the future (code rollback) → branch untouched.
    stamped[("gateway", "schema_version")] = gw.SCHEMA_VERSION + 1
    gw.ensure_schema()
    assert stamped[("gateway", "schema_version")] == gw.SCHEMA_VERSION + 1


# ---------------------------------------------------------------------------
# §10 «Деградация» — bot.gateway import never breaks addon registration
# ---------------------------------------------------------------------------


def test_addon_routers_survive_without_platform_libs():
    """discord.py / vkbottle are не установлены на этапе 1 — реестр
    аддонов и импорт bot.gateway обязаны работать без них (A10)."""
    import importlib

    import bot.gateway  # noqa: F401 — must not raise
    from bot.addons import build_addon_routers

    routers = build_addon_routers()
    assert isinstance(routers, list)
    # And the gateway router itself is importable.
    module = importlib.import_module("bot.gateway.wizard_ui")
    assert module.gateway_router is not None


# ---------------------------------------------------------------------------
# §4.7 — Fable5 HTTP contract (200 {"screen"} / 409 tg_only / 404 off)
# ---------------------------------------------------------------------------


def _run_fable5_scenario(monkeypatch, scenario):
    """Spin up an aiohttp test client around Fable5 and run `scenario`."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import bot.access as access
    from bot.addons.fable5 import server as f5

    monkeypatch.setattr(access, "can_admin", lambda uid: True)
    monkeypatch.setattr(f5, "_check", lambda token: None)  # auth bypass
    monkeypatch.setattr(f5.storage, "get_owner_id", lambda: 111, raising=False)

    async def _main():
        app = web.Application()
        f5.register_http_endpoints(app)
        async with TestClient(TestServer(app)) as client:
            await scenario(client)

    asyncio.run(_main())


def test_fable5_menu_contract(monkeypatch):
    async def scenario(client):
        # GET /menu → {"screen": {...}} (§4.7).
        r = await client.get("/fable5/t/menu?screen=settings")
        assert r.status == 200
        data = await r.json()
        assert data["screen"]["screen_id"] == "settings"

        # Unknown screen → 404 {"error": "unknown_screen"}.
        r = await client.get("/fable5/t/menu?screen=nope")
        assert r.status == 404

        # POST /action screen-action → 200 {"screen": {...}}.
        r = await client.post("/fable5/t/action", json={"action": "main:settings"})
        assert r.status == 200
        data = await r.json()
        assert data["screen"]["screen_id"] == "settings"

        # Non-migrated action → 409 {"error": "tg_only", "deeplink": ...}.
        # roles:menu мигрирован в волне 2b (фикс 1.5) — используем
        # всё ещё немигрированный hermes:menu.
        r = await client.post("/fable5/t/action", json={"action": "hermes:menu"})
        assert r.status == 409
        data = await r.json()
        assert data["error"] == "tg_only"
        assert "deeplink" in data

    _run_fable5_scenario(monkeypatch, scenario)


def test_fable5_menu_404_when_gateway_disabled(monkeypatch):
    """Rollback §8.1: enabled=false → menu endpoints answer 404."""
    from bot.gateway import screens as gs

    monkeypatch.setattr(gs, "is_gateway_enabled", lambda: False)

    async def scenario(client):
        r = await client.get("/fable5/t/menu?screen=main")
        assert r.status == 404
        r = await client.post("/fable5/t/action", json={"action": "main:settings"})
        assert r.status == 404

    _run_fable5_scenario(monkeypatch, scenario)
