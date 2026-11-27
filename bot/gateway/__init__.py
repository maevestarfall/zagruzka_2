"""Кроссплатформенный шлюз «Лилуш» (Этап 1 ТЗ-1).

Единое дерево экранов бота для всех платформ:

- ``menu``      — модель MenuScreen/MenuButton + реестр экранов;
- ``screens``   — билдеры экранов (main, settings, gateway:*);
- ``dispatch``  — единая маршрутизация действий (dispatch_action);
- ``identity``  — привязка внешних аккаунтов к Telegram-ролям;
- ``adapters``  — рендеры под платформы (telegram, web);
- ``wizard_ui`` — Telegram-роутер для экранов gateway:*.

Контракт: Telegram остаётся эталоном UX; остальные платформы
рендерят те же экраны через адаптеры.
"""

import logging as _logging

#: Текущая версия схемы ветки ``addons.gateway`` в state.json (ТЗ-1 §9Б.9).
SCHEMA_VERSION = 1


def ensure_schema() -> None:
    """Versioned-config guard for the ``gateway`` state branch (§9Б.9).

    * version < current  → run migrations (dict → dict, one per version);
    * version > current  (code was rolled back) → do NOT touch the branch,
      log loudly;
    * missing            → stamp the current version.
    """
    log = _logging.getLogger("gateway.schema")
    try:
        from ..addons import state as addon_state

        stored = int(addon_state.get("gateway", "schema_version", 0) or 0)
        if stored > SCHEMA_VERSION:
            log.error(
                "gateway schema_version %s is newer than code (%s) — "
                "branch left untouched (code rollback?)",
                stored, SCHEMA_VERSION,
            )
            return
        if stored < SCHEMA_VERSION:
            # Migrations ladder: v0 → v1 is just stamping the version —
            # the branch layout of этап 1 IS version 1.
            addon_state.set_("gateway", "schema_version", SCHEMA_VERSION)
            if stored:
                log.info("gateway schema migrated %s → %s", stored, SCHEMA_VERSION)
    except Exception:  # noqa: BLE001
        log.exception("gateway schema check failed (non-fatal)")


from . import dispatch, identity, menu, screens  # noqa: F401,E402
from .dispatch import dispatch_action  # noqa: F401,E402
from .menu import MenuButton, MenuScreen, build_screen, register_screen  # noqa: F401,E402

# Этап 4 (часть C): нейтральные экраны волн 2–3. Модуль сам регистрирует
# себя через публичные API; деградация не должна ронять импорт шлюза.
try:
    from . import screens_wave2  # noqa: F401,E402
except Exception:  # noqa: BLE001
    _logging.getLogger("gateway").exception("screens_wave2 failed to load")

# Этап 4 (фикс 1.5): недостающие экраны волны 2 (roles, memory, access,
# terminal, brains, slack). Тот же паттерн саморегистрации, что и wave2.
try:
    from . import screens_wave2b  # noqa: F401,E402
except Exception:  # noqa: BLE001
    _logging.getLogger("gateway").exception("screens_wave2b failed to load")

# Этапы 2–3 (части A/B): адаптеры Discord и VK. Тяжёлые зависимости
# (discord.py, aiohttp long-poll) импортируются лениво внутри start(),
# поэтому сам импорт безопасен даже на голом Python (A10).
try:
    from .adapters import discord_gw  # noqa: F401,E402
except Exception:  # noqa: BLE001
    _logging.getLogger("gateway").exception("discord adapter failed to load")
try:
    from .adapters import vk_gw  # noqa: F401,E402
except Exception:  # noqa: BLE001
    _logging.getLogger("gateway").exception("vk adapter failed to load")

# Этап 5 (§6А, R11–R12): медиашина. Регистрирует экран «🗂 Мои медиа»
# и действия gateway:media:*; выключена по умолчанию (media_enabled).
try:
    from . import media_bus  # noqa: F401,E402
except Exception:  # noqa: BLE001
    _logging.getLogger("gateway").exception("media_bus failed to load")

# Этап 6 (R8): broadcast. Заменяет заглушку экрана «🔔 Уведомления»
# на рабочие чекбоксы; все флаги по умолчанию false (dark launch §8.1).
try:
    from . import broadcast  # noqa: F401,E402
except Exception:  # noqa: BLE001
    _logging.getLogger("gateway").exception("broadcast failed to load")

ensure_schema()
