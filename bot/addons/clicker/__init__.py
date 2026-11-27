"""«🤖 Макрос действий (Кликер)» — manual click/copy/pause macros.

This surfaces (under the «🧖 баня» screen) a feature that lived only in an
older build: a manual action-macro the owner writes as plain lines —

    Клик: Запустить проверку
    Пауза: 3
    Скопировать: Номер карты
    Клик: Очистить

The bot then walks the steps as a visual simulation in chat (⏳ → ✅),
honouring the pauses. It also hands out a browser macro-RECORDER
(Tampermonkey userscript) so the owner can record clicks/copies on a real
site (F12/F9), replay them, and copy a ready macro back into the bot.

``parse_macro`` is pure and unit-testable (no aiogram).
"""

from __future__ import annotations

import re

ADDON = "clicker"

# Recognised line prefixes (case-insensitive, ru/en synonyms).
_CLICK = ("клик", "нажми", "нажать", "тык", "click", "press", "tap")
_PAUSE = ("пауза", "подожди", "ждать", "жди", "wait", "sleep", "delay")
_COPY = ("скопировать", "копировать", "копируй", "copy")
_TYPE = ("ввести", "введи", "впиши", "вписать", "вводить", "type", "enter", "fill")
_FILE = ("файл", "загрузи", "загрузить", "прикрепи", "прикрепить", "upload", "attach")
_GOTO = ("перейти", "открой", "открыть", "сайт", "goto", "open", "url")
_LOGIN = ("логин", "login", "юзернейм", "username")
_PASSWORD = ("пароль", "password", "пасс")


def _strip_prefix(line: str) -> tuple[str, str]:
    """Split ``'Клик: текст'`` → ``('клик', 'текст')``. No colon → ('', line)."""
    m = re.match(r"\s*([A-Za-zА-Яа-яЁё ]+?)\s*[:：\-—]\s*(.*)$", line)
    if m:
        return m.group(1).strip().lower(), m.group(2).strip()
    return "", line.strip()


def _kind(word: str) -> str | None:
    """Classify a single keyword token → 'click'/'pause'/'copy'/None."""
    w = (word or "").strip().lower().rstrip(":：")
    if not w:
        return None
    if any(w.startswith(p) for p in _PAUSE):
        return "pause"
    if any(w.startswith(p) for p in _COPY):
        return "copy"
    if any(w.startswith(p) for p in _PASSWORD):
        return "type_password"
    if any(w.startswith(p) for p in _LOGIN):
        return "type_login"
    if any(w.startswith(p) for p in _FILE):
        return "upload"
    if any(w.startswith(p) for p in _TYPE):
        return "type"
    if any(w.startswith(p) for p in _GOTO):
        return "goto"
    if any(w.startswith(p) for p in _CLICK):
        return "click"
    return None


def _split_type(rest: str) -> tuple[str, str]:
    """Parse the text/field of a type step.

    Supports ``текст | поле`` (pipe) and ``текст в поле`` (last « в »).
    Returns ``(text, field)`` — field may be empty.
    """
    if "|" in rest:
        text, field = rest.split("|", 1)
        return text.strip(), field.strip()
    m = re.search(r"^(.*)\s+в\s+([^ ].*)$", rest, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return rest.strip(), ""


def parse_macro(text: str) -> list[dict]:
    """Parse a free-text macro into ordered steps.

    Each step is one of::

        {"op": "click", "label": "..."}
        {"op": "copy",  "label": "..."}
        {"op": "pause", "seconds": 3.0}

    Recognises both ``Клик: текст`` (colon) and ``нажми текст`` (leading
    verb, no colon), ru/en synonyms. Unrecognised non-empty lines are
    treated as a click on that text (so a bare list of button names works).
    """
    steps: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        prefix, rest = _strip_prefix(line)
        kind = _kind(prefix)
        if kind is None:
            # No colon-prefix matched — try a leading verb without a colon.
            parts = line.split(None, 1)
            lead = _kind(parts[0]) if parts else None
            if lead is not None:
                kind = lead
                rest = parts[1].strip() if len(parts) > 1 else ""
        if kind == "pause":
            num = re.search(r"[\d.,]+", rest or line)
            try:
                secs = float(num.group(0).replace(",", ".")) if num else 1.0
            except (ValueError, AttributeError):
                secs = 1.0
            steps.append({"op": "pause", "seconds": max(0.0, min(secs, 120.0))})
        elif kind == "copy":
            steps.append({"op": "copy", "label": rest or line})
        elif kind == "goto":
            steps.append({"op": "goto", "url": rest or line})
        elif kind == "type":
            text_val, field = _split_type(rest or line)
            steps.append({"op": "type", "text": text_val, "field": field})
        elif kind == "type_login":
            steps.append({"op": "type_login", "field": (rest or "").strip()})
        elif kind == "type_password":
            steps.append({"op": "type_password", "field": (rest or "").strip()})
        elif kind == "upload":
            steps.append({"op": "upload", "file": (rest or line).strip()})
        elif kind == "click":
            steps.append({"op": "click", "label": rest or line})
        else:
            # Bare line → treat as a click target.
            steps.append({"op": "click", "label": line})
    return steps


def describe_step(step: dict) -> str:
    op = step.get("op")
    if op == "pause":
        return f"⏸ Пауза {step.get('seconds', 0):g} сек"
    if op == "copy":
        return f"📋 Скопировать: {step.get('label', '')}"
    if op == "goto":
        return f"🌐 Перейти: {step.get('url', '')}"
    if op == "type":
        field = step.get("field") or "поле"
        return f"⌨️ Ввести «{step.get('text', '')}» в «{field}»"
    if op == "type_login":
        return f"🔑 Ввести логин в «{step.get('field') or 'поле логина'}»"
    if op == "type_password":
        return f"🔑 Ввести пароль в «{step.get('field') or 'поле пароля'}»"
    if op == "upload":
        return f"📎 Файл: {step.get('file', '')}"
    return f"👆 Клик: {step.get('label', '')}"


def build_clicker_router():
    from .handlers import build_clicker_router as _b

    return _b()


__all__ = ["ADDON", "parse_macro", "describe_step", "build_clicker_router"]
