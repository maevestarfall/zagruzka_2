# Идемпотентная миграция legacy plaintext-секретов в secret_store (ЧАСТЬ 18.4 §3).
# Co-authored with CoCo
"""Миграция legacy plaintext-секретов в защищённое хранилище (18.4 §3).

Источник списка — инвентарь ``docs/PERSONAL_DATA_INVENTORY.md`` (18.1),
закреплённый кодом в :mod:`.categories` (``_SECRET_STATE_PATTERNS``).
Мигрируются ТОЛЬКО секреты, живущие в state; env-секреты (паттерн
``token_ref``: в state лежит только ИМЯ env-переменной) миграции и
синхронизации НЕ подлежат. Классификация — по фактическому источнику:
например Slack-токен живёт в ``state.json`` (``addons.slack.token``,
``bot/addons/slack/credentials.py``: приоритет №1 — addon_state) и
мигрирует как state-секрет.

Каждый секрет:

1. читается из живого ``state.json``;
2. шифруется и пишется в secret_store (ключ записи = dotted-путь;
   для элементов списков — стабильный ``id`` записи, НЕ индекс);
3. проверяется обратным чтением + расшифровкой + сверкой байт-в-байт;
4. помечается мигрированным, но **plaintext-оригинал НЕ удаляется**
   (удаление — только 18.7; копия живёт и в ``state.json.bak``).

Идемпотентность: повторный запуск НЕ создаёт дублей и НЕ перешифровывает
уже мигрированное — если расшифровка существующей записи совпадает с
текущим plaintext, запись пропускается (``skipped``). Если plaintext
изменился после миграции — запись перешифровывается (``updated``).

Читающий код переключается ПОСТЕПЕННО через
:func:`.secret_store.read_secret_with_fallback` — до 18.7 каждый
читатель обязан уметь читать legacy plaintext (нет записи / нет master
key). Ни один живой коннектор не перестаёт работать из-за миграции.

Без master key миграция честно возвращает ``available=False`` и ничего
не делает — бот не падает.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import secret_store
from .categories import _SECRET_STATE_PATTERNS, _match_dotted

logger = logging.getLogger(__name__)

# Бакет самого хранилища — не мигрировать сам в себя.
_STORE_PREFIX = "_settings.addons.masterroom.secret_store"

# Поля-ссылки на env (паттерн token_ref): НЕ секреты, НЕ мигрируются.
_ENV_REF_FIELDS = frozenset({"token_ref", "user_token_ref"})

# Списки masterroom с plaintext-полем token и стабильным id (18.1 §2.1).
_LIST_SECRETS: tuple[tuple[str, str], ...] = (
    # (dotted-путь списка, секретное поле элемента)
    ("_settings.addons.masterroom.servers", "token"),
    ("_settings.addons.masterroom.github_accounts", "token"),
)


@dataclass
class MigrationReport:
    """Итог миграции — без единого значения секрета внутри."""

    available: bool = True
    reason: str = ""
    migrated: list[str] = field(default_factory=list)   # новые записи
    updated: list[str] = field(default_factory=list)    # plaintext изменился
    skipped: list[str] = field(default_factory=list)    # уже мигрировано
    failed: list[str] = field(default_factory=list)     # сверка не прошла

    @property
    def ok(self) -> bool:
        return self.available and not self.failed


def _iter_state_secret_leaves(state: dict[str, Any]):
    """Yield (dotted_key, plaintext_str) для всех STATE-секретов инвентаря.

    Скалярные листы — по ``_SECRET_STATE_PATTERNS``; элементы списков
    (servers/github_accounts) — отдельно, по стабильному ``id``.
    Env-ref-поля и сам secret_store пропускаются.
    """
    from .categories import _flatten_state  # noqa: PLC0415

    list_paths = {p for p, _ in _LIST_SECRETS}
    for dotted, value in _flatten_state(state or {}):
        if dotted == _STORE_PREFIX or dotted.startswith(_STORE_PREFIX + "."):
            continue
        if dotted.rsplit(".", 1)[-1] in _ENV_REF_FIELDS:
            continue
        if dotted in list_paths:
            continue  # списки обрабатываются ниже поэлементно
        if not _match_dotted(dotted, _SECRET_STATE_PATTERNS):
            continue
        if isinstance(value, str) and value.strip():
            yield dotted, value
        elif isinstance(value, (dict, list)) and value:
            # шаблоны/структуры с авторизацией — сериализуем канонично
            yield dotted, json.dumps(
                value, ensure_ascii=False, sort_keys=True,
            )

    # списки с plaintext-полем token: ключ по стабильному id, не по индексу
    for list_path, secret_field in _LIST_SECRETS:
        node: Any = state
        for seg in list_path.split("."):
            node = node.get(seg) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, list):
            continue
        for item in node:
            if not isinstance(item, dict):
                continue
            token = str(item.get(secret_field) or "").strip()
            if not token:
                continue  # основной путь github_accounts — token_ref (env)
            stable_id = str(item.get("id") or "").strip()
            if not stable_id:
                continue
            yield f"{list_path}.{stable_id}.{secret_field}", token


def migrate_plaintext_secrets(
    state: dict[str, Any] | None = None,
) -> MigrationReport:
    """Выполнить (идемпотентную) миграцию. Plaintext НЕ удаляется.

    ``state`` — для тестов; по умолчанию живой ``storage._state``.
    """
    report = MigrationReport()
    if not secret_store.is_available():
        report.available = False
        report.reason = secret_store.secrets_unavailable_reason()
        logger.info(
            "pdsync migrate: secrets category unavailable (%s) — no-op",
            report.reason,
        )
        return report

    if state is None:
        from ....storage import storage  # noqa: PLC0415

        state = storage._state  # noqa: SLF001 — read-only обход

    for key, plaintext in _iter_state_secret_leaves(state):
        try:
            if secret_store.has_secret(key):
                existing = secret_store.get_secret(key)
                if existing == plaintext:
                    report.skipped.append(key)   # no-op: уже мигрировано
                    continue
                secret_store.put_secret(key, plaintext, migrated_from="state")
                verified = secret_store.get_secret(key)
                if verified != plaintext:
                    report.failed.append(key)
                    continue
                report.updated.append(key)
                continue
            secret_store.put_secret(key, plaintext, migrated_from="state")
            # приёмка: обратное чтение + расшифровка + сверка
            verified = secret_store.get_secret(key)
            if verified != plaintext:
                report.failed.append(key)
                continue
            report.migrated.append(key)
        except (secret_store.CryptoUnavailableError,
                secret_store.IntegrityError) as exc:
            logger.error(
                "pdsync migrate: %r failed: %s (value not logged)",
                key, type(exc).__name__,
            )
            report.failed.append(key)

    logger.info(
        "pdsync migrate: migrated=%d updated=%d skipped=%d failed=%d",
        len(report.migrated), len(report.updated),
        len(report.skipped), len(report.failed),
    )
    return report


async def migrate_plaintext_secrets_async(
    state: dict[str, Any] | None = None,
) -> MigrationReport:
    """argon2/AES над множеством записей — вне event loop (18.4 запреты)."""
    return await asyncio.to_thread(migrate_plaintext_secrets, state)
