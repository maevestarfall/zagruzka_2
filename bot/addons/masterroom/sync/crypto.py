# Криптомодуль sync: argon2id-хеши паролей + envelope encryption (ЧАСТЬ 18.4).
# Co-authored with CoCo
"""Криптомодуль защиты учётных данных (ЧАСТЬ 18.4 §1, п.11.5 / 18.D ТЗ).

Два независимых механизма:

* **Пароли пользователей сайта** (если проходят через бот) — ТОЛЬКО
  argon2id-хеши (``argon2-cffi``). Plaintext-пароль никогда не хранится:
  ни локально, ни во внешней БД, ни в журнале.
* **API-токены, пароли внешних сервисов, cookie, session state** — ТОЛЬКО
  envelope encryption:

  - master key — из env-переменной ``SYNC_MASTER_KEY`` (base64, 32 байта);
    в state допустима только СТРОКА-ИМЯ переменной (паттерн ``token_ref``).
    Master key НИКОГДА не пишется в state.json / логи / журнал / ciphertext;
  - на КАЖДУЮ запись — свой случайный data key (32 байта), зашифрованный
    (wrapped) master key;
  - AEAD AES-256-GCM (`cryptography`): nonce уникален на КАЖДУЮ операцию
    шифрования (`os.urandom`), ciphertext несёт auth tag; при провале
    проверки целостности — :class:`IntegrityError`, а не «тихое» чтение
    мусора.

Библиотеки ``cryptography`` и ``argon2-cffi`` импортируются ЛЕНИВО
(правило 18.3 §1: бюджет памяти Render 512MB, импорт при старте не нужен).
Никакой самодельной криптографии: только штатные примитивы библиотек.

Отсутствие master key — штатный режим: :func:`is_available` возвращает
``False``, категория ``secrets`` помечается «недоступна для синхронизации»,
бот НЕ падает.

Блокирующие операции (argon2, шифрование больших значений) — через
``asyncio.to_thread`` (паттерн ``bot/tts.py`` / ``bot/wizard.py``):
см. async-обёртки внизу модуля.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
from typing import Any

# Имя env-переменной master key. В state может лежать только ЭТА строка
# (имя), никогда — значение ключа.
MASTER_KEY_ENV = "SYNC_MASTER_KEY"

_MASTER_KEY_LEN = 32   # AES-256
_DATA_KEY_LEN = 32     # AES-256
_NONCE_LEN = 12        # GCM standard nonce

ENVELOPE_VERSION = 1
ENVELOPE_ALG = "AES-256-GCM"


class CryptoUnavailableError(RuntimeError):
    """Криптография недоступна: нет master key или не установлена библиотека."""


class IntegrityError(ValueError):
    """Проверка целостности ciphertext провалена (подмена/порча данных)."""


# ─── master key ──────────────────────────────────────────────────────────────


def _load_master_key() -> bytes | None:
    """Прочитать master key из env. ``None`` — не задан/невалиден.

    Значение читается напрямую из ``os.environ`` (а не из снапшота
    ``bot.config``), чтобы ключ можно было добавить/убрать без пересборки
    и чтобы значение нигде не кэшировалось дольше необходимого.
    """
    raw = (os.environ.get(MASTER_KEY_ENV) or "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(key) != _MASTER_KEY_LEN:
        return None
    return key


def _aesgcm(key: bytes):
    """Ленивый импорт AESGCM (правило 18.3 §1)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import (  # noqa: PLC0415
            AESGCM,
        )
    except ImportError as exc:  # pragma: no cover — зависимость в requirements
        raise CryptoUnavailableError(
            "библиотека cryptography не установлена"
        ) from exc
    return AESGCM(key)


def is_available() -> bool:
    """True, если envelope encryption готова: есть master key и библиотека."""
    key = _load_master_key()
    if key is None:
        return False
    try:
        _aesgcm(key)
    except CryptoUnavailableError:
        return False
    return True


def unavailable_reason() -> str:
    """Безопасная (без секретов) причина недоступности шифрования."""
    if _load_master_key() is None:
        raw = (os.environ.get(MASTER_KEY_ENV) or "").strip()
        if not raw:
            return f"не задан master key ({MASTER_KEY_ENV})"
        return f"{MASTER_KEY_ENV} не является base64-ключом на 32 байта"
    try:
        _aesgcm(b"\x00" * _MASTER_KEY_LEN)
    except CryptoUnavailableError:
        return "библиотека cryptography не установлена"
    return ""


# ─── envelope encryption ─────────────────────────────────────────────────────


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise IntegrityError("повреждённое base64-поле envelope") from exc


def encrypt(plaintext: bytes | str, *, aad: str = "") -> dict[str, Any]:
    """Зашифровать значение envelope-схемой. Возвращает JSON-safe dict.

    Схема: свежий data key на запись; data key шифруется master key
    (wrap) со СВОИМ уникальным nonce; значение шифруется data key со
    своим уникальным nonce. Оба nonce — ``os.urandom`` на каждую
    операцию, повторное шифрование того же значения даёт другой
    ciphertext (приёмка 18.4).

    ``aad`` — additional authenticated data (например, ключ записи):
    привязывает ciphertext к записи, подмена местами двух ciphertext
    ловится проверкой целостности.
    """
    master = _load_master_key()
    if master is None:
        raise CryptoUnavailableError(
            f"master key не задан ({MASTER_KEY_ENV})"
        )
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    aad_b = aad.encode("utf-8") if aad else None

    data_key = os.urandom(_DATA_KEY_LEN)
    key_nonce = os.urandom(_NONCE_LEN)
    wrapped_key = _aesgcm(master).encrypt(key_nonce, data_key, None)

    nonce = os.urandom(_NONCE_LEN)
    ct = _aesgcm(data_key).encrypt(nonce, plaintext, aad_b)

    return {
        "v": ENVELOPE_VERSION,
        "alg": ENVELOPE_ALG,
        "key_nonce": _b64e(key_nonce),
        "wrapped_key": _b64e(wrapped_key),
        "nonce": _b64e(nonce),
        "ct": _b64e(ct),
        "aad": aad or "",
    }


def decrypt(envelope: dict[str, Any], *, aad: str | None = None) -> bytes:
    """Расшифровать envelope. Провал целостности → :class:`IntegrityError`.

    ``aad`` по умолчанию берётся из самого envelope; можно передать
    явно, чтобы дополнительно проверить привязку к ожидаемой записи.
    """
    master = _load_master_key()
    if master is None:
        raise CryptoUnavailableError(
            f"master key не задан ({MASTER_KEY_ENV})"
        )
    if not isinstance(envelope, dict):
        raise IntegrityError("envelope не является объектом")
    if envelope.get("v") != ENVELOPE_VERSION or envelope.get("alg") != ENVELOPE_ALG:
        raise IntegrityError("неизвестная версия/алгоритм envelope")

    expected_aad = envelope.get("aad") or "" if aad is None else aad
    aad_b = expected_aad.encode("utf-8") if expected_aad else None

    key_nonce = _b64d(str(envelope.get("key_nonce") or ""))
    wrapped_key = _b64d(str(envelope.get("wrapped_key") or ""))
    nonce = _b64d(str(envelope.get("nonce") or ""))
    ct = _b64d(str(envelope.get("ct") or ""))

    try:
        from cryptography.exceptions import InvalidTag  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise CryptoUnavailableError(
            "библиотека cryptography не установлена"
        ) from exc

    try:
        data_key = _aesgcm(master).decrypt(key_nonce, wrapped_key, None)
    except InvalidTag as exc:
        raise IntegrityError(
            "провал проверки целостности wrapped data key"
        ) from exc
    try:
        return _aesgcm(data_key).decrypt(nonce, ct, aad_b)
    except InvalidTag as exc:
        raise IntegrityError("провал проверки целостности ciphertext") from exc


def decrypt_text(envelope: dict[str, Any], *, aad: str | None = None) -> str:
    return decrypt(envelope, aad=aad).decode("utf-8")


# ─── argon2id-хеши паролей ──────────────────────────────────────────────────


def _password_hasher():
    """Ленивый импорт argon2-cffi. По умолчанию — argon2id."""
    try:
        from argon2 import PasswordHasher  # noqa: PLC0415
        from argon2.low_level import Type  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — зависимость в requirements
        raise CryptoUnavailableError(
            "библиотека argon2-cffi не установлена"
        ) from exc
    # Явный argon2id (Type.ID — и так default у argon2-cffi, но фиксируем).
    return PasswordHasher(type=Type.ID)


def hash_password(password: str) -> str:
    """argon2id-хеш пароля. НИКОГДА не хранить plaintext-пароль."""
    if not password:
        raise ValueError("пустой пароль")
    return _password_hasher().hash(password)


def verify_password(hashed: str, password: str) -> bool:
    """Сверить пароль с argon2id-хешем. Ошибки → False (без исключений)."""
    try:
        from argon2.exceptions import (  # noqa: PLC0415
            InvalidHashError,
            VerificationError,
        )
    except ImportError as exc:  # pragma: no cover
        raise CryptoUnavailableError(
            "библиотека argon2-cffi не установлена"
        ) from exc
    try:
        return _password_hasher().verify(hashed, password)
    except (VerificationError, InvalidHashError, ValueError):
        return False


# ─── async-обёртки (не блокировать event loop) ───────────────────────────────


async def encrypt_async(plaintext: bytes | str, *, aad: str = "") -> dict[str, Any]:
    return await asyncio.to_thread(encrypt, plaintext, aad=aad)


async def decrypt_async(
    envelope: dict[str, Any], *, aad: str | None = None,
) -> bytes:
    return await asyncio.to_thread(decrypt, envelope, aad=aad)


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(hashed: str, password: str) -> bool:
    return await asyncio.to_thread(verify_password, hashed, password)
