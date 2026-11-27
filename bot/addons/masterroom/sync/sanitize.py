# Write-time sanitiser for sync error texts (ЧАСТЬ 18.2).
# Co-authored with CoCo
"""Санитизация текстов ошибок sync ДО записи в state (18.2 §2).

Гарантия: в ``last_error_public`` (и вообще в state / UI) никогда не
попадают connection string, токены, cookie, пароли и user:pass@host.
Санитизация выполняется НА ЗАПИСИ (`state.set_sync_last_error_public`),
а не на выводе — так экран может честно печатать то, что лежит в state.

Модуль без побочных эффектов на import и без внешних зависимостей.
"""

from __future__ import annotations

import re

REDACTED = "[скрыто]"

# Любой URL со схемой: postgres://, postgresql://, mysql://, redis://,
# https:// … — connection string почти всегда именно такой формы, а
# «обычные» URL в текстах ошибок тоже могут нести токены в query.
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>]+")

# userinfo@host без схемы: «user:pa$$w0rd@db.example.com».
_USERINFO_RE = re.compile(r"\b[\w.+\-]+:[^@\s\"'<>]+@[\w.\-]+(?::\d+)?")

# key=value / key: value с секретным именем ключа.
_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_\-]?key|apikey|"
    r"cookie|set-cookie|authorization|auth|access[_\-]?key|session)\b"
    r"\s*[=:]\s*[^\s;,\"'<>]+"
)

# AWS Access Key ID: ровно 20 символов (AKIA/ASIA/…) — короче порога
# _LONG_TOKEN_RE, поэтому ловится отдельным паттерном (приёмка 18.4 §4).
_AWS_KEY_RE = re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA|AGPA|"
                         r"AIDA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{16}\b")

# Длинные непрерывные base64/hex-подобные блоки — типичные токены.
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}={0,2}\b")

_MAX_LEN = 400


def sanitize_error_public(text: str | None, max_len: int = _MAX_LEN) -> str:
    """Вернуть безопасный публичный текст ошибки.

    Вырезает connection string / URL, userinfo@host, пары
    password=…/token=…/cookie=… и длинные токеноподобные строки,
    затем обрезает до ``max_len`` символов.
    """
    s = (text or "").strip()
    if not s:
        return ""
    s = _URL_RE.sub(REDACTED, s)
    s = _USERINFO_RE.sub(REDACTED, s)
    s = _KV_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", s)
    s = _AWS_KEY_RE.sub(REDACTED, s)
    s = _LONG_TOKEN_RE.sub(REDACTED, s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s
