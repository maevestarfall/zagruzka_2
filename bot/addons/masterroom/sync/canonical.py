# Canonical JSON (RFC 8785 profile) + SHA-256 checksums (ЧАСТЬ 18.5).
# Co-authored with CoCo
"""Каноническая сериализация JSON и checksums (ЧАСТЬ 18.0 п.6, 18.5 §3).

Контракт подтверждения read-after-write: checksum — SHA-256 от UTF-8
RFC 8785 canonical JSON БЕЗ поля ``checksum``. Оба конца сверки (то, что
отправили, и то, что прочитали назад из внешней БД) прогоняются через
ОДНУ И ТУ ЖЕ функцию :func:`canonical_json_bytes`, поэтому порядок
ключей / пробелы / прочие представления не влияют на результат.

Профиль RFC 8785 (JCS), реализованный здесь:

* объекты — ключи сортируются по UTF-16 code units (как в RFC);
* строки — минимальный escaping JSON (управляющие символы, ``"`` и
  ``\\``), без ``ensure_ascii``;
* числа — целые как десятичные; float с целым значением — без дробной
  части (``10.0`` → ``"10"``), остальные — кратчайшее round-trip
  представление Python (совпадает с ES6 для типичных значений);
  NaN/Infinity запрещены (это и так не-JSON);
* без пробелов между токенами.

Данные бота приходят из ``state.json`` (обычный JSON), поэтому профиль
покрывает все реальные значения. Модуль без внешних зависимостей и без
побочных эффектов на import.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

__all__ = [
    "canonical_json_bytes",
    "canonical_json_str",
    "checksum_of",
    "payload_checksum",
    "payload_byte_size",
]

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _ser_str(s: str) -> str:
    out = ['"']
    for ch in s:
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < "\x20":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _ser_number(x: int | float) -> str:
    if isinstance(x, bool):  # pragma: no cover — отсечено раньше
        raise TypeError("bool не число")
    if isinstance(x, int):
        return str(x)
    if math.isnan(x) or math.isinf(x):
        raise ValueError("NaN/Infinity недопустимы в canonical JSON")
    if x == int(x) and abs(x) < 1e21:
        return str(int(x))
    # Кратчайшее round-trip представление; для типичных значений
    # совпадает с сериализацией ES6 (RFC 8785 §3.2.2.3).
    r = repr(x)
    if "e" in r:
        mant, _, exp = r.partition("e")
        exp_i = int(exp)
        r = f"{mant}e{'+' if exp_i >= 0 else '-'}{abs(exp_i)}"
    return r


def _ser(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_ser_str(value))
    elif isinstance(value, (int, float)):
        out.append(_ser_number(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _ser(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        # RFC 8785: сортировка ключей по UTF-16 code units.
        keys = sorted(
            (str(k) for k in value.keys()),
            key=lambda k: k.encode("utf-16-be"),
        )
        for i, k in enumerate(keys):
            if i:
                out.append(",")
            out.append(_ser_str(k))
            out.append(":")
            _ser(value[k], out)
        out.append("}")
    else:
        raise TypeError(
            f"тип {type(value).__name__} не сериализуется в canonical JSON"
        )


def canonical_json_str(value: Any) -> str:
    out: list[str] = []
    _ser(value, out)
    return "".join(out)


def canonical_json_bytes(value: Any) -> bytes:
    """UTF-8 байты канонического JSON значения."""
    return canonical_json_str(value).encode("utf-8")


def checksum_of(value: Any) -> str:
    """SHA-256 hex канонического JSON значения (как есть)."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def payload_checksum(payload: dict[str, Any]) -> str:
    """Checksum payload'а операции: поле ``checksum`` исключается (18.0 п.6)."""
    scrubbed = {k: v for k, v in payload.items() if k != "checksum"}
    return checksum_of(scrubbed)


def payload_byte_size(payload: dict[str, Any]) -> int:
    """Byte size payload'а (canonical, без поля ``checksum``) для сверки."""
    scrubbed = {k: v for k, v in payload.items() if k != "checksum"}
    return len(canonical_json_bytes(scrubbed))
