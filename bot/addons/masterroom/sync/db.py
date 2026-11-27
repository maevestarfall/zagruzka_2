# PostgreSQL adapter for personal-data sync (ЧАСТЬ 18.3).
# Co-authored with CoCo
"""Асинхронный адаптер внешнего PostgreSQL (ЧАСТЬ 18.3 §1).

Правила (18.0 общие, 18.3 §1):

* Подключение ТОЛЬКО по ``DATABASE_URL`` из env (``bot.config``).
  Нет переменной → адаптер честно возвращает «не настроено», ничего
  не импортирует и не падает — штатный локальный режим.
* **Ленивый импорт**: ``asyncpg`` импортируется ВНУТРИ функций (паттерн
  Discord-адаптера). Без ``DATABASE_URL`` библиотека не загружается при
  старте — бюджет памяти Render 512MB.
* Один стандартный интерфейс для Neon / Yandex Managed PostgreSQL /
  любого совместимого PostgreSQL, включая ``sslmode=require``.
  НЕ YDB под видом PostgreSQL.
* Все запросы — только параметризованные (плейсхолдеры asyncpg ``$1``).
* ``DATABASE_URL`` никогда не логируется; :func:`db_label` возвращает
  только host + имя БД (без user/password/query-параметров).
* Ошибки наружу — только классом (:func:`classify_error`): «нет доступа»,
  «таймаут», «SSL» и т.п., без URL и учёток.

Схема всех таблиц бота — ТОЛЬКО ``masterroom_sync`` (18.0 п.5); миграции —
:mod:`bot.addons.masterroom.sync.migrations`. ``public`` и таблицы каркаса
сайта не трогаются никогда, даже на общей БД.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Таймауты сети: проверка не должна вешать event loop надолго.
CONNECT_TIMEOUT_S = 15.0
QUERY_TIMEOUT_S = 15.0


# ─── Конфигурация ────────────────────────────────────────────────────────────


def get_database_url() -> str:
    """``DATABASE_URL`` из env (уже проверен по формату в bot.config)."""
    from ....config import DATABASE_URL

    return DATABASE_URL or ""


def is_configured() -> bool:
    return bool(get_database_url())


def db_label(url: str | None = None) -> str:
    """Безопасная метка назначения: ТОЛЬКО host + имя БД.

    Без user/password/port/query-параметров — метка попадает в state
    и на экран (18.3 §1).
    """
    raw = url if url is not None else get_database_url()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        dbname = (parsed.path or "").lstrip("/").split("?", 1)[0]
    except ValueError:
        return ""
    if not host:
        return ""
    return f"{host}/{dbname}" if dbname else host


# ─── Классификация ошибок (безопасный публичный текст) ──────────────────────


def classify_error(exc: BaseException) -> str:
    """Класс ошибки для UI/state — БЕЗ URL, учёток и сырого текста.

    Возвращаемая строка безопасна сама по себе; дополнительно её всё
    равно прогоняет write-time санитайзер state (18.2 §2).
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "таймаут подключения к БД"
    name = type(exc).__name__
    mod = type(exc).__module__ or ""
    text = str(exc).lower()
    if "ssl" in name.lower() or "ssl" in text or "certificate" in text:
        return "ошибка SSL при подключении к БД"
    if mod.startswith("asyncpg"):
        low = name.lower()
        if "password" in low or "auth" in low or "privilege" in low:
            return "нет доступа (проверьте пользователя/пароль)"
        if "insufficient" in low or "permission" in text:
            return "нет доступа (недостаточно прав)"
        if "invalidcatalogname" in low:
            return "база данных не найдена"
        if "cannotconnectnow" in low or "connectiondoesnotexist" in low \
                or "connectionfailure" in low:
            return "не удалось подключиться к БД"
        return f"ошибка PostgreSQL ({name})"
    if isinstance(exc, (ConnectionError, OSError)):
        return "сеть/хост недоступны"
    return f"ошибка подключения ({name})"


# ─── Подключение ─────────────────────────────────────────────────────────────


async def connect() -> Any:
    """Открыть соединение asyncpg или вернуть ``None``, если не настроено.

    ``asyncpg`` импортируется здесь (ленивый импорт). ``sslmode`` /
    ``channel_binding`` в query-строке DSN asyncpg разбирает сам
    (Neon / Yandex Managed PostgreSQL работают из коробки).
    Исключения подключения пробрасываются вызывающему — тот обязан
    перевести их в безопасный текст через :func:`classify_error`.
    """
    url = get_database_url()
    if not url:
        return None
    import asyncpg  # ленивый импорт — см. правило бюджета памяти

    return await asyncpg.connect(
        dsn=url,
        timeout=CONNECT_TIMEOUT_S,
        command_timeout=QUERY_TIMEOUT_S,
    )


# ─── «Проверить подключение» (18.3 §4) ───────────────────────────────────────


async def check_connection() -> dict[str, Any]:
    """Полная последовательность проверки подключения.

    connect → ``SELECT 1`` → проверка/применение миграций → тестовая
    запись+чтение+удаление в СЛУЖЕБНОЙ таблице ``sync_healthcheck``
    (НЕ в таблицах данных). Ничего не пишет в state — это делает
    вызывающий handler (гарантия 18.3 §4: обрыв проверки не портит
    локальные данные).

    Возвращает dict:
    ``{"ok": bool, "label": str, "schema_version": int,
       "error_public": str}``
    """
    result: dict[str, Any] = {
        "ok": False,
        "label": "",
        "schema_version": 0,
        "error_public": "",
    }
    if not is_configured():
        result["error_public"] = "внешняя БД не настроена (нет DATABASE_URL)"
        return result

    conn = None
    try:
        conn = await asyncio.wait_for(connect(), timeout=CONNECT_TIMEOUT_S + 5)
        if conn is None:  # гонка: env исчез между is_configured и connect
            result["error_public"] = "внешняя БД не настроена (нет DATABASE_URL)"
            return result

        # 1. SELECT 1 — БД жива.
        one = await conn.fetchval("SELECT 1")
        if one != 1:
            result["error_public"] = "БД ответила некорректно на SELECT 1"
            return result

        # 2. Миграции схемы masterroom_sync (идемпотентно, в транзакциях).
        from .migrations import apply_migrations

        result["schema_version"] = await apply_migrations(conn)

        # 3. Тестовая запись + чтение + удаление в СЛУЖЕБНОЙ таблице.
        probe = f"probe-{uuid.uuid4()}"
        await conn.execute(
            "INSERT INTO masterroom_sync.sync_healthcheck (probe_id) "
            "VALUES ($1)",
            probe,
        )
        back = await conn.fetchval(
            "SELECT probe_id FROM masterroom_sync.sync_healthcheck "
            "WHERE probe_id = $1",
            probe,
        )
        await conn.execute(
            "DELETE FROM masterroom_sync.sync_healthcheck "
            "WHERE probe_id = $1",
            probe,
        )
        if back != probe:
            result["error_public"] = "тестовая запись/чтение не совпали"
            return result

        result["ok"] = True
        result["label"] = db_label()
        return result
    except BaseException as exc:  # noqa: BLE001 — переводим в безопасный класс
        # НИКОГДА не логируем сам URL; текст исключения тоже не пишем
        # в лог целиком — asyncpg может включить туда DSN-параметры.
        logger.warning("pdsync db check failed: %s", classify_error(exc))
        result["error_public"] = classify_error(exc)
        return result
    finally:
        if conn is not None:
            try:
                await asyncio.wait_for(conn.close(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
