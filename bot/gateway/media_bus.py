"""Этап 5 ТЗ-1 (§6А, R11–R12): медиашина — кросс-платформенный конвейер.

Медиафайл, присланный боту на любой платформе, регистрируется как
**артефакт** и может быть обработан/получен/дообработан с любой другой
платформы. Ключевые решения (ТЗ §6А.2, обязательные):

1. Байты НЕ храним — в ``state.json`` только метаданные + устойчивые
   платформо-нативные указатели (``origin``). Байты скачиваются лениво
   в момент обработки (паттерн ``media_ui.py``).
2. Кольцевой буфер: максимум 10 последних артефактов.
3. Прямые URL не сохраняются нигде (Discord CDN умирает через 24 ч,
   VK Long Poll — быстрее). Резолверы §6А.6: TG ``file_id`` /
   Discord message-координаты / VK ``peer_id + cmid``.
4. Обработка — только в ядре ``bot/media.py``; адаптеры дают лишь
   вход/выход байтов. Дублировать провайдерную логику ЗАПРЕЩЕНО.

Rollback (ТЗ §8.1): флаг ``addon_state("gateway", "media_enabled")`` —
по умолчанию ``False``, вся шина «выключена по рождению»; врезки в
адаптеры и ``media_ui`` при выключенном флаге — no-op.

Результаты обработки и web-загрузки становятся telegram-артефактами:
бот отправляет результат владельцу в TG служебным сообщением и хранит
полученный ``file_id`` (решение ТЗ §6А.6 для VISION, применённое ко
всем результатам — ноль нового хранилища).
"""

from __future__ import annotations

import base64
import logging
import secrets
import time
from typing import Any

from ..addons import state as addon_state
from .dispatch import (
    GatewayContext,
    ScreenResult,
    register_action,
    register_action_prefix,
)
from .menu import MenuButton, MenuScreen, register_screen

logger = logging.getLogger("gateway.media")

ADDON = "gateway"
ARTIFACTS_KEY = "media_artifacts"
ENABLED_KEY = "media_enabled"

#: кольцевой буфер (ТЗ §6А.2 п.2)
MAX_ARTIFACTS = 10
#: жёсткий лимит TG Bot API на download (ТЗ §6А.5)
MAX_FILE_BYTES = 20 * 1024 * 1024

TOO_BIG_TEXT = "⚠️ Файл больше 20 МБ — это лимит обработки. Пришли поменьше."
EXPIRED_TEXT = "⚠️ Артефакт истёк (исходное сообщение недоступно). Пришли файл заново."

_KIND_ICONS = {"photo": "🖼", "video": "🎬"}
_PLATFORM_ICONS = {"telegram": "✈️ TG", "discord": "🎮 Discord",
                   "vk": "🔵 VK", "web": "👁 Site"}


class MediaExpiredError(Exception):
    """Исходное сообщение артефакта удалено/недоступно."""


# --------------------------------------------------------------------------
# флаг медиашины (rollback §8.1 — dark launch)
# --------------------------------------------------------------------------

def media_enabled() -> bool:
    try:
        return bool(addon_state.get(ADDON, ENABLED_KEY, False))
    except Exception:  # noqa: BLE001
        return False


def set_media_enabled(enabled: bool) -> None:
    addon_state.set_(ADDON, ENABLED_KEY, bool(enabled))


# --------------------------------------------------------------------------
# реестр артефактов (кольцевой буфер в state.json)
# --------------------------------------------------------------------------

def _load() -> list[dict]:
    return list(addon_state.get(ADDON, ARTIFACTS_KEY, []) or [])


def _save(items: list[dict]) -> None:
    addon_state.set_(ADDON, ARTIFACTS_KEY, items[-MAX_ARTIFACTS:])


def _new_id(existing: set[str]) -> str:
    """Короткий id вида ``m7f3`` (≤6 символов — влезает в action-строки)."""
    for _ in range(64):
        candidate = "m" + secrets.token_hex(2)[:3]
        if candidate not in existing:
            return candidate
    return "m" + secrets.token_hex(2)


def register_artifact(kind: str, origin: dict,
                      parent_id: str | None = None,
                      step: str = "") -> dict:
    """Зарегистрировать артефакт; возвращает его dict-представление."""
    items = _load()
    existing = {str(a.get("id")) for a in items}
    parent = None
    if parent_id:
        parent = next((a for a in items if a.get("id") == parent_id), None)
    chain = list((parent or {}).get("chain") or [])
    if step:
        chain.append(step)
    artifact = {
        "id": _new_id(existing),
        "kind": kind,
        "origin": dict(origin),
        "created_at": time.time(),
        "chain": chain,
        "parent_id": parent_id,
    }
    items.append(artifact)
    _save(items)
    return artifact


def list_artifacts() -> list[dict]:
    return _load()


def get_artifact(artifact_id: str) -> dict | None:
    for artifact in _load():
        if str(artifact.get("id")) == str(artifact_id):
            return artifact
    return None


# --------------------------------------------------------------------------
# резолверы (ТЗ §6А.6) — ленивое скачивание по устойчивому указателю
# --------------------------------------------------------------------------

_bot: Any = None


def _get_bot() -> Any:
    """Ленивый aiogram Bot (тот же паттерн, что в identity)."""
    global _bot
    if _bot is None:
        try:
            from aiogram import Bot

            from ..config import BOT_TOKEN
            if BOT_TOKEN:
                _bot = Bot(BOT_TOKEN)
        except Exception:  # noqa: BLE001
            logger.warning("media_bus: aiogram Bot unavailable")
    return _bot


async def _resolve_telegram(origin: dict) -> bytes:
    bot = _get_bot()
    if bot is None:
        raise MediaExpiredError(EXPIRED_TEXT)
    try:
        file = await bot.get_file(str(origin["file_id"]))
        if (getattr(file, "file_size", 0) or 0) > MAX_FILE_BYTES:
            raise ValueError(TOO_BIG_TEXT)
        buf = await bot.download_file(file.file_path)
        return buf.read()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("tg media resolve failed: %s", type(exc).__name__)
        raise MediaExpiredError(EXPIRED_TEXT) from exc


async def _resolve_discord(origin: dict) -> bytes:
    from .adapters import discord_gw

    data = await discord_gw.fetch_attachment_bytes(origin, MAX_FILE_BYTES)
    if data is None:
        raise MediaExpiredError(EXPIRED_TEXT)
    return data


async def _resolve_vk(origin: dict) -> bytes:
    from .adapters import vk_gw

    data = await vk_gw.fetch_media_bytes(origin, MAX_FILE_BYTES)
    if data is None:
        raise MediaExpiredError(EXPIRED_TEXT)
    return data


async def resolve_bytes(artifact: dict) -> bytes:
    """Байты артефакта по устойчивому ``origin``-указателю.

    Raises ``MediaExpiredError`` (исходник удалён) или ``ValueError``
    (файл больше лимита). URL нигде не кешируется.
    """
    origin = artifact.get("origin") or {}
    platform = str(origin.get("platform") or "")
    if platform == "telegram":
        return await _resolve_telegram(origin)
    if platform == "discord":
        return await _resolve_discord(origin)
    if platform == "vk":
        return await _resolve_vk(origin)
    raise MediaExpiredError(EXPIRED_TEXT)


async def _download_url(url: str) -> bytes:
    """Скачать результат провайдера (URL или data-URL) с лимитом размера."""
    if url.startswith("data:"):
        _, _, payload = url.partition(",")
        data = base64.b64decode(payload)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(TOO_BIG_TEXT)
        return data
    import aiohttp

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120)
    ) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(TOO_BIG_TEXT)
    return data


# --------------------------------------------------------------------------
# результат → telegram-артефакт (служебное сообщение владельцу, §6А.6)
# --------------------------------------------------------------------------

def _owner_id() -> int | None:
    try:
        from . import identity

        return identity._owner_id()  # noqa: SLF001 — единый источник owner id
    except Exception:  # noqa: BLE001
        return None


async def store_result(data: bytes, kind: str, caption: str,
                       parent_id: str | None, step: str) -> dict | None:
    """Отправить результат владельцу в TG и зарегистрировать артефакт.

    Возвращает новый артефакт или ``None``, если TG недоступен.
    """
    bot = _get_bot()
    owner = _owner_id()
    if bot is None or owner is None:
        return None
    try:
        from aiogram.types import BufferedInputFile

        if kind == "video":
            msg = await bot.send_video(
                owner, BufferedInputFile(data, filename="result.mp4"),
                caption=caption[:1000],
            )
            file_id = msg.video.file_id if msg.video else None
        else:
            msg = await bot.send_document(
                owner, BufferedInputFile(data, filename="result.png"),
                caption=caption[:1000],
            )
            file_id = msg.document.file_id if msg.document else None
        if not file_id:
            return None
        return register_artifact(
            kind=kind,
            origin={"platform": "telegram", "file_id": file_id},
            parent_id=parent_id,
            step=step,
        )
    except Exception:  # noqa: BLE001
        logger.warning("store_result failed", exc_info=True)
        return None


# --------------------------------------------------------------------------
# экраны: 🗂 Мои медиа + экран артефакта
# --------------------------------------------------------------------------

def _is_admin(user_id: int | None) -> bool:
    try:
        from .. import access

        return user_id is not None and access.can_admin(int(user_id))
    except Exception:  # noqa: BLE001
        return False


def _age(created_at: float) -> str:
    mins = max(0, int((time.time() - float(created_at or 0)) // 60))
    if mins < 60:
        return f"{mins} мин"
    return f"{mins // 60} ч"


def _artifact_label(artifact: dict) -> str:
    icon = _KIND_ICONS.get(str(artifact.get("kind")), "📎")
    origin = artifact.get("origin") or {}
    src = _PLATFORM_ICONS.get(str(origin.get("platform")), "?")
    return f"{icon} {artifact.get('id')} · {src} · {_age(artifact.get('created_at', 0))}"


def build_gateway_media(user_id: int | None) -> MenuScreen:
    if not _is_admin(user_id):
        return MenuScreen(
            screen_id="gateway:media",
            title="🗂 Мои медиа",
            body="⛔ Только владелец.",
            rows=[[MenuButton(text="↩️ Назад", action="gateway:home")]],
            parent="gateway:home",
        )
    enabled = media_enabled()
    rows: list[list[MenuButton]] = []
    body_lines = [
        "Общие медиа-артефакты всех платформ (последние 10).",
        f"Медиашина: {'✅ включена' if enabled else '⚪️ выключена'}",
    ]
    if enabled:
        artifacts = list(reversed(list_artifacts()))
        if not artifacts:
            body_lines.append("")
            body_lines.append("Пока пусто — пришли боту фото на любой платформе.")
        for artifact in artifacts:
            rows.append([MenuButton(
                text=_artifact_label(artifact),
                action=f"gateway:media:art:{artifact.get('id')}",
            )])
    rows.append([MenuButton(
        text="⏸ Выключить медиашину" if enabled else "▶️ Включить медиашину",
        action="gateway:media:toggle",
    )])
    rows.append([MenuButton(text="↩️ Назад", action="gateway:home")])
    return MenuScreen(
        screen_id="gateway:media",
        title="🗂 Мои медиа",
        body="\n".join(body_lines),
        rows=rows,
        parent="gateway:home",
    )


def build_artifact_screen(artifact_id: str, user_id: int | None) -> MenuScreen:
    artifact = get_artifact(artifact_id)
    if artifact is None or not _is_admin(user_id):
        return MenuScreen(
            screen_id="gateway:media",
            title="🗂 Мои медиа",
            body=EXPIRED_TEXT if artifact is None else "⛔ Только владелец.",
            rows=[[MenuButton(text="↩️ К списку", action="gateway:media")]],
            parent="gateway:media",
        )
    aid = str(artifact["id"])
    chain = " → ".join(artifact.get("chain") or []) or "исходник"
    body = (
        f"{_artifact_label(artifact)}\n"
        f"Цепочка: {chain}\n"
        f"Родитель: {artifact.get('parent_id') or '—'}"
    )
    rows: list[list[MenuButton]] = []
    if artifact.get("kind") == "photo":
        rows.append([MenuButton(
            text="🎬 Сделать видео из этого",
            action=f"gateway:media:video:{aid}",
        )])
        rows.append([MenuButton(
            text="🖼 Убрать фон",
            action=f"gateway:media:rmbg:{aid}",
        )])
    rows.append([
        MenuButton(text="📤 в TG", action=f"gateway:media:send:tg:{aid}"),
        MenuButton(text="📤 в Discord",
                   action=f"gateway:media:send:discord:{aid}"),
    ])
    rows.append([MenuButton(text="🗂 Мои медиа", action="gateway:media")])
    return MenuScreen(
        screen_id=f"gateway:media:art:{aid}",
        title=f"🗂 Артефакт {aid}",
        body=body,
        rows=rows,
        parent="gateway:media",
    )


# --------------------------------------------------------------------------
# действия (обработка — только через ядро bot/media.py)
# --------------------------------------------------------------------------

async def _act_toggle(action: str, ctx: GatewayContext) -> ScreenResult:
    if not _is_admin(ctx.user_id):
        return ScreenResult(error="forbidden", text="⛔ Только владелец.")
    set_media_enabled(not media_enabled())
    return ScreenResult(screen=build_gateway_media(ctx.user_id))


async def _act_art(action: str, ctx: GatewayContext) -> ScreenResult:
    aid = action.rsplit(":", 1)[-1]
    return ScreenResult(screen=build_artifact_screen(aid, ctx.user_id))


async def _resolve_or_error(aid: str) -> tuple[bytes | None, str | None]:
    artifact = get_artifact(aid)
    if artifact is None:
        return None, EXPIRED_TEXT
    try:
        return await resolve_bytes(artifact), None
    except MediaExpiredError:
        return None, EXPIRED_TEXT
    except ValueError as exc:
        return None, str(exc)


async def _act_video(action: str, ctx: GatewayContext) -> ScreenResult:
    """R12: «Сделать видео из этого» — img2video с РЕЗУЛЬТАТОМ (parent_id)."""
    if not _is_admin(ctx.user_id):
        return ScreenResult(error="forbidden", text="⛔ Только владелец.")
    aid = action.rsplit(":", 1)[-1]
    data, err = await _resolve_or_error(aid)
    if data is None:
        return ScreenResult(screen=build_gateway_media(ctx.user_id), text=err)
    from .. import media

    try:
        mp4_url = await media.image_to_video(data, "", quality="auto")
        mp4 = await _download_url(mp4_url)
    except ValueError as exc:
        return ScreenResult(text=str(exc))
    except Exception:  # noqa: BLE001
        logger.warning("media_bus img2video failed", exc_info=True)
        return ScreenResult(text="⚠️ Не получилось сделать видео. Попробуй позже.")
    new_art = await store_result(
        mp4, "video", f"✅ Готово: img2video · артефакт из {aid}",
        parent_id=aid, step=f"{ctx.platform}:img2video",
    )
    if new_art is None:
        return ScreenResult(text="⚠️ Видео готово, но доставить в TG не удалось.")
    return ScreenResult(
        screen=build_artifact_screen(new_art["id"], ctx.user_id),
        text=f"✅ Готово: img2video · артефакт {new_art['id']} (отправлен в TG)",
    )


async def _act_rmbg(action: str, ctx: GatewayContext) -> ScreenResult:
    if not _is_admin(ctx.user_id):
        return ScreenResult(error="forbidden", text="⛔ Только владелец.")
    aid = action.rsplit(":", 1)[-1]
    data, err = await _resolve_or_error(aid)
    if data is None:
        return ScreenResult(screen=build_gateway_media(ctx.user_id), text=err)
    from .. import media

    try:
        png_url, _jpg_url = await media.remove_background(data)
        png = await _download_url(png_url)
    except ValueError as exc:
        return ScreenResult(text=str(exc))
    except Exception:  # noqa: BLE001
        logger.warning("media_bus rmbg failed", exc_info=True)
        return ScreenResult(text="⚠️ Не получилось убрать фон. Попробуй позже.")
    new_art = await store_result(
        png, "photo", f"✅ Готово: rmbg · артефакт из {aid}",
        parent_id=aid, step=f"{ctx.platform}:rmbg",
    )
    if new_art is None:
        return ScreenResult(text="⚠️ Фон убран, но доставить в TG не удалось.")
    return ScreenResult(
        screen=build_artifact_screen(new_art["id"], ctx.user_id),
        text=f"✅ Готово: rmbg · артефакт {new_art['id']} (отправлен в TG)",
    )


async def _act_send(action: str, ctx: GatewayContext) -> ScreenResult:
    """📤 Доставка артефакта владельцу на выбранную платформу."""
    if not _is_admin(ctx.user_id):
        return ScreenResult(error="forbidden", text="⛔ Только владелец.")
    # gateway:media:send:<target>:<id>
    parts = action.split(":")
    target, aid = parts[-2], parts[-1]
    data, err = await _resolve_or_error(aid)
    if data is None:
        return ScreenResult(screen=build_gateway_media(ctx.user_id), text=err)
    artifact = get_artifact(aid) or {}
    kind = str(artifact.get("kind") or "photo")

    if target == "tg":
        bot = _get_bot()
        owner = _owner_id()
        if bot is None or owner is None:
            return ScreenResult(text="⚠️ TG недоступен.")
        try:
            from aiogram.types import BufferedInputFile

            if kind == "video":
                await bot.send_video(
                    owner, BufferedInputFile(data, filename=f"{aid}.mp4"),
                    caption=f"🗂 Артефакт {aid}",
                )
            else:
                await bot.send_document(
                    owner, BufferedInputFile(data, filename=f"{aid}.png"),
                    caption=f"🗂 Артефакт {aid}",
                )
        except Exception:  # noqa: BLE001
            logger.warning("media send to tg failed", exc_info=True)
            return ScreenResult(text="⚠️ Доставить в TG не удалось.")
        return ScreenResult(
            screen=build_artifact_screen(aid, ctx.user_id),
            text="✅ Отправлено в TG.",
        )

    if target == "discord":
        from . import identity
        from .adapters import discord_gw

        links = [l for l in identity.get_links()
                 if str(l.get("platform")) == "discord"]
        if not links:
            return ScreenResult(text="⚠️ Discord-аккаунт не привязан (🔗 Привязать акк).")
        filename = f"{aid}.mp4" if kind == "video" else f"{aid}.png"
        ok = False
        for link in links:
            if await discord_gw.send_dm_file(
                str(link.get("external_id")), data, filename,
                caption=f"🗂 Артефакт {aid}",
            ):
                ok = True
        if not ok:
            return ScreenResult(text="⚠️ Доставить в Discord не удалось (адаптер офлайн?).")
        return ScreenResult(
            screen=build_artifact_screen(aid, ctx.user_id),
            text="✅ Отправлено в Discord.",
        )

    return ScreenResult(text="⚠️ Эта платформа доставки пока не поддерживается.")


async def _route(action: str, ctx: GatewayContext) -> ScreenResult:
    """Роутер префикса ``gateway:media:*``."""
    if action == "gateway:media:toggle":
        return await _act_toggle(action, ctx)
    if action.startswith("gateway:media:art:"):
        return await _act_art(action, ctx)
    if action.startswith("gateway:media:video:"):
        return await _act_video(action, ctx)
    if action.startswith("gateway:media:rmbg:"):
        return await _act_rmbg(action, ctx)
    if action.startswith("gateway:media:send:"):
        return await _act_send(action, ctx)
    return ScreenResult(error="bad_action", text="⚠️ Неизвестное медиа-действие.")


def register() -> None:
    register_screen("gateway:media", build_gateway_media)
    register_action_prefix("gateway:media:", _route)


register()
