# Regression tests for main-menu buttons 22 («🖥 VISION») and 23
# («🗺 Карта проекта»).
#
# Bug: with no public base URL configured (local dev — PUBLIC_URL &
# friends unset) ``get_link()`` returns a *relative* path like
# ``/vision/<token>/``.  That string was passed straight into
# ``InlineKeyboardButton(url=...)`` — Telegram rejects such messages
# with ``BUTTON_URL_INVALID`` at send time, so the whole screen
# silently never renders (the user taps the button and nothing
# happens).
#
# The fake message below validates reply_markup the same way the
# Telegram Bot API does: any url-button whose url is not an absolute
# http(s)/tg link makes the send fail.

from __future__ import annotations

import types

import pytest

pytest.importorskip("aiogram")
pytest.importorskip("aiohttp")

import bot.wizard as wizard  # noqa: E402


class TelegramBadRequestStub(Exception):
    """Stands in for aiogram's TelegramBadRequest(BUTTON_URL_INVALID)."""


def _validate_markup(markup) -> None:
    if markup is None:
        return
    for row in markup.inline_keyboard:
        for btn in row:
            url = getattr(btn, "url", None)
            if url is not None and not url.startswith(
                ("http://", "https://", "tg://")
            ):
                raise TelegramBadRequestStub(
                    f"BUTTON_URL_INVALID: {url!r}"
                )


class _FakeMessage:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        _validate_markup(reply_markup)
        self.sent.append(("answer", text, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        _validate_markup(reply_markup)
        self.sent.append(("edit_text", text, reply_markup))

    async def answer_document(self, *args, **kwargs):
        self.sent.append(("answer_document",))


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = types.SimpleNamespace(id=12345)
        self.message = _FakeMessage()
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))


class _FakeState:
    async def clear(self) -> None:
        return None


@pytest.fixture()
def local_env(monkeypatch):
    """Local-dev environment: no public base URL resolvable."""
    for var in (
        "PUBLIC_URL",
        "RENDER_EXTERNAL_URL",
        "RAILWAY_PUBLIC_DOMAIN",
        "FLY_APP_NAME",
        "ORACLE_PUBLIC_HOST",
        "KEEP_ALIVE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(wizard, "can_use", lambda uid=None: True)
    monkeypatch.setattr(wizard, "can_admin", lambda uid=None: True)


@pytest.mark.asyncio
async def test_vision_screen_renders_without_public_url(local_env):
    """Button 22: the VISION screen must render even in local dev."""
    from bot.addons.vision import server as vis_server

    vis_server.generate_link()  # token exists, link is relative
    q = _FakeQuery("main:vision")
    await wizard.cb_main_menu(q, _FakeState())
    assert q.message.sent, (
        "VISION screen was never sent — url-button with a relative "
        "link killed the message (BUTTON_URL_INVALID)"
    )


@pytest.mark.asyncio
async def test_vision_rotate_renders_without_public_url(local_env):
    """Button 22 → «🔄 Новая ссылка» must update the screen locally."""
    q = _FakeQuery("main:vision:rotate")
    await wizard.cb_main_menu(q, _FakeState())
    assert q.message.sent, (
        "VISION rotate screen was silently swallowed — the edit_text "
        "failed with BUTTON_URL_INVALID inside contextlib.suppress"
    )
    # And the user must not have received the error alert.
    assert not any(alert for _, alert in q.answers if alert), (
        "user got an error alert instead of the rotated link"
    )


@pytest.mark.asyncio
async def test_projmap_screen_renders_without_public_url(local_env):
    """Button 23: the project-map home screen must render locally."""
    from bot.addons.projectmap import store as pm_store

    pm_store.generate_link()  # enabled=True, link is relative
    q = _FakeQuery("main:projmap")
    await wizard.cb_main_menu(q, _FakeState())
    assert q.message.sent, (
        "project-map screen was never sent — url-button with a "
        "relative link killed the message (BUTTON_URL_INVALID)"
    )


@pytest.mark.asyncio
async def test_projmap_open_updates_screen_without_public_url(local_env):
    """Button 23 → «🔗 Включить карту» must show the updated screen."""
    from bot.addons.projectmap import store as pm_store

    pm_store.disable_link()
    q = _FakeQuery("main:projmap:open")
    await wizard.cb_main_menu(q, _FakeState())
    assert q.message.sent, (
        "project-map «open» edit was silently swallowed — the "
        "edit_text failed with BUTTON_URL_INVALID inside suppress"
    )


@pytest.mark.asyncio
async def test_test_screen_renders_without_public_url(local_env):
    """Button 20: the «🧪 Тест» screen must render even in local dev.

    The default 'lilush' target's URL comes from ``get_link()``; when
    no public base is resolvable it used to be relative and killed the
    screen via BUTTON_URL_INVALID on the «🔗 Открыть превью» button.
    """
    from bot.addons import test as test_addon

    test_addon.generate_link()  # token exists, enabled=True
    q = _FakeQuery("main:test")
    await wizard.cb_main_menu(q, _FakeState())
    assert q.message.sent, (
        "«🧪 Тест» screen was never sent — url-button with a relative "
        "link killed the message (BUTTON_URL_INVALID)"
    )


def test_test_addon_link_is_absolute_without_public_url(local_env):
    """``get_link()`` of the test addon must never be relative."""
    from bot.addons import test as test_addon

    test_addon.generate_link()
    link = test_addon.get_link()
    assert link.startswith(("http://", "https://")), link


@pytest.mark.asyncio
async def test_vision_button_still_a_url_button_with_public_url(
    local_env, monkeypatch
):
    """With PUBLIC_URL set the native url-button must stay."""
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    q = _FakeQuery("main:vision")
    await wizard.cb_main_menu(q, _FakeState())
    assert q.message.sent
    _, _, markup = q.message.sent[-1]
    urls = [
        btn.url
        for row in markup.inline_keyboard
        for btn in row
        if getattr(btn, "url", None)
    ]
    assert any(u.startswith("https://bot.example.com/vision/") for u in urls)
