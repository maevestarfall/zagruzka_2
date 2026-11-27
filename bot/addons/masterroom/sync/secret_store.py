# Защищённое локальное хранилище секретов категории `secrets` (ЧАСТЬ 18.4 §2).
# Co-authored with CoCo
"""Защищённый слой хранения секретов (ЧАСТЬ 18.4 §2).

Секреты категории ``secrets`` хранятся ТОЛЬКО зашифрованными (envelope
encryption из :mod:`.crypto`). Ciphertext лежит в общем ``state.json``
под ключом ``_settings.addons.masterroom.secret_store`` — запись идёт
через :mod:`bot.addons.state` → атомарный ``storage._save()``
(tmp → fsync → os.replace), никакого параллельного слоя записи
(правило 18.0 §2).

Формат бакета::

    secret_store: {
        "entries": {
            "<stable-key>": {          # ключ записи = dotted-путь источника
                ...envelope...,        # см. crypto.encrypt(): v/alg/nonce/ct/…
                "meta": {
                    "migrated_from": "state" | "file",
                    "migrated_at": <epoch>,
                },
            },
        },
    }

Инварианты:

* ключи записей стабильны (dotted-путь plaintext-источника из инвентаря
  18.1) — повторная миграция попадает в ту же запись, дублей нет;
* значения — только ciphertext + метаданные; НИКАКИХ ключей шифрования
  (master/data key) в state (18.0 §3);
* aad каждого envelope = ключ записи: подмена ciphertext между записями
  ловится проверкой целостности;
* без master key хранилище честно недоступно (:func:`is_available`),
  никакие операции не падают с трейсами секретов.

Секреты-ФАЙЛЫ (``DATA_DIR/yt_cookies.txt``, ``session/*`` — инвентарь
18.1 §3) шифруются функцией :func:`encrypt_file_payload` ПЕРЕД любой
отправкой наружу (18.5): в S3 такой файл может попасть только как
ciphertext. Локальный plaintext-файл в 18.4 НЕ трогается.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .crypto import (
    CryptoUnavailableError,
    IntegrityError,
    decrypt,
    encrypt,
    is_available as crypto_available,
    unavailable_reason,
)

# Ключ бакета внутри addon-state masterroom. Итоговый dotted-путь:
# `_settings.addons.masterroom.secret_store` — в categories.py он
# закреплён за категорией `secrets`.
_ADDON = "masterroom"
_BUCKET_KEY = "secret_store"

__all__ = [
    "CryptoUnavailableError",
    "IntegrityError",
    "delete_secret",
    "encrypt_file_payload",
    "decrypt_file_payload",
    "get_secret",
    "get_secret_async",
    "has_secret",
    "is_available",
    "list_secret_keys",
    "put_secret",
    "put_secret_async",
    "read_secret_with_fallback",
    "secrets_unavailable_reason",
]


def is_available() -> bool:
    """True, если хранилище готово (master key + библиотека)."""
    return crypto_available()


def secrets_unavailable_reason() -> str:
    """Безопасный текст причины недоступности категории ``secrets``."""
    return unavailable_reason()


# ─── низкоуровневый доступ к бакету ──────────────────────────────────────────


def _bucket() -> dict[str, Any]:
    from ... import state as addon_state  # noqa: PLC0415 — ленивый импорт

    raw = addon_state.get(_ADDON, _BUCKET_KEY, None)
    if not isinstance(raw, dict):
        raw = {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        raw = {"entries": {}}
    return raw


def _save_bucket(bucket: dict[str, Any]) -> None:
    from ... import state as addon_state  # noqa: PLC0415

    addon_state.set_(_ADDON, _BUCKET_KEY, bucket)


# ─── CRUD ────────────────────────────────────────────────────────────────────


def put_secret(
    key: str,
    plaintext: str | bytes,
    *,
    migrated_from: str = "state",
) -> None:
    """Зашифровать и сохранить секрет под стабильным ключом ``key``.

    Повторный вызов с тем же ключом перезаписывает ту же запись
    (дублей нет). Plaintext-источник НЕ трогается (удаление — 18.7).
    """
    k = (key or "").strip()
    if not k:
        raise ValueError("пустой ключ записи secret_store")
    envelope = encrypt(plaintext, aad=k)
    envelope["meta"] = {
        "migrated_from": migrated_from,
        "migrated_at": int(time.time()),
    }
    bucket = _bucket()
    bucket["entries"][k] = envelope
    _save_bucket(bucket)


def get_secret(key: str) -> str | None:
    """Расшифровать секрет. ``None`` — записи нет.

    Порча/подмена ciphertext → :class:`IntegrityError` (явная ошибка,
    не «тихое» чтение мусора). Нет master key →
    :class:`CryptoUnavailableError`.
    """
    entry = _bucket()["entries"].get((key or "").strip())
    if not isinstance(entry, dict):
        return None
    envelope = {kk: vv for kk, vv in entry.items() if kk != "meta"}
    return decrypt(envelope, aad=(key or "").strip()).decode("utf-8")


def has_secret(key: str) -> bool:
    """True, если зашифрованная запись существует (без расшифровки)."""
    return isinstance(_bucket()["entries"].get((key or "").strip()), dict)


def list_secret_keys() -> list[str]:
    return sorted(_bucket()["entries"].keys())


def delete_secret(key: str) -> bool:
    bucket = _bucket()
    if (key or "").strip() in bucket["entries"]:
        del bucket["entries"][(key or "").strip()]
        _save_bucket(bucket)
        return True
    return False


# ─── fallback-чтение (18.4 §3 п.5) ───────────────────────────────────────────


def read_secret_with_fallback(key: str, legacy_plaintext: str | None) -> str | None:
    """Единый паттерн ПОСТЕПЕННОГО переключения читателей.

    Порядок: зашифрованная запись (если есть master key и запись) →
    legacy plaintext как раньше. Ни один живой коннектор не перестаёт
    работать из-за миграции: при любом сбое защищённого слоя (нет ключа,
    нет записи, порча) честно возвращается legacy-значение.

    ``IntegrityError`` намеренно НЕ глотается молча — логируется факт
    (без значения секрета) и происходит откат на legacy.
    """
    try:
        value = get_secret(key)
        if value:
            return value
    except CryptoUnavailableError:
        pass  # нет master key — штатный режим, читаем legacy
    except IntegrityError:
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).error(
            "secret_store: integrity check failed for %r — falling back "
            "to legacy plaintext (value not logged)", key,
        )
    return legacy_plaintext


# ─── FILE-секреты: шифрование перед отправкой наружу (для 18.5) ─────────────


def encrypt_file_payload(data: bytes, *, rel_path: str) -> bytes:
    """Зашифровать содержимое FILE-секрета перед выгрузкой (18.4 §2 / 18.5).

    Возвращает JSON-байты envelope: именно ЭТО (и только это) может
    попасть в S3 для файла категории ``secrets``. aad = относительный
    путь файла — подмена объекта между ключами ловится на расшифровке.
    """
    envelope = encrypt(data, aad=f"file:{rel_path}")
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


def decrypt_file_payload(payload: bytes, *, rel_path: str) -> bytes:
    """Обратная операция для :func:`encrypt_file_payload`."""
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IntegrityError("повреждённый envelope файла") from exc
    return decrypt(envelope, aad=f"file:{rel_path}")


# ─── async-обёртки (большие значения — не блокировать loop) ─────────────────


async def put_secret_async(
    key: str, plaintext: str | bytes, *, migrated_from: str = "state",
) -> None:
    await asyncio.to_thread(
        put_secret, key, plaintext, migrated_from=migrated_from,
    )


async def get_secret_async(key: str) -> str | None:
    return await asyncio.to_thread(get_secret, key)
