# Sync category registry: 8 categories, exclusion registry, resolvers, counters.
# Co-authored with CoCo
"""Единый реестр категорий синхронизации (ЧАСТЬ 18.1).

Источник истины: ``docs/PERSONAL_DATA_INVENTORY.md``. Модуль read-only —
никаких подключений к БД/S3, никаких записей в state или на диск.

Инварианты (закреплены unit-тестами):

* каждая сущность попадает РОВНО в одну категорию;
* ``other`` — обязательный catch-all: всё, что не попало в 1-7,
  автоматически падает сюда; новая сущность не может «выпасть» из sync;
* реестр исключений (``.frozen``, namespace ``vault``, tmp/bak-файлы
  стораджа, локальный журнал sync) — единственное законное «выпадение»
  из синхронизации; catch-all обязан его пропускать;
* файл, помеченный как секрет, резолвится в ``secrets`` независимо от
  расположения — ``media``/``other`` не могут его захватить.
"""

from __future__ import annotations

import fnmatch
import json
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

# --- категории ----------------------------------------------------------


class SyncCategory(str, Enum):
    """8 независимых категорий синхронизации (порядок = порядок ТЗ)."""

    ARTICLES = "articles"          # Статьи и черновики
    MEDIA = "media"                # Изображения, видео и остальные файлы
    FINGERPRINTS = "fingerprints"  # Отпечатки и технические идентификаторы
    ACCOUNTS = "accounts"          # Логины и учётные записи
    SECRETS = "secrets"            # Пароли, токены, cookie и session state
    MODULE_STATE = "module_state"  # Настройки и состояние модулей
    HISTORY = "history"            # История, задания, проекты и логи
    OTHER = "other"                # Catch-all: всё неотнесённое


ALL_CATEGORIES: tuple[SyncCategory, ...] = tuple(SyncCategory)

CATEGORY_LABEL: dict[SyncCategory, str] = {
    SyncCategory.ARTICLES: "Статьи и черновики",
    SyncCategory.MEDIA: "Изображения, видео и остальные файлы",
    SyncCategory.FINGERPRINTS: "Отпечатки и технические идентификаторы",
    SyncCategory.ACCOUNTS: "Логины и учётные записи",
    SyncCategory.SECRETS: "Секреты: пароли, токены, cookie и session state",
    SyncCategory.MODULE_STATE: "Настройки и состояние модулей",
    SyncCategory.HISTORY: "История, задания, проекты и логи",
    SyncCategory.OTHER: "Остальные данные",
}

# --- реестр исключений ---------------------------------------------------
# Единственное законное «выпадение» из синхронизации. Фиксируется СПИСКОМ,
# а не молчаливой логикой (приёмка 18.1). Kill-switch (`bot/selfdestruct.py`)
# имеет приоритет над sync — его артефакты не покидают инстанс.

# Имена файлов/glob-паттернов относительно DATA_DIR.
EXCLUSION_FILE_PATTERNS: tuple[str, ...] = (
    ".frozen",              # необратимый маркер freeze kill-switch
    "state.json.tmp",       # незавершённая атомарная запись стораджа
    "state.json.bak",       # предыдущая версия state (те же секреты)
    "sync_journal.db",      # локальный журнал sync (появится в 18.3+)
    "sync_journal.db-*",    # sqlite -wal / -shm / -journal спутники
)

# Dotted-path-префиксы ключей state.json (см. state_path ниже).
EXCLUSION_STATE_PREFIXES: tuple[str, ...] = (
    "_settings.addons.vault",  # конфигурация kill-switch, namespace vault
)


def is_excluded_file(path: str | Path) -> bool:
    """True, если файл входит в реестр исключений (по имени файла)."""
    name = Path(path).name
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUSION_FILE_PATTERNS)


def is_excluded_state_path(dotted: str) -> bool:
    """True, если dotted-путь ключа state входит в реестр исключений."""
    return any(
        dotted == p or dotted.startswith(p + ".")
        for p in EXCLUSION_STATE_PREFIXES
    )


# --- секреты-файлы на диске ----------------------------------------------
# Из инвентаря §3: FILE-секреты. Резолвятся в SECRETS независимо от того,
# что лежат в DATA_DIR рядом с медиа. Пути/glob'ы — относительно DATA_DIR.

SECRET_FILE_PATTERNS: tuple[str, ...] = (
    "yt_cookies.txt",     # cookies YouTube в открытом виде (bot/config.py)
    "session/*",          # браузерные сессии stealth (wizard, render_deploy)
    "session/**/*",
    "*cookies*",          # страховка: любой cookies-файл в DATA_DIR
    "*.storage_state",    # выгрузки Playwright storage state
)


def is_secret_file(rel_path: str | Path) -> bool:
    """True, если файл (путь относительно DATA_DIR) — FILE-секрет."""
    rel = str(Path(rel_path).as_posix()).lstrip("/")
    name = Path(rel).name
    for pat in SECRET_FILE_PATTERNS:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


# --- резолвер: dotted-путь ключа state.json → категория -------------------
# Правила упорядочены: исключения → секреты → тематические → catch-all.
# Формат dotted-пути: "_settings.keys.openrouter",
# "_settings.addons.slack.token", "12345.history", …

# STATE-секреты (инвентарь §1.2 и §2.1). Wildcard `*` — один сегмент.
_SECRET_STATE_PATTERNS: tuple[str, ...] = (
    "_settings.keys",
    "_settings.keys.*",
    "_settings.external_tools",
    "_settings.external_tools.*",
    "_settings.media.slots.*.api_key",
    "_settings.media.templates",
    "_settings.media.templates.*",
    "_settings.addons.slack.token",
    # защищённое хранилище 18.4: ciphertext-записи категории secrets
    "_settings.addons.masterroom.secret_store",
    "_settings.addons.masterroom.secret_store.*",
    "_settings.addons.mailbox.password",
    "_settings.addons.helpzavr.groq_api_key",
    "_settings.addons.helpzavr.openrouter_api_key",
    "_settings.addons.editor_agent.password",
    "_settings.addons.editor_agent.api_key",
    "_settings.addons.masterroom.token",
    "_settings.addons.masterroom.servers",
    "_settings.addons.masterroom.render_bot_token",
    "_settings.addons.masterroom.render_github_token",
    "_settings.addons.masterroom.render_gh_password",
    "_settings.addons.masterroom.gh_web_password",
    "_settings.addons.masterroom.render_password",
    "_settings.addons.debates.debate_owner_token",
    "_settings.addons.debates.admin_connect_secret",
    "_settings.addons.test.token",
    "_settings.addons.fable5.token",
    "_settings.addons.projectmap.token",
    "_settings.addons.guide.token",
    "_settings.addons.vision.token",
    "_settings.addons.obuchalka.*_token",
)

_ACCOUNT_STATE_PATTERNS: tuple[str, ...] = (
    # учётки без секретов; github_accounts несут token_ref (имя env-var),
    # а поле plaintext `token` внутри — предмет миграции 18.4
    "_settings.addons.masterroom.github_accounts",
    "_settings.addons.masterroom.render_email",
    "_settings.addons.masterroom.render_gh_login",
    "_settings.addons.masterroom.gh_web_login",
    "_settings.addons.mailbox.address",
    "_settings.addons.mailbox.login",
    "_settings.addons.swapface.user_token_ref",
    "_settings.owner_id",
    "_settings.co_owners",
)

_FINGERPRINT_STATE_PATTERNS: tuple[str, ...] = (
    "_settings.addons.stealth",
    "_settings.addons.stealth.*",
    "_settings.addons.stealth_fp",
    "_settings.addons.stealth_fp.*",
)

_HISTORY_STATE_PATTERNS: tuple[str, ...] = (
    "_settings.addons.masterroom.projects",
)

_ARTICLE_STATE_PATTERNS: tuple[str, ...] = (
    "_settings.addons.promptbox",
    "_settings.addons.promptbox.*",
    "_settings.addons.publication",
    "_settings.addons.publication.*",
)


def _match_dotted(dotted: str, patterns: Iterable[str]) -> bool:
    parts = dotted.split(".")
    for pat in patterns:
        pparts = pat.split(".")
        # префиксное совпадение: правило покрывает и вложенные ключи
        if len(pparts) <= len(parts):
            if all(
                fnmatch.fnmatch(seg, pseg)
                for seg, pseg in zip(parts, pparts)
            ):
                return True
    return False


def category_for_state_path(dotted: str) -> SyncCategory | None:
    """Категория для dotted-пути ключа ``state.json``.

    ``None`` — запись реестра исключений (не синхронизируется вовсе).
    Всё неопознанное падает в :attr:`SyncCategory.OTHER` (catch-all).
    """
    dotted = (dotted or "").strip(".")
    if not dotted:
        return SyncCategory.OTHER
    if is_excluded_state_path(dotted):
        return None
    # секреты — раньше любых тематических правил
    if _match_dotted(dotted, _SECRET_STATE_PATTERNS):
        return SyncCategory.SECRETS
    if _match_dotted(dotted, _FINGERPRINT_STATE_PATTERNS):
        return SyncCategory.FINGERPRINTS
    if _match_dotted(dotted, _ACCOUNT_STATE_PATTERNS):
        return SyncCategory.ACCOUNTS
    if _match_dotted(dotted, _ARTICLE_STATE_PATTERNS):
        return SyncCategory.ARTICLES
    if _match_dotted(dotted, _HISTORY_STATE_PATTERNS):
        return SyncCategory.HISTORY
    # per-user записи (числовые top-level ключи) — история переписки
    head = dotted.split(".", 1)[0]
    if head.isdigit():
        return SyncCategory.HISTORY
    # остальной _settings (модель, тумблеры, addon-настройки) — module_state
    if dotted == "_settings" or dotted.startswith("_settings."):
        return SyncCategory.MODULE_STATE
    return SyncCategory.OTHER


# --- резолвер: файл на диске → категория ----------------------------------

_MEDIA_DIR_PREFIXES: tuple[str, ...] = (
    "storage/",    # файловый менеджер экрана «💾 Хранилище»
    "downloads/",  # yt-dlp
    "releases/",   # publisher
)

_HISTORY_DIR_PREFIXES: tuple[str, ...] = (
    "projects/",   # PROJECTS_DIR
)

_HISTORY_FILE_PATTERNS: tuple[str, ...] = (
    "jobs.db",     # очередь заданий bot/jobs.py
    "jobs.db-*",
    "*.log",
)


def category_for_file(rel_path: str | Path) -> SyncCategory | None:
    """Категория для файла (путь относительно ``DATA_DIR``).

    ``None`` — реестр исключений. ``state.json`` намеренно не имеет
    файловой категории: его содержимое раскладывается по ключам через
    :func:`category_for_state_path`, поэтому сам контейнер тоже ``None``.
    Порядок правил: исключения → контейнер state → секреты → тематические
    → catch-all ``other``.
    """
    rel = str(Path(rel_path).as_posix()).lstrip("/")
    if not rel:
        return None
    if is_excluded_file(rel):
        return None
    if rel == "state.json":
        return None  # контейнер; категоризация по-ключево
    if is_secret_file(rel):
        return SyncCategory.SECRETS
    for prefix in _HISTORY_DIR_PREFIXES:
        if rel.startswith(prefix):
            return SyncCategory.HISTORY
    name = Path(rel).name
    if any(fnmatch.fnmatch(name, p) for p in _HISTORY_FILE_PATTERNS):
        return SyncCategory.HISTORY
    for prefix in _MEDIA_DIR_PREFIXES:
        if rel.startswith(prefix):
            return SyncCategory.MEDIA
    return SyncCategory.OTHER


# --- подсчёт записей / байтов по категориям --------------------------------


def _flatten_state(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield (dotted_path, leaf_value) для листовых значений state-дерева."""
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            sub = f"{prefix}.{k}" if prefix else str(k)
            yield from _flatten_state(v, sub)
    else:
        if prefix:
            yield prefix, obj


def _leaf_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))


def count_state(state: dict[str, Any]) -> dict[SyncCategory, dict[str, int]]:
    """Подсчёт по state-дереву: {category: {"records": n, "bytes": b}}.

    Записи реестра исключений не учитываются нигде (легальное выпадение).
    """
    out: dict[SyncCategory, dict[str, int]] = {
        c: {"records": 0, "bytes": 0} for c in ALL_CATEGORIES
    }
    for dotted, value in _flatten_state(state or {}):
        cat = category_for_state_path(dotted)
        if cat is None:
            continue
        out[cat]["records"] += 1
        out[cat]["bytes"] += _leaf_bytes(value)
    return out


def count_files(data_dir: str | Path) -> dict[SyncCategory, dict[str, int]]:
    """Подсчёт по файлам ``DATA_DIR``: {category: {"records", "bytes"}}.

    Read-only обход; ошибки stat (гонки с удалением) молча пропускаются.
    """
    out: dict[SyncCategory, dict[str, int]] = {
        c: {"records": 0, "bytes": 0} for c in ALL_CATEGORIES
    }
    root = Path(data_dir)
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            cat = category_for_file(rel)
            if cat is None:
                continue
            out[cat]["records"] += 1
            out[cat]["bytes"] += path.stat().st_size
        except OSError:
            continue
    return out


def count_all(
    state: dict[str, Any] | None = None,
    data_dir: str | Path | None = None,
) -> dict[SyncCategory, dict[str, int]]:
    """Суммарный подсчёт state + файлы (для экрана 18.2 и предпросмотра 18.5).

    Без аргументов берёт живой state через ``bot.storage.storage`` и
    ``DATA_DIR`` из конфигурации (ленивые импорты — без побочных эффектов
    на import модуля).
    """
    if state is None:
        from ....storage import storage  # noqa: PLC0415 — ленивый импорт

        state = storage._state  # noqa: SLF001 — read-only доступ
    if data_dir is None:
        from ....config import DATA_DIR  # noqa: PLC0415

        data_dir = DATA_DIR
    totals = count_state(state)
    for cat, agg in count_files(data_dir).items():
        totals[cat]["records"] += agg["records"]
        totals[cat]["bytes"] += agg["bytes"]
    return totals
