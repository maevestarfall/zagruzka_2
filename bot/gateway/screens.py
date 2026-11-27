"""Wave-1 screen builders + all ``gateway:*`` screens (ТЗ-1 §4, §7).

Wave 1 migrates the two top-level screens — «главное меню» (``main``)
and «⚙️ Настройки» (``settings``) — into the neutral ``MenuScreen``
format. :func:`bot.wizard._kb_main_after_claim` /
``_kb_settings_menu`` become thin ``screen_to_tg`` converters, and the
TG output stays byte-equal to the legacy keyboards (verified by
``tests/test_gateway_stage1.py``).

The ``gateway:*`` screens (§4.2–4.5) are defined here too, so they are
rendered through the same pipeline on every platform.
"""

from __future__ import annotations

import logging
import time

from ..addons import state as addon_state
from .dispatch import (
    GatewayContext,
    ScreenResult,
    register_action,
    register_action_prefix,
)
from .menu import MenuButton, MenuScreen, build_screen, register_screen

logger = logging.getLogger("gateway.screens")

ADDON = "gateway"

GATEWAY_BUTTON_TEXT = "🌐 Платформы: TG · Site · Discord · VK"


def is_gateway_enabled() -> bool:
    """Rollback flag (§8.1): ``False`` hides the button and the web menu."""
    return bool(addon_state.get(ADDON, "enabled", True))


def _mask(token: str) -> str:
    """Mask a secret for UI display: first 4 + … + last 4 (R7-E1)."""
    token = token or ""
    if len(token) <= 8:
        return "…" if token else ""
    return f"{token[:4]}…{token[-4:]}"


# --------------------------------------------------------------------------
# Wave 1 · «main» — mirrors bot.wizard._kb_main_after_claim row-for-row
# --------------------------------------------------------------------------

def build_main_screen(user_id: int | None) -> MenuScreen:
    from ..access import can_admin

    try:
        from ..addons.vision.server import get_status as _vision_status
        vision_ready = bool(_vision_status().get("active"))
    except Exception:  # noqa: BLE001
        vision_ready = False
    vision_label = (
        "🟢 VISION — готова" if vision_ready else "⚪ VISION — веб-панель"
    )

    rows: list[list[MenuButton]] = [
        [MenuButton(text="🤖 Helpzavr", action="main:helpzavr")],
        [MenuButton(text="✨ Красивый текст", action="main:pretty")],
        [MenuButton(text="📬 Проверка почты", action="main:mailbox")],
        [MenuButton(text="🎬 Генерация фото и видео", action="main:media_toggle")],
        [MenuButton(text="🕵️ Исследование и поиск человека", action="main:osint")],
        [MenuButton(text="🗑 Скинуть весь мусор", action="main:dump")],
        [MenuButton(text="🛡 AI CHECK", action="main:ai_check")],
        [MenuButton(text="🎤 Дебаты", action="main:debates")],
        [MenuButton(text="🔬 Анализатор PRO", action="main:analyzer")],
        [MenuButton(text="💳 Выдача", action="main:issuance")],
        [MenuButton(text="📝 Markitdown", action="main:markitdown")],
        [MenuButton(text="🐙 GitHub проекты", action="main:github")],
        [MenuButton(text="💻 Кодер (Coder Firm)", action="main:coder")],
        [MenuButton(text="🪄 Регенерация", action="main:regen")],
        [MenuButton(text="✨ Vibe (Vibe Coding)", action="main:vibe")],
        [MenuButton(text="🖥 Терминал", action="main:terminal")],
        [MenuButton(text="🌐 Браузерная", action="main:browser")],
        [MenuButton(text="🧠 Умное зрение", action="main:smartvision")],
        [MenuButton(text="🗄 Хранилище промтов", action="main:promptbox")],
    ]
    if can_admin(user_id):
        rows.extend(
            [
                [MenuButton(text="🧪 Тест", action="main:test")],
                [MenuButton(text="🔁 Ревью→фикс→проверка→повтор (GitHub)", action="lrv:home")],
                [MenuButton(text=vision_label, action="main:vision")],
                [MenuButton(text="🗺 Карта проекта", action="main:projmap")],
                [MenuButton(text="🎓 Обучалка", action="main:obuchalka")],
                [MenuButton(text="🧭 Контекст", action="crelay:home")],
                [MenuButton(text="🛠 Master Room", action="main:masterroom")],
                [MenuButton(text="📥 Скачать видео", action="main:download")],
                [MenuButton(text="🖥 Комп (localdesktop)", action="main:install:comp")],
                [MenuButton(text="⬇️ Скачать (bash2mp4)", action="main:install:downloader")],
                [MenuButton(text="🧪 Анализатор (agent-skills)", action="main:install:analyzer")],
                [MenuButton(text="🎞 Монтажёр", action="main:editor")],
                [MenuButton(text="🧠 Перенастроить мозг", action="main:brain")],
                [MenuButton(text="🧩 Прочие", action="misc:home")],
                [MenuButton(text="📊 Статистика токенов (/tokens)", action="main:tokens")],
            ]
        )
    # «📍 План карта» — прямо над «⚙️ Настройки» (user request).
    if can_admin(user_id):
        rows.append([MenuButton(text="📍 План карта", action="main:planmap")])
    # Settings always sits at the bottom (user request preserved 1:1).
    rows.append([MenuButton(text="⚙️ Настройки", action="main:settings")])
    return MenuScreen(
        screen_id="main",
        title="Главное меню",
        rows=rows,
        parent=None,
    )


# --------------------------------------------------------------------------
# Wave 1 · «settings» — mirrors bot.wizard._kb_settings_menu row-for-row,
# plus the new «🌐 Платформы» button (R1) in the admin block.
# --------------------------------------------------------------------------

def build_settings_screen(user_id: int | None) -> MenuScreen:
    from ..access import can_admin

    from ..addons.algorithm.scheduler import is_auto_monitoring_enabled
    monitor_enabled = is_auto_monitoring_enabled()
    monitor_label = "⏰ Авто-мониторинг: ВКЛ" if monitor_enabled else "⏰ Авто-мониторинг: ВЫКЛ"
    selfcheck_on = bool(addon_state.get("system", "startup_selfcheck", True))
    selfcheck_label = (
        "🛡 Самопроверка старта: ВКЛ" if selfcheck_on else "🛡 Самопроверка старта: ВЫКЛ"
    )
    ctxclear_on = bool(addon_state.get("system", "ctx_clear_each", False))
    ctxclear_label = (
        "🧹 Чистить контекст каждое действие: ВКЛ" if ctxclear_on
        else "🧹 Чистить контекст каждое действие: ВЫКЛ"
    )

    rows: list[list[MenuButton]] = [
        [MenuButton(text="🎭 Роли", action="roles:menu")],
        [MenuButton(text="🔊 Голосовой ответчик", action="tts:menu")],
        [MenuButton(text="🧠 Соображалка", action="main:thinking")],
        [MenuButton(text="🎯 Приоритет", action="main:priority")],
        [MenuButton(text="📜 Инструкции", action="instr:home")],
        [MenuButton(text="📚 Поступенчатая инструкция", action="main:playbook")],
        [MenuButton(text="🕵️ Антидетект", action="main:stealth")],
        [MenuButton(text="🧠 Контекст", action="ctx:home")],
        [MenuButton(text="📦 Отделение", action="sep:home")],
        [MenuButton(text="📢 Публикация", action="pub:home")],
        [MenuButton(text="💾 Память бота", action="main:memory")],
        [MenuButton(text="💻 RAM", action="main:ram")],
        [MenuButton(text="🧩 Алгоритм", action="algo:menu")],
        [MenuButton(text="🔌 API (массовый импорт)", action="api:batch")],
        [MenuButton(text="🔌 Unstructured", action="main:unstructured")],
        [MenuButton(text="🔒 Сейф данных", action="main:vault")],
        [MenuButton(text="🔌 Каналы-источники", action="main:sources")],
        [MenuButton(text=monitor_label, action="main:toggle_monitoring")],
        [MenuButton(text=selfcheck_label, action="main:toggle_selfcheck")],
        [MenuButton(text=ctxclear_label, action="main:toggle_ctxclear")],
        [MenuButton(text="🐙 GitHub проекты", action="gh:menu")],
        [MenuButton(text="🖥 Терминал", action="main:terminal")],
        [MenuButton(text="📚 Узнать о функционале", action="main:learn")],
        [MenuButton(text="🎯 Мотивация авто", action="motauto:home")],
    ]
    if user_id is not None and can_admin(user_id):
        # SwapFace (InsightFaceSwap) — только владелец/со-владелец (§7).
        # Порядок строго совпадает с bot.wizard._kb_settings_menu (тест A2).
        rows.append([MenuButton(text="🎭 SwapFace", action="swf:menu")])
        if is_gateway_enabled():
            rows.append([MenuButton(text=GATEWAY_BUTTON_TEXT, action="gateway:home")])
        rows.append([MenuButton(text="⛔ Доступ", action="access:menu")])
    rows.append([MenuButton(text="↩️ В главное меню", action="settings:back")])
    return MenuScreen(
        screen_id="settings",
        title="⚙️ Настройки",
        rows=rows,
        parent="main",
    )


# --------------------------------------------------------------------------
# gateway:* screens (§4.2–4.5)
# --------------------------------------------------------------------------

def _platform_status_lines() -> list[str]:
    lines = ["  ✈️ Telegram      ✅ активен (родная платформа)"]
    # Site / VISION
    try:
        from ..addons.vision.server import get_link as _vis_link, is_enabled as _vis_enabled
        if _vis_enabled() and _vis_link():
            lines.append("  👁 Сайт (VISION) ✅ включён")
        else:
            lines.append("  👁 Сайт (VISION) ⚪️ выключен")
    except Exception:  # noqa: BLE001
        lines.append("  👁 Сайт (VISION) ⚠️ недоступен")
    # Discord / VK — живые статусы адаптеров (этапы 2–3, части A/B).
    icons = {"off": "⚪️", "connecting": "🟡", "online": "✅", "error": "🔴"}
    try:
        from .adapters import discord_gw

        ds = discord_gw.get_status()
        state = str(ds.get("state", "off"))
        icon = icons.get(state, "⚪️")
        if state == "online":
            lines.append(f"  🎮 Discord       {icon} онлайн ({int(ds.get('guilds', 0))} серв.)")
        elif discord_gw.get_token():
            label = {"connecting": "подключается…", "error": "ошибка"}.get(state, "выключен")
            lines.append(f"  🎮 Discord       {icon} {label}")
        else:
            lines.append("  🎮 Discord       ⚪️ не подключён")
    except Exception:  # noqa: BLE001
        lines.append("  🎮 Discord       ⚪️ не подключён")
    try:
        from .adapters import vk_gw

        vs = vk_gw.get_status()
        state = str(vs.get("state", "off"))
        icon = icons.get(state, "⚪️")
        if state == "online":
            group = vs.get("group") or ""
            lines.append(f"  🔵 VK            {icon} онлайн {group}".rstrip())
        elif vk_gw.get_token():
            label = {"connecting": "подключается…", "error": "ошибка"}.get(state, "выключен")
            lines.append(f"  🔵 VK            {icon} {label}")
        else:
            lines.append("  🔵 VK            ⚪️ не подключён")
    except Exception:  # noqa: BLE001
        lines.append("  🔵 VK            ⚪️ не подключён")
    return lines


def build_gateway_home(user_id: int | None) -> MenuScreen:
    body = "Управляй Лилуш с любой площадки:\n\n" + "\n".join(_platform_status_lines())
    rows = [
        [
            MenuButton(text="👁 Сайт", action="gateway:site"),
            MenuButton(text="🎮 Discord", action="gateway:discord"),
        ],
        [
            MenuButton(text="🔵 VK", action="gateway:vk"),
            MenuButton(text="🔗 Привязать акк", action="gateway:link"),
        ],
        [
            MenuButton(text="📡 Статус", action="gateway:status"),
            MenuButton(text="🔔 Уведомления", action="gateway:notify"),
        ],
        [MenuButton(text="🗂 Мои медиа", action="gateway:media")],
        [MenuButton(text="↩️ Назад", action="main:settings")],
    ]
    return MenuScreen(
        screen_id="gateway:home",
        title="🌐 Платформы",
        body=body,
        rows=rows,
        parent="settings",
    )


def build_gateway_site(user_id: int | None) -> MenuScreen:
    """«👁 Сайт» — re-uses the existing VISION flow (§4.2, §13.5)."""
    rows: list[list[MenuButton]] = []
    body = ""
    try:
        from ..addons.vision.server import get_link as _vis_link, is_enabled as _vis_enabled
        if _vis_enabled() and _vis_link():
            link = _vis_link()
            body = (
                "Сайт включён. Панель VISION + вкладка «🤖 Меню бота» — "
                "то же меню, что в TG.\n\n"
                "«🔄 Новая ссылка» отзывает старую."
            )
            rows.append([MenuButton(text="🔗 Открыть сайт", action="", url=link)])
            rows.append([MenuButton(text="🔄 Новая ссылка", action="gateway:site:rotate")])
            rows.append([MenuButton(text="🔴 Выключить", action="gateway:site:off")])
        else:
            body = (
                "Сайт выключен. Включи, чтобы управлять Лилуш из браузера — "
                "меню, терминал, логи и state."
            )
            rows.append([MenuButton(text="🟢 Включить сайт", action="gateway:site:on")])
    except Exception:  # noqa: BLE001
        logger.exception("vision status failed")
        body = "⚠️ Не удалось получить статус VISION. Проверь логи."
    rows.append([MenuButton(text="↩️ Назад", action="gateway:home")])
    return MenuScreen(
        screen_id="gateway:site",
        title="👁 Сайт (VISION)",
        body=body,
        rows=rows,
        parent="gateway:home",
    )


def _build_platform_stub(
    screen_id: str, title: str, hint: str, live_screen: str | None = None
) -> MenuScreen:
    """Discord / VK screens этапа 1 — сохранены как входные точки.

    Контракт частей A/B: заглушки ``gateway:discord``/``gateway:vk`` НЕ
    трогаем (их тесты фиксируют identity и screen_id); живые панели
    адаптеров живут на отдельных экранах ``*:live``, и заглушка теперь
    ведёт туда первой кнопкой.
    """
    body = f"{hint}\n\nПанель подключения и статус — на следующем экране."
    rows = []
    if live_screen:
        rows.append([MenuButton(text="🔌 Открыть панель подключения", action=live_screen)])
    rows.append([MenuButton(text="❓ Как получить токен", action=f"{screen_id}:help")])
    rows.append([MenuButton(text="↩️ Назад", action="gateway:home")])
    return MenuScreen(
        screen_id=screen_id,
        title=title,
        body=body,
        rows=rows,
        parent="gateway:home",
    )


def build_gateway_discord(user_id: int | None) -> MenuScreen:
    return _build_platform_stub(
        "gateway:discord",
        "🎮 Discord",
        "Управляй Лилуш из Discord: подключи бота по токену.",
        live_screen="gateway:discord:live",
    )


def build_gateway_vk(user_id: int | None) -> MenuScreen:
    return _build_platform_stub(
        "gateway:vk",
        "🔵 VK",
        "Управляй Лилуш из VK: подключи сообщество по токену.",
        live_screen="gateway:vk:live",
    )


def build_gateway_link(user_id: int | None) -> MenuScreen:
    from . import identity

    body_lines = []
    codes = identity.active_codes_for(int(user_id)) if user_id is not None else []
    if not codes and user_id is not None:
        code = identity.generate_code(int(user_id))
        if code:
            codes = identity.active_codes_for(int(user_id))
    if codes:
        newest = max(codes, key=lambda c: float(c.get("expires_at", 0)))
        mins = max(1, int((float(newest.get("expires_at", 0)) - time.time()) // 60))
        body_lines.append(f"Твой код: {newest.get('code')}")
        body_lines.append(f"Действует ещё ~{mins} мин.")
    else:
        body_lines.append("Активных кодов нет (лимит — 3). Нажми «🔁 Новый код».")
    body_lines.append("")
    body_lines.append(
        "Напиши этот код боту в ЛИЧНЫЕ сообщения в Discord или VK — "
        "и та платформа получит права владельца."
    )
    rows = [
        [
            MenuButton(text="🔁 Новый код", action="gateway:link:new"),
            MenuButton(text="📋 Мои привязки", action="gateway:link:mine"),
        ],
        [MenuButton(text="↩️ Назад", action="gateway:home")],
    ]
    return MenuScreen(
        screen_id="gateway:link",
        title="🔗 Привязка аккаунта",
        body="\n".join(body_lines),
        rows=rows,
        parent="gateway:home",
    )


def build_gateway_link_mine(user_id: int | None) -> MenuScreen:
    from . import identity

    links = identity.get_links()
    rows: list[list[MenuButton]] = []
    if links:
        body = "Привязанные аккаунты (🗑 = отвязать, мгновенно разжалует в гостя):"
        for link in links:
            platform = str(link.get("platform", "?"))
            ext = str(link.get("external_id", "?"))
            label = str(link.get("label") or ext)
            icon = {"discord": "🎮", "vk": "🔵"}.get(platform, "🔗")
            rows.append(
                [
                    MenuButton(
                        text=f"🗑 {icon} {platform}: {label}",
                        action=f"gateway:unlink:{platform}:{ext}",
                    )
                ]
            )
    else:
        body = "Привязок пока нет. Выпиши код на экране «🔗 Привязка аккаунта»."
    rows.append([MenuButton(text="↩️ Назад", action="gateway:link")])
    return MenuScreen(
        screen_id="gateway:link:mine",
        title="📋 Мои привязки",
        body=body,
        rows=rows,
        parent="gateway:link",
    )


def build_gateway_status(user_id: int | None) -> MenuScreen:
    from . import identity

    lines = _platform_status_lines()
    links = identity.get_links()
    lines.append("")
    lines.append(f"Привязок аккаунтов: {len(links)}")

    # Живая диагностика адаптеров (этап 6, R10): аптайм + последняя ошибка.
    import time as _time

    for label, mod_name in (("🎮 Discord", "discord_gw"), ("🔵 VK", "vk_gw")):
        try:
            import importlib
            mod = importlib.import_module(
                f"bot.gateway.adapters.{mod_name}"
            )
            status = mod.get_status()
            state = str(status.get("state", "off"))
            since = float(status.get("since", 0) or 0)
            parts = [f"{label}: {state}"]
            if state == "online" and since:
                mins = max(0, int((_time.time() - since) // 60))
                parts.append(f"аптайм ~{mins} мин")
            error = status.get("error")
            if error:
                parts.append(f"ошибка: {error}")
            lines.append(" · ".join(parts))
        except Exception:  # noqa: BLE001 — диагностика не должна падать
            lines.append(f"{label}: недоступен")
    return MenuScreen(
        screen_id="gateway:status",
        title="📡 Статус платформ",
        body="\n".join(lines),
        rows=[
            [MenuButton(text="🔄 Обновить", action="gateway:status")],
            [MenuButton(text="↩️ Назад", action="gateway:home")],
        ],
        parent="gateway:home",
    )


def build_gateway_notify(user_id: int | None) -> MenuScreen:
    return MenuScreen(
        screen_id="gateway:notify",
        title="🔔 Уведомления",
        body=(
            "Дублирование уведомлений владельцу в Discord/VK появится "
            "на этапе broadcast — после подключения самих платформ."
        ),
        rows=[[MenuButton(text="↩️ Назад", action="gateway:home")]],
        parent="gateway:home",
    )


# --------------------------------------------------------------------------
# gateway action handlers (mutating actions, shared by TG and web)
# --------------------------------------------------------------------------

async def _act_site_rotate(action: str, ctx: GatewayContext) -> ScreenResult:
    from ..addons.vision.server import generate_link
    generate_link()
    return ScreenResult(screen=build_gateway_site(ctx.user_id))


async def _act_site_off(action: str, ctx: GatewayContext) -> ScreenResult:
    from ..addons.vision.server import disable_link
    disable_link()
    return ScreenResult(screen=build_gateway_site(ctx.user_id))


async def _act_site_on(action: str, ctx: GatewayContext) -> ScreenResult:
    from ..addons.vision.server import generate_link, get_link, is_enabled
    if not (is_enabled() and get_link()):
        generate_link()
    return ScreenResult(screen=build_gateway_site(ctx.user_id))


async def _act_link_new(action: str, ctx: GatewayContext) -> ScreenResult:
    from . import identity
    if ctx.user_id is None:
        return ScreenResult(error="forbidden", text="⛔ Только владелец.")
    code = identity.generate_code(int(ctx.user_id))
    if code is None:
        return ScreenResult(
            screen=build_gateway_link(ctx.user_id),
            text="⚠️ Лимит: не больше 3 активных кодов. Подожди, пока истекут.",
        )
    return ScreenResult(screen=build_gateway_link(ctx.user_id))


async def _act_platform_connect(action: str, ctx: GatewayContext) -> ScreenResult:
    """Этапы 2–3 подключены: connect ведёт на живую панель адаптера."""
    live_id = ("gateway:discord:live" if "discord" in action
               else "gateway:vk:live")
    screen = build_screen(live_id, ctx.user_id)
    if screen is not None:
        return ScreenResult(screen=screen)
    platform = "Discord" if "discord" in action else "VK"
    return ScreenResult(
        text=f"⚠️ Панель {platform} недоступна. Проверь логи бота.",
    )


async def _act_platform_help(action: str, ctx: GatewayContext) -> ScreenResult:
    if "discord" in action:
        text = (
            "❓ Как получить Discord Bot Token:\n"
            "1. discord.com/developers → New Application.\n"
            "2. Вкладка Bot → Reset Token → скопируй токен.\n"
            "3. Там же включи MESSAGE CONTENT INTENT "
            "(Privileged Gateway Intents).\n"
            "4. Вкладка OAuth2 → URL Generator → scope «bot» → пригласи "
            "бота на свой сервер."
        )
    else:
        text = (
            "❓ Как получить ключ сообщества VK:\n"
            "1. Управление сообществом → Настройки → Работа с API.\n"
            "2. «Создать ключ» с правом «Сообщения сообщества».\n"
            "3. Там же включи «Возможности ботов» и Long Poll API."
        )
    return ScreenResult(text=text)


async def _act_unlink(action: str, ctx: GatewayContext) -> ScreenResult:
    """``gateway:unlink:<platform>:<external_id>`` → drop the link (A9)."""
    from . import identity

    parts = action.split(":", 3)
    if len(parts) != 4:
        return ScreenResult(error="bad_action", text="⚠️ Некорректная привязка.")
    _, _, platform, external_id = parts
    identity.unlink(platform, external_id)
    return ScreenResult(screen=build_gateway_link_mine(ctx.user_id))


def _register_all() -> None:
    register_screen("main", build_main_screen)
    register_screen("settings", build_settings_screen)
    register_screen("gateway:home", build_gateway_home)
    register_screen("gateway:site", build_gateway_site)
    register_screen("gateway:discord", build_gateway_discord)
    register_screen("gateway:vk", build_gateway_vk)
    register_screen("gateway:link", build_gateway_link)
    register_screen("gateway:link:mine", build_gateway_link_mine)
    register_screen("gateway:status", build_gateway_status)
    register_screen("gateway:notify", build_gateway_notify)

    register_action("gateway:site:rotate", _act_site_rotate)
    register_action("gateway:site:off", _act_site_off)
    register_action("gateway:site:on", _act_site_on)
    register_action("gateway:link:new", _act_link_new)
    register_action("gateway:discord:connect", _act_platform_connect)
    register_action("gateway:vk:connect", _act_platform_connect)
    register_action("gateway:discord:help", _act_platform_help)
    register_action("gateway:vk:help", _act_platform_help)
    register_action_prefix("gateway:unlink:", _act_unlink)


_register_all()
