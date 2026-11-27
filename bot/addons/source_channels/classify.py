"""AI classification of a captured post into one of four buckets.

Primary path: ask the bot's own LLM brain (``addons._llm.chat_once``) to
return strict JSON. If no brain is configured or the call fails, fall
back to fast regex heuristics so the feature still works offline.

Output dict::

    {
        "category": "check" | "media" | "code" | "money",
        "is_instruction": bool,
        "summary": str,        # one short line for the report
        "money_kind": "card" | "api_key" | "",  # only when category==money
    }
"""

from __future__ import annotations

import json
import logging
import re

from . import store

logger = logging.getLogger(__name__)

# ── heuristics ─────────────────────────────────────────────────────────

_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_CVV_RE = re.compile(r"\bcvv\b|\bcvc\b", re.IGNORECASE)
_EXPIRY_RE = re.compile(r"\b(0[1-9]|1[0-2])\s*[/\-]\s*\d{2,4}\b")
_API_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|api[_-]?key|bearer\s+[A-Za-z0-9._\-]{16,}"
    r"|AIza[0-9A-Za-z\-_]{20,}|ghp_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}"
    r"|xai-[A-Za-z0-9]{16,}|[A-Za-z0-9]{32,})",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```|</?[a-z]+>|def\s+\w+\(|function\s+\w+\(|import\s+\w+|#include\b|console\.log|System\.out")
_MONEY_WORD_RE = re.compile(
    r"карт|card|деньг|money|оплат|payment|перевод|реквизит|iban|paypal|"
    r"qiwi|сбер|тинькоф|тинькофф|visa|mastercard|мир\b",
    re.IGNORECASE,
)
_INSTRUCTION_RE = re.compile(
    r"^\s*(сделай|выполни|запусти|создай|добавь|удали|сгенерируй|настрой|"
    r"проверь|отправь|опубликуй|собери|найди|do |make |run |create |build |"
    r"please |нужно|надо|задача:|task:|инструкция)",
    re.IGNORECASE,
)


def _heuristic(text: str, *, has_media: bool) -> dict:
    t = text or ""
    is_instr = bool(_INSTRUCTION_RE.search(t.strip()))

    # money / cards / api keys take priority — they go to the safe.
    digits = re.sub(r"[ -]", "", t)
    card_like = bool(_CARD_RE.search(t)) and len(re.sub(r"\D", "", digits)) >= 13
    if card_like or _CVV_RE.search(t) or _EXPIRY_RE.search(t):
        return {"category": store.CAT_MONEY, "is_instruction": is_instr,
                "summary": t[:80] or "карта", "money_kind": "card"}
    if _API_RE.search(t) and (
        "key" in t.lower() or "sk-" in t.lower() or "token" in t.lower()
        or "ghp_" in t.lower() or "aiza" in t.lower() or "hf_" in t.lower()
    ):
        return {"category": store.CAT_MONEY, "is_instruction": is_instr,
                "summary": t[:80] or "api-ключ", "money_kind": "api_key"}
    if _MONEY_WORD_RE.search(t) and card_like:
        return {"category": store.CAT_MONEY, "is_instruction": is_instr,
                "summary": t[:80], "money_kind": "card"}

    if _CODE_FENCE_RE.search(t):
        return {"category": store.CAT_CODE, "is_instruction": is_instr,
                "summary": t[:80] or "код", "money_kind": ""}

    if has_media:
        return {"category": store.CAT_MEDIA, "is_instruction": is_instr,
                "summary": t[:80] or "медиа", "money_kind": ""}

    return {"category": store.CAT_CHECK, "is_instruction": is_instr,
            "summary": t[:80] or "проверить", "money_kind": ""}


_SYSTEM = (
    "Ты — фильтр входящих сообщений из каналов-источников. "
    "Тебе дают текст поста (и пометку, есть ли фото/видео). "
    "Верни СТРОГО JSON без пояснений вида: "
    '{"category":"check|media|code|money","is_instruction":true|false,'
    '"summary":"короткая строка до 80 символов","money_kind":"card|api_key|"}.\n'
    "Категории:\n"
    "- money: банковская карта, реквизиты, деньги, ИЛИ api-ключ/токен "
    "(тогда money_kind = card или api_key);\n"
    "- code: фрагмент кода, скрипт, конфиг;\n"
    "- media: пост про фото/видео или с прикреплённым фото/видео;\n"
    "- check: всё остальное, что требует ручной проверки.\n"
    "is_instruction = true, если это команда что-то сделать (инструкция к исполнению)."
)


async def classify(text: str, *, has_media: bool) -> dict:
    """Classify one captured post. Never raises — always returns a dict."""
    text = text or ""
    try:
        from .._llm import chat_once, NoBrainError
    except Exception:  # noqa: BLE001
        return _heuristic(text, has_media=has_media)

    user = (
        f"Есть фото/видео: {'да' if has_media else 'нет'}\n"
        f"Текст поста:\n{text[:1500] if text else '(пусто)'}"
    )
    try:
        raw = await chat_once(_SYSTEM, user, max_tokens=200, temperature=0.0)
    except NoBrainError:
        return _heuristic(text, has_media=has_media)
    except Exception as exc:  # noqa: BLE001
        logger.warning("source_channels classify LLM failed: %s", exc)
        return _heuristic(text, has_media=has_media)

    data = _parse_json(raw)
    if not data:
        return _heuristic(text, has_media=has_media)

    cat = str(data.get("category", "")).lower().strip()
    if cat not in store.ALL_CATEGORIES:
        return _heuristic(text, has_media=has_media)
    return {
        "category": cat,
        "is_instruction": bool(data.get("is_instruction")),
        "summary": (str(data.get("summary", "")) or text[:80])[:120],
        "money_kind": str(data.get("money_kind", "")).lower().strip(),
    }


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
