"""Этап 5 ТЗ-1 (§6А, R11–R12) — медиашина.

* dark launch: флаг ``media_enabled`` по умолчанию False;
* кольцевой буфер: максимум 10 артефактов;
* URL нигде не хранится — только устойчивые origin-указатели;
* цепочка обработки (chain/parent_id) наследуется;
* экраны: список, артефакт, права (только владелец);
* доступ к действиям ``gateway:media:*`` требует admin.

Все тесты — без сети: addon-state в RAM, резолверы мокаются.
"""

from __future__ import annotations

import pytest

from bot.gateway import media_bus
from bot.gateway.dispatch import GatewayContext, dispatch_action


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
    # media_bus импортирует state как модуль — патчим и там
    monkeypatch.setattr(media_bus.addon_state, "get",
                        lambda addon, key, default=None: store.get((addon, key), default))
    monkeypatch.setattr(media_bus.addon_state, "set_",
                        lambda addon, key, value: store.__setitem__((addon, key), value))
    return store


@pytest.fixture()
def as_admin(monkeypatch):
    """Единый источник прав: и диспетчер, и экраны (паттерн wave2b)."""
    import bot.access as access

    monkeypatch.setattr(access, "can_admin", lambda uid: uid == 42)
    monkeypatch.setattr(media_bus, "_is_admin", lambda uid: uid == 42)
    from bot.gateway import dispatch
    monkeypatch.setattr(dispatch, "_is_owner", lambda uid: uid == 42)


# ---------------------------------------------------------------------------
# dark launch / rollback (§8.1)
# ---------------------------------------------------------------------------


def test_media_disabled_by_default(mem_state):
    assert media_bus.media_enabled() is False


def test_toggle_flag(mem_state):
    media_bus.set_media_enabled(True)
    assert media_bus.media_enabled() is True
    media_bus.set_media_enabled(False)
    assert media_bus.media_enabled() is False


# ---------------------------------------------------------------------------
# реестр артефактов
# ---------------------------------------------------------------------------


def test_register_and_get_artifact(mem_state):
    art = media_bus.register_artifact(
        kind="photo",
        origin={"platform": "telegram", "file_id": "AAA"},
        step="tg:upload",
    )
    assert art["kind"] == "photo"
    assert art["chain"] == ["tg:upload"]
    fetched = media_bus.get_artifact(art["id"])
    assert fetched is not None
    assert fetched["origin"]["file_id"] == "AAA"


def test_ring_buffer_keeps_last_10(mem_state):
    ids = [
        media_bus.register_artifact(
            "photo", {"platform": "telegram", "file_id": str(i)}
        )["id"]
        for i in range(15)
    ]
    stored = media_bus.list_artifacts()
    assert len(stored) == media_bus.MAX_ARTIFACTS == 10
    stored_ids = [a["id"] for a in stored]
    assert stored_ids == ids[-10:]


def test_chain_inherited_from_parent(mem_state):
    parent = media_bus.register_artifact(
        "photo", {"platform": "discord", "channel_id": 1,
                  "message_id": 2, "attachment_id": 3},
        step="discord:upload",
    )
    child = media_bus.register_artifact(
        "video", {"platform": "telegram", "file_id": "BBB"},
        parent_id=parent["id"], step="tg:img2video",
    )
    assert child["chain"] == ["discord:upload", "tg:img2video"]
    assert child["parent_id"] == parent["id"]


def test_no_raw_urls_stored(mem_state):
    """§6А.2 п.3: прямые URL не сохраняются нигде."""
    media_bus.register_artifact(
        "photo", {"platform": "vk", "peer_id": 1, "cmid": 2}
    )
    import json
    dump = json.dumps(media_bus.list_artifacts())
    assert "http://" not in dump and "https://" not in dump


# ---------------------------------------------------------------------------
# экраны
# ---------------------------------------------------------------------------


def test_media_screen_forbidden_for_guest(mem_state, as_admin):
    screen = media_bus.build_gateway_media(None)
    assert "Только владелец" in screen.body


def test_media_screen_shows_artifacts_when_enabled(mem_state, as_admin):
    media_bus.set_media_enabled(True)
    art = media_bus.register_artifact(
        "photo", {"platform": "telegram", "file_id": "CCC"}
    )
    screen = media_bus.build_gateway_media(42)
    actions = [b.action for row in screen.rows for b in row]
    assert f"gateway:media:art:{art['id']}" in actions
    assert "gateway:media:toggle" in actions


def test_artifact_screen_has_processing_buttons(mem_state, as_admin):
    art = media_bus.register_artifact(
        "photo", {"platform": "telegram", "file_id": "DDD"}
    )
    screen = media_bus.build_artifact_screen(art["id"], 42)
    actions = [b.action for row in screen.rows for b in row]
    aid = art["id"]
    assert f"gateway:media:video:{aid}" in actions
    assert f"gateway:media:rmbg:{aid}" in actions
    assert f"gateway:media:send:tg:{aid}" in actions


def test_artifact_screen_expired(mem_state, as_admin):
    screen = media_bus.build_artifact_screen("nope", 42)
    assert "истёк" in screen.body


def test_callback_data_limits(mem_state, as_admin):
    """§10: все action-строки экранов ≤ 64 байт."""
    media_bus.set_media_enabled(True)
    art = media_bus.register_artifact(
        "photo", {"platform": "telegram", "file_id": "EEE"}
    )
    for screen in (media_bus.build_gateway_media(42),
                   media_bus.build_artifact_screen(art["id"], 42)):
        for row in screen.rows:
            for b in row:
                assert len(b.action.encode()) <= 64, b.action


# ---------------------------------------------------------------------------
# действия через диспетчер
# ---------------------------------------------------------------------------


async def test_toggle_requires_admin(mem_state, as_admin):
    ctx = GatewayContext(platform="discord", external_id="1", user_id=None)
    result = await dispatch_action("gateway:media:toggle", ctx)
    assert result.error == "forbidden"


async def test_toggle_via_dispatch(mem_state, as_admin):
    ctx = GatewayContext(platform="telegram", external_id="42", user_id=42)
    result = await dispatch_action("gateway:media:toggle", ctx)
    assert result.screen is not None
    assert media_bus.media_enabled() is True


async def test_video_action_uses_media_core(mem_state, as_admin, monkeypatch):
    """R12: обработка идёт через ядро bot/media.py, результат → TG-артефакт."""
    art = media_bus.register_artifact(
        "photo", {"platform": "telegram", "file_id": "FFF"},
        step="tg:upload",
    )

    async def fake_resolve(a):
        return b"IMGBYTES"

    calls = {}

    async def fake_img2video(image_bytes, prompt, *, quality="auto", **kw):
        calls["img2video"] = (image_bytes, quality)
        return "data:video/mp4;base64,QUFB"  # b"AAA"

    async def fake_store(data, kind, caption, parent_id, step):
        calls["store"] = (data, kind, parent_id, step)
        return media_bus.register_artifact(
            kind, {"platform": "telegram", "file_id": "RESULT"},
            parent_id=parent_id, step=step,
        )

    monkeypatch.setattr(media_bus, "resolve_bytes", fake_resolve)
    import bot.media as media_core
    monkeypatch.setattr(media_core, "image_to_video", fake_img2video)
    monkeypatch.setattr(media_bus, "store_result", fake_store)

    ctx = GatewayContext(platform="vk", external_id="9", user_id=42)
    result = await dispatch_action(f"gateway:media:video:{art['id']}", ctx)

    assert calls["img2video"][0] == b"IMGBYTES"
    data, kind, parent_id, step = calls["store"]
    assert data == b"AAA" and kind == "video"
    assert parent_id == art["id"] and step == "vk:img2video"
    assert result.screen is not None
    assert "Готово" in (result.text or "")


async def test_expired_artifact_returns_hint(mem_state, as_admin, monkeypatch):
    art = media_bus.register_artifact(
        "photo", {"platform": "discord", "channel_id": 1,
                  "message_id": 2, "attachment_id": 3},
    )

    async def fake_resolve(a):
        raise media_bus.MediaExpiredError(media_bus.EXPIRED_TEXT)

    monkeypatch.setattr(media_bus, "resolve_bytes", fake_resolve)
    ctx = GatewayContext(platform="telegram", external_id="42", user_id=42)
    result = await dispatch_action(f"gateway:media:rmbg:{art['id']}", ctx)
    assert "истёк" in (result.text or "")
