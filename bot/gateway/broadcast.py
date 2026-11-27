"""Этап 6 ТЗ-1 (R8): дублирование уведомлений владельцу на платформы.

``broadcast_to_owner(text)`` шлёт служебное уведомление владельца во все
платформы, для которых включён флаг в настройках ``broadcast`` — строго
по схеме хранения ТЗ §3.5::

    addons.gateway.broadcast = {"discord": false, "vk": false}

Правила (ТЗ §8, этап 6 + §8.1 rollback):

* dark launch — все флаги по умолчанию ``false``, деплой сам по себе
  ничего не меняет в поведении бота;
* отправка НИКОГДА не ломает вызывающий код (try/except + лог);
* доставка идёт только на ПРИВЯЗАННЫЕ аккаунты владельца
  (``identity.get_links()``) — чужим никогда;
* токены в логи не попадают.

Отклонение от ТЗ (записано в AI_CHANGELOG): врезка сделана не в
``bot/send.py`` (это CLI-утилита для shell-сессий, а не путь
уведомлений), а в ``identity._notify_owner`` — фактическую единую точку
служебных уведомлений владельцу.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..addons import state as addon_state
from .dispatch import GatewayContext, ScreenResult, register_action
from .menu import MenuButton, MenuScreen, register_screen

logger = logging.getLogger("gateway.broadcast")

ADDON = "gateway"
BROADCAST_KEY = "broadcast"

#: платформы, на которые умеем дублировать уведомления
PLATFORMS: tuple[str, ...] = ("discord", "vk")

_PLATFORM_LABELS = {"discord": "🎮 Discord", "vk": "🔵 VK"}


# --------------------------------------------------------------------------
# флаги (dark launch: всё выключено по рождению)
# --------------------------------------------------------------------------

def get_flags() -> dict[str, bool]:
    raw = addon_state.get(ADDON, BROADCAST_KEY, {}) or {}
    return {p: bool(raw.get(p, False)) for p in PLATFORMS}


def set_flag(platform: str, enabled: bool) -> None:
    if platform not in PLATFORMS:
        raise ValueError(f"unknown broadcast platform: {platform}")
    flags = get_flags()
    flags[platform] = bool(enabled)
    addon_state.set_(ADDON, BROADCAST_KEY, flags)


# --------------------------------------------------------------------------
# доставка
# --------------------------------------------------------------------------

def _owner_links(platform: str) -> list[dict]:
    from . import identity

    return [
        link for link in identity.get_links()
        if str(link.get("platform")) == platform
    ]


async def _deliver_discord(text: str) -> bool:
    try:
        from .adapters import discord_gw
    except Exception:  # noqa: BLE001 — discord.py может отсутствовать
        return False
    delivered = False
    for link in _owner_links("discord"):
        try:
            if await discord_gw.send_dm(str(link.get("external_id")), text):
                delivered = True
        except Exception:  # noqa: BLE001
            logger.warning("discord broadcast failed", exc_info=True)
    return delivered


async def _deliver_vk(text: str) -> bool:
    try:
        from .adapters import vk_gw
    except Exception:  # noqa: BLE001
        return False
    delivered = False
    for link in _owner_links("vk"):
        try:
            if await vk_gw.send_text(str(link.get("external_id")), text):
                delivered = True
        except Exception:  # noqa: BLE001
            logger.warning("vk broadcast failed", exc_info=True)
    return delivered


async def broadcast_to_owner(text: str) -> dict[str, bool]:
    """Продублировать уведомление владельцу на включённые платформы.

    Возвращает ``{platform: delivered}`` только для включённых платформ.
    Никогда не бросает исключений наружу.
    """
    results: dict[str, bool] = {}
    try:
        flags = get_flags()
        if flags.get("discord"):
            results["discord"] = await _deliver_discord(text)
        if flags.get("vk"):
            results["vk"] = await _deliver_vk(text)
    except Exception:  # noqa: BLE001 — broadcast не должен ломать вызывающего
        logger.warning("broadcast_to_owner failed", exc_info=True)
    return results


def schedule_broadcast(text: str) -> None:
    """Fire-and-forget вариант для sync-кода (паттерн identity)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with contextlib.suppress(Exception):
        task = loop.create_task(broadcast_to_owner(text))
        task.add_done_callback(_consume_task_error)


def _consume_task_error(task: "asyncio.Task") -> None:
    with contextlib.suppress(Exception):
        task.exception()


# --------------------------------------------------------------------------
# экран «🔔 Уведомления» (заменяет заглушку из screens.py)
# --------------------------------------------------------------------------

def _is_admin(user_id: int | None) -> bool:
    try:
        from .. import access
        return user_id is not None and access.can_admin(int(user_id))
    except Exception:  # noqa: BLE001
        return False


def build_gateway_notify(user_id: int | None) -> MenuScreen:
    if not _is_admin(user_id):
        return MenuScreen(
            screen_id="gateway:notify",
            title="🔔 Уведомления",
            body="⛔ Только владелец.",
            rows=[[MenuButton(text="↩️ Назад", action="gateway:home")]],
            parent="gateway:home",
        )
    flags = get_flags()
    rows: list[list[MenuButton]] = []
    for platform in PLATFORMS:
        mark = "✅ вкл" if flags[platform] else "⚪️ выкл"
        rows.append([MenuButton(
            text=f"{_PLATFORM_LABELS[platform]}: {mark}",
            action=f"gateway:notify:toggle:{platform}",
        )])
    rows.append([MenuButton(text="🧪 Тест-уведомление",
                            action="gateway:notify:test")])
    rows.append([MenuButton(text="↩️ Назад", action="gateway:home")])
    return MenuScreen(
        screen_id="gateway:notify",
        title="🔔 Уведомления",
        body=(
            "Дублировать служебные уведомления владельца на платформы.\n"
            "Работает только для привязанных аккаунтов (🔗 Привязать акк).\n"
            "По умолчанию всё выключено."
        ),
        rows=rows,
        parent="gateway:home",
    )


async def _act_toggle(action: str, ctx: GatewayContext) -> ScreenResult:
    if not _is_admin(ctx.user_id):
        return ScreenResult(error="forbidden", text="⛔ Только владелец.")
    platform = action.rsplit(":", 1)[-1]
    if platform not in PLATFORMS:
        return ScreenResult(error="unknown_platform")
    flags = get_flags()
    set_flag(platform, not flags[platform])
    return ScreenResult(screen=build_gateway_notify(ctx.user_id))


async def _act_test(action: str, ctx: GatewayContext) -> ScreenResult:
    if not _is_admin(ctx.user_id):
        return ScreenResult(error="forbidden", text="⛔ Только владелец.")
    results = await broadcast_to_owner("🧪 Тест-уведомление от Лилуш")
    if not results:
        text = "⚪️ Все платформы выключены — включи хотя бы одну."
    else:
        parts = [
            f"{_PLATFORM_LABELS[p]}: {'✅ доставлено' if ok else '⚠️ не доставлено'}"
            for p, ok in results.items()
        ]
        text = "\n".join(parts)
    return ScreenResult(screen=build_gateway_notify(ctx.user_id), text=text)


def register() -> None:
    register_screen("gateway:notify", build_gateway_notify)
    for platform in PLATFORMS:
        register_action(f"gateway:notify:toggle:{platform}", _act_toggle)
    register_action("gateway:notify:test", _act_test)


register()
